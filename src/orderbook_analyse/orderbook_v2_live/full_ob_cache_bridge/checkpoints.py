"""Periodic in-RAM book checkpoints for causal pre-roll anchors (anchor_retention_v2)."""

from __future__ import annotations

import hashlib
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Sequence

import orjson

from orderbook_analyse.orderbook_v2_live.full_book_state import ConsistentBookSnapshot
from orderbook_analyse.orderbook_v2_live.full_ob_cache_bridge.protocol import (
    CHECKPOINT_KIND_INITIAL,
    CHECKPOINT_KIND_PERIODIC,
    CHECKPOINT_KIND_RESYNC,
)


def book_content_hash(bids: Sequence[Sequence[float]], asks: Sequence[Sequence[float]]) -> str:
    """Deterministic hash of normalized full book levels."""
    nb = sorted(([float(p), float(q)] for p, q in bids), key=lambda x: -x[0])
    na = sorted(([float(p), float(q)] for p, q in asks), key=lambda x: x[0])
    return hashlib.sha256(
        orjson.dumps({"b": nb, "a": na}, option=orjson.OPT_SORT_KEYS)
    ).hexdigest()


@dataclass(frozen=True)
class BookCheckpoint:
    receive_time_ns: int
    event_ts_ms: int | None
    cts_ms: int | None
    update_id: int | None
    seq: int | None
    bids: list[list[float]]
    asks: list[list[float]]
    approx_bytes: int
    symbol: str = ""
    epoch_id: int = 0
    kind: str = CHECKPOINT_KIND_PERIODIC
    bid_level_count: int = 0
    ask_level_count: int = 0
    book_content_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "receive_time_ns": self.receive_time_ns,
            "event_ts_ms": self.event_ts_ms,
            "cts_ms": self.cts_ms,
            "u": self.update_id,
            "seq": self.seq,
            "b": self.bids,
            "a": self.asks,
            "approx_bytes": self.approx_bytes,
            "symbol": self.symbol,
            "epoch_id": self.epoch_id,
            "kind": self.kind,
            "bid_level_count": self.bid_level_count,
            "ask_level_count": self.ask_level_count,
            "book_content_hash": self.book_content_hash,
        }

    @staticmethod
    def from_consistent(
        snap: ConsistentBookSnapshot,
        *,
        receive_time_ns: int | None = None,
        symbol: str | None = None,
        epoch_id: int = 0,
        kind: str = CHECKPOINT_KIND_PERIODIC,
    ) -> BookCheckpoint:
        bids = [[float(p), float(q)] for p, q in sorted(snap.bids.items(), reverse=True)]
        asks = [[float(p), float(q)] for p, q in sorted(snap.asks.items())]
        approx = 64 + 24 * (len(bids) + len(asks))
        sym = symbol if symbol is not None else str(getattr(snap, "symbol", "") or "")
        return BookCheckpoint(
            receive_time_ns=int(
                receive_time_ns
                if receive_time_ns is not None
                else (snap.receive_time_ns or time.time_ns())
            ),
            event_ts_ms=snap.event_ts_ms,
            cts_ms=snap.cts_ms,
            update_id=snap.update_id,
            seq=snap.seq,
            bids=bids,
            asks=asks,
            approx_bytes=approx,
            symbol=sym,
            epoch_id=int(epoch_id),
            kind=str(kind),
            bid_level_count=len(bids),
            ask_level_count=len(asks),
            book_content_hash=book_content_hash(bids, asks),
        )

    @staticmethod
    def from_dict(obj: dict[str, Any]) -> BookCheckpoint:
        bids = obj.get("b") or []
        asks = obj.get("a") or []
        return BookCheckpoint(
            receive_time_ns=int(obj["receive_time_ns"]),
            event_ts_ms=obj.get("event_ts_ms"),
            cts_ms=obj.get("cts_ms"),
            update_id=obj.get("u"),
            seq=obj.get("seq"),
            bids=bids,
            asks=asks,
            approx_bytes=int(obj.get("approx_bytes") or 0),
            symbol=str(obj.get("symbol") or ""),
            epoch_id=int(obj.get("epoch_id") or 0),
            kind=str(obj.get("kind") or CHECKPOINT_KIND_PERIODIC),
            bid_level_count=int(obj.get("bid_level_count") or len(bids)),
            ask_level_count=int(obj.get("ask_level_count") or len(asks)),
            book_content_hash=str(
                obj.get("book_content_hash") or book_content_hash(bids, asks)
            ),
        )


class CheckpointRing:
    """Time-bounded checkpoint deque with retention > delta window (v2)."""

    def __init__(
        self,
        *,
        interval_sec: float,
        max_checkpoints: int,
        max_bytes: int,
        window_sec: float = 690.0,
    ) -> None:
        self.interval_sec = float(interval_sec)
        self.max_checkpoints = int(max_checkpoints)
        self.max_bytes = int(max_bytes)
        self.window_sec = float(window_sec)
        self._items: deque[BookCheckpoint] = deque()
        self._bytes = 0
        self._lock = threading.RLock()
        self._last_store_ns = 0
        self.skipped_oversized = 0
        self.epoch_id = 0
        self._has_initial_for_epoch = False
        self._need_resync = False

    def begin_resync_epoch(self, *, now_ns: int | None = None) -> int:
        """Advance epoch at reconnect; keep prior checkpoints for multi-epoch replay."""
        now = int(now_ns if now_ns is not None else time.time_ns())
        with self._lock:
            self.epoch_id += 1
            self._need_resync = True
            self._has_initial_for_epoch = False
            self._last_store_ns = 0
            self._evict_locked(now)
            return self.epoch_id

    def maybe_store(
        self,
        snap: ConsistentBookSnapshot,
        *,
        now_ns: int | None = None,
        symbol: str | None = None,
        force_kind: str | None = None,
    ) -> bool:
        now = int(now_ns if now_ns is not None else time.time_ns())
        with self._lock:
            if not snap.book_ready or snap.update_id is None:
                return False

            kind = force_kind
            if kind is None:
                if self._need_resync:
                    kind = CHECKPOINT_KIND_RESYNC
                elif not self._has_initial_for_epoch:
                    kind = CHECKPOINT_KIND_INITIAL
                else:
                    kind = CHECKPOINT_KIND_PERIODIC
                    if self._last_store_ns and (now - self._last_store_ns) < int(
                        self.interval_sec * 1_000_000_000
                    ):
                        return False

            store_ns = int(snap.receive_time_ns or now)
            ck = BookCheckpoint.from_consistent(
                snap,
                receive_time_ns=store_ns,
                symbol=symbol,
                epoch_id=self.epoch_id,
                kind=kind,
            )
            if ck.approx_bytes > self.max_bytes:
                self.skipped_oversized += 1
                return False
            self._items.append(ck)
            self._bytes += ck.approx_bytes
            self._last_store_ns = now
            if kind in (CHECKPOINT_KIND_INITIAL, CHECKPOINT_KIND_RESYNC):
                self._has_initial_for_epoch = True
                self._need_resync = False
            elif kind == CHECKPOINT_KIND_PERIODIC:
                self._has_initial_for_epoch = True
            self._evict_locked(now)
            return True

    def _evict_locked(self, now_ns: int) -> None:
        cutoff = now_ns - int(self.window_sec * 1_000_000_000)
        while self._items and self._items[0].receive_time_ns < cutoff:
            old = self._items.popleft()
            self._bytes -= old.approx_bytes
        while len(self._items) > self.max_checkpoints or self._bytes > self.max_bytes:
            if not self._items:
                break
            old = self._items.popleft()
            self._bytes -= old.approx_bytes

    def latest_at_or_before(
        self, receive_time_ns: int, *, epoch_id: int | None = None
    ) -> BookCheckpoint | None:
        with self._lock:
            chosen: BookCheckpoint | None = None
            for ck in self._items:
                if ck.receive_time_ns > receive_time_ns:
                    break
                if epoch_id is not None and ck.epoch_id != epoch_id:
                    continue
                chosen = ck
            return chosen

    def items_snapshot(self) -> list[BookCheckpoint]:
        with self._lock:
            return list(self._items)

    def snapshot_meta(self) -> dict[str, Any]:
        with self._lock:
            return {
                "checkpoint_count": len(self._items),
                "checkpoint_bytes": self._bytes,
                "skipped_oversized": self.skipped_oversized,
                "oldest_receive_time_ns": self._items[0].receive_time_ns if self._items else None,
                "newest_receive_time_ns": self._items[-1].receive_time_ns if self._items else None,
                "epoch_id": self.epoch_id,
                "need_resync": self._need_resync,
                "window_sec": self.window_sec,
            }

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._bytes = 0
            self._last_store_ns = 0
            self._has_initial_for_epoch = False
            self._need_resync = False


def checkpoint_payload_bytes(ck: BookCheckpoint) -> bytes:
    return orjson.dumps(ck.to_dict())
