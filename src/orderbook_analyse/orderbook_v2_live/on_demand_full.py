"""On-demand full-depth orderbook (RAM only) via REST snapshot + WS deltas."""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx

from orderbook_analyse.orderbook_v2_live.full_book_state import (
    DEFAULT_CLIP_PCT,
    FULL_DEPTH,
    MAX_UI_BARS_PER_SIDE,
    RPI_INCLUDED_IN_FULL_OB,
    FullBookState,
    aggregate_full_book,
    full_orderbook_topic,
    parse_full_orderbook_topic,
)
from orderbook_analyse.orderbook_v2_live.full_ob_sync import (
    AlignStatus,
    BufferState,
    DeltaOutcome,
    align_snapshot_to_buffer,
    extract_u_seq,
)
from orderbook_analyse.orderbook_v2_live.on_demand_lease import (
    LeaseKey,
    LeaseManager,
    PILOT_SYMBOLS,
)

logger = logging.getLogger(__name__)

BYBIT_REST_FULL_OB = "https://api.bybit.com/v5/market/full_orderbook"
MAX_FULL_TOPICS = 2
SOURCE_NAME = "orderbook_v3_live_full_on_demand"


@dataclass
class FullBookRuntime:
    symbol: str
    book: FullBookState
    subscribed: bool = False
    subscription_confirmed: bool = False
    subscription_state: str = "stopped"  # stopped|starting|syncing|live|error
    last_error: str = ""
    resync_needed: bool = False
    pending_deltas: list[dict[str, Any]] = field(default_factory=list)
    sync_buffer: BufferState = field(default_factory=BufferState)
    gap_count: int = 0
    reconnect_count: int = 0
    last_rest_snapshot: dict[str, Any] | None = None


def load_full_book_settings() -> dict[str, Any]:
    enabled = (os.environ.get("OB_V3_ON_DEMAND_ENABLE") or "false").lower() in {"1", "true", "yes"}
    full_enabled = (os.environ.get("OB_V3_FULL_BOOK_ENABLE") or "true").lower() in {"1", "true", "yes"}
    return {
        "enabled": enabled and full_enabled,
        "max_active_topics": int(os.environ.get("OB_V3_FULL_BOOK_MAX_ACTIVE") or str(MAX_FULL_TOPICS)),
        "heartbeat_sec": float(os.environ.get("OB_V3_ON_DEMAND_HEARTBEAT_SEC") or "15"),
        "lease_ttl_sec": float(os.environ.get("OB_V3_ON_DEMAND_LEASE_TTL_SEC") or "45"),
        "pilot_symbols": PILOT_SYMBOLS,
        "clip_pct": float(os.environ.get("OB_V3_FULL_BOOK_CLIP_PCT") or str(DEFAULT_CLIP_PCT)),
        "max_ui_bars": int(os.environ.get("OB_V3_FULL_BOOK_UI_BARS") or str(MAX_UI_BARS_PER_SIDE)),
        "rest_url": os.environ.get("OB_V3_FULL_BOOK_REST_URL") or BYBIT_REST_FULL_OB,
    }


class FullBookOnDemandManager:
    """Optional full-depth layer. RAM only; snapshots are UI-aggregated."""

    def __init__(
        self,
        *,
        market: str = "linear",
        send_chunk: Callable,
        confirmed_topics: list[str],
        settings: dict[str, Any] | None = None,
    ) -> None:
        cfg = settings or load_full_book_settings()
        self.enabled = bool(cfg["enabled"])
        self.market = market
        self._send_chunk = send_chunk
        self._confirmed_topics = confirmed_topics
        self.clip_pct = float(cfg["clip_pct"])
        self.max_ui_bars = int(cfg["max_ui_bars"])
        self.rest_url = str(cfg["rest_url"])
        self.leases = LeaseManager(
            heartbeat_sec=cfg["heartbeat_sec"],
            lease_ttl_sec=cfg["lease_ttl_sec"],
            max_active_topics=cfg["max_active_topics"],
            pilot_symbols=cfg["pilot_symbols"],
        )
        # Patch lease depth validation for FULL_DEPTH via wrapper methods.
        self.runtimes: dict[str, FullBookRuntime] = {}
        self._inflight: set[str] = set()
        self._grace_until: dict[str, datetime] = {}
        self._book_lock = threading.Lock()
        self._http = httpx.Client(timeout=12.0)
        self._global_observers: list[Callable[..., None]] = []
        self._pending_resync_notify: tuple[str, dict[str, Any], int] | None = None
        self.max_rest_align_attempts = int(os.environ.get("OB_V3_FULL_BOOK_ALIGN_ATTEMPTS") or "12")
        self.lock_hold_ns_last: int = 0
        self.lock_hold_ns_max: int = 0

    def _note_lock_hold(self, t0_ns: int) -> None:
        import time as _time

        held = _time.perf_counter_ns() - t0_ns
        self.lock_hold_ns_last = held
        if held > self.lock_hold_ns_max:
            self.lock_hold_ns_max = held

    def close(self) -> None:
        try:
            self._http.close()
        except Exception:
            pass

    def add_observer(self, callback: Callable[..., None]) -> None:
        """Register raw-message observer (flight recorder). Same book, no second WS."""
        self._global_observers.append(callback)

    def _notify_observers(
        self,
        *,
        symbol: str,
        payload: dict[str, Any],
        received_at: datetime,
        receive_time_ns: int,
        phase: str,
        outcome: str | None = None,
    ) -> None:
        for cb in list(self._global_observers):
            try:
                cb(
                    symbol=symbol,
                    payload=payload,
                    received_at=received_at,
                    receive_time_ns=receive_time_ns,
                    phase=phase,
                    outcome=outcome,
                    runtime=self.runtimes.get(symbol.upper()),
                )
            except Exception:
                logger.exception("full_book_observer_failed symbol=%s", symbol)

    def _get_runtime(self, symbol: str) -> FullBookRuntime:
        sym = symbol.upper()
        rt = self.runtimes.get(sym)
        if rt is None:
            rt = FullBookRuntime(symbol=sym, book=FullBookState(symbol=sym))
            self.runtimes[sym] = rt
        return rt

    def handle_message(self, payload: dict[str, Any], received_at: datetime) -> bool:
        if not self.enabled:
            return False
        topic = str(payload.get("topic") or "")
        sym = parse_full_orderbook_topic(topic)
        if sym is None:
            return False
        if self.leases.active_count(LeaseKey(sym, FULL_DEPTH)) <= 0 and sym not in self._grace_until:
            return True  # consume but ignore
        import time as _time

        receive_time_ns = _time.time_ns()
        rt = self._get_runtime(sym)
        data = payload.get("data") or {}
        msg_type = str(payload.get("type") or data.get("type") or "delta").lower()
        bids = data.get("b") or []
        asks = data.get("a") or []
        u, seq = extract_u_seq(payload)
        ts_ms = payload.get("ts") or data.get("ts")
        cts_ms = payload.get("cts") or data.get("cts")
        notify: tuple[str, str | None] | None = None
        t0 = _time.perf_counter_ns()
        with self._book_lock:
            # Pre-ready: buffer deltas only (Full-OB WS has no snapshot).
            if not rt.book.book_ready:
                if u is None or seq is None:
                    self._note_lock_hold(t0)
                    return True
                if msg_type == "snapshot":
                    self._note_lock_hold(t0)
                    return True
                status = rt.sync_buffer.push(u=u, seq=seq, payload=payload)
                rt.pending_deltas = [d.payload for d in rt.sync_buffer.items]
                rt.subscription_state = "syncing"
                notify = ("buffer", status)
            elif u is not None and u == 1 and rt.book.update_id not in (None, 1):
                rt.book.clear()
                rt.sync_buffer.clear()
                rt.pending_deltas.clear()
                rt.resync_needed = True
                rt.gap_count += 1
                rt.subscription_state = "starting"
                notify = ("u_reset", DeltaOutcome.U_RESET.value)
            else:
                outcome = rt.book.apply_delta(
                    bids=bids,
                    asks=asks,
                    u=u,
                    seq=seq,
                    ts_ms=ts_ms,
                    cts_ms=cts_ms,
                    receive_time_ns=receive_time_ns,
                    enforce_continuity=True,
                )
                if outcome in (DeltaOutcome.GAP, DeltaOutcome.U_RESET):
                    rt.book.clear()
                    rt.sync_buffer.clear()
                    rt.pending_deltas.clear()
                    rt.resync_needed = True
                    rt.gap_count += 1
                    rt.subscription_state = "starting"
                elif outcome is DeltaOutcome.APPLIED:
                    rt.subscription_state = "live"
                notify = ("live", outcome.value)
        self._note_lock_hold(t0)
        if notify is not None:
            self._notify_observers(
                symbol=sym,
                payload=payload,
                received_at=received_at,
                receive_time_ns=receive_time_ns,
                phase=notify[0],
                outcome=notify[1],
            )
        return True

    def _fetch_rest_snapshot(self, symbol: str) -> dict[str, Any]:
        resp = self._http.get(
            self.rest_url,
            params={"category": self.market, "symbol": symbol.upper()},
        )
        resp.raise_for_status()
        body = resp.json()
        if int(body.get("retCode") or 0) != 0:
            raise RuntimeError(f"full_ob_rest:{body.get('retMsg')}")
        return body.get("result") or {}

    def _apply_rest_snapshot(self, rt: FullBookRuntime) -> None:
        """Align REST snapshot to buffered deltas per Bybit Full-OB contract."""
        import time as _time

        rt.subscription_state = "syncing"
        last_err = "NO_VALID_INITIAL_SNAPSHOT"
        for _attempt in range(self.max_rest_align_attempts):
            result = self._fetch_rest_snapshot(rt.symbol)
            snap_u = result.get("u")
            snap_seq = result.get("seq")
            if snap_u is None or snap_seq is None:
                last_err = "snapshot_missing_u_or_seq"
                continue
            need_more = False
            with self._book_lock:
                align = align_snapshot_to_buffer(
                    snap_u=int(snap_u),
                    snap_seq=int(snap_seq),
                    buffer=list(rt.sync_buffer.items),
                )
                if align.status in (AlignStatus.NEED_NEWER_SNAPSHOT, AlignStatus.SEQ_U_MISMATCH):
                    last_err = align.reason
                elif align.status is AlignStatus.NEED_MORE_DELTAS:
                    last_err = align.reason
                    need_more = True
                else:
                    recv_ns = _time.time_ns()
                    rt.book.apply_snapshot(
                        bids=result.get("b") or [],
                        asks=result.get("a") or [],
                        u=int(snap_u),
                        seq=int(snap_seq),
                        ts_ms=result.get("ts") or result.get("cts"),
                        cts_ms=result.get("cts"),
                        receive_time_ns=recv_ns,
                        mark_ready=True,
                    )
                    rt.last_rest_snapshot = dict(result)
                    applied_ok = True
                    for delta in align.remaining:
                        data = delta.payload.get("data") or {}
                        out = rt.book.apply_delta(
                            bids=data.get("b") or [],
                            asks=data.get("a") or [],
                            u=delta.u,
                            seq=delta.seq,
                            ts_ms=delta.payload.get("ts") or data.get("ts"),
                            cts_ms=delta.payload.get("cts") or data.get("cts"),
                            enforce_continuity=True,
                        )
                        if out is DeltaOutcome.GAP:
                            applied_ok = False
                            rt.book.clear()
                            last_err = f"gap_while_applying_buffer:{delta.u}"
                            break
                        if out in (
                            DeltaOutcome.IGNORED_STALE_U,
                            DeltaOutcome.IGNORED_DUP_U,
                            DeltaOutcome.IGNORED_DECREASING_SEQ,
                        ):
                            continue
                        if out is not DeltaOutcome.APPLIED:
                            applied_ok = False
                            last_err = f"buffer_apply_{out.value}"
                            break
                    if applied_ok and rt.book.book_ready:
                        rt.sync_buffer.clear()
                        rt.pending_deltas.clear()
                        rt.resync_needed = False
                        rt.subscription_state = "live"
                        rt.last_error = ""
                        # Notify FR outside lock: full REST seed for RESYNC/INITIAL checkpoint.
                        # Snapshot dict already held on runtime; no JSON/zstd here.
                        snap_payload = {
                            "topic": full_orderbook_topic(rt.symbol),
                            "type": "snapshot",
                            "ts": result.get("ts"),
                            "cts": result.get("cts"),
                            "data": dict(rt.last_rest_snapshot or result),
                        }
                        self._pending_resync_notify = (
                            rt.symbol,
                            snap_payload,
                            recv_ns,
                        )
                        return
                    if not applied_ok:
                        rt.sync_buffer.clear()
            if need_more:
                _time.sleep(0.05)
                continue
        with self._book_lock:
            rt.book.clear()
            rt.book.book_ready = False
            rt.resync_needed = True
            rt.subscription_state = "error"
            rt.last_error = last_err

    def flush_pending_resync_notify(self) -> None:
        """Deliver resync_ready observer event after _apply_rest_snapshot (off book lock)."""
        pending = getattr(self, "_pending_resync_notify", None)
        self._pending_resync_notify = None
        if not pending:
            return
        symbol, payload, recv_ns = pending
        from datetime import datetime, timezone

        self._notify_observers(
            symbol=symbol,
            payload=payload,
            received_at=datetime.now(timezone.utc),
            receive_time_ns=int(recv_ns),
            phase="resync_ready",
            outcome="checkpoint",
        )

    def handle_request(self, req: dict[str, Any]) -> dict[str, Any]:
        op = str(req.get("operation") or "").strip().lower()
        request_id = req.get("request_id")
        symbol = str(req.get("symbol") or "").strip().upper()
        lease_id = str(req.get("lease_id") or "").strip()

        def base(**extra: Any) -> dict[str, Any]:
            out = {
                "request_id": request_id,
                "ok": True,
                "error": None,
                "symbol": symbol or None,
                "depth": FULL_DEPTH,
                "book_mode": "full",
                "subscription_state": "stopped",
                "expires_at": None,
            }
            out.update(extra)
            return out

        if not self.enabled:
            return base(ok=False, error="disabled", subscription_state="error")
        try:
            if op == "acquire":
                if not symbol or not lease_id:
                    raise ValueError("symbol_and_lease_required")
                lease, _ = self._acquire(symbol=symbol, lease_id=lease_id)
                rt = self._get_runtime(lease.symbol)
                if rt.subscription_state == "stopped":
                    rt.subscription_state = "starting"
                return base(
                    symbol=lease.symbol,
                    subscription_state=rt.subscription_state,
                    expires_at=_iso(lease.expires_at),
                )
            if op == "heartbeat":
                if not lease_id:
                    raise ValueError("lease_id_required")
                lease = self._heartbeat(lease_id, symbol=symbol or None)
                rt = self.runtimes.get(lease.symbol)
                state = rt.subscription_state if rt else "starting"
                return base(
                    symbol=lease.symbol,
                    subscription_state=state,
                    expires_at=_iso(lease.expires_at),
                )
            if op == "release":
                if not lease_id:
                    raise ValueError("lease_id_required")
                lease = self.leases._leases.get(lease_id)
                sym = lease.symbol if lease else (symbol or None)
                key, unsub = self._release(lease_id)
                if key is not None and unsub:
                    self._grace_until[key.symbol] = datetime.now(timezone.utc) + timedelta(
                        seconds=self.leases.lease_ttl_sec
                    )
                return base(
                    symbol=sym,
                    subscription_state="grace" if key is not None and unsub else "stopped",
                )
            if op == "status":
                rt = self.runtimes.get(symbol) if symbol else None
                state = rt.subscription_state if rt else "stopped"
                if symbol and self.leases.active_count(LeaseKey(symbol, FULL_DEPTH)) <= 0:
                    state = "stopped"
                return base(subscription_state=state)
            if op == "snapshot":
                if not symbol:
                    raise ValueError("symbol_required")
                if lease_id:
                    try:
                        self._heartbeat(lease_id, symbol=symbol)
                    except KeyError:
                        pass
                    except ValueError:
                        pass
                if self.leases.active_count(LeaseKey(symbol, FULL_DEPTH)) <= 0:
                    return base(ok=False, error="no_active_lease", subscription_state="stopped")
                return self._snapshot_response(req, symbol)
            return base(ok=False, error="unknown_operation", subscription_state="error")
        except RuntimeError as exc:
            if str(exc) == "capacity_reached":
                return base(ok=False, error="capacity_reached", subscription_state="capacity")
            return base(ok=False, error=str(exc), subscription_state="error")
        except KeyError:
            return base(ok=False, error="unknown_lease", subscription_state="stopped")
        except ValueError as exc:
            return base(ok=False, error=str(exc), subscription_state="error")
        except Exception as exc:
            logger.exception("full_book_request_failed")
            return base(ok=False, error=str(exc), subscription_state="error")

    def _acquire(self, *, symbol: str, lease_id: str):
        # Temporarily allow FULL_DEPTH in LeaseManager.
        return _acquire_full(self.leases, symbol=symbol, lease_id=lease_id)

    def _heartbeat(self, lease_id: str, *, symbol: str | None):
        return _heartbeat_full(self.leases, lease_id, symbol=symbol)

    def _release(self, lease_id: str):
        return self.leases.release(lease_id)

    def _snapshot_response(self, req: dict[str, Any], symbol: str) -> dict[str, Any]:
        import time as _time

        rt = self._get_runtime(symbol)
        t0 = _time.perf_counter_ns()
        with self._book_lock:
            snap = rt.book.copy_consistent_snapshot()
            sub_state = rt.subscription_state
        self._note_lock_hold(t0)
        # Aggregation / JSON happen outside the lock.
        include_full = bool(req.get("full_levels") or req.get("include_full_levels"))
        agg = aggregate_full_book(
            snap,
            max_bars_per_side=self.max_ui_bars,
            clip_pct=self.clip_pct,
        )
        ts = snap.last_event_at
        event_ms = snap.event_ts_ms
        now = datetime.now(timezone.utc)
        freshness_ms = None
        freshness_state = "unknown"
        if ts is not None:
            freshness_ms = max(0, int((now - ts).total_seconds() * 1000))
            if freshness_ms <= 15_000:
                freshness_state = "fresh"
            elif freshness_ms <= 180_000:
                freshness_state = "delayed"
            else:
                freshness_state = "stale"
        timestamp_utc = None
        if event_ms is not None:
            timestamp_utc = datetime.fromtimestamp(event_ms / 1000.0, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%f"
            )[:-3] + "Z"
        elif ts is not None:
            timestamp_utc = ts.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        out = {
            "request_id": req.get("request_id"),
            "ok": True,
            "error": None,
            "symbol": symbol,
            "depth": FULL_DEPTH,
            "book_mode": "full",
            "source": SOURCE_NAME,
            "coverage": "on_demand_full",
            "subscription_state": sub_state,
            "freshness_state": freshness_state,
            "freshness_ms": freshness_ms,
            "timestamp_utc": timestamp_utc,
            "data_status": "current" if agg.get("bids") or agg.get("asks") else "no_data",
            "tick_size": None,
            "update_id": snap.update_id,
            "seq": snap.seq,
            "cts_ms": snap.cts_ms,
            "receive_time_ns": snap.receive_time_ns,
            "book_ready": snap.book_ready,
            "levels_capped_at_1000": False,
            **agg,
        }
        if include_full:
            full_bids, full_asks = snap.full_levels()
            out["full_bids"] = full_bids
            out["full_asks"] = full_asks
            out["full_level_count"] = len(full_bids) + len(full_asks)
        return out

    async def subscribe_symbol(self, ws, symbol: str) -> None:
        sym = symbol.upper()
        topic = full_orderbook_topic(sym)
        rt = self._get_runtime(sym)
        if sym in self._inflight:
            return
        self._inflight.add(sym)
        try:
            rt.subscription_state = "syncing"
            rt.subscribed = True
            with self._book_lock:
                rt.book.clear()
                rt.sync_buffer.clear()
                rt.pending_deltas.clear()
            if topic not in self._confirmed_topics:
                await self._send_chunk(ws, "subscribe", [topic])
                self._confirmed_topics.append(topic)
            rt.subscription_confirmed = True
            # REST snapshot in thread to avoid blocking event loop.
            import asyncio

            await asyncio.to_thread(self._apply_rest_snapshot, rt)
            self.flush_pending_resync_notify()
            if rt.book.book_ready:
                rt.subscription_state = "live"
        except Exception as exc:
            rt.last_error = str(exc)
            rt.subscription_state = "error"
            logger.warning("full_book_subscribe_failed %s err=%s", sym, exc)
        finally:
            self._inflight.discard(sym)

    async def unsubscribe_symbol(self, ws, symbol: str) -> None:
        sym = symbol.upper()
        topic = full_orderbook_topic(sym)
        try:
            if topic in self._confirmed_topics:
                await self._send_chunk(ws, "unsubscribe", [topic])
                self._confirmed_topics.remove(topic)
        finally:
            with self._book_lock:
                rt = self.runtimes.pop(sym, None)
                if rt is not None:
                    rt.book.clear()

    def on_reconnect(self, *, reason: str = "transport_reconnect") -> None:
        now = datetime.now(timezone.utc)
        import time as _time

        recv_ns = _time.time_ns()
        for key, _ in self.leases.expire_due(now=now):
            self._grace_until.pop(key.symbol, None)
            self.runtimes.pop(key.symbol, None)
        for sym, rt in list(self.runtimes.items()):
            prev_u = rt.book.update_id
            prev_seq = rt.book.seq
            prev_ts = rt.book.event_ts_ms
            prev_recv = rt.book.last_receive_time_ns
            rt.subscription_confirmed = False
            rt.resync_needed = True
            rt.subscription_state = "starting"
            rt.reconnect_count += 1
            with self._book_lock:
                rt.book.clear()
                rt.sync_buffer.clear()
                rt.pending_deltas.clear()
            topic = full_orderbook_topic(sym)
            if topic in self._confirmed_topics:
                self._confirmed_topics.remove(topic)
            # Notify FR: end prior epoch / open RESYNC_BOUNDARY (no JSON under lock).
            self._notify_observers(
                symbol=sym,
                payload={
                    "topic": topic,
                    "type": "reconnect",
                    "reason": reason,
                    "prev_u": prev_u,
                    "prev_seq": prev_seq,
                    "prev_exchange_ts_ms": prev_ts,
                    "prev_receive_time_ns": prev_recv,
                    "disconnect_ts": now.isoformat().replace("+00:00", "Z"),
                    "reconnect_ts": now.isoformat().replace("+00:00", "Z"),
                    "reconnect_count": rt.reconnect_count,
                },
                received_at=now,
                receive_time_ns=recv_ns,
                phase="reconnect",
                outcome=reason,
            )

    async def tick(self, ws) -> None:
        if not self.enabled:
            return
        now = datetime.now(timezone.utc)
        expired = self.leases.expire_due(now=now)
        for key, unsub in expired:
            if unsub:
                self._grace_until[key.symbol] = now + timedelta(seconds=self.leases.lease_ttl_sec)
        for sym, until in list(self._grace_until.items()):
            if now >= until:
                del self._grace_until[sym]
                await self.unsubscribe_symbol(ws, sym)
        for key in self.leases.active_keys():
            if key.depth != FULL_DEPTH:
                continue
            rt = self._get_runtime(key.symbol)
            if rt.resync_needed or not rt.book.book_ready or not rt.subscription_confirmed:
                await self.subscribe_symbol(ws, key.symbol)
                self.flush_pending_resync_notify()

    def health_dict(self) -> dict[str, Any]:
        if not self.enabled:
            return {"full_book_enabled": False}
        return {
            "full_book_enabled": True,
            "full_book_active_topics": sum(
                1 for k in self.leases.active_keys() if k.depth == FULL_DEPTH
            ),
            "full_book_rpi_included": RPI_INCLUDED_IN_FULL_OB,
            "full_book_lock_hold_ns_last": self.lock_hold_ns_last,
            "full_book_lock_hold_ns_max": self.lock_hold_ns_max,
            "full_book_runtimes": [
                {
                    "symbol": rt.symbol,
                    "subscription_state": rt.subscription_state,
                    "raw_bids": len(rt.book.bids),
                    "raw_asks": len(rt.book.asks),
                    "snapshot_loaded": rt.book.snapshot_loaded,
                    "book_ready": rt.book.book_ready,
                    "update_id": rt.book.update_id,
                    "seq": rt.book.seq,
                    "cts_ms": rt.book.cts_ms,
                    "gap_count": rt.gap_count,
                    "reconnect_count": rt.reconnect_count,
                    "last_error": rt.last_error,
                }
                for rt in self.runtimes.values()
            ],
        }


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _acquire_full(leases: LeaseManager, *, symbol: str, lease_id: str):
    """Acquire with FULL_DEPTH without changing OB1000-only validation permanently."""
    sym = leases._validate_symbol(symbol)
    ts = leases._now(None)
    key = LeaseKey(sym, FULL_DEPTH)
    existing = leases._leases.get(lease_id)
    if existing is not None:
        if existing.symbol != sym or existing.depth != FULL_DEPTH:
            raise ValueError("lease_symbol_mismatch")
        existing.last_heartbeat = ts
        existing.expires_at = ts + timedelta(seconds=leases.lease_ttl_sec)
        return existing, False
    had_active = leases.active_count(key) > 0
    if not had_active and leases.active_topic_count() >= leases.max_active_topics:
        # Count only full-depth toward this manager's cap: active_topic_count includes all
        # depths in shared manager — we use a dedicated LeaseManager instance so OK.
        raise RuntimeError("capacity_reached")
    from orderbook_analyse.orderbook_v2_live.on_demand_lease import Lease

    lease = Lease(
        lease_id=lease_id,
        session_id=lease_id,
        symbol=sym,
        depth=FULL_DEPTH,
        created_at=ts,
        last_heartbeat=ts,
        expires_at=ts + timedelta(seconds=leases.lease_ttl_sec),
    )
    leases._leases[lease_id] = lease
    return lease, not had_active


def _heartbeat_full(leases: LeaseManager, lease_id: str, *, symbol: str | None):
    ts = leases._now(None)
    lease = leases._leases.get(lease_id)
    if lease is None:
        raise KeyError(lease_id)
    if lease.depth != FULL_DEPTH:
        raise ValueError("lease_depth_mismatch")
    if symbol is not None:
        sym = leases._validate_symbol(symbol)
        if lease.symbol != sym:
            raise ValueError("lease_symbol_mismatch")
    lease.last_heartbeat = ts
    lease.expires_at = ts + timedelta(seconds=leases.lease_ttl_sec)
    return lease
