"""History of measurements for the graph, kept in a small SQLite file.

A point is recorded every few seconds. Points are buffered in memory and
written in batches (once a minute) so the disk is not touched constantly.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path

log = logging.getLogger(__name__)

FIELDS = (
    "ts", "level",
    "net_ms", "net_jitter", "net_loss",
    "router_ms", "router_jitter", "router_loss",
    "isp_ms", "isp_jitter", "isp_loss",
    "dns_ms", "wifi_dbm",
)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS points (
    ts REAL PRIMARY KEY,
    level TEXT,
    {", ".join(f"{name} REAL" for name in FIELDS[2:])}
)
"""


class History:
    def __init__(self, path: Path | None, keep_hours: float = 24, memory_hours: float = 1.0,
                 flush_every: float = 60.0) -> None:
        self.path = path
        self.keep = keep_hours * 3600
        self.memory = deque()
        self.memory_span = memory_hours * 3600
        self.flush_every = flush_every
        self._unsaved: list[dict] = []
        self._last_flush = time.monotonic()
        self._lock = threading.Lock()
        self._db: sqlite3.Connection | None = None
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                self._db = sqlite3.connect(str(path), check_same_thread=False)
                self._db.execute("PRAGMA journal_mode=WAL")
                self._db.execute("PRAGMA synchronous=NORMAL")
                self._db.execute(SCHEMA)
                self._db.commit()
                self._load_recent()
            except sqlite3.Error as exc:
                log.warning("History disabled (%s): %s", path, exc)
                self._db = None

    def _load_recent(self) -> None:
        assert self._db is not None
        since = time.time() - self.memory_span
        rows = self._db.execute(
            f"SELECT {', '.join(FIELDS)} FROM points WHERE ts >= ? ORDER BY ts", (since,)
        ).fetchall()
        for row in rows:
            self.memory.append(dict(zip(FIELDS, row)))

    def add(self, point: dict) -> None:
        clean = {name: point.get(name) for name in FIELDS}
        with self._lock:
            self.memory.append(clean)
            horizon = clean["ts"] - self.memory_span
            while self.memory and self.memory[0]["ts"] < horizon:
                self.memory.popleft()
            self._unsaved.append(clean)
        if time.monotonic() - self._last_flush >= self.flush_every:
            self.flush()

    def since(self, seconds: float) -> list[dict]:
        horizon = time.time() - seconds
        with self._lock:
            if seconds <= self.memory_span:
                return [p for p in self.memory if p["ts"] >= horizon]
        if self._db is None:
            return [p for p in self.memory if p["ts"] >= horizon]
        self.flush()
        with self._lock:
            rows = self._db.execute(
                f"SELECT {', '.join(FIELDS)} FROM points WHERE ts >= ? ORDER BY ts", (horizon,)
            ).fetchall()
        return [dict(zip(FIELDS, row)) for row in rows]

    def flush(self) -> None:
        with self._lock:
            pending, self._unsaved = self._unsaved, []
            self._last_flush = time.monotonic()
            if self._db is None or not pending:
                return
            try:
                self._db.executemany(
                    f"INSERT OR REPLACE INTO points ({', '.join(FIELDS)}) "
                    f"VALUES ({', '.join('?' for _ in FIELDS)})",
                    [tuple(p[name] for name in FIELDS) for p in pending],
                )
                self._db.execute("DELETE FROM points WHERE ts < ?", (time.time() - self.keep,))
                self._db.commit()
            except sqlite3.Error as exc:
                log.warning("Could not save history: %s", exc)

    def close(self) -> None:
        self.flush()
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None
