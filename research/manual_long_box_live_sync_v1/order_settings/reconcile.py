"""REST reconciliation — HTTP ack is not final exchange state."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from research.manual_long_box_live_sync_v1.config import PRICE_EPS
from research.manual_long_box_live_sync_v1.exchange_dry_run import DryRunOrder, OrderStatus


@dataclass
class ConfirmedOrder:
    order_id: str
    order_link_id: str | None
    order_status: str
    price: float | None
    qty: float | None
    leaves_qty: float | None
    cum_exec_qty: float | None
    raw: dict[str, Any]
    stop_loss: float | None = None
    take_profit: float | None = None
    side: str | None = None
    position_idx: int | None = None


def _parse_order_row(row: dict[str, Any]) -> ConfirmedOrder:
    info = row.get("info") or row
    return ConfirmedOrder(
        order_id=str(row.get("id") or info.get("orderId") or ""),
        order_link_id=info.get("orderLinkId"),
        order_status=str(row.get("orderStatus") or info.get("orderStatus") or ""),
        price=_f(row.get("price") or info.get("price")),
        qty=_f(info.get("qty")),
        leaves_qty=_f(info.get("leavesQty")),
        cum_exec_qty=_f(info.get("cumExecQty")),
        stop_loss=_f(info.get("stopLoss")),
        take_profit=_f(info.get("takeProfit")),
        side=str(info.get("side") or row.get("side") or "") or None,
        position_idx=_int(info.get("positionIdx")),
        raw=row,
    )


def _int(x) -> int | None:
    try:
        if x is None or x == "":
            return None
        return int(x)
    except (TypeError, ValueError):
        return None


def _order_matches_expectation(
    parsed: ConfirmedOrder,
    *,
    expected_side: str | None,
    expected_position_idx: int | None,
) -> bool:
    if expected_side and parsed.side and parsed.side != expected_side:
        return False
    if expected_position_idx is not None and parsed.position_idx is not None:
        if int(parsed.position_idx) != int(expected_position_idx):
            return False
    return True


def _f(x) -> float | None:
    try:
        if x is None or x == "":
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def managed_order_link_prefix(drawing_id: str) -> str:
    return f"mlb-sync-{drawing_id}"


def discover_managed_open_order_ex(
    order_manager,
    symbol: str,
    drawing_id: str,
    *,
    state_order_id: str | None = None,
    state_order_link_id: str | None = None,
    timeout: float = 5.0,
    expected_side: str | None = None,
    expected_position_idx: int | None = None,
) -> tuple[ConfirmedOrder | None, str | None]:
    """Find the chart's limit order; fail-closed when multiple prefix matches."""
    sym = symbol.upper()
    if state_order_id:
        found = find_limit_order(
            order_manager,
            sym,
            order_id=str(state_order_id),
            timeout=timeout,
            expected_side=expected_side,
            expected_position_idx=expected_position_idx,
        )
        if found:
            return found, None
    if state_order_link_id:
        found = find_limit_order(
            order_manager,
            sym,
            order_link_id=str(state_order_link_id),
            timeout=timeout,
            expected_side=expected_side,
            expected_position_idx=expected_position_idx,
        )
        if found:
            return found, None
    prefix = managed_order_link_prefix(drawing_id)
    rows = order_manager.fetch_open_orders_direct(
        sym, timeout=timeout, order_filter="Order"
    )
    prefix_hits: list[ConfirmedOrder] = []
    for row in rows or []:
        parsed = _parse_order_row(row)
        if not _order_matches_expectation(
            parsed,
            expected_side=expected_side,
            expected_position_idx=expected_position_idx,
        ):
            continue
        link = str(parsed.order_link_id or "")
        if link == prefix or link.startswith(prefix + "-"):
            prefix_hits.append(parsed)
    if len(prefix_hits) > 1:
        return None, "ambiguous_managed_orders"
    if len(prefix_hits) == 1:
        return prefix_hits[0], None
    return None, None


def discover_managed_open_order(
    order_manager,
    symbol: str,
    drawing_id: str,
    *,
    state_order_id: str | None = None,
    state_order_link_id: str | None = None,
    timeout: float = 5.0,
) -> ConfirmedOrder | None:
    """Find the chart's limit order even when local JSON is stale (cancelled / old ids)."""
    row, err = discover_managed_open_order_ex(
        order_manager,
        symbol,
        drawing_id,
        state_order_id=state_order_id,
        state_order_link_id=state_order_link_id,
        timeout=timeout,
    )
    if err:
        return None
    return row


def find_limit_order(
    order_manager,
    symbol: str,
    *,
    order_id: str | None = None,
    order_link_id: str | None = None,
    timeout: float = 5.0,
    expected_side: str | None = None,
    expected_position_idx: int | None = None,
) -> ConfirmedOrder | None:
    rows = order_manager.fetch_open_orders_direct(
        symbol, timeout=timeout, order_filter="Order"
    )
    for row in rows or []:
        parsed = _parse_order_row(row)
        if not _order_matches_expectation(
            parsed,
            expected_side=expected_side,
            expected_position_idx=expected_position_idx,
        ):
            continue
        if order_id and parsed.order_id == str(order_id):
            return parsed
        if order_link_id and parsed.order_link_id == str(order_link_id):
            return parsed
    return None


def wait_for_order_rest(
    order_manager,
    symbol: str,
    *,
    order_id: str | None = None,
    order_link_id: str | None = None,
    expected_price: float | None = None,
    price_tol: float = 1e-9,
    attempts: int = 8,
    delay_sec: float = 0.25,
) -> tuple[bool, ConfirmedOrder | None, str]:
    """Poll open orders until visible or terminal fill (not on book)."""
    last = None
    for _ in range(attempts):
        found = find_limit_order(
            order_manager,
            symbol,
            order_id=order_id,
            order_link_id=order_link_id,
        )
        if found:
            if expected_price is not None and found.price is not None:
                if abs(found.price - expected_price) > price_tol:
                    last = found
                    time.sleep(delay_sec)
                    continue
            return True, found, "confirmed_on_book"
        last = None
        time.sleep(delay_sec)
    if last:
        return True, last, "confirmed_price_pending"
    return False, None, "not_found_on_book"


def map_bybit_order_status(raw: str) -> OrderStatus:
    s = (raw or "").strip().lower().replace(" ", "")
    if s == "filled":
        return OrderStatus.FILLED
    if s in ("partiallyfilled",):
        return OrderStatus.PARTIALLY_FILLED
    if s in ("cancelled", "deactivated", "rejected"):
        return OrderStatus.CANCELLED
    if s in ("new", "untriggered", "active", "created"):
        return OrderStatus.OPEN
    return OrderStatus.OPEN


def confirmed_to_dry_view(confirmed: ConfirmedOrder, *, amend_in_flight: bool = False) -> DryRunOrder:
    return DryRunOrder(
        order_id=confirmed.order_id,
        order_link_id=str(confirmed.order_link_id or ""),
        symbol="",
        side="Buy",
        price=float(confirmed.price or 0.0),
        qty=float(confirmed.qty or 0.0),
        status=map_bybit_order_status(confirmed.order_status),
        filled_qty=float(confirmed.cum_exec_qty or 0.0),
        amend_in_flight=amend_in_flight,
        stop_loss=confirmed.stop_loss,
        take_profit=confirmed.take_profit,
    )


def prices_differ(a: float | None, b: float | None, *, eps: float = PRICE_EPS) -> bool:
    if a is None and b is None:
        return False
    if a is None or b is None:
        return True
    return abs(float(a) - float(b)) > eps


def wait_for_amend_fields(
    order_manager,
    symbol: str,
    *,
    order_id: str,
    order_link_id: str | None = None,
    expected_entry: float | None = None,
    expected_sl: float | None = None,
    expected_tp: float | None = None,
    tick_size: float | None = None,
    attempts: int = 8,
    delay_sec: float = 0.25,
) -> tuple[bool, ConfirmedOrder | None, str, dict[str, Any]]:
    """Poll until changed fields match REST (tick-normalized, not HTTP-ACK alone)."""
    from .price_tick import prices_equal_tick, round_price_to_tick

    last: ConfirmedOrder | None = None
    last_reason = "amend_verify_failed"
    for _ in range(attempts):
        found = find_limit_order(
            order_manager,
            symbol,
            order_id=order_id,
            order_link_id=order_link_id,
        )
        last = found
        if not found:
            last_reason = "order_not_on_book_during_verify"
            time.sleep(delay_sec)
            continue
        failures: list[str] = []
        if expected_entry is not None:
            if found.price is None:
                failures.append("entry_missing_in_rest")
            elif not prices_equal_tick(found.price, expected_entry, tick_size):
                failures.append(
                    "entry_mismatch:"
                    f"rest={found.price}"
                    f" expected={round_price_to_tick(expected_entry, tick_size)}"
                )
        if expected_sl is not None:
            if found.stop_loss is None:
                failures.append("stop_loss_missing_in_rest")
            elif not prices_equal_tick(found.stop_loss, expected_sl, tick_size):
                failures.append(
                    "stop_loss_mismatch:"
                    f"rest={found.stop_loss}"
                    f" expected={round_price_to_tick(expected_sl, tick_size)}"
                )
        if expected_tp is not None:
            if found.take_profit is None:
                failures.append("take_profit_missing_in_rest")
            elif not prices_equal_tick(found.take_profit, expected_tp, tick_size):
                failures.append(
                    "take_profit_mismatch:"
                    f"rest={found.take_profit}"
                    f" expected={round_price_to_tick(expected_tp, tick_size)}"
                )
        if not failures:
            return True, found, "amend_confirmed", {"matched": True}
        last_reason = ";".join(failures)
        time.sleep(delay_sec)
    detail = {
        "last_rest": last,
        "failures": last_reason,
        "expected_entry": expected_entry,
        "expected_sl": expected_sl,
        "expected_tp": expected_tp,
        "tick_size": tick_size,
    }
    return False, last, last_reason, detail
