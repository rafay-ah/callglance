"""Command-line entry point: ``callglance [command]``."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import time

from callglance import APP_ID, DBUS_IFACE, DBUS_PATH, __version__

USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
COLORS = {"good": "32", "fair": "33", "poor": "31", "offline": "31", "unknown": "90"}


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


def _bold(text: str) -> str:
    return _c(text, "1")


def _ms(value) -> str:
    if value is None:
        return "–"
    return f"{value:.0f} ms" if value >= 10 else f"{value:.1f} ms"


def _pct(value) -> str:
    if value is None:
        return "–"
    return "0%" if value == 0 else f"{value:.1f}%"


# -- talking to the running service ------------------------------------------------------

def _call_service(method: str, *args, timeout_ms: int = 2000):
    """Call the running service; None if it is not running (or no session bus)."""
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
    except (ImportError, ValueError):
        return None
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        params = None
        if args:
            sig = "".join("u" if isinstance(a, int) else "s" for a in args)
            params = GLib.Variant(f"({sig})", args)
        reply = bus.call_sync(APP_ID, DBUS_PATH, DBUS_IFACE, method, params, None,
                              Gio.DBusCallFlags.NO_AUTO_START, timeout_ms, None)
        return reply.unpack()
    except Exception:
        return None


def _measure_locally(seconds: float, on_progress=None) -> dict:
    """Run the engine in this process for a little while (service not running)."""
    import tempfile
    from pathlib import Path

    from callglance.config import Config
    from callglance.engine import Engine

    config = Config()
    config.path = Path(tempfile.mkdtemp(prefix="callglance-cli-")) / "config.json"
    config.set("window", min(float(config["window"]), seconds), save=False)
    config.set("loss_window", seconds, save=False)
    engine = Engine(config, history=None)
    engine.start()
    start = time.monotonic()
    try:
        while time.monotonic() - start < seconds:
            if on_progress:
                on_progress(time.monotonic() - start, seconds)
            time.sleep(0.25)
        return engine.snapshot()
    finally:
        engine.stop()


# -- rendering ------------------------------------------------------------------------

def render(snap: dict) -> str:
    level = snap.get("level", "unknown")
    lines = [f"{_c('●', COLORS.get(level, '0'))} {_bold(snap.get('headline', ''))}",
             f"  {snap.get('detail', '')}", ""]
    rows = [("", "Latency", "Jitter", "Loss", "Measured with")]
    for seg in snap.get("segments", []):
        if seg["id"] == "internet":
            targets = seg.get("targets") or []
            via = ", ".join(f"{t['address']} ({t['method']})" for t in targets)
        else:
            via = f"{seg.get('address') or '–'} ({seg.get('method') or '–'})"
        mark = {"origin": " ◀ starts here", "affected": ""}.get(seg.get("status"), "")
        rows.append((seg["label"], _ms(seg.get("latency_ms")), _ms(seg.get("jitter_ms")),
                     _pct(seg.get("loss_pct")), via + mark))
    widths = [max(len(r[i]) for r in rows) for i in range(4)]
    for row in rows:
        lines.append("  " + "  ".join(row[i].ljust(widths[i]) for i in range(4)) + "  " + row[4])
    dns = snap.get("dns") or {}
    if dns:
        lines.append(f"  DNS lookups: {_ms(dns.get('latency_ms'))} via {dns.get('server')}")
    wifi = snap.get("wifi") or {}
    if wifi:
        parts = [wifi.get("ssid") or "Wi-Fi"]
        if wifi.get("signal_dbm") is not None:
            parts.append(f"{wifi['signal_dbm']} dBm ({wifi.get('quality')})")
        elif wifi.get("signal_pct") is not None:
            parts.append(f"{wifi['signal_pct']}% signal")
        if wifi.get("band"):
            parts.append(wifi["band"] + (f" ch {wifi['channel']}" if wifi.get("channel") else ""))
        if wifi.get("bitrate_mbps"):
            parts.append(f"{wifi['bitrate_mbps']:g} Mb/s")
        if wifi.get("standard"):
            parts.append(wifi["standard"])
        lines.append("  Wi-Fi: " + " · ".join(parts))
    tips = snap.get("tips") or []
    if tips:
        lines.append("")
        lines.extend(f"  → {tip}" for tip in tips)
    probe = snap.get("probe") or {}
    if probe and not probe.get("icmp"):
        lines.append("")
        lines.append(_c("  Measuring without ICMP (not permitted on this system). "
                        "Run `callglance enable-icmp` for ping-based measurements.", "90"))
    return "\n".join(lines)


# -- commands ---------------------------------------------------------------------------

def cmd_status(args) -> int:
    reply = _call_service("GetSnapshot")
    snap = json.loads(reply[0]) if reply and reply[0] not in ("", "{}") else None
    if snap is None and not args.watch:
        def progress(done: float, total: float) -> None:
            if sys.stderr.isatty():
                sys.stderr.write(f"\rMeasuring your connection… {int(total - done)}s ")
                sys.stderr.flush()

        snap = _measure_locally(args.seconds, progress)
        if sys.stderr.isatty():
            sys.stderr.write("\r" + " " * 40 + "\r")
    if args.watch:
        try:
            while True:
                reply = _call_service("GetSnapshot")
                if not reply:
                    print("CallGlance is not running. Start it with `callglance`.")
                    return 1
                snap = json.loads(reply[0])
                sys.stdout.write("\033[H\033[2J" if USE_COLOR else "\n")
                print(render(snap) if not args.json else json.dumps(snap, indent=2))
                time.sleep(2)
        except KeyboardInterrupt:
            return 0
    print(json.dumps(snap, indent=2) if args.json else render(snap))
    return 0 if snap.get("level") in ("good", "unknown") else 2


def cmd_speedtest(args) -> int:
    import threading

    from callglance.config import Config
    from callglance.engine import Engine
    from callglance.speedtest import SpeedTest

    engine = Engine(Config(), history=None)
    engine.start()
    done = threading.Event()

    def update(state) -> None:
        if state.status == "running" and sys.stderr.isatty():
            live = f" {state.live_mbps:.0f} Mb/s" if state.live_mbps else ""
            sys.stderr.write(f"\r{state.phase or ''}{live} [{int(state.progress * 100):3d}%]   ")
            sys.stderr.flush()
        if state.status in ("done", "error", "cancelled"):
            if sys.stderr.isatty():
                sys.stderr.write("\r" + " " * 40 + "\r")
            result.append(state)
            done.set()

    result: list = []
    print("Measuring idle latency…", file=sys.stderr)
    time.sleep(6)  # let the engine collect an idle baseline
    test = SpeedTest(engine.internet_latency, update)
    test.start()
    try:
        done.wait()
    except KeyboardInterrupt:
        test.cancel()
        done.wait(5)
    engine.stop()
    state = result[0] if result else None
    if state is None or state.status != "done":
        print(f"Speed test failed: {state.error if state else 'cancelled'}")
        return 1
    print(_bold(state.summary))
    print(f"  Download   {state.download_mbps:.1f} Mb/s")
    print(f"  Upload     {state.upload_mbps:.1f} Mb/s")
    print(f"  Latency    {_ms(state.idle_latency_ms)} idle, {_ms(state.loaded_down_ms)} while "
          f"downloading, {_ms(state.loaded_up_ms)} while uploading")
    if state.grade:
        print(f"  Bufferbloat grade {state.grade} (+{state.bufferbloat_ms:.0f} ms under load)")
    for tip in state.tips:
        print(f"  → {tip}")
    print(_c(f"  Used about {state.data_used_mb:.0f} MB of data · speed.cloudflare.com", "90"))
    return 0


def cmd_doctor(args) -> int:
    from callglance import autostart, shellext
    from callglance.netinfo import default_route, dns_servers, iface_kind
    from callglance.probes import ping_group_range, ping_sockets_allowed

    ok, warn, bad = _c("✓", "32"), _c("!", "33"), _c("✗", "31")
    print(_bold(f"CallGlance {__version__}") + f" · Python {sys.version.split()[0]}")
    try:
        import gi

        print(f"{ok} PyGObject {gi.__version__}")
    except ImportError:
        print(f"{bad} PyGObject missing: install python3-gi")
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("AyatanaAppIndicator3", "0.1")
        print(f"{ok} GTK 3 + AyatanaAppIndicator (tray fallback)")
    except (ImportError, ValueError):
        print(f"{warn} Tray fallback unavailable: install gir1.2-gtk-3.0 and "
              "gir1.2-ayatanaappindicator3-0.1")
    rng = ping_group_range()
    if ping_sockets_allowed():
        print(f"{ok} Unprivileged ICMP allowed (ping_group_range {rng})")
    else:
        print(f"{warn} Unprivileged ICMP not allowed (ping_group_range {rng}); using UDP/TCP "
              "probes. `callglance enable-icmp` turns it on.")
    route = default_route()
    if route:
        print(f"{ok} Default route via {route.gateway or '(point-to-point)'} on {route.iface} "
              f"({iface_kind(route.iface)})")
    else:
        print(f"{bad} No default route: offline?")
    servers = dns_servers()
    print(f"{ok if servers else warn} DNS servers: {', '.join(servers) or 'none found'}")
    if route and iface_kind(route.iface) == "wifi":
        from callglance.wifi import WifiReader

        info = WifiReader().read(route.iface)
        print(f"{ok if info.source != 'none' else warn} Wi-Fi: {info.ssid or '?'} · "
              f"{info.signal_dbm if info.signal_dbm is not None else '?'} dBm · "
              f"{info.band or '?'} · {info.bitrate_mbps or '?'} Mb/s (via {info.source})")
    if shellext.is_gnome_session():
        state = shellext.shell_state()
        enabled = shellext.is_enabled_in_settings()
        where = shellext.installed_dir()
        print(f"{ok if state == 'active' else warn} GNOME Shell extension: "
              f"{state or 'shell not reachable'}, enabled={enabled}, installed at {where}")
        if state == "unknown":
            print("   Log out and back in once so GNOME Shell loads it.")
    running = _call_service("GetSettings") is not None
    print(f"{ok if running else warn} Service {'running' if running else 'not running'}")
    print(f"{ok if autostart.is_enabled() else warn} Start at login: "
          f"{'on' if autostart.is_enabled() else 'off'}")
    return 0


def cmd_autostart(args) -> int:
    from callglance import autostart

    if args.state == "status":
        print("on" if autostart.is_enabled() else "off")
        return 0
    if _call_service("SetSetting", "autostart", json.dumps(args.state == "on")) is None:
        autostart.set_enabled(args.state == "on")
    print("Start at login:", "on" if autostart.is_enabled() else "off")
    return 0


def cmd_extension(args) -> int:
    from callglance import shellext

    if args.action in ("install", "enable"):
        path = shellext.install_user_copy(force=args.action == "install")
        print(f"Extension available at {path}")
        if shellext.enable_in_settings():
            print("Enabled. If the dot does not appear in the top bar, log out and back in.")
        else:
            print("Could not enable it automatically (GNOME settings schema not found).")
        return 0
    print(f"state={shellext.shell_state()} enabled={shellext.is_enabled_in_settings()} "
          f"installed={shellext.installed_dir()}")
    return 0


SYSCTL_FILE = "/etc/sysctl.d/60-callglance-ping.conf"


def cmd_enable_icmp(args) -> int:
    from callglance.probes import ping_sockets_allowed

    if ping_sockets_allowed():
        print("Unprivileged ICMP is already allowed.")
        return 0
    content = ("# Allow unprivileged ICMP echo (\"ping sockets\") for every group. Same as\n"
               "# systemd's upstream default; installed by `callglance enable-icmp`.\n"
               "net.ipv4.ping_group_range = 0 2147483647\n")
    script = (f"printf %s {shlex.quote(content)} > {SYSCTL_FILE} && "
              f"sysctl -q -p {SYSCTL_FILE}")
    print("This lets every program send pings without special privileges (as Fedora and Arch")
    print(f"do by default). It writes {SYSCTL_FILE} and needs your password once.")
    tool = shutil.which("pkexec") or shutil.which("sudo")
    if tool is None:
        print(f"Run as root:\n  sh -c {shlex.quote(script)}")
        return 1
    result = subprocess.run([tool, "sh", "-c", script], check=False)
    if result.returncode == 0 and ping_sockets_allowed():
        print("Done. CallGlance switches to ICMP within a minute.")
        return 0
    print("Not changed.")
    return 1


def cmd_ui(args) -> int:
    from callglance.gtkui import main as ui_main

    return ui_main(show=args.show)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="callglance",
        description="Know at a glance whether your connection is good enough for calls.",
    )
    parser.add_argument("--version", action="version", version=f"CallGlance {__version__}")
    parser.add_argument("--background", action="store_true",
                        help="start without showing anything (used at login)")
    parser.add_argument("--demo", action="store_true",
                        help="simulate a connection, to try the interface")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    sub = parser.add_subparsers(dest="command", metavar="command")
    p = sub.add_parser("status", help="print the current verdict and numbers")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--watch", action="store_true", help="refresh every 2 seconds")
    p.add_argument("--seconds", type=float, default=12.0,
                   help="how long to measure if CallGlance is not running (default 12)")
    sub.add_parser("speedtest", help="run a speed test with latency under load")
    sub.add_parser("doctor", help="check what CallGlance can use on this system")
    p = sub.add_parser("autostart", help="start at login: on, off or status")
    p.add_argument("state", nargs="?", choices=["on", "off", "status"], default="status")
    p = sub.add_parser("extension", help="install or enable the GNOME Shell extension")
    p.add_argument("action", nargs="?", choices=["install", "enable", "status"],
                   default="status")
    sub.add_parser("enable-icmp", help="allow unprivileged ping (asks for your password)")
    p = sub.add_parser("ui", help=argparse.SUPPRESS)
    p.add_argument("--show", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    level = logging.WARNING - 10 * min(args.verbose, 2)
    logging.basicConfig(level=level, format="%(name)s: %(message)s")
    commands = {
        "status": cmd_status, "speedtest": cmd_speedtest, "doctor": cmd_doctor,
        "autostart": cmd_autostart, "extension": cmd_extension,
        "enable-icmp": cmd_enable_icmp, "ui": cmd_ui,
    }
    if args.command:
        return commands[args.command](args)
    try:
        from callglance import service
    except (ImportError, ValueError) as exc:
        print(f"callglance: PyGObject is required to run the app ({exc}).\n"
              "Install it with: sudo apt install python3-gi", file=sys.stderr)
        return 1
    return service.run(demo=args.demo, background=args.background)
