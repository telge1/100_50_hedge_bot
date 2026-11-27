"""Read-only bounded public-trade fanout for research consumers (no second Bybit WS).

Attaches after WsPublicTrade normalization, before/alongside CH insert buffer.
Observer only enqueues; no analysis / DB / disk in the collector hot path.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from signal_generator.bybit.live.ws_public_trade import WsPublicTrade

logger = logging.getLogger(__name__)

DEFAULT_QUEUE_SIZE = 8192
DEFAULT_BATCH_LIMIT = 256
MAX_BATCH_LIMIT = 1024
SUBSCRIBER_TTL_SEC = 120.0
SOURCE_NAME = "public_trade_event_fanout_v1"
DEDUPE_KEY = "symbol+trade_id"  # restart-safe stable Bybit i field


def _utc_iso(dt: datetime | None = None) -> str:
    d = dt or datetime.now(timezone.utc)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _dec(v: Any) -> str:
    if isinstance(v, Decimal):
        return format(v, "f")
    return str(v)


@dataclass
class FanoutMetrics:
    enqueue_ns_samples: deque[int] = field(default_factory=lambda: deque(maxlen=4096))
    enqueued_total: int = 0
    delivered_total: int = 0
    dropped_total: int = 0
    overflow_total: int = 0

    def note_enqueue(self, elapsed_ns: int) -> None:
        self.enqueue_ns_samples.append(int(elapsed_ns))
        self.enqueued_total += 1

    def percentile_ns(self, p: float) -> float | None:
        if not self.enqueue_ns_samples:
            return None
        arr = sorted(self.enqueue_ns_samples)
        idx = min(len(arr) - 1, max(0, int(round((p / 100.0) * (len(arr) - 1)))))
        return float(arr[idx])


@dataclass
class TradeSubscriber:
    subscriber_id: str
    symbols: frozenset[str] | None  # None = all
    max_queue: int
    created_at: str
    queue: deque[dict[str, Any]]
    lock: threading.Lock = field(default_factory=threading.Lock)
    high_water_mark: int = 0
    enqueued_count: int = 0
    delivered_count: int = 0
    dropped_count: int = 0
    overflow_count: int = 0
    coverage_valid: bool = True
    overflow: bool = False
    last_poll_at: str | None = None
    last_heartbeat_at: str | None = None
    last_exchange_time: str | None = None
    last_receive_time_ns: int | None = None
    cursor: int = 0
    record_ordinal: int = 0
    connection_generation: int = 0
    closed: bool = False
    seen_ids: set[str] = field(default_factory=set)  # in-process dedupe window

    def metrics_dict(self) -> dict[str, Any]:
        with self.lock:
            return {
                "subscriber_id": self.subscriber_id,
                "symbols": sorted(self.symbols) if self.symbols is not None else None,
                "queue_size": len(self.queue),
                "max_queue": self.max_queue,
                "high_water_mark": self.high_water_mark,
                "enqueued_count": self.enqueued_count,
                "delivered_count": self.delivered_count,
                "dropped_count": self.dropped_count,
                "overflow_count": self.overflow_count,
                "coverage_valid": self.coverage_valid,
                "overflow": self.overflow,
                "created_at": self.created_at,
                "last_poll_at": self.last_poll_at,
                "last_heartbeat_at": self.last_heartbeat_at,
                "last_exchange_time": self.last_exchange_time,
                "last_receive_time_ns": self.last_receive_time_ns,
                "cursor": self.cursor,
                "connection_generation": self.connection_generation,
                "closed": self.closed,
            }


class PublicTradeEventFanout:
    """Per-subscriber bounded queues fed from the existing publicTrade WS path."""

    def __init__(
        self,
        *,
        default_queue_size: int = DEFAULT_QUEUE_SIZE,
        subscriber_ttl_sec: float = SUBSCRIBER_TTL_SEC,
    ) -> None:
        self.default_queue_size = int(default_queue_size)
        self.subscriber_ttl_sec = float(subscriber_ttl_sec)
        self._subs: dict[str, TradeSubscriber] = {}
        self._lock = threading.RLock()
        self.metrics = FanoutMetrics()
        self._connection_generation = 0
        self._second_ws = False  # invariant: never open a second WS here

    @property
    def second_bybit_trade_ws(self) -> bool:
        return False

    def note_reconnect(self) -> int:
        with self._lock:
            self._connection_generation += 1
            gen = self._connection_generation
            for sub in self._subs.values():
                with sub.lock:
                    sub.connection_generation = gen
                    # restart-safe identity is trade_id; clear ephemeral seen window
                    sub.seen_ids.clear()
            return gen

    def create_subscriber(
        self,
        *,
        symbol: str | None = None,
        symbols: list[str] | None = None,
        max_queue: int | None = None,
        subscriber_id: str | None = None,
    ) -> dict[str, Any]:
        sym_set: frozenset[str] | None
        if symbols:
            sym_set = frozenset(s.upper().strip() for s in symbols if s)
        elif symbol:
            sym_set = frozenset({symbol.upper().strip()})
        else:
            sym_set = None
        sid = subscriber_id or f"pts-{uuid.uuid4().hex[:12]}"
        qsize = int(max_queue or self.default_queue_size)
        if qsize < 1:
            return {"ok": False, "error": "invalid_max_queue"}
        now = _utc_iso()
        sub = TradeSubscriber(
            subscriber_id=sid,
            symbols=sym_set,
            max_queue=qsize,
            created_at=now,
            queue=deque(),
            last_heartbeat_at=now,
            connection_generation=self._connection_generation,
        )
        with self._lock:
            if sid in self._subs:
                return {"ok": False, "error": "subscriber_exists", "subscriber_id": sid}
            self._subs[sid] = sub
        return {
            "ok": True,
            "subscriber_id": sid,
            "symbols": sorted(sym_set) if sym_set is not None else None,
            "max_queue": qsize,
            "cursor": 0,
            "coverage": sub.metrics_dict(),
            "source": SOURCE_NAME,
            "dedupe_key": DEDUPE_KEY,
            "second_bybit_trade_ws": False,
        }

    def remove_subscriber(self, subscriber_id: str) -> dict[str, Any]:
        sid = str(subscriber_id or "").strip()
        with self._lock:
            sub = self._subs.pop(sid, None)
            if sub is None:
                return {
                    "ok": True,
                    "removed": False,
                    "subscriber_id": sid,
                    "reason": "unknown_subscriber",
                }
            sub.closed = True
        return {"ok": True, "removed": True, "subscriber_id": sid}

    def heartbeat(self, subscriber_id: str) -> dict[str, Any]:
        sid = str(subscriber_id or "").strip()
        with self._lock:
            sub = self._subs.get(sid)
            if sub is None or sub.closed:
                return {"ok": False, "error": "unknown_subscriber"}
            sub.last_heartbeat_at = _utc_iso()
            return {"ok": True, "subscriber_id": sid, "coverage": sub.metrics_dict()}

    def timeout_cleanup(self, *, now: datetime | None = None) -> dict[str, Any]:
        now_dt = now or datetime.now(timezone.utc)
        removed: list[str] = []
        with self._lock:
            items = list(self._subs.items())
        for sid, sub in items:
            ref = sub.last_heartbeat_at or sub.created_at
            try:
                ts = datetime.fromisoformat(ref.replace("Z", "+00:00"))
            except ValueError:
                ts = now_dt
            if (now_dt - ts).total_seconds() > self.subscriber_ttl_sec:
                self.remove_subscriber(sid)
                removed.append(sid)
        return {"ok": True, "removed": removed, "count": len(removed)}

    def shutdown(self) -> None:
        with self._lock:
            ids = list(self._subs.keys())
        for sid in ids:
            self.remove_subscriber(sid)

    def status(self) -> dict[str, Any]:
        with self._lock:
            subs = [s.metrics_dict() for s in self._subs.values()]
        return {
            "ok": True,
            "source": SOURCE_NAME,
            "subscriber_count": len(subs),
            "subscribers": subs,
            "enqueued_total": self.metrics.enqueued_total,
            "delivered_total": self.metrics.delivered_total,
            "dropped_total": self.metrics.dropped_total,
            "overflow_total": self.metrics.overflow_total,
            "connection_generation": self._connection_generation,
            "second_bybit_trade_ws": False,
            "dedupe_key": DEDUPE_KEY,
            "default_queue_size": self.default_queue_size,
        }

    def poll_events(
        self,
        *,
        subscriber_id: str,
        cursor: int | None = None,
        limit: int = DEFAULT_BATCH_LIMIT,
    ) -> dict[str, Any]:
        sid = str(subscriber_id or "").strip()
        lim = int(limit)
        if lim < 1:
            return {"ok": False, "error": "invalid_limit"}
        if lim > MAX_BATCH_LIMIT:
            lim = MAX_BATCH_LIMIT
        with self._lock:
            sub = self._subs.get(sid)
            if sub is None or sub.closed:
                return {"ok": False, "error": "unknown_subscriber"}
        with sub.lock:
            if cursor is not None:
                want = int(cursor)
                if want < 0:
                    return {"ok": False, "error": "invalid_cursor"}
                if want > sub.cursor:
                    sub.coverage_valid = False
                    return {
                        "ok": False,
                        "error": "cursor_ahead",
                        "cursor": sub.cursor,
                        "requested": want,
                        "coverage_valid": False,
                    }
                if want < sub.cursor and want < (sub.cursor - len(sub.queue)):
                    sub.coverage_valid = False
                    return {
                        "ok": False,
                        "error": "cursor_expired",
                        "cursor": sub.cursor,
                        "requested": want,
                        "coverage_valid": False,
                    }
            batch: list[dict[str, Any]] = []
            while sub.queue and len(batch) < lim:
                batch.append(sub.queue.popleft())
            sub.delivered_count += len(batch)
            self.metrics.delivered_total += len(batch)
            if batch:
                last = batch[-1]
                sub.cursor = int(last.get("record_ordinal") or sub.cursor) + 1
                if last.get("exchange_event_time"):
                    sub.last_exchange_time = str(last["exchange_event_time"])
                if last.get("receive_time_ns") is not None:
                    sub.last_receive_time_ns = int(last["receive_time_ns"])
            sub.last_poll_at = _utc_iso()
            sub.last_heartbeat_at = sub.last_poll_at
            has_more = len(sub.queue) > 0
            coverage = {
                "coverage_valid": sub.coverage_valid and not sub.overflow,
                "overflow": sub.overflow,
                "queue_size": len(sub.queue),
                "high_water_mark": sub.high_water_mark,
                "dropped_count": sub.dropped_count,
                "overflow_count": sub.overflow_count,
                "cursor": sub.cursor,
                "last_exchange_time": sub.last_exchange_time,
                "last_receive_time": sub.last_receive_time_ns,
            }
        return {
            "ok": True,
            "subscriber_id": sid,
            "events": batch,
            "has_more": has_more,
            "cursor": sub.cursor,
            "coverage_valid": coverage["coverage_valid"],
            "queue_size": coverage["queue_size"],
            "high_water_mark": coverage["high_water_mark"],
            "dropped_count": coverage["dropped_count"],
            "overflow_count": coverage["overflow_count"],
            "last_exchange_time": coverage["last_exchange_time"],
            "last_receive_time": coverage["last_receive_time"],
            "coverage": coverage,
            "source": SOURCE_NAME,
        }

    def on_trade(
        self,
        trade: WsPublicTrade,
        *,
        receive_time_ns: int | None = None,
    ) -> None:
        """Hot-path safe: put_nowait equivalent; never blocks; never opens a WS."""
        t0 = time.perf_counter_ns()
        try:
            self._ingest(trade, receive_time_ns=receive_time_ns)
        except Exception:
            logger.exception("public_trade_fanout_ingest_failed symbol=%s", getattr(trade, "symbol", "?"))
        finally:
            self.metrics.note_enqueue(time.perf_counter_ns() - t0)

    def _ingest(self, trade: WsPublicTrade, *, receive_time_ns: int | None) -> None:
        recv_ns = int(receive_time_ns if receive_time_ns is not None else time.time_ns())
        sym = str(trade.symbol).upper()
        tid = str(trade.trade_id)
        dedupe_id = f"{sym}|{tid}"
        exch = trade.trade_ts
        if exch.tzinfo is None:
            exch = exch.replace(tzinfo=timezone.utc)
        event = {
            "trade_id": tid,
            "symbol": sym,
            "side": str(trade.side),
            "price": _dec(trade.price),
            "quantity": _dec(trade.size),
            "notional": _dec(trade.notional),
            "exchange_event_time": _utc_iso(exch),
            "receive_time_ns": recv_ns,
            "source": SOURCE_NAME,
            "tick_direction": str(trade.tick_direction or ""),
            "is_rpi_trade": int(trade.is_rpi_trade or 0),
            "is_block_trade": False,
            "connection_generation": int(self._connection_generation),
            "dedupe_key": dedupe_id,
        }
        with self._lock:
            subs = list(self._subs.values())
        if not subs:
            return
        for sub in subs:
            if sub.closed:
                continue
            if sub.symbols is not None and sym not in sub.symbols:
                continue
            self._enqueue_for_sub(sub, event, dedupe_id=dedupe_id)

    def _enqueue_for_sub(
        self, sub: TradeSubscriber, event: dict[str, Any], *, dedupe_id: str
    ) -> None:
        with sub.lock:
            if dedupe_id in sub.seen_ids:
                return
            # bounded in-process seen set
            if len(sub.seen_ids) > max(10_000, sub.max_queue * 4):
                sub.seen_ids.clear()
            sub.seen_ids.add(dedupe_id)
            sub.record_ordinal += 1
            item = dict(event)
            item["record_ordinal"] = sub.record_ordinal
            item["subscriber_id"] = sub.subscriber_id
            if len(sub.queue) >= sub.max_queue:
                sub.overflow = True
                sub.coverage_valid = False
                sub.dropped_count += 1
                sub.overflow_count += 1
                self.metrics.dropped_total += 1
                self.metrics.overflow_total += 1
                # fail-closed: do not enqueue newest; leave overflow marker event if room by dropping oldest
                if sub.queue:
                    sub.queue.popleft()
                    overflow_ev = {
                        **item,
                        "event_type": "overflow",
                        "overflow": True,
                        "valid_for_analysis": False,
                    }
                    sub.queue.append(overflow_ev)
                    sub.enqueued_count += 1
                    sub.high_water_mark = max(sub.high_water_mark, len(sub.queue))
                return
            item["event_type"] = "trade"
            item["overflow"] = False
            item["valid_for_analysis"] = True
            sub.queue.append(item)
            sub.enqueued_count += 1
            sub.high_water_mark = max(sub.high_water_mark, len(sub.queue))
            sub.last_exchange_time = item["exchange_event_time"]
            sub.last_receive_time_ns = int(item["receive_time_ns"])
