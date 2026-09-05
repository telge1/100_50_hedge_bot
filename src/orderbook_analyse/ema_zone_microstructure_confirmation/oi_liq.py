"""OI / liquidation classifying features (no hard gates, no lookahead)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd

from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows import MISSING


def _asof_oi(oi: pd.DataFrame, asof: datetime) -> float | None:
    if oi is None or oi.empty:
        return None
    col = "minute" if "minute" in oi.columns else "bucket_time"
    asof_ts = pd.Timestamp(asof)
    if asof_ts.tzinfo is None:
        asof_ts = asof_ts.tz_localize("UTC")
    prior = oi[pd.to_datetime(oi[col], utc=True) <= asof_ts]
    if prior.empty:
        return None
    return float(prior.iloc[-1]["open_interest"])


def oi_features(
    oi: pd.DataFrame,
    *,
    window_id: str,
    contact_at: datetime | None,
    price_before: float | None,
    price_after: float | None,
    lookback_m: int = 15,
) -> dict[str, Any]:
    """Causal OI change ending at contact (or window center); never uses future OI."""
    row: dict[str, Any] = {
        "window_id": window_id,
        "oi_abs_change": MISSING,
        "oi_rel_change": MISSING,
        "oi_price_combo": MISSING,
        "oi_asof_utc": MISSING,
        "oi_lookback_m": lookback_m,
        "lookahead_safe": True,
    }
    if contact_at is None or oi is None or oi.empty:
        return row
    from datetime import timedelta

    prev_t = contact_at - timedelta(minutes=lookback_m)
    oi_now = _asof_oi(oi, contact_at)
    oi_prev = _asof_oi(oi, prev_t)
    if oi_now is None or oi_prev is None or oi_prev == 0:
        return row
    abs_ch = oi_now - oi_prev
    rel_ch = abs_ch / oi_prev
    combo = "undetermined"
    if price_before is not None and price_after is not None:
        pup = price_after > price_before
        pdown = price_after < price_before
        oup = abs_ch > 0
        odown = abs_ch < 0
        if pup and oup:
            combo = "price_up_oi_up"
        elif pup and odown:
            combo = "price_up_oi_down"
        elif pdown and oup:
            combo = "price_down_oi_up"
        elif pdown and odown:
            combo = "price_down_oi_down"
    row.update(
        {
            "oi_abs_change": abs_ch,
            "oi_rel_change": rel_ch,
            "oi_price_combo": combo,
            "oi_asof_utc": contact_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
    )
    return row


def liquidation_features(
    liq: pd.DataFrame,
    *,
    window_id: str,
    start: datetime,
    end: datetime,
    contact_at: datetime | None,
) -> dict[str, Any]:
    """Long/short liquidation notional in window up to evidence end (no future)."""
    row: dict[str, Any] = {
        "window_id": window_id,
        "liq_long_notional": 0.0,
        "liq_short_notional": 0.0,
        "liq_flush": False,
        "liq_squeeze": False,
        "liq_possible_exhaustion": False,
        "liq_evidence_until": MISSING,
        "lookahead_safe": True,
    }
    if liq is None or liq.empty:
        return row
    evidence_end = contact_at or end
    # only events at or before evidence_end and within window
    tcol = "event_time"
    times = pd.to_datetime(liq[tcol], utc=True)
    mask = (times >= pd.Timestamp(start)) & (times <= pd.Timestamp(evidence_end))
    sub = liq.loc[mask]
    if sub.empty:
        row["liq_evidence_until"] = evidence_end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        return row
    side_col = "side" if "side" in sub.columns else "liquidated_position_side"
    notional_col = "notional" if "notional" in sub.columns else "notional_estimate"
    sides = sub[side_col].astype(str).str.upper()
    # Bybit: Buy = liquidated long, Sell = liquidated short
    long_m = sides.isin(["BUY", "LONG", "LIQUIDATED_LONG", "B"])
    short_m = sides.isin(["SELL", "SHORT", "LIQUIDATED_SHORT", "S"])
    long_n = float(sub.loc[long_m, notional_col].sum()) if long_m.any() else 0.0
    short_n = float(sub.loc[short_m, notional_col].sum()) if short_m.any() else 0.0
    total = long_n + short_n
    flush = total > 0 and (long_n > 2 * short_n or short_n > 2 * long_n)
    # squeeze heuristic: opposing liquidations dominate briefly — classifying only
    squeeze = total > 0 and min(long_n, short_n) > 0 and max(long_n, short_n) / max(min(long_n, short_n), 1) < 1.5
    exhaustion = total > 0 and flush  # possible exhaustion marker; not a gate
    row.update(
        {
            "liq_long_notional": long_n,
            "liq_short_notional": short_n,
            "liq_flush": flush,
            "liq_squeeze": squeeze,
            "liq_possible_exhaustion": exhaustion,
            "liq_evidence_until": evidence_end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
    )
    return row
