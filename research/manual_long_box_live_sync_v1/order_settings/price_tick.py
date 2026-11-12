"""Canonical Bybit tick rounding (same strategy as BybitOrderManager.amend_order_direct)."""

from __future__ import annotations

from typing import Any


def tick_decimal_places(tick_size: float | None) -> int:
    if not tick_size:
        return 6
    s = f"{float(tick_size):.18f}".rstrip("0")
    if s.endswith("."):
        s = s[:-1]
    if "." in s:
        return len(s.split(".", 1)[1])
    return 0


def round_price_to_tick(price: float | None, tick_size: float | None) -> float | None:
    if price is None:
        return None
    if not tick_size or tick_size <= 0:
        return round(float(price), 6)
    ts = float(tick_size)
    px = float(price)
    dec = tick_decimal_places(ts)
    stepped = round(round(px / ts) * ts, dec)
    return stepped


def prices_equal_tick(
    a: float | str | None,
    b: float | str | None,
    tick_size: float | None,
) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    ra = round_price_to_tick(float(a), tick_size)
    rb = round_price_to_tick(float(b), tick_size)
    if ra is None or rb is None:
        return False
    return ra == rb


def fetch_tick_size_public(
    symbol: str,
    *,
    base_url: str = "https://api.bybit.com",
) -> float | None:
    """Instruments-info (no auth) — same source as BybitOrderManager.get_symbol_tick_size."""
    try:
        import requests

        sym = symbol.upper()
        url = f"{base_url.rstrip('/')}/v5/market/instruments-info?category=linear&symbol={sym}"
        data = requests.get(url, timeout=10).json()
        if data.get("retCode") == 0:
            row = (data.get("result") or {}).get("list") or []
            if row:
                pf = row[0].get("priceFilter") or {}
                ts = pf.get("tickSize")
                if ts not in (None, ""):
                    return float(ts)
    except Exception:
        pass
    return None


def resolve_symbol_tick_size(order_manager: Any, symbol: str) -> float | None:
    if order_manager is None:
        return None
    try:
        ts = order_manager.get_symbol_tick_size(symbol.upper())
        if ts:
            return float(ts)
    except Exception:
        pass
    return None


def resolve_tick_size_for_symbol(
    settings: Any,
    symbol: str,
    *,
    order_manager: Any | None = None,
) -> tuple[float | None, str]:
    """OM tick first, then public instruments-info. Returns (tick_size, source)."""
    sym = symbol.upper()
    om = order_manager
    if om is None:
        try:
            from .credentials import build_order_manager

            if getattr(settings, "api_key", None) and getattr(settings, "api_secret", None):
                om = build_order_manager(settings, require_armed=False)
        except Exception:
            om = None
    ts = resolve_symbol_tick_size(om, sym) if om is not None else None
    if ts:
        return float(ts), "bybit_order_manager"
    base = "https://api-testnet.bybit.com"
    if str(getattr(settings, "environment", "mainnet")).lower() == "mainnet":
        base = "https://api.bybit.com"
    pub = fetch_tick_size_public(sym, base_url=base)
    if pub:
        return float(pub), "instruments_info_public"
    return None, "missing"


def normalize_box_prices(
    box: dict[str, float],
    tick_size: float | None,
) -> dict[str, float]:
    if not tick_size or tick_size <= 0:
        raise ValueError("tick_size_required")
    return {
        "entry_price": float(round_price_to_tick(float(box["entry_price"]), tick_size) or 0),
        "stop_price": float(round_price_to_tick(float(box["stop_price"]), tick_size) or 0),
        "target_price": float(round_price_to_tick(float(box["target_price"]), tick_size) or 0),
    }


def build_amend_price_snapshot(
    *,
    chart_raw: dict[str, float],
    exchange_current: dict[str, float | None],
    tick_size: float,
    tick_source: str,
) -> dict[str, Any]:
    normalized = normalize_box_prices(
        {
            "entry_price": chart_raw["entry_price"],
            "stop_price": chart_raw["stop_price"],
            "target_price": chart_raw["target_price"],
        },
        tick_size,
    )
    changes = {
        "entry_price": not prices_equal_tick(
            exchange_current.get("entry_price"), normalized["entry_price"], tick_size
        ),
        "stop_loss": not prices_equal_tick(
            exchange_current.get("stop_loss"), normalized["stop_price"], tick_size
        ),
        "take_profit": not prices_equal_tick(
            exchange_current.get("take_profit"), normalized["target_price"], tick_size
        ),
    }
    return {
        "tick_size": tick_size,
        "tick_source": tick_source,
        "chart_raw": dict(chart_raw),
        "normalized_targets": {
            "entry_price": normalized["entry_price"],
            "stop_loss": normalized["stop_price"],
            "take_profit": normalized["target_price"],
        },
        "exchange_snapshot": {
            "entry_price": exchange_current.get("entry_price"),
            "stop_loss": exchange_current.get("stop_loss"),
            "take_profit": exchange_current.get("take_profit"),
        },
        "changes": changes,
        "snapshot_version": 2,
    }
