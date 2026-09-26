"""TP selection with 5m default and 1m fallback when 5m pools are too far."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from bot.forward_test.config import (
    MAX_5M_TP_ROOM_PCT,
    MIN_TP_ROOM_PCT,
    POOL_LOOKBACK_HOURS,
    POOL_LOOKFORWARD_HOURS,
)


def _ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def load_active_lower_pools(
    symbol: str,
    as_of: datetime,
    *,
    timeframe: str = "5m",
    lookback_hours: int = POOL_LOOKBACK_HOURS,
    lookforward_hours: int = POOL_LOOKFORWARD_HOURS,
) -> list[Any]:
    """ACTIVE lower pools for a timeframe (5m or 1m)."""
    from dashboard.research_charts.service import liquidity_location_overlay_bundle
    from ob_microstructure_breakout_bot.exit_backtest.cluster_mass import PoolSnap

    as_of = _ensure_utc(as_of)
    start = int((as_of - timedelta(hours=lookback_hours)).timestamp())
    end = int((as_of + timedelta(hours=lookforward_hours)).timestamp())
    payload = liquidity_location_overlay_bundle(
        symbol=symbol,
        timeframe=timeframe,
        start=start,
        end=end,
        allow_stale=True,
        liquidity_location_as_of=as_of.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    pools: list[Any] = []
    for ov in (payload.get("liquidity") or {}).get("overlays") or []:
        md = ov.get("metadata") or {}
        if md.get("side") != "lower":
            continue
        if str(md.get("pool_status") or "").upper() != "ACTIVE":
            continue
        bottom = ov.get("bottom_price")
        top = ov.get("top_price")
        if bottom is None or top is None:
            continue
        pools.append(
            PoolSnap(
                bottom=float(bottom),
                top=float(top),
                strength=float(md.get("strength") or 0.0),
                pool_id=str(md.get("pool_id") or ov.get("id") or ""),
            )
        )
    pools.sort(key=lambda p: (p.top, p.bottom), reverse=True)
    return pools


def select_tp_with_1m_fallback(
    *,
    symbol: str,
    entry_ts: datetime,
    entry_price: float,
    min_room_pct: float = MIN_TP_ROOM_PCT,
    max_5m_room_pct: float = MAX_5M_TP_ROOM_PCT,
) -> dict[str, Any]:
    """Pick TP from 5m pools, or 1m if 5m TP is missing / too far below.

    Rule:
    - 5m remains the setup timeframe.
    - If the nearest usable 5m lower TP is missing or Entry→TP room is too large
      (pools are roughly as deep as the pump origin), switch to 1m pools for TP.
    """
    from ob_microstructure_breakout_bot.exit_backtest.pool_bounce_backtest import (
        select_tp_lower_pool,
    )

    result: dict[str, Any] = {
        "ok": False,
        "tp_price": None,
        "tp_room_pct": None,
        "tp_timeframe": None,
        "tp_source": None,
        "reason": "",
        "five_m_tp_price": None,
        "five_m_tp_room_pct": None,
    }

    lower_5m = load_active_lower_pools(symbol, entry_ts, timeframe="5m")
    _gap5, tp5, room5 = select_tp_lower_pool(
        pool_bottom=entry_price,
        pool_top=entry_price,
        lower_pools=lower_5m,
        min_room_pct=min_room_pct,
    )
    result["five_m_tp_price"] = float(tp5) if tp5 is not None else None
    result["five_m_tp_room_pct"] = float(room5) if room5 is not None else None

    use_1m = False
    if tp5 is None or room5 is None:
        use_1m = True
        result["reason"] = "no_usable_5m_tp"
    elif float(room5) > float(max_5m_room_pct):
        use_1m = True
        result["reason"] = "5m_tp_too_far"

    if not use_1m:
        result.update(
            {
                "ok": True,
                "tp_price": float(tp5),
                "tp_room_pct": float(room5),
                "tp_timeframe": "5m",
                "tp_source": "5m_lower_pool",
                "reason": "5m_tp_ok",
            }
        )
        return result

    lower_1m = load_active_lower_pools(symbol, entry_ts, timeframe="1m")
    _gap1, tp1, room1 = select_tp_lower_pool(
        pool_bottom=entry_price,
        pool_top=entry_price,
        lower_pools=lower_1m,
        min_room_pct=min_room_pct,
    )
    if tp1 is None or room1 is None:
        result["reason"] = f"{result['reason']}|no_usable_1m_tp"
        return result

    result.update(
        {
            "ok": True,
            "tp_price": float(tp1),
            "tp_room_pct": float(room1),
            "tp_timeframe": "1m",
            "tp_source": "1m_lower_pool_fallback",
            "reason": result["reason"] or "1m_fallback",
        }
    )
    return result


def load_active_upper_pools(
    symbol: str,
    as_of: datetime,
    *,
    timeframe: str = "5m",
    lookback_hours: int = POOL_LOOKBACK_HOURS,
    lookforward_hours: int = POOL_LOOKFORWARD_HOURS,
) -> list[Any]:
    """ACTIVE upper pools for a timeframe (5m or 1m)."""
    from dashboard.research_charts.service import liquidity_location_overlay_bundle
    from ob_microstructure_breakout_bot.exit_backtest.cluster_mass import PoolSnap

    as_of = _ensure_utc(as_of)
    start = int((as_of - timedelta(hours=lookback_hours)).timestamp())
    end = int((as_of + timedelta(hours=lookforward_hours)).timestamp())
    payload = liquidity_location_overlay_bundle(
        symbol=symbol,
        timeframe=timeframe,
        start=start,
        end=end,
        allow_stale=True,
        liquidity_location_as_of=as_of.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    pools: list[Any] = []
    for ov in (payload.get("liquidity") or {}).get("overlays") or []:
        md = ov.get("metadata") or {}
        if md.get("side") != "upper":
            continue
        if str(md.get("pool_status") or "").upper() != "ACTIVE":
            continue
        bottom = ov.get("bottom_price")
        top = ov.get("top_price")
        if bottom is None or top is None:
            continue
        pools.append(
            PoolSnap(
                bottom=float(bottom),
                top=float(top),
                strength=float(md.get("strength") or 0.0),
                pool_id=str(md.get("pool_id") or ov.get("id") or ""),
            )
        )
    pools.sort(key=lambda p: (p.bottom, p.top))
    return pools


def select_tp_long_with_1m_fallback(
    *,
    symbol: str,
    entry_ts: datetime,
    entry_price: float,
    min_room_pct: float = MIN_TP_ROOM_PCT,
    max_5m_room_pct: float = MAX_5M_TP_ROOM_PCT,
) -> dict[str, Any]:
    """Pick long TP from 5m upper pools, or 1m if 5m TP is missing / too far."""
    from ob_microstructure_breakout_bot.exit_backtest.pool_bounce_long_backtest import (
        select_tp_upper_pool,
    )

    result: dict[str, Any] = {
        "ok": False,
        "tp_price": None,
        "tp_room_pct": None,
        "tp_timeframe": None,
        "tp_source": None,
        "reason": "",
        "five_m_tp_price": None,
        "five_m_tp_room_pct": None,
    }

    upper_5m = load_active_upper_pools(symbol, entry_ts, timeframe="5m")
    tp5, room5 = select_tp_upper_pool(
        entry_price=entry_price,
        upper_pools=upper_5m,
        min_room_pct=min_room_pct,
    )
    result["five_m_tp_price"] = float(tp5) if tp5 is not None else None
    result["five_m_tp_room_pct"] = float(room5) if room5 is not None else None

    use_1m = False
    if tp5 is None or room5 is None:
        use_1m = True
        result["reason"] = "no_usable_5m_tp"
    elif float(room5) > float(max_5m_room_pct):
        use_1m = True
        result["reason"] = "5m_tp_too_far"

    if not use_1m:
        result.update(
            {
                "ok": True,
                "tp_price": float(tp5),
                "tp_room_pct": float(room5),
                "tp_timeframe": "5m",
                "tp_source": "5m_upper_pool",
                "reason": "5m_tp_ok",
            }
        )
        return result

    upper_1m = load_active_upper_pools(symbol, entry_ts, timeframe="1m")
    tp1, room1 = select_tp_upper_pool(
        entry_price=entry_price,
        upper_pools=upper_1m,
        min_room_pct=min_room_pct,
    )
    if tp1 is None or room1 is None:
        result["reason"] = f"{result['reason']}|no_usable_1m_tp"
        return result

    result.update(
        {
            "ok": True,
            "tp_price": float(tp1),
            "tp_room_pct": float(room1),
            "tp_timeframe": "1m",
            "tp_source": "1m_upper_pool_fallback",
            "reason": result["reason"] or "1m_fallback",
        }
    )
    return result
