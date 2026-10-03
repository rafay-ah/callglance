"""End-to-end: the real engine, unprivileged, in a simulated home network.

Builds client, router, ISP and "internet" network namespaces joined by a
userspace link emulator, injects trouble on one link at a time and checks
the verdict. Needs root (namespaces). Run with: sudo pytest -m netsim
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from netsim import topology  # noqa: E402

pytestmark = pytest.mark.netsim


@pytest.fixture(scope="module")
def net():
    reason = topology.available()
    if reason:
        pytest.skip(reason)
    topology.up()
    yield topology
    topology.down()


@pytest.fixture(autouse=True)
def clean(net):
    net.clear()
    net.allow_ping(True)
    yield


def run_engine(seconds, kind="wifi", window=None):
    cmd = ["ip", "netns", "exec", topology.NS["client"], "setpriv", "--reuid=65534",
           "--regid=65534", "--clear-groups", sys.executable,
           str(HERE / "netsim" / "run_engine.py"), "--seconds", str(seconds), "--kind", kind]
    if window:
        cmd += ["--window", str(window)]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=seconds + 60,
                         env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    final = [json.loads(line) for line in out.stdout.splitlines() if line.startswith('{"final"')]
    assert final, out.stderr
    return final[0]["final"]


def seg(snap, key):
    return next(s for s in snap["segments"] if s["id"] == key)


def test_clean_network_with_icmp(net):
    snap = run_engine(10)
    assert snap["level"] == "good"
    assert snap["probe"]["icmp"] is True
    assert snap["path"]["isp_hop"]["address"] == "100.64.0.1"
    assert [h["addr"] for h in snap["path"]["hops"]] == ["192.168.1.1", "100.64.0.1", "1.1.1.1"]


def test_wifi_jitter_and_loss_blame_the_wifi(net):
    net.impair("wifi", jitter=40, loss=2)
    snap = run_engine(25, window=20)
    assert snap["culprit"] == "wifi" and snap["headline"] == "Your Wi-Fi is the problem"
    assert seg(snap, "router")["status"] == "origin"


def test_loss_on_the_access_line_blames_the_isp(net):
    net.impair("access", loss=4)
    snap = run_engine(40, window=30)
    assert snap["culprit"] == "isp" and snap["level"] in ("fair", "poor")


def test_upstream_jitter_blames_the_isp(net):
    net.impair("upstream", jitter=130)
    snap = run_engine(20, window=15, kind="ethernet")
    assert snap["culprit"] == "isp" and snap["origin"] == "internet"
    assert "home network is fine" in snap["detail"]


def test_upstream_outage(net):
    net.impair("upstream", loss=100)
    snap = run_engine(12)
    assert snap["level"] == "offline" and snap["culprit"] == "isp"


def test_wifi_outage(net):
    net.impair("wifi", loss=100)
    snap = run_engine(12)
    assert snap["level"] == "offline" and snap["culprit"] == "wifi"


def test_without_icmp_everything_is_still_measured(net):
    net.allow_ping(False)
    snap = run_engine(14)
    assert snap["probe"]["icmp"] is False and snap["level"] == "good"
    assert seg(snap, "router")["method"] == "tcp:9"
    assert seg(snap, "isp")["method"] == "udp-ttl2"
    assert {t["method"] for t in seg(snap, "internet")["targets"]} == {"dns"}


def test_dns_interception_is_detected(net):
    net.allow_ping(False)
    subprocess.run(["ip", "netns", "exec", topology.NS["router"], "iptables", "-t", "nat", "-A",
                    "PREROUTING", "-i", "wifR", "-p", "udp", "--dport", "53", "-j", "DNAT",
                    "--to-destination", "192.168.1.1"], check=True)
    try:
        snap = run_engine(10)
    finally:
        subprocess.run(["ip", "netns", "exec", topology.NS["router"], "iptables", "-t", "nat",
                        "-F"], check=False)
    assert {t["method"] for t in seg(snap, "internet")["targets"]} == {"tcp:443"}


def test_wifi_loss_without_icmp(net):
    net.allow_ping(False)
    net.impair("wifi", loss=3)
    snap = run_engine(45, window=35)
    assert snap["culprit"] == "wifi"
