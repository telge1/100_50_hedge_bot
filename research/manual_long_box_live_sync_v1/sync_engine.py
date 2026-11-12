"""Map activated manual long boxes to a single managed limit order (DRY-RUN)."""

from __future__ import annotations

import math
import threading
from typing import Any

from research.manual_long_box_research_v1.models import LongBoxSetup
from research.manual_long_box_research_v1.recognition import (
    recognize_long_boxes,
    recognize_short_boxes,
)
from research.manual_long_box_research_v1.validation import (
    validate_long_geometry,
    validate_short_geometry,
)

from .activation import BoxActivation
from .config import DRY_RUN_ONLY, PRICE_EPS
from .drawing_watch import DrawingFileObservation, hash_payload
from .exchange_dry_run import DryRunExchangeAdapter, OrderStatus
from .order_links import resolve_order_link_id
from .persistence import BoxSyncState, EventLog, StateStore


def _prices_equal(a: float, b: float) -> bool:
    return abs(a - b) <= PRICE_EPS


def _prev_terminal(prev: BoxSyncState | None) -> bool:
    return bool(prev and prev.order_status in ("FILLED", "CANCELLED"))


def _box_side(box: LongBoxSetup) -> str:
    raw = box.raw if isinstance(box.raw, dict) else {}
    if raw.get("drawing_type") == "short_position":
        return "short"
    return "long"


class ManualLongBoxSyncEngine:
    def __init__(
        self,
        exchange: DryRunExchangeAdapter,
        state: StateStore,
        event_log: EventLog,
        *,
        dry_run: bool = DRY_RUN_ONLY,
    ) -> None:
        self.exchange = exchange
        self.state = state
        self.event_log = event_log
        self.dry_run = dry_run
        self._locks: dict[str, threading.Lock] = {}

    def _lock(self, drawing_id: str) -> threading.Lock:
        if drawing_id not in self._locks:
            self._locks[drawing_id] = threading.Lock()
        return self._locks[drawing_id]

    def find_box(
        self, payload: dict[str, Any], activation: BoxActivation
    ) -> LongBoxSetup | None:
        setups, _ = recognize_long_boxes(
            payload, symbol=activation.symbol, manual_only=True
        )
        for s in setups:
            if s.drawing_id == activation.drawing_id:
                return s
        short_setups, _ = recognize_short_boxes(
            payload, symbol=activation.symbol, manual_only=True
        )
        for s in short_setups:
            if s.drawing_id == activation.drawing_id:
                return s
        return None

    def process_observation(
        self,
        obs: DrawingFileObservation,
        activations: dict[str, BoxActivation],
        *,
        latency_probe_start: float | None = None,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for act in activations.values():
            if not act.enabled:
                continue
            if not act.dry_run and self.dry_run:
                results.append(
                    {"drawing_id": act.drawing_id, "skipped": "dry_run_required"}
                )
                continue
            r = self._sync_one(obs, act, latency_probe_start=latency_probe_start)
            if r:
                results.append(r)
        return results

    def _sync_one(
        self,
        obs: DrawingFileObservation,
        activation: BoxActivation,
        *,
        latency_probe_start: float | None = None,
    ) -> dict[str, Any]:
        did = activation.drawing_id
        with self._lock(did):
            box = self.find_box(obs.payload, activation)
            if box is None:
                self.event_log.append(
                    {"event": "box_missing", "drawing_id": did, "path": str(obs.path)}
                )
                return {"drawing_id": did, "ok": False, "reason": "box_not_in_drawings"}

            side = _box_side(box)
            if side == "short":
                ok_geom, geom_reason = validate_short_geometry(
                    box.stop_price, box.entry_price, box.target_price
                )
            else:
                ok_geom, geom_reason = validate_long_geometry(
                    box.stop_price, box.entry_price, box.target_price
                )
            if not ok_geom:
                self.event_log.append(
                    {"event": "invalid_geometry", "drawing_id": did, "reason": geom_reason}
                )
                return {"drawing_id": did, "ok": False, "reason": geom_reason}

            content_hash = hash_payload(
                {
                    "id": did,
                    "entry": box.entry_price,
                    "stop": box.stop_price,
                    "target": box.target_price,
                    "start": box.start_ts,
                    "end": box.end_ts,
                }
            )

            prev = self.state.get(did)
            if prev and prev.order_status == "FILLED":
                return {
                    "drawing_id": did,
                    "ok": True,
                    "action": "skipped_filled_no_second_order",
                }
            prev_terminal = _prev_terminal(prev)
            link = resolve_order_link_id(
                did,
                activation_link_id=activation.order_link_id,
                prev_link_id=prev.order_link_id if prev else None,
                prev_terminal=prev_terminal,
            )
            open_order = self.exchange.get_open_by_link(link)
            if (
                prev
                and prev.order_status == "CANCELLED"
                and prev.order_link_id
                and prev.order_link_id != link
                and self.exchange.get_open_by_link(prev.order_link_id)
            ):
                return {
                    "drawing_id": did,
                    "ok": False,
                    "reason": "prior_cycle_still_open",
                }

            if (
                not self.dry_run
                and prev
                and prev.order_status not in ("FILLED", "CANCELLED")
                and open_order is None
                and prev.exchange_order_id
            ):
                live = self.exchange.get_order(prev.exchange_order_id)
                if live is None:
                    self.event_log.append(
                        {
                            "event": "stale_state_no_blind_create",
                            "drawing_id": did,
                            "order_id": prev.exchange_order_id,
                        }
                    )
                    return {
                        "drawing_id": did,
                        "ok": False,
                        "reason": "reconcile_required",
                    }

            new_cycle_after_cancel = (
                prev
                and prev.order_status == "CANCELLED"
                and open_order is None
                and activation.order_link_id
                and activation.order_link_id != (prev.order_link_id or "")
            )
            if (
                prev
                and _prices_equal(prev.entry_price, box.entry_price)
                and not new_cycle_after_cancel
            ):
                sl_changed = not _prices_equal(prev.stop_price, box.stop_price)
                tp_changed = not _prices_equal(prev.target_price, box.target_price)
                if sl_changed or tp_changed:
                    # SL/TP only via chart Setzen → confirm amend (never auto-exchange here).
                    prev.desired_sl = box.stop_price
                    prev.desired_tp = box.target_price
                    prev.last_action = "desired_sl_tp_documented"
                    prev.content_hash = content_hash
                    self.state.put(prev)
                    self.event_log.append(
                        {
                            "event": "sl_tp_pending_manual_setzen",
                            "drawing_id": did,
                            "stop": box.stop_price,
                            "target": box.target_price,
                        }
                    )
                return {"drawing_id": did, "ok": True, "action": "no_entry_change"}

            if activation.qty_doge is not None and activation.qty_doge > 0:
                qty = float(activation.qty_doge)
            else:
                qty = max(activation.notional_usd / box.entry_price, 0.0)
            if not math.isfinite(qty) or qty <= 0:
                return {"drawing_id": did, "ok": False, "reason": "invalid_qty"}

            action = ""
            ack = None
            order_id = prev.exchange_order_id if prev else None

            create_limit = (
                self.exchange.create_limit_short
                if side == "short" and hasattr(self.exchange, "create_limit_short")
                else self.exchange.create_limit_long
            )
            if open_order is None and (prev is None or new_cycle_after_cancel):
                ack = create_limit(
                    symbol=box.symbol,
                    price=box.entry_price,
                    qty=qty,
                    order_link_id=link,
                    stop_loss=box.stop_price,
                    take_profit=box.target_price,
                )
                action = "CREATE_LIMIT"
                if ack.ok and ack.order_id:
                    order_id = ack.order_id
            else:
                oid = open_order.order_id if open_order else order_id
                if not oid:
                    ack = create_limit(
                        symbol=box.symbol,
                        price=box.entry_price,
                        qty=qty,
                        order_link_id=link,
                        stop_loss=box.stop_price,
                        take_profit=box.target_price,
                    )
                    action = "CREATE_LIMIT_RECOVER"
                    order_id = ack.order_id
                else:
                    live = self.exchange.get_order(oid)
                    if live and live.status == OrderStatus.FILLED:
                        self.event_log.append(
                            {"event": "skip_amend_filled", "drawing_id": did, "order_id": oid}
                        )
                        return {
                            "drawing_id": did,
                            "ok": True,
                            "action": "skipped_filled_no_second_order",
                        }
                    if live and live.amend_in_flight:
                        return {"drawing_id": did, "ok": False, "reason": "amend_in_flight"}
                    amend_qty = float(live.qty or qty)
                    try:
                        from research.manual_long_box_live_sync_v1.order_settings.order_sizing import (
                            amend_notional_ok,
                        )

                        ok_amend, amend_err = amend_notional_ok(amend_qty, box.entry_price)
                        if not ok_amend:
                            self.event_log.append(
                                {
                                    "event": "amend_blocked_notional_cap",
                                    "drawing_id": did,
                                    "reason": amend_err,
                                }
                            )
                            return {
                                "drawing_id": did,
                                "ok": False,
                                "reason": amend_err or "amend_notional_exceeds_cap",
                            }
                    except ImportError:
                        pass
                    ack = self.exchange.amend_limit(oid, new_price=box.entry_price)
                    action = "AMEND_LIMIT"
                    order_id = oid

            if ack and not ack.ok:
                self.event_log.append(
                    {"event": "exchange_reject", "drawing_id": did, "action": action, "msg": ack.message}
                )
                return {"drawing_id": did, "ok": False, "reason": ack.message, "action": action}

            if not ack or not ack.ok or not order_id:
                return {"drawing_id": did, "ok": False, "reason": "no_ack"}

            live_after = self.exchange.get_order(order_id)
            st = BoxSyncState(
                drawing_id=did,
                symbol=box.symbol,
                exchange_order_id=order_id,
                order_link_id=link,
                entry_price=box.entry_price,
                stop_price=box.stop_price,
                target_price=box.target_price,
                order_status=live_after.status.value if live_after else ack.status,
                content_hash=content_hash,
                last_action=action,
                desired_sl=box.stop_price,
                desired_tp=box.target_price,
            )
            self.state.put(st)

            open_count = len(self.exchange.reconcile_open_orders(box.symbol))
            rec = {
                "event": "sync_ok",
                "drawing_id": did,
                "action": action,
                "order_id": order_id,
                "entry": box.entry_price,
                "open_orders_symbol": open_count,
                "dry_run": self.dry_run,
            }
            if latency_probe_start is not None:
                import time

                rec["latency_sec"] = time.time() - latency_probe_start
            self.event_log.append(rec)
            return {
                "drawing_id": did,
                "ok": True,
                "action": action,
                "order_id": order_id,
                "entry_price": box.entry_price,
                "open_orders": open_count,
                "latency_sec": rec.get("latency_sec"),
            }

    def handle_fill_during_amend(self, drawing_id: str) -> dict[str, Any]:
        """Test hook: fill while sync layer would amend — must not spawn 2nd order."""
        with self._lock(drawing_id):
            prev = self.state.get(drawing_id)
            if not prev:
                return {"ok": False, "reason": "no_state"}
            oid = prev.exchange_order_id
            self.exchange.simulate_fill(oid, fraction=1.0)
            link = prev.order_link_id or f"mlb-sync-{drawing_id}"
            open_after = self.exchange.get_open_by_link(link)
            prev.order_status = OrderStatus.FILLED.value
            prev.last_action = "SIMULATED_FILL"
            self.state.put(prev)
            return {
                "ok": True,
                "filled_order_id": oid,
                "second_open_order": open_after is not None,
            }
