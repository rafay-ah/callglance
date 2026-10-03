"""Decide when to raise a desktop notification about call quality.

One notification per "episode": when quality has stayed below call grade for
``notify_after`` seconds. Another one only if it gets worse (e.g. stutter turns
into an outage), and a "back to good" note when it recovers. A connection that
is permanently mediocre therefore notifies once, not every ten minutes.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from callglance.verdict import FAIR, GOOD, OFFLINE, POOR, SEVERITY

BAD_LEVELS = (FAIR, POOR, OFFLINE)
RECOVERY_HOLD = 30.0  # seconds of good quality before an episode is over


@dataclass
class Notice:
    kind: str  # "degraded" or "recovered"
    title: str
    body: str
    urgency: int  # 0 low, 1 normal, 2 critical (freedesktop spec)


def _duration(seconds: float) -> str:
    minutes = int(round(seconds / 60))
    if minutes < 1:
        return "under a minute"
    if minutes == 1:
        return "1 minute"
    if minutes < 60:
        return f"{minutes} minutes"
    hours = minutes / 60
    return f"{hours:.1f} hours".replace(".0 ", " ")


class QualityNotifier:
    def __init__(self, settings: Callable[[str], object], clock: Callable[[], float] = time.monotonic):
        self.settings = settings
        self.clock = clock
        self.bad_since: float | None = None
        self.good_since: float | None = None
        self.episode_start: float | None = None
        self.notified_level: str | None = None
        self.last_notice_at: float | None = None

    def update(self, snap: dict) -> Notice | None:
        if not self.settings("notifications"):
            return None
        now = self.clock()
        level = snap.get("level")
        if level in BAD_LEVELS:
            self.good_since = None
            if self.bad_since is None:
                self.bad_since = now
            if self.episode_start is None:
                self.episode_start = self.bad_since
            if now - self.bad_since < float(self.settings("notify_after") or 0):
                return None
            worse = (self.notified_level is None
                     or SEVERITY[level] > SEVERITY[self.notified_level])
            if not worse:
                return None
            cooldown = float(self.settings("notify_cooldown") or 0)
            # The cooldown only stops repeat "it dropped" notices; an outage after a
            # stutter is news and goes through.
            if (self.notified_level is None and self.last_notice_at is not None
                    and now - self.last_notice_at < cooldown):
                return None
            self.notified_level = level
            self.last_notice_at = now
            tips = snap.get("tips") or []
            body = snap.get("detail") or ""
            if tips:
                body = f"{body}\n{tips[0]}"
            urgency = 2 if level == OFFLINE else 1
            return Notice("degraded", snap.get("headline") or "Call quality dropped", body,
                          urgency)

        if level == GOOD:
            self.bad_since = None
            if self.good_since is None:
                self.good_since = now
            if self.episode_start is not None and now - self.good_since >= RECOVERY_HOLD:
                notice = None
                if self.notified_level is not None and self.settings("notify_recovery"):
                    lasted = _duration(self.good_since - self.episode_start)
                    notice = Notice("recovered", "Back to good for calls",
                                    f"Your connection recovered after {lasted}.", 0)
                self.episode_start = None
                self.notified_level = None
                return notice
        return None
