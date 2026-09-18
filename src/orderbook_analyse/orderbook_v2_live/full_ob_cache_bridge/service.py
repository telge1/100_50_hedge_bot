"""Full-OB read-only cache bridge service (anchor_retention_v2)."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from orderbook_analyse.orderbook_v2_live.full_ob_cache_bridge.checkpoints import CheckpointRing
from orderbook_analyse.orderbook_v2_live.full_ob_cache_bridge.config import CacheBridgeSettings
from orderbook_analyse.orderbook_v2_live.full_ob_cache_bridge.dump import write_freeze_dump
from orderbook_analyse.orderbook_v2_live.full_ob_cache_bridge.freeze import build_freeze_bundle
from orderbook_analyse.orderbook_v2_live.full_ob_cache_bridge.paths import UnsafeDumpPath
from orderbook_analyse.orderbook_v2_live.full_ob_cache_bridge.protocol import (
    CONTRACT_ID,
    OP_FREEZE_PRE_ROLL,
    OP_STATUS,
    PROTOCOL_VERSION,
    REPLAY_CAPABILITY_NONE,
    REPLAY_CAPABILITY_RAW_FULL_BOOK,
    STATUS_BRIDGE_DISABLED,
    STATUS_DATA_COMPLETE,
    STATUS_FREEZE_CONCURRENCY_LIMIT,
    STATUS_INTERNAL_ERROR,
    STATUS_INVALID_REQUEST,
    STATUS_PAYLOAD_LIMIT_EXCEEDED,
    STATUS_PAYLOAD_TOO_LARGE,
    STATUS_PROTOCOL_MISMATCH,
    STATUS_REQUEST_BUSY,
    STATUS_SYMBOL_NOT_ALLOWED,
)
from orderbook_analyse.orderbook_v2_live.full_ob_cache_bridge.socket_server import BridgeSocketServer

logger = logging.getLogger(__name__)


class FullObCacheBridge:
    """
    In-process bridge: longer-retained book checkpoints + RO freeze of FR ringbuffer.

    Default: constructed only when settings.enabled. Never opens sockets when disabled.
    """

    def __init__(
        self,
        settings: CacheBridgeSettings,
        *,
        collector_instance_id: str | None = None,
        collector_start_time: datetime | None = None,
        get_ring_snapshot: Callable[[str], list[Any]] | None = None,
        get_ring_meta: Callable[[str], dict[str, Any]] | None = None,
        get_book_snapshot: Callable[[str], Any] | None = None,
        get_runtime_meta: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self.settings = settings
        self.collector_instance_id = collector_instance_id or uuid.uuid4().hex
        self.collector_start_time = collector_start_time or datetime.now(timezone.utc)
        self._get_ring_snapshot = get_ring_snapshot
        self._get_ring_meta = get_ring_meta
        self._get_book_snapshot = get_book_snapshot
        self._get_runtime_meta = get_runtime_meta
        ck_window = float(
            getattr(settings, "checkpoint_retention_seconds", None)
            or settings.max_pre_roll_seconds
        )
        self._checkpoints: dict[str, CheckpointRing] = {
            sym: CheckpointRing(
                interval_sec=settings.checkpoint_interval_sec,
                max_checkpoints=settings.max_checkpoints_per_symbol,
                max_bytes=settings.max_checkpoint_bytes_per_symbol,
                window_sec=ck_window,
            )
            for sym in settings.symbols
        }
        self._freeze_lock = threading.Lock()
        self._last_request_mono = 0.0
        self._socket: BridgeSocketServer | None = None
        self._reconnect_marks: dict[str, int] = {s: 0 for s in settings.symbols}

    def mark_reconnect(self, symbol: str | None = None) -> None:
        now = time.time_ns()
        if symbol is None:
            for s in self._reconnect_marks:
                self._reconnect_marks[s] = now
                ring = self._checkpoints.get(s)
                if ring is not None:
                    ring.begin_resync_epoch(now_ns=now)
            return
        sym = symbol.upper()
        if sym in self._reconnect_marks:
            self._reconnect_marks[sym] = now
            ring = self._checkpoints.get(sym)
            if ring is not None:
                ring.begin_resync_epoch(now_ns=now)

    def on_book_update(self, symbol: str, consistent_snap: Any) -> None:
        if not self.settings.enabled:
            return
        sym = symbol.upper()
        ring = self._checkpoints.get(sym)
        if ring is None:
            return
        try:
            # Snapshot already copied by caller outside/inside book lock; store here only.
            ring.maybe_store(consistent_snap, symbol=sym)
        except Exception:
            logger.exception("bridge_checkpoint_store_failed %s", sym)

    async def start(self) -> None:
        if not self.settings.enabled:
            return
        self._socket = BridgeSocketServer(
            self.settings.socket_path,
            self.handle_request,
            max_request_bytes=self.settings.max_request_bytes,
            mode=self.settings.socket_mode,
            read_timeout_sec=min(5.0, self.settings.request_timeout_sec),
        )
        await self._socket.start()
        logger.info(
            "full_ob_cache_bridge_started contract=%s socket=%s dump_root=%s ck_retention=%s",
            CONTRACT_ID,
            self.settings.socket_path,
            self.settings.dump_root,
            getattr(self.settings, "checkpoint_retention_seconds", None),
        )

    async def stop(self) -> None:
        if self._socket is not None:
            await self._socket.stop()
            self._socket = None

    def handle_request(self, req: dict[str, Any]) -> dict[str, Any]:
        try:
            if not self.settings.enabled:
                return {
                    "ok": False,
                    "protocol_version": PROTOCOL_VERSION,
                    "status": STATUS_BRIDGE_DISABLED,
                    "bridge_enabled": False,
                    "error": "bridge_disabled",
                }
            pv = req.get("protocol_version")
            if pv is not None and pv != PROTOCOL_VERSION:
                return {
                    "ok": False,
                    "protocol_version": PROTOCOL_VERSION,
                    "status": STATUS_PROTOCOL_MISMATCH,
                    "error": "protocol_mismatch",
                    "request_id": req.get("request_id"),
                }
            op = str(req.get("operation") or "").strip().lower()
            if op == OP_STATUS:
                return self._status()
            if op == OP_FREEZE_PRE_ROLL:
                return self._freeze(req)
            return {
                "ok": False,
                "protocol_version": PROTOCOL_VERSION,
                "status": STATUS_INVALID_REQUEST,
                "error": "unknown_operation",
                "request_id": req.get("request_id"),
            }
        except Exception as exc:
            logger.exception("bridge_handle_request_failed")
            return {
                "ok": False,
                "protocol_version": PROTOCOL_VERSION,
                "status": STATUS_INTERNAL_ERROR,
                "error": type(exc).__name__,
                "request_id": req.get("request_id"),
            }

    def _status(self) -> dict[str, Any]:
        symbols_out: dict[str, Any] = {}
        for sym in sorted(self.settings.symbols):
            meta = self._get_ring_meta(sym) if self._get_ring_meta else {}
            ck = self._checkpoints[sym].snapshot_meta()
            rt = self._get_runtime_meta(sym) if self._get_runtime_meta else {}
            symbols_out[sym] = {
                "buffer_start_ns": meta.get("buffer_start_ns"),
                "buffer_end_ns": meta.get("buffer_end_ns"),
                "message_count": meta.get("message_count"),
                "overflow_count": meta.get("overflow_count"),
                "gap_count": rt.get("gap_count"),
                "last_event_time_ms": rt.get("event_ts_ms"),
                "last_receive_time_ns": rt.get("last_receive_time_ns"),
                "freshness_ms": rt.get("freshness_ms"),
                "anchor_status": ck,
                "reconnect_mark_ns": self._reconnect_marks.get(sym),
            }
        any_sym = next(iter(sorted(self.settings.symbols)))
        s0 = symbols_out[any_sym]
        warm = all(
            (symbols_out[s].get("message_count") or 0) > 0
            and (symbols_out[s].get("anchor_status") or {}).get("checkpoint_count", 0) > 0
            for s in self.settings.symbols
        )
        return {
            "ok": True,
            "protocol_version": PROTOCOL_VERSION,
            "contract_id": CONTRACT_ID,
            "collector_instance_id": self.collector_instance_id,
            "collector_start_time": self.collector_start_time.isoformat().replace("+00:00", "Z"),
            "symbols": sorted(self.settings.symbols),
            "buffer_start": s0.get("buffer_start_ns"),
            "buffer_end": s0.get("buffer_end_ns"),
            "retention_seconds": self.settings.max_pre_roll_seconds,
            "checkpoint_retention_seconds": getattr(
                self.settings, "checkpoint_retention_seconds", None
            ),
            "message_count": s0.get("message_count"),
            "gap_count": s0.get("gap_count"),
            "overflow_count": s0.get("overflow_count"),
            "last_event_time": s0.get("last_event_time_ms"),
            "last_receive_time": s0.get("last_receive_time_ns"),
            "freshness_ms": s0.get("freshness_ms"),
            "replay_capability": (
                REPLAY_CAPABILITY_RAW_FULL_BOOK if warm else REPLAY_CAPABILITY_NONE
            ),
            "anchor_status": {s: symbols_out[s]["anchor_status"] for s in symbols_out},
            "bridge_enabled": True,
            "per_symbol": symbols_out,
            "status": STATUS_DATA_COMPLETE if warm else "BUFFER_NOT_WARM",
        }

    def _freeze(self, req: dict[str, Any]) -> dict[str, Any]:
        request_id = str(req.get("request_id") or "").strip()
        symbol = str(req.get("symbol") or "").strip().upper()
        if not request_id or not symbol:
            return {
                "ok": False,
                "protocol_version": PROTOCOL_VERSION,
                "status": STATUS_INVALID_REQUEST,
                "error": "request_id_and_symbol_required",
            }
        if symbol not in self.settings.symbols:
            return {
                "ok": False,
                "protocol_version": PROTOCOL_VERSION,
                "status": STATUS_SYMBOL_NOT_ALLOWED,
                "request_id": request_id,
                "symbol": symbol,
                "error": "symbol_not_allowed",
            }
        try:
            requested = float(req.get("requested_pre_roll_seconds"))
        except (TypeError, ValueError):
            return {
                "ok": False,
                "protocol_version": PROTOCOL_VERSION,
                "status": STATUS_INVALID_REQUEST,
                "request_id": request_id,
                "error": "bad_pre_roll_seconds",
            }

        now_mono = time.monotonic()
        if now_mono - self._last_request_mono < self.settings.min_request_interval_sec:
            return {
                "ok": False,
                "protocol_version": PROTOCOL_VERSION,
                "status": STATUS_FREEZE_CONCURRENCY_LIMIT,
                "request_id": request_id,
                "error": "rate_limited",
            }
        if not self._freeze_lock.acquire(blocking=False):
            return {
                "ok": False,
                "protocol_version": PROTOCOL_VERSION,
                "status": STATUS_FREEZE_CONCURRENCY_LIMIT,
                "request_id": request_id,
                "error": "busy",
            }
        try:
            self._last_request_mono = now_mono
            return self._freeze_locked(request_id=request_id, symbol=symbol, requested=requested)
        finally:
            self._freeze_lock.release()

    def _freeze_locked(
        self, *, request_id: str, symbol: str, requested: float
    ) -> dict[str, Any]:
        t0_ns = time.time_ns()
        # Atomic cut: copy under source locks as provided by callbacks (quick).
        ring_items = self._get_ring_snapshot(symbol) if self._get_ring_snapshot else []
        ring_meta = self._get_ring_meta(symbol) if self._get_ring_meta else {}
        book_snap = self._get_book_snapshot(symbol) if self._get_book_snapshot else None
        rt_meta = self._get_runtime_meta(symbol) if self._get_runtime_meta else {}
        ck_snapshot = self._checkpoints[symbol].items_snapshot()

        t0_book = None
        if book_snap is not None:
            bids, asks = book_snap.full_levels()
            t0_book = {
                "u": book_snap.update_id,
                "seq": book_snap.seq,
                "event_ts_ms": book_snap.event_ts_ms,
                "cts_ms": book_snap.cts_ms,
                "receive_time_ns": book_snap.receive_time_ns,
                "b": bids,
                "a": asks,
                "book_ready": book_snap.book_ready,
            }
            # Opportunistic checkpoint at T0 for future warmness (outside book lock).
            self.on_book_update(symbol, book_snap)

        req = max(0.0, min(float(requested), float(self.settings.max_pre_roll_seconds)))
        analysis_start = t0_ns - int(req * 1_000_000_000)
        # Contract: latest checkpoint with checkpoint_ts <= requested_pre_roll_start_ts
        anchor = self._checkpoints[symbol].latest_at_or_before(analysis_start)
        # Refresh snapshot after opportunistic store
        ck_snapshot = self._checkpoints[symbol].items_snapshot()
        if anchor is None:
            # Re-select after opportunistic store
            anchor = self._checkpoints[symbol].latest_at_or_before(analysis_start)

        reconnect_ns = int(self._reconnect_marks.get(symbol, 0) or 0)
        reconnect_in_window = bool(
            reconnect_ns
            and ring_items
            and min(int(it.receive_time_ns) for it in ring_items)
            <= reconnect_ns
            <= t0_ns
        )

        bundle = build_freeze_bundle(
            symbol=symbol,
            t0_ns=t0_ns,
            requested_pre_roll_seconds=requested,
            min_pre_roll_seconds=self.settings.min_pre_roll_seconds,
            max_pre_roll_seconds=self.settings.max_pre_roll_seconds,
            ring_items=ring_items,
            overflow_count=int(ring_meta.get("overflow_count") or 0),
            reconnect_count=int(rt_meta.get("reconnect_count") or 0),
            reconnect_in_window=reconnect_in_window,
            gap_count_runtime=int(rt_meta.get("gap_count") or 0),
            last_receive_ns=rt_meta.get("last_receive_time_ns"),
            stale_after_ms=self.settings.stale_after_ms,
            anchor=anchor,
            t0_book_snap=t0_book,
            available_checkpoints=ck_snapshot,
            reconnect_mark_ns=reconnect_ns or None,
        )
        forecast_id_seed = f"{symbol}_{request_id}_{bundle.t0_ns}"
        try:
            dump = write_freeze_dump(
                dump_root=self.settings.dump_root,
                request_id=request_id,
                forecast_id_seed=forecast_id_seed,
                symbol=symbol,
                collector_instance_id=self.collector_instance_id,
                bundle=bundle,
                max_payload_bytes=self.settings.max_payload_bytes,
            )
        except UnsafeDumpPath as exc:
            status = (
                STATUS_PAYLOAD_LIMIT_EXCEEDED
                if str(exc) == "payload_too_large"
                else STATUS_INVALID_REQUEST
            )
            return {
                "ok": False,
                "protocol_version": PROTOCOL_VERSION,
                "request_id": request_id,
                "symbol": symbol,
                "status": status,
                "error": str(exc),
                "data_complete": False,
            }

        manifest = dump["manifest"]
        return {
            "ok": True,
            "protocol_version": PROTOCOL_VERSION,
            "contract_id": CONTRACT_ID,
            **{k: manifest[k] for k in manifest if k != "created_at_ns"},
            "dump_dir": dump["dump_dir"],
            "payload_bytes": dump["payload_bytes"],
        }

    def health_dict(self) -> dict[str, Any]:
        return {
            "full_ob_cache_bridge_enabled": self.settings.enabled,
            "full_ob_cache_bridge_contract": CONTRACT_ID,
            "full_ob_cache_bridge_socket": str(self.settings.socket_path),
            "full_ob_cache_bridge_symbols": sorted(self.settings.symbols),
            "full_ob_cache_bridge_checkpoint_retention_sec": getattr(
                self.settings, "checkpoint_retention_seconds", None
            ),
            "full_ob_cache_bridge_anchors": {
                s: self._checkpoints[s].snapshot_meta() for s in self._checkpoints
            },
        }
