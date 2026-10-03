import tempfile
import threading
import time
from pathlib import Path

from callglance.config import Config
from callglance.demo import DemoEngine, prefill_history
from callglance.engine import Engine, combine_internet
from callglance.history import History
from callglance.netinfo import Route
from callglance.stats import Summary


def test_combine_internet_takes_the_best_target_and_pools_loss():
    a = Summary(sent=100, received=99, latency_ms=12, jitter_ms=2, loss_pct=1, min_ms=10,
                max_ms=20, last_ms=12)
    b = Summary(sent=100, received=97, latency_ms=25, jitter_ms=4, loss_pct=3, min_ms=20,
                max_ms=40, last_ms=25)
    out = combine_internet([a, b], [a, b])
    assert out.latency_ms == 12 and out.jitter_ms == 2
    assert out.loss_pct == 2.0 and out.sent == 200


def test_combine_internet_drops_a_target_that_is_far_worse():
    good = Summary(sent=100, received=100, latency_ms=12, jitter_ms=2, loss_pct=0, min_ms=10,
                   max_ms=20, last_ms=12)
    broken = Summary(sent=100, received=60, latency_ms=30, jitter_ms=5, loss_pct=40, min_ms=20,
                     max_ms=90, last_ms=30)
    assert combine_internet([good, broken], [good, broken]).loss_pct == 0.0


def test_engine_measures_localhost_without_privileges():
    """End to end on loopback: TCP handshakes to a closed port answer like pings."""
    tmp = Path(tempfile.mkdtemp())
    config = Config(tmp / "config.json")
    config.set("public_targets", ["127.0.0.1"], save=False)
    engine = Engine(config, history=History(None), route_reader=lambda: Route("lo", "127.0.0.1", 0),
                    dns_reader=lambda: ["127.0.0.1"], kind_reader=lambda iface: "ethernet",
                    icmp=False)
    snaps = []
    got = threading.Event()

    def listener(snap):
        snaps.append(snap)
        if len(snaps) >= 4:
            got.set()

    engine.add_listener(listener)
    engine.start()
    try:
        assert got.wait(20)
    finally:
        engine.stop()
    snap = snaps[-1]
    assert snap["level"] == "good", snap["headline"]
    router = next(s for s in snap["segments"] if s["id"] == "router")
    assert router["method"] == "tcp:9" and router["received"] > 0
    assert snap["probe"]["icmp"] is False
    assert snap["link"]["kind"] == "ethernet"


def test_demo_engine_and_history_prefill():
    history = History(None)
    prefill_history(history)
    assert len(history.since(3600)) >= 700
    engine = DemoEngine(Config(Path(tempfile.mkdtemp()) / "c.json"), history, offset=0)
    snaps = []
    engine.add_listener(snaps.append)
    engine.start()
    time.sleep(5)
    engine.stop()
    assert snaps and snaps[-1]["level"] == "good"
    assert snaps[-1]["wifi"]["ssid"] == "Home"
