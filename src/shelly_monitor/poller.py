"""Background poller for `serve --poll`: the polling loop in a daemon thread of the dashboard."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager

log = logging.getLogger(__name__)

RESTART_DELAY = 60.0


class BackgroundPoller:
    """Runs `target(stop, mark_ok)` in a daemon thread. `target` loops until `stop` is set and
    calls `mark_ok()` after every successful cycle. If it raises, the error is logged and the
    loop is started again after `restart_delay` seconds: it never stops the web server."""

    def __init__(self, target: Callable[[threading.Event, Callable[[], None]], None],
                 restart_delay: float = RESTART_DELAY):
        self.target = target
        self.restart_delay = restart_delay
        self.last_ok: float | None = None  # epoch of the last successful cycle
        self.started: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._crashed = False  # the loop died and waits for its restart

    @property
    def running(self) -> bool:
        """The polling loop is alive (False while waiting to restart after a crash)."""
        return self._thread is not None and self._thread.is_alive() and not self._crashed

    def mark_ok(self) -> None:
        self.last_ok = time.time()

    def _supervise(self) -> None:
        while not self._stop.is_set():
            try:
                self.target(self._stop, self.mark_ok)
            except Exception:
                log.error("poller crashed; restarting in %gs", self.restart_delay, exc_info=True)
                self._crashed = True
                self._stop.wait(self.restart_delay)
                self._crashed = False
            else:
                return

    def start(self) -> None:
        self.started = time.time()
        self._stop.clear()
        self._thread = threading.Thread(target=self._supervise, name="poller", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)


class Maintenance:
    """Serialises the in-process poller with a database restore.

    The poller holds `lock` for a whole cycle and keeps its read-write store in `store` (opened
    with `shared=True`). `exclusive()` waits for the running cycle to end, blocks the next ones,
    closes the poller's connection (the poller reopens it at its next cycle) and releases on exit.
    Backups need none of this: they read a consistent snapshot through their own connection."""

    def __init__(self):
        self.lock = threading.Lock()
        self.store = None

    @contextmanager
    def exclusive(self):
        with self.lock:
            if self.store is not None:
                self.store.close()
                self.store = None
            yield
