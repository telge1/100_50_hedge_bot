from __future__ import annotations

import json
import logging
import math
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from uuid import uuid4

from core.websocket_client import BybitWebSocketClient
from .math_utils import calculate_pnl

from .audit_logger import AuditLogger
from .base import HedgeStrategy, StrategyContext
from .cycle_submit_identity import cycle_submit_identity
from .fixed_cycle_strategy import ShortFixedCycleHedgeStrategy
from .models import (
    FillEvent,
    HedgeSnapshot,
    ManagedOrder,
    RuntimeState,
    StrategyIntent,
    snapshot_from_mapping,
    trace_dicts,
    utcnow,
)
from .exchange_errors import ExchangeUnavailableError, compact_exchange_error
from .order_manager import BybitOrderManager, OrderPayload
from .position_manager import PositionManager
from .trailing_fallback import TrailingFallbackManager
from . import purpose_mapping


@dataclass
class GenericRuntimeConfig:
    api_key: str
    secret_key: str
    symbol: str = "BTCUSDT"
    category: str = "linear"
    min_order_value: float = 7.0
    price_poll_interval_seconds: float = 1.0
    reconcile_interval_seconds: float = 8.0
    log_file: str = "logs/generic_hedge_runtime.log"
    audit_log_file: str = "logs/generic_hedge_runtime_audit.jsonl"
    strategy_state_file: str | None = None
    health_file: str | None = None
    ensure_exchange_ready: bool = True
    bot_name: str = "long_bot_1"
    calc_audit_log_file: str | None = None
    confirmed_pnl_history_file: str | None = None


class GenericHedgeRuntime:
    EXPECTED_EXIT_CANCEL_TIMEOUT_SECONDS = 10.0
    PENDING_FINAL_EXIT_SUBMISSIONS_KEY = "pending_final_exit_submissions"
    PENDING_FINAL_EXIT_MAX_AGE_MS = 60_000
    _REPLACE_CANCEL_REASONS = frozenset(
        {
            "trigger_diff",
            "qty_diff",
            "position_idx_mismatch",
            "trigger_direction_mismatch",
            "trigger_by_mismatch",
            "close_on_trigger_mismatch",
            "order_filter_mismatch",
            "final_exit_signature_mismatch",
        }
    )

    def __init__(
        self,
        config: GenericRuntimeConfig,
        strategy: HedgeStrategy,
        *,
        logger: logging.Logger | None = None,
        order_manager: BybitOrderManager | None = None,
        websocket_client: BybitWebSocketClient | None = None,
    ) -> None:
        self.config = config
        self.strategy = strategy
        self.logger = logger or logging.getLogger(f"runtime.{strategy.name}")
        self.order_manager = order_manager or BybitOrderManager(config.api_key, config.secret_key)
        self.websocket_client = websocket_client
        self.runtime_state = RuntimeState()
        self.position_manager = PositionManager()
        self.audit = AuditLogger(
            self.logger,
            config.audit_log_file,
            extra_fields={"bot_name": config.bot_name},
            runtime_state=self.runtime_state,
        )
        self.context = StrategyContext(
            audit=self.audit,
            runtime_name=strategy.name,
            symbol=config.symbol,
            category=config.category,
            min_order_value=config.min_order_value,
            order_manager=self.order_manager,
            refresh_snapshot=self.refresh_snapshot,
            cancel_open_orders_by_purpose=self.cancel_open_orders_by_purpose,
        )
        self._stop_event = threading.Event()
        self._price_thread: threading.Thread | None = None
        self._reconcile_thread: threading.Thread | None = None
        self._ws_thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._max_leverage_ready_symbols: set[tuple[str, str]] = set()
        self._trailing_fallback = TrailingFallbackManager()
        self._bootstrap_in_progress = False

    def _should_log_idle_event(
        self,
        event_key: str,
        payload: dict[str, Any],
        interval_seconds: float = 60.0,
    ) -> bool:
        cache = getattr(self, "_idle_log_cache", None)
        if cache is None:
            cache = {}
            setattr(self, "_idle_log_cache", cache)
        try:
            signature = json.dumps(payload, sort_keys=True, default=str)
        except Exception:
            signature = repr(payload)
        now = time.time()
        last = cache.get(event_key)
        if (
            last is None
            or last.get("signature") != signature
            or (now - float(last.get("timestamp", 0.0))) >= interval_seconds
        ):
            cache[event_key] = {"signature": signature, "timestamp": now}
            return True
        return False

    def _should_log_repeated_event(
        self,
        event_key: str,
        signature_payload: dict[str, Any],
        interval_seconds: float = 60.0,
    ) -> bool:
        cache = getattr(self, "_idle_log_cache", None)
        if cache is None:
            cache = {}
            setattr(self, "_idle_log_cache", cache)
        try:
            signature = json.dumps(signature_payload, sort_keys=True, default=str)
        except Exception:
            signature = repr(signature_payload)
        now = time.time()
        last = cache.get(event_key)
        if (
            last is None
            or last.get("signature") != signature
            or (now - float(last.get("timestamp", 0.0))) >= interval_seconds
        ):
            cache[event_key] = {"signature": signature, "timestamp": now}
            return True
        return False

    def _bot_group_dir(self) -> Path:
        """
        Resolve the bot group directory based on the configured bot name / strategy side.

        IMPORTANT:
        - Use an absolute path rooted at the project root (two levels above this file)
          so that blacklist/state files are written consistently regardless of the
          current working directory of the runner process.
        - This must match the BOT_GROUP_DIR used in shell scripts like
          live_bots/*_hedge_bot/shared_scripts/*.sh so that
          blacklisted_symbols.json is shared across runtime and helper scripts.
        """
        project_root = Path(__file__).resolve().parents[1]

        # Prefer explicit bot_name if available (e.g. "short_bot_1", "long_bot_1").
        bot_name = getattr(self.config, "bot_name", None)
        strategy_side = getattr(self.config, "strategy_side", None)

        # Short-hedge bots live under live_bots/short_hedge_bot
        if (isinstance(bot_name, str) and bot_name.startswith("short_bot_")) or strategy_side == "short":
            return project_root / "live_bots" / "short_hedge_bot"

        # Default: long-primary hedge bots
        return project_root / "live_bots" / "100_50_hedge_bot"

    def _blacklist_file_path(self) -> Path:
        path = self._bot_group_dir() / "state" / "blacklisted_symbols.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _load_blacklisted_symbols(self) -> dict[str, dict[str, Any]]:
        path = self._blacklist_file_path()
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8") or "{}")
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}
        return payload

    def _write_blacklisted_symbols(self, data: dict[str, dict[str, Any]]) -> None:
        path = self._blacklist_file_path()
        tmp_path = path.with_name(f".{path.name}.tmp.{int(time.time() * 1_000)}")
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)

    def _is_symbol_blacklisted(self, symbol: str) -> tuple[bool, dict[str, Any] | None]:
        normalized = str(symbol or "").upper()
        data = self._load_blacklisted_symbols()
        entry = data.get(normalized)
        return (entry is not None, entry)

    def _blacklist_symbol(self, symbol: str, reason: str, ret_code: int, ret_msg: str) -> None:
        normalized = symbol.upper()
        data = self._load_blacklisted_symbols()
        data[normalized] = {
            "reason": reason,
            "ret_code": ret_code,
            "ret_msg": ret_msg,
            "blocked_by": self.config.bot_name,
            "blocked_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write_blacklisted_symbols(data)
        blacklist_path = self._blacklist_file_path()
        payload = {
            "symbol": normalized,
            "reason": reason,
            "ret_code": ret_code,
            "ret_msg": ret_msg,
            "bot_name": self.config.bot_name,
            "strategy_side": getattr(self.config, "strategy_side", None),
            "blacklist_path": str(blacklist_path),
            "blacklist_path_abs": str(blacklist_path.resolve()),
            "cwd": os.getcwd(),
        }
        # Event when the symbol is added to the blacklist.
        self.audit.log_event("fixed_cycle_blacklisted_symbol_added", **payload)
        # Explicit event to debug blacklist file resolution (path + cwd).
        self.audit.log_event(
            "fixed_cycle_blacklist_file_resolved",
            **payload,
        )

    def _release_dynamic_symbol_reservation(self) -> None:
        bot_name = self.config.bot_name or "long_bot_1"
        bot_group_dir = self._bot_group_dir()
        state_dir = bot_group_dir / "state"
        state_file = state_dir / "active_bot_symbols.json"
        lock_file = state_dir / "active_bot_symbols.lock"
        script_path = bot_group_dir / "shared_scripts" / "active_bot_symbols.py"
        if not script_path.exists():
            self.logger.warning(
                "dynamic_symbol_release_script_missing",
                {"script": str(script_path)},
            )
            return
        cmd = [
            sys.executable,
            str(script_path),
            "--bot-name",
            bot_name,
            "--state-file",
            str(state_file),
            "--lock-file",
            str(lock_file),
            "release",
            "--bot-group-dir",
            str(bot_group_dir),
            "--source",
            "permission_reject",
        ]
        try:
            subprocess.run(cmd, check=False)
            payload = {
                "symbol": self.config.symbol,
                "bot_name": bot_name,
                "state_file": str(state_file),
                "lock_file": str(lock_file),
                "bot_group_dir": str(bot_group_dir),
                "script_path": str(script_path),
                "cwd": os.getcwd(),
            }
            self.audit.log_event("dynamic_symbol_reservation_released_after_reject", **payload)
        except Exception as exc:
            self.logger.warning(
                "dynamic_symbol_reservation_release_failed",
                {"error": str(exc), "cmd": cmd},
            )

    @staticmethod
    def _snapshot_idle_summary(snapshot: HedgeSnapshot) -> dict[str, Any]:
        active_orders = [
            {
                "purpose": getattr(order, "purpose", None),
                "status": getattr(order, "status", None),
            }
            for order in snapshot.active_orders
            if not GenericHedgeRuntime._is_terminal_order_status(getattr(order, "status", None))
        ]
        return {
            "symbol": snapshot.symbol,
            "long_qty": snapshot.long_qty,
            "short_qty": snapshot.short_qty,
            "long_avg": snapshot.long_avg,
            "short_avg": snapshot.short_avg,
            "active_order_count": len(active_orders),
            "active_orders": active_orders,
        }

    def _compact_order_for_audit(self, order: Any) -> dict[str, Any]:
        return {
            "purpose": getattr(order, "purpose", None),
            "status": getattr(order, "status", None),
            "side": getattr(order, "side", None),
            "filled_qty": getattr(order, "filled_qty", None),
            "remaining_qty": getattr(order, "remaining_qty", None),
            "qty": getattr(order, "qty", None),
        }

    def _compact_snapshot_for_audit(self, snapshot: HedgeSnapshot) -> dict[str, Any]:
        return {
            "symbol": snapshot.symbol,
            "source": snapshot.source,
            "long_qty": snapshot.long_qty,
            "short_qty": snapshot.short_qty,
            "long_avg": snapshot.long_avg,
            "short_avg": snapshot.short_avg,
            "active_orders": [
                self._compact_order_for_audit(order)
                for order in snapshot.active_orders
                if not self._is_terminal_order_status(getattr(order, "status", None))
            ],
        }

    @staticmethod
    def _verbose_audit_enabled() -> bool:
        return str(os.environ.get("FIXED_CYCLE_VERBOSE_AUDIT", "")).lower() in {
            "1",
            "true",
            "yes",
        }

    def bootstrap(self) -> HedgeSnapshot:
        self._bootstrap_in_progress = True
        try:
            self._load_strategy_state()
            if self.config.ensure_exchange_ready:
                self.order_manager.ensure_hedge_mode(self.config.symbol, self.config.category)
                self.order_manager.ensure_max_leverage(self.config.symbol, self.config.category)
            self._recover_active_orders_from_exchange()
            self._ensure_max_leverage_before_trading()
            snapshot = self.refresh_snapshot("startup")
            state = self.runtime_state.strategy_state
            allow_start = True
            startup_flat_confirmed = False
            startup_flat_confirmation_reason: str | None = None
            if (
                snapshot.long_qty <= 0.0
                and snapshot.short_qty <= 0.0
                and not GenericHedgeRuntime._has_nonterminal_snapshot_orders(snapshot)
            ):
                snapshot, startup_flat_confirmed, startup_flat_confirmation_reason = self._confirm_startup_flat_snapshot(
                    snapshot
                )
                if not startup_flat_confirmed:
                    allow_start = False
                    blocked_payload = {
                        "strategy": self.strategy.name,
                        "symbol": self.config.symbol,
                        "long_qty": snapshot.long_qty,
                        "short_qty": snapshot.short_qty,
                        "active_order_count": len(snapshot.active_orders or ()),
                        "reason": startup_flat_confirmation_reason or "unknown",
                        "strategy_state_file": self.config.strategy_state_file,
                    }
                    self.logger.warning(
                        "fixed_cycle_startup_flat_confirmation_failed_start_blocked %s",
                        blocked_payload,
                    )
                    self.audit.log_event(
                        "fixed_cycle_startup_flat_confirmation_failed_start_blocked",
                        **blocked_payload,
                    )
            if startup_flat_confirmed:
                self.audit.log_event(
                    "fixed_cycle_startup_flat_position_detected",
                    strategy=self.strategy.name,
                    symbol=self.config.symbol,
                    long_qty=snapshot.long_qty,
                    short_qty=snapshot.short_qty,
                    active_order_count=len(snapshot.active_orders or ()),
                    strategy_state_file=self.config.strategy_state_file,
                )
                startup_state_cleaned = self.strategy.prepare_for_clean_startup(
                    snapshot,
                    self.runtime_state,
                    self.context,
                )
                if startup_state_cleaned:
                    self._save_strategy_state()
                    self.audit.log_event(
                        "fixed_cycle_startup_zero_state_reset_persisted",
                        strategy=self.strategy.name,
                        symbol=self.config.symbol,
                        long_qty=snapshot.long_qty,
                        short_qty=snapshot.short_qty,
                        active_order_count=len(snapshot.active_orders or ()),
                        strategy_state_file=self.config.strategy_state_file,
                    )
                    self.logger.info(
                        "startup_state_cleaned_for_fresh_entry %s",
                        {
                            "symbol": self.config.symbol,
                            "strategy": self.strategy.name,
                            "snapshot_long_qty": snapshot.long_qty,
                            "snapshot_short_qty": snapshot.short_qty,
                        },
                    )
                conflict, conflict_details = self._startup_state_conflict()
                if conflict:
                    self.logger.warning(
                        "startup_state_indicates_existing_context %s",
                        conflict_details,
                    )
                    block_payload = {
                        "state_file": self.config.strategy_state_file or "<none>",
                        "active_orders": [order.client_order_id for order in self.runtime_state.active_orders.values()],
                        "conflict_details": conflict_details,
                        "snapshot_long_qty": snapshot.long_qty,
                        "snapshot_short_qty": snapshot.short_qty,
                    }
                    self.logger.warning(
                        "startup_fresh_entry_blocked_by_state %s",
                        block_payload,
                    )
                    allow_start = False
            self.audit.log_event(
                "runtime_bootstrap",
                strategy=self.strategy.name,
                symbol=self.config.symbol,
                category=self.config.category,
                snapshot=self._compact_snapshot_for_audit(snapshot),
            )
            if allow_start:
                self._dispatch(
                    "start",
                    self.strategy.on_start(snapshot, self.runtime_state, self.context),
                    snapshot,
                )
                self._save_strategy_state()
        finally:
            self._bootstrap_in_progress = False
        return snapshot

    def start(self) -> None:
        self._bootstrap_in_progress = True
        try:
            self._start_websocket()
            self.bootstrap()
        except Exception:
            self._bootstrap_in_progress = False
            raise
        self._start_price_loop()
        self._start_reconcile_loop()

    def stop(self) -> None:
        self._stop_event.set()
        if self.websocket_client:
            self.websocket_client.stop()
        for thread in (self._price_thread, self._reconcile_thread, self._ws_thread):
            if thread and thread.is_alive():
                thread.join(timeout=2)
        self._save_strategy_state()

    def process_tick(self) -> HedgeSnapshot:
        self._retry_pending_rest_fills()
        with self._lock:
            snapshot = self.refresh_snapshot("tick")
            self._clear_pending_final_exit_submissions_if_flat(snapshot, source="tick")
            if self._trailing_fallback.active:
                self._trailing_fallback.update(snapshot.current_price)
                if self._trailing_fallback.should_submit():
                    fallback_intent = StrategyIntent(
                        purpose="TRAILING_SHORT_REDUCE",
                        side="short",
                        position_idx=2,
                        qty=self._trailing_fallback.qty,
                        order_type="Market",
                        reduce_only=True,
                    )

                    self.audit.log_event(
                        "trailing_fallback_triggered",
                        strategy=self.strategy.name,
                        intent=fallback_intent,
                        trailing_lowest_price=self._trailing_fallback.state.lowest_price,
                        trailing_max_rebound=self._trailing_fallback.state.max_rebound_price,
                        trailing_dist=self._trailing_fallback.state.trailing_dist,
                        snapshot_price=snapshot.current_price,
                    )
                    submitted_client_id = self.submit_intent(fallback_intent, snapshot, source="trailing_fallback")
                    if submitted_client_id:
                        self._trailing_fallback.mark_submitted()
                        self._trailing_fallback.reset()
                        self.runtime_state.strategy_state.pop("trailing_active", None)
            self._dispatch("tick", self.strategy.on_tick(snapshot, self.runtime_state, self.context), snapshot)
            self._save_strategy_state()
            return snapshot

    def reconcile_once(self) -> HedgeSnapshot:
        self._retry_pending_rest_fills()
        with self._lock:
            self._reconcile_active_orders()
            snapshot = self.refresh_snapshot("reconcile")
            self._clear_pending_final_exit_submissions_if_flat(snapshot, source="reconcile")
            self._dispatch(
                "reconcile",
                self.strategy.on_reconcile(snapshot, self.runtime_state, self.context),
                snapshot,
            )
            self._save_strategy_state()
            return snapshot

    def refresh_snapshot(self, source: str) -> HedgeSnapshot:
        positions = self._fetch_exchange_position_mapping(source)
        current_price = self.order_manager.fetch_mark_price(self.config.symbol, self.config.category)
        if current_price is None:
            current_price = self.runtime_state.last_snapshot.current_price if self.runtime_state.last_snapshot else 0.0
        snapshot = snapshot_from_mapping(
            symbol=self.config.symbol,
            current_price=current_price,
            positions=positions,
            runtime_state=self.runtime_state,
            source=source,
        )
        self.runtime_state.last_snapshot = snapshot
        compact_snapshot = self._compact_snapshot_for_audit(snapshot)
        should_log_snapshot = False
        if source == "tick":
            should_log_snapshot = self._should_log_idle_event(
                "snapshot_refreshed:tick",
                compact_snapshot,
                interval_seconds=120.0,
            )
        elif source == "reconcile":
            should_log_snapshot = self._should_log_idle_event(
                "snapshot_refreshed:reconcile",
                compact_snapshot,
                interval_seconds=120.0,
            )
        elif source in {"fill", "fixed_cycle_post_fill_rest"}:
            should_log_snapshot = self._should_log_idle_event(
                f"snapshot_refreshed:{source}",
                compact_snapshot,
                interval_seconds=30.0,
            )
        else:
            should_log_snapshot = True
        if should_log_snapshot:
            self.audit.log_event(
                "snapshot_refreshed",
                strategy=self.strategy.name,
                source=source,
                symbol=self.config.symbol,
                snapshot=compact_snapshot,
            )
        return snapshot

    def _fetch_exchange_position_mapping(self, source: str) -> dict[str, float]:
        symbol = self.config.symbol
        category = self.config.category
        max_attempts = 5 if source == "startup" else 1
        attempt = 0
        long_qty = 0.0
        short_qty = 0.0
        long_avg = 0.0
        short_avg = 0.0
        rows: list[dict[str, Any]] = []
        while attempt < max_attempts:
            attempt += 1
            fetched = self.order_manager.fetch_positions(symbol, category) or []
            rows = fetched
            rows_count = len(rows)
            rows_preview: list[dict[str, Any]] = []
            for row in rows[:5]:
                rows_preview.append(
                    {
                        "symbol": row.get("symbol"),
                        "side": row.get("side"),
                        "size": row.get("size") or row.get("positionQty"),
                        "qty": row.get("qty"),
                        "positionIdx": row.get("positionIdx"),
                    }
                )
            log_extra_base = {
                "reason": source,
                "source": source,
                "attempt": attempt,
                "category": category,
                "symbol": symbol,
            }
            fetch_started_payload = {
                **log_extra_base,
                "rows_count": rows_count,
                "rows_preview": rows_preview,
            }
            self.logger.debug(
                "bootstrap_positions_fetch_started %s",
                fetch_started_payload,
            )
            raw_payload = {
                **log_extra_base,
                "rows_count": rows_count,
                "rows_preview": rows_preview,
            }
            self.logger.debug(
                "bootstrap_positions_raw %s",
                raw_payload,
            )
            parsed_long_qty = 0.0
            parsed_short_qty = 0.0
            parsed_long_avg = 0.0
            parsed_short_avg = 0.0
            for position in rows:
                side = str(position.get("side") or position.get("positionSide") or "").lower()
                size = float(position.get("size") or position.get("positionQty") or 0.0)
                avg = float(position.get("avgPrice") or position.get("entryPrice") or 0.0)
                if side in {"buy", "long"}:
                    parsed_long_qty = size
                    parsed_long_avg = avg
                elif side in {"sell", "short"}:
                    parsed_short_qty = size
                    parsed_short_avg = avg
            long_qty = parsed_long_qty
            short_qty = parsed_short_qty
            long_avg = parsed_long_avg
            short_avg = parsed_short_avg
            parsed_payload = {
                **log_extra_base,
                "rows_count": rows_count,
                "rows_preview": rows_preview,
                "parsed_long_qty": long_qty,
                "parsed_short_qty": short_qty,
                "parsed_long_avg": long_avg,
                "parsed_short_avg": short_avg,
            }
            self.logger.debug(
                "bootstrap_positions_parsed %s",
                parsed_payload,
            )
            if long_qty > 0.0 or short_qty > 0.0:
                break
            if attempt < max_attempts:
                retry_payload = {
                    **log_extra_base,
                    "rows_count": rows_count,
                    "rows_preview": rows_preview,
                    "parsed_long_qty": long_qty,
                    "parsed_short_qty": short_qty,
                }
                self.logger.debug(
                    "bootstrap_positions_empty_retry %s",
                    retry_payload,
                )
                time.sleep(0.3)
        if long_qty <= 0.0 and short_qty <= 0.0:
            rows_count = len(rows)
            rows_preview = []
            for row in rows[:5]:
                rows_preview.append(
                    {
                        "symbol": row.get("symbol"),
                        "side": row.get("side"),
                        "size": row.get("size") or row.get("positionQty"),
                        "qty": row.get("qty"),
                        "positionIdx": row.get("positionIdx"),
                    }
                )
            final_payload = {
                "reason": source,
                "source": source,
                "attempt": attempt,
                "category": category,
                "symbol": symbol,
                "rows_count": rows_count,
                "rows_preview": rows_preview,
                "parsed_long_qty": long_qty,
                "parsed_short_qty": short_qty,
                "parsed_long_avg": long_avg,
                "parsed_short_avg": short_avg,
            }
            self.logger.debug(
                "bootstrap_positions_final_flat %s",
                final_payload,
            )
        self.position_manager.sync_positions(long_qty, long_avg, short_qty, short_avg)
        return {
            "long_qty": self.position_manager.long_size,
            "short_qty": self.position_manager.short_size,
            "long_avg": self.position_manager.long_avg,
            "short_avg": self.position_manager.short_avg,
        }

    def _confirm_startup_flat_snapshot(
        self,
        initial_snapshot: HedgeSnapshot,
    ) -> tuple[HedgeSnapshot, bool, str | None]:
        attempt_payload = {
            "reason": "startup",
            "symbol": self.config.symbol,
            "initial_long_qty": initial_snapshot.long_qty,
            "initial_short_qty": initial_snapshot.short_qty,
            "active_orders": [order.client_order_id for order in initial_snapshot.active_orders],
        }
        self.logger.info(
            "startup_flat_confirm_attempt_1 %s",
            attempt_payload,
        )
        time.sleep(1.0)
        confirm_snapshot = self.refresh_snapshot("startup_confirm")
        open_order_count = 0
        try:
            open_orders = self.order_manager.fetch_open_orders(self.config.symbol, self.config.category) or []
            open_order_count = len(open_orders)
        except Exception as exc:
            payload = {
                "symbol": self.config.symbol,
                "error": str(exc),
            }
            self.logger.warning(
                "fixed_cycle_startup_flat_confirmation_open_order_check_failed %s",
                payload,
            )
            self.audit.log_event("fixed_cycle_startup_flat_confirmation_open_order_check_failed", **payload)
            return confirm_snapshot, False, "open_order_check_failed"
        if open_order_count > 0:
            payload = {
                "symbol": self.config.symbol,
                "open_order_count": open_order_count,
                "open_orders": [
                    {
                        "orderId": str(order.get("orderId")),
                        "orderLinkId": str(order.get("orderLinkId")),
                    }
                    for order in open_orders
                ],
            }
            self.logger.info(
                "fixed_cycle_startup_flat_confirmation_rejected_open_orders_found %s",
                payload,
            )
            self.audit.log_event(
                "fixed_cycle_startup_flat_confirmation_rejected_open_orders_found",
                **payload,
            )
            return confirm_snapshot, False, "open_orders_found"
        confirm_payload = {
            "reason": "startup",
            "symbol": self.config.symbol,
            "long_qty": confirm_snapshot.long_qty,
            "short_qty": confirm_snapshot.short_qty,
            "active_orders": [order.client_order_id for order in confirm_snapshot.active_orders],
        }
        self.logger.info(
            "startup_flat_confirm_attempt_2 %s",
            confirm_payload,
        )
        confirmed = (
            confirm_snapshot.long_qty <= 0.0
            and confirm_snapshot.short_qty <= 0.0
            and not confirm_snapshot.active_orders
        )
        if confirmed:
            self.logger.info(
                "startup_flat_confirmed_allow_fresh_entry %s",
                confirm_payload,
            )
        else:
            confirm_payload["reason"] = "non_flat"
            self.logger.info(
                "startup_flat_not_confirmed_block_fresh_entry %s",
                confirm_payload,
            )
        return confirm_snapshot, confirmed, None if confirmed else "non_flat"

    def _startup_state_conflict(self) -> tuple[bool, dict[str, Any]]:
        state = self.runtime_state.strategy_state
        cycle_state = state.get("cycle_state") or {}
        conflict = bool(
            state.get("initial_entry_confirmed")
            or state.get("initial_entry_submitted")
            or int(state.get("cycle_completed_count") or 0) > 0
            or cycle_state.get("trade_active")
            or cycle_state.get("long_add_pending")
            or cycle_state.get("cycle_waiting_for_short_tp")
            or int(cycle_state.get("short_tp_pending_cycle") or 0) > 0
        )
        details = {
            "initial_entry_confirmed": state.get("initial_entry_confirmed"),
            "initial_entry_submitted": state.get("initial_entry_submitted"),
            "cycle_completed_count": state.get("cycle_completed_count"),
            "trade_active": cycle_state.get("trade_active"),
            "long_add_pending": cycle_state.get("long_add_pending"),
            "cycle_waiting_for_short_tp": cycle_state.get("cycle_waiting_for_short_tp"),
            "short_tp_pending_cycle": cycle_state.get("short_tp_pending_cycle"),
            "pending_cycle_loss_usdt": state.get("pending_cycle_loss_usdt"),
        }
        return conflict, details

    def handle_websocket_event(self, topic: str, payload: Any) -> None:
        if isinstance(payload, list):
            return
        if topic not in {"order", "position"}:
            return
        self.audit.log_event("ws_event", strategy=self.strategy.name, topic=topic, payload=payload)
        if topic == "position":
            self._sync_position_manager_from_ws(payload)
            return
        if topic != "order":
            return
        order_id = payload.get("orderId")
        if not order_id:
            return
        client_id = self.runtime_state.exchange_to_client_id.get(order_id)
        if not client_id:
            return
        managed_order = self.runtime_state.active_orders.get(client_id)
        if not managed_order:
            return
        previous_status = managed_order.status
        managed_order.status = self._normalize_order_status(payload.get("orderStatus"), managed_order.status)
        managed_order.updated_at = utcnow()
        if managed_order.status in {"PARTIAL", "FILLED"}:
            managed_order.filled_qty = float(payload.get("cumExecQty") or managed_order.filled_qty or 0.0)
            managed_order.remaining_qty = max(managed_order.qty - managed_order.filled_qty, 0.0)
        snapshot = self.runtime_state.last_snapshot or self.refresh_snapshot("ws_order")
        self._dispatch(
            "order_update",
            self.strategy.on_order_update(payload, snapshot, self.runtime_state, self.context),
            snapshot,
        )
        normalized_status = managed_order.status
        if normalized_status in {"CANCELED", "CANCELLED", "REJECTED"}:
            self.audit.log_event(
                "ws_order_terminal_diagnostics",
                strategy=self.strategy.name,
                symbol=self.config.symbol,
                category=self.config.category,
                client_order_id=client_id,
                exchange_order_id=order_id,
                purpose=managed_order.purpose,
                side=managed_order.side,
                managed_status_before=previous_status,
                normalized_status=normalized_status,
                raw_order_status=payload.get("orderStatus"),
                cancel_type=payload.get("cancelType"),
                reject_reason=payload.get("rejectReason"),
                cancel_reason=payload.get("cancelReason"),
                stop_order_type=payload.get("stopOrderType"),
                order_type=payload.get("orderType"),
                side_raw=payload.get("side"),
                position_idx=payload.get("positionIdx"),
                reduce_only=payload.get("reduceOnly"),
                close_on_trigger=payload.get("closeOnTrigger"),
                trigger_price=payload.get("triggerPrice"),
                trigger_by=payload.get("triggerBy"),
                trigger_direction=payload.get("triggerDirection"),
                qty=payload.get("qty"),
                cum_exec_qty=payload.get("cumExecQty"),
                leaves_qty=payload.get("leavesQty"),
                price=payload.get("price"),
                avg_price=payload.get("avgPrice"),
                created_time=payload.get("createdTime"),
                updated_time=payload.get("updatedTime"),
                full_payload=payload,
            )
        if normalized_status in {"CANCELED", "CANCELLED", "REJECTED"}:
            self._finalize_managed_order(client_id, managed_order)
        elif normalized_status == "FILLED":
            if float(managed_order.remaining_qty or 0.0) <= 1e-9:
                self._finalize_managed_order(client_id, managed_order)
            else:
                self.audit.log_event(
                    "order_terminal_but_remaining_qty_wait",
                    strategy=self.strategy.name,
                    client_order_id=client_id,
                    exchange_order_id=order_id,
                    purpose=managed_order.purpose,
                    status=managed_order.status,
                    filled_qty=managed_order.filled_qty,
                    remaining_qty=managed_order.remaining_qty,
                    raw_order_status=payload.get("orderStatus"),
                    cum_exec_qty=payload.get("cumExecQty"),
                    qty=payload.get("qty"),
                )
        self._save_strategy_state()

    def on_websocket_fill(
        self,
        exchange_order_id: str,
        qty: float,
        price: float,
        *,
        exec_id: str | None = None,
        cumulative_qty: float | None = None,
        order_link_id: str | None = None,
        order_side: str | None = None,
        order_status: str | None = None,
        **kwargs: Any,
    ) -> None:
        if kwargs:
            self.logger.debug(
                "websocket_fill_extra_kwargs %s",
                {
                    "exchange_order_id": exchange_order_id,
                    "order_link_id": order_link_id,
                    "exec_id": exec_id,
                    "extra_keys": sorted(kwargs.keys()),
                },
            )
        client_id = (
            self.runtime_state.exchange_to_client_id.get(exchange_order_id)
            or (order_link_id if order_link_id in self.runtime_state.active_orders else None)
        )
        if not client_id:
            try:
                client_id = self._recover_fixed_cycle_unmatched_fill(
                    exchange_order_id=exchange_order_id,
                    order_link_id=order_link_id,
                    qty=qty,
                    price=price,
                    exec_id=exec_id,
                )
            except Exception as exc:
                self.logger.exception(
                    "fixed_cycle_unmatched_fill_recovery_failed %s",
                    {
                        "exchange_order_id": exchange_order_id,
                        "order_link_id": order_link_id,
                        "qty": qty,
                        "price": price,
                        "exec_id": exec_id,
                        "order_side": order_side,
                        "order_status": order_status,
                        "error": str(exc),
                    },
                )
                return
            if not client_id:
                self.audit.log_event(
                    "unmatched_fill",
                    strategy=self.strategy.name,
                    exchange_order_id=exchange_order_id,
                    order_link_id=order_link_id,
                    qty=qty,
                    price=price,
                    exec_id=exec_id,
                    cumulative_qty=cumulative_qty,
                )
                return
        self._ingest_fill_event(
            exchange_order_id=exchange_order_id,
            client_id=client_id,
            qty=qty,
            price=price,
            exec_id=exec_id,
            cumulative_qty=cumulative_qty,
            source="websocket",
            order_link_id=order_link_id,
        )

    def _infer_fixed_cycle_unmatched_fill(self, order_link_id: str | None) -> dict[str, Any] | None:
        if not order_link_id:
            return None
        normalized = str(order_link_id).lower()
        prefix = "fixed_cycle-"
        if not normalized.startswith(prefix):
            return None
        candidate = normalized[len(prefix) :]
        cycle_match = re.match(r"cycle_(\d+)_(.+)", candidate)
        if cycle_match:
            cycle_index = int(cycle_match.group(1))
            role_segment = cycle_match.group(2)
            purpose = f"CYCLE_{cycle_index}_{role_segment.upper()}"
            cycle_role = "long_reduce" if "long" in role_segment else "short_reduce"
            metadata = {
                "cycle_index": cycle_index,
                "cycle_role": cycle_role,
            }
            return {
                "purpose": purpose,
                "side": "long" if "long" in role_segment else "short",
                "reduce_only": True,
                "metadata": metadata,
                "inferred_cycle_index": cycle_index,
                "inferred_cycle_role": cycle_role,
            }
        if candidate.startswith("short_sl_exit"):
            return {
                "purpose": self.strategy.SHORT_SL_EXIT_PURPOSE,
                "side": "short",
                "reduce_only": True,
                "metadata": {"exit_type": "short_sl", "exit_mode": "basket_exit"},
                "inferred_cycle_index": None,
                "inferred_cycle_role": "short_sl_exit",
            }
        if candidate.startswith("long_tp_exit"):
            return {
                "purpose": self.strategy.LONG_TP_EXIT_PURPOSE,
                "side": "long",
                "reduce_only": True,
                "metadata": {"exit_type": "long_tp", "exit_mode": "basket_exit"},
                "inferred_cycle_index": None,
                "inferred_cycle_role": "long_tp_exit",
            }
        return None

    def _startup_flat_reset_guard_active(self) -> bool:
        return bool(self.runtime_state.strategy_state.get("startup_flat_reset_applied"))

    def _is_stale_purpose_blocked_after_flat_restart(self, purpose: str | None) -> bool:
        normalized = str(purpose or "").upper()
        if not normalized:
            return False
        final_exit_purposes = {
            getattr(self.strategy, "LONG_TP_EXIT_PURPOSE", "LONG_TP_EXIT"),
            getattr(self.strategy, "LONG_SL_EXIT_PURPOSE", "LONG_SL_EXIT"),
            getattr(self.strategy, "SHORT_TP_EXIT_PURPOSE", "SHORT_TP_EXIT"),
            getattr(self.strategy, "SHORT_SL_EXIT_PURPOSE", "SHORT_SL_EXIT"),
            getattr(self.strategy, "SHORT_HARD_STOP_PURPOSE", "SHORT_HARD_STOP_EXIT"),
        }
        if normalized in final_exit_purposes:
            return True
        return normalized.startswith("CYCLE_")

    @staticmethod
    def _has_nonterminal_snapshot_orders(snapshot: HedgeSnapshot) -> bool:
        for order in snapshot.active_orders:
            if not GenericHedgeRuntime._is_terminal_order_status(getattr(order, "status", None)):
                return True
        return False

    @staticmethod
    def _has_nonterminal_runtime_orders(runtime_state: RuntimeState) -> bool:
        for order in runtime_state.active_orders.values():
            if not GenericHedgeRuntime._is_terminal_order_status(getattr(order, "status", None)):
                return True
        return False

    def _confirm_post_refill_structure_rebuild_progress(
        self,
        *,
        intent: StrategyIntent,
        snapshot: HedgeSnapshot,
        source: str,
    ) -> None:
        metadata = dict(intent.metadata or {})
        if not metadata.get("post_refill_structure_rebuild_required"):
            return
        expected_raw = metadata.get("post_refill_expected_purposes") or []
        expected_purposes = [str(purpose) for purpose in expected_raw if purpose]
        if not expected_purposes:
            return
        state = self.runtime_state.strategy_state
        confirmed = set(state.get("post_refill_structure_rebuild_confirmed_purposes") or [])
        if intent.purpose:
            confirmed.add(str(intent.purpose))
        state["post_refill_structure_rebuild_confirmed_purposes"] = sorted(confirmed)
        active_purposes = {
            str(order.purpose)
            for order in self.runtime_state.active_orders.values()
            if order.purpose and not self._is_terminal_order_status(getattr(order, "status", None))
        }
        active_purposes.update(
            str(getattr(order, "purpose", ""))
            for order in snapshot.active_orders
            if getattr(order, "purpose", None)
            and not self._is_terminal_order_status(getattr(order, "status", None))
        )
        satisfied = confirmed | active_purposes
        if all(purpose in satisfied for purpose in expected_purposes):
            state["post_refill_structure_rebuild_required"] = False
            state["post_refill_structure_rebuild_confirmed_purposes"] = []
            self.audit.log_event(
                "fixed_cycle_post_refill_structure_rebuild_completed",
                strategy=self.strategy.name,
                source=source,
                submitted_or_equivalent_purposes=sorted(confirmed),
                active_order_purposes=sorted(active_purposes),
                next_required_purpose=state.get("next_required_purpose"),
            )
        self._save_strategy_state()

    def _recover_fixed_cycle_unmatched_fill(
        self,
        exchange_order_id: str,
        order_link_id: str | None,
        qty: float,
        price: float,
        *,
        exec_id: str | None = None,
    ) -> str | None:
        if (
            (exchange_order_id and exchange_order_id in self.runtime_state.terminal_exchange_ids)
            or (order_link_id and order_link_id in self.runtime_state.terminal_client_ids)
            or (exec_id and exec_id in self.runtime_state.terminal_exec_ids)
        ):
            self.audit.log_event(
                "fixed_cycle_reconcile_terminal_order_ignored",
                strategy=self.strategy.name,
                exchange_order_id=exchange_order_id,
                order_link_id=order_link_id,
                exec_id=exec_id,
                qty=qty,
                price=price,
                reason="terminal_order_already_processed",
            )
            return None
        inference = self._infer_fixed_cycle_unmatched_fill(order_link_id)
        if not inference:
            return None
        if self._bootstrap_in_progress:
            last_snapshot = self.runtime_state.last_snapshot
            age_seconds = max(
                0.0,
                (utcnow() - self.runtime_state.started_at).total_seconds(),
            )
            payload = {
                "strategy": self.strategy.name,
                "symbol": self.config.symbol,
                "order_id": exchange_order_id,
                "order_link_id": order_link_id,
                "exec_id": exec_id,
                "qty": qty,
                "price": price,
                "reason": "bootstrap_in_progress",
                "bootstrap_in_progress": bool(self._bootstrap_in_progress),
                "runtime_age_seconds": round(age_seconds, 2),
                "last_snapshot_source": getattr(last_snapshot, "source", None),
                "last_snapshot_long_qty": getattr(last_snapshot, "long_qty", None),
                "last_snapshot_short_qty": getattr(last_snapshot, "short_qty", None),
                "during_initial_startup": True,
            }
            if self._should_log_repeated_event(
                f"blocked_unmatched_fill_bootstrap:{order_link_id or exchange_order_id}",
                {
                    "order_id": exchange_order_id,
                    "order_link_id": order_link_id,
                    "reason": "bootstrap_in_progress",
                    "last_snapshot_source": payload["last_snapshot_source"],
                    "last_snapshot_long_qty": payload["last_snapshot_long_qty"],
                    "last_snapshot_short_qty": payload["last_snapshot_short_qty"],
                },
                interval_seconds=120.0,
            ):
                self.audit.log_event(
                    "fixed_cycle_blocked_unmatched_fill_during_bootstrap",
                    **payload,
                )
            return None
        inferred_purpose = str(inference.get("purpose") or "")
        if (
            self._startup_flat_reset_guard_active()
            and self._is_stale_purpose_blocked_after_flat_restart(inferred_purpose)
        ):
            self.audit.log_event(
                "fixed_cycle_blocked_stale_unmatched_fill_after_flat_restart",
                strategy=self.strategy.name,
                symbol=self.config.symbol,
                order_link_id=order_link_id,
                inferred_purpose=inferred_purpose,
                order_id=exchange_order_id,
                qty=qty,
                price=price,
                reason="startup_flat_reset_applied",
            )
            return None
        client_order_id = order_link_id or exchange_order_id
        if not client_order_id:
            return None
        if client_order_id in self.runtime_state.active_orders:
            return client_order_id
        metadata = dict(inference.get("metadata") or {})
        if order_link_id:
            metadata.setdefault("order_link_id", order_link_id)
        metadata.setdefault("unmatched_fill", True)
        managed_order = ManagedOrder(
            client_order_id=client_order_id,
            side=inference["side"],
            qty=max(qty, 0.0),
            purpose=inference["purpose"],
            price=price,
            order_type="Market",
            reduce_only=inference["reduce_only"],
            exchange_order_id=exchange_order_id,
            status="OPEN",
            filled_qty=0.0,
            remaining_qty=max(qty, 0.0),
            metadata=metadata,
        )
        self.runtime_state.active_orders[client_order_id] = managed_order
        if exchange_order_id:
            self.runtime_state.exchange_to_client_id[exchange_order_id] = client_order_id
        self.audit.log_event(
            "fixed_cycle_unmatched_fill_recovered",
            strategy=self.strategy.name,
            order_id=exchange_order_id,
            exchange_order_id=exchange_order_id,
            order_link_id=order_link_id,
            inferred_purpose=inference["purpose"],
            inferred_side=inference["side"],
            inferred_cycle_index=inference.get("inferred_cycle_index"),
            inferred_cycle_role=inference.get("inferred_cycle_role"),
            exec_id=exec_id,
            exec_qty=qty,
            exec_price=price,
        )
        return client_order_id

    def _dispatch(self, source: str, intents: list[StrategyIntent], snapshot: HedgeSnapshot) -> None:
        strategy_state = self.runtime_state.strategy_state
        if not intents:
            compact_snapshot = self._compact_snapshot_for_audit(snapshot)
            noop_payload = {
                "source": source,
                "initial_entry_confirmed": bool(strategy_state.get("initial_entry_confirmed")),
                "current_effective_cycle": int(strategy_state.get("current_effective_cycle") or 0),
                "cycle_waiting_for_short_tp": bool(strategy_state.get("cycle_waiting_for_short_tp")),
                "pending_long_cycle_index": int(strategy_state.get("pending_long_cycle_index") or 0),
                "long_qty": snapshot.long_qty,
                "short_qty": snapshot.short_qty,
                "active_orders": compact_snapshot["active_orders"],
            }
            if self._should_log_idle_event("strategy_noop", noop_payload, interval_seconds=120.0):
                self.audit.log_event(
                    "strategy_noop",
                    strategy=self.strategy.name,
                    **noop_payload,
                )
            return
        entry_purposes = {self.strategy.LONG_ENTRY_PURPOSE, self.strategy.SHORT_ENTRY_PURPOSE}
        entry_intents = [intent for intent in intents if intent.purpose in entry_purposes]
        if entry_intents:
            unsettled_orders = self._runtime_unsettled_strategy_orders()
            if unsettled_orders:
                self.audit.log_event(
                    "strategy_initial_entry_blocked_unsettled_runtime_orders",
                    strategy=self.strategy.name,
                    source=source,
                    intent_count=len(entry_intents),
                    unsettled_orders=unsettled_orders,
                    snapshot=snapshot,
                )
                intents = [intent for intent in intents if intent.purpose not in entry_purposes]
                if not intents:
                    return
                entry_intents = []
            if entry_intents:
                strategy_state = self.runtime_state.strategy_state
                if (
                    strategy_state.get("post_exit_cleanup_required")
                    and not strategy_state.get("post_exit_cleanup_verified")
                ):
                    self.audit.log_event(
                        "strategy_initial_entry_blocked_post_exit_cleanup_pending",
                        strategy=self.strategy.name,
                        source=source,
                        intent_count=len(entry_intents),
                        cleanup_attempts=strategy_state.get("post_exit_cleanup_attempts", 0),
                        snapshot=snapshot,
                    )
                    intents = [intent for intent in intents if intent.purpose not in entry_purposes]
                    if not intents:
                        return
                    entry_intents = []
            if entry_intents:
                _, snapshot_unsettled_orders = self.strategy._collect_unsettled_strategy_orders(
                    snapshot, self.runtime_state
                )
                if snapshot_unsettled_orders:
                    strategy_state = self.runtime_state.strategy_state
                    ignore_snapshot = False
                    verified_at = strategy_state.get("post_exit_cleanup_verified_snapshot_updated_at")
                    snapshot_ts = getattr(snapshot, "updated_at", None)
                    if (
                        strategy_state.get("post_exit_cleanup_verified")
                        and not strategy_state.get("post_exit_cleanup_required")
                        and verified_at
                        and snapshot_ts
                    ):
                        try:
                            verified_dt = datetime.fromisoformat(verified_at)
                            if verified_dt.tzinfo is None:
                                verified_dt = verified_dt.replace(tzinfo=timezone.utc)
                            if snapshot_ts < verified_dt:
                                ignore_snapshot = True
                                self.audit.log_event(
                                    "strategy_initial_entry_snapshot_orders_ignored_after_verified_cleanup",
                                    strategy=self.strategy.name,
                                    source=source,
                                    snapshot_updated_at=snapshot_ts.isoformat(),
                                    verified_snapshot_updated_at=verified_dt.isoformat(),
                                )
                        except ValueError:
                            pass
                    if not ignore_snapshot:
                        self.audit.log_event(
                            "strategy_initial_entry_blocked_unsettled_snapshot_orders",
                            strategy=self.strategy.name,
                            source=source,
                            intent_count=len(entry_intents),
                            unsettled_snapshot_orders=snapshot_unsettled_orders,
                            snapshot=snapshot,
                        )
                        intents = [intent for intent in intents if intent.purpose not in entry_purposes]
                        if not intents:
                            return
                        entry_intents = []
            if entry_intents:
                self.audit.log_event(
                    "strategy_initial_entry_dispatched",
                    strategy=self.strategy.name,
                    intent_count=len(entry_intents),
                    snapshot=snapshot,
                    source=source,
                )
        self.logger.debug(
            "strategy_intents_handoff %s",
            {
                "source": source,
                "intent_count": len(intents),
                "purposes": [intent.purpose for intent in intents],
                "sides": [intent.side for intent in intents],
                "reduce_only_flags": [intent.reduce_only for intent in intents],
                "trigger_prices": [intent.trigger_price for intent in intents],
                "prices": [intent.price for intent in intents],
                "initial_entry_confirmed": bool(strategy_state.get("initial_entry_confirmed")),
                "current_long_cycle_index": int(strategy_state.get("current_long_cycle_index") or 0),
                "current_short_cycle_index": int(strategy_state.get("current_short_cycle_index") or 0),
                "current_effective_cycle": int(strategy_state.get("current_effective_cycle") or 0),
                "cycle_waiting_for_short_tp": bool(strategy_state.get("cycle_waiting_for_short_tp")),
                "pending_long_cycle_index": int(strategy_state.get("pending_long_cycle_index") or 0),
            },
        )
        for intent in intents:
            self.submit_intent(intent, snapshot, source)
            # Wenn während der Initial-Entry-Batch ein nicht-retrybarer Symbol-Permission-Fehler
            # erkannt wurde, weitere Initial-Entry-Intents derselben Batch nicht mehr submitten.
            state = self.runtime_state.strategy_state
            if state.get("initial_entry_retry_blocked") and (
                intent.purpose in {self.strategy.LONG_ENTRY_PURPOSE, self.strategy.SHORT_ENTRY_PURPOSE}
            ):
                self.audit.log_event(
                    "fixed_cycle_initial_entry_batch_loop_stopped_after_permission_reject",
                    strategy=self.strategy.name,
                    symbol=self.config.symbol,
                    bot_name=self.config.bot_name,
                    blocked_symbol=state.get("initial_entry_blocked_symbol"),
                    first_failed_purpose=intent.purpose,
                    source=source,
                )
                break

    def _dispatch_reconcile_terminal_cancel(
        self,
        client_id: str,
        managed_order: ManagedOrder,
        history_order: dict[str, Any],
        normalized_status: str,
    ) -> None:
        snapshot = self.runtime_state.last_snapshot or self.refresh_snapshot("reconcile_terminal")
        payload = {
            "orderStatus": normalized_status,
            "orderId": managed_order.exchange_order_id,
            "orderLinkId": client_id,
            "qty": managed_order.qty,
            "side": managed_order.side,
            "reduceOnly": managed_order.reduce_only,
            "purpose": managed_order.purpose,
            "rejectReason": history_order.get("rejectReason"),
        }
        intents = self.strategy.on_order_update(payload, snapshot, self.runtime_state, self.context)
        self._dispatch("order_reconcile", intents, snapshot)

    def _expected_exit_cancel_purposes(self) -> set[str]:
        return {
            getattr(self.strategy, "LONG_TP_EXIT_PURPOSE", "LONG_TP_EXIT"),
            getattr(self.strategy, "SHORT_SL_EXIT_PURPOSE", "SHORT_SL_EXIT"),
            getattr(self.strategy, "LONG_SL_EXIT_PURPOSE", "LONG_SL_EXIT"),
            getattr(self.strategy, "SHORT_TP_EXIT_PURPOSE", "SHORT_TP_EXIT"),
            getattr(self.strategy, "LONG_TP_EXIT_RECOVERY_PURPOSE", "LONG_TP_EXIT_RECOVERY"),
            getattr(self.strategy, "SHORT_SL_EXIT_RECOVERY_PURPOSE", "SHORT_SL_EXIT_RECOVERY"),
            "FINAL_LONG_EXIT",
            "FINAL_SHORT_EXIT",
        }

    def _register_expected_exit_cancel(
        self,
        client_id: str,
        order: ManagedOrder,
        replace_context: dict[str, Any] | None,
    ) -> None:
        if not replace_context:
            return
        if order.purpose not in self._expected_exit_cancel_purposes():
            return
        state = self.runtime_state.strategy_state
        registry = state.setdefault("expected_exit_cancels", [])
        now = time.monotonic()
        expires_in_ms = int(self.EXPECTED_EXIT_CANCEL_TIMEOUT_SECONDS * 1000)
        entry = {
            "client_order_id": client_id,
            "exchange_order_id": order.exchange_order_id,
            "purpose": order.purpose,
            "reason": replace_context.get("reason") or "replace_open_purpose",
            "replacement_purpose": replace_context.get("replacement_purpose") or order.purpose,
            "created_at_monotonic": now,
            "expires_at_monotonic": now + self.EXPECTED_EXIT_CANCEL_TIMEOUT_SECONDS,
            "consumed": False,
        }
        registry.append(entry)
        self.audit.log_event(
            "fixed_cycle_expected_cancel_registered",
            strategy=self.strategy.name,
            purpose=entry["purpose"],
            client_order_id=entry["client_order_id"],
            exchange_order_id=entry["exchange_order_id"],
            reason=entry["reason"],
            replacement_purpose=entry["replacement_purpose"],
            expires_in_ms=expires_in_ms,
        )

    def _confirm_expected_exit_cancel_replacement(
        self,
        intent: StrategyIntent,
        client_id: str,
        exchange_order_id: str | None,
    ) -> None:
        if intent.purpose not in self._expected_exit_cancel_purposes():
            return
        state = self.runtime_state.strategy_state
        registry = state.get("expected_exit_cancels")
        if not isinstance(registry, list) or not registry:
            return
        remaining: list[dict[str, Any]] = []
        consumed_match: dict[str, Any] | None = None
        fallback_match: dict[str, Any] | None = None
        for entry in registry:
            if not isinstance(entry, dict):
                continue
            if entry.get("replacement_purpose") == intent.purpose:
                if bool(entry.get("consumed")) and consumed_match is None:
                    consumed_match = entry
                    continue
                if fallback_match is None:
                    fallback_match = entry
                    continue
            remaining.append(entry)
        confirmed_entry = consumed_match or fallback_match
        if confirmed_entry is not None:
            if fallback_match is not None and confirmed_entry is not fallback_match:
                remaining.append(fallback_match)
            state["expected_exit_cancels"] = remaining
            self.audit.log_event(
                "fixed_cycle_expected_cancel_replacement_confirmed",
                strategy=self.strategy.name,
                purpose=confirmed_entry.get("purpose"),
                client_order_id=confirmed_entry.get("client_order_id"),
                exchange_order_id=confirmed_entry.get("exchange_order_id"),
                confirmed_entry_consumed=bool(confirmed_entry.get("consumed")),
                replacement_purpose=intent.purpose,
                replacement_client_order_id=client_id,
                replacement_exchange_order_id=exchange_order_id,
            )

    def _should_audit_order_payload_ready(
        self,
        managed_order: ManagedOrder,
        *,
        trigger_price: float | None,
    ) -> bool:
        purpose = str(managed_order.purpose or "").upper()
        exit_purposes = {
            self.strategy.LONG_TP_EXIT_PURPOSE,
            self.strategy.LONG_SL_EXIT_PURPOSE,
            self.strategy.SHORT_TP_EXIT_PURPOSE,
            self.strategy.SHORT_SL_EXIT_PURPOSE,
            getattr(self.strategy, "SHORT_HARD_STOP_PURPOSE", "SHORT_HARD_STOP_EXIT"),
        }
        if self._verbose_audit_enabled():
            return True
        return bool(
            managed_order.reduce_only
            or trigger_price is not None
            or managed_order.metadata.get("market_fallback")
            or purpose in exit_purposes
        )

    def _log_order_payload_ready(
        self,
        managed_order: ManagedOrder,
        *,
        trigger_price: float | None,
        exchange_side: str,
        reference_price: float,
        trigger_direction: Any,
        trigger_by: Any,
        order_filter: Any,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if not self._should_audit_order_payload_ready(managed_order, trigger_price=trigger_price):
            return
        payload = {
            "strategy": self.strategy.name,
            "purpose": managed_order.purpose,
            "side": managed_order.side,
            "order_type": managed_order.order_type,
            "qty": managed_order.qty,
            "price": managed_order.price,
            "trigger_price": trigger_price,
            "reduce_only": managed_order.reduce_only,
            "order_link_id": managed_order.client_order_id,
            "exchange_side": exchange_side,
            "reference_price": reference_price,
            "trigger_direction": trigger_direction,
            "trigger_by": trigger_by,
            "order_filter": order_filter,
        }
        if extra:
            payload.update(extra)
        self.audit.log_event("order_payload_ready", **payload)

    def _is_final_exit_purpose(self, purpose: Any) -> bool:
        purpose_text = str(purpose or "").upper()
        final_exit_purposes = {
            getattr(self.strategy, "LONG_TP_EXIT_PURPOSE", "LONG_TP_EXIT"),
            getattr(self.strategy, "LONG_TP_EXIT_RECOVERY_PURPOSE", "LONG_TP_EXIT_RECOVERY"),
            getattr(self.strategy, "LONG_SL_EXIT_PURPOSE", "LONG_SL_EXIT"),
            getattr(self.strategy, "SHORT_TP_EXIT_PURPOSE", "SHORT_TP_EXIT"),
            getattr(self.strategy, "SHORT_SL_EXIT_PURPOSE", "SHORT_SL_EXIT"),
            getattr(self.strategy, "SHORT_SL_EXIT_RECOVERY_PURPOSE", "SHORT_SL_EXIT_RECOVERY"),
            getattr(self.strategy, "SHORT_HARD_STOP_PURPOSE", "SHORT_HARD_STOP_EXIT"),
            "FINAL_LONG_EXIT",
            "FINAL_SHORT_EXIT",
        }
        return purpose_text in {str(item or "").upper() for item in final_exit_purposes}

    def _pending_final_exit_submissions(self) -> dict[str, dict[str, Any]]:
        state = self.runtime_state.strategy_state
        pending = state.get(self.PENDING_FINAL_EXIT_SUBMISSIONS_KEY)
        if not isinstance(pending, dict):
            pending = {}
            state[self.PENDING_FINAL_EXIT_SUBMISSIONS_KEY] = pending
        return pending

    def _build_final_exit_submit_signature(
        self,
        *,
        purpose: Any,
        qty: Any,
        trigger_price: Any,
        metadata: dict[str, Any] | None,
    ) -> dict[str, Any]:
        raw_metadata = metadata if isinstance(metadata, dict) else {}
        cycle_index_raw = raw_metadata.get("cycle_index")
        try:
            cycle_index = int(cycle_index_raw or 0)
        except (TypeError, ValueError):
            cycle_index = 0
        return {
            "purpose": str(purpose or "").upper(),
            "symbol": str(self.config.symbol or "").upper(),
            "qty": float(qty or 0.0),
            "trigger_price": self._safe_float(trigger_price, None),
            "basket_tp_price": self._safe_float(raw_metadata.get("basket_tp_price"), None),
            "basket_break_even_price": self._safe_float(raw_metadata.get("basket_break_even_price"), None),
            "exit_mode": str(raw_metadata.get("exit_mode") or ""),
            "exit_type": str(raw_metadata.get("exit_type") or ""),
            "cycle_index": cycle_index,
            "trade_block_id": str(self.runtime_state.strategy_state.get("trade_block_id") or ""),
        }

    def _final_exit_signatures_match(
        self,
        left: dict[str, Any],
        right: dict[str, Any],
        *,
        trigger_tol: float,
        qty_tol: float,
    ) -> bool:
        if str(left.get("purpose") or "") != str(right.get("purpose") or ""):
            return False
        if str(left.get("symbol") or "") != str(right.get("symbol") or ""):
            return False
        if str(left.get("exit_mode") or "") != str(right.get("exit_mode") or ""):
            return False
        if str(left.get("exit_type") or "") != str(right.get("exit_type") or ""):
            return False
        left_cycle = int(left.get("cycle_index") or 0)
        right_cycle = int(right.get("cycle_index") or 0)
        if left_cycle > 0 and right_cycle > 0 and left_cycle != right_cycle:
            return False
        left_trade_block = str(left.get("trade_block_id") or "")
        right_trade_block = str(right.get("trade_block_id") or "")
        if left_trade_block and right_trade_block and left_trade_block != right_trade_block:
            return False
        left_qty = float(left.get("qty") or 0.0)
        right_qty = float(right.get("qty") or 0.0)
        if abs(left_qty - right_qty) > qty_tol:
            return False
        left_trigger = self._safe_float(left.get("trigger_price"), None)
        right_trigger = self._safe_float(right.get("trigger_price"), None)
        if left_trigger is None or right_trigger is None:
            if left_trigger is not None or right_trigger is not None:
                return False
        elif abs(left_trigger - right_trigger) > trigger_tol:
            return False
        for key in ("basket_tp_price", "basket_break_even_price"):
            left_value = self._safe_float(left.get(key), None)
            right_value = self._safe_float(right.get(key), None)
            if left_value is None or right_value is None:
                if left_value is not None or right_value is not None:
                    return False
            elif abs(left_value - right_value) > trigger_tol:
                return False
        return True

    def _prune_stale_pending_final_exit_submissions(self) -> None:
        pending = self._pending_final_exit_submissions()
        if not pending:
            return
        now_ms = int(time.time() * 1000)
        stale_client_ids = [
            client_id
            for client_id, entry in pending.items()
            if (
                isinstance(entry, dict)
                and now_ms - int(entry.get("created_at_ms") or 0) > self.PENDING_FINAL_EXIT_MAX_AGE_MS
            )
        ]
        for client_id in stale_client_ids:
            pending.pop(client_id, None)
        if not pending:
            self.runtime_state.strategy_state.pop(self.PENDING_FINAL_EXIT_SUBMISSIONS_KEY, None)

    def _register_pending_final_exit_submission(
        self,
        *,
        client_order_id: str,
        exchange_order_id: str | None,
        purpose: str,
        side: str,
        qty: float,
        trigger_price: float | None,
        signature: dict[str, Any],
        source: str,
    ) -> None:
        self._prune_stale_pending_final_exit_submissions()
        pending = self._pending_final_exit_submissions()
        pending[client_order_id] = {
            "client_order_id": client_order_id,
            "exchange_order_id": exchange_order_id,
            "purpose": purpose,
            "side": side,
            "qty": float(qty or 0.0),
            "trigger_price": self._safe_float(trigger_price, None),
            "signature": dict(signature or {}),
            "source": source,
            "created_at_ms": int(time.time() * 1000),
        }

    def _clear_pending_final_exit_submission(
        self,
        *,
        client_order_id: str | None = None,
        purpose: str | None = None,
    ) -> None:
        raw_pending = self.runtime_state.strategy_state.get(self.PENDING_FINAL_EXIT_SUBMISSIONS_KEY)
        if not isinstance(raw_pending, dict) or not raw_pending:
            return
        pending = raw_pending
        if client_order_id:
            pending.pop(str(client_order_id), None)
        if purpose:
            purpose_text = str(purpose or "").upper()
            removable = [
                cid
                for cid, entry in pending.items()
                if str((entry or {}).get("purpose") or "").upper() == purpose_text
            ]
            for cid in removable:
                pending.pop(cid, None)
        if not pending:
            self.runtime_state.strategy_state.pop(self.PENDING_FINAL_EXIT_SUBMISSIONS_KEY, None)

    def _find_matching_pending_final_exit_submission(
        self,
        intent: StrategyIntent,
        *,
        trigger_tol: float,
        qty_tol: float,
    ) -> dict[str, Any] | None:
        self._prune_stale_pending_final_exit_submissions()
        pending = self._pending_final_exit_submissions()
        if not pending:
            return None
        intent_signature = self._build_final_exit_submit_signature(
            purpose=intent.purpose,
            qty=intent.qty,
            trigger_price=intent.trigger_price,
            metadata=intent.metadata,
        )
        for entry in pending.values():
            if not isinstance(entry, dict):
                continue
            if str(entry.get("purpose") or "").upper() != str(intent.purpose or "").upper():
                continue
            if str(entry.get("side") or "").lower() != str(intent.side or "").lower():
                continue
            candidate_signature = entry.get("signature") or {}
            if not isinstance(candidate_signature, dict):
                continue
            if self._final_exit_signatures_match(
                candidate_signature,
                intent_signature,
                trigger_tol=trigger_tol,
                qty_tol=qty_tol,
            ):
                return entry
        return None

    def _clear_pending_final_exit_submissions_if_flat(
        self,
        snapshot: HedgeSnapshot,
        *,
        source: str,
    ) -> None:
        if snapshot.long_qty > 0.0 or snapshot.short_qty > 0.0:
            return
        if GenericHedgeRuntime._has_nonterminal_runtime_orders(self.runtime_state):
            return
        if GenericHedgeRuntime._has_nonterminal_snapshot_orders(snapshot):
            return
        raw_pending = self.runtime_state.strategy_state.get(self.PENDING_FINAL_EXIT_SUBMISSIONS_KEY)
        if not isinstance(raw_pending, dict) or not raw_pending:
            return
        pending_count = len(raw_pending)
        self.runtime_state.strategy_state.pop(self.PENDING_FINAL_EXIT_SUBMISSIONS_KEY, None)
        self.audit.log_event(
            "fixed_cycle_pending_final_exit_submit_guard_cleared",
            strategy=self.strategy.name,
            source=source,
            reason="clean_flat_reset",
            pending_count=pending_count,
            symbol=snapshot.symbol,
        )

    def submit_intent(self, intent: StrategyIntent, snapshot: HedgeSnapshot, source: str) -> str | None:
        if self._startup_flat_reset_guard_active():
            purpose = str(intent.purpose or "").upper()
            allowed_initial_entry_purposes = {
                getattr(self.strategy, "LONG_ENTRY_PURPOSE", "INITIAL_LONG_ENTRY"),
                getattr(self.strategy, "SHORT_ENTRY_PURPOSE", "INITIAL_SHORT_ENTRY"),
            }
            if purpose not in allowed_initial_entry_purposes:
                if self._is_stale_purpose_blocked_after_flat_restart(purpose):
                    last_snapshot = self.runtime_state.last_snapshot or snapshot
                    nonterminal_runtime_order_count = sum(
                        1
                        for order in self.runtime_state.active_orders.values()
                        if not self._is_terminal_order_status(getattr(order, "status", None))
                    )
                    nonterminal_snapshot_order_count = sum(
                        1
                        for order in last_snapshot.active_orders
                        if not self._is_terminal_order_status(getattr(order, "status", None))
                    )
                    payload = {
                        "strategy": self.strategy.name,
                        "symbol": self.config.symbol,
                        "purpose": intent.purpose,
                        "side": intent.side,
                        "qty": intent.qty,
                        "trigger_price": intent.trigger_price,
                        "source": source,
                        "order_link_id": (intent.metadata or {}).get("order_link_id"),
                        "startup_flat_reset_applied": bool(
                            self.runtime_state.strategy_state.get("startup_flat_reset_applied")
                        ),
                        "initial_entry_confirmed": bool(
                            self.runtime_state.strategy_state.get("initial_entry_confirmed")
                        ),
                        "long_qty": getattr(last_snapshot, "long_qty", None),
                        "short_qty": getattr(last_snapshot, "short_qty", None),
                        "active_nonterminal_runtime_order_count": nonterminal_runtime_order_count,
                        "active_nonterminal_snapshot_order_count": nonterminal_snapshot_order_count,
                        "reason": "startup_flat_reset_applied",
                        "stale_classification": "stale_non_initial_after_flat_restart",
                    }
                    if self._should_log_repeated_event(
                        f"blocked_stale_intent:{source}:{purpose}:{intent.side}",
                        {
                            "purpose": intent.purpose,
                            "side": intent.side,
                            "source": source,
                            "startup_flat_reset_applied": payload["startup_flat_reset_applied"],
                            "initial_entry_confirmed": payload["initial_entry_confirmed"],
                            "long_qty": payload["long_qty"],
                            "short_qty": payload["short_qty"],
                            "active_nonterminal_runtime_order_count": nonterminal_runtime_order_count,
                            "active_nonterminal_snapshot_order_count": nonterminal_snapshot_order_count,
                            "stale_classification": payload["stale_classification"],
                        },
                        interval_seconds=120.0,
                    ):
                        self.audit.log_event(
                            "fixed_cycle_blocked_stale_intent_after_flat_restart",
                            **payload,
                        )
                    return None
        submit_price = intent.price if intent.price is not None else snapshot.current_price
        if intent.qty <= 0 or submit_price <= 0:
            self.audit.log_event(
                "intent_rejected",
                strategy=self.strategy.name,
                source=source,
                reason="invalid_qty_or_price",
                intent=intent,
                snapshot=snapshot,
            )
            return None
        normalized_qty = self.order_manager.normalize_qty(self.config.symbol, intent.qty, self.config.category)
        notional = normalized_qty * submit_price
        if normalized_qty <= 0 or notional < self.config.min_order_value:
            self.audit.log_event(
                "intent_rejected",
                strategy=self.strategy.name,
                source=source,
                reason="below_min_order_value",
                normalized_qty=normalized_qty,
                notional=notional,
                min_order_value=self.config.min_order_value,
                intent=intent,
            )
            return None
        self._ensure_max_leverage_before_trading()
        equivalent_order, reason, candidate_id, existing_trigger, existing_qty = self._find_equivalent_open_order(intent)
        duplicate_order, duplicate_source = self._find_duplicate_open_cycle_purpose(intent, snapshot)
        if duplicate_order is not None and equivalent_order is None and reason not in self._REPLACE_CANCEL_REASONS:
            existing_status = str(getattr(duplicate_order, "status", None) or "").upper()
            existing_trigger_price = getattr(duplicate_order, "trigger_price", None)
            self.audit.log_event(
                "fixed_cycle_duplicate_open_purpose_submit_blocked",
                strategy=self.strategy.name,
                purpose=intent.purpose,
                intent_qty=intent.qty,
                intent_trigger_price=intent.trigger_price,
                existing_client_order_id=getattr(duplicate_order, "client_order_id", None),
                existing_exchange_order_id=getattr(duplicate_order, "exchange_order_id", None),
                existing_status=existing_status,
                existing_trigger_price=existing_trigger_price,
                source=duplicate_source,
            )
            return getattr(duplicate_order, "client_order_id", None)
        if equivalent_order is not None:
            decision = "reuse"
        elif reason == "no_candidate":
            decision = "create"
        else:
            decision = "replace"
        exit_purposes = set(self.strategy._exit_purposes())
        metadata = getattr(intent, "metadata", {}) or {}
        purpose = getattr(intent, "purpose", None)
        side = getattr(intent, "side", None)
        reduce_only = getattr(intent, "reduce_only", False)
        position_idx = getattr(intent, "position_idx", None)
        cycle_role = metadata.get("cycle_role")
        cycle_index = metadata.get("cycle_index")
        is_exit_intent = intent.purpose in exit_purposes or str(
            metadata.get("cycle_role") or ""
        ).lower() == "long_reduce"
        is_final_exit_intent = self._is_final_exit_purpose(intent.purpose)
        final_exit_signature = (
            self._build_final_exit_submit_signature(
                purpose=intent.purpose,
                qty=intent.qty,
                trigger_price=intent.trigger_price,
                metadata=metadata,
            )
            if is_final_exit_intent
            else None
        )
        equivalence_check_payload = {
            "purpose": intent.purpose,
            "side": intent.side,
            "result": decision,
            "reject_reason": reason,
            "candidate_client_order_id": candidate_id,
            "existing_trigger_price": existing_trigger,
            "existing_qty": existing_qty,
            "new_trigger_price": intent.trigger_price,
            "new_qty": intent.qty,
        }
        should_log_equivalence_check = True
        no_candidate_default = reason == "no_candidate" and candidate_id is None
        if no_candidate_default and not self._verbose_audit_enabled():
            should_log_equivalence_check = self._should_log_idle_event(
                f"intent_equivalence_check:no_candidate:{source}:{intent.purpose}:{intent.side}",
                {
                    "source": source,
                    "purpose": intent.purpose,
                    "side": intent.side,
                    "reason": reason,
                },
                interval_seconds=120.0,
            )
            if should_log_equivalence_check:
                self.audit.log_event(
                    "intent_equivalence_check",
                    strategy=self.strategy.name,
                    source=source,
                    purpose=intent.purpose,
                    side=intent.side,
                    candidate_client_order_id=candidate_id,
                    result=decision,
                    reject_reason=reason,
                )
        elif reason == "no_candidate":
            should_log_equivalence_check = self._should_log_idle_event(
                "intent_equivalence_check:no_candidate",
                equivalence_check_payload,
            )
        if should_log_equivalence_check and not (no_candidate_default and not self._verbose_audit_enabled()):
            self.audit.log_event(
                "intent_equivalence_check",
                strategy=self.strategy.name,
                purpose=intent.purpose,
                side=intent.side,
                candidate_client_order_id=candidate_id,
                result=decision,
                reject_reason=reason,
                existing_trigger_price=existing_trigger,
                existing_qty=existing_qty,
                new_trigger_price=intent.trigger_price,
                new_qty=intent.qty,
            )
        should_log_replace_decision = True
        if reason == "no_candidate" and not self._verbose_audit_enabled():
            should_log_replace_decision = self._should_log_repeated_event(
                f"intent_replace_decision:{source}:{intent.purpose}:{intent.side}",
                {
                    "source": source,
                    "purpose": intent.purpose,
                    "side": intent.side,
                    "decision": decision,
                    "reason": reason,
                },
                interval_seconds=120.0,
            )
        if should_log_replace_decision and not (no_candidate_default and not self._verbose_audit_enabled()):
            self.audit.log_event(
                "intent_replace_decision",
                strategy=self.strategy.name,
                purpose=intent.purpose,
                side=intent.side,
                decision=decision,
                reason=reason,
            )
        replace_purposes_raw = intent.metadata.get("replace_open_purpose")
        post_refill_structure_rebuild_required = bool(
            intent.metadata.get("post_refill_structure_rebuild_required")
        )
        open_positions_exist = float(snapshot.long_qty or 0.0) > 0.0 or float(snapshot.short_qty or 0.0) > 0.0
        if (
            reason == "no_candidate"
            and replace_purposes_raw
            and not GenericHedgeRuntime._has_nonterminal_runtime_orders(self.runtime_state)
            and not GenericHedgeRuntime._has_nonterminal_snapshot_orders(snapshot)
        ):
            if post_refill_structure_rebuild_required and open_positions_exist:
                self.audit.log_event(
                    "fixed_cycle_post_refill_empty_snapshot_create_new_allowed",
                    strategy=self.strategy.name,
                    purpose=intent.purpose,
                    side=intent.side,
                    replace_open_purpose=replace_purposes_raw,
                    runtime_active_order_count=len(self.runtime_state.active_orders),
                    snapshot_active_order_count=len(snapshot.active_orders),
                )
            else:
                skip_reason = (
                    "active_orders_empty_race_condition"
                    if snapshot.active_orders
                    else "no_runtime_orders"
                )
                self.audit.log_event(
                    "intent_skip_due_to_empty_snapshot",
                    strategy=self.strategy.name,
                    purpose=intent.purpose,
                    side=intent.side,
                    reason=skip_reason,
                    replace_open_purpose=replace_purposes_raw,
                    runtime_active_order_count=len(self.runtime_state.active_orders),
                    snapshot_active_order_count=len(snapshot.active_orders),
                    nonterminal_runtime_order_count=sum(
                        1
                        for order in self.runtime_state.active_orders.values()
                        if not self._is_terminal_order_status(getattr(order, "status", None))
                    ),
                    nonterminal_snapshot_order_count=sum(
                        1
                        for order in snapshot.active_orders
                        if not self._is_terminal_order_status(getattr(order, "status", None))
                    ),
                    source=source,
                    qty=intent.qty,
                    trigger_price=intent.trigger_price,
                )
                return None
        if equivalent_order:
            if is_final_exit_intent:
                self.audit.log_event(
                    "fixed_cycle_duplicate_final_exit_submit_blocked",
                    strategy=self.strategy.name,
                    purpose=intent.purpose,
                    symbol=self.config.symbol,
                    trigger_price=intent.trigger_price,
                    qty=intent.qty,
                    signature=final_exit_signature,
                    existing_client_order_id=equivalent_order.client_order_id,
                    existing_exchange_order_id=equivalent_order.exchange_order_id,
                    source=source,
                    reason=reason,
                )
            self.audit.log_event(
                "intent_reuse_existing_order",
                strategy=self.strategy.name,
                purpose=intent.purpose,
                side=intent.side,
                client_order_id=equivalent_order.client_order_id,
                exchange_order_id=equivalent_order.exchange_order_id,
            )
            self._confirm_post_refill_structure_rebuild_progress(
                intent=intent,
                snapshot=snapshot,
                source="equivalent",
            )
            return equivalent_order.client_order_id

        final_symbol_payload = {"symbol": self.config.symbol}
        if hasattr(self.strategy, "config"):
            final_symbol_payload["strategy_symbol"] = getattr(self.strategy.config, "symbol", None)
        self.logger.info("final_symbol_used", final_symbol_payload)
        emergency_latency_ms = None
        if intent.purpose in {
            getattr(self.strategy, "EMERGENCY_FLAT_LONG_PURPOSE", None),
            getattr(self.strategy, "EMERGENCY_FLAT_SHORT_PURPOSE", None),
        }:
            trigger_ts = self.runtime_state.strategy_state.get("emergency_trigger_monotonic")
            if trigger_ts:
                emergency_latency_ms = max(0, int((time.monotonic() - trigger_ts) * 1000))
        intent_event_payload = {
            "strategy": self.strategy.name,
            "purpose": intent.purpose,
            "side": intent.side,
            "qty": intent.qty,
            "order_type": intent.order_type,
            "trigger_price": intent.trigger_price,
            "reduce_only": intent.reduce_only,
            "position_idx": intent.position_idx,
        }
        if emergency_latency_ms is not None:
            intent_event_payload["emergency_latency_ms"] = emergency_latency_ms
        self.audit.log_event("intent_submit_started", **intent_event_payload)
        replace_purposes_raw = intent.metadata.get("replace_open_purpose")
        replace_purposes: list[str] = []
        safe_cycle_replacement = False
        if replace_purposes_raw:
            replace_purposes = (
                [replace_purposes_raw]
                if isinstance(replace_purposes_raw, str)
                else list(replace_purposes_raw)
            )
            replacement_order = next(
                (
                    order
                    for order in self.runtime_state.active_orders.values()
                    if order.purpose in replace_purposes
                    and not self._is_terminal_order_status(order.status)
                ),
                None,
            )
            replacement_requirements = (
                self._cycle_first_leg_reduce_requirements(replacement_order.purpose)
                if replacement_order
                else None
            )
            if replacement_order and replacement_requirements:
                old_cycle_role = intent.metadata.get("cycle_role")
                old_reduce_only = intent.reduce_only
                old_position_idx = intent.position_idx
                cycle_role_value = (
                    replacement_order.metadata.get("cycle_role")
                    or replacement_requirements["cycle_role"]
                )
                intent.metadata["cycle_role"] = cycle_role_value
                cycle_index_value = replacement_order.metadata.get("cycle_index")
                if cycle_index_value is not None:
                    intent.metadata["cycle_index"] = cycle_index_value
                intent.reduce_only = True
                intent.position_idx = int(replacement_requirements["position_idx"])
                intent.side = str(replacement_requirements["side"])
                expected_position_idx = int(replacement_requirements["position_idx"])
                replacement_position_idx = int(
                    replacement_order.metadata.get("position_idx") or expected_position_idx
                )
                safe_cycle_replacement = bool(
                    replacement_order.reduce_only
                    and replacement_order.side == replacement_requirements["side"]
                    and replacement_position_idx == expected_position_idx
                )
                self.audit.log_event(
                    "fixed_cycle_long_reduce_intent_metadata_restored",
                    strategy=self.strategy.name,
                    purpose=intent.purpose,
                    replaced_purpose=replacement_order.purpose,
                    old_cycle_role=old_cycle_role,
                    new_cycle_role=cycle_role_value,
                    old_reduce_only=old_reduce_only,
                    new_reduce_only=intent.reduce_only,
                    old_position_idx=old_position_idx,
                    new_position_idx=intent.position_idx,
                    cycle_index=cycle_index_value,
                    replace_open_purposes=replace_purposes,
                )
            if reason in self._REPLACE_CANCEL_REASONS:
                replace_context = {
                    "reason": reason,
                    "existing_trigger_price": existing_trigger,
                    "new_trigger_price": intent.trigger_price,
                    "existing_qty": existing_qty,
                    "new_qty": intent.qty,
                    "replacement_purpose": intent.purpose,
                }
                self._cancel_open_orders_by_purpose_internal(
                    replace_purposes,
                    replace_context,
                    intent=intent,
                )
        purpose_upper = str(intent.purpose or "").upper()
        if purpose_upper == getattr(self.strategy, "RECOVERY_RELOAD_LONG_ENTRY", "RECOVERY_RELOAD_LONG_ENTRY"):
            prefix = "fc-rrl"
        elif purpose_upper == getattr(self.strategy, "RECOVERY_RELOAD_SHORT_ENTRY", "RECOVERY_RELOAD_SHORT_ENTRY"):
            prefix = "fc-rrs"
        else:
            prefix = f"{self.strategy.name}-{str(intent.purpose or '').lower()}"
        if bool(metadata.get("normal_cycle_second_leg_split")):
            try:
                split_stage_index = int(metadata.get("split_stage_index"))
                prefix = f"{prefix}-split{split_stage_index}"
            except (TypeError, ValueError):
                pass
        client_id = f"{prefix}-{uuid4().hex[:8]}"
        current_price = snapshot.current_price
        strategy_state = self.runtime_state.strategy_state
        fallback_context = self._build_long_add_market_fallback_context(
            intent=intent,
            snapshot=snapshot,
            normalized_qty=normalized_qty,
        )
        should_force_fallback = bool(fallback_context and fallback_context.get("should_fallback"))
        if intent.trigger_price is not None:
            trigger_price = intent.trigger_price
            invalid_reason = None
            if current_price is None or current_price <= 0:
                self.audit.log_event(
                    "intent_trigger_invalid",
                    strategy=self.strategy.name,
                    purpose=intent.purpose,
                    side=intent.side,
                    trigger_price=trigger_price,
                    current_price=current_price,
                    direction=intent.trigger_direction,
                    reason="missing_current_price",
                )
                return None
            if (
                intent.position_idx == 2
                and intent.trigger_direction == 2
                and current_price <= trigger_price
                and intent.metadata.get("cycle_role") == "short_reduce"
            ):
                if self._trailing_fallback.active:
                    return None
                self._trailing_fallback.activate(
                    purpose=intent.purpose,
                    position_idx=intent.position_idx,
                    qty=intent.qty,
                    trigger_price=trigger_price,
                    current_price=current_price,
                    trailing_dist=float(self.strategy.config.trailing_stop_dist),
                )
                strategy_state["trailing_active"] = intent.purpose
                return None
            if intent.trigger_direction == 2 and trigger_price >= current_price:
                invalid_reason = "falling_trigger_not_below_market"
            elif intent.trigger_direction == 1 and trigger_price <= current_price:
                invalid_reason = "rising_trigger_not_above_market"
            if invalid_reason is not None and not should_force_fallback:
                self.audit.log_event(
                    "intent_trigger_invalid",
                    strategy=self.strategy.name,
                    purpose=intent.purpose,
                    side=intent.side,
                    trigger_price=trigger_price,
                    current_price=current_price,
                    direction=intent.trigger_direction,
                    reason=invalid_reason,
                )
                return None
        submit_qty = normalized_qty
        fallback_reason: str | None = None
        force_market_fallback = False
        if fallback_context:
            cycle_role = str(intent.metadata.get("cycle_role") or "")
            self.audit.log_event(
                "pre_long_add_trigger_validation",
                strategy=self.strategy.name,
                purpose=intent.purpose,
                side=intent.side,
                trigger_price=fallback_context["trigger_price"],
                current_price=fallback_context["current_price"],
                qty=normalized_qty,
                clamped_qty=fallback_context["fallback_qty"],
                available_long_qty=fallback_context["available_long_qty"],
                should_fallback=fallback_context["should_fallback"],
                cycle_role=cycle_role,
            )
            if fallback_context["should_fallback"]:
                fallback_reason = "stale_trigger"
                force_market_fallback = True
                submit_qty = fallback_context["fallback_qty"]
                self.audit.log_event(
                    "stale_long_add_trigger_market_fallback",
                    strategy=self.strategy.name,
                    purpose=intent.purpose,
                    side=intent.side,
                    trigger_price=fallback_context["trigger_price"],
                    current_price=fallback_context["current_price"],
                    fallback_qty=fallback_context["fallback_qty"],
                    available_long_qty=fallback_context["available_long_qty"],
                    cycle_role=cycle_role,
                )
        first_leg_requirements = self._cycle_first_leg_reduce_requirements(intent.purpose)
        submit_notional = submit_qty * submit_price
        if first_leg_requirements:
            old_reduce_only = intent.reduce_only
            old_side = intent.side
            old_position = intent.position_idx
            old_cycle_role = intent.metadata.get("cycle_role")
            corrected = False
            expected_cycle_role = first_leg_requirements["cycle_role"]
            expected_side = str(first_leg_requirements["side"])
            expected_position_idx = int(first_leg_requirements["position_idx"])
            if intent.metadata.get("cycle_role") != expected_cycle_role:
                intent.metadata["cycle_role"] = expected_cycle_role
                corrected = True
            if intent.side != expected_side:
                intent.side = expected_side
                corrected = True
            if not intent.reduce_only:
                intent.reduce_only = True
                corrected = True
            if intent.position_idx != expected_position_idx:
                intent.position_idx = expected_position_idx
                corrected = True
            # NOTE: This audit hook intentionally renames the intent from "ADD" to
            # the canonical long/short reduce settings. The name is historical,
            # so we rewrite `reduce_only` and `side` here to keep the runtime
            # semantics consistent with a reduce leg.
            if corrected:
                self.audit.log_event(
                    "fixed_cycle_long_reduce_intent_corrected",
                    strategy=self.strategy.name,
                    purpose=intent.purpose,
                    old_reduce_only=old_reduce_only,
                    new_reduce_only=intent.reduce_only,
                    old_side=old_side,
                    new_side=intent.side,
                    old_position_idx=old_position,
                    new_position_idx=intent.position_idx,
                    old_cycle_role=old_cycle_role,
                    new_cycle_role=intent.metadata.get("cycle_role"),
                    trigger_price=intent.trigger_price,
                    qty=intent.qty,
                )
        short_tp_pending_cycle = int(
            self.runtime_state.strategy_state.get("short_tp_pending_cycle") or 0
        )
        cycle_waiting_flag = bool(
            self.runtime_state.strategy_state.get("cycle_waiting_for_short_tp")
        )
        cycle_waiting = cycle_waiting_flag and short_tp_pending_cycle > 0
        blocking_cycle_index = None
        if first_leg_requirements:
            cycle_index = 0
            try:
                cycle_index = int((intent.metadata or {}).get("cycle_index") or 0)
            except (TypeError, ValueError):
                cycle_index = 0
            if cycle_index <= 0:
                match = re.search(r"CYCLE_(\d+)_", str(intent.purpose or "").upper())
                if match:
                    cycle_index = int(match.group(1))
            blocking_cycle_resolver = getattr(self.strategy, "_blocking_cycle_before_long_add", None)
            cycle_entry_resolver = getattr(self.strategy, "_get_cycle_sequence_entry", None)
            if callable(blocking_cycle_resolver) and cycle_index > 0:
                blocking_cycle = blocking_cycle_resolver(self.runtime_state, cycle_index)
                if blocking_cycle is not None:
                    blocking_cycle_index = int(blocking_cycle[0])
                    cycle_waiting = True
                elif callable(cycle_entry_resolver):
                    cycle_entry = cycle_entry_resolver(self.runtime_state, cycle_index)
                    if bool((cycle_entry or {}).get("complete")):
                        cycle_waiting = False
                elif not cycle_waiting:
                    cycle_waiting = False
            if (
                blocking_cycle_index == cycle_index
                and short_tp_pending_cycle <= 0
                and cycle_waiting
            ):
                self.audit.log_event(
                    "fixed_cycle_same_cycle_long_reduce_blocker_ignored",
                    strategy=self.strategy.name,
                    purpose=intent.purpose,
                    cycle_index=cycle_index,
                    blocking_cycle_index=blocking_cycle_index,
                    short_tp_pending_cycle=short_tp_pending_cycle,
                    cycle_waiting_for_short_tp=cycle_waiting_flag,
                )
                cycle_waiting = False
                blocking_cycle_index = None
        allow_short_long_reduce = (
            isinstance(self.strategy, ShortFixedCycleHedgeStrategy)
            and short_tp_pending_cycle > 0
            and purpose
            == getattr(
                self.strategy,
                "_get_second_leg_purpose",
                lambda idx: None,
            )(short_tp_pending_cycle)
        )
        if (
            cycle_waiting
            and first_leg_requirements
            and not safe_cycle_replacement
            and not allow_short_long_reduce
        ):
            existing_purposes = [
                order.purpose for order in self.runtime_state.active_orders.values()
            ]
            self.audit.log_event(
                "fixed_cycle_long_reduce_intent_blocked_phase",
                strategy=self.strategy.name,
                purpose=intent.purpose,
                cycle_waiting_for_short_tp=True,
                reduce_only=intent.reduce_only,
                side=intent.side,
                position_idx=intent.position_idx,
                cycle_role=intent.metadata.get("cycle_role"),
                qty=intent.qty,
                trigger_price=intent.trigger_price,
                active_order_purposes=existing_purposes,
                blocking_cycle_index=blocking_cycle_index,
            )
            return None
        managed_order = ManagedOrder(
            client_order_id=client_id,
            side=intent.side,
            qty=submit_qty,
            purpose=intent.purpose,
            price=intent.price,
            order_type=intent.order_type,
            reduce_only=intent.reduce_only,
            remaining_qty=submit_qty,
            metadata={
                **dict(intent.metadata),
                "source": source,
                "entry_price": snapshot.long_avg if intent.side == "long" else snapshot.short_avg,
                "snapshot_price": snapshot.current_price,
                "trigger_price": intent.trigger_price,
                "trigger_direction": intent.trigger_direction,
                "trigger_by": intent.trigger_by,
                "close_on_trigger": intent.close_on_trigger,
                "position_idx": intent.position_idx,
                "order_filter": intent.order_filter,
                "market_fallback": bool(intent.metadata.get("market_fallback")) or force_market_fallback,
                "market_fallback_reason": intent.metadata.get("fallback_reason") or fallback_reason,
            },
            trace=list(intent.trace),
        )
        # Mark final exit intents that should use the trading-stop API so downstream
        # logic can treat them specially (no normal order cancel, different submit path).
        purpose_upper = str(intent.purpose or "").upper()
        if hasattr(self.strategy, "_get_final_exit_purposes"):
            final_exit_trading_stop_purposes = {
                str(p).upper()
                for p in (self.strategy._get_final_exit_purposes() or set())
            }
        else:
            final_exit_trading_stop_purposes = {
                str(getattr(self.strategy, "LONG_TP_EXIT_PURPOSE", "LONG_TP_EXIT")).upper(),
                str(getattr(self.strategy, "SHORT_SL_EXIT_PURPOSE", "SHORT_SL_EXIT")).upper(),
            }
        if is_final_exit_intent and purpose_upper in final_exit_trading_stop_purposes:
            managed_order.metadata["trading_stop_api"] = True
        if is_final_exit_intent and final_exit_signature:
            self._register_pending_final_exit_submission(
                client_order_id=managed_order.client_order_id,
                exchange_order_id=managed_order.exchange_order_id,
                purpose=managed_order.purpose,
                side=managed_order.side,
                qty=managed_order.qty,
                trigger_price=self._safe_float(managed_order.metadata.get("trigger_price"), None),
                signature=final_exit_signature,
                source=source,
            )
        if first_leg_requirements:
            exchange_side_check = self._exchange_side(intent.side, intent.reduce_only)
            expected_exchange_side = first_leg_requirements["exchange_side"]
            expected_position_idx = int(first_leg_requirements["position_idx"])
            expected_cycle_role = first_leg_requirements["cycle_role"]
            if (
                exchange_side_check != expected_exchange_side
                or not intent.reduce_only
                or intent.position_idx != expected_position_idx
            ):
                self.audit.log_event(
                    "fixed_cycle_invalid_long_reduce_order_blocked",
                    strategy=self.strategy.name,
                    purpose=intent.purpose,
                    exchange_side=exchange_side_check,
                    reduce_only=intent.reduce_only,
                    position_idx=intent.position_idx,
                    cycle_role=intent.metadata.get("cycle_role"),
                    expected_exchange_side=expected_exchange_side,
                    expected_position_idx=expected_position_idx,
                    expected_cycle_role=expected_cycle_role,
                    qty=intent.qty,
                    trigger_price=intent.trigger_price,
                )
                return None
        try:
            response = self._submit_to_exchange(
                managed_order,
                snapshot,
                force_market_fallback=force_market_fallback,
            )
        except Exception as exc:
            self.audit.log_event(
                "order_rejected",
                strategy=self.strategy.name,
                purpose=managed_order.purpose,
                side=managed_order.side,
                order_type=managed_order.order_type,
                qty=managed_order.qty,
                price=managed_order.price,
                order_link_id=managed_order.client_order_id,
                status="rejected",
                error_code=type(exc).__name__,
                error_message=str(exc),
            )
            self.audit.log_event(
                "intent_submit_failed",
                strategy=self.strategy.name,
                purpose=managed_order.purpose,
                side=managed_order.side,
                reason="exception",
                error_code=type(exc).__name__,
                error_message=str(exc),
            )
            if is_final_exit_intent:
                self._clear_pending_final_exit_submission(client_order_id=managed_order.client_order_id)
            raise
        if not response:
            error_info = getattr(self.order_manager, "last_post_error", None)
            if self._should_block_symbol_permission_error(managed_order, error_info):
                self._handle_symbol_permission_reject(managed_order, error_info)
                return None
            error_code = "no_response"
            error_message = "exchange returned no response"
            if error_info and error_info.get("retCode"):
                error_code = f"bybit_{error_info.get('retCode')}"
                error_message = error_info.get("retMsg") or error_message
            self.audit.log_event(
                "order_rejected",
                strategy=self.strategy.name,
                purpose=managed_order.purpose,
                side=managed_order.side,
                order_type=managed_order.order_type,
                qty=managed_order.qty,
                price=managed_order.price,
                order_link_id=managed_order.client_order_id,
                status="rejected",
                error_code=error_code,
                error_message=error_message,
            )
            self.audit.log_event(
                "intent_submit_failed",
                strategy=self.strategy.name,
                purpose=managed_order.purpose,
                side=managed_order.side,
                reason="no_response",
                error_code=error_code,
                error_message=error_message,
            )
            self.audit.log_event(
                "intent_submit_failed",
                strategy=self.strategy.name,
                source=source,
                client_order_id=client_id,
                intent=intent,
                traces=trace_dicts(intent.trace),
            )
            if force_market_fallback:
                self.audit.log_event(
                    "long_add_market_fallback_failed",
                    strategy=self.strategy.name,
                    purpose=intent.purpose,
                    side=intent.side,
                    client_order_id=client_id,
                    qty=managed_order.qty,
                    reason=fallback_reason,
                    error_code=self._long_add_error_code(error_info),
                    error_message=self._long_add_error_message(error_info),
                )
                return None
            if fallback_context and self._should_trigger_rejection_market_fallback(error_info):
                self.audit.log_event(
                    "long_add_conditional_rejected_market_fallback",
                    strategy=self.strategy.name,
                    purpose=intent.purpose,
                    side=intent.side,
                    trigger_price=fallback_context["trigger_price"],
                    current_price=fallback_context["current_price"],
                    ret_code=self._long_add_error_code(error_info),
                    ret_msg=self._long_add_error_message(error_info),
                )
                fallback_reason = "conditional_rejection"
                force_market_fallback = True
                managed_order.qty = fallback_context["fallback_qty"]
                managed_order.remaining_qty = managed_order.qty
                managed_order.metadata["market_fallback"] = True
                managed_order.metadata["market_fallback_reason"] = fallback_reason
                response = self._submit_to_exchange(
                    managed_order,
                    snapshot,
                    force_market_fallback=True,
                )
                if not response:
                    error_info = getattr(self.order_manager, "last_post_error", error_info)
                    self.audit.log_event(
                        "long_add_market_fallback_failed",
                        strategy=self.strategy.name,
                        purpose=intent.purpose,
                        side=intent.side,
                        client_order_id=client_id,
                        qty=managed_order.qty,
                        reason=fallback_reason,
                        error_code=self._long_add_error_code(error_info),
                        error_message=self._long_add_error_message(error_info),
                    )
                    if is_final_exit_intent:
                        self._clear_pending_final_exit_submission(client_order_id=managed_order.client_order_id)
                    return None
            else:
                if is_final_exit_intent:
                    self._clear_pending_final_exit_submission(client_order_id=managed_order.client_order_id)
                return None
        exchange_order_id = ((response.get("result") or {}).get("orderId")) if isinstance(response, dict) else None
        response_code = None
        if isinstance(response, dict):
            for key in ("retCode", "ret_code", "code"):
                if key in response:
                    response_code = response[key]
                    break
        # Für Trading-Stop-basierte Exit-Orders wird keine echte Bybit-OrderId
        # im Runtime-State hinterlegt, damit nachfolgende REST-Operationen
        # (cancel, history lookups, etc.) nicht mit synthetischen IDs arbeiten.
        if getattr(managed_order, "metadata", None) and managed_order.metadata.get("trading_stop_api"):
            exchange_order_id = None
        self.audit.log_event(
            "order_submitted",
            strategy=self.strategy.name,
            purpose=managed_order.purpose,
            side=managed_order.side,
            order_type=managed_order.order_type,
            qty=managed_order.qty,
            price=managed_order.price,
            order_link_id=managed_order.client_order_id,
            exchange_order_id=exchange_order_id,
            status="submitted",
            response_code=response_code,
        )
        managed_order.exchange_order_id = exchange_order_id
        managed_order.status = "OPEN"
        if is_exit_intent:
            state = self.runtime_state.strategy_state
            state["exit_orders_submitted_once"] = True
        self._mark_strategy_cycle_purpose_status(
            purpose=managed_order.purpose,
            metadata=managed_order.metadata,
            status="SUBMITTED",
        )
        self.runtime_state.active_orders[client_id] = managed_order
        self._process_pending_unmatched_fills(
            client_id=client_id,
            exchange_order_id=exchange_order_id,
        )
        if exchange_order_id:
            self.runtime_state.exchange_to_client_id[exchange_order_id] = client_id
        if is_final_exit_intent:
            self._clear_pending_final_exit_submission(client_order_id=client_id)
        self.audit.log_event(
            "intent_submitted",
            strategy=self.strategy.name,
            source=source,
            client_order_id=client_id,
            exchange_order_id=exchange_order_id,
            purpose=intent.purpose,
            side=intent.side,
            qty=managed_order.qty,
            normalized_qty=submit_qty,
            order_type=managed_order.order_type,
            reduce_only=managed_order.reduce_only,
            trigger_price=intent.trigger_price,
            submit_notional=submit_notional,
        )
        self._mark_refill_intent_registry_submitted(
            intent=intent,
            client_order_id=client_id,
            exchange_order_id=exchange_order_id,
        )
        self._confirm_post_refill_structure_rebuild_progress(
            intent=intent,
            snapshot=snapshot,
            source="submitted",
        )
        self._confirm_expected_exit_cancel_replacement(intent, client_id, exchange_order_id)
        if self._verbose_audit_enabled():
            self.audit.log_event(
                "intent_submitted_verbose",
                strategy=self.strategy.name,
                source=source,
                client_order_id=client_id,
                exchange_order_id=exchange_order_id,
                intent=intent,
                traces=trace_dicts(intent.trace),
            )
        if fallback_reason:
            fallback_event = {
                "strategy": self.strategy.name,
                "purpose": intent.purpose,
                "side": intent.side,
                "client_order_id": client_id,
                "qty": managed_order.qty,
                "reason": fallback_reason,
            }
            if fallback_context:
                fallback_event.update(
                    {
                        "trigger_price": fallback_context["trigger_price"],
                        "current_price": fallback_context["current_price"],
                        "available_long_qty": fallback_context["available_long_qty"],
                        "fallback_qty": fallback_context["fallback_qty"],
                    }
                )
            self.audit.log_event("long_add_market_fallback_submitted", **fallback_event)
        self._save_strategy_state()
        return client_id

    def _mark_refill_intent_registry_submitted(
        self,
        intent: StrategyIntent,
        client_order_id: str,
        exchange_order_id: str | None,
    ) -> None:
        if intent.purpose not in {"REFILL_LONG", "REFILL_SHORT"}:
            return
        state = self.runtime_state.strategy_state
        registry = state.get("refill_intent_registry") or {}
        entry = registry.get(intent.purpose)
        if not entry or entry.get("refill_batch_id") != state.get("refill_batch_id"):
            return
        entry["status"] = "SUBMITTED"
        entry["client_order_id"] = client_order_id
        if exchange_order_id:
            entry["exchange_order_id"] = exchange_order_id
        self.audit.log_event(
            "fixed_cycle_refill_intent_marked_submitted",
            strategy=self.strategy.name,
            purpose=intent.purpose,
            refill_batch_id=entry.get("refill_batch_id"),
            client_order_id=client_order_id,
            exchange_order_id=exchange_order_id,
        )

    def _cycle_first_leg_reduce_requirements(
        self,
        purpose: object,
    ) -> dict[str, object] | None:
        normalized = str(purpose or "").upper()
        # NOTE: Historically the Long cycle helpers keep the "_LONG_ADD" suffix,
        # but the runtime treats these intents as Long reduce/close legs. They end
        # up as side=Sell, position_idx=1, reduce_only=True. Do not infer that the
        # name implies a position increase.
        first_leg_side_getter = getattr(self.strategy, "_get_first_leg_side", None)
        first_leg_side = (
            str(first_leg_side_getter() or "long").lower()
            if callable(first_leg_side_getter)
            else "long"
        )
        if first_leg_side == "long" and purpose_mapping.is_cycle_long_add(normalized):
            return {
                "side": "long",
                "position_idx": 1,
                "reduce_only": True,
                "cycle_role": "long_reduce",
                "exchange_side": "Sell",
            }
        # NOTE: Likewise, the short cycle helper reuses "SHORT_ADD" even though the
        # runtime treats it as a Short reduce leg (Buy, position_idx=2,
        # reduce_only=True). This naming is misleading but intentional.
        if first_leg_side == "short" and purpose_mapping.is_cycle_short_reduce(normalized):
            return {
                "side": "short",
                "position_idx": 2,
                "reduce_only": True,
                "cycle_role": "short_reduce",
                "exchange_side": "Buy",
            }
        if first_leg_side == "long" and purpose_mapping.is_cycle_short_add(normalized):
            return {
                "side": "short",
                "position_idx": 2,
                "reduce_only": True,
                "cycle_role": "short_reduce",
                "exchange_side": "Buy",
            }
        if first_leg_side == "short" and purpose_mapping.is_cycle_long_reduce(normalized):
            return {
                "side": "long",
                "position_idx": 1,
                "reduce_only": True,
                "cycle_role": "long_reduce",
                "exchange_side": "Sell",
            }
        return None

    def _build_long_add_market_fallback_context(
        self,
        *,
        intent: StrategyIntent,
        snapshot: HedgeSnapshot,
        normalized_qty: float,
    ) -> dict[str, Any] | None:
        purpose = str(intent.purpose or "").upper()
        if not ((purpose.startswith("CYCLE_") and purpose.endswith("_LONG_ADD")) or purpose == "LONG_REDUCE"):
            return None
        trigger_price = self._safe_float(intent.trigger_price, None)
        if trigger_price is None:
            trigger_price = self._safe_float(intent.metadata.get("trigger_price"), None)
        if trigger_price is None:
            return None
        current_price = float(snapshot.current_price or 0.0)
        if current_price <= 0:
            return None
        available_long_qty = float(snapshot.long_qty or 0.0)
        fallback_qty = normalized_qty
        if available_long_qty > 0 and fallback_qty > available_long_qty:
            fallback_qty = available_long_qty
        if fallback_qty <= 0:
            return None
        return {
            "trigger_price": trigger_price,
            "current_price": current_price,
            "available_long_qty": available_long_qty,
            "fallback_qty": fallback_qty,
            "should_fallback": current_price <= trigger_price,
        }

    def _should_trigger_rejection_market_fallback(
        self,
        error_info: dict[str, Any] | None,
    ) -> bool:
        if not error_info:
            return False
        code = str(
            error_info.get("retCode")
            or error_info.get("ret_code")
            or error_info.get("code")
            or ""
        )
        msg = str(
            error_info.get("retMsg")
            or error_info.get("ret_msg")
            or error_info.get("message")
            or error_info.get("error")
            or ""
        )
        if code == "110093":
            return True
        return "expect falling" in msg.lower()

    @staticmethod
    def _long_add_error_code(error_info: dict[str, Any] | None) -> str | None:
        if not error_info:
            return None
        return (
            error_info.get("retCode")
            or error_info.get("ret_code")
            or error_info.get("code")
        )

    @staticmethod
    def _long_add_error_message(error_info: dict[str, Any] | None) -> str | None:
        if not error_info:
            return None
        return (
            error_info.get("retMsg")
            or error_info.get("ret_msg")
            or error_info.get("message")
            or error_info.get("error")
            or str(error_info)
        )

    def cancel_open_orders_by_purpose(self, purposes: list[str]) -> None:
        with self._lock:
            self._cancel_open_orders_by_purpose_internal(purposes)


    def _find_equivalent_open_order(
        self, intent: StrategyIntent
    ) -> tuple[
        ManagedOrder | None,
        str,
        str | None,
        float | None,
        float | None,
    ]:
        tick_size = float(self.strategy.config.price_tick_size or 0.0) or 1e-8
        price_tol = tick_size * 3
        qty_tol = (float(self.strategy.config.qty_step or 0.0) or 1e-9) * 2
        target_trigger = intent.trigger_price or 0.0
        first_leg_requirements = self._cycle_first_leg_reduce_requirements(intent.purpose)
        is_first_leg_reduce = first_leg_requirements is not None
        is_exit_order = intent.purpose in {
            "LONG_TP_EXIT",
            "SHORT_SL_EXIT",
            "LONG_SL_EXIT",
            "SHORT_TP_EXIT",
        }
        is_final_exit_intent = self._is_final_exit_purpose(intent.purpose)
        final_exit_intent_signature = (
            self._build_final_exit_submit_signature(
                purpose=intent.purpose,
                qty=intent.qty,
                trigger_price=intent.trigger_price,
                metadata=intent.metadata,
            )
            if is_final_exit_intent
            else None
        )
        long_add_qty_tol = max(qty_tol, 50.0)
        exit_trigger_tol = max(price_tol, tick_size * 2)
        last_candidate_id = None
        last_trigger = None
        last_qty = None
        last_reason = "no_candidate"
        candidate_count = 0
        rejected_count = 0
        stage_mismatch_reject_count = 0
        same_identity_reject_reason: str | None = None
        intent_metadata = getattr(intent, "metadata", {}) or {}
        intent_identity: tuple | None = None
        for order in self.runtime_state.active_orders.values():
            if order.status not in {"OPEN", "PARTIAL"}:
                continue
            if order.purpose != intent.purpose:
                continue
            if order.side != intent.side or order.order_type != intent.order_type:
                continue
            if order.reduce_only != intent.reduce_only:
                continue
            existing_idx = int(
                order.metadata.get("position_idx") or (1 if order.side == "long" else 2)
            )
            intent_idx = int(intent.position_idx or (1 if intent.side == "long" else 2))
            candidate_count += 1

            # Split-/Stage-aware Equivalence: nur Orders mit identischer Submit-Identität
            # (Purpose + Stage/Split-Daten) können als äquivalent betrachtet werden.
            order_metadata = dict(order.metadata or {})
            if intent_identity is None:
                intent_identity = self._cycle_submit_identity(
                    str(intent.purpose or "").upper(), intent_metadata
                )
            order_identity = self._cycle_submit_identity(
                str(order.purpose or "").upper(), order_metadata
            )
            if intent_identity != order_identity:
                last_candidate_id = order.client_order_id
                last_reason = "stage_identity_mismatch"
                rejected_count += 1
                stage_mismatch_reject_count += 1
                self.audit.log_event(
                    "intent_equivalence_reject",
                    strategy=self.strategy.name,
                    purpose=intent.purpose,
                    side=intent.side,
                    candidate_client_order_id=order.client_order_id,
                    reason=last_reason,
                    expected_identity=repr(intent_identity),
                    actual_identity=repr(order_identity),
                )
                continue

            if existing_idx != intent_idx:
                last_candidate_id = order.client_order_id
                last_reason = "position_idx_mismatch"
                rejected_count += 1
                self.audit.log_event(
                    "intent_equivalence_reject",
                    strategy=self.strategy.name,
                    purpose=intent.purpose,
                    side=intent.side,
                    candidate_client_order_id=order.client_order_id,
                    reason=last_reason,
                    expected=intent.position_idx,
                    actual=order.metadata.get("position_idx"),
                )
                continue
            if str(order.metadata.get("trigger_direction") or "") != str(intent.trigger_direction or ""):
                last_candidate_id = order.client_order_id
                last_reason = "trigger_direction_mismatch"
                rejected_count += 1
                self.audit.log_event(
                    "intent_equivalence_reject",
                    strategy=self.strategy.name,
                    purpose=intent.purpose,
                    side=intent.side,
                    candidate_client_order_id=order.client_order_id,
                    reason=last_reason,
                    expected=intent.trigger_direction,
                    actual=order.metadata.get("trigger_direction"),
                )
                continue
            if str(order.metadata.get("trigger_by") or "") != str(intent.trigger_by or ""):
                last_candidate_id = order.client_order_id
                last_reason = "trigger_by_mismatch"
                rejected_count += 1
                self.audit.log_event(
                    "intent_equivalence_reject",
                    strategy=self.strategy.name,
                    purpose=intent.purpose,
                    side=intent.side,
                    candidate_client_order_id=order.client_order_id,
                    reason=last_reason,
                    expected=intent.trigger_by,
                    actual=order.metadata.get("trigger_by"),
                )
                continue
            if order.metadata.get("close_on_trigger") != intent.close_on_trigger:
                last_candidate_id = order.client_order_id
                last_reason = "close_on_trigger_mismatch"
                rejected_count += 1
                self.audit.log_event(
                    "intent_equivalence_reject",
                    strategy=self.strategy.name,
                    purpose=intent.purpose,
                    side=intent.side,
                    candidate_client_order_id=order.client_order_id,
                    reason=last_reason,
                    expected=intent.close_on_trigger,
                    actual=order.metadata.get("close_on_trigger"),
                )
                continue
            existing_filter = str(order.metadata.get("order_filter") or "")
            intent_filter = str(intent.order_filter or "")
            if existing_filter != intent_filter:
                last_candidate_id = order.client_order_id
                last_reason = "order_filter_mismatch"
                rejected_count += 1
                self.audit.log_event(
                    "intent_equivalence_reject",
                    strategy=self.strategy.name,
                    purpose=intent.purpose,
                    side=intent.side,
                    candidate_client_order_id=order.client_order_id,
                    reason=last_reason,
                    expected=intent_filter,
                    actual=existing_filter,
                )
                continue
            existing_trigger = self._safe_float(order.metadata.get("trigger_price"), None)
            existing_qty = order.qty
            last_candidate_id = order.client_order_id
            last_trigger = existing_trigger
            last_qty = existing_qty
            if is_final_exit_intent and final_exit_intent_signature is not None:
                existing_signature = self._build_final_exit_submit_signature(
                    purpose=order.purpose,
                    qty=order.qty,
                    trigger_price=order.metadata.get("trigger_price"),
                    metadata=order.metadata,
                )
                if not self._final_exit_signatures_match(
                    existing_signature,
                    final_exit_intent_signature,
                    trigger_tol=exit_trigger_tol,
                    qty_tol=qty_tol,
                ):
                    last_reason = "final_exit_signature_mismatch"
                    rejected_count += 1
                    self.audit.log_event(
                        "intent_equivalence_reject",
                        strategy=self.strategy.name,
                        purpose=intent.purpose,
                        side=intent.side,
                        candidate_client_order_id=order.client_order_id,
                        reason=last_reason,
                        expected=final_exit_intent_signature,
                        actual=existing_signature,
                    )
                    continue
            self.audit.log_event(
                "intent_equivalence_candidate",
                strategy=self.strategy.name,
                purpose=intent.purpose,
                side=intent.side,
                candidate_client_order_id=order.client_order_id,
                candidate_exchange_order_id=order.exchange_order_id,
                result="match" if existing_trigger is not None and abs(existing_trigger - target_trigger) <= price_tol and abs(existing_qty - intent.qty) <= qty_tol else "reject",
                reject_reason=last_reason,
                existing_trigger_price=existing_trigger,
                new_trigger_price=target_trigger,
                existing_qty=existing_qty,
                new_qty=intent.qty,
            )
            if is_exit_order:
                trigger_limit = exit_trigger_tol
            else:
                trigger_limit = price_tol
            if existing_trigger is None or abs(existing_trigger - target_trigger) > trigger_limit:
                last_reason = "trigger_diff"
                rejected_count += 1
                same_identity_reject_reason = "trigger_diff"
                self.audit.log_event(
                    "intent_equivalence_reject",
                    strategy=self.strategy.name,
                    purpose=intent.purpose,
                    side=intent.side,
                    candidate_client_order_id=order.client_order_id,
                    reason=last_reason,
                    expected=target_trigger,
                    actual=existing_trigger,
                )
                continue
            if is_first_leg_reduce:
                qty_limit = long_add_qty_tol
            else:
                qty_limit = qty_tol
            if abs(existing_qty - intent.qty) > qty_limit:
                last_reason = "qty_diff"
                rejected_count += 1
                same_identity_reject_reason = "qty_diff"
                self.audit.log_event(
                    "intent_equivalence_reject",
                    strategy=self.strategy.name,
                    purpose=intent.purpose,
                    side=intent.side,
                    candidate_client_order_id=order.client_order_id,
                    reason=last_reason,
                    expected=intent.qty,
                    actual=existing_qty,
                )
                continue
            return order, "match", last_candidate_id, existing_trigger, existing_qty
        if is_final_exit_intent:
            pending_candidate = self._find_matching_pending_final_exit_submission(
                intent,
                trigger_tol=exit_trigger_tol,
                qty_tol=qty_tol,
            )
            if pending_candidate:
                pending_client_order_id = str(pending_candidate.get("client_order_id") or "")
                pending_exchange_order_id = str(pending_candidate.get("exchange_order_id") or "")
                pending_trigger = self._safe_float(pending_candidate.get("trigger_price"), None)
                pending_qty = float(pending_candidate.get("qty") or 0.0)
                self.audit.log_event(
                    "intent_equivalence_candidate",
                    strategy=self.strategy.name,
                    purpose=intent.purpose,
                    side=intent.side,
                    candidate_client_order_id=pending_client_order_id,
                    candidate_exchange_order_id=pending_exchange_order_id,
                    result="match",
                    reject_reason="pending_submit_in_progress",
                    existing_trigger_price=pending_trigger,
                    new_trigger_price=target_trigger,
                    existing_qty=pending_qty,
                    new_qty=intent.qty,
                )
                return (
                    ManagedOrder(
                        client_order_id=pending_client_order_id,
                        side=intent.side,
                        qty=pending_qty or float(intent.qty or 0.0),
                        purpose=intent.purpose,
                        price=intent.price,
                        order_type=intent.order_type,
                        reduce_only=intent.reduce_only,
                        exchange_order_id=pending_exchange_order_id or None,
                        status="PENDING_SUBMIT",
                        remaining_qty=pending_qty or float(intent.qty or 0.0),
                        metadata=dict(intent.metadata or {}),
                        trace=[],
                    ),
                    "pending_submit_in_progress",
                    pending_client_order_id or None,
                    pending_trigger,
                    pending_qty,
                )

        if same_identity_reject_reason:
            final_reason = same_identity_reject_reason
        elif candidate_count > 0 and rejected_count == stage_mismatch_reject_count:
            final_reason = "no_candidate"
        else:
            final_reason = last_reason if candidate_count > 0 else "no_candidate"
        equivalence_summary_payload = {
            "purpose": intent.purpose,
            "total_candidates": candidate_count,
            "rejected_candidates": rejected_count,
            "final_reason": final_reason,
        }
        should_log_equivalence_summary = True
        if final_reason == "no_candidate":
            should_log_equivalence_summary = self._should_log_idle_event(
                f"intent_equivalence_summary:no_candidate:{intent.purpose}:{intent.side}",
                {
                    "purpose": intent.purpose,
                    "side": intent.side,
                    "final_reason": final_reason,
                },
                interval_seconds=120.0,
            )
        if should_log_equivalence_summary:
            self.audit.log_event(
                "intent_equivalence_summary",
                strategy=self.strategy.name,
                purpose=intent.purpose,
                total_candidates=candidate_count,
                rejected_candidates=rejected_count,
                final_reason=final_reason,
            )
        return None, final_reason, last_candidate_id, last_trigger, last_qty

    def _ensure_max_leverage_before_trading(self) -> None:
        cache_key = (self.config.category, self.config.symbol.upper())
        if cache_key in self._max_leverage_ready_symbols:
            return
        ensured = self.order_manager.ensure_max_leverage(self.config.symbol, self.config.category)
        max_leverage_event_key = f"max_leverage_preflight:{cache_key[0]}:{cache_key[1]}"
        max_leverage_payload = {
            "strategy": self.strategy.name,
            "symbol": self.config.symbol,
            "category": self.config.category,
            "ensured": ensured,
        }
        should_log_max_leverage = True
        if not ensured:
            should_log_max_leverage = self._should_log_idle_event(
                max_leverage_event_key,
                max_leverage_payload,
            )
        if should_log_max_leverage:
            self.audit.log_event(
                "max_leverage_preflight",
                **max_leverage_payload,
            )
        if ensured:
            self._max_leverage_ready_symbols.add(cache_key)

        rules = self.order_manager.get_cached_instrument_rules(
            self.config.symbol, self.config.category
        )
        symbol_upper = self.config.symbol.upper()
        if rules:
            self.runtime_state.instrument_rules[symbol_upper] = rules
            self.logger.info(
                "loaded_instrument_rules %s",
                {
                    "symbol": symbol_upper,
                    "tick_size": str(rules.get("tick_size") or "0"),
                    "qty_step": str(rules.get("qty_step") or "0"),
                    "min_order_qty": str(rules.get("min_order_qty") or "0"),
                    "min_notional_value": str(rules.get("min_notional") or "0"),
                    "source": "bybit",
                },
            )
        else:
            self.logger.warning(
                "loaded_instrument_rules_missing %s",
                {"symbol": symbol_upper, "reason": "rules_not_found_in_runtime_state"},
            )
            rules = self.order_manager.get_cached_instrument_rules(
                self.config.symbol, self.config.category
            )
            if rules:
                symbol_upper = self.config.symbol.upper()
                self.runtime_state.instrument_rules[symbol_upper] = rules
                self.logger.info(
                    "loaded_instrument_rules %s",
                    {
                        "symbol": symbol_upper,
                        "tick_size": str(rules.get("tick_size") or "0"),
                        "qty_step": str(rules.get("qty_step") or "0"),
                        "min_order_qty": str(rules.get("min_order_qty") or "0"),
                        "min_notional_value": str(rules.get("min_notional") or "0"),
                    },
                )
            else:
                self.logger.warning(
                    "loaded_instrument_rules missing for %s", self.config.symbol.upper()
                )
            rules = self.order_manager.get_cached_instrument_rules(self.config.symbol, self.config.category)
            if rules:
                self.runtime_state.instrument_rules[self.config.symbol.upper()] = rules
            else:
                self.logger.warning(
                    "Instrument rules missing while ensuring max leverage for %s",
                    self.config.symbol.upper(),
                )

    def _cancel_open_orders_by_purpose_internal(
        self,
        purposes: list[str],
        replace_context: dict[str, Any] | None = None,
        *,
        intent: StrategyIntent | None = None,
    ) -> None:
        purposes_set = {purpose for purpose in purposes if purpose}
        if not purposes_set:
            return
        intent_metadata = dict(getattr(intent, "metadata", None) or {}) if intent else {}
        intent_identity: tuple | None = None
        if intent and bool(intent_metadata.get("normal_cycle_second_leg_split")):
            intent_identity = cycle_submit_identity(
                str(intent.purpose or "").upper(),
                intent_metadata,
            )
        snapshot = self.runtime_state.last_snapshot
        long_qty = float(snapshot.long_qty or 0.0) if snapshot else 0.0
        short_qty = float(snapshot.short_qty or 0.0) if snapshot else 0.0
        for client_id, order in list(self.runtime_state.active_orders.items()):
            if order.purpose not in purposes_set or self._is_terminal_order_status(order.status):
                continue
            if intent_identity is not None:
                order_identity = cycle_submit_identity(
                    str(order.purpose or "").upper(),
                    dict(order.metadata or {}),
                )
                if order_identity != intent_identity:
                    continue
            # Trading-stop basierte Exit-Orders werden nicht über den normalen
            # /v5/order/cancel-Pfad gecancelt. Stattdessen werden sie nur aus dem
            # lokalen Runtime-State entfernt, wenn ein neuer Trading-Stop gesetzt
            # wird oder ein Reset erfolgt.
            if getattr(order, "metadata", None) and getattr(order, "metadata", {}).get("trading_stop_api"):
                self.audit.log_event(
                    "fixed_cycle_trading_stop_final_exit_skipped_cancel",
                    strategy=self.strategy.name,
                    client_order_id=client_id,
                    purpose=order.purpose,
                    long_qty=long_qty,
                    short_qty=short_qty,
                )
                self.runtime_state.active_orders.pop(client_id, None)
                if order.exchange_order_id:
                    self.runtime_state.exchange_to_client_id.pop(order.exchange_order_id, None)
                continue
            # CRITICAL EXIT PROTECTION GUARD
            if order.purpose == self.strategy.SHORT_SL_EXIT_PURPOSE:
                if long_qty <= 0.0 and short_qty > 0.0:
                    self.audit.log_event(
                        "cancel_blocked_protect_short_sl",
                        strategy=self.strategy.name,
                        client_order_id=client_id,
                        reason="short_position_still_open",
                        long_qty=long_qty,
                        short_qty=short_qty,
                    )
                    continue
            if order.purpose == self.strategy.LONG_TP_EXIT_PURPOSE:
                if short_qty <= 0.0 and long_qty > 0.0:
                    self.audit.log_event(
                        "cancel_blocked_protect_long_tp",
                        strategy=self.strategy.name,
                        client_order_id=client_id,
                        reason="long_position_still_open",
                        long_qty=long_qty,
                        short_qty=short_qty,
                    )
                    continue
            # Variant C: do not cancel residual staged second-leg reduces while
            # basket coverage is insufficient and inventory is still open.
            if long_qty > 0.0 or short_qty > 0.0:
                order_is_residual = False
                checker = getattr(
                    self.strategy, "_order_is_open_residual_staged_second_leg", None
                )
                if callable(checker):
                    try:
                        order_is_residual = bool(checker(order))
                    except Exception:
                        order_is_residual = False
                if order_is_residual:
                    allow_fn = getattr(
                        self.strategy,
                        "allow_cancel_residual_staged_second_leg_orders",
                        None,
                    )
                    protect = False
                    if callable(allow_fn):
                        try:
                            decision = allow_fn(
                                snapshot, self.runtime_state
                            )
                            protect = bool(
                                getattr(decision, "staging_incomplete", False)
                                and not bool(getattr(decision, "coverage_ok", True))
                            )
                        except Exception:
                            protect = False
                    if protect:
                        self.audit.log_event(
                            "cancel_blocked_protect_residual_staged_second_leg",
                            strategy=self.strategy.name,
                            client_order_id=client_id,
                            purpose=order.purpose,
                            reason="insufficient_basket_coverage",
                            long_qty=long_qty,
                            short_qty=short_qty,
                        )
                        continue
            canceled = False
            if order.exchange_order_id:
                self._register_expected_exit_cancel(client_id, order, replace_context)
                canceled = self.order_manager.cancel_order(
                    order.exchange_order_id,
                    symbol=self.config.symbol,
                    category=self.config.category,
                )
            order.status = "CANCELED" if canceled or not order.exchange_order_id else order.status
            self.runtime_state.active_orders.pop(client_id, None)
            if order.exchange_order_id:
                self.runtime_state.exchange_to_client_id.pop(order.exchange_order_id, None)
            self._clear_pending_final_exit_submission(
                client_order_id=client_id,
                purpose=order.purpose,
            )
            self.audit.log_event(
                "intent_replaced_cancel",
                strategy=self.strategy.name,
                client_order_id=client_id,
                exchange_order_id=order.exchange_order_id,
                purpose=order.purpose,
                canceled=canceled,
                reason=replace_context.get("reason") if replace_context else None,
                existing_trigger_price=replace_context.get("existing_trigger_price") if replace_context else None,
                new_trigger_price=replace_context.get("new_trigger_price") if replace_context else None,
                existing_qty=replace_context.get("existing_qty") if replace_context else None,
                new_qty=replace_context.get("new_qty") if replace_context else None,
            )

    def _should_block_symbol_permission_error(
        self,
        managed_order: ManagedOrder,
        error_info: dict[str, Any] | None,
    ) -> bool:
        if not error_info:
            return False
        ret_code = int(error_info.get("retCode") or 0)
        # Non-retryable agreement/permission errors for specific contracts
        if ret_code not in {110123, 110125, 110126}:
            return False
        return managed_order.purpose in {
            self.strategy.LONG_ENTRY_PURPOSE,
            self.strategy.SHORT_ENTRY_PURPOSE,
        }

    def _handle_symbol_permission_reject(
        self,
        managed_order: ManagedOrder,
        error_info: dict[str, Any],
    ) -> None:
        symbol = str(self.config.symbol or "").upper()
        ret_code = int(error_info.get("retCode") or 0)
        ret_msg = str(error_info.get("retMsg") or "")
        payload = {
            "symbol": symbol,
            "bot_name": self.config.bot_name,
            "purpose": managed_order.purpose,
            "ret_code": ret_code,
            "ret_msg": ret_msg,
        }
        self.audit.log_event("fixed_cycle_symbol_permission_rejected", **payload)
        self._blacklist_symbol(
            symbol,
            "bybit_110126_required_agreement",
            ret_code,
            ret_msg,
        )
        self._release_dynamic_symbol_reservation()
        state = self.runtime_state.strategy_state
        state["initial_entry_blocked_symbol"] = symbol
        state["initial_entry_retry_blocked"] = True
        state["initial_entry_retry_count"] = 0
        state["initial_entry_submitted"] = False
        state["dynamic_symbol_reselection_reason"] = "bybit_110126"
        state["next_dynamic_entry_allowed_at"] = datetime.now(timezone.utc).isoformat()
        abort_payload = {
            "symbol": symbol,
            "purpose": managed_order.purpose,
            "ret_code": ret_code,
            "ret_msg": ret_msg,
        }
        self.audit.log_event("fixed_cycle_initial_entry_batch_aborted_after_non_retryable_error", **abort_payload)
        reselect_payload = {
            "symbol": symbol,
            "bot_name": self.config.bot_name,
            "reason": "bybit_110126_required_agreement",
        }
        self.audit.log_event("dynamic_symbol_reselect_requested", **reselect_payload)

    def _submit_to_exchange(
        self,
        managed_order: ManagedOrder,
        snapshot: HedgeSnapshot,
        force_market_fallback: bool = False,
    ) -> Any:
        exchange_side = self._exchange_side(managed_order.side, managed_order.reduce_only)
        position_idx_raw = managed_order.metadata.get("position_idx")
        position_idx = int(position_idx_raw) if position_idx_raw is not None else (1 if managed_order.side == "long" else 2)
        exit_api = managed_order.metadata.get("exit_api")
        trigger_price = self._safe_float(managed_order.metadata.get("trigger_price"), None)
        trigger_direction = managed_order.metadata.get("trigger_direction")
        trigger_by = managed_order.metadata.get("trigger_by")
        close_on_trigger = managed_order.metadata.get("close_on_trigger")
        order_filter = managed_order.metadata.get("order_filter")
        tp_limit_price = self._safe_float(managed_order.metadata.get("tp_limit_price"), None)
        slippage_tolerance_type = managed_order.metadata.get("slippage_tolerance_type")
        slippage_tolerance = self._safe_float(managed_order.metadata.get("slippage_tolerance"), None)
        purpose_upper = str(managed_order.purpose or "").upper()

        # Final Basket-Exit-Orders werden über Bybit /v5/position/trading-stop
        # als positionsgebundene TP/SL gesetzt, nicht mehr als klassische
        # Conditional Orders über /v5/order/create. Wir behalten dennoch einen
        # ManagedOrder-Eintrag als logischen Exit-Schutz im Runtime-State.
        if getattr(managed_order, "metadata", None) and managed_order.metadata.get("trading_stop_api"):
            # Nur LONG_TP_EXIT und SHORT_SL_EXIT werden hier über Trading-Stop
            # abgebildet. Andere Exit-Pfade nutzen weiterhin den normalen Order-Pfad.
            if trigger_price is None or trigger_price <= 0:
                # Fallback – ohne gültigen Trigger-Preis kein Trading-Stop möglich.
                self.audit.log_event(
                    "fixed_cycle_trading_stop_final_exit_failed",
                    strategy=self.strategy.name,
                    purpose=managed_order.purpose,
                    reason="missing_trigger_price",
                    position_idx=position_idx,
                )
                return None

            trigger_by_value = str(trigger_by) if trigger_by else "LastPrice"
            symbol = self.config.symbol
            category = self.config.category

            resolved_trigger_price = self._prepare_trading_stop_trigger_price(
                managed_order=managed_order,
                snapshot=snapshot,
                symbol=symbol,
                category=category,
                position_idx=position_idx,
                trigger_price=trigger_price,
                trigger_by=trigger_by_value,
            )
            if resolved_trigger_price is None or resolved_trigger_price <= 0:
                return None
            trigger_price = resolved_trigger_price

            self.audit.log_event(
                "fixed_cycle_trading_stop_final_exit_submit_started",
                strategy=self.strategy.name,
                purpose=managed_order.purpose,
                symbol=symbol,
                category=category,
                position_idx=position_idx,
                trigger_price=trigger_price,
                trigger_by=trigger_by_value,
            )

            long_tp = getattr(self.strategy, "LONG_TP_EXIT_PURPOSE", "LONG_TP_EXIT")
            short_sl = getattr(self.strategy, "SHORT_SL_EXIT_PURPOSE", "SHORT_SL_EXIT")
            long_sl = getattr(self.strategy, "LONG_SL_EXIT_PURPOSE", "LONG_SL_EXIT")
            short_tp = getattr(self.strategy, "SHORT_TP_EXIT_PURPOSE", "SHORT_TP_EXIT")

            if purpose_upper in {long_tp, short_tp}:
                # Take-Profit-Feld für Long- oder Short-Position setzen.
                response = self.order_manager.set_full_position_trading_stop(
                    symbol=symbol,
                    position_idx=position_idx,
                    category=category,
                    take_profit=trigger_price,
                    stop_loss=None,
                    trigger_by=trigger_by_value,
                )
            elif purpose_upper in {long_sl, short_sl}:
                # Stop-Loss-Feld für Long- oder Short-Position setzen.
                response = self.order_manager.set_full_position_trading_stop(
                    symbol=symbol,
                    position_idx=position_idx,
                    category=category,
                    take_profit=None,
                    stop_loss=trigger_price,
                    trigger_by=trigger_by_value,
                )
            else:
                # Unerwarteter Purpose mit trading_stop_api-Flag – zur Sicherheit
                # zurück in den normalen Pfad fallen lassen.
                response = None

            ret_code = None
            ret_msg = None
            is_success = False
            raw_error_info = None
            if isinstance(response, dict):
                ret_code = response.get("retCode")
                ret_msg = response.get("retMsg")
            else:
                raw_error_info = getattr(self.order_manager, "last_post_error", None)
                if isinstance(raw_error_info, dict):
                    ret_code = raw_error_info.get("retCode")
                    ret_msg = raw_error_info.get("retMsg")

            # Bybit 34040 = "not modified" (idempotent success für bereits
            # gesetzten Trading-Stop).
            success_codes = {0, "0", 34040, "34040"}
            is_success = ret_code in success_codes

            synthetic_order_id = f"trading-stop:{purpose_upper}:{str(symbol or '').upper()}:{position_idx}"
            wrapped_response: dict[str, Any] = {
                "retCode": ret_code,
                "retMsg": ret_msg,
                "result": {
                    "orderId": synthetic_order_id,
                    "orderLinkId": managed_order.client_order_id,
                    "tradingStop": True,
                },
                "raw": response,
                "raw_error_info": raw_error_info,
            }

            state = self.runtime_state.strategy_state
            if is_success:
                state["final_exit_trading_stop_active"] = True
                state["final_exit_trading_stop_signature"] = state.get("last_exit_signature")
                if purpose_upper == getattr(self.strategy, "LONG_TP_EXIT_PURPOSE", "LONG_TP_EXIT"):
                    state["final_exit_trading_stop_long_tp"] = float(trigger_price)
                if purpose_upper == getattr(self.strategy, "SHORT_SL_EXIT_PURPOSE", "SHORT_SL_EXIT"):
                    state["final_exit_trading_stop_short_sl"] = float(trigger_price)
                if hasattr(self.strategy, "_record_final_exit_trading_stop_submission"):
                    try:
                        self.strategy._record_final_exit_trading_stop_submission(
                            self.runtime_state,
                            snapshot,
                            purpose=managed_order.purpose,
                            side=managed_order.side,
                            client_order_id=managed_order.client_order_id,
                            exchange_order_id=synthetic_order_id,
                            trigger_price=trigger_price,
                            position_idx=position_idx,
                        )
                    except Exception:
                        self.logger.exception(
                            "fixed_cycle_trading_stop_context_store_failed %s",
                            {
                                "purpose": managed_order.purpose,
                                "side": managed_order.side,
                                "symbol": symbol,
                                "position_idx": position_idx,
                            },
                        )
                state["exit_orders_submitted_once"] = True
                state["exit_rebuild_allowed"] = False
                state["force_exit_rebuild"] = False
                if ret_code in (34040, "34040"):
                    self.audit.log_event(
                        "fixed_cycle_trading_stop_final_exit_not_modified",
                        strategy=self.strategy.name,
                        purpose=managed_order.purpose,
                        symbol=symbol,
                        category=category,
                        position_idx=position_idx,
                        trigger_price=trigger_price,
                        trigger_by=trigger_by_value,
                        ret_code=ret_code,
                        ret_msg=ret_msg,
                    )
                self.audit.log_event(
                    "fixed_cycle_trading_stop_final_exit_set",
                    strategy=self.strategy.name,
                    purpose=managed_order.purpose,
                    symbol=symbol,
                    category=category,
                    position_idx=position_idx,
                    trigger_price=trigger_price,
                    trigger_by=trigger_by_value,
                    synthetic_order_id=synthetic_order_id,
                    ret_code=ret_code,
                    ret_msg=ret_msg,
                )
                self.audit.log_event(
                    "fixed_cycle_trading_stop_final_exit_marked_active",
                    strategy=self.strategy.name,
                    purpose=managed_order.purpose,
                    symbol=symbol,
                    category=category,
                    position_idx=position_idx,
                    trigger_price=trigger_price,
                    trigger_by=trigger_by_value,
                    ret_code=ret_code,
                    ret_msg=ret_msg,
                    final_exit_trading_stop_active=True,
                    final_exit_trading_stop_signature=state.get("final_exit_trading_stop_signature"),
                    final_exit_trading_stop_long_tp=state.get("final_exit_trading_stop_long_tp"),
                    final_exit_trading_stop_short_sl=state.get("final_exit_trading_stop_short_sl"),
                )
                return wrapped_response

            # Fehlgeschlagener Trading-Stop: nur Diagnose-Log, kein aktiver Exit-Schutz.
            self.audit.log_event(
                "fixed_cycle_trading_stop_final_exit_failed",
                strategy=self.strategy.name,
                purpose=managed_order.purpose,
                symbol=symbol,
                category=category,
                position_idx=position_idx,
                trigger_price=trigger_price,
                trigger_by=trigger_by_value,
                ret_code=ret_code,
                ret_msg=ret_msg,
            )

            return None

        if force_market_fallback and managed_order.reduce_only:
            self._log_order_payload_ready(
                managed_order,
                trigger_price=trigger_price,
                exchange_side=exchange_side,
                reference_price=snapshot.current_price,
                trigger_direction=trigger_direction,
                trigger_by=trigger_by,
                order_filter=order_filter,
                extra={
                    "market_fallback": True,
                    "fallback_reason": managed_order.metadata.get("market_fallback_reason"),
                },
            )
            return self.order_manager.place_reduce_market_order(
                symbol=self.config.symbol,
                side=exchange_side,
                qty=managed_order.qty,
                position_idx=position_idx,
                category=self.config.category,
                order_link_id=managed_order.client_order_id,
            )
        if managed_order.reduce_only and trigger_price is not None:
            self._log_order_payload_ready(
                managed_order,
                trigger_price=trigger_price,
                exchange_side=exchange_side,
                reference_price=snapshot.current_price,
                trigger_direction=trigger_direction,
                trigger_by=trigger_by,
                order_filter=order_filter,
            )
            return self.order_manager.place_reduce_market_order(
                symbol=self.config.symbol,
                side=exchange_side,
                qty=managed_order.qty,
                position_idx=position_idx,
                category=self.config.category,
                order_link_id=managed_order.client_order_id,
                trigger_price=trigger_price,
                trigger_direction=int(trigger_direction) if trigger_direction is not None else None,
                trigger_by=str(trigger_by) if trigger_by else None,
                close_on_trigger=bool(close_on_trigger) if close_on_trigger is not None else False,
            )
        if exit_api == "short_tp_limit":
            self._log_order_payload_ready(
                managed_order,
                trigger_price=trigger_price,
                exchange_side=exchange_side,
                reference_price=snapshot.current_price,
                trigger_direction=trigger_direction,
                trigger_by=trigger_by,
                order_filter=order_filter,
            )
            return self.order_manager.set_short_take_profit_limit(
                symbol=self.config.symbol,
                tp_price=trigger_price or 0.0,
                tp_limit_price=tp_limit_price or float(managed_order.price or trigger_price or 0.0),
                position_size=managed_order.qty,
                position_idx=position_idx,
                category=self.config.category,
                trigger_by=str(trigger_by or "LastPrice"),
            )
        if managed_order.order_type == "Limit" or trigger_price is not None:
            payload = OrderPayload(
                category=self.config.category,
                symbol=self.config.symbol,
                side=exchange_side,
                order_type=managed_order.order_type,
                price=managed_order.price,
                qty=managed_order.qty,
                reduce_only=managed_order.reduce_only,
                position_idx=position_idx,
                order_link_id=managed_order.client_order_id,
                trigger_price=trigger_price,
                trigger_direction=int(trigger_direction) if trigger_direction is not None else None,
                trigger_by=str(trigger_by) if trigger_by else None,
                close_on_trigger=bool(close_on_trigger) if close_on_trigger is not None else None,
                order_filter=str(order_filter) if order_filter else None,
                slippage_tolerance_type=slippage_tolerance_type,
                slippage_tolerance=slippage_tolerance,
            )
            self._log_order_payload_ready(
                managed_order,
                trigger_price=trigger_price,
                exchange_side=exchange_side,
                reference_price=snapshot.current_price,
                trigger_direction=trigger_direction,
                trigger_by=trigger_by,
                order_filter=order_filter,
            )
            return self.order_manager.place_limit_order(payload)
        if managed_order.reduce_only:
            self._log_order_payload_ready(
                managed_order,
                trigger_price=trigger_price,
                exchange_side=exchange_side,
                reference_price=snapshot.current_price,
                trigger_direction=trigger_direction,
                trigger_by=trigger_by,
                order_filter=order_filter,
            )
            return self.order_manager.place_reduce_market_order(
                symbol=self.config.symbol,
                side=exchange_side,
                qty=managed_order.qty,
                position_idx=position_idx,
                category=self.config.category,
                order_link_id=managed_order.client_order_id,
            )
        self._log_order_payload_ready(
            managed_order,
            trigger_price=trigger_price,
            exchange_side=exchange_side,
            reference_price=snapshot.current_price,
            trigger_direction=trigger_direction,
            trigger_by=trigger_by,
            order_filter=order_filter,
        )
        return self.order_manager.place_market_order(
            symbol=self.config.symbol,
            side=exchange_side,
            qty=managed_order.qty,
            price=snapshot.current_price,
            position_idx=position_idx,
            category=self.config.category,
            order_link_id=managed_order.client_order_id,
        )

    def _normalize_runtime_price(self, price: float) -> float:
        if price <= 0:
            return 0.0
        normalize_fn = getattr(self.strategy, "_normalize_price", None)
        if callable(normalize_fn):
            try:
                normalized = normalize_fn(price, self.runtime_state)
                return float(normalized or 0.0)
            except Exception:
                pass
        return float(
            self.order_manager.normalize_price(
                self.config.symbol,
                price,
                self.config.category,
            )
        )

    def _resolve_trading_stop_tick_size(self, symbol: str) -> float:
        rules = self.runtime_state.instrument_rules.get(str(symbol or "").upper()) or {}
        tick_size = self._safe_float(rules.get("tick_size"), None)
        if tick_size and tick_size > 0:
            return tick_size
        cached_rules = self.order_manager.get_cached_instrument_rules(symbol, self.config.category) or {}
        tick_size = self._safe_float(cached_rules.get("tick_size"), None)
        if tick_size and tick_size > 0:
            return tick_size
        return max(float(getattr(self.config, "price_tick_size", 0.0) or 0.0), 0.0)

    def _resolve_trading_stop_base_price(
        self,
        *,
        symbol: str,
        category: str,
        snapshot: HedgeSnapshot,
        trigger_by: str,
    ) -> tuple[float | None, str]:
        trigger_by_upper = str(trigger_by or "LastPrice").strip().upper()
        current_price = self._safe_float(getattr(snapshot, "current_price", None), None)

        def _valid(value: float | None) -> bool:
            return value is not None and value > 0

        if trigger_by_upper == "MARKPRICE":
            mark_price = self.order_manager.fetch_mark_price(symbol, category)
            if _valid(mark_price):
                return float(mark_price), "mark_price"
            if _valid(current_price):
                return float(current_price), "snapshot_current_price"
            last_price = self.order_manager.fetch_last_price(symbol, category)
            if _valid(last_price):
                return float(last_price), "last_price_fallback"
            return None, "missing_mark_price"

        last_price = self.order_manager.fetch_last_price(symbol, category)
        if _valid(last_price):
            return float(last_price), "last_price"
        if _valid(current_price):
            return float(current_price), "snapshot_current_price"
        mark_price = self.order_manager.fetch_mark_price(symbol, category)
        if _valid(mark_price):
            return float(mark_price), "mark_price_fallback"
        return None, "missing_last_price"

    def _prepare_trading_stop_trigger_price(
        self,
        *,
        managed_order: ManagedOrder,
        snapshot: HedgeSnapshot,
        symbol: str,
        category: str,
        position_idx: int,
        trigger_price: float,
        trigger_by: str,
    ) -> float | None:
        purpose = str(managed_order.purpose or "")
        purpose_upper = purpose.upper()
        side = str(managed_order.side or "").lower()
        original_price = float(trigger_price or 0.0)
        tick_size = self._resolve_trading_stop_tick_size(symbol)
        base_price, source_price_field = self._resolve_trading_stop_base_price(
            symbol=symbol,
            category=category,
            snapshot=snapshot,
            trigger_by=trigger_by,
        )
        log_payload = {
            "strategy": self.strategy.name,
            "symbol": symbol,
            "purpose": purpose,
            "position_idx": position_idx,
            "side": side,
            "original_price": original_price,
            "base_price": base_price,
            "tick_size": tick_size,
            "source_price_field": source_price_field,
        }
        if original_price <= 0 or tick_size <= 0 or base_price is None or base_price <= 0:
            self.audit.log_event(
                "fixed_cycle_trading_stop_price_invalid_skipped",
                **log_payload,
                reason="missing_price_basis_or_tick_size",
            )
            return None

        requires_price_above_base = purpose_upper in {
            getattr(self.strategy, "LONG_TP_EXIT_PURPOSE", "LONG_TP_EXIT"),
            getattr(self.strategy, "SHORT_SL_EXIT_PURPOSE", "SHORT_SL_EXIT"),
        }
        if not requires_price_above_base or original_price > base_price:
            return original_price

        clamped_price = base_price + (2.0 * tick_size)
        normalized_price = self._normalize_runtime_price(clamped_price)
        attempts = 0
        while normalized_price <= base_price and attempts < 5:
            clamped_price += tick_size
            normalized_price = self._normalize_runtime_price(clamped_price)
            attempts += 1

        if normalized_price <= base_price:
            self.audit.log_event(
                "fixed_cycle_trading_stop_price_invalid_skipped",
                **log_payload,
                clamped_price=clamped_price,
                normalized_price=normalized_price,
                reason="clamp_still_invalid_after_normalization",
            )
            return None

        self.audit.log_event(
            "fixed_cycle_trading_stop_price_clamped",
            **log_payload,
            clamped_price=clamped_price,
            normalized_price=normalized_price,
            reason="violates_bybit_base_price_rule",
        )
        return normalized_price

    @staticmethod
    def _exchange_side(side: str, reduce_only: bool) -> str:
        if side == "long":
            return "Sell" if reduce_only else "Buy"
        return "Buy" if reduce_only else "Sell"

    def _start_websocket(self) -> None:
        if self.websocket_client is None:
            self.websocket_client = BybitWebSocketClient(self.config.api_key, self.config.secret_key)
        self.websocket_client.add_callback(self.handle_websocket_event)
        self.websocket_client.set_fill_callback(self.on_websocket_fill)
        self._ws_thread = threading.Thread(target=self.websocket_client.run, daemon=True)
        self._ws_thread.start()
        self.audit.log_event(
            "websocket_started",
            strategy=self.strategy.name,
            symbol=self.config.symbol,
            health_file=self.config.health_file,
        )

    def _start_price_loop(self) -> None:
        def poll() -> None:
            while not self._stop_event.wait(self.config.price_poll_interval_seconds):
                try:
                    self.process_tick()
                except Exception as exc:
                    self.audit.log_event("price_loop_error", strategy=self.strategy.name, error=str(exc))

        self._price_thread = threading.Thread(target=poll, daemon=True)
        self._price_thread.start()

    def _start_reconcile_loop(self) -> None:
        def reconcile() -> None:
            while not self._stop_event.wait(self.config.reconcile_interval_seconds):
                try:
                    self.reconcile_once()
                except Exception as exc:
                    self.audit.log_event("reconcile_loop_error", strategy=self.strategy.name, error=str(exc))

        self._reconcile_thread = threading.Thread(target=reconcile, daemon=True)
        self._reconcile_thread.start()

    def _sync_position_manager_from_ws(self, payload: dict[str, Any]) -> None:
        side = str(payload.get("side") or "").lower()
        size = float(payload.get("size") or 0.0)
        avg = float(payload.get("entryPrice") or 0.0)
        if side in {"buy", "long"}:
            self.position_manager.sync_positions(
                long_size=size,
                long_avg=avg,
                short_size=self.position_manager.short_size,
                short_avg=self.position_manager.short_avg,
            )
        elif side in {"sell", "short"}:
            self.position_manager.sync_positions(
                long_size=self.position_manager.long_size,
                long_avg=self.position_manager.long_avg,
                short_size=size,
                short_avg=avg,
            )
        self.audit.log_event(
            "position_ws_synced",
            strategy=self.strategy.name,
            long_size=self.position_manager.long_size,
            long_avg=self.position_manager.long_avg,
            short_size=self.position_manager.short_size,
            short_avg=self.position_manager.short_avg,
        )

    def _confirm_fill_via_rest(
        self,
        *,
        client_id: str,
        exchange_order_id: str,
        managed_order: ManagedOrder,
    ) -> tuple[bool, dict[str, Any] | None]:
        try:
            history_rows = self.order_manager.fetch_order_history(
                symbol=self.config.symbol,
                category=self.config.category,
                order_id=exchange_order_id or None,
                order_link_id=client_id,
                limit=5,
            ) or []
            open_rows = self.order_manager.fetch_open_orders(
                self.config.symbol, self.config.category
            ) or []
        except Exception as exc:
            self.audit.log_event(
                "fixed_cycle_rest_fill_confirmation_failed",
                strategy=self.strategy.name,
                client_order_id=client_id,
                exchange_order_id=exchange_order_id,
                purpose=managed_order.purpose,
                reason="rest_lookup_failed",
                error=str(exc),
            )
            return False, None

        history_match = None
        for row in history_rows:
            if not isinstance(row, dict):
                continue
            if exchange_order_id and str(row.get("orderId") or "") == str(exchange_order_id):
                history_match = row
                break
            if str(row.get("orderLinkId") or "") == str(client_id):
                history_match = row
                break

        open_match = None
        for row in open_rows:
            if not isinstance(row, dict):
                continue
            if exchange_order_id and str(row.get("orderId") or "") == str(exchange_order_id):
                open_match = row
                break
            if str(row.get("orderLinkId") or "") == str(client_id):
                open_match = row
                break

        history_status = self._normalize_order_status(
            (history_match or {}).get("orderStatus"),
            managed_order.status,
        )
        history_cumulative = self._safe_float(
            (history_match or {}).get("cumExecQty"),
            None,
        )
        order_qty = float(managed_order.qty or 0.0)
        history_is_terminal = self._is_terminal_order_status(history_status)
        rest_terminal = history_is_terminal or (
            history_cumulative is not None
            and history_cumulative >= order_qty - 1e-9
        )
        if open_match and not self._is_terminal_order_status(
            self._normalize_order_status((open_match or {}).get("orderStatus"), "OPEN")
        ):
            rest_terminal = False

        payload = {
            "history_status": history_status,
            "history_is_terminal": history_is_terminal,
            "history_cumulative_qty": history_cumulative,
            "open_order_present": bool(open_match),
            "order_qty": order_qty,
            "rest_terminal": rest_terminal,
            "history_match_present": bool(history_match),
        }
        return rest_terminal, payload

    def _pending_rest_fill_confirmations(self) -> dict[str, dict[str, Any]]:
        state = self.runtime_state.strategy_state
        pending = state.get("pending_rest_fill_confirmations")
        if not isinstance(pending, dict):
            pending = {}
            state["pending_rest_fill_confirmations"] = pending
        return pending

    def _schedule_pending_rest_fill_confirmation(
        self,
        *,
        client_id: str,
        exchange_order_id: str,
        managed_order: ManagedOrder,
        fill_event_data: dict[str, Any],
        last_error: str | None = None,
        initial_delay_seconds: float = 0.5,
    ) -> None:
        pending = self._pending_rest_fill_confirmations()
        now = time.monotonic()
        existing = dict(pending.get(client_id) or {})
        retry_count = int(existing.get("retry_count") or 0)
        first_seen_at = float(existing.get("first_seen_at") or now)
        entry = {
            "client_order_id": client_id,
            "exchange_order_id": exchange_order_id,
            "purpose": managed_order.purpose,
            "side": managed_order.side,
            "qty": float(managed_order.qty or 0.0),
            "filled_qty": float(managed_order.filled_qty or 0.0),
            "remaining_qty": float(managed_order.remaining_qty or 0.0),
            "status": str(managed_order.status or ""),
            "last_exec_id": fill_event_data.get("exec_id"),
            "retry_count": retry_count,
            "first_seen_at": first_seen_at,
            "next_retry_at": now + initial_delay_seconds,
            "last_error": last_error,
            "fill_event_data": fill_event_data,
            "exhausted_logged": bool(existing.get("exhausted_logged")),
        }
        pending[client_id] = entry
        self.audit.log_event(
            "fixed_cycle_pending_rest_fill_retry_scheduled",
            strategy=self.strategy.name,
            client_order_id=client_id,
            exchange_order_id=exchange_order_id,
            purpose=managed_order.purpose,
            retry_count=retry_count,
            next_retry_at=entry["next_retry_at"],
            last_error=last_error,
        )

    def _remove_pending_rest_fill_confirmation(self, client_id: str) -> None:
        pending = self._pending_rest_fill_confirmations()
        pending.pop(client_id, None)

    def _rest_fill_dispatch_registry(self) -> list[str]:
        state = self.runtime_state.strategy_state
        registry = state.get("rest_fill_dispatch_keys")
        if not isinstance(registry, list):
            registry = []
            state["rest_fill_dispatch_keys"] = registry
        return registry

    def _rest_fill_dispatch_key(self, client_id: str, exchange_order_id: str) -> str:
        return str(exchange_order_id or client_id or "")

    def _dispatch_rest_confirmed_fill(
        self,
        *,
        client_id: str,
        exchange_order_id: str,
        managed_order: ManagedOrder,
        fill_event_data: dict[str, Any],
        rest_payload: dict[str, Any],
        source: str,
    ) -> None:
        with self._lock:
            order_qty = float(managed_order.qty or 0.0)
            rest_cumulative = self._safe_float(rest_payload.get("history_cumulative_qty"), None)
            if rest_cumulative is not None:
                managed_order.filled_qty = min(order_qty, rest_cumulative)
            elif rest_payload.get("history_status") == "FILLED" and order_qty > 0:
                managed_order.filled_qty = max(float(managed_order.filled_qty or 0.0), order_qty)
            managed_order.remaining_qty = max(order_qty - float(managed_order.filled_qty or 0.0), 0.0)
            history_status = self._normalize_order_status(
                rest_payload.get("history_status"),
                managed_order.status,
            )
            managed_order.status = history_status
            fill_metadata = dict(fill_event_data.get("metadata") or managed_order.metadata or {})
            fill_event = FillEvent(
                exchange_order_id=exchange_order_id,
                client_order_id=client_id,
                side=str(fill_event_data.get("side") or managed_order.side),
                purpose=str(fill_event_data.get("purpose") or managed_order.purpose),
                exec_qty=float(fill_event_data.get("exec_qty") or managed_order.filled_qty or 0.0),
                exec_price=float(fill_event_data.get("exec_price") or 0.0),
                order_type=str(fill_event_data.get("order_type") or managed_order.order_type),
                reduce_only=bool(fill_event_data.get("reduce_only") if "reduce_only" in fill_event_data else managed_order.reduce_only),
                status=history_status,
                cumulative_qty=float(managed_order.filled_qty or 0.0),
                incremental_qty=float(fill_event_data.get("incremental_qty") or 0.0),
                exec_id=fill_event_data.get("exec_id"),
                metadata=fill_metadata,
                traces=list(fill_event_data.get("traces") or managed_order.trace),
            )
            dispatch_key = self._rest_fill_dispatch_key(client_id, exchange_order_id)
            dispatch_registry = self._rest_fill_dispatch_registry()
            should_dispatch_fill = (
                history_status == "FILLED"
                and float(managed_order.filled_qty or 0.0) > 0.0
            )
            self.audit.log_event(
                "fixed_cycle_rest_fill_confirmation_success",
                strategy=self.strategy.name,
                client_order_id=client_id,
                exchange_order_id=exchange_order_id,
                purpose=managed_order.purpose,
                confirmed_status=history_status,
                confirmed_cumulative_qty=rest_payload.get("history_cumulative_qty"),
                order_qty=managed_order.qty,
                source=source,
            )
            if should_dispatch_fill:
                self._mark_strategy_cycle_purpose_status(
                    purpose=managed_order.purpose,
                    metadata=fill_metadata,
                    status="FILLED",
                )
            self.audit.log_event(
                "fixed_cycle_filled_order_finalized_before_strategy_fill",
                strategy=self.strategy.name,
                client_order_id=client_id,
                exchange_order_id=exchange_order_id,
                purpose=managed_order.purpose,
                status=managed_order.status,
                source=source,
                filled_qty=managed_order.filled_qty,
                remaining_qty=managed_order.remaining_qty,
            )
            self._finalize_managed_order(client_id, managed_order)
        if not should_dispatch_fill:
            self.audit.log_event(
                "fixed_cycle_rest_terminal_without_fill_finalized",
                strategy=self.strategy.name,
                client_order_id=client_id,
                exchange_order_id=exchange_order_id,
                purpose=fill_event.purpose,
                confirmed_status=history_status,
                confirmed_cumulative_qty=rest_payload.get("history_cumulative_qty"),
            )
            self._remove_pending_rest_fill_confirmation(client_id)
            self._save_strategy_state()
            return
        if dispatch_key in dispatch_registry:
            self.audit.log_event(
                "fixed_cycle_rest_fill_duplicate_dispatch_blocked",
                strategy=self.strategy.name,
                client_order_id=client_id,
                exchange_order_id=exchange_order_id,
                purpose=fill_event.purpose,
                dispatch_key=dispatch_key,
            )
            self._remove_pending_rest_fill_confirmation(client_id)
            self._save_strategy_state()
            return
        dispatch_registry.append(dispatch_key)
        snapshot = self.refresh_snapshot("fill")
        self._dispatch("fill", self.strategy.on_fill(fill_event, snapshot, self.runtime_state, self.context), snapshot)
        self._remove_pending_rest_fill_confirmation(client_id)
        self._save_strategy_state()

    def _retry_pending_rest_fills(self) -> None:
        pending = self._pending_rest_fill_confirmations()
        if not pending:
            return
        now = time.monotonic()
        for client_id, entry in list(pending.items()):
            next_retry_at = float(entry.get("next_retry_at") or 0.0)
            if next_retry_at > now:
                continue
            managed_order = self.runtime_state.active_orders.get(client_id)
            if not managed_order:
                self._remove_pending_rest_fill_confirmation(client_id)
                continue
            exchange_order_id = str(entry.get("exchange_order_id") or managed_order.exchange_order_id or "")
            rest_confirmed, rest_payload = self._confirm_fill_via_rest(
                client_id=client_id,
                exchange_order_id=exchange_order_id,
                managed_order=managed_order,
            )
            if not rest_payload:
                entry["retry_count"] = int(entry.get("retry_count") or 0) + 1
                entry["last_error"] = "rest_lookup_failed"
                entry["next_retry_at"] = now + 1.0
                continue
            if rest_confirmed:
                self._dispatch_rest_confirmed_fill(
                    client_id=client_id,
                    exchange_order_id=exchange_order_id,
                    managed_order=managed_order,
                    fill_event_data=dict(entry.get("fill_event_data") or {}),
                    rest_payload=rest_payload,
                    source="rest_retry_confirmed",
                )
                continue
            entry["retry_count"] = int(entry.get("retry_count") or 0) + 1
            entry["last_error"] = "rest_not_terminal"
            elapsed = now - float(entry.get("first_seen_at") or now)
            if elapsed >= 15.0:
                if not bool(entry.get("exhausted_logged")):
                    self.audit.log_event(
                        "fixed_cycle_rest_fill_confirmation_retry_exhausted",
                        strategy=self.strategy.name,
                        client_order_id=client_id,
                        exchange_order_id=exchange_order_id,
                        purpose=managed_order.purpose,
                        retry_count=entry["retry_count"],
                        elapsed_seconds=elapsed,
                    )
                    entry["exhausted_logged"] = True
                entry["next_retry_at"] = now + 5.0
            else:
                entry["next_retry_at"] = now + 1.0
                self.audit.log_event(
                    "fixed_cycle_rest_fill_confirmation_retry",
                    strategy=self.strategy.name,
                    client_order_id=client_id,
                    exchange_order_id=exchange_order_id,
                    purpose=managed_order.purpose,
                    retry_count=entry["retry_count"],
                    next_retry_at=entry["next_retry_at"],
                    history_status=rest_payload.get("history_status"),
                    history_cumulative_qty=rest_payload.get("history_cumulative_qty"),
                )

    def _ingest_fill_event(
        self,
        *,
        exchange_order_id: str,
        client_id: str,
        qty: float,
        price: float,
        exec_id: str | None,
        cumulative_qty: float | None,
        source: str,
        order_link_id: str | None = None,
    ) -> None:
        managed_order = self.runtime_state.active_orders.get(client_id)
        if not managed_order:
            key = order_link_id or exchange_order_id
            pending_payload = {
                "exchange_order_id": exchange_order_id,
                "qty": qty,
                "price": price,
                "exec_id": exec_id,
                "cumulative_qty": cumulative_qty,
                "source": source,
                "order_link_id": order_link_id,
            }
            if key and self._store_pending_unmatched_fill(key, pending_payload):
                self.audit.log_event(
                    "fixed_cycle_pending_unmatched_fill_stored",
                    strategy=self.strategy.name,
                    order_link_id=order_link_id,
                    exchange_order_id=exchange_order_id,
                    exec_id=exec_id,
                    qty=qty,
                    price=price,
                )
                return
            processed_qty = self.runtime_state.processed_fill_cumulative.get(client_id, 0.0)
            if processed_qty > 0:
                self.audit.log_event(
                    "fill_duplicate_ignored",
                    strategy=self.strategy.name,
                    client_order_id=client_id,
                    exchange_order_id=exchange_order_id,
                    cumulative_qty=cumulative_qty,
                    processed_cumulative=processed_qty,
                    source=source,
                )
            return
        processed_qty = self.runtime_state.processed_fill_cumulative.get(client_id, 0.0)
        if cumulative_qty is not None and cumulative_qty <= processed_qty:
            return
        with self._lock:
            processed_exec_ids = set(managed_order.metadata.get("processed_exec_ids") or [])
            if exec_id and exec_id in processed_exec_ids:
                return
            if exec_id:
                processed_exec_ids.add(exec_id)
                managed_order.metadata["processed_exec_ids"] = sorted(processed_exec_ids)
            if processed_qty >= managed_order.qty and managed_order.qty > 0:
                return
            previous_filled = managed_order.filled_qty
            order_qty = float(managed_order.qty or 0.0)
            if cumulative_qty is not None and cumulative_qty > previous_filled:
                new_filled = min(order_qty, cumulative_qty)
                incremental_qty = new_filled - previous_filled
                managed_order.filled_qty = new_filled
            else:
                incremental_qty = qty
                managed_order.filled_qty = min(order_qty, previous_filled + qty)
            managed_order.remaining_qty = max(order_qty - managed_order.filled_qty, 0.0)
            managed_order.updated_at = utcnow()
            remaining_qty = managed_order.remaining_qty
            should_finalize = False
            if (
                remaining_qty <= 1e-9
                and (cumulative_qty is not None or managed_order.filled_qty >= order_qty - 1e-9)
            ):
                managed_order.status = "FILLED"
                should_finalize = True
            else:
                managed_order.status = "PARTIAL"
            entry_price = float(
                managed_order.metadata.get("entry_price")
                or (
                    self.runtime_state.last_snapshot.long_avg
                    if managed_order.side == "long" and self.runtime_state.last_snapshot
                    else self.runtime_state.last_snapshot.short_avg
                    if self.runtime_state.last_snapshot
                    else 0.0
                )
            )
            calculated_pnl = 0.0
            runtime_gross_pnl = None
            runtime_entry_fee = None
            runtime_exit_fee = None
            runtime_fee_rate = None
            pnl_calc_source = "runtime_calculate_pnl"
            if managed_order.reduce_only and incremental_qty > 0 and entry_price > 0:
                qty_float = float(incremental_qty)
                price_value = float(price)
                fee_rate = float(getattr(self.strategy.config, "order_fee_rate_pct", 0.0)) / 100.0
                if qty_float > 0:
                    if managed_order.side.lower() == "long":
                        gross_pnl = (price_value - entry_price) * qty_float
                    else:
                        gross_pnl = (entry_price - price_value) * qty_float
                    entry_fee = abs(entry_price * qty_float) * fee_rate
                    exit_fee = abs(price_value * qty_float) * fee_rate
                    calculated_pnl = gross_pnl - entry_fee - exit_fee
                    runtime_gross_pnl = gross_pnl
                    runtime_entry_fee = entry_fee
                    runtime_exit_fee = exit_fee
                    runtime_fee_rate = fee_rate
                    pnl_calc_source = "runtime_calculate_pnl_with_fees"
                else:
                    calculated_pnl = calculate_pnl(entry_price, price_value, incremental_qty, managed_order.side)
                pnl = calculated_pnl
                if managed_order.side == "long":
                    self.runtime_state.realized_long_pnl_total += pnl
                    self.runtime_state.temporary_pnl_by_order[client_id] = (
                        self.runtime_state.temporary_pnl_by_order.get(client_id, 0.0) + pnl
                    )
                else:
                    self.runtime_state.realized_short_pnl_total += pnl
                    self.runtime_state.temporary_pnl_by_order[client_id] = (
                        self.runtime_state.temporary_pnl_by_order.get(client_id, 0.0) + pnl
                    )
            elif managed_order.reduce_only:
                calculated_pnl = calculate_pnl(entry_price, price, incremental_qty, managed_order.side)
                pnl = calculated_pnl
                if managed_order.side == "long":
                    self.runtime_state.realized_long_pnl_total += pnl
                    self.runtime_state.temporary_pnl_by_order[client_id] = (
                        self.runtime_state.temporary_pnl_by_order.get(client_id, 0.0) + pnl
                    )
                else:
                    self.runtime_state.realized_short_pnl_total += pnl
                    self.runtime_state.temporary_pnl_by_order[client_id] = (
                        self.runtime_state.temporary_pnl_by_order.get(client_id, 0.0) + pnl
                    )
            else:
                pnl = 0.0
            fill_metadata = {
                **dict(managed_order.metadata),
                "fill_source": source,
            }
            if managed_order.reduce_only:
                fill_metadata.update(
                    {
                        "runtime_calculated_pnl": calculated_pnl,
                        "exec_pnl": calculated_pnl,
                        "entry_price_for_pnl": entry_price,
                        "pnl_calc_source": pnl_calc_source,
                        "runtime_gross_pnl": runtime_gross_pnl,
                        "runtime_entry_fee": runtime_entry_fee,
                        "runtime_exit_fee": runtime_exit_fee,
                        "runtime_fee_rate": runtime_fee_rate,
                    }
                )
                if runtime_gross_pnl is not None:
                    self.audit.log_event(
                        "fixed_cycle_runtime_pnl_calculated_with_fees",
                        strategy=self.strategy.name,
                        purpose=managed_order.purpose,
                        cycle_role=managed_order.metadata.get("cycle_role"),
                        entry_price=entry_price,
                        exit_price=price,
                        qty=qty,
                        gross_pnl=runtime_gross_pnl,
                        entry_fee=runtime_entry_fee,
                        exit_fee=runtime_exit_fee,
                        net_pnl=calculated_pnl,
                        fee_rate=runtime_fee_rate,
                    )
            fill_event = FillEvent(
                exchange_order_id=exchange_order_id,
                client_order_id=client_id,
                side=managed_order.side,
                purpose=managed_order.purpose,
                exec_qty=qty,
                exec_price=price,
                order_type=managed_order.order_type,
                reduce_only=managed_order.reduce_only,
                status=managed_order.status,
                cumulative_qty=cumulative_qty,
                incremental_qty=incremental_qty,
                exec_id=exec_id,
                metadata=fill_metadata,
                traces=list(managed_order.trace),
            )
        self.runtime_state.processed_fill_cumulative[client_id] = max(
            processed_qty, managed_order.filled_qty
        )
        with self._lock:
            if managed_order.reduce_only:
                self.audit.log_event(
                    "fill_runtime_calculated_pnl_attached",
                    strategy=self.strategy.name,
                    client_order_id=client_id,
                    exchange_order_id=exchange_order_id,
                    purpose=managed_order.purpose,
                    side=managed_order.side,
                    reduce_only=managed_order.reduce_only,
                    exec_qty=qty,
                    incremental_qty=incremental_qty,
                    exec_price=price,
                    entry_price_for_pnl=entry_price,
                    runtime_calculated_pnl=calculated_pnl,
                    source=source,
                )
            self.audit.log_event(
                "fill_received",
                strategy=self.strategy.name,
                source=source,
                fill=fill_event.to_dict(),
            )
            if managed_order.status != "FILLED":
                self.audit.log_event(
                    "fixed_cycle_partial_execution_accumulated",
                    strategy=self.strategy.name,
                    purpose=managed_order.purpose,
                    client_order_id=client_id,
                    exchange_order_id=exchange_order_id,
                    incremental_qty=incremental_qty,
                    filled_qty=managed_order.filled_qty,
                    order_qty=order_qty,
                    remaining_qty=managed_order.remaining_qty,
                )
        rest_confirmed, rest_payload = self._confirm_fill_via_rest(
            client_id=client_id,
            exchange_order_id=exchange_order_id,
            managed_order=managed_order,
        )
        if not rest_payload:
            self._schedule_pending_rest_fill_confirmation(
                client_id=client_id,
                exchange_order_id=exchange_order_id,
                managed_order=managed_order,
                fill_event_data=fill_event.to_dict(),
                last_error="rest_lookup_failed",
                initial_delay_seconds=0.5,
            )
            self._save_strategy_state()
            return
        if not rest_confirmed:
            self.audit.log_event(
                "fixed_cycle_ws_fill_deferred_until_rest_terminal",
                strategy=self.strategy.name,
                client_order_id=client_id,
                exchange_order_id=exchange_order_id,
                purpose=managed_order.purpose,
                ws_status=managed_order.status,
                filled_qty=managed_order.filled_qty,
                remaining_qty=managed_order.remaining_qty,
                history_status=rest_payload.get("history_status"),
                history_cumulative_qty=rest_payload.get("history_cumulative_qty"),
                open_order_present=rest_payload.get("open_order_present"),
            )
            self._schedule_pending_rest_fill_confirmation(
                client_id=client_id,
                exchange_order_id=exchange_order_id,
                managed_order=managed_order,
                fill_event_data=fill_event.to_dict(),
                last_error="rest_not_terminal",
                initial_delay_seconds=0.5,
            )
            self._save_strategy_state()
            return
        self._dispatch_rest_confirmed_fill(
            client_id=client_id,
            exchange_order_id=exchange_order_id,
            managed_order=managed_order,
            fill_event_data=fill_event.to_dict(),
            rest_payload=rest_payload,
            source=f"{source}_rest_confirmed",
        )

    def _reconcile_active_orders(self) -> None:
        if not self.runtime_state.active_orders:
            self.audit.log_event(
                "reconcile_skipped",
                strategy=self.strategy.name,
                reason="no_active_orders",
            )
            return
        try:
            raw_open_orders = self.order_manager.fetch_open_orders(
                self.config.symbol, self.config.category
            ) or []
        except ExchangeUnavailableError as exc:
            error_info = compact_exchange_error(exc.original_exception)
            self._log_reconcile_exchange_unavailable(
                client_order_id=None,
                exchange_order_id=None,
                purpose=None,
                current_status=None,
                endpoint_failed="open_orders",
                error=error_info,
            )
            return
        open_orders: list[dict[str, Any]] = []
        for raw_order in raw_open_orders:
            if not isinstance(raw_order, dict):
                self.audit.log_event(
                    "reconcile_order_skip_invalid_payload",
                    strategy=self.strategy.name,
                    payload_type=type(raw_order).__name__,
                    payload_repr=repr(raw_order),
                    source="open_orders",
                )
                continue
            open_orders.append(raw_order)
        open_by_exchange_id = {
            str(order.get("orderId")): order for order in open_orders if order.get("orderId")
        }
        open_by_link_id = {
            str(order.get("orderLinkId")): order for order in open_orders if order.get("orderLinkId")
        }
        for client_id, managed_order in list(self.runtime_state.active_orders.items()):
            metadata = getattr(managed_order, "metadata", None) or {}
            if metadata.get("trading_stop_api"):
                # Trading-Stop-Exit-Orders (LONG_TP_EXIT/SHORT_SL_EXIT) werden positionsgebunden
                # über /v5/position/trading-stop gesetzt und besitzen keine klassische
                # exchange_order_id. Sie dürfen daher nicht über open_orders/order_history
                # reconciled werden.
                continue
            if self._is_terminal_order_status(managed_order.status):
                self._normalize_terminal_order_quantities(managed_order)
                self.audit.log_event(
                    "reconcile_terminal_order_skip_stale_fill_inference",
                    strategy=self.strategy.name,
                    client_order_id=client_id,
                    exchange_order_id=managed_order.exchange_order_id,
                    managed_order=self._managed_order_summary(managed_order),
                    history_order=None,
                    normalized_history_status=managed_order.status,
                )
                self.audit.log_event(
                    "fixed_cycle_reconcile_terminal_order_ignored",
                    strategy=self.strategy.name,
                    client_order_id=client_id,
                    exchange_order_id=managed_order.exchange_order_id,
                    purpose=managed_order.purpose,
                    status=managed_order.status,
                    source="runtime_active_orders",
                )
                self._finalize_managed_order(client_id, managed_order)
                continue
            open_match = open_by_exchange_id.get(managed_order.exchange_order_id or "") or open_by_link_id.get(client_id)
            if open_match:
                previous_filled_qty = managed_order.filled_qty
                managed_order.status = self._normalize_order_status(open_match.get("orderStatus"), "OPEN")
                managed_order.updated_at = utcnow()
                managed_order.filled_qty = float(open_match.get("cumExecQty") or managed_order.filled_qty or 0.0)
                managed_order.remaining_qty = max(managed_order.qty - managed_order.filled_qty, 0.0)
                if self._is_terminal_order_status(managed_order.status):
                    self.audit.log_event(
                        "fixed_cycle_reconcile_terminal_order_ignored",
                        strategy=self.strategy.name,
                        client_order_id=client_id,
                        exchange_order_id=managed_order.exchange_order_id,
                        purpose=managed_order.purpose,
                        status=managed_order.status,
                        source="open_orders",
                    )
                    if managed_order.status == "FILLED":
                        self._mark_strategy_cycle_purpose_status(
                            purpose=managed_order.purpose,
                            metadata=managed_order.metadata,
                            status="FILLED",
                        )
                        snapshot = self.runtime_state.last_snapshot
                        self.strategy._mark_initial_entry_reconciled_from_terminal_order(
                            self.runtime_state,
                            snapshot,
                            purpose=managed_order.purpose,
                            exchange_order_id=managed_order.exchange_order_id,
                            client_order_id=managed_order.client_order_id,
                            source="terminal_fill_reconcile",
                        )
                    self._finalize_managed_order(client_id, managed_order)
                    continue
                self.audit.log_event(
                    "order_reconciled_open",
                    strategy=self.strategy.name,
                    client_order_id=client_id,
                    exchange_order_id=managed_order.exchange_order_id,
                    managed_order=self._managed_order_summary(managed_order),
                    exchange_order=self._exchange_order_summary(open_match),
                    previous_filled_qty=previous_filled_qty,
                    status=managed_order.status,
                    filled_qty=managed_order.filled_qty,
                    remaining_qty=managed_order.remaining_qty,
                    reconcile_source="open_orders",
                )
                continue
            self.audit.log_event(
                "reconcile_open_order_miss",
                strategy=self.strategy.name,
                client_order_id=client_id,
                exchange_order_id=managed_order.exchange_order_id,
                managed_order=self._managed_order_summary(managed_order),
            )
            try:
                history = self.order_manager.fetch_order_history(
                    self.config.symbol,
                    self.config.category,
                    order_id=managed_order.exchange_order_id,
                    order_link_id=client_id,
                    limit=1,
                ) or []
            except ExchangeUnavailableError as exc:
                error_info = compact_exchange_error(exc.original_exception)
                self._log_reconcile_exchange_unavailable(
                    client_order_id=client_id,
                    exchange_order_id=managed_order.exchange_order_id,
                    purpose=managed_order.purpose,
                    current_status=managed_order.status,
                    endpoint_failed="order_history",
                    error=error_info,
                )
                continue
            if not history:
                self.audit.log_event(
                    "reconcile_history_miss",
                    strategy=self.strategy.name,
                    client_order_id=client_id,
                    exchange_order_id=managed_order.exchange_order_id,
                    managed_order=self._managed_order_summary(managed_order),
                )
                continue
            history_order = history[0]
            if not isinstance(history_order, dict):
                self.audit.log_event(
                    "reconcile_order_skip_invalid_payload",
                    strategy=self.strategy.name,
                    payload_type=type(history_order).__name__,
                    payload_repr=repr(history_order),
                    source="history",
                    client_order_id=client_id,
                    exchange_order_id=managed_order.exchange_order_id,
                )
                continue
            normalized_history_status = self._normalize_order_status(history_order.get("orderStatus"), managed_order.status)
            avg_fill_price = self._history_fill_price(history_order, managed_order.price or 0.0)
            cumulative_qty = float(history_order.get("cumExecQty") or 0.0)
            self.audit.log_event(
                "reconcile_history_found",
                strategy=self.strategy.name,
                client_order_id=client_id,
                exchange_order_id=managed_order.exchange_order_id,
                managed_order=self._managed_order_summary(managed_order),
                history_order=self._history_order_summary(history_order),
                normalized_history_status=normalized_history_status,
                inferred_fill_price=avg_fill_price,
                cumulative_qty=cumulative_qty,
            )
            if self._is_terminal_order_status(managed_order.status):
                self._normalize_terminal_order_quantities(managed_order)
                self.audit.log_event(
                    "reconcile_terminal_order_skip_stale_fill_inference",
                    strategy=self.strategy.name,
                    client_order_id=client_id,
                    exchange_order_id=managed_order.exchange_order_id,
                    managed_order=self._managed_order_summary(managed_order),
                    history_order=self._history_order_summary(history_order),
                    normalized_history_status=normalized_history_status,
                )
                continue
            if normalized_history_status in {"FILLED", "PARTIAL"} and cumulative_qty > managed_order.filled_qty:
                exec_price = avg_fill_price if avg_fill_price > 0 else float(managed_order.price or 0.0)
                incremental_qty = max(cumulative_qty - managed_order.filled_qty, 0.0)
                self.audit.log_event(
                    "reconcile_fill_inferred",
                    strategy=self.strategy.name,
                    client_order_id=client_id,
                    exchange_order_id=managed_order.exchange_order_id,
                    inferred_status=normalized_history_status,
                    incremental_qty=incremental_qty,
                    cumulative_qty=cumulative_qty,
                    exec_price=exec_price,
                    history_order=self._history_order_summary(history_order),
                )
                self._ingest_fill_event(
                    exchange_order_id=managed_order.exchange_order_id or client_id,
                    client_id=client_id,
                    qty=incremental_qty,
                    price=exec_price,
                    exec_id=f"reconcile-{client_id}-{cumulative_qty}",
                    cumulative_qty=cumulative_qty,
                    source="reconcile",
                )
                managed_order = self.runtime_state.active_orders.get(client_id)
                if not managed_order:
                    continue
            if normalized_history_status == "FILLED" and cumulative_qty <= managed_order.filled_qty:
                managed_order.status = "FILLED"
                managed_order.updated_at = utcnow()
                self._mark_strategy_cycle_purpose_status(
                    purpose=managed_order.purpose,
                    metadata=managed_order.metadata,
                    status="FILLED",
                )
                snapshot = self.runtime_state.last_snapshot
                self.strategy._mark_initial_entry_reconciled_from_terminal_order(
                    self.runtime_state,
                    snapshot,
                    purpose=managed_order.purpose,
                    exchange_order_id=managed_order.exchange_order_id,
                    client_order_id=managed_order.client_order_id,
                    source="history_terminal_filled",
                )
                self._finalize_managed_order(client_id, managed_order)
                self.audit.log_event(
                    "fixed_cycle_reconcile_terminal_order_ignored",
                    strategy=self.strategy.name,
                    client_order_id=client_id,
                    exchange_order_id=managed_order.exchange_order_id,
                    purpose=managed_order.purpose,
                    status=normalized_history_status,
                    source="history_terminal_filled",
                )
                continue
            if normalized_history_status in {"CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}:
                managed_order.status = normalized_history_status
                managed_order.updated_at = utcnow()
                self._finalize_managed_order(client_id, managed_order)
                self.audit.log_event(
                    "order_reconciled_terminal",
                    strategy=self.strategy.name,
                    client_order_id=client_id,
                    exchange_order_id=managed_order.exchange_order_id,
                    history_order=self._history_order_summary(history_order),
                    managed_order=self._managed_order_summary(managed_order),
                    status=normalized_history_status,
                )
                self._dispatch_reconcile_terminal_cancel(
                    client_id,
                    managed_order,
                    history_order,
                    normalized_history_status,
                )
                continue
            if normalized_history_status == "PARTIAL":
                managed_order.status = "PARTIAL"
                managed_order.updated_at = utcnow()
                managed_order.filled_qty = max(managed_order.filled_qty, cumulative_qty)
                managed_order.remaining_qty = max(managed_order.qty - managed_order.filled_qty, 0.0)
                self.audit.log_event(
                    "order_reconciled_partial",
                    strategy=self.strategy.name,
                    client_order_id=client_id,
                    exchange_order_id=managed_order.exchange_order_id,
                    history_order=self._history_order_summary(history_order),
                    managed_order=self._managed_order_summary(managed_order),
                    status=normalized_history_status,
                    filled_qty=managed_order.filled_qty,
                    remaining_qty=managed_order.remaining_qty,
                )
        self._save_strategy_state()

    def _log_reconcile_exchange_unavailable(
        self,
        *,
        client_order_id: str | None,
        exchange_order_id: str | None,
        purpose: str | None,
        current_status: str | None,
        endpoint_failed: str,
        error: dict[str, Any],
    ) -> None:
        event_key = f"reconcile_exchange_unavailable:{endpoint_failed}:{client_order_id or 'n/a'}:{error.get('error_class')}"
        payload = {
            "client_order_id": client_order_id,
            "exchange_order_id": exchange_order_id,
            "purpose": purpose,
            "current_status": current_status,
            "endpoint_failed": endpoint_failed,
            "error_class": error.get("error_class"),
            "error_message": error.get("error_message"),
            "action": "keep_runtime_order_open",
        }
        if self._should_log_idle_event(event_key, payload, interval_seconds=120.0):
            self.audit.log_event("reconcile_exchange_unavailable", strategy=self.strategy.name, **payload)

    @staticmethod
    def _history_fill_price(history_order: dict[str, Any], fallback_price: float) -> float:
        avg_price = history_order.get("avgPrice")
        if avg_price not in (None, "", "0", 0):
            return float(avg_price)
        cum_exec_value = history_order.get("cumExecValue")
        cum_exec_qty = history_order.get("cumExecQty")
        try:
            if cum_exec_value not in (None, "", "0", 0) and cum_exec_qty not in (None, "", "0", 0):
                return float(cum_exec_value) / float(cum_exec_qty)
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    def _store_pending_unmatched_fill(
        self, key: str, payload: dict[str, Any]
    ) -> bool:
        with self._lock:
            pending = self.runtime_state.pending_unmatched_fills.setdefault(key, [])
            pending.append(payload)
        return True

    def _process_pending_unmatched_fills(
        self, client_id: str, exchange_order_id: str | None
    ) -> None:
        keys = {client_id}
        if exchange_order_id:
            keys.add(exchange_order_id)
        for key in keys:
            with self._lock:
                pending = self.runtime_state.pending_unmatched_fills.pop(key, [])
            for fill_data in pending:
                fill_payload = dict(fill_data)
                fill_payload["client_id"] = client_id
                self._ingest_fill_event(**fill_payload)

    def _infer_fixed_cycle_unmatched_fill(
        self,
        order_link_id: str | None,
    ) -> dict[str, Any] | None:
        if not order_link_id:
            return None
        normalized = str(order_link_id).lower()
        prefix = "fixed_cycle-"
        if not normalized.startswith(prefix):
            return None
        candidate = normalized[len(prefix) :]
        cycle_match = re.match(r"cycle_(\d+)_(.+)", candidate)
        if cycle_match:
            cycle_index = int(cycle_match.group(1))
            role_segment = cycle_match.group(2)
            purpose = f"CYCLE_{cycle_index}_{role_segment.upper()}"
            cycle_role = "long_reduce" if "long" in role_segment else "short_reduce"
            metadata = {
                "cycle_index": cycle_index,
                "cycle_role": cycle_role,
            }
            return {
                "purpose": purpose,
                "side": "long" if "long" in role_segment else "short",
                "reduce_only": True,
                "metadata": metadata,
                "inferred_cycle_index": cycle_index,
                "inferred_cycle_role": cycle_role,
            }
        if candidate.startswith("short_sl_exit"):
            return {
                "purpose": self.strategy.SHORT_SL_EXIT_PURPOSE,
                "side": "short",
                "reduce_only": True,
                "metadata": {"exit_type": "short_sl", "exit_mode": "basket_exit"},
                "inferred_cycle_index": None,
                "inferred_cycle_role": "short_sl_exit",
            }
        if candidate.startswith("long_tp_exit"):
            return {
                "purpose": self.strategy.LONG_TP_EXIT_PURPOSE,
                "side": "long",
                "reduce_only": True,
                "metadata": {"exit_type": "long_tp", "exit_mode": "basket_exit"},
                "inferred_cycle_index": None,
                "inferred_cycle_role": "long_tp_exit",
            }
        return None

    def _load_strategy_state(self) -> None:
        if not self.config.strategy_state_file:
            return
        path = Path(self.config.strategy_state_file)
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.audit.log_event("strategy_state_load_failed", strategy=self.strategy.name, path=str(path))
            return
        if not isinstance(payload, dict):
            return
        self.runtime_state.strategy_state = dict(payload.get("strategy_state") or {})
        self.runtime_state.realized_long_pnl_total = float(payload.get("realized_long_pnl_total") or 0.0)
        self.runtime_state.realized_short_pnl_total = float(payload.get("realized_short_pnl_total") or 0.0)
        self.runtime_state.sequence = int(payload.get("sequence") or 0)
        restored_active_orders = payload.get("active_orders") or []
        if isinstance(restored_active_orders, list):
            for item in restored_active_orders:
                order = self._managed_order_from_dict(item)
                if not order:
                    continue
                self.runtime_state.active_orders[order.client_order_id] = order
                if order.exchange_order_id:
                    self.runtime_state.exchange_to_client_id[order.exchange_order_id] = order.client_order_id
        self.audit.log_event(
            "strategy_state_loaded",
            strategy=self.strategy.name,
            path=str(path),
            strategy_state=self.runtime_state.strategy_state,
            realized_long_pnl_total=self.runtime_state.realized_long_pnl_total,
            realized_short_pnl_total=self.runtime_state.realized_short_pnl_total,
            sequence=self.runtime_state.sequence,
            restored_active_order_count=len(self.runtime_state.active_orders),
        )

    def _save_strategy_state(self) -> None:
        if not self.config.strategy_state_file:
            return
        path = Path(self.config.strategy_state_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "strategy": self.strategy.name,
            "symbol": self.config.symbol,
            "category": self.config.category,
            "strategy_state": self.runtime_state.strategy_state,
            "realized_long_pnl_total": self.runtime_state.realized_long_pnl_total,
            "realized_short_pnl_total": self.runtime_state.realized_short_pnl_total,
            "sequence": self.runtime_state.sequence,
            "active_orders": [
                self._managed_order_to_dict(order)
                for order in self.runtime_state.active_orders.values()
                if not self._is_terminal_order_status(order.status)
            ],
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def _recover_active_orders_from_exchange(self) -> None:
        open_orders = self.order_manager.fetch_open_orders(self.config.symbol, self.config.category) or []
        if not open_orders and not self.runtime_state.active_orders:
            return
        recovered_count = 0
        matched_client_ids: set[str] = set()
        for exchange_order in open_orders:
            exchange_order_id = str(exchange_order.get("orderId") or "")
            recovery_source = "exchange_order_link_id"
            client_id = str(
                exchange_order.get("orderLinkId")
                or self.runtime_state.exchange_to_client_id.get(exchange_order_id)
                or ""
            )
            if not exchange_order.get("orderLinkId") and client_id:
                recovery_source = "exchange_order_id_mapping"
            if not client_id:
                client_id = self._match_existing_active_order(exchange_order, matched_client_ids) or ""
                if client_id:
                    recovery_source = "persisted_order_match"
            if not client_id:
                recovered_purpose = self._classify_unknown_order_purpose(exchange_order)
                client_id = (
                    f"recovered-{self.strategy.name}--{recovered_purpose.lower()}--{exchange_order_id or uuid4().hex[:8]}"
                )
                recovery_source = "heuristic_classification"
                self.audit.log_event(
                    "startup_order_recovery_classified",
                    strategy=self.strategy.name,
                    exchange_order=self._exchange_order_summary(exchange_order),
                    classified_purpose=recovered_purpose,
                    classified_client_order_id=client_id,
                    classification_inputs=self._classification_inputs(exchange_order),
                )
            existing = self.runtime_state.active_orders.get(client_id)
            recovered = self._recover_managed_order(client_id, exchange_order, existing)
            if existing and existing.exchange_order_id and existing.exchange_order_id != recovered.exchange_order_id:
                self.runtime_state.exchange_to_client_id.pop(existing.exchange_order_id, None)
            self.runtime_state.active_orders[client_id] = recovered
            if recovered.exchange_order_id:
                self.runtime_state.exchange_to_client_id[recovered.exchange_order_id] = client_id
            self.audit.log_event(
                "startup_order_recovery_attached",
                strategy=self.strategy.name,
                recovery_source=recovery_source,
                exchange_order=self._exchange_order_summary(exchange_order),
                client_order_id=client_id,
                purpose=recovered.purpose,
                side=recovered.side,
                reduce_only=recovered.reduce_only,
                order_type=recovered.order_type,
                had_existing_state=existing is not None,
            )
            matched_client_ids.add(client_id)
            recovered_count += 1

        for client_id, order in list(self.runtime_state.active_orders.items()):
            if self._is_terminal_order_status(order.status):
                self.runtime_state.active_orders.pop(client_id, None)
                if order.exchange_order_id:
                    self.runtime_state.exchange_to_client_id.pop(order.exchange_order_id, None)
                self._clear_pending_final_exit_submission(
                    client_order_id=client_id,
                    purpose=order.purpose,
                )
                continue
            has_open_match = any(
                (
                    str(item.get("orderId") or "") == str(order.exchange_order_id or "")
                    or str(item.get("orderLinkId") or "") == client_id
                )
                for item in open_orders
            )
            if not has_open_match:
                self.runtime_state.active_orders.pop(client_id, None)
                if order.exchange_order_id:
                    self.runtime_state.exchange_to_client_id.pop(order.exchange_order_id, None)
                self._clear_pending_final_exit_submission(
                    client_order_id=client_id,
                    purpose=order.purpose,
                )
                self.audit.log_event(
                    "startup_order_recovery_pruned_stale_order",
                    strategy=self.strategy.name,
                    client_order_id=client_id,
                    exchange_order_id=order.exchange_order_id,
                    purpose=order.purpose,
                    side=order.side,
                    status=order.status,
                )
        self.audit.log_event(
            "startup_order_recovery_completed",
            strategy=self.strategy.name,
            recovered_order_count=recovered_count,
            active_order_count=len(self.runtime_state.active_orders),
        )

    def _match_existing_active_order(
        self,
        exchange_order: dict[str, Any],
        matched_client_ids: set[str],
    ) -> str | None:
        candidates: list[tuple[int, str, dict[str, Any]]] = []
        for client_id, existing in self.runtime_state.active_orders.items():
            if client_id in matched_client_ids:
                continue
            if self._is_terminal_order_status(existing.status):
                continue
            score, details = self._score_recovery_match(existing, exchange_order)
            if score > 0:
                candidates.append((score, client_id, details))
        if not candidates:
            self.audit.log_event(
                "startup_order_recovery_match_skipped",
                strategy=self.strategy.name,
                reason="no_candidates",
                exchange_order=self._exchange_order_summary(exchange_order),
            )
            return None
        candidates.sort(reverse=True)
        best_score, best_client_id, best_details = candidates[0]
        second_best_score = candidates[1][0] if len(candidates) > 1 else -1
        if best_score < 80:
            self.audit.log_event(
                "startup_order_recovery_match_skipped",
                strategy=self.strategy.name,
                reason="score_below_threshold",
                threshold=80,
                best_match_client_order_id=best_client_id,
                best_match_score=best_score,
                best_match_details=best_details,
                candidate_scores=[
                    {
                        "client_order_id": candidate_client_id,
                        "score": candidate_score,
                        "details": candidate_details,
                    }
                    for candidate_score, candidate_client_id, candidate_details in candidates[:5]
                ],
                exchange_order=self._exchange_order_summary(exchange_order),
            )
            return None
        if best_score == second_best_score:
            self.audit.log_event(
                "startup_order_recovery_match_skipped",
                strategy=self.strategy.name,
                reason="ambiguous_candidates",
                best_match_score=best_score,
                candidate_scores=[
                    {
                        "client_order_id": candidate_client_id,
                        "score": candidate_score,
                        "details": candidate_details,
                    }
                    for candidate_score, candidate_client_id, candidate_details in candidates[:5]
                ],
                exchange_order=self._exchange_order_summary(exchange_order),
            )
            return None
        self.audit.log_event(
            "startup_order_recovery_matched_existing",
            strategy=self.strategy.name,
            matched_client_order_id=best_client_id,
            match_score=best_score,
            match_details=best_details,
            candidate_scores=[
                {
                    "client_order_id": candidate_client_id,
                    "score": candidate_score,
                    "details": candidate_details,
                }
                for candidate_score, candidate_client_id, candidate_details in candidates[:5]
            ],
            exchange_order=self._exchange_order_summary(exchange_order),
        )
        return best_client_id

    def _score_recovery_match(self, existing: ManagedOrder, exchange_order: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        exchange_order_id = str(exchange_order.get("orderId") or "")
        exchange_side = self._runtime_side_from_exchange(exchange_order)
        exchange_reduce_only = bool(exchange_order.get("reduceOnly"))
        exchange_order_type = str(exchange_order.get("orderType") or existing.order_type or "")
        exchange_qty = self._safe_float(exchange_order.get("qty"), None)
        exchange_price = self._safe_float(exchange_order.get("price"), None)

        score = 0
        details: dict[str, Any] = {
            "existing_exchange_order_id": existing.exchange_order_id,
            "existing_side": existing.side,
            "exchange_side": exchange_side,
            "existing_reduce_only": existing.reduce_only,
            "exchange_reduce_only": exchange_reduce_only,
            "existing_order_type": existing.order_type,
            "exchange_order_type": exchange_order_type,
            "existing_qty": existing.qty,
            "exchange_qty": exchange_qty,
            "existing_price": existing.price,
            "exchange_price": exchange_price,
        }
        if exchange_order_id and existing.exchange_order_id and exchange_order_id == existing.exchange_order_id:
            score += 1000
            details["matched_exchange_order_id"] = True
        if existing.side == exchange_side:
            score += 30
        else:
            details["rejected_reason"] = "side_mismatch"
            return 0, details
        if existing.reduce_only == exchange_reduce_only:
            score += 20
        else:
            details["rejected_reason"] = "reduce_only_mismatch"
            return 0, details
        if str(existing.order_type or "").lower() == str(exchange_order_type or "").lower():
            score += 15
        if exchange_qty is not None:
            if abs(existing.qty - exchange_qty) <= 1e-9:
                score += 25
            elif existing.qty > 0:
                relative_diff = abs(existing.qty - exchange_qty) / existing.qty
                if relative_diff <= 0.01:
                    score += 15
                elif relative_diff <= 0.05:
                    score += 5
                else:
                    details["rejected_reason"] = "qty_mismatch"
                    details["qty_relative_diff"] = relative_diff
                    return 0, details
        if existing.order_type == "Limit":
            if existing.price is None or exchange_price is None:
                details["rejected_reason"] = "missing_limit_price"
                return 0, details
            if abs(existing.price - exchange_price) <= 1e-9:
                score += 25
            elif existing.price and abs(existing.price - exchange_price) / abs(existing.price) <= 0.001:
                score += 15
            else:
                details["rejected_reason"] = "price_mismatch"
                return 0, details
        details["final_score"] = score
        return score, details

    def _recover_managed_order(
        self,
        client_id: str,
        exchange_order: dict[str, Any],
        existing: ManagedOrder | None,
    ) -> ManagedOrder:
        side = self._runtime_side_from_exchange(exchange_order)
        reduce_only = bool(exchange_order.get("reduceOnly") or (existing.reduce_only if existing else False))
        order_type = str(exchange_order.get("orderType") or (existing.order_type if existing else "Market"))
        qty = float(exchange_order.get("qty") or (existing.qty if existing else 0.0))
        price = self._safe_float(exchange_order.get("price"), existing.price if existing else None)
        recovered_purpose = self._recover_purpose_from_client_id(client_id)
        purpose = (
            existing.purpose
            if existing
            else recovered_purpose
            if recovered_purpose != "RECOVERED_ORDER"
            else self._classify_unknown_order_purpose(exchange_order)
        )
        status = self._normalize_order_status(exchange_order.get("orderStatus"), existing.status if existing else "OPEN")
        filled_qty = float(exchange_order.get("cumExecQty") or (existing.filled_qty if existing else 0.0))
        remaining_qty = max(qty - filled_qty, 0.0)
        metadata = dict(existing.metadata) if existing else {}
        metadata = self._recover_split_metadata_from_client_id(
            client_id,
            purpose,
            existing_metadata=metadata,
        )
        metadata.setdefault("recovered_from_exchange", True)
        metadata["recovery_source"] = "startup"
        metadata.setdefault("position_idx", exchange_order.get("positionIdx"))
        metadata.setdefault("recovered_purpose_classification", purpose)
        return ManagedOrder(
            client_order_id=client_id,
            side=side,
            qty=qty,
            purpose=purpose,
            price=price,
            order_type=order_type,
            reduce_only=reduce_only,
            exchange_order_id=str(exchange_order.get("orderId") or (existing.exchange_order_id if existing else "")) or None,
            status=status,
            filled_qty=filled_qty,
            remaining_qty=remaining_qty,
            metadata=metadata,
            trace=list(existing.trace) if existing else [],
            created_at=existing.created_at if existing else utcnow(),
            updated_at=utcnow(),
        )

    @staticmethod
    def _managed_order_to_dict(order: ManagedOrder) -> dict[str, Any]:
        return {
            "client_order_id": order.client_order_id,
            "side": order.side,
            "qty": order.qty,
            "purpose": order.purpose,
            "price": order.price,
            "order_type": order.order_type,
            "reduce_only": order.reduce_only,
            "exchange_order_id": order.exchange_order_id,
            "status": order.status,
            "filled_qty": order.filled_qty,
            "remaining_qty": order.remaining_qty,
            "metadata": order.metadata,
            "trace": trace_dicts(order.trace),
            "created_at": order.created_at.isoformat(),
            "updated_at": order.updated_at.isoformat(),
        }

    def _managed_order_from_dict(self, item: dict[str, Any]) -> ManagedOrder | None:
        if not isinstance(item, dict):
            return None
        client_order_id = str(item.get("client_order_id") or "")
        if not client_order_id:
            return None
        created_at_raw = item.get("created_at")
        updated_at_raw = item.get("updated_at")
        return ManagedOrder(
            client_order_id=client_order_id,
            side=str(item.get("side") or "long"),
            qty=float(item.get("qty") or 0.0),
            purpose=str(item.get("purpose") or self._recover_purpose_from_client_id(client_order_id)),
            price=self._safe_float(item.get("price"), None),
            order_type=str(item.get("order_type") or "Market"),
            reduce_only=bool(item.get("reduce_only")),
            exchange_order_id=str(item.get("exchange_order_id") or "") or None,
            status=self._normalize_order_status(item.get("status"), "OPEN"),
            filled_qty=float(item.get("filled_qty") or 0.0),
            remaining_qty=float(item.get("remaining_qty") or 0.0),
            metadata=dict(item.get("metadata") or {}),
            trace=[],
            created_at=self._safe_datetime(created_at_raw) or utcnow(),
            updated_at=self._safe_datetime(updated_at_raw) or utcnow(),
        )

    @staticmethod
    def _normalize_order_status(raw_status: Any, default: str = "OPEN") -> str:
        status = str(raw_status or "").strip().lower()
        if not status:
            return default
        if status in {"new", "open", "untriggered", "triggered", "active"}:
            return "OPEN"
        if status in {"partiallyfilled", "partial", "partially_filled"}:
            return "PARTIAL"
        if status in {"filled", "done"}:
            return "FILLED"
        if status in {"cancelled", "canceled", "deactivated", "partiallyfilledcanceled", "partially_filled_canceled"}:
            return "CANCELED"
        if status in {"expired", "expire"}:
            return "EXPIRED"
        if status in {"rejected", "reject"}:
            return "REJECTED"
        if status in {"pending_submit", "pending"}:
            return "PENDING_SUBMIT"
        return default

    @staticmethod
    def _is_terminal_order_status(status: Any) -> bool:
        return str(status or "").upper() in {"FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}

    def _cycle_submit_identity(self, purpose_upper: str, metadata: dict[str, Any]) -> tuple:
        return cycle_submit_identity(purpose_upper, metadata)

    def _find_duplicate_open_cycle_purpose(
        self, intent: StrategyIntent, snapshot: HedgeSnapshot
    ) -> tuple[ManagedOrder | None, str | None]:
        purpose_upper = str(intent.purpose or "").upper()
        if not purpose_upper.startswith("CYCLE_"):
            return None, None
        intent_metadata = getattr(intent, "metadata", {}) or {}
        intent_identity = self._cycle_submit_identity(purpose_upper, intent_metadata)

        def _order_status(order: Any) -> str:
            status = getattr(order, "status", None)
            if status is None and isinstance(order, dict):
                status = order.get("status")
            return str(status or "").upper()

        for order_source, orders in (
            ("runtime", self.runtime_state.active_orders.values()),
            ("snapshot", snapshot.active_orders),
        ):
            for order in orders:
                # Zweck muss immer übereinstimmen
                order_purpose = str(
                    getattr(order, "purpose", None)
                    or (order.get("purpose") if isinstance(order, dict) else None)
                    or ""
                ).upper()
                if order_purpose != purpose_upper:
                    continue
                status_normalized = self._normalize_order_status(
                    getattr(order, "status", None)
                    if hasattr(order, "status")
                    else order.get("status")
                    if isinstance(order, dict)
                    else None
                )
                if self._is_terminal_order_status(status_normalized):
                    continue

                order_metadata = (
                    getattr(order, "metadata", None)
                    if hasattr(order, "metadata")
                    else order.get("metadata")
                    if isinstance(order, dict)
                    else None
                ) or {}
                order_identity = self._cycle_submit_identity(order_purpose, order_metadata)

                # Duplicate nur, wenn Submit-Identität exakt übereinstimmt.
                # Dadurch sind gestagte und normale Second-Leg-Splits Stage-aware:
                # - gleiche Purpose & gleiche Stage → Duplicate
                # - gleiche Purpose & andere Stage → erlaubt
                if order_identity == intent_identity:
                    return order, order_source
        return None, None

    def _is_unsettled_strategy_order(self, order: ManagedOrder) -> bool:
        purpose = str(getattr(order, "purpose", "") or "").upper()
        status = str(getattr(order, "status", "") or "").upper()
        remaining_qty = float(getattr(order, "remaining_qty", 0.0) or 0.0)

        strategy_purpose = (
            purpose
            in {
                self.strategy.LONG_ENTRY_PURPOSE,
                self.strategy.SHORT_ENTRY_PURPOSE,
                self.strategy.LONG_TP_EXIT_PURPOSE,
                self.strategy.LONG_SL_EXIT_PURPOSE,
                self.strategy.SHORT_TP_EXIT_PURPOSE,
                self.strategy.SHORT_SL_EXIT_PURPOSE,
                getattr(self.strategy, "SHORT_HARD_STOP_PURPOSE", "SHORT_HARD_STOP_EXIT"),
            }
            or purpose.startswith("CYCLE_")
            or purpose.startswith("REFILL_")
        )
        if not strategy_purpose:
            return False

        if status in {"FILLED", "CANCELED", "CANCELLED", "REJECTED"}:
            return False

        if remaining_qty > 1e-9:
            return True

        return True

    def _runtime_unsettled_strategy_orders(self) -> list[dict[str, Any]]:
        result = []
        for order in self.runtime_state.active_orders.values():
            if self._is_unsettled_strategy_order(order):
                result.append(self._managed_order_summary(order))
        return result

    @staticmethod
    def _normalize_terminal_order_quantities(managed_order: ManagedOrder) -> None:
        status = str(managed_order.status or "").upper()
        qty = float(managed_order.qty or 0.0)
        filled = float(managed_order.filled_qty or 0.0)
        remaining = float(managed_order.remaining_qty or 0.0)
        if status == "FILLED":
            managed_order.filled_qty = max(filled, qty)
            managed_order.remaining_qty = 0.0
        elif status in {"CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}:
            managed_order.remaining_qty = 0.0
            managed_order.filled_qty = min(filled, qty)

    def _mark_strategy_cycle_purpose_status(
        self,
        *,
        purpose: str | None,
        metadata: dict[str, Any] | None,
        status: str,
    ) -> None:
        if not purpose:
            return
        marker = getattr(self.strategy, "_mark_cycle_purpose_status", None)
        if not callable(marker):
            return
        marker(
            self.runtime_state,
            purpose=str(purpose),
            metadata=dict(metadata or {}),
            status=status,
        )

    def _finalize_managed_order(self, client_id: str, managed_order: ManagedOrder) -> None:
        self._normalize_terminal_order_quantities(managed_order)
        self.audit.log_event(
            "fixed_cycle_terminal_order_removed_from_active_runtime",
            strategy=self.strategy.name,
            client_order_id=client_id,
            exchange_order_id=managed_order.exchange_order_id,
            purpose=managed_order.purpose,
            status=managed_order.status,
            filled_qty=managed_order.filled_qty,
            remaining_qty=managed_order.remaining_qty,
        )
        if managed_order.status == "FILLED" and managed_order.purpose in {
            self.strategy.LONG_ENTRY_PURPOSE,
            self.strategy.SHORT_ENTRY_PURPOSE,
        }:
            snapshot = self.runtime_state.last_snapshot or self.refresh_snapshot("order_finalized")
            self.strategy._mark_initial_entry_reconciled_from_terminal_order(
                self.runtime_state,
                snapshot,
                purpose=managed_order.purpose,
                exchange_order_id=managed_order.exchange_order_id,
                client_order_id=client_id,
                source="order_finalized",
            )
        self.runtime_state.active_orders.pop(client_id, None)
        if managed_order.exchange_order_id:
            self.runtime_state.exchange_to_client_id.pop(managed_order.exchange_order_id, None)
        self._clear_pending_final_exit_submission(
            client_order_id=client_id,
            purpose=managed_order.purpose,
        )
        self.audit.log_event(
            "order_finalized",
            strategy=self.strategy.name,
            client_order_id=client_id,
            exchange_order_id=managed_order.exchange_order_id,
            status=managed_order.status,
            filled_qty=managed_order.filled_qty,
            remaining_qty=managed_order.remaining_qty,
        )
        self.runtime_state.terminal_client_ids.add(client_id)
        if managed_order.exchange_order_id:
            self.runtime_state.terminal_exchange_ids.add(managed_order.exchange_order_id)
        processed_exec_ids = set(managed_order.metadata.get("processed_exec_ids") or [])
        processed_exec_ids.update(managed_order.metadata.get("exec_ids") or [])
        exec_id_attr = getattr(managed_order, "exec_id", None)
        if exec_id_attr:
            processed_exec_ids.add(exec_id_attr)
        self.runtime_state.terminal_exec_ids.update(processed_exec_ids)

    @staticmethod
    def _runtime_side_from_exchange(order: dict[str, Any]) -> str:
        position_idx = str(order.get("positionIdx") or "").strip()
        if position_idx == "1":
            return "long"
        if position_idx == "2":
            return "short"
        side = str(order.get("side") or "").lower()
        reduce_only = bool(order.get("reduceOnly"))
        if reduce_only:
            return "short" if side == "buy" else "long"
        return "long" if side in {"buy", "long"} else "short"

    @staticmethod
    def _recover_split_stage_index_from_client_id(client_id: str) -> int | None:
        match = re.search(r"-split(\d+)(?:-|$)", str(client_id or "").lower())
        if not match:
            return None
        try:
            return int(match.group(1))
        except (TypeError, ValueError):
            return None

    def _recover_split_metadata_from_client_id(
        self,
        client_id: str,
        purpose: str,
        existing_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        metadata = dict(existing_metadata or {})
        split_stage_index = self._recover_split_stage_index_from_client_id(client_id)
        if split_stage_index is None:
            return metadata

        purpose_upper = str(purpose or "").upper()
        cycle_match = re.search(r"CYCLE_(\d+)", purpose_upper)
        cycle_index = None
        if cycle_match:
            try:
                cycle_index = int(cycle_match.group(1))
            except (TypeError, ValueError):
                cycle_index = None

        split_stage_count = metadata.get("split_stage_count") or metadata.get("stage_count")
        if split_stage_count is None and cycle_index is not None:
            state = getattr(self.runtime_state, "strategy_state", {}) or {}
            stage_count_map = state.get("normal_cycle_second_leg_split_stage_count") or {}
            cycle_key = str(cycle_index)
            split_stage_count = (
                stage_count_map.get(cycle_key)
                or stage_count_map.get(cycle_index)
                or metadata.get("split_count")
            )

        metadata.setdefault("normal_cycle_second_leg_split", True)
        metadata.setdefault("split_stage_index", split_stage_index)
        metadata.setdefault("stage_index", split_stage_index + 1)
        metadata.setdefault("split_index", split_stage_index + 1)

        if cycle_index is not None:
            metadata.setdefault("cycle_index", cycle_index)
            metadata.setdefault("split_cycle_index", cycle_index)

        if split_stage_count is not None:
            try:
                split_stage_count = int(split_stage_count)
                metadata.setdefault("split_stage_count", split_stage_count)
                metadata.setdefault("stage_count", split_stage_count)
                metadata.setdefault("split_count", split_stage_count)
            except (TypeError, ValueError):
                metadata.setdefault("split_metadata_missing_stage_count", True)
        else:
            metadata.setdefault("split_metadata_missing_stage_count", True)

        metadata.setdefault("split_metadata_recovered_from_client_id", True)
        return metadata

    def _recover_purpose_from_client_id(self, client_id: str) -> str:
        prefix = f"{self.strategy.name}-"
        if client_id.startswith(prefix):
            remainder = client_id[len(prefix):]
            if "-" in remainder:
                purpose_part = remainder.rsplit("-", 1)[0]
                purpose_part = re.sub(r"-split\d+$", "", purpose_part)
                return purpose_part.upper()
        recovered_prefix = f"recovered-{self.strategy.name}-"
        if client_id.startswith(recovered_prefix):
            remainder = client_id[len(recovered_prefix):]
            if remainder.startswith("-") and "--" in remainder[1:]:
                purpose_part = remainder[1:].split("--", 1)[0]
                if purpose_part:
                    purpose_part = re.sub(r"-split\d+$", "", purpose_part)
                    return purpose_part.upper()
            if "-" in remainder:
                purpose_part = remainder.rsplit("-", 1)[0]
                purpose_part = re.sub(r"-split\d+$", "", purpose_part)
                return purpose_part.upper()
        return "RECOVERED_ORDER"

    def _classify_unknown_order_purpose(self, exchange_order: dict[str, Any]) -> str:
        side = self._runtime_side_from_exchange(exchange_order)
        reduce_only = bool(exchange_order.get("reduceOnly"))
        order_type = str(exchange_order.get("orderType") or "").lower()
        strategy_name = self.strategy.name

        if strategy_name == "dynamic_breakeven_hedge":
            if reduce_only and side == "short":
                return "DYN_SHORT_COMPENSATE" if order_type == "limit" else "DYN_SHORT_REDUCE"
            if reduce_only and side == "long":
                return "DYN_LONG_REDUCE"
        if strategy_name == "basket_exit_hedge":
            if reduce_only and side == "long":
                return "BASKET_EXIT_LONG"
            if reduce_only and side == "short":
                return "BASKET_EXIT_SHORT"

        if reduce_only:
            return f"RECOVERED_{side.upper()}_REDUCE"
        return f"RECOVERED_{side.upper()}_ENTRY"

    def _classification_inputs(self, exchange_order: dict[str, Any]) -> dict[str, Any]:
        return {
            "strategy_name": self.strategy.name,
            "position_idx": exchange_order.get("positionIdx"),
            "exchange_side": exchange_order.get("side"),
            "runtime_side": self._runtime_side_from_exchange(exchange_order),
            "reduce_only": bool(exchange_order.get("reduceOnly")),
            "order_type": exchange_order.get("orderType"),
            "qty": self._safe_float(exchange_order.get("qty"), None),
            "price": self._safe_float(exchange_order.get("price"), None),
        }

    def _exchange_order_summary(self, exchange_order: dict[str, Any]) -> dict[str, Any]:
        return {
            "order_id": str(exchange_order.get("orderId") or ""),
            "order_link_id": str(exchange_order.get("orderLinkId") or ""),
            "status": str(exchange_order.get("orderStatus") or ""),
            "side": str(exchange_order.get("side") or ""),
            "position_idx": exchange_order.get("positionIdx"),
            "reduce_only": bool(exchange_order.get("reduceOnly")),
            "order_type": str(exchange_order.get("orderType") or ""),
            "qty": self._safe_float(exchange_order.get("qty"), None),
            "price": self._safe_float(exchange_order.get("price"), None),
            "cum_exec_qty": self._safe_float(exchange_order.get("cumExecQty"), None),
        }

    def _history_order_summary(self, history_order: dict[str, Any]) -> dict[str, Any]:
        return {
            "order_id": str(history_order.get("orderId") or ""),
            "order_link_id": str(history_order.get("orderLinkId") or ""),
            "status": str(history_order.get("orderStatus") or ""),
            "price": self._safe_float(history_order.get("price"), None),
            "avg_price": self._safe_float(history_order.get("avgPrice"), None),
            "cum_exec_qty": self._safe_float(history_order.get("cumExecQty"), None),
            "cum_exec_value": self._safe_float(history_order.get("cumExecValue"), None),
        }

    def _managed_order_summary(self, managed_order: ManagedOrder) -> dict[str, Any]:
        return {
            "client_order_id": managed_order.client_order_id,
            "exchange_order_id": managed_order.exchange_order_id,
            "side": managed_order.side,
            "purpose": managed_order.purpose,
            "order_type": managed_order.order_type,
            "reduce_only": managed_order.reduce_only,
            "status": managed_order.status,
            "qty": managed_order.qty,
            "filled_qty": managed_order.filled_qty,
            "remaining_qty": managed_order.remaining_qty,
            "price": managed_order.price,
        }

    @staticmethod
    def _safe_float(value: Any, default: float | None) -> float | None:
        if value in (None, ""):
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_datetime(value: Any) -> Any:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None


def configure_runtime_logging(log_file: str) -> None:
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    file_handler = RotatingFileHandler(log_path, maxBytes=10 * 1024 * 1024, backupCount=5)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)
    root_logger.addHandler(file_handler)