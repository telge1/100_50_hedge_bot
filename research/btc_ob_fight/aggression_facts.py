"""Measurable public-trade aggression and price-response facts (no interpretation)."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from .config import iso_z, utc
from .facts import json_safe
from .profile_state_episodes import episode_time_span

AGGRESSION_FACTS_CONTRACT = "aggression_facts_v1"
BALANCED_DELTA_FRAC_HEURISTIC = 0.05  # UNFROZEN — not a trading threshold
CALC_INSUFFICIENT = "INSUFFICIENT_DENOMINATOR"

DIR_NET_BUY = "NET_BUY_AGGRESSION_OBSERVED"
DIR_NET_SELL = "NET_SELL_AGGRESSION_OBSERVED"
DIR_BALANCED = "BALANCED_AGGRESSION_OBSERVED"


def _safe_div(num: float | None, den: float | None) -> float | None:
    if num is None or den is None or den == 0 or not math.isfinite(den):
        return None
    val = num / den
    if not math.isfinite(val):
        return None
    return val


def classify_aggression_direction(
    delta_quote: float,
    total_quote: float,
    *,
    balanced_frac: float = BALANCED_DELTA_FRAC_HEURISTIC,
) -> dict[str, Any]:
    if total_quote <= 0:
        return {
            "direction_observed": DIR_BALANCED,
            "calculation_status": CALC_INSUFFICIENT,
            "balanced_heuristic": "UNFROZEN_REPORTING_HEURISTIC",
            "balanced_frac_threshold": balanced_frac,
        }
    frac = abs(delta_quote) / total_quote
    if frac < balanced_frac:
        direction = DIR_BALANCED
    elif delta_quote > 0:
        direction = DIR_NET_BUY
    else:
        direction = DIR_NET_SELL
    return {
        "direction_observed": direction,
        "calculation_status": "COMPUTED",
        "balanced_heuristic": "UNFROZEN_REPORTING_HEURISTIC",
        "balanced_frac_threshold": balanced_frac,
        "delta_fraction_of_total": frac,
    }


def aggression_for_trades(
    trades: list[dict[str, Any]],
    *,
    label: str | None = None,
) -> dict[str, Any]:
    if not trades:
        return json_safe(
            {
                "label": label,
                "trade_count": 0,
                "taker_buy_quote": 0.0,
                "taker_sell_quote": 0.0,
                "taker_delta_quote": 0.0,
                "buy_fraction": None,
                "sell_fraction": None,
                "price_change_bps": None,
                "range_bps": None,
                "mfe_bps": None,
                "mae_bps": None,
                "delta_per_second": None,
                "quote_per_second": None,
                "price_impact_per_1m_delta": None,
                "calculation_status": CALC_INSUFFICIENT,
                "aggression": classify_aggression_direction(0.0, 0.0),
            }
        )

    xs = sorted(trades, key=lambda t: (t["ts"], t["trade_id"]))
    buy_q = sum(t["notional"] for t in xs if t["side"] == "Buy")
    sell_q = sum(t["notional"] for t in xs if t["side"] == "Sell")
    delta = buy_q - sell_q
    total = buy_q + sell_q
    first_p, last_p = xs[0]["price"], xs[-1]["price"]
    high_p = max(t["price"] for t in xs)
    low_p = min(t["price"] for t in xs)
    chg_bps = (last_p - first_p) / first_p * 10000.0 if first_p else None
    range_bps = (high_p - low_p) / first_p * 10000.0 if first_p else None
    mfe = max((high_p - first_p) / first_p * 10000.0, (first_p - low_p) / first_p * 10000.0) if first_p else None
    mae = min((high_p - first_p) / first_p * 10000.0, (first_p - low_p) / first_p * 10000.0) if first_p else None

    duration = (xs[-1]["ts"] - xs[0]["ts"]).total_seconds()
    duration = max(duration, 1e-9)
    delta_per_s = delta / duration
    quote_per_s = total / duration

    impact = None
    impact_status = "COMPUTED"
    if abs(delta) < 1e-9:
        impact_status = CALC_INSUFFICIENT
    else:
        impact = _safe_div(chg_bps, delta / 1e6)

    return json_safe(
        {
            "label": label,
            "start_ts": iso_z(xs[0]["ts"]),
            "end_ts": iso_z(xs[-1]["ts"]),
            "trade_count": len(xs),
            "taker_buy_quote": buy_q,
            "taker_sell_quote": sell_q,
            "taker_delta_quote": delta,
            "buy_fraction": _safe_div(buy_q, total),
            "sell_fraction": _safe_div(sell_q, total),
            "price_change_bps": chg_bps,
            "range_bps": range_bps,
            "mfe_bps": mfe,
            "mae_bps": mae,
            "delta_per_second": delta_per_s,
            "quote_per_second": quote_per_s,
            "price_impact_per_1m_delta": impact,
            "price_impact_calculation_status": impact_status,
            "aggression": classify_aggression_direction(delta, total),
        }
    )


def build_aggression_buckets(
    trades: list[dict[str, Any]],
    anchor: datetime,
    window_end: datetime,
    *,
    bucket_seconds: int = 60,
) -> list[dict[str, Any]]:
    anchor = utc(anchor)
    window_end = utc(window_end)
    obs = [t for t in trades if anchor <= t["ts"] < window_end]
    buckets: list[dict[str, Any]] = []
    t = anchor
    idx = 0
    while t < window_end:
        nxt = min(t + timedelta(seconds=bucket_seconds), window_end)
        chunk = [x for x in obs if t <= x["ts"] < nxt]
        row = aggression_for_trades(chunk, label=f"bucket_{idx}_{bucket_seconds}s")
        row["bucket_index"] = idx
        row["bucket_start_utc"] = iso_z(t)
        row["bucket_end_utc"] = iso_z(nxt)
        buckets.append(row)
        t = nxt
        idx += 1
    return buckets


def aggression_for_episode(
    episode: dict[str, Any],
    trades: list[dict[str, Any]],
) -> dict[str, Any]:
    start, end = episode_time_span(episode)
    chunk = [t for t in trades if start <= t["ts"] <= end]
    facts = aggression_for_trades(chunk, label=episode.get("episode_id"))
    facts["episode_id"] = episode.get("episode_id")
    facts["profile_state"] = episode.get("state")
    return facts
