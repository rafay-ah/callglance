"""Turn measurements into a plain-language verdict.

The idea is the one network engineers apply to an ``mtr`` report: a problem
belongs to the first hop where it appears *and keeps appearing downstream*.

* Loss or jitter that already shows up on the way to your own router is caused
  by your Wi-Fi (or your cable/router when wired).
* If the router is clean but your ISP's first router or the internet targets
  are not, the problem is outside your home: your ISP.
* Loss that shows up at an intermediate router but *not* further along is
  ignored: routers rate-limit answers to pings aimed at themselves, and that does
  not affect traffic passing through them.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

from callglance.stats import Summary

GOOD, FAIR, POOR, OFFLINE, UNKNOWN = "good", "fair", "poor", "offline", "unknown"
SEVERITY = {UNKNOWN: -1, GOOD: 0, FAIR: 1, POOR: 2, OFFLINE: 3}

WIFI, LOCAL, ISP = "wifi", "local", "isp"


@dataclass
class Thresholds:
    """Call-grade limits for ping-style measurements (round-trip, both directions).

    See README "How the verdict works" for the sources behind these numbers.
    """

    # Round-trip time. Teams wants < 100 ms client-to-edge and Meet is best below
    # 100 ms; Meet degrades from 300 ms, which is also ITU-T G.114's 150 ms one way.
    latency_fair: float = 100.0
    latency_poor: float = 300.0
    # Mean |difference| of consecutive RTTs. Teams, Cisco and Google Voice use
    # 30 ms (one way), Zoom 40 ms; an RTT-based figure adds both directions.
    jitter_fair: float = 30.0
    jitter_poor: float = 50.0
    # Round-trip loss (either direction). Teams < 1%, Zoom <= 2%, G.1010 voice < 3%.
    loss_fair: float = 1.5
    loss_poor: float = 3.0

    @classmethod
    def from_dict(cls, data: dict | None) -> Thresholds:
        th = cls()
        for key, value in (data or {}).items():
            if hasattr(th, key) and isinstance(value, (int, float)):
                setattr(th, key, float(value))
        return th

    def as_dict(self) -> dict:
        return asdict(self)


def metric_level(value: float | None, fair: float, poor: float) -> str:
    if value is None:
        return UNKNOWN
    if value >= poor:
        return POOR
    if value >= fair:
        return FAIR
    return GOOD


def worst(*levels: str) -> str:
    return max(levels, key=lambda lv: SEVERITY[lv]) if levels else UNKNOWN


@dataclass
class Verdict:
    level: str
    headline: str
    detail: str
    culprit: str | None = None  # wifi | local | isp
    cause: str | None = None  # loss | jitter | latency | outage
    origin: str | None = None  # segment where the problem starts: router | isp | internet
    tips: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)  # per-metric level at the internet

    def as_dict(self) -> dict:
        return asdict(self)


def fmt_ms(value: float | None) -> str:
    if value is None:
        return "–"
    return f"{value:.0f} ms" if value >= 10 else f"{value:.1f} ms"


def fmt_pct(value: float | None) -> str:
    if value is None:
        return "–"
    if value == 0:
        return "0%"
    return f"{value:.1f}%" if value < 10 else f"{value:.0f}%"


@dataclass
class Inputs:
    internet: Summary | None  # best public target, normal window
    internet_recent: Summary | None  # best public target, last few seconds (outages)
    router: Summary | None
    router_recent: Summary | None
    isp: Summary | None
    isp_recent: Summary | None = None
    has_route: bool = True
    link_kind: str = "other"  # wifi | ethernet | vpn | mobile | other
    wifi: dict | None = None  # WifiInfo.as_dict()
    router_measurable: bool = True  # False when the router ignores every probe method


def _value(metric: str, s: Summary) -> float | None:
    return {"loss": s.loss_pct, "jitter": s.jitter_ms, "latency": s.latency_ms}[metric]


BACKGROUND_LOSS = 0.002  # what a healthy hop loses anyway (0.2%)
LIKELIHOOD_RATIO = math.log(3.0)


def _loss_evidence(seg: Summary, net: Summary) -> str:
    """Is the segment's loss count better explained by "this segment causes at least
    half of the loss seen at the internet" or by "this segment is healthy"?

    A binomial likelihood ratio: with few probes neither hypothesis wins and the
    answer is "unknown" instead of a coin flip.
    """
    n = seg.sent
    k = n - seg.received
    p_net = max((net.loss_pct or 0.0) / 100.0, 2 * BACKGROUND_LOSS)
    p_half = min(0.5 * p_net, 0.99)
    if p_half <= BACKGROUND_LOSS or n == 0:
        return "unknown"
    ll_cause = k * math.log(p_half) + (n - k) * math.log(1 - p_half)
    ll_clean = k * math.log(BACKGROUND_LOSS) + (n - k) * math.log(1 - BACKGROUND_LOSS)
    ratio = ll_cause - ll_clean
    if ratio >= LIKELIHOOD_RATIO:
        return "yes"
    if ratio <= -LIKELIHOOD_RATIO:
        return "no"
    return "unknown"


def _evidence(metric: str, seg: Summary | None, net: Summary, th: Thresholds) -> str:
    """Does ``seg`` already show the problem seen at the internet? yes / no / unknown."""
    if seg is None or not seg.reachable:
        return "unknown"
    if metric == "loss":
        return _loss_evidence(seg, net)
    value, net_value = _value(metric, seg), _value(metric, net) or 0.0
    if value is None:
        return "unknown"
    floor = {"jitter": th.jitter_fair, "latency": th.latency_fair}[metric]
    return "yes" if value >= max(0.5 * floor, 0.5 * net_value) else "no"


def _degraded(seg: Summary | None, net: Summary, th: Thresholds) -> bool:
    """Independent signs of trouble on a segment: jitter, or a long tail of slow replies.

    Wi-Fi trouble almost never shows up as loss alone (the radio retransmits),
    so this settles cases where loss statistics are still inconclusive.
    """
    if seg is None or not seg.reachable:
        return False
    if (seg.jitter_ms or 0.0) >= max(0.25 * th.jitter_fair, 0.5 * (net.jitter_ms or 0.0)):
        return True
    spread = (seg.latency_ms or 0.0) - (seg.min_ms or 0.0)
    net_spread = (net.latency_ms or 0.0) - (net.min_ms or 0.0)
    return spread >= max(10.0, 0.5 * net_spread)


def _origin(metric: str, net: Summary, router: Summary | None, isp: Summary | None,
            th: Thresholds) -> str:
    """Where does a problem seen at the internet targets start?"""
    for name, seg in (("router", router), ("isp", isp)):
        evidence = _evidence(metric, seg, net, th)
        if evidence == "yes" or (evidence == "unknown" and _degraded(seg, net, th)):
            return name
    return "internet"


IMPACT = {
    ("loss", FAIR): "Calls may stutter",
    ("loss", POOR): "Calls will break up",
    ("jitter", FAIR): "Calls may stutter",
    ("jitter", POOR): "Calls will be choppy",
    ("latency", FAIR): "Expect a slight delay",
    ("latency", POOR): "Expect talk-over delays",
}


def _describe(metric: str, s: Summary) -> str:
    if metric == "loss":
        return f"{fmt_pct(s.loss_pct)} packet loss"
    if metric == "jitter":
        return f"{fmt_ms(s.jitter_ms)} of jitter"
    return f"{fmt_ms(s.latency_ms)} latency"


def _wifi_tips(wifi: dict | None) -> list[str]:
    tips: list[str] = []
    if not wifi:
        return tips
    dbm = wifi.get("signal_dbm")
    quality = wifi.get("quality")
    if quality in ("poor", "fair"):
        strength = f" ({dbm} dBm)" if dbm is not None else ""
        tips.append(f"Your Wi-Fi signal is weak{strength}. Move closer to the router or "
                    "remove obstacles between you.")
    if wifi.get("band") == "2.4 GHz":
        tips.append("You're on 2.4 GHz, which is slower and more crowded. "
                    "Switch to 5 GHz if your router offers it.")
    rate = wifi.get("bitrate_mbps")
    if rate is not None and rate < 20:
        tips.append(f"Your Wi-Fi link is slow ({rate:g} Mb/s).")
    return tips


def assess(inp: Inputs, th: Thresholds) -> Verdict:
    """Compute the verdict for one moment. Pure function, easy to test."""
    on_wifi = inp.link_kind == "wifi"
    home_culprit = WIFI if on_wifi else LOCAL

    if not inp.has_route:
        what = "Wi-Fi is disconnected" if on_wifi else "No network connection"
        return Verdict(OFFLINE, "You're offline", f"{what}.", cause="outage")

    net = inp.internet
    if net is None or net.sent < 4:
        return Verdict(UNKNOWN, "Checking your connection…", "Measuring latency, jitter and loss.")

    # -- outages: judged on the last few seconds so they show up immediately --
    recent = inp.internet_recent
    if recent is not None and recent.sent >= 3 and recent.received == 0:
        router_down = (
            inp.router_measurable
            and inp.router_recent is not None
            and inp.router_recent.sent >= 3
            and inp.router_recent.received == 0
        )
        if router_down:
            where = "Wi-Fi" if on_wifi else "home network"
            headline = ("Your Wi-Fi is the problem" if on_wifi
                        else "Your home network is the problem")
            return Verdict(OFFLINE, headline, f"Can't reach your router over your {where}.",
                           culprit=home_culprit, cause="outage", origin="router",
                           tips=_wifi_tips(inp.wifi) if on_wifi else
                           ["Check the cable and that your router is powered on."])
        if inp.router_measurable and inp.router is not None and inp.router.reachable:
            isp_up = inp.isp_recent is not None and inp.isp_recent.reachable
            detail = ("No internet: your router and your ISP's first router answer, "
                      "but nothing beyond them does." if isp_up else
                      "No internet: your router answers, but nothing beyond it does.")
            return Verdict(OFFLINE, "Your ISP is the problem", detail,
                           culprit=ISP, cause="outage", origin="internet" if isp_up else "isp",
                           tips=["Restart your modem/router. If that doesn't help, your ISP "
                                 "probably has an outage."])
        return Verdict(OFFLINE, "No internet connection", "Nothing on the internet answers.",
                       cause="outage")

    levels = {
        "loss": metric_level(net.loss_pct, th.loss_fair, th.loss_poor),
        "jitter": metric_level(net.jitter_ms, th.jitter_fair, th.jitter_poor),
        "latency": metric_level(net.latency_ms, th.latency_fair, th.latency_poor),
    }
    overall = worst(*[lv for lv in levels.values() if lv != UNKNOWN]) if any(
        lv != UNKNOWN for lv in levels.values()) else UNKNOWN

    if overall in (GOOD, UNKNOWN):
        tips = []
        if on_wifi and inp.wifi and inp.wifi.get("quality") == "poor":
            tips = _wifi_tips(inp.wifi)[:1]
        loss = "no packet loss" if not net.loss_pct else f"{fmt_pct(net.loss_pct)} loss"
        detail = f"{fmt_ms(net.latency_ms)} latency · {fmt_ms(net.jitter_ms)} jitter · {loss}"
        return Verdict(GOOD, "Good for calls", detail, tips=tips, metrics=levels)

    # -- something is off: find the worst metric and where it starts ---------
    order = {"loss": 0, "jitter": 1, "latency": 2}
    failing = sorted(
        (m for m, lv in levels.items() if lv in (FAIR, POOR)),
        key=lambda m: (-SEVERITY[levels[m]], order[m]),
    )
    metric = failing[0]
    router = inp.router if inp.router_measurable else None
    origin = _origin(metric, net, router, inp.isp, th)

    impact = IMPACT[(metric, overall)]
    what = _describe(metric, net)
    home = "Your Wi-Fi is fine." if on_wifi else "Your home network is fine."
    if origin == "router":
        culprit = home_culprit
        headline = "Your Wi-Fi is the problem" if on_wifi else "Your home network is the problem"
        where = "on your Wi-Fi" if on_wifi else "inside your home network"
        detail = f"{impact}: {what} {where}."
        tips = _wifi_tips(inp.wifi) if on_wifi else []
        if on_wifi:
            if not tips:
                tips.append("Something on your Wi-Fi is busy, or there is interference. "
                            "Pause big downloads, uploads or backups.")
            tips.append("A wired Ethernet connection is the most reliable fix for calls.")
        else:
            tips.append("Check the cable, then restart your router.")
    else:
        culprit = ISP
        headline = "Your ISP is the problem"
        where = ("from your ISP's first router onward" if origin == "isp"
                 else "beyond your router")
        detail = f"{impact}: {what} {where}. {home}"
        tips = ["Restarting your modem or router sometimes helps. If it keeps happening, "
                "contact your ISP."]
        if metric == "latency":
            tips.insert(0, "Run a speed test: latency that jumps under load (bufferbloat) is "
                           "often fixable with your router's SQM or QoS setting.")
    return Verdict(overall, headline, detail, culprit=culprit, cause=metric, origin=origin,
                   tips=tips, metrics=levels)


class VerdictTracker:
    """Adds hysteresis: get worse immediately, get better only once it sticks.

    A connection hovering around a threshold should not make the panel dot
    flicker between green and amber every two seconds.
    """

    def __init__(self, settle: int = 3) -> None:
        self.settle = settle
        self.current: Verdict | None = None
        self._better_streak = 0

    def update(self, new: Verdict) -> Verdict:
        cur = self.current
        if cur is None or cur.level in (UNKNOWN,) or new.level == UNKNOWN:
            self.current, self._better_streak = new, 0
            return new
        if SEVERITY[new.level] >= SEVERITY[cur.level]:
            self.current, self._better_streak = new, 0
            return new
        self._better_streak += 1
        if self._better_streak >= self.settle:
            self.current, self._better_streak = new, 0
            return new
        return cur

    def reset(self) -> None:
        self.current = None
        self._better_streak = 0
