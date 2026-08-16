"""Buffered ClickHouse candle inserts for live collector."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Sequence

from signal_generator.db.candles import Candle1m, CandleRepository

logger = logging.getLogger(__name__)


class CandleInsertBuffer:
    """Batch closed candles; flush by max rows or interval."""

    def __init__(
        self,
        repo: CandleRepository,
        *,
        max_rows: int = 50,
        flush_interval_s: float = 0.5,
    ) -> None:
        self.repo = repo
        self.max_rows = max(1, max_rows)
        self.flush_interval_s = max(0.05, flush_interval_s)
        self._buf: list[Candle1m] = []
        self._lock = threading.Lock()
        self._last_flush = datetime.now(timezone.utc)
        self.total_inserted = 0
        self.flush_count = 0

    def add(self, candle: Candle1m) -> int:
        """Add candle; may flush. Returns rows inserted this call (0 if buffered)."""
        with self._lock:
            self._buf.append(candle)
            if len(self._buf) >= self.max_rows:
                return self._flush_unlocked()
            age = (datetime.now(timezone.utc) - self._last_flush).total_seconds()
            if age >= self.flush_interval_s:
                return self._flush_unlocked()
            return 0

    def add_many(self, candles: Sequence[Candle1m]) -> int:
        inserted = 0
        with self._lock:
            self._buf.extend(candles)
            while len(self._buf) >= self.max_rows:
                inserted += self._flush_unlocked()
            age = (datetime.now(timezone.utc) - self._last_flush).total_seconds()
            if self._buf and age >= self.flush_interval_s:
                inserted += self._flush_unlocked()
        return inserted

    def flush(self) -> int:
        with self._lock:
            return self._flush_unlocked()

    def _flush_unlocked(self) -> int:
        if not self._buf:
            return 0
        batch = self._buf
        self._buf = []
        n = self.repo.insert_candles(batch)
        self.total_inserted += n
        self.flush_count += 1
        self._last_flush = datetime.now(timezone.utc)
        logger.debug("candle buffer flush rows=%s", n)
        return n

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._buf)
