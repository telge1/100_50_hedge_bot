"""Bounded raw delta ringbuffer for Full-OB flight recorder."""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.record_envelope import (
    approx_envelope_bytes,
)


@dataclass(frozen=True)
class RawItem:
    receive_time_ns: int
    payload: dict[str, Any]
    kind: str  # delta | lifecycle | trade | meta
    approx_bytes: int


class BoundedRawRingBuffer:
    """Time-bounded + byte/message-bounded ring. Overflow is explicit."""

    def __init__(
        self,
        *,
        window_sec: float,
        max_messages: int,
        max_bytes: int,
    ) -> None:
        self.window_sec = float(window_sec)
        self.max_messages = int(max_messages)
        self.max_bytes = int(max_bytes)
        self._items: deque[RawItem] = deque()
        self._bytes = 0
        self._lock = threading.RLock()
        self.overflow_count = 0
        self.dropped_oldest = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    @property
    def nbytes(self) -> int:
        with self._lock:
            return self._bytes

    def append(
        self,
        payload: dict[str, Any],
        *,
        kind: str = "delta",
        receive_time_ns: int | None = None,
    ) -> None:
        ts = int(receive_time_ns if receive_time_ns is not None else time.time_ns())
        # Envelope is already a compact copy; do not re-orjson on the hot path.
        approx = int(payload.get("_approx_bytes") or approx_envelope_bytes(payload))
        item = RawItem(receive_time_ns=ts, payload=payload, kind=kind, approx_bytes=approx)
        with self._lock:
            self._items.append(item)
            self._bytes += approx
            self._evict_locked(now_ns=ts)

    def _evict_locked(self, *, now_ns: int) -> None:
        cutoff = now_ns - int(self.window_sec * 1_000_000_000)
        while self._items and self._items[0].receive_time_ns < cutoff:
            old = self._items.popleft()
            self._bytes -= old.approx_bytes
            self.dropped_oldest += 1
        while len(self._items) > self.max_messages or self._bytes > self.max_bytes:
            if not self._items:
                break
            old = self._items.popleft()
            self._bytes -= old.approx_bytes
            self.dropped_oldest += 1
            self.overflow_count += 1

    def snapshot(self) -> list[RawItem]:
        with self._lock:
            return list(self._items)

    def flush(self) -> list[RawItem]:
        with self._lock:
            out = list(self._items)
            self._items.clear()
            self._bytes = 0
            return out

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._bytes = 0

    def coverage_seconds(self, now_ns: int | None = None) -> float:
        with self._lock:
            if not self._items:
                return 0.0
            now = int(now_ns if now_ns is not None else time.time_ns())
            return max(0.0, (now - self._items[0].receive_time_ns) / 1e9)

    def mark_overflow(self) -> None:
        with self._lock:
            self.overflow_count += 1
