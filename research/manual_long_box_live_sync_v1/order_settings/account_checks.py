"""READ-ONLY account gates for manual long box test orders."""

from __future__ import annotations

from typing import Any

from .order_sizing import MARGIN_FEE_BUFFER_USDT, TEST_ORDER_NOTIONAL_USDT


def fetch_symbol_leverage(order_manager, symbol: str) -> float | None:
    try:
        rows = order_manager.fetch_positions_direct(symbol, timeout=5)
    except Exception:
        return None
    for row in rows or []:
        info = row.get("info") or {}
        lev = info.get("leverage")
        if lev is not None and str(lev).strip() != "":
            try:
                return float(lev)
            except (TypeError, ValueError):
                continue
    return None


def verify_leverage_one_x(order_manager, symbol: str) -> dict[str, Any]:
    lev = fetch_symbol_leverage(order_manager, symbol)
    if lev is None:
        return {"ok": False, "error": "leverage_unknown"}
    if abs(lev - 1.0) > 1e-9:
        return {"ok": False, "error": "leverage_not_1x", "leverage": lev}
    return {"ok": True, "leverage": lev}


def verify_available_margin(
    order_manager,
    *,
    required_usdt: float | None = None,
) -> dict[str, Any]:
    need = float(required_usdt or float(TEST_ORDER_NOTIONAL_USDT + MARGIN_FEE_BUFFER_USDT))
    try:
        avail = order_manager.get_account_available_balance()
    except Exception:
        avail = None
    if avail is None:
        return {"ok": False, "error": "margin_unknown"}
    if float(avail) < need:
        return {
            "ok": False,
            "error": "insufficient_margin",
            "available_margin_usdt": float(avail),
            "required_usdt": need,
        }
    return {"ok": True, "available_margin_usdt": float(avail), "required_usdt": need}
