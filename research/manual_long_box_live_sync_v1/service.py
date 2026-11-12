"""Long-running drawings poller + debounce (no dashboard restart)."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from .activation import load_activations
from .config import DEBOUNCE_SEC, DEFAULT_POLL_SEC
from .drawing_watch import DrawingChangeDetector
from .exchange_dry_run import DryRunExchangeAdapter
from .persistence import EventLog, StateStore
from .sync_engine import ManualLongBoxSyncEngine


class SyncService:
    def __init__(
        self,
        *,
        drawings_path: Path,
        activations_path: Path,
        state_path: Path,
        event_log_path: Path,
        poll_sec: float = DEFAULT_POLL_SEC,
        debounce_sec: float = DEBOUNCE_SEC,
        engine: ManualLongBoxSyncEngine | None = None,
    ) -> None:
        self.drawings_path = drawings_path
        self.activations_path = activations_path
        self.detector = DrawingChangeDetector(drawings_path)
        self.state = StateStore(state_path)
        self.events = EventLog(event_log_path)
        if engine is not None:
            self.engine = engine
            self.exchange = engine.exchange
        else:
            self.exchange = DryRunExchangeAdapter()
            self.engine = ManualLongBoxSyncEngine(self.exchange, self.state, self.events)
        self.poll_sec = poll_sec
        self.debounce_sec = debounce_sec
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._debounce_timer: threading.Timer | None = None
        self._debounce_lock = threading.Lock()
        self._probe_start: float | None = None

    def reload_activations(self):
        return load_activations(self.activations_path)

    def run_once(self) -> list[dict]:
        activations = self.reload_activations()
        from .drawing_watch import observe_drawings

        obs = observe_drawings(self.drawings_path)
        if not obs:
            return []
        return self.engine.process_observation(
            obs, activations, latency_probe_start=self._probe_start
        )

    def _flush_debounced(self) -> None:
        self._probe_start = None
        self.run_once()

    def _on_change(self) -> None:
        import time as _time

        self._probe_start = _time.time()
        with self._debounce_lock:
            if self._debounce_timer:
                self._debounce_timer.cancel()
            self._debounce_timer = threading.Timer(self.debounce_sec, self._flush_debounced)
            self._debounce_timer.daemon = True
            self._debounce_timer.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            changed, obs = self.detector.poll()
            if changed and obs:
                self._on_change()
            self._stop.wait(self.poll_sec)

    def start(self, background: bool = True) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        if background:
            self._thread = threading.Thread(target=self._loop, name="mlb-sync", daemon=True)
            self._thread.start()
        else:
            self._loop()

    def stop(self) -> None:
        self._stop.set()
        with self._debounce_lock:
            if self._debounce_timer:
                self._debounce_timer.cancel()
        if self._thread:
            self._thread.join(timeout=2.0)
