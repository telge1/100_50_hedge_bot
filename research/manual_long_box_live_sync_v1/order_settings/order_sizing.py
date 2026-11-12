"""Fixed 10 USDT test sizing — floor to qty step, never exceed cap."""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from typing import Any

TEST_ORDER_NOTIONAL_USDT = Decimal("10.00")
MARGIN_FEE_BUFFER_USDT = Decimal("0.35")


def _dec(x: float | str | Decimal) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def floor_qty_to_step(qty_raw: Decimal, qty_step: Decimal) -> Decimal:
    if qty_step <= 0:
        return qty_raw
    steps = (qty_raw / qty_step).quantize(Decimal("1"), rounding=ROUND_DOWN)
    return steps * qty_step


def fetch_instrument_steps_public(symbol: str) -> tuple[Decimal, Decimal] | None:
    try:
        import requests

        url = (
            "https://api.bybit.com/v5/market/instruments-info"
            f"?category=linear&symbol={symbol.upper()}"
        )
        resp = requests.get(url, timeout=8)
        data = resp.json()
        if data.get("retCode") != 0:
            return None
        lot = (data.get("result", {}).get("list") or [{}])[0].get("lotSizeFilter") or {}
        return _dec(lot.get("qtyStep", "0.01")), _dec(lot.get("minOrderQty", "0"))
    except Exception:
        return None


def fetch_instrument_steps(order_manager, symbol: str) -> tuple[Decimal, Decimal] | None:
    qty_step, min_qty = order_manager.get_symbol_info(symbol)
    if qty_step is None:
        return None
    return _dec(qty_step), _dec(min_qty or 0)


def compute_fixed_notional_qty(
    entry_price: float,
    *,
    qty_step: Decimal,
    min_qty: Decimal,
    notional_cap: Decimal | None = None,
) -> tuple[Decimal | None, dict[str, Any]]:
    cap = notional_cap or TEST_ORDER_NOTIONAL_USDT
    entry = _dec(entry_price)
    if entry <= 0:
        return None, {"error": "invalid_entry"}
    qty_raw = cap / entry
    qty = floor_qty_to_step(qty_raw, qty_step)
    if qty <= 0:
        return None, {"error": "qty_zero_after_floor", "qty_step": str(qty_step)}
    if min_qty > 0 and qty < min_qty:
        min_notional = min_qty * entry
        return None, {
            "error": "below_min_qty",
            "min_qty": str(min_qty),
            "min_notional_usdt": str(min_notional),
        }
    notional = qty * entry
    if notional > cap:
        return None, {
            "error": "notional_exceeds_cap",
            "notional_usdt": str(notional),
            "cap_usdt": str(cap),
        }
    min_notional_needed = min_qty * entry if min_qty > 0 else Decimal("0")
    if min_notional_needed > cap:
        return None, {
            "error": "min_notional_above_cap",
            "min_notional_usdt": str(min_notional_needed),
        }
    return qty, {
        "qty_doge": str(qty),
        "notional_usdt": str(notional),
        "notional_cap_usdt": str(cap),
        "qty_step": str(qty_step),
    }


def amend_notional_ok(existing_qty: float, new_entry: float) -> tuple[bool, str | None]:
    """Open order qty unchanged — check new entry keeps notional ≤ cap."""
    cap = TEST_ORDER_NOTIONAL_USDT
    notional = _dec(existing_qty) * _dec(new_entry)
    if notional > cap:
        return False, f"amend_notional_exceeds_cap:{notional}>{cap}"
    return True, None
