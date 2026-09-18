"""Read-only Full-OB delta fanout for research consumers (no second Bybit WS).

Attaches to FullBookOnDemandManager.add_observer. Observer only enqueues;
no DB/disk/analysis in the keeper hot path.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_QUEUE_SIZE = 8192
DEFAULT_BATCH_LIMIT = 256
MAX_BATCH_LIMIT = 1024
SUBSCRIBER_TTL_SEC = 120.0
SOURCE_NAME = "full_ob_event_fanout_v1"


def _utc_iso(dt: datetime | None = None) -> str:
    d = dt or datetime.now(timezone.utc)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _ms_to_iso(ms: Any) -> str | None:
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        )[:-3] + "Z"
    except (TypeError, ValueError, OSError, OverflowError):
        return None


@dataclass
class FanoutMetrics:
    enqueue_ns_samples: deque[int] = field(default_factory=lambda: deque(maxlen=2048))
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
class Subscriber:
    subscriber_id: str
    symbol: str
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
    last_sequence_id: int | None = None
    last_update_id: int | None = None
    snapshot_generation: int = 0
    cursor: int = 0  # next record_ordinal expected / last delivered+1
    record_ordinal: int = 0
    gap_seen: bool = False
    closed: bool = False

    def metrics_dict(self) -> dict[str, Any]:
        with self.lock:
            return {
                "subscriber_id": self.subscriber_id,
                "symbol": self.symbol,
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
                "last_sequence_id": self.last_sequence_id,
                "last_update_id": self.last_update_id,
                "snapshot_generation": self.snapshot_generation,
                "cursor": self.cursor,
                "gap_seen": self.gap_seen,
                "closed": self.closed,
            }


class FullObEventFanout:
    """Per-symbol bounded queues fed by FullBookOnDemandManager observers."""

    def __init__(
        self,
        *,
        default_queue_size: int = DEFAULT_QUEUE_SIZE,
        subscriber_ttl_sec: float = SUBSCRIBER_TTL_SEC,
    ) -> None:
        self.default_queue_size = int(default_queue_size)
        self.subscriber_ttl_sec = float(subscriber_ttl_sec)
        self._subs: dict[str, Subscriber] = {}
        self._by_symbol: dict[str, set[str]] = {}
        self._lock = threading.RLock()
        self._attached = False
        self._manager: Any = None
        self.metrics = FanoutMetrics()
        self._generation: dict[str, int] = {}
        self._book_ready: dict[str, bool] = {}

    def attach(self, manager: Any) -> None:
        """Register as observer on FullBookOnDemandManager (idempotent)."""
        with self._lock:
            if self._attached and self._manager is manager:
                return
            self._manager = manager
            manager.add_observer(self.on_full_ob_message)
            self._attached = True

    def detach(self) -> None:
        with self._lock:
            # Observers list has no remove API — mark detached; callback no-ops.
            self._attached = False
            self._manager = None

    def shutdown(self) -> None:
        with self._lock:
            ids = list(self._subs.keys())
        for sid in ids:
            self.remove_subscriber(sid)
        self.detach()

    def create_subscriber(
        self,
        *,
        symbol: str,
        max_queue: int | None = None,
        subscriber_id: str | None = None,
    ) -> dict[str, Any]:
        sym = symbol.upper().strip()
        if not sym:
            return {"ok": False, "error": "symbol_required"}
        sid = subscriber_id or f"fos-{uuid.uuid4().hex[:12]}"
        qsize = int(max_queue or self.default_queue_size)
        if qsize < 1:
            return {"ok": False, "error": "invalid_max_queue"}
        now = _utc_iso()
        gen = self._generation.get(sym, 0)
        sub = Subscriber(
            subscriber_id=sid,
            symbol=sym,
            max_queue=qsize,
            created_at=now,
            queue=deque(),
            last_heartbeat_at=now,
            snapshot_generation=gen,
        )
        with self._lock:
            if sid in self._subs:
                return {"ok": False, "error": "subscriber_exists", "subscriber_id": sid}
            self._subs[sid] = sub
            self._by_symbol.setdefault(sym, set()).add(sid)
        return {
            "ok": True,
            "subscriber_id": sid,
            "symbol": sym,
            "max_queue": qsize,
            "snapshot_generation": gen,
            "book_ready": bool(self._book_ready.get(sym)),
            "cursor": 0,
            "coverage": sub.metrics_dict(),
            "source": SOURCE_NAME,
        }

    def remove_subscriber(self, subscriber_id: str) -> dict[str, Any]:
        """Idempotent remove."""
        sid = str(subscriber_id or "").strip()
        with self._lock:
            sub = self._subs.pop(sid, None)
            if sub is None:
                return {"ok": True, "removed": False, "subscriber_id": sid, "reason": "unknown_subscriber"}
            sub.closed = True
            bucket = self._by_symbol.get(sub.symbol)
            if bucket is not None:
                bucket.discard(sid)
                if not bucket:
                    self._by_symbol.pop(sub.symbol, None)
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
            age = (now_dt - ts).total_seconds()
            if age > self.subscriber_ttl_sec:
                self.remove_subscriber(sid)
                removed.append(sid)
        return {"ok": True, "removed": removed, "count": len(removed)}

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
                # Fail-closed if client skips ahead past what we have
                if want > sub.cursor:
                    return {
                        "ok": False,
                        "error": "cursor_ahead",
                        "cursor": sub.cursor,
                        "requested": want,
                    }
                # Rewind not supported for dropped history
                if want < sub.cursor and want < (sub.cursor - len(sub.queue)):
                    return {
                        "ok": False,
                        "error": "cursor_expired",
                        "cursor": sub.cursor,
                        "requested": want,
                    }
            batch: list[dict[str, Any]] = []
            while sub.queue and len(batch) < lim:
                batch.append(sub.queue.popleft())
            sub.delivered_count += len(batch)
            self.metrics.delivered_total += len(batch)
            if batch:
                last = batch[-1]
                sub.cursor = int(last.get("record_ordinal") or sub.cursor) + 1
                if last.get("sequence_id") is not None:
                    sub.last_sequence_id = int(last["sequence_id"])
                if last.get("update_id") is not None:
                    sub.last_update_id = int(last["update_id"])
                if last.get("snapshot_generation") is not None:
                    sub.snapshot_generation = int(last["snapshot_generation"])
            sub.last_poll_at = _utc_iso()
            sub.last_heartbeat_at = sub.last_poll_at
            has_more = len(sub.queue) > 0
            coverage = {
                "coverage_valid": sub.coverage_valid,
                "overflow": sub.overflow,
                "queue_size": len(sub.queue),
                "high_water_mark": sub.high_water_mark,
                "enqueued_count": sub.enqueued_count,
                "delivered_count": sub.delivered_count,
                "dropped_count": sub.dropped_count,
                "overflow_count": sub.overflow_count,
                "gap_seen": sub.gap_seen,
                "snapshot_generation": sub.snapshot_generation,
                "cursor": sub.cursor,
                "last_sequence_id": sub.last_sequence_id,
                "last_update_id": sub.last_update_id,
            }
        return {
            "ok": True,
            "subscriber_id": sid,
            "symbol": sub.symbol,
            "events": batch,
            "has_more": has_more,
            "coverage": coverage,
            "source": SOURCE_NAME,
        }

    def on_full_ob_message(
        self,
        *,
        symbol: str,
        payload: dict[str, Any],
        received_at: datetime,
        receive_time_ns: int,
        phase: str,
        outcome: str | None = None,
        runtime: Any = None,
        **_extra: Any,
    ) -> None:
        # Always ingest when called directly (unit tests / inject).
        # After detach(), manager reference is cleared; skip only if explicitly detached
        # while still registered (shutdown path sets _attached False).
        if self._manager is not None and not self._attached:
            return
        t0 = time.perf_counter_ns()
        try:
            self._ingest(
                symbol=symbol,
                payload=payload,
                received_at=received_at,
                receive_time_ns=receive_time_ns,
                phase=phase,
                outcome=outcome,
                runtime=runtime,
            )
        except Exception:
            logger.exception("full_ob_event_fanout_ingest_failed symbol=%s", symbol)
        finally:
            self.metrics.note_enqueue(time.perf_counter_ns() - t0)

    def _ingest(
        self,
        *,
        symbol: str,
        payload: dict[str, Any],
        received_at: datetime,
        receive_time_ns: int,
        phase: str,
        outcome: str | None,
        runtime: Any,
    ) -> None:
        sym = symbol.upper()
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        msg_type = str(payload.get("type") or data.get("type") or "delta").lower()
        u = data.get("u")
        seq = data.get("seq")
        if u is None:
            u = payload.get("u")
        if seq is None:
            seq = payload.get("seq")
        try:
            update_id = int(u) if u is not None else None
        except (TypeError, ValueError):
            update_id = None
        try:
            sequence_id = int(seq) if seq is not None else None
        except (TypeError, ValueError):
            sequence_id = None
        ts = payload.get("ts") if payload.get("ts") is not None else data.get("ts")
        cts = payload.get("cts") if payload.get("cts") is not None else data.get("cts")
        exchange_event_time = _ms_to_iso(cts if cts is not None else ts)

        book_ready = bool(getattr(getattr(runtime, "book", None), "book_ready", False))
        # Snapshot generation tracking
        gen = self._generation.get(sym, 0)
        if phase == "resync_ready" or (phase == "live" and outcome == "applied" and book_ready and gen == 0):
            # first ready: ensure generation >= 1
            if gen == 0:
                gen = 1
                self._generation[sym] = gen
        if phase in {"u_reset"} or (phase == "live" and outcome in {"gap", "u_reset"}):
            gen = self._generation.get(sym, 0) + 1
            self._generation[sym] = gen
            self._book_ready[sym] = False
        if phase == "resync_ready":
            gen = self._generation.get(sym, 0)
            if gen < 1:
                gen = 1
            else:
                # successful re-align after gap keeps bumped generation or bumps once
                pass
            self._generation[sym] = max(gen, self._generation.get(sym, 0))
            gen = self._generation[sym]
            self._book_ready[sym] = True
        if phase == "live" and outcome == "applied":
            self._book_ready[sym] = True
            if self._generation.get(sym, 0) < 1:
                self._generation[sym] = 1
                gen = 1
            else:
                gen = self._generation[sym]

        is_gap = phase == "live" and outcome in {"gap", "u_reset"} or phase == "u_reset"
        valid_for_analysis = (
            (phase == "live" and outcome == "applied" and book_ready)
            or phase == "resync_ready"
        )
        # Pre-snapshot buffer events are delivered but marked invalid for analysis
        if phase == "buffer":
            valid_for_analysis = False

        with self._lock:
            sub_ids = list(self._by_symbol.get(sym, ()))

        if not sub_ids:
            return

        base_event = {
            "symbol": sym,
            "event_type": msg_type if phase != "resync_ready" else "snapshot",
            "phase": phase,
            "outcome": outcome,
            "exchange_event_time": exchange_event_time,
            "receive_time_ns": int(receive_time_ns),
            "received_at": _utc_iso(received_at) if isinstance(received_at, datetime) else str(received_at),
            "sequence_id": sequence_id,
            "update_id": update_id,
            "u": update_id,
            "seq": sequence_id,
            "ts": int(ts) if ts is not None else None,
            "cts": int(cts) if cts is not None else None,
            "snapshot_generation": int(self._generation.get(sym, gen)),
            "source": SOURCE_NAME,
            "book_ready": bool(self._book_ready.get(sym)),
            "valid_for_analysis": valid_for_analysis,
            "gap": bool(is_gap),
            "resnapshot": phase == "resync_ready",
            "b": data.get("b") if isinstance(data, dict) else None,
            "a": data.get("a") if isinstance(data, dict) else None,
        }

        level_events = self._expand_levels(base_event, data if isinstance(data, dict) else {})
        if not level_events:
            level_events = [
                {
                    **base_event,
                    "side": None,
                    "price": None,
                    "old_qty": None,
                    "new_qty": None,
                    "delta_qty": None,
                }
            ]

        for sid in sub_ids:
            with self._lock:
                sub = self._subs.get(sid)
            if sub is None or sub.closed:
                continue
            self._enqueue_for_sub(sub, level_events, is_gap=is_gap)

    def _expand_levels(self, base: dict[str, Any], data: dict[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for side, key in (("bid", "b"), ("ask", "a")):
            rows = data.get(key) or []
            if not isinstance(rows, list):
                continue
            for row in rows:
                price = None
                new_qty = None
                if isinstance(row, (list, tuple)) and len(row) >= 2:
                    try:
                        price = float(row[0])
                        new_qty = float(row[1])
                    except (TypeError, ValueError):
                        continue
                elif isinstance(row, dict):
                    try:
                        price = float(row.get("price"))
                        new_qty = float(row.get("size") if row.get("size") is not None else row.get("qty"))
                    except (TypeError, ValueError):
                        continue
                else:
                    continue
                out.append(
                    {
                        **base,
                        "side": side,
                        "price": price,
                        "old_qty": None,  # post-mutation observer; pre-qty not available without pre-hook
                        "new_qty": new_qty,
                        "delta_qty": None,
                    }
                )
        return out

    def _enqueue_for_sub(
        self,
        sub: Subscriber,
        events: list[dict[str, Any]],
        *,
        is_gap: bool,
    ) -> None:
        with sub.lock:
            if is_gap:
                sub.gap_seen = True
                sub.coverage_valid = False
            for ev in events:
                sub.record_ordinal += 1
                item = dict(ev)
                item["record_ordinal"] = sub.record_ordinal
                item["subscriber_id"] = sub.subscriber_id
                if len(sub.queue) >= sub.max_queue:
                    # drop oldest to make room? Spec: overflow fail-closed, count drop
                    # Do NOT silently drop without flag — drop newest attempt after marking overflow
                    sub.overflow = True
                    sub.coverage_valid = False
                    sub.overflow_count += 1
                    sub.dropped_count += 1
                    self.metrics.overflow_total += 1
                    self.metrics.dropped_total += 1
                    # still try to keep a gap marker visible: replace last slot with overflow marker
                    overflow_marker = {
                        **item,
                        "event_type": "overflow",
                        "valid_for_analysis": False,
                        "gap": True,
                        "overflow": True,
                        "side": None,
                        "price": None,
                        "old_qty": None,
                        "new_qty": None,
                        "delta_qty": None,
                    }
                    if sub.queue:
                        sub.queue.popleft()
                        sub.dropped_count += 1
                        self.metrics.dropped_total += 1
                    sub.queue.append(overflow_marker)
                    sub.enqueued_count += 1
                    continue
                sub.queue.append(item)
                sub.enqueued_count += 1
                if len(sub.queue) > sub.high_water_mark:
                    sub.high_water_mark = len(sub.queue)
                if item.get("sequence_id") is not None:
                    sub.last_sequence_id = int(item["sequence_id"])
                if item.get("update_id") is not None:
                    sub.last_update_id = int(item["update_id"])
                if item.get("snapshot_generation") is not None:
                    sub.snapshot_generation = int(item["snapshot_generation"])

    def bump_generation(self, symbol: str) -> int:
        sym = symbol.upper()
        with self._lock:
            self._generation[sym] = self._generation.get(sym, 0) + 1
            self._book_ready[sym] = False
            return self._generation[sym]

    def note_snapshot_ready(self, symbol: str) -> int:
        sym = symbol.upper()
        with self._lock:
            if self._generation.get(sym, 0) < 1:
                self._generation[sym] = 1
            self._book_ready[sym] = True
            return self._generation[sym]

    def handle_request(self, req: dict[str, Any]) -> dict[str, Any] | None:
        """Return response dict for fanout ops, or None if not a fanout operation."""
        op = str(req.get("operation") or "").strip().lower()
        request_id = req.get("request_id")
        fanout_ops = {
            "create_subscriber",
            "poll_events",
            "subscriber_heartbeat",
            "remove_subscriber",
            "fanout_cleanup",
            "fanout_status",
        }
        if op not in fanout_ops:
            return None

        def wrap(body: dict[str, Any]) -> dict[str, Any]:
            out = {"request_id": request_id, "operation": op, **body}
            return out

        try:
            if op == "create_subscriber":
                return wrap(
                    self.create_subscriber(
                        symbol=str(req.get("symbol") or ""),
                        max_queue=req.get("max_queue"),
                        subscriber_id=req.get("subscriber_id"),
                    )
                )
            if op == "poll_events":
                return wrap(
                    self.poll_events(
                        subscriber_id=str(req.get("subscriber_id") or ""),
                        cursor=req.get("cursor"),
                        limit=int(req.get("limit") or DEFAULT_BATCH_LIMIT),
                    )
                )
            if op == "subscriber_heartbeat":
                return wrap(self.heartbeat(str(req.get("subscriber_id") or "")))
            if op == "remove_subscriber":
                return wrap(self.remove_subscriber(str(req.get("subscriber_id") or "")))
            if op == "fanout_cleanup":
                return wrap(self.timeout_cleanup())
            if op == "fanout_status":
                with self._lock:
                    subs = [s.metrics_dict() for s in self._subs.values()]
                return wrap(
                    {
                        "ok": True,
                        "attached": self._attached,
                        "subscribers": subs,
                        "metrics": {
                            "enqueued_total": self.metrics.enqueued_total,
                            "delivered_total": self.metrics.delivered_total,
                            "dropped_total": self.metrics.dropped_total,
                            "overflow_total": self.metrics.overflow_total,
                            "enqueue_ns_p50": self.metrics.percentile_ns(50),
                            "enqueue_ns_p95": self.metrics.percentile_ns(95),
                            "enqueue_ns_p99": self.metrics.percentile_ns(99),
                        },
                        "source": SOURCE_NAME,
                    }
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("fanout_request_failed")
            return wrap({"ok": False, "error": str(exc)})
        return wrap({"ok": False, "error": "unknown_operation"})
