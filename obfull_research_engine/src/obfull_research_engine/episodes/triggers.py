"""Primary causal triggers for episode_candidate_v1."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .baseline import BaselineSlice, compare_extreme, robust_stats


def _flag(row: pd.Series, name: str) -> bool:
    v = row.get(name, 0)
    try:
        return int(v) == 1
    except (TypeError, ValueError):
        return bool(v)


def quality_ok_for_triggers(row: pd.Series, cfg: dict[str, Any], trigger_requires: list[str]) -> tuple[bool, str | None]:
    mq = cfg["minimum_quality_requirements"]
    if mq.get("require_feature_row_valid") and not _flag(row, "feature_row_valid"):
        return False, "feature_row_invalid"
    if mq.get("require_book_valid") and "book_valid" in trigger_requires and not _flag(row, "book_valid"):
        return False, "book_invalid"
    if mq.get("require_trades_valid") and "trades_valid" in trigger_requires and not _flag(row, "trades_valid"):
        return False, "trades_invalid"
    if mq.get("require_price_valid") and "price_valid" in trigger_requires and not _flag(row, "price_valid"):
        return False, "price_invalid"
    if "oi_valid" in trigger_requires:
        if mq.get("require_oi_valid_for_oi_triggers") and not _flag(row, "oi_valid"):
            return False, "oi_invalid"
        max_age = int(mq.get("max_open_interest_age_ms_for_oi", 5000))
        age = int(row.get("open_interest_age_ms", 10**9))
        if age > max_age:
            return False, "oi_stale"
    if "liquidations_valid" in trigger_requires:
        if mq.get("require_liquidations_valid_for_liq_triggers") and not _flag(row, "liquidations_valid"):
            return False, "liquidations_invalid"
    return True, None


def _feature_value(row: pd.Series, feature: str) -> float:
    if feature == "abs_taker_notional":
        return float(abs(row["taker_buy_notional_usdt"]) + abs(row["taker_sell_notional_usdt"]))
    if feature == "abs_mid_return_1s_past_bps":
        v = row.get("mid_return_1s_past_bps")
        return float(abs(v)) if v is not None and pd.notna(v) else float("nan")
    v = row.get(feature)
    if v is None or (isinstance(v, float) and np.isnan(v)) or pd.isna(v):
        return float("nan")
    return float(v)


def _baseline_feature_array(df: pd.DataFrame, start: int, end: int, feature: str) -> np.ndarray:
    if feature == "abs_taker_notional":
        b = df["taker_buy_notional_usdt"].iloc[start:end].to_numpy(dtype=float)
        s = df["taker_sell_notional_usdt"].iloc[start:end].to_numpy(dtype=float)
        return np.abs(b) + np.abs(s)
    if feature == "abs_mid_return_1s_past_bps":
        return np.abs(df["mid_return_1s_past_bps"].iloc[start:end].to_numpy(dtype=float))
    return df[feature].iloc[start:end].to_numpy(dtype=float)


def evaluate_primary_triggers(
    *,
    df: pd.DataFrame,
    i: int,
    row: pd.Series,
    baseline: BaselineSlice,
    cfg: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Return map trigger_name -> {fired, detail...}. Causally uses baseline [start,i)."""
    results: dict[str, dict[str, Any]] = {}
    z_thr = float(cfg["robust_z_threshold"])
    q_thr = float(cfg["quantile_threshold"])
    mad_scale = float(cfg["mad_scale"])
    primaries = cfg["primary_triggers"]

    if not baseline.valid:
        for name in primaries:
            results[name] = {"fired": False, "reason": baseline.invalid_reason or "baseline_invalid"}
        return results

    for name, spec in primaries.items():
        requires = list(spec.get("require") or [])
        ok, reason = quality_ok_for_triggers(row, cfg, requires)
        if not ok:
            results[name] = {"fired": False, "reason": reason}
            continue

        kind = spec.get("kind")
        if kind == "compound_primary":
            results[name] = _compound_primary(df, i, row, baseline, cfg, name)
            continue

        feature = spec["feature"]
        compare = spec["compare"]
        x = _feature_value(row, feature)
        if spec.get("require_positive_value") and (not np.isfinite(x) or x <= 0):
            results[name] = {"fired": False, "reason": "non_positive_value"}
            continue
        arr = _baseline_feature_array(df, baseline.start_idx, baseline.end_idx_exclusive, feature)
        stats = robust_stats(arr, q_thr)
        cmp = compare_extreme(x=x, stats=stats, compare=compare, z_threshold=z_thr, mad_scale=mad_scale)
        results[name] = {
            "fired": bool(cmp["fired"]),
            "reason": cmp["reason"],
            "z": cmp["z"],
            "feature": feature,
            "x": x,
            "median": stats.median,
            "mad": stats.mad,
            "q_hi": stats.q_hi,
            "q_lo": stats.q_lo,
        }
    return results


def _compound_primary(
    df: pd.DataFrame,
    i: int,
    row: pd.Series,
    baseline: BaselineSlice,
    cfg: dict[str, Any],
    name: str,
) -> dict[str, Any]:
    z_thr = float(cfg["robust_z_threshold"])
    q_thr = float(cfg["quantile_threshold"])
    mad_scale = float(cfg["mad_scale"])
    params = cfg["compound_primary_params"][name]

    if name == "HIGH_VOLUME_LOW_PRICE_RESPONSE":
        vol_x = _feature_value(row, params["volume_feature"])
        px_x = _feature_value(row, params["price_feature"])
        vol_stats = robust_stats(
            _baseline_feature_array(df, baseline.start_idx, baseline.end_idx_exclusive, params["volume_feature"]),
            q_thr,
        )
        px_stats = robust_stats(
            _baseline_feature_array(df, baseline.start_idx, baseline.end_idx_exclusive, params["price_feature"]),
            q_thr,
        )
        vol = compare_extreme(x=vol_x, stats=vol_stats, compare="high", z_threshold=z_thr, mad_scale=mad_scale)
        # low price response: z low or below median band
        px = compare_extreme(x=px_x, stats=px_stats, compare="not_high", z_threshold=z_thr, mad_scale=mad_scale)
        # additionally require price abs not extreme-high
        px_high = compare_extreme(x=px_x, stats=px_stats, compare="high", z_threshold=z_thr, mad_scale=mad_scale)
        fired = bool(vol["fired"] and px["fired"] and not px_high["fired"])
        return {
            "fired": fired,
            "reason": "ok" if fired else "compound_not_met",
            "volume": vol,
            "price": px,
        }

    if name == "LOW_VISIBLE_RESISTANCE_HIGH_RESPONSE":
        # up path: ask depth low + price up extreme
        ask_x = _feature_value(row, params["up_depth_feature"])
        bid_x = _feature_value(row, params["down_depth_feature"])
        ret = _feature_value(row, "mid_return_1s_past_bps")
        ask_stats = robust_stats(
            _baseline_feature_array(df, baseline.start_idx, baseline.end_idx_exclusive, params["up_depth_feature"]),
            q_thr,
        )
        bid_stats = robust_stats(
            _baseline_feature_array(df, baseline.start_idx, baseline.end_idx_exclusive, params["down_depth_feature"]),
            q_thr,
        )
        ret_stats = robust_stats(
            _baseline_feature_array(df, baseline.start_idx, baseline.end_idx_exclusive, "mid_return_1s_past_bps"),
            q_thr,
        )
        ask_low = compare_extreme(x=ask_x, stats=ask_stats, compare="low", z_threshold=z_thr, mad_scale=mad_scale)
        bid_low = compare_extreme(x=bid_x, stats=bid_stats, compare="low", z_threshold=z_thr, mad_scale=mad_scale)
        up = compare_extreme(x=ret, stats=ret_stats, compare="high", z_threshold=z_thr, mad_scale=mad_scale)
        down = compare_extreme(x=ret, stats=ret_stats, compare="low", z_threshold=z_thr, mad_scale=mad_scale)
        fired = bool((ask_low["fired"] and up["fired"]) or (bid_low["fired"] and down["fired"]))
        return {
            "fired": fired,
            "reason": "ok" if fired else "compound_not_met",
            "ask_low": ask_low,
            "bid_low": bid_low,
            "up": up,
            "down": down,
        }

    return {"fired": False, "reason": f"unknown_compound:{name}"}
