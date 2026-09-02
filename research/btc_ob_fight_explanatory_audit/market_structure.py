"""Market structure: candles, swings, retest classification."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from research.btc_ob_fight.config import iso_z

from .config import ANCHOR, CORE_END, CORE_START, TPO_VAH, UPPER_OUTER, VOLUME_VVAH


def build_market_structure(
    trades: list[dict[str, Any]],
    *,
    peak_ts: datetime,
    peak_price: float,
    reclaim_ts: datetime,
    reclaim_price: float,
    extended_trades: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """30m bracket candle at anchor + swing/retest structure."""
    bracket_start = ANCHOR - timedelta(minutes=30)
    bracket_trades = [t for t in trades if bracket_start <= t["ts"] < ANCHOR + timedelta(minutes=30)]
    if not bracket_trades:
        bracket_trades = [t for t in trades if CORE_START <= t["ts"] < CORE_END]

    o = bracket_trades[0]["price"]
    c = bracket_trades[-1]["price"]
    h = max(t["price"] for t in bracket_trades)
    l = min(t["price"] for t in bracket_trades)
    body = c - o
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    span = h - l if h > l else 1e-9
    close_loc = (c - l) / span

    # Prior swing high before anchor (30m lookback)
    pre = [t for t in trades if CORE_START <= t["ts"] < ANCHOR]
    prior_swing_high = max((t["price"] for t in pre), default=None)

    ext = extended_trades or []
    retest_high = None
    retest_ts = None
    if ext:
        retest_high = max(t["price"] for t in ext)
        retest_ts = max(ext, key=lambda t: t["price"])["ts"]

    retest_class = "AMBIGUOUS"
    if retest_high is not None and peak_price:
        diff_bps = (retest_high - peak_price) / peak_price * 10000.0
        if retest_high > peak_price + 0.05:
            retest_class = "HIGHER_HIGH"
        elif abs(retest_high - peak_price) <= 0.05:
            retest_class = "EQUAL_HIGH"
        else:
            retest_class = "LOWER_HIGH"

    return {
        "swing_definition": "local extrema not optimized; first_attack_peak=max trade price in core post-anchor; retest_high=max in extended post-core",
        "breakout_candle_30m": {
            "bracket_start": iso_z(bracket_start),
            "bracket_end": iso_z(ANCHOR + timedelta(minutes=30)),
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "body": body,
            "upper_wick": upper_wick,
            "lower_wick": lower_wick,
            "close_location_fraction": close_loc,
        },
        "profile_levels": {
            "tpo_vah": TPO_VAH,
            "volume_vvah": VOLUME_VVAH,
            "upper_outer_edge": UPPER_OUTER,
        },
        "first_price_peak": {"ts": iso_z(peak_ts), "price": peak_price},
        "prior_swing_high_before_anchor": prior_swing_high,
        "reclaim": {"ts": iso_z(reclaim_ts), "price": reclaim_price},
        "later_retest": {
            "within_standard_30m_window": retest_ts <= CORE_END if retest_ts else False,
            "status_if_outside": "NOT_AVAILABLE_TO_STANDARD_30M_DECISION_WINDOW",
            "retest_high_ts": iso_z(retest_ts) if retest_ts else None,
            "retest_high_price": retest_high,
            "peak_price": peak_price,
            "diff_price": (retest_high - peak_price) if retest_high else None,
            "diff_bps": ((retest_high - peak_price) / peak_price * 10000.0) if retest_high and peak_price else None,
            "classification": retest_class,
            "higher_high_achieved": retest_class == "HIGHER_HIGH",
        },
        "return_below_vah_after_reclaim": _return_below_level(trades, reclaim_ts, TPO_VAH),
        "return_below_vvah_after_reclaim": _return_below_level(trades, reclaim_ts, VOLUME_VVAH),
    }


def _return_below_level(trades: list[dict[str, Any]], after: datetime, level: float) -> bool | None:
    post = [t for t in trades if t["ts"] > after]
    if not post:
        return None
    return any(t["price"] < level for t in post)
