"""A tiny single-threaded event loop for the measurement engine.

The engine runs in its own thread so that probe timing is not disturbed by D-Bus
traffic or UI work in the main GLib loop. ``selectors`` plus a timer heap is all
it needs, and keeps the engine free of any GLib dependency (so it is easy to
test and to use from the command line).
"""

from __future__ import annotations

import heapq
import itertools
import os
import selectors
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any


class TimerHandle:
    __slots__ = ("when", "callback", "args", "cancelled")

    def __init__(self, when: float, callback: Callable[..., Any], args: tuple):
        self.when = when
        self.callback = callback
        self.args = args
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class Loop:
    def __init__(self) -> None:
        self._selector = selectors.DefaultSelector()
        self._timers: list[tuple[float, int, TimerHandle]] = []
        self._counter = itertools.count()
        self._handlers: dict[int, list[Any]] = {}  # fd -> [events, fileobj, read_cb, write_cb]
        self._pending: deque[tuple[Callable[..., Any], tuple]] = deque()
        self._lock = threading.Lock()
        self._wake_r, self._wake_w = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        self._selector.register(self._wake_r, selectors.EVENT_READ, None)
        self._running = False
        self._thread_id: int | None = None
        self.on_error: Callable[[BaseException], None] | None = None

    # -- time -------------------------------------------------------------
    @staticmethod
    def time() -> float:
        return time.monotonic()

    # -- timers -----------------------------------------------------------
    def call_at(self, when: float, callback: Callable[..., Any], *args: Any) -> TimerHandle:
        handle = TimerHandle(when, callback, args)
        heapq.heappush(self._timers, (when, next(self._counter), handle))
        return handle

    def call_later(self, delay: float, callback: Callable[..., Any], *args: Any) -> TimerHandle:
        return self.call_at(self.time() + max(0.0, delay), callback, *args)

    def call_soon_threadsafe(self, callback: Callable[..., Any], *args: Any) -> None:
        with self._lock:
            self._pending.append((callback, args))
        try:
            os.write(self._wake_w, b"\0")
        except BlockingIOError:
            pass  # the pipe is already full of wake-ups

    # -- I/O --------------------------------------------------------------
    def _update(self, fd: int) -> None:
        entry = self._handlers[fd]
        events = (selectors.EVENT_READ if entry[2] else 0) | (
            selectors.EVENT_WRITE if entry[3] else 0
        )
        if events == 0:
            del self._handlers[fd]
            self._selector.unregister(fd)
        elif entry[0] == 0:
            entry[0] = events
            self._selector.register(fd, events, None)
        elif entry[0] != events:
            entry[0] = events
            self._selector.modify(fd, events, None)

    def add_reader(self, fileobj: Any, callback: Callable[[], Any]) -> None:
        fd = fileobj if isinstance(fileobj, int) else fileobj.fileno()
        self._handlers.setdefault(fd, [0, fileobj, None, None])[2] = callback
        self._update(fd)

    def add_writer(self, fileobj: Any, callback: Callable[[], Any]) -> None:
        fd = fileobj if isinstance(fileobj, int) else fileobj.fileno()
        self._handlers.setdefault(fd, [0, fileobj, None, None])[3] = callback
        self._update(fd)

    def remove_reader(self, fileobj: Any) -> None:
        fd = fileobj if isinstance(fileobj, int) else fileobj.fileno()
        if fd in self._handlers:
            self._handlers[fd][2] = None
            self._update(fd)

    def remove_writer(self, fileobj: Any) -> None:
        fd = fileobj if isinstance(fileobj, int) else fileobj.fileno()
        if fd in self._handlers:
            self._handlers[fd][3] = None
            self._update(fd)

    # -- running ----------------------------------------------------------
    def _run_callback(self, callback: Callable[..., Any], args: tuple) -> None:
        try:
            callback(*args)
        except Exception as exc:  # keep the engine alive whatever a probe does
            if self.on_error is not None:
                self.on_error(exc)
            else:
                raise

    def run(self) -> None:
        self._running = True
        self._thread_id = threading.get_ident()
        while self._running:
            timeout: float | None = None
            while self._timers and self._timers[0][2].cancelled:
                heapq.heappop(self._timers)
            if self._timers:
                timeout = max(0.0, self._timers[0][0] - self.time())
            if self._pending:
                timeout = 0.0
            for key, events in self._selector.select(timeout):
                fd = key.fd
                if fd == self._wake_r:
                    try:
                        while os.read(self._wake_r, 512):
                            pass
                    except BlockingIOError:
                        pass
                    continue
                entry = self._handlers.get(fd)
                if entry is None:
                    continue
                if events & selectors.EVENT_READ and entry[2] is not None:
                    self._run_callback(entry[2], ())
                entry = self._handlers.get(fd)
                if entry is not None and events & selectors.EVENT_WRITE and entry[3] is not None:
                    self._run_callback(entry[3], ())
            while True:
                with self._lock:
                    if not self._pending:
                        break
                    callback, args = self._pending.popleft()
                self._run_callback(callback, args)
            now = self.time()
            while self._timers and self._timers[0][0] <= now:
                _, _, handle = heapq.heappop(self._timers)
                if not handle.cancelled:
                    self._run_callback(handle.callback, handle.args)
        self._thread_id = None

    def stop(self) -> None:
        if self._thread_id is None or self._thread_id == threading.get_ident():
            self._running = False
        else:
            self.call_soon_threadsafe(self._set_stopped)

    def _set_stopped(self) -> None:
        self._running = False

    def close(self) -> None:
        self._selector.close()
        for fd in (self._wake_r, self._wake_w):
            try:
                os.close(fd)
            except OSError:
                pass
