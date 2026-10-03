"""Demo mode: a simulated connection that plays a short story on a loop.

``callglance --demo`` runs the real verdict pipeline on synthetic probe results,
so the panel, popover, widget and notifications can be tried (and recorded for
the README) without a flaky network at hand. The story: all good, then the
Wi-Fi gets congested, recovery, then the ISP starts dropping packets.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass

from callglance.config import Config
from callglance.engine import Engine
from callglance.history import History
from callglance.monitor import Schedule
from callglance.netinfo import Route
from callglance.stats import Sample, SampleWindow, summarize
from callglance.verdict import Thresholds, metric_level, worst

GATEWAY = "192.168.1.1"
ISP_HOP = "100.64.12.1"


@dataclass
class Phase:
    name: str
    seconds: float
    wifi_jitter: float = 0.0  # ms of random extra delay added on the Wi-Fi
    wifi_loss: float = 0.0  # % per probe
    isp_loss: float = 0.0
    isp_jitter: float = 0.0
    signal: int = -54


STORY = [
    Phase("good", 40),
    Phase("wifi", 32, wifi_jitter=150.0, wifi_loss=3.0, signal=-76),
    Phase("good", 24),
    Phase("isp", 30, isp_loss=9.0, isp_jitter=12.0),
    Phase("good", 24),
]
STORY_LENGTH = sum(p.seconds for p in STORY)


def phase_at(elapsed: float) -> Phase:
    t = elapsed % STORY_LENGTH
    for phase in STORY:
        if t < phase.seconds:
            return phase
        t -= phase.seconds
    return STORY[0]


class FakeMonitor:
    """Quacks like monitor.TargetMonitor, fed with synthetic samples."""

    def __init__(self, key: str, address: str, method: str, base_ms: float) -> None:
        self.key = key
        self.address = address
        self.method_name = method
        self.base_ms = base_ms
        self.window = SampleWindow(keep=300.0)
        self.schedule = Schedule()
        self.ever_replied = True
        self.confirmed = True
        self.exhausted = False
        self.unreachable = False
        self.train = 0

    def summary(self, seconds: float, now: float | None = None):
        now = time.monotonic() if now is None else now
        self.window.prune(now)
        return summarize(self.window.since(now - seconds), self.schedule.train_length)

    def silent_for_method(self, _min_age: float) -> bool:
        return False

    def stop(self) -> None:
        pass

    def reset(self) -> None:
        self.window.clear()


class FakeDns:
    def __init__(self) -> None:
        self.results: list = []
        self._pending: dict = {}

    def lookup(self) -> None:
        self.results.append((time.time(), random.uniform(11, 16), "zoom.us"))
        del self.results[:-8]

    def stop(self) -> None:
        pass

    def state(self) -> dict:
        ok = [r[1] for r in self.results[-4:]]
        latency = sorted(ok)[len(ok) // 2] if ok else None
        return {"server": GATEWAY, "latency_ms": None if latency is None else round(latency, 1),
                "last_ms": round(self.results[-1][1], 1) if self.results else None,
                "last_name": "zoom.us", "failing": False,
                "level": "good" if latency is not None else "unknown"}


class DemoEngine(Engine):
    def __init__(self, config: Config, history: History | None = None,
                 offset: float = 0.0) -> None:
        super().__init__(
            config, history,
            route_reader=lambda: Route("wlp2s0", GATEWAY, 600),
            dns_reader=lambda: [GATEWAY],
            kind_reader=lambda iface: "wifi",
            link_up=lambda iface: True,
            icmp=False,
        )
        self.rng = random.Random(7)
        self.story_start = time.monotonic() - offset
        self._icmp_checked = float("inf")  # never switch probe methods in the demo

    # -- replace the network with the story ----------------------------------------
    def _apply_route(self, route: Route | None) -> None:
        if self.monitors:
            return  # the story never changes networks
        self.route = route or Route("wlp2s0", GATEWAY, 600)
        self.link_kind = "wifi"
        self.local_ip = "192.168.1.23"
        self.monitors = {
            "router": FakeMonitor("router", GATEWAY, "tcp:9", 2.2),
            "isp": FakeMonitor("isp", ISP_HOP, "udp-ttl2", 9.5),
            "net:1.1.1.1": FakeMonitor("net:1.1.1.1", "1.1.1.1", "dns", 17.0),
            "net:8.8.8.8": FakeMonitor("net:8.8.8.8", "8.8.8.8", "dns", 21.0),
        }
        self.hops = [
            {"ttl": 1, "addr": GATEWAY, "rtt_ms": 2.1},
            {"ttl": 2, "addr": ISP_HOP, "rtt_ms": 9.8},
            {"ttl": 3, "addr": "203.0.113.9", "rtt_ms": 12.4},
            {"ttl": 4, "addr": "1.1.1.1", "rtt_ms": 17.2},
        ]
        self.isp_hop = (2, ISP_HOP)
        self.discovered_at = time.time()
        self.dns = FakeDns()
        self.dns_intercepted = False
        self._generate()

    def _discover(self) -> None:
        pass

    def _poll_wifi(self) -> None:
        pass

    def _maybe_fall_back(self) -> None:
        pass

    def _generate(self) -> None:
        """One burst of five probes per target, every interval, shaped by the story."""
        interval = float(self.config["interval"])
        self.loop.call_later(interval, self._generate)
        now = self.loop.time()
        phase = phase_at(now - self.story_start)
        signal = phase.signal + self.rng.randint(-2, 2)
        self.wifi = {
            "iface": "wlp2s0", "ssid": "Home", "signal_dbm": signal,
            "signal_pct": max(0, min(100, 2 * (signal + 100))), "frequency_mhz": 5180,
            "band": "5 GHz", "channel": 36, "bitrate_mbps": 866.7 if signal > -70 else 175.5,
            "max_bitrate_mbps": 1201.0, "standard": "Wi-Fi 6", "source": "demo",
            "quality": "excellent" if signal >= -60 else ("good" if signal >= -67 else
                                                          ("fair" if signal >= -75 else "poor")),
        }
        for key, mon in self.monitors.items():
            mon.train += 1
            for idx in range(5):
                wifi_delay = self.rng.random() ** 2 * phase.wifi_jitter
                lost = self.rng.random() * 100 < phase.wifi_loss
                rtt = mon.base_ms * (1 + 0.08 * self.rng.random()) + wifi_delay
                if key != "router":
                    rtt += self.rng.random() * phase.isp_jitter
                    lost = lost or self.rng.random() * 100 < phase.isp_loss
                if key.startswith("net:"):
                    rtt += 0.6 * math.sin(now / 9.0)
                sample = Sample(t=now + idx * 0.02, train=mon.train, idx=idx,
                                rtt=None if lost else rtt, done=True)
                mon.window.add(sample)


def prefill_history(history: History, minutes: int = 60, seed: int = 3) -> None:
    """An hour of plausible history: mostly calm, one Wi-Fi dip, one ISP blip."""
    rng = random.Random(seed)
    th = Thresholds()
    now = time.time()
    steps = minutes * 12
    for i in range(steps):
        ts = now - (steps - i) * 5
        age_min = (steps - i) * 5 / 60
        wifi_bad = 37 <= age_min <= 41
        isp_bad = 15 <= age_min <= 17.5
        jitter_wifi = rng.uniform(25, 60) if wifi_bad else rng.uniform(0.3, 2.0)
        router_ms = 2.1 + rng.random() * 0.8 + (rng.uniform(10, 50) if wifi_bad else 0)
        isp_ms = 9.4 + rng.random() * 1.5 + (router_ms - 2.1)
        net_ms = 17.0 + rng.random() * 2.0 + 0.6 * math.sin(ts / 90) + (router_ms - 2.1)
        loss = rng.choice([4.0, 6.0, 8.0, 10.0]) if isp_bad else (
            rng.choice([0.0] * 30 + [2.0]) if wifi_bad else 0.0)
        jitter = jitter_wifi + (rng.uniform(1, 4) if isp_bad else rng.uniform(0.2, 1.5))
        level = worst(
            metric_level(net_ms, th.latency_fair, th.latency_poor),
            metric_level(jitter, th.jitter_fair, th.jitter_poor),
            metric_level(loss, th.loss_fair, th.loss_poor),
        )
        history.add({
            "ts": round(ts, 1), "level": level,
            "net_ms": round(net_ms, 1), "net_jitter": round(jitter, 1), "net_loss": loss,
            "router_ms": round(router_ms, 1), "router_jitter": round(jitter_wifi, 1),
            "router_loss": 0.0,
            "isp_ms": round(isp_ms, 1), "isp_jitter": round(jitter * 0.9, 1),
            "isp_loss": loss * 0.8,
            "dns_ms": round(rng.uniform(10, 18), 1), "wifi_dbm": -54 + rng.randint(-3, 3) - (
                20 if wifi_bad else 0),
        })
