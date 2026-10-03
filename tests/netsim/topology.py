#!/usr/bin/env python3
"""A simulated home network built from Linux network namespaces.

    client ─(wifi)─ router ─(access)─ isp ─(upstream)─ internet
    .50       emu    .1   100.64.0.2/.1    203.0.113.1/.2   1.1.1.1, 8.8.8.8

Each bracketed link runs through ``linkemu.py`` so a test can add delay, jitter
and loss exactly where it wants: on the Wi-Fi, on the ISP access line, or
further upstream. Needs root (or CAP_NET_ADMIN) and iproute2.

    sudo python3 tests/netsim/topology.py up
    sudo python3 tests/netsim/topology.py impair wifi --jitter 40 --loss 2
    sudo python3 tests/netsim/topology.py down
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PREFIX = "cg"
NS = {name: f"{PREFIX}{name}" for name in ("client", "router", "isp", "inet",
                                           "wifi", "access", "upstream")}
LINKS = {
    # emulator ns: (left ns, left addr, right ns, right addr)
    "wifi": ("client", "192.168.1.50/24", "router", "192.168.1.1/24"),
    "access": ("router", "100.64.0.2/30", "isp", "100.64.0.1/30"),
    "upstream": ("isp", "203.0.113.1/30", "inet", "203.0.113.2/30"),
}
STATE = Path(os.environ.get("CG_NETSIM_STATE", "/tmp/callglance-netsim"))
PYTHON = os.environ.get("CG_NETSIM_PYTHON", sys.executable)


def sh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=check, capture_output=True, text=True)


def nsexec(ns: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return sh("ip", "netns", "exec", NS[ns], *args, check=check)


def available() -> str | None:
    """None if the simulator can run here, otherwise the reason it cannot."""
    if os.geteuid() != 0:
        return "needs root"
    if not shutil.which("ip"):
        return "needs iproute2"
    probe = f"{PREFIX}probe{os.getpid()}"
    result = sh("ip", "netns", "add", probe, check=False)
    if result.returncode != 0:
        return f"cannot create network namespaces: {result.stderr.strip()}"
    sh("ip", "netns", "del", probe, check=False)
    return None


def _no_offload(ns: str, dev: str) -> None:
    # The emulator re-injects frames; checksums must already be filled in.
    if shutil.which("ethtool"):
        nsexec(ns, "ethtool", "-K", dev, "tx", "off", "rx", "off", check=False)


def _spawn(ns: str, *args: str, name: str) -> None:
    log = open(STATE / f"{name}.log", "w")
    proc = subprocess.Popen(["ip", "netns", "exec", NS[ns], *args], stdout=log,
                            stderr=subprocess.STDOUT, start_new_session=True)
    (STATE / f"{name}.pid").write_text(str(proc.pid))


def up() -> None:
    down(quiet=True)
    STATE.mkdir(parents=True, exist_ok=True)
    for ns in NS.values():
        sh("ip", "netns", "add", ns)
    for ns in NS:
        nsexec(ns, "ip", "link", "set", "lo", "up")
        nsexec(ns, "sysctl", "-qw", "net.ipv6.conf.all.disable_ipv6=1", check=False)

    for link, (left, laddr, right, raddr) in LINKS.items():
        lname, rname = f"{link[:3]}L", f"{link[:3]}R"  # devices on the endpoint side
        eleft, eright = f"{link[:3]}a", f"{link[:3]}b"  # devices inside the emulator ns
        sh("ip", "link", "add", lname, "netns", NS[left], "type", "veth", "peer", "name",
           eleft, "netns", NS[link])
        sh("ip", "link", "add", rname, "netns", NS[right], "type", "veth", "peer", "name",
           eright, "netns", NS[link])
        nsexec(left, "ip", "addr", "add", laddr, "dev", lname)
        nsexec(right, "ip", "addr", "add", raddr, "dev", rname)
        for ns, dev in ((left, lname), (right, rname), (link, eleft), (link, eright)):
            nsexec(ns, "ip", "link", "set", dev, "up")
            _no_offload(ns, dev)
        ctl = STATE / f"{link}.json"
        ctl.write_text(json.dumps({"delay_ms": 0, "jitter_ms": 0, "loss_pct": 0}))
        _spawn(link, PYTHON, str(HERE / "linkemu.py"), eleft, eright, str(ctl), name=f"emu-{link}")

    # Addresses of the "internet" and routing.
    nsexec("inet", "ip", "addr", "add", "1.1.1.1/32", "dev", "lo")
    nsexec("inet", "ip", "addr", "add", "8.8.8.8/32", "dev", "lo")
    nsexec("client", "ip", "route", "add", "default", "via", "192.168.1.1")
    nsexec("router", "ip", "route", "add", "default", "via", "100.64.0.1")
    nsexec("isp", "ip", "route", "add", "192.168.1.0/24", "via", "100.64.0.2")
    nsexec("isp", "ip", "route", "add", "default", "via", "203.0.113.2")
    nsexec("inet", "ip", "route", "add", "default", "via", "203.0.113.1")
    for ns in ("router", "isp"):
        nsexec(ns, "sysctl", "-qw", "net.ipv4.ip_forward=1")
    # Real home routers answer DNS; public resolvers obviously do.
    _spawn("router", PYTHON, str(HERE / "dnsd.py"), "192.168.1.1", name="dns-router")
    _spawn("inet", PYTHON, str(HERE / "dnsd.py"), "--identity=SIM", "1.1.1.1", "8.8.8.8",
           name="dns-inet")
    allow_ping(True)

    # Wait until the whole path forwards packets.
    deadline = time.time() + 10
    while time.time() < deadline:
        if nsexec("client", "ping", "-c", "1", "-W", "1", "8.8.8.8", check=False).returncode == 0:
            return
        time.sleep(0.2)
    raise RuntimeError("simulated network did not come up")


def allow_ping(allowed: bool) -> None:
    """Toggle unprivileged ICMP sockets for the client (systemd's default is allowed)."""
    value = "0 2147483647" if allowed else "1 0"
    nsexec("client", "sysctl", "-qw", f"net.ipv4.ping_group_range={value}")


def impair(link: str, delay: float = 0.0, jitter: float = 0.0, loss: float = 0.0) -> None:
    """Per-direction impairment on a link (round-trip loss is roughly double)."""
    ctl = STATE / f"{link}.json"
    tmp = ctl.with_suffix(".tmp")
    tmp.write_text(json.dumps({"delay_ms": delay, "jitter_ms": jitter, "loss_pct": loss}))
    os.replace(tmp, ctl)


def clear() -> None:
    for link in LINKS:
        impair(link)


def down(quiet: bool = False) -> None:
    if STATE.exists():
        for pidfile in STATE.glob("*.pid"):
            try:
                os.killpg(int(pidfile.read_text()), signal.SIGTERM)
            except (OSError, ValueError):
                pass
            pidfile.unlink(missing_ok=True)
    for ns in NS.values():
        sh("ip", "netns", "del", ns, check=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("up")
    sub.add_parser("down")
    sub.add_parser("clear")
    p = sub.add_parser("impair")
    p.add_argument("link", choices=sorted(LINKS))
    p.add_argument("--delay", type=float, default=0.0)
    p.add_argument("--jitter", type=float, default=0.0)
    p.add_argument("--loss", type=float, default=0.0)
    p = sub.add_parser("ping")
    p.add_argument("state", choices=["on", "off"])
    args = parser.parse_args()
    if args.cmd == "up":
        up()
    elif args.cmd == "down":
        down()
    elif args.cmd == "clear":
        clear()
    elif args.cmd == "impair":
        impair(args.link, args.delay, args.jitter, args.loss)
    elif args.cmd == "ping":
        allow_ping(args.state == "on")


if __name__ == "__main__":
    main()
