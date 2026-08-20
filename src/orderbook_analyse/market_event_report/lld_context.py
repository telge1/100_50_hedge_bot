"""Optional LLD / liquidity-pool context via trading_research_platform."""

from __future__ import annotations

import sys
from datetime import timezone
from pathlib import Path
from typing import Any

import pandas as pd

DEFAULT_TRP = Path("/home/telgenbuescher/projects/trading_research_platform")
NEAR_BPS = 15.0


def _unavailable(reason: str) -> dict[str, Any]:
    return {
        "available": False,
        "reason": reason,
        "nearest_upper_pool": None,
        "nearest_lower_pool": None,
        "distance_upper_bps": None,
        "distance_lower_bps": None,
        "event_interaction": "unavailable",
    }


def _to_utc_aware(ts: Any):
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        return t.tz_localize("UTC").to_pydatetime()
    return t.tz_convert("UTC").to_pydatetime()


def _naive_utc(ts: Any) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    if t.tzinfo is not None:
        return t.tz_convert("UTC").tz_localize(None)
    return t


def build_lld_context(
    candles: pd.DataFrame,
    *,
    symbol: str,
    event_open_time: Any,
    trp_root: Path | None = None,
    near_bps: float = NEAR_BPS,
) -> dict[str, Any]:
    """Causal LLD snapshot at event minute (pools created before event only)."""
    root = Path(trp_root) if trp_root is not None else DEFAULT_TRP
    if not root.exists():
        return _unavailable(f"trp_root_missing:{root}")

    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    try:
        from data.models import Candle
        from data.timeframes import aggregate
        from indicators.liquidity_location import LiquidityLocationConfig
        from indicators.liquidity_location.engine import run_liquidity_location
    except Exception as exc:  # noqa: BLE001 — optional dependency
        return _unavailable(f"import_failed:{type(exc).__name__}:{exc}")

    if candles.empty:
        return _unavailable("no_candles")

    df = candles.sort_values("open_time").reset_index(drop=True).copy()
    df["open_time"] = pd.to_datetime(df["open_time"])
    event_t = pd.Timestamp(event_open_time)
    event_rows = df.loc[df["open_time"] == event_t]
    if event_rows.empty:
        return _unavailable("event_minute_missing")

    try:
        c1 = [
            Candle(
                timestamp=_to_utc_aware(r.open_time),
                open=float(r.open),
                high=float(r.high),
                low=float(r.low),
                close=float(r.close),
                volume=float(getattr(r, "volume", 0.0) or 0.0),
                symbol=symbol,
                timeframe="1m",
            )
            for r in df.itertuples(index=False)
        ]
        bars5 = aggregate(c1, "5m")
        result = run_liquidity_location(
            bars5,
            LiquidityLocationConfig(enabled=True, amount=300, highest_len=2, lowest_len=2),
        )
    except Exception as exc:  # noqa: BLE001
        return _unavailable(f"lld_engine_failed:{type(exc).__name__}:{exc}")

    ev = event_rows.iloc[0]
    px = float(ev["close"])
    hi = float(ev["high"])
    lo = float(ev["low"])
    event_naive = _naive_utc(event_t)

    uppers: list[dict[str, Any]] = []
    lowers: list[dict[str, Any]] = []
    for p in result.pools:
        created = _naive_utc(p.created_timestamp)
        if created >= event_naive:
            continue
        inv = _naive_utc(p.invalidated_timestamp) if p.invalidated_timestamp else None
        if inv is not None and inv <= event_naive:
            continue
        item = {
            "bottom": float(p.bottom_price),
            "top": float(p.top_price),
            "mid": (float(p.bottom_price) + float(p.top_price)) / 2.0,
            "created": str(created),
            "side": p.side,
        }
        if p.side == "upper":
            uppers.append(item)
        else:
            lowers.append(item)

    def dist_bps(level: float, price: float) -> float:
        return (level - price) / price * 10_000.0

    nearest_up = sorted(uppers, key=lambda x: abs(x["mid"] - px))[0] if uppers else None
    nearest_lo = sorted(lowers, key=lambda x: abs(x["mid"] - px))[0] if lowers else None
    dist_up = dist_bps(nearest_up["mid"], px) if nearest_up else None
    dist_lo = dist_bps(nearest_lo["mid"], px) if nearest_lo else None

    post = df.loc[df["open_time"] > event_t].head(60)
    interaction_parts: list[str] = []

    def interact(pool: dict[str, Any], side: str) -> None:
        top, bot, mid = pool["top"], pool["bottom"], pool["mid"]
        if side == "upper":
            if hi >= bot:
                interaction_parts.append("touch_upper")
            if abs(dist_bps(mid, px)) <= near_bps:
                interaction_parts.append("near_upper")
            if hi > top:
                interaction_parts.append("break_upper")
                if not post.empty and float(post.iloc[-1]["close"]) < mid:
                    interaction_parts.append("reclaim_upper")
        else:
            if lo <= top:
                interaction_parts.append("touch_lower")
            if abs(dist_bps(mid, px)) <= near_bps:
                interaction_parts.append("near_lower")
            if lo < bot:
                interaction_parts.append("break_lower")
                if not post.empty and float(post.iloc[-1]["close"]) > mid:
                    interaction_parts.append("reclaim_lower")

    if nearest_up:
        interact(nearest_up, "upper")
    if nearest_lo:
        interact(nearest_lo, "lower")

    return {
        "available": True,
        "reason": None,
        "config": {"amount": 300, "highest_len": 2, "lowest_len": 2, "near_bps": near_bps},
        "n_active_upper_at_event": len(uppers),
        "n_active_lower_at_event": len(lowers),
        "nearest_upper_pool": nearest_up,
        "nearest_lower_pool": nearest_lo,
        "distance_upper_bps": dist_up,
        "distance_lower_bps": dist_lo,
        "event_interaction": "+".join(interaction_parts) if interaction_parts else "none",
        "causality_note": (
            "Pools included only if created_timestamp < event_open_time and not invalidated "
            "before event. Break/reclaim may inspect post-event path and must not be treated "
            "as a pre-event known feature."
        ),
        "timezone": str(timezone.utc),
    }
