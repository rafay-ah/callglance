"""Turn raw probe samples into latency, jitter and packet-loss figures."""

from __future__ import annotations

import statistics
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field


@dataclass
class Sample:
    t: float  # monotonic send time (seconds)
    train: int  # which burst this probe belonged to
    idx: int  # position inside the burst; 0 is the radio "warm-up" probe
    rtt: float | None = None  # milliseconds, None = lost
    done: bool = False


@dataclass
class Summary:
    sent: int = 0
    received: int = 0
    latency_ms: float | None = None  # median RTT
    jitter_ms: float | None = None  # mean |delta| between consecutive probes
    loss_pct: float | None = None
    min_ms: float | None = None
    max_ms: float | None = None
    last_ms: float | None = None

    @property
    def measurable(self) -> bool:
        return self.sent > 0

    @property
    def reachable(self) -> bool:
        return self.received > 0

    def as_dict(self) -> dict:
        return {
            "sent": self.sent,
            "received": self.received,
            "latency_ms": _round(self.latency_ms),
            "jitter_ms": _round(self.jitter_ms),
            "loss_pct": _round(self.loss_pct, 2),
            "min_ms": _round(self.min_ms),
            "max_ms": _round(self.max_ms),
            "last_ms": _round(self.last_ms),
        }


def _round(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def summarize(samples: Iterable[Sample], train_length: int = 1) -> Summary:
    """Summarise completed samples.

    * Loss counts every probe, including the warm-up probe of each burst.
    * Latency and jitter skip the warm-up probe when bursts have 3+ probes: the
      first packet after an idle gap pays for Wi-Fi power saving, which a call's
      steady 50 packets/s stream never does.
    * Jitter is the mean absolute difference between consecutive RTTs inside a
      burst (in the spirit of RFC 3550's inter-arrival jitter). With single-probe
      bursts it falls back to consecutive probes.
    """
    done = [s for s in samples if s.done]
    out = Summary(sent=len(done))
    if not done:
        return out
    received = [s for s in done if s.rtt is not None]
    out.received = len(received)
    out.loss_pct = 100.0 * (out.sent - out.received) / out.sent
    if not received:
        return out

    skip_warmup = train_length >= 3
    measured = [s for s in received if not (skip_warmup and s.idx == 0)] or received
    rtts = [s.rtt for s in measured]
    out.latency_ms = statistics.median(rtts)
    out.min_ms = min(rtts)
    out.max_ms = max(rtts)
    out.last_ms = max(received, key=lambda s: (s.t, s.idx)).rtt

    diffs: list[float] = []
    if train_length >= 2:
        trains: dict[int, list[Sample]] = {}
        for s in measured:
            trains.setdefault(s.train, []).append(s)
        for burst in trains.values():
            burst.sort(key=lambda s: s.idx)
            for a, b in zip(burst, burst[1:], strict=False):
                if b.idx == a.idx + 1:  # only truly consecutive packets
                    diffs.append(abs(b.rtt - a.rtt))
    if not diffs:
        ordered = sorted(measured, key=lambda s: (s.t, s.idx))
        diffs = [abs(b.rtt - a.rtt) for a, b in zip(ordered, ordered[1:], strict=False)]
    if diffs:
        out.jitter_ms = sum(diffs) / len(diffs)
    return out


@dataclass
class SampleWindow:
    """Completed samples for one target, pruned to ``keep`` seconds."""

    keep: float = 300.0
    samples: deque = field(default_factory=deque)

    def add(self, sample: Sample) -> None:
        self.samples.append(sample)

    def prune(self, now: float) -> None:
        horizon = now - self.keep
        while self.samples and self.samples[0].t < horizon:
            self.samples.popleft()

    def since(self, start: float) -> list[Sample]:
        return [s for s in self.samples if s.t >= start]

    def clear(self) -> None:
        self.samples.clear()
