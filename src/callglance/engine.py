"""The measurement engine: probes every segment of the path and judges the result.

Segments, from you outwards:

* **router**: your default gateway (Wi-Fi or Ethernet hop).
* **isp**: the first router of your ISP, found traceroute-style.
* **internet**: public anycast targets (1.1.1.1 and 8.8.8.8 by default).

Everything runs on a private event loop in a background thread. Every
``interval`` seconds a snapshot (a JSON-friendly dict) is published to
listeners, and every few seconds a point is added to the history.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from callglance import __version__
from callglance.config import Config
from callglance.history import History
from callglance.loop import Loop
from callglance.monitor import Schedule, TargetMonitor
from callglance.netinfo import (
    KNOWN_RESOLVERS,
    Route,
    default_route,
    dns_servers,
    iface_is_up,
    iface_kind,
    pick_isp_hop,
)
from callglance.probes import (
    DnsQuery,
    IcmpEcho,
    Probe,
    Reply,
    TcpConnect,
    UdpTtl,
    local_address_for,
    ping_sockets_allowed,
)
from callglance.stats import Summary
from callglance.verdict import (
    FAIR,
    GOOD,
    POOR,
    UNKNOWN,
    Inputs,
    Thresholds,
    Verdict,
    VerdictTracker,
    assess,
    metric_level,
    worst,
)
from callglance.wifi import WifiReader

log = logging.getLogger(__name__)

HISTORY_EVERY = 5.0  # seconds between history points
HISTORY_WINDOW = 10.0  # seconds of data behind each history point
RECENT_WINDOW = 6.0  # seconds used to detect outages quickly
DISCOVERY_EVERY = 300.0
MAX_TTL = 8
FALLBACK_AFTER = 8.0  # seconds without answers before trying another probe method

SEGMENT_LABELS = {"router": "Router", "isp": "ISP", "internet": "Internet"}


def _clock_offset() -> float:
    """CLOCK_BOOTTIME - CLOCK_MONOTONIC: grows by the time spent suspended."""
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME) - time.monotonic()
    except (AttributeError, OSError):
        return 0.0


def combine_internet(summaries: list[Summary], loss_summaries: list[Summary]) -> Summary | None:
    """Merge the public targets into one "internet" figure.

    Latency and jitter: the best target (one slow anycast site is not your
    connection's fault). Loss: pooled across targets for better statistics, but a
    target losing far more than the others is left out (rate limiting or a
    problem on that provider's side).
    """
    measured = [s for s in summaries if s.sent]
    if not measured:
        return None
    reachable = [s for s in measured if s.received]
    out = Summary()
    if reachable:
        out.latency_ms = min(s.latency_ms for s in reachable if s.latency_ms is not None)
        jitters = [s.jitter_ms for s in reachable if s.jitter_ms is not None]
        out.jitter_ms = min(jitters) if jitters else None
        out.min_ms = min(s.min_ms for s in reachable if s.min_ms is not None)
        out.max_ms = min(s.max_ms for s in reachable if s.max_ms is not None)
        lasts = [s.last_ms for s in reachable if s.last_ms is not None]
        out.last_ms = min(lasts) if lasts else None
    pool = [s for s in loss_summaries if s.sent]
    if len(pool) >= 2:
        # Only drop a target when it is dramatically worse than the best one;
        # small samples differ by a few percent through chance alone.
        best = min(s.loss_pct or 0.0 for s in pool)
        keep = [s for s in pool
                if (s.loss_pct or 0.0) < max(3 * best, best + 10.0)]
        pool = keep or pool
    sent = sum(s.sent for s in pool)
    received = sum(s.received for s in pool)
    out.sent, out.received = sent, received
    out.loss_pct = 100.0 * (sent - received) / sent if sent else None
    return out


def _merge_loss(main: Summary, loss: Summary) -> Summary:
    """Latency/jitter from the short window, loss from the longer one."""
    if loss.sent:
        main.sent, main.received, main.loss_pct = loss.sent, loss.received, loss.loss_pct
    return main


class DnsTimer:
    """Times a real lookup of a call service's hostname every few seconds."""

    def __init__(self, loop: Loop, server: str, names: list[str], timeout: float = 2.0) -> None:
        self.loop = loop
        self.server = server
        self.names = names or ["zoom.us"]
        self.timeout = timeout
        self.results: deque[tuple[float, float | None, str]] = deque(maxlen=8)
        self._probe: DnsQuery | None = None
        self._pending: dict[int, tuple[float, str]] = {}
        self._seq = 0
        self._name_idx = 0

    def start(self) -> None:
        try:
            self._probe = DnsQuery(self.server)
            self._probe.attach(self.loop, self._on_reply)
        except OSError as exc:
            log.debug("DNS timing disabled: %s", exc)
            self._probe = None

    def stop(self) -> None:
        if self._probe is not None:
            self._probe.detach()
            self._probe = None

    def lookup(self) -> None:
        if self._probe is None:
            return
        self._seq = (self._seq + 1) & 0xFFFF
        name = self.names[self._name_idx % len(self.names)]
        self._name_idx += 1
        self._pending[self._seq] = (time.time(), name)
        if not self._probe.send(self._seq, name):
            self._finish(self._seq, None)
            return
        self.loop.call_later(self.timeout, self._expire, self._seq)

    def _on_reply(self, reply: Reply) -> None:
        self._finish(reply.seq, reply.rtt_ms)

    def _expire(self, seq: int) -> None:
        if seq in self._pending and self._probe is not None:
            self._probe.forget(seq)
            self._finish(seq, None)

    def _finish(self, seq: int, rtt: float | None) -> None:
        entry = self._pending.pop(seq, None)
        if entry is not None:
            self.results.append((entry[0], rtt, entry[1]))

    def state(self) -> dict:
        ok = [r for r in self.results if r[1] is not None]
        recent = list(self.results)[-4:]
        latency = sorted(r[1] for r in ok[-4:])[len(ok[-4:]) // 2] if ok else None
        failing = bool(recent) and all(r[1] is None for r in recent[-2:])
        if failing:
            level = POOR
        elif latency is None:
            level = UNKNOWN
        else:
            level = metric_level(latency, 100.0, 300.0)
        last = self.results[-1] if self.results else None
        return {
            "server": self.server,
            "latency_ms": None if latency is None else round(latency, 1),
            "last_ms": None if not last or last[1] is None else round(last[1], 1),
            "last_name": last[2] if last else None,
            "failing": failing,
            "level": level,
        }


class Engine:
    def __init__(
        self,
        config: Config,
        history: History | None = None,
        wifi_reader: WifiReader | None = None,
        route_reader: Callable[[], Route | None] = default_route,
        dns_reader: Callable[[], list[str]] = dns_servers,
        kind_reader: Callable[[str], str] = iface_kind,
        icmp: bool | None = None,
    ) -> None:
        self.config = config
        self.history = history
        self.wifi_reader = wifi_reader or WifiReader()
        self.route_reader = route_reader
        self.dns_reader = dns_reader
        self.kind_reader = kind_reader
        self.icmp = ping_sockets_allowed() if icmp is None else icmp
        self.thresholds = Thresholds.from_dict(config["thresholds"])
        self.tracker = VerdictTracker()
        self.loop = Loop()
        self.loop.on_error = lambda exc: log.exception("engine error", exc_info=exc)
        self.monitors: dict[str, TargetMonitor] = {}
        self.route: Route | None = None
        self.link_kind = "other"
        self.local_ip: str | None = None
        self.hops: list[dict] = []
        self.isp_hop: tuple[int, str] | None = None
        self.discovering = False
        self.discovered_at: float | None = None
        self.wifi: dict | None = None
        self.dns: DnsTimer | None = None
        self.started_at = time.time()
        self.network_since = time.monotonic()
        self.extra: dict[str, Any] = {}  # e.g. speed test state, set by the service
        self._listeners: list[Callable[[dict], None]] = []
        self._snapshot: dict = {}
        self._snapshot_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_history = 0.0
        self._clock_offset = _clock_offset()
        self._workers = ThreadPoolExecutor(max_workers=1, thread_name_prefix="callglance-io")
        self._wifi_busy = False
        self._discovery_timer = None

    # -- public API (any thread) ------------------------------------------------
    def add_listener(self, callback: Callable[[dict], None]) -> None:
        self._listeners.append(callback)

    def snapshot(self) -> dict:
        with self._snapshot_lock:
            return dict(self._snapshot)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="callglance-engine", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self._shutdown)
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._workers.shutdown(wait=False)

    def rediscover(self) -> None:
        self.loop.call_soon_threadsafe(self._discover)

    def set_thresholds(self, data: dict) -> None:
        self.thresholds = Thresholds.from_dict(data)

    def call_in_engine(self, fn: Callable[..., Any], *args: Any) -> None:
        self.loop.call_soon_threadsafe(fn, *args)

    # -- engine thread ----------------------------------------------------------
    def _run(self) -> None:
        self.loop.call_soon_threadsafe(self._setup)
        try:
            self.loop.run()
        finally:
            self.loop.close()

    def _shutdown(self) -> None:
        self._stop_monitors()
        self.loop.stop()

    def _setup(self) -> None:
        self._apply_route(self.route_reader())
        self._tick()

    def _schedule(self) -> Schedule:
        cfg = self.config
        return Schedule(
            interval=float(cfg["interval"]),
            train_length=max(1, int(cfg["train_length"])),
            spacing=float(cfg["train_spacing_ms"]) / 1000.0,
            timeout=float(cfg["probe_timeout"]),
        )

    def _slow_schedule(self) -> Schedule:
        # Routers rate-limit "time exceeded" errors (Linux: ~1/s per host), so
        # TTL-based probes go one at a time.
        base = self._schedule()
        return Schedule(interval=max(2.0, base.interval), train_length=1, spacing=0.0,
                        timeout=base.timeout)

    def _tcp_schedule(self) -> Schedule:
        base = self._schedule()
        return Schedule(interval=base.interval, train_length=min(3, base.train_length),
                        spacing=0.05, timeout=base.timeout)

    @property
    def public_targets(self) -> list[str]:
        return list(self.config["public_targets"]) or ["1.1.1.1"]

    def _stop_monitors(self) -> None:
        for mon in self.monitors.values():
            mon.stop()
        self.monitors.clear()
        if self.dns is not None:
            self.dns.stop()
            self.dns = None
        if self._discovery_timer is not None:
            self._discovery_timer.cancel()
            self._discovery_timer = None

    def _apply_route(self, route: Route | None) -> None:
        self._stop_monitors()
        self.tracker.reset()
        self.route = route
        self.hops, self.isp_hop, self.discovered_at = [], None, None
        self.network_since = time.monotonic()
        if route is None:
            self.link_kind = "other"
            self.wifi = None
            self.local_ip = None
            return
        self.link_kind = self.kind_reader(route.iface)
        self.local_ip = local_address_for(route.gateway or self.public_targets[0])
        if self.link_kind != "wifi":
            self.wifi = None
        sched, tcp_sched = self._schedule(), self._tcp_schedule()
        n = len(self.public_targets) + 2
        step = sched.interval / n

        if route.gateway:
            self._add_monitor("router", route.gateway, self._router_methods(route.gateway), 0.0)
        for i, addr in enumerate(self.public_targets):
            methods: list = []
            if self.icmp:
                methods.append(("icmp", lambda a=addr: IcmpEcho(a), sched))
            if addr in KNOWN_RESOLVERS:
                methods.append(("dns", lambda a=addr: DnsQuery(a), sched))
            methods.append(("tcp:443", lambda a=addr: TcpConnect(a, 443), tcp_sched))
            self._add_monitor(f"net:{addr}", addr, methods, step * (i + 2))

        servers = self.dns_reader()
        self.dns = DnsTimer(self.loop, servers[0] if servers else self.public_targets[0],
                            list(self.config["dns_names"]))
        self.dns.start()
        self.loop.call_later(1.0, self.dns.lookup)
        self.loop.call_later(0.5, self._discover)
        self._poll_wifi()

    def _router_methods(self, gw: str) -> list:
        sched, tcp_sched, slow = self._schedule(), self._tcp_schedule(), self._slow_schedule()
        target = self.public_targets[0]
        methods: list = []
        if self.icmp:
            methods.append(("icmp", lambda: IcmpEcho(gw), sched))
        methods.append(("dns", lambda: DnsQuery(gw), sched))
        methods.append(("tcp:80", lambda: TcpConnect(gw, 80), tcp_sched))
        methods.append(("tcp:443", lambda: TcpConnect(gw, 443), tcp_sched))
        if self.icmp:
            methods.append(("icmp-ttl1", lambda: IcmpEcho(target, ttl=1, expect=gw), slow))
        else:
            methods.append(("udp-ttl1", lambda: UdpTtl(target, 1, expect=gw), slow))
        return methods

    def _isp_methods(self, ttl: int, addr: str) -> list:
        sched, slow = self._schedule(), self._slow_schedule()
        target = self.public_targets[0]
        methods: list = []
        if self.icmp:
            methods.append(("icmp", lambda: IcmpEcho(addr), sched))
            methods.append((f"icmp-ttl{ttl}", lambda: IcmpEcho(target, ttl=ttl, expect=addr), slow))
        else:
            methods.append((f"udp-ttl{ttl}", lambda: UdpTtl(target, ttl, expect=addr), slow))
        return methods

    def _add_monitor(self, key: str, addr: str, methods: list, offset: float) -> None:
        mon = TargetMonitor(self.loop, key, addr, methods, offset=offset)
        self.monitors[key] = mon
        mon.start()

    # -- path discovery -----------------------------------------------------------
    def _discover(self) -> None:
        """Find the hops toward the internet (TTL 1..MAX_TTL), then the ISP's router."""
        if self.route is None or self.discovering:
            return
        self.discovering = True
        target = self.public_targets[0]
        found: dict[int, tuple[str | None, float]] = {}
        probes: list[Probe] = []

        def on_reply(ttl: int, reply: Reply) -> None:
            if ttl not in found or found[ttl][0] is None:
                found[ttl] = (reply.addr, reply.rtt_ms)

        for ttl in range(1, MAX_TTL + 1):
            try:
                probe: Probe = IcmpEcho(target, ttl=ttl) if self.icmp else UdpTtl(target, ttl)
                probe.attach(self.loop, lambda r, t=ttl: on_reply(t, r))
            except OSError as exc:
                log.debug("hop discovery unavailable: %s", exc)
                self.discovering = False
                return
            probes.append(probe)
            for k in range(3):
                seq = ttl * 16 + k
                # Spread sends out a little: routers limit how fast they answer.
                self.loop.call_later(0.05 * k + 0.01 * ttl, probe.send, seq)

        def finish() -> None:
            for p in probes:
                p.detach()
            self.discovering = False
            if self.route is None:
                return
            hops = []
            for ttl in range(1, MAX_TTL + 1):
                addr, rtt = found.get(ttl, (None, None))
                hops.append({"ttl": ttl, "addr": addr, "rtt_ms": None if rtt is None else
                             round(rtt, 1)})
                if addr == target:
                    break
            # Trim the unanswered tail.
            while hops and hops[-1]["addr"] is None:
                hops.pop()
            self.hops = hops
            self.discovered_at = time.time()
            choice = pick_isp_hop([(h["ttl"], h["addr"]) for h in hops if h["addr"] != target],
                                  self.route.gateway)
            if choice != self.isp_hop:
                old = self.monitors.pop("isp", None)
                if old is not None:
                    old.stop()
                self.isp_hop = choice
                if choice is not None:
                    ttl, addr = choice
                    self._add_monitor("isp", addr, self._isp_methods(ttl, addr),
                                      self._schedule().interval / 2)
            log.info("path: %s; ISP hop: %s",
                     " > ".join(h["addr"] or "*" for h in hops), choice)
            self._discovery_timer = self.loop.call_later(DISCOVERY_EVERY, self._discover)

        self.loop.call_later(2.0, finish)

    # -- Wi-Fi --------------------------------------------------------------------
    def _poll_wifi(self) -> None:
        route = self.route
        if route is None or self.link_kind != "wifi":
            return
        if not self._wifi_busy:
            self._wifi_busy = True
            iface = route.iface

            def work() -> None:
                try:
                    info = self.wifi_reader.read(iface).as_dict()
                except Exception as exc:  # never let a D-Bus hiccup kill the engine
                    log.debug("wifi read failed: %s", exc)
                    info = None
                self.loop.call_soon_threadsafe(self._wifi_done, iface, info)

            try:
                self._workers.submit(work)
            except RuntimeError:
                self._wifi_busy = False
        self.loop.call_later(5.0, self._poll_wifi_if_current, route)

    def _poll_wifi_if_current(self, route: Route) -> None:
        if self.route == route:
            self._poll_wifi()

    def _wifi_done(self, iface: str, info: dict | None) -> None:
        self._wifi_busy = False
        if self.route is not None and self.route.iface == iface and self.link_kind == "wifi":
            self.wifi = info

    # -- the 2-second heartbeat ------------------------------------------------------
    def _tick(self) -> None:
        interval = float(self.config["interval"])
        self.loop.call_later(interval, self._tick)

        offset = _clock_offset()
        if offset - self._clock_offset > 5.0:
            log.info("resumed from suspend; starting fresh")
            self._clock_offset = offset
            self._apply_route(self.route_reader())
        self._clock_offset = offset

        route = self.route_reader()
        if route is not None and not iface_is_up(route.iface):
            route = None
        if (route and (route.iface, route.gateway)) != (
            self.route and (self.route.iface, self.route.gateway)
        ):
            log.info("network changed: %s -> %s", self.route, route)
            self._apply_route(route)

        if self.dns is not None:
            dns_every = max(5.0, float(self.config["dns_interval"]))
            last = self.dns.results[-1][0] if self.dns.results else 0.0
            if time.time() - last >= dns_every and not self.dns._pending:
                self.dns.lookup()

        self._maybe_fall_back()
        snap = self._build_snapshot()
        with self._snapshot_lock:
            self._snapshot = snap
        now = time.monotonic()
        if now - self._last_history >= HISTORY_EVERY and snap.get("level") != UNKNOWN:
            self._last_history = now
            if self.history is not None:
                try:
                    self.history.add(self._history_point(snap["ts"]))
                except Exception as exc:
                    log.warning("history error: %s", exc)
        for listener in list(self._listeners):
            try:
                listener(snap)
            except Exception:
                log.exception("snapshot listener failed")

    def _maybe_fall_back(self) -> None:
        now = self.loop.time()
        alive = any(
            any(s.rtt is not None for s in m.window.since(now - 10.0))
            for m in self.monitors.values()
        )
        if not alive:
            return  # a real outage: do not blame the probe method
        for mon in self.monitors.values():
            if mon.silent_for_method(FALLBACK_AFTER):
                if not mon.fall_back():
                    mon.exhausted = True

    # -- results ----------------------------------------------------------------
    def _segment_summary(self, mon: TargetMonitor | None, window: float,
                         now: float) -> Summary | None:
        if mon is None:
            return None
        main = mon.summary(window, now)
        loss = mon.summary(max(window, float(self.config.get("loss_window", 60))), now)
        return _merge_loss(main, loss)

    def _internet(self, window: float, loss_window: float, now: float) -> Summary | None:
        mons = [m for k, m in self.monitors.items() if k.startswith("net:")]
        if not mons:
            return None
        return combine_internet([m.summary(window, now) for m in mons],
                                [m.summary(loss_window, now) for m in mons])

    def _history_point(self, ts: float) -> dict:
        now = self.loop.time()
        net = self._internet(HISTORY_WINDOW, HISTORY_WINDOW, now)
        router = self.monitors.get("router")
        isp = self.monitors.get("isp")
        r = router.summary(HISTORY_WINDOW, now) if router else None
        i = isp.summary(HISTORY_WINDOW, now) if isp else None
        dns = self.dns.state() if self.dns else {}
        verdict = self.tracker.current
        return {
            "ts": round(ts, 1),
            "level": verdict.level if verdict else UNKNOWN,
            "net_ms": net.latency_ms if net else None,
            "net_jitter": net.jitter_ms if net else None,
            "net_loss": net.loss_pct if net else None,
            "router_ms": r.latency_ms if r else None,
            "router_jitter": r.jitter_ms if r else None,
            "router_loss": r.loss_pct if r else None,
            "isp_ms": i.latency_ms if i else None,
            "isp_jitter": i.jitter_ms if i else None,
            "isp_loss": i.loss_pct if i else None,
            "dns_ms": dns.get("last_ms"),
            "wifi_dbm": (self.wifi or {}).get("signal_dbm"),
        }

    def _segment_dict(self, key: str, summary: Summary | None, mon: TargetMonitor | None,
                      verdict: Verdict) -> dict:
        th = self.thresholds
        if summary is None or not summary.sent:
            level = UNKNOWN
        elif not summary.received:
            level = POOR
        else:
            level = worst(
                metric_level(summary.loss_pct, th.loss_fair, th.loss_poor),
                metric_level(summary.jitter_ms, th.jitter_fair, th.jitter_poor),
                metric_level(summary.latency_ms, th.latency_fair, th.latency_poor),
            )
        # Where the verdict says the problem starts, and everything downstream of it.
        order = ["router", "isp", "internet"]
        status = "ok"
        if verdict.origin in order and verdict.level in (FAIR, POOR, "offline"):
            if order.index(key) == order.index(verdict.origin):
                status = "origin"
            elif order.index(key) > order.index(verdict.origin):
                status = "affected"
        if summary is None or (mon is not None and mon.exhausted and not mon.ever_replied):
            status = "unmeasured" if status == "ok" else status
        out = {
            "id": key,
            "label": SEGMENT_LABELS[key],
            "level": level,
            "status": status,
            "address": mon.address if mon else None,
            "method": mon.method_name if mon else None,
        }
        out.update((summary or Summary()).as_dict())
        return out

    def _build_snapshot(self) -> dict:
        now = self.loop.time()
        window = float(self.config["window"])
        loss_window = max(window, float(self.config.get("loss_window", 60)))
        router_mon = self.monitors.get("router")
        isp_mon = self.monitors.get("isp")
        net = self._internet(window, loss_window, now)
        net_recent = self._internet(RECENT_WINDOW, RECENT_WINDOW, now)
        router = self._segment_summary(router_mon, window, now)
        router_recent = router_mon.summary(RECENT_WINDOW, now) if router_mon else None
        isp = self._segment_summary(isp_mon, window, now)
        isp_recent = isp_mon.summary(RECENT_WINDOW, now) if isp_mon else None
        router_measurable = bool(router_mon and (router_mon.ever_replied or
                                                 not router_mon.exhausted))
        inputs = Inputs(
            internet=net,
            internet_recent=net_recent,
            router=router,
            router_recent=router_recent,
            isp=isp if isp_mon and isp_mon.ever_replied else None,
            isp_recent=isp_recent if isp_mon and isp_mon.ever_replied else None,
            has_route=self.route is not None,
            link_kind=self.link_kind,
            wifi=self.wifi,
            router_measurable=router_measurable,
        )
        verdict = self.tracker.update(assess(inputs, self.thresholds))

        dns = self.dns.state() if self.dns else None
        tips = list(verdict.tips)
        if dns and dns.get("latency_ms") and dns["latency_ms"] >= 300 and verdict.level == GOOD:
            tips.append(f"DNS lookups are slow ({dns['latency_ms']:.0f} ms): joining calls may "
                        "take a moment longer.")

        targets = []
        for key, mon in self.monitors.items():
            if key.startswith("net:"):
                s = _merge_loss(mon.summary(window, now), mon.summary(loss_window, now))
                targets.append({"address": mon.address, "method": mon.method_name,
                                **s.as_dict()})

        segments = [self._segment_dict("router", router, router_mon, verdict)]
        if isp_mon is not None or self.discovering or not self.discovered_at:
            segments.append(self._segment_dict("isp", isp if isp_mon and isp_mon.ever_replied
                                               else None, isp_mon, verdict))
        internet = self._segment_dict("internet", net, None, verdict)
        internet["targets"] = targets
        segments.append(internet)

        methods = {m.method_name for m in self.monitors.values()}
        snap = {
            "version": __version__,
            "ts": round(time.time(), 2),
            "level": verdict.level,
            "headline": verdict.headline,
            "detail": verdict.detail,
            "culprit": verdict.culprit,
            "cause": verdict.cause,
            "origin": verdict.origin,
            "tips": tips,
            "metrics": {
                "latency_ms": net.as_dict()["latency_ms"] if net else None,
                "jitter_ms": net.as_dict()["jitter_ms"] if net else None,
                "loss_pct": net.as_dict()["loss_pct"] if net else None,
            },
            "levels": verdict.metrics,
            "link": {
                "iface": self.route.iface if self.route else None,
                "kind": self.link_kind,
                "gateway": self.route.gateway if self.route else None,
                "local_ip": self.local_ip,
            },
            "wifi": self.wifi,
            "segments": segments,
            "dns": dns,
            "path": {
                "hops": self.hops,
                "isp_hop": ({"ttl": self.isp_hop[0], "address": self.isp_hop[1]}
                            if self.isp_hop else None),
                "discovered_at": self.discovered_at,
            },
            "probe": {
                "icmp": self.icmp,
                "methods": sorted(methods),
                "unprivileged": True,
            },
            "thresholds": self.thresholds.as_dict(),
            "window": window,
        }
        snap.update(self.extra)
        return snap

    # -- speed test support ------------------------------------------------------
    def internet_latency_since(self, seconds: float) -> Summary | None:
        """Latency to the internet targets over the last ``seconds`` (thread-safe enough
        for reporting: called from the speed test through call_in_engine)."""
        now = self.loop.time()
        return self._internet(seconds, seconds, now)
