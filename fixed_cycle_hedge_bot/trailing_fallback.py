from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from fixed_cycle_hedge_bot.order_manager import BybitOrderManager

logger = logging.getLogger(__name__)

POSITION_IDX = 2
TRIGGER_BY = "LastPrice"


@dataclass
class ShortTpFallbackState:
    active: bool = False
    purpose: str | None = None
    position_idx: int = POSITION_IDX
    qty: float = 0.0
    original_trigger_price: float = 0.0
    activation_price: float = 0.0
    trailing_distance: float = 0.0
    lowest_price: float = 0.0
    submitted: bool = False
    submit_failed: bool = False
    client_order_id: str | None = None
    exchange_order_id: str | None = None
    started_at_ms: int | None = None
    last_submit_attempt_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "purpose": self.purpose,
            "position_idx": self.position_idx,
            "qty": self.qty,
            "original_trigger_price": self.original_trigger_price,
            "activation_price": self.activation_price,
            "trailing_distance": self.trailing_distance,
            "lowest_price": self.lowest_price,
            "submitted": self.submitted,
            "submit_failed": self.submit_failed,
            "client_order_id": self.client_order_id,
            "exchange_order_id": self.exchange_order_id,
            "started_at_ms": self.started_at_ms,
            "last_submit_attempt_ms": self.last_submit_attempt_ms,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "ShortTpFallbackState":
        if not payload:
            return cls()
        return cls(
            active=bool(payload.get("active")),
            purpose=payload.get("purpose"),
            position_idx=int(payload.get("position_idx") or POSITION_IDX),
            qty=float(payload.get("qty") or 0.0),
            original_trigger_price=float(payload.get("original_trigger_price") or 0.0),
            activation_price=float(payload.get("activation_price") or 0.0),
            trailing_distance=float(payload.get("trailing_distance") or 0.0),
            lowest_price=float(payload.get("lowest_price") or 0.0),
            submitted=bool(payload.get("submitted")),
            submit_failed=bool(payload.get("submit_failed")),
            client_order_id=payload.get("client_order_id"),
            exchange_order_id=payload.get("exchange_order_id"),
            started_at_ms=payload.get("started_at_ms"),
            last_submit_attempt_ms=payload.get("last_submit_attempt_ms"),
        )


def _build_stop_body(
    symbol: str,
    category: str,
    qty: float,
    stop_price: float,
) -> dict[str, Any]:
    client_order_id = f"short-fallback-{int(time.time() * 1000)}"
    return {
        "category": category,
        "symbol": symbol.upper(),
        "side": "Buy",
        # This is a stop-triggered market close. The trigger is capped,
        # but the final execution price can still move during market fill.
        "orderType": "Market",
        "qty": f"{qty}",
        "triggerPrice": f"{stop_price}",
        "triggerDirection": 1,
        "triggerBy": TRIGGER_BY,
        "positionIdx": POSITION_IDX,
        "reduceOnly": True,
        "closeOnTrigger": True,
        "orderLinkId": client_order_id,
    }


def start_short_tp_fallback(
    state: ShortTpFallbackState,
    *,
    qty: float,
    original_trigger_price: float,
    current_price: float,
    activation_drop_pct: float,
    stop_offset_pct: float,
) -> bool:
    if qty <= 0 or original_trigger_price <= 0 or current_price <= 0:
        return False
    activation_reference = original_trigger_price * (1 - activation_drop_pct)
    activation_price = min(max(current_price, 0.0), activation_reference)
    activation_price = min(activation_price, original_trigger_price)
    if activation_price <= 0:
        return False
    trailing_distance = activation_price * stop_offset_pct
    state.active = True
    state.purpose = "SHORT_TP_FALLBACK"
    state.position_idx = POSITION_IDX
    state.qty = qty
    state.original_trigger_price = original_trigger_price
    state.activation_price = activation_price
    state.trailing_distance = trailing_distance
    state.lowest_price = current_price
    state.submitted = False
    state.submit_failed = False
    state.client_order_id = None
    state.exchange_order_id = None
    state.started_at_ms = int(time.time() * 1000)
    state.last_submit_attempt_ms = None
    return True


def update_short_tp_fallback(
    state: ShortTpFallbackState,
    *,
    order_manager: BybitOrderManager,
    symbol: str,
    category: str,
    current_price: float,
    activation_drop_pct: float,
    stop_offset_pct: float,
) -> tuple[bool, dict[str, Any] | None]:
    if not state.active:
        return False, None
    if current_price > 0 and (state.lowest_price <= 0 or current_price < state.lowest_price):
        state.lowest_price = current_price
    if state.submitted:
        return False, None

    if state.submit_failed:
        state.submit_failed = False

    if state.qty <= 0 or state.activation_price <= 0:
        return False, None
    if current_price > state.activation_price:
        return False, None
    # original_trigger_price is a ceiling for the stop trigger only.
    # Because the fallback submits a Market stop order, exchange fill
    # price can still deviate once the trigger has fired.
    stop_price = state.lowest_price * (1 + stop_offset_pct)
    stop_price = max(stop_price, state.activation_price)
    stop_price = min(stop_price, state.original_trigger_price or float("inf"))
    normalized_qty = order_manager.normalize_qty(symbol, state.qty, category)
    trigger_price = order_manager.normalize_price(symbol, stop_price, category)
    if normalized_qty <= 0 or trigger_price <= 0:
        return False, None
    body = _build_stop_body(
        symbol=symbol,
        category=category,
        qty=normalized_qty,
        stop_price=trigger_price,
    )
    state.client_order_id = body.get("orderLinkId")
    logger.info(
        "%s %s",
        "SHORT_TP_FALLBACK_PRE_SUBMIT",
        {
            "original_trigger_price": state.original_trigger_price,
            "activation_price": state.activation_price,
            "lowest_price": state.lowest_price,
            "trailing_distance": state.trailing_distance,
            "qty": state.qty,
            "normalized_qty": normalized_qty,
            "stop_price_raw": stop_price,
            "trigger_price_normalized": trigger_price,
            "submitted": state.submitted,
            "submit_failed": state.submit_failed,
        },
    )
    response = order_manager._post("/v5/order/create", json.dumps(body))
    accepted = (
        isinstance(response, dict)
        and response.get("retCode") in (0, "0")
        and bool((response.get("result") or {}).get("orderId"))
    )
    if accepted:
        order_id = (response.get("result") or {}).get("orderId")

        if not order_id:
            state.submit_failed = True
        else:
            state.submitted = True
            state.exchange_order_id = order_id
    else:
        state.submit_failed = True
    return accepted, response


def reset_short_tp_fallback(state: ShortTpFallbackState) -> None:
    state.active = False
    state.purpose = None
    state.position_idx = POSITION_IDX
    state.qty = 0.0
    state.original_trigger_price = 0.0
    state.activation_price = 0.0
    state.trailing_distance = 0.0
    state.lowest_price = 0.0
    state.submitted = False
    state.submit_failed = False
    state.client_order_id = None
    state.exchange_order_id = None
    state.started_at_ms = None
    state.last_submit_attempt_ms = None


@dataclass
class TrailingFallbackState:
    active: bool = False
    purpose: str | None = None
    position_idx: int | None = None
    qty: float = 0.0
    trailing_offset_pct: float = 0.0
    requote_step_pct: float | None = None
    original_trigger: float | None = None
    current_trigger_price: float | None = None
    last_reference_price: float | None = None
    lowest_price: float | None = None
    pending_reference_price: float | None = None
    requote_needed: bool = False
    cancel_pending: bool = False
    submit_pending: bool = False
    exchange_order_id: str | None = None
    client_order_id: str | None = None
    max_rebound_price: float | None = None
    trailing_dist: float = 0.0


class TrailingFallbackManager:
    def __init__(self) -> None:
        self.state = TrailingFallbackState()

    @property
    def active(self) -> bool:
        return self.state.active

    @property
    def qty(self) -> float:
        return self.state.qty

    def activate(
        self,
        *,
        purpose: str,
        position_idx: int,
        qty: float,
        trigger_price: float,
        reference_price: float | None = None,
        current_price: float | None = None,
        trailing_offset_pct: float | None = None,
        trailing_dist: float | None = None,
        requote_step_pct: float | None = None,
    ) -> None:
        base_price = reference_price if reference_price is not None else current_price
        if base_price is None:
            base_price = 0.0
        offset_pct = trailing_offset_pct if trailing_offset_pct is not None else 0.0
        self.state.active = True
        self.state.purpose = purpose
        self.state.position_idx = position_idx
        self.state.qty = qty
        self.state.original_trigger = trigger_price
        self.state.last_reference_price = base_price
        self.state.lowest_price = base_price
        self.state.trailing_offset_pct = offset_pct
        self.state.trailing_dist = trailing_dist if trailing_dist is not None else (base_price * offset_pct)
        self.state.requote_step_pct = requote_step_pct
        self.state.current_trigger_price = trigger_price
        self.state.max_rebound_price = (
            base_price + self.state.trailing_dist if base_price and self.state.trailing_dist else trigger_price
        )
        self.state.requote_needed = True
        self.state.cancel_pending = False
        self.state.submit_pending = False
        self.state.exchange_order_id = None
        self.state.client_order_id = None

    def update(self, current_price: float) -> None:
        if not self.state.active:
            return
        if self.state.lowest_price is None or current_price < self.state.lowest_price:
            self.state.lowest_price = current_price
        if self.state.trailing_dist > 0 and self.state.lowest_price is not None:
            self.state.max_rebound_price = self.state.lowest_price + self.state.trailing_dist

    def should_submit(self) -> bool:
        return (
            self.active
            and not self.state.submit_pending
            and self.state.max_rebound_price is not None
            and self.state.lowest_price is not None
            and self.state.max_rebound_price <= (self.state.original_trigger or float("inf"))
        )

    def get_next_trigger_price(self) -> float | None:
        return self.state.current_trigger_price

    def mark_cancel_pending(self) -> None:
        self.state.cancel_pending = True

    def mark_submit_pending(self) -> None:
        self.state.submit_pending = True

    def mark_order_live(
        self, client_id: str | None = None, exchange_id: str | None = None
    ) -> None:
        self.state.client_order_id = client_id
        self.state.exchange_order_id = exchange_id
        self.state.requote_needed = False
        self.state.submit_pending = False
        self.state.cancel_pending = False
        if self.state.pending_reference_price is not None:
            self.state.last_reference_price = self.state.pending_reference_price
            self.state.pending_reference_price = None

    def mark_submitted(self) -> None:
        self.state.submit_pending = True

    def reset(self) -> None:
        self.state = TrailingFallbackState()

