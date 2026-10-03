"""Continuously measure one target with bursts of probes.

Each burst ("train") mimics a voice stream: by default 5 packets 20 ms apart,
every 2 seconds. The first packet wakes up the Wi-Fi radio (power saving) and is
left out of latency/jitter, exactly like the steady packet flow of a real call.

A target has a chain of probe methods (e.g. ICMP, then DNS, then TCP). If the
current method never gets an answer while the network is demonstrably up, the
monitor moves to the next one, so a router that ignores pings is still measured.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from callglance.loop import Loop, TimerHandle
from callglance.probes import Probe, Reply
from callglance.stats import Sample, SampleWindow, Summary, summarize

log = logging.getLogger(__name__)

ProbeFactory = Callable[[], Probe]


@dataclass
class Schedule:
    interval: float = 2.0
    train_length: int = 5
    spacing: float = 0.020
    timeout: float = 1.0


class TargetMonitor:
    def __init__(
        self,
        loop: Loop,
        key: str,
        address: str,
        methods: list[tuple[str, ProbeFactory, Schedule]],
        offset: float = 0.0,
        keep: float = 300.0,
    ) -> None:
        self.loop = loop
        self.key = key
        self.address = address
        self.methods = methods
        self.offset = offset
        self.window = SampleWindow(keep=keep)
        self.method_index = 0
        self.method_name = ""
        self.schedule = methods[0][2] if methods else Schedule()
        self.exhausted = False  # every method tried, nothing ever answered
        self.unreachable = False  # ...and nothing else on the path answered either
        self._probe: Probe | None = None
        self._timer: TimerHandle | None = None
        self._pending: dict[int, Sample] = {}
        self._seq = 0
        self._train = 0
        self._started_at = 0.0
        self._replies_with_method = 0
        self._sent_with_method = 0
        self._preconfirmed = False
        self.ever_replied = False
        self.reply_addr: str | None = None
        self.on_sample: Callable[[TargetMonitor, Sample], None] | None = None

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        self._open_method()
        self._timer = self.loop.call_later(self.offset, self._send_train)

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._close_method()
        self._pending.clear()

    def _open_method(self) -> None:
        self._close_method()
        while self.method_index < len(self.methods):
            name, factory, schedule = self.methods[self.method_index]
            try:
                probe = factory()
                probe.attach(self.loop, self._on_reply)
            except OSError as exc:
                log.debug("%s: method %s unavailable: %s", self.key, name, exc)
                self.method_index += 1
                continue
            self._probe = probe
            self.method_name = name
            self.schedule = schedule
            self._started_at = self.loop.time()
            self._replies_with_method = 0
            self._sent_with_method = 0
            self._preconfirmed = False
            log.debug("%s (%s): probing with %s", self.key, self.address, name)
            return
        self._probe = None
        self.method_name = "none"
        self.exhausted = True

    def _close_method(self) -> None:
        if self._probe is not None:
            self._probe.detach()
            self._probe = None
        # Anything still in flight on the old method can no longer be answered.
        self._pending.clear()

    def can_fall_back(self) -> bool:
        return self.method_index + 1 < len(self.methods)

    def silent_for_method(self, min_age: float) -> bool:
        """No reply since the current method started, and it had a fair chance."""
        return (
            self._probe is not None
            and self._replies_with_method == 0
            and self._sent_with_method >= 4
            and self.loop.time() - self._started_at >= min_age
        )

    @property
    def confirmed(self) -> bool:
        """The current method has had at least one answer, so silence means loss."""
        return self._replies_with_method > 0 or self._preconfirmed

    def use_method(self, index: int, confirmed: bool = False) -> None:
        """Switch to a method known to work (found by the engine's preflight)."""
        if index != self.method_index or self._probe is None:
            self.method_index = index
            self._open_method()
            self.window.clear()
        self.exhausted = False
        self.unreachable = False
        self._preconfirmed = confirmed

    def mark_unreachable(self) -> None:
        """Nothing answers this target, whatever the method: its silence is real loss."""
        self.exhausted = True
        self.unreachable = True

    def silent_since(self, seconds: float, now: float | None = None) -> bool:
        now = self.loop.time() if now is None else now
        recent = self.window.since(now - seconds)
        return bool(recent) and all(s.rtt is None for s in recent)

    def fall_back(self) -> bool:
        """Switch to the next probe method. Returns False when none is left."""
        if not self.can_fall_back():
            return False
        self.method_index += 1
        log.info("%s (%s): no answers to %s, trying %s", self.key, self.address,
                 self.method_name, self.methods[self.method_index][0])
        self._open_method()
        # Unanswered probes of a method the target ignores are not packet loss.
        self.window.clear()
        return self._probe is not None

    def restart_methods(self) -> None:
        """Start again from the preferred method (e.g. after the network changed)."""
        self.method_index = 0
        self.exhausted = False
        self._open_method()
        self.window.clear()

    # -- probing ------------------------------------------------------------
    def _send_train(self) -> None:
        sched = self.schedule
        self._timer = self.loop.call_later(sched.interval, self._send_train)
        if self._probe is None:
            return
        self._train += 1
        for idx in range(sched.train_length):
            self.loop.call_later(idx * sched.spacing, self._send_one, self._probe, self._train, idx)

    def _send_one(self, probe: Probe, train: int, idx: int) -> None:
        if probe is not self._probe:
            return  # the method changed while the burst was in flight
        self._seq = (self._seq + 1) & 0xFFFF
        seq = self._seq
        sample = Sample(t=self.loop.time(), train=train, idx=idx)
        self._pending[seq] = sample  # register before sending: replies can be synchronous
        self._sent_with_method += 1
        if not probe.send(seq):
            self._complete(seq, None)
            return
        self.loop.call_later(self.schedule.timeout, self._expire, probe, seq)

    def _on_reply(self, reply: Reply) -> None:
        if reply.seq in self._pending:
            self._replies_with_method += 1
            self.ever_replied = True
            self.reply_addr = reply.addr
            self._complete(reply.seq, reply.rtt_ms)

    def _expire(self, probe: Probe, seq: int) -> None:
        if probe is not self._probe:
            return
        sample = self._pending.get(seq)
        if sample is None:
            return
        # A late answer to a recycled sequence number must not count: forget it.
        probe.forget(seq)
        self._complete(seq, None)

    def _complete(self, seq: int, rtt: float | None) -> None:
        sample = self._pending.pop(seq, None)
        if sample is None:
            return
        sample.rtt = rtt
        sample.done = True
        self.window.add(sample)
        if self.on_sample is not None:
            self.on_sample(self, sample)

    # -- results ------------------------------------------------------------
    def summary(self, seconds: float, now: float | None = None) -> Summary:
        now = self.loop.time() if now is None else now
        self.window.prune(now)
        return summarize(self.window.since(now - seconds), self.schedule.train_length)

    def reset(self) -> None:
        self.window.clear()
