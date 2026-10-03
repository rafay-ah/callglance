"""On-demand speed test with latency under load (bufferbloat).

Throughput comes from Cloudflare's public speed-test endpoints
(``speed.cloudflare.com/__down`` and ``/__up``), the same ones the browser test
uses. While data flows, the engine keeps measuring latency to the internet, so
we can tell how much the connection lags when it is busy. That lag, not raw
speed, is what usually ruins calls when someone else starts a big download.

Only the standard library is used; nothing runs unless you ask for a test.
"""

from __future__ import annotations

import http.client
import logging
import os
import ssl
import threading
import time
import urllib.parse
from base64 import b64encode
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

log = logging.getLogger(__name__)

HOST = "speed.cloudflare.com"
STREAMS = 4
PHASE_SECONDS = 8.0
RAMP_SECONDS = 1.0  # TCP slow start: leave the first second out of the average
MAX_DOWNLOAD_BYTES = 250_000_000
MAX_UPLOAD_BYTES = 100_000_000
MAX_REQUEST_BYTES = 25_000_000  # larger requests get refused by the endpoint
TIMEOUT = 15.0

# Waveform-style bufferbloat grades: extra latency under load, in ms.
GRADES = [(5, "A+"), (30, "A"), (60, "B"), (200, "C"), (400, "D")]


def grade_for(increase_ms: float | None) -> str | None:
    if increase_ms is None:
        return None
    for limit, grade in GRADES:
        if increase_ms < limit:
            return grade
    return "F"


def call_capacity(down: float | None, up: float | None) -> str:
    """What kind of call fits through this connection (vendor bandwidth guidance)."""
    if down is None or up is None:
        return "unknown"
    slowest = min(down, up)
    if slowest >= 4.0:  # Zoom 1080p: 3.8 up / 3.0 down; Teams "best": 4 Mbps
        return "hd"
    if slowest >= 1.5:  # Teams recommended 1:1 video; Zoom 720p 1.2 Mbps
        return "video"
    if slowest >= 0.1:  # audio needs ~60-100 kbps
        return "audio"
    return "none"


CAPACITY_TEXT = {
    "hd": "Enough for HD video calls",
    "video": "Enough for video calls (up to 720p)",
    "audio": "Audio calls only",
    "none": "Too slow for calls",
    "unknown": "",
}


@dataclass
class SpeedTestState:
    status: str = "idle"  # idle | running | done | error | cancelled
    phase: str | None = None  # latency | download | upload
    progress: float = 0.0  # 0..1 over the whole test
    started_at: float | None = None
    finished_at: float | None = None
    download_mbps: float | None = None
    upload_mbps: float | None = None
    idle_latency_ms: float | None = None
    loaded_down_ms: float | None = None
    loaded_up_ms: float | None = None
    bufferbloat_ms: float | None = None
    grade: str | None = None
    capacity: str = "unknown"
    summary: str = ""
    data_used_mb: float = 0.0
    error: str | None = None
    live_mbps: float | None = None
    tips: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        out = asdict(self)
        for key in ("download_mbps", "upload_mbps", "live_mbps", "data_used_mb"):
            if out[key] is not None:
                out[key] = round(out[key], 1)
        for key in ("idle_latency_ms", "loaded_down_ms", "loaded_up_ms", "bufferbloat_ms"):
            if out[key] is not None:
                out[key] = round(out[key], 1)
        out["progress"] = round(out["progress"], 3)
        return out


class Cancelled(Exception):
    pass


def _proxy_for_https() -> tuple[str, int, dict] | None:
    url = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
    if not url:
        return None
    no_proxy = os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or ""
    if any(HOST.endswith(d.strip().lstrip(".")) for d in no_proxy.split(",") if d.strip()):
        return None
    parsed = urllib.parse.urlsplit(url if "://" in url else f"http://{url}")
    headers = {}
    if parsed.username:
        creds = f"{urllib.parse.unquote(parsed.username)}:{urllib.parse.unquote(parsed.password or '')}"
        headers["Proxy-Authorization"] = "Basic " + b64encode(creds.encode()).decode()
    return parsed.hostname or "", parsed.port or 8080, headers


def _connect() -> http.client.HTTPSConnection:
    context = ssl.create_default_context()
    proxy = _proxy_for_https()
    if proxy:
        conn = http.client.HTTPSConnection(proxy[0], proxy[1], timeout=TIMEOUT, context=context)
        conn.set_tunnel(HOST, 443, headers=proxy[2])
    else:
        conn = http.client.HTTPSConnection(HOST, 443, timeout=TIMEOUT, context=context)
    return conn


class _Counter:
    def __init__(self) -> None:
        self.bytes = 0
        self.lock = threading.Lock()

    def add(self, n: int) -> None:
        with self.lock:
            self.bytes += n


class _UploadBody:
    """A file-like request body that counts what http.client has sent."""

    CHUNK = os.urandom(64 * 1024)

    def __init__(self, size: int, counter: _Counter, stop: threading.Event) -> None:
        self.remaining = size
        self.counter = counter
        self.stop = stop

    def read(self, n: int = -1) -> bytes:
        if self.stop.is_set():
            raise Cancelled()
        if self.remaining <= 0:
            return b""
        n = min(n if n and n > 0 else len(self.CHUNK), len(self.CHUNK), self.remaining)
        self.remaining -= n
        self.counter.add(n)
        return self.CHUNK[:n]


class SpeedTest:
    """Runs one test in a background thread and reports progress via ``on_update``."""

    def __init__(
        self,
        latency_since: Callable[[float], float | None],
        on_update: Callable[[SpeedTestState], None],
    ) -> None:
        self.latency_since = latency_since  # median internet latency over the last N s
        self.on_update = on_update
        self.state = SpeedTestState()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        if self.running:
            return False
        self._stop.clear()
        self.state = SpeedTestState(status="running", phase="latency", started_at=time.time())
        self._emit()
        self._thread = threading.Thread(target=self._run, name="callglance-speedtest",
                                        daemon=True)
        self._thread.start()
        return True

    def cancel(self) -> None:
        self._stop.set()

    def _emit(self) -> None:
        try:
            self.on_update(SpeedTestState(**asdict(self.state)))
        except Exception:
            log.exception("speed test listener failed")

    # -- phases ---------------------------------------------------------------
    def _run(self) -> None:
        st = self.state
        try:
            st.idle_latency_ms = self.latency_since(10.0)
            st.progress = 0.04
            self._emit()
            st.phase = "download"
            self._emit()
            st.download_mbps, used = self._transfer("down", 0.04, 0.52)
            st.loaded_down_ms = self.latency_since(PHASE_SECONDS - RAMP_SECONDS)
            st.data_used_mb += used / 1e6
            st.phase = "upload"
            self._emit()
            st.upload_mbps, used = self._transfer("up", 0.52, 1.0)
            st.loaded_up_ms = self.latency_since(PHASE_SECONDS - RAMP_SECONDS)
            st.data_used_mb += used / 1e6
            self._finish()
        except Cancelled:
            st.status = "cancelled"
            st.summary = "Speed test cancelled"
        except Exception as exc:
            log.warning("speed test failed: %s", exc)
            st.status = "error"
            st.error = _friendly_error(exc)
            st.summary = "Speed test failed"
        finally:
            st.phase = None
            st.live_mbps = None
            st.finished_at = time.time()
            if st.status == "running":
                st.status = "done"
            self._emit()

    def _finish(self) -> None:
        st = self.state
        loaded = [v for v in (st.loaded_down_ms, st.loaded_up_ms) if v is not None]
        if st.idle_latency_ms is not None and loaded:
            st.bufferbloat_ms = max(0.0, max(loaded) - st.idle_latency_ms)
            st.grade = grade_for(st.bufferbloat_ms)
        st.capacity = call_capacity(st.download_mbps, st.upload_mbps)
        st.summary = CAPACITY_TEXT[st.capacity]
        st.progress = 1.0
        if st.grade in ("C", "D", "F"):
            st.tips.append(
                f"Latency jumps by {st.bufferbloat_ms:.0f} ms when the connection is busy "
                "(bufferbloat). Calls will lag whenever someone downloads or uploads. "
                "Turning on SQM / Smart Queue in your router fixes this.")
        elif st.grade == "B":
            st.tips.append("Latency rises a little under load. Fine for calls, unless the "
                           "connection is saturated.")

    def _transfer(self, direction: str, p0: float, p1: float) -> tuple[float | None, int]:
        counter = _Counter()
        stop = threading.Event()
        errors: list[BaseException] = []
        cap = MAX_DOWNLOAD_BYTES if direction == "down" else MAX_UPLOAD_BYTES

        def worker() -> None:
            size = 1_000_000
            conn = None
            try:
                while not stop.is_set() and not self._stop.is_set():
                    if conn is None:
                        conn = _connect()
                    started = time.monotonic()
                    if direction == "down":
                        conn.request("GET", f"/__down?bytes={size}")
                        resp = conn.getresponse()
                        if resp.status != 200:
                            raise RuntimeError(f"HTTP {resp.status} from {HOST}")
                        while not stop.is_set():
                            chunk = resp.read(64 * 1024)
                            if not chunk:
                                break
                            counter.add(len(chunk))
                            if self._stop.is_set():
                                raise Cancelled()
                        if stop.is_set():
                            conn.close()
                            conn = None
                            break
                    else:
                        body = _UploadBody(size, counter, stop)
                        conn.request("POST", "/__up", body=body,
                                     headers={"Content-Length": str(size),
                                              "Content-Type": "application/octet-stream"})
                        resp = conn.getresponse()
                        resp.read()
                        if resp.status != 200:
                            raise RuntimeError(f"HTTP {resp.status} from {HOST}")
                    took = time.monotonic() - started
                    if took < 1.0:
                        size = min(size * 2, MAX_REQUEST_BYTES)
            except Cancelled:
                pass
            except Exception as exc:
                if not stop.is_set():
                    errors.append(exc)
            finally:
                if conn is not None:
                    conn.close()

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(STREAMS)]
        start = time.monotonic()
        for t in threads:
            t.start()
        samples: list[tuple[float, int]] = []
        while True:
            time.sleep(0.25)
            now = time.monotonic() - start
            total = counter.bytes
            samples.append((now, total))
            recent = [s for s in samples if s[0] >= now - 1.0]
            if len(recent) >= 2 and recent[-1][0] > recent[0][0]:
                self.state.live_mbps = (recent[-1][1] - recent[0][1]) * 8 / 1e6 / (
                    recent[-1][0] - recent[0][0])
            self.state.progress = p0 + (p1 - p0) * min(1.0, now / PHASE_SECONDS)
            self._emit()
            if self._stop.is_set():
                stop.set()
                raise Cancelled()
            if now >= PHASE_SECONDS or total >= cap or (errors and not any(
                    t.is_alive() for t in threads)):
                break
        stop.set()
        for t in threads:
            t.join(timeout=2.0)
        if errors and counter.bytes == 0:
            raise errors[0]
        # Average after the ramp-up second (or over everything for very short runs).
        end_t, end_b = samples[-1]
        base = next((s for s in samples if s[0] >= RAMP_SECONDS), samples[0])
        if end_t - base[0] < 0.5:
            base = (0.0, 0)
        mbps = (end_b - base[1]) * 8 / 1e6 / max(end_t - base[0], 1e-6)
        return mbps, counter.bytes


def _friendly_error(exc: BaseException) -> str:
    if isinstance(exc, (TimeoutError, OSError)) and "timed out" in str(exc):
        return "The speed test server did not answer in time."
    if isinstance(exc, ssl.SSLError):
        return "Could not establish a secure connection to the speed test server."
    if isinstance(exc, OSError):
        return f"Network error: {exc.strerror or exc}"
    return str(exc)
