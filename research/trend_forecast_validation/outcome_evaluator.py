"""Causal outcome evaluation: targets, first-touch, MFE/MAE (from t+1 only)."""

from __future__ import annotations

import math
from typing import Any, Iterable

import numpy as np
import pandas as pd

from research.trend_forecast_validation.config import ForecastValidationConfig


def _targets_for_signal(sig: dict[str, Any], cfg: ForecastValidationConfig) -> list[dict[str, Any]]:
    close = float(sig["close"])
    atr = sig.get("ATR")
    atr_v = float(atr) if atr is not None and math.isfinite(float(atr)) else None
    direction = str(sig["forecast_direction"])
    out: list[dict[str, Any]] = []

    for pct in cfg.percent_targets:
        if direction == "bullish":
            px = close * (1.0 + pct / 100.0)
        else:
            px = close * (1.0 - pct / 100.0)
        out.append({"target_id": f"pct_{pct:.2f}", "target_kind": "percent", "target_price": px, "target_param": pct})

    if atr_v and atr_v > 0:
        for mult in cfg.atr_target_multiples:
            if direction == "bullish":
                px = close + mult * atr_v
            else:
                px = close - mult * atr_v
            out.append(
                {
                    "target_id": f"atr_{mult:.1f}",
                    "target_kind": "atr",
                    "target_price": px,
                    "target_param": mult,
                }
            )

    # Structure targets
    if direction == "bullish":
        sh = sig.get("structure_level_high") or sig.get("external_swing_high")
        if sh is not None and math.isfinite(float(sh)) and float(sh) > close:
            out.append(
                {
                    "target_id": "structure_prior_high",
                    "target_kind": "structure",
                    "target_price": float(sh),
                    "target_param": None,
                }
            )
        # New high above signal swing / signal high
        out.append(
            {
                "target_id": "new_high_above_signal_high",
                "target_kind": "structure",
                "target_price": float(sig["high"]),
                "target_param": None,
            }
        )
    else:
        sl = sig.get("structure_level_low") or sig.get("external_swing_low")
        if sl is not None and math.isfinite(float(sl)) and float(sl) < close:
            out.append(
                {
                    "target_id": "structure_prior_low",
                    "target_kind": "structure",
                    "target_price": float(sl),
                    "target_param": None,
                }
            )
        out.append(
            {
                "target_id": "new_low_below_signal_low",
                "target_kind": "structure",
                "target_price": float(sig["low"]),
                "target_param": None,
            }
        )
    return out


def _first_touch_path(
    *,
    direction: str,
    entry: float,
    target: float,
    invalidation: float | None,
    highs: np.ndarray,
    lows: np.ndarray,
    timestamps: list[Any],
    ambiguity_mode: str,
) -> dict[str, Any]:
    """Walk future bars; never uses signal candle."""
    result = {
        "outcome": "OPEN",
        "first_touch": "none",
        "bars_to_target": None,
        "bars_to_invalidation": None,
        "target_timestamp": None,
        "invalidation_timestamp": None,
        "ambiguous": False,
        "optimistic_outcome": None,
        "conservative_outcome": None,
    }
    inv = float(invalidation) if invalidation is not None and math.isfinite(float(invalidation)) else None

    for j in range(len(highs)):
        hi = float(highs[j])
        lo = float(lows[j])
        hit_t = hit_i = False
        if direction == "bullish":
            hit_t = hi >= target
            hit_i = inv is not None and lo <= inv
        else:
            hit_t = lo <= target
            hit_i = inv is not None and hi >= inv

        if hit_t and hit_i:
            result["ambiguous"] = True
            result["first_touch"] = "same_candle"
            result["bars_to_target"] = j + 1
            result["bars_to_invalidation"] = j + 1
            result["target_timestamp"] = str(timestamps[j])
            result["invalidation_timestamp"] = str(timestamps[j])
            result["optimistic_outcome"] = "SUCCESS"
            result["conservative_outcome"] = "FAILURE"
            # Primary report always uses conservative bound; class stays AMBIGUOUS.
            result["outcome"] = "AMBIGUOUS"
            return result

        if hit_t:
            result["outcome"] = "SUCCESS"
            result["first_touch"] = "target"
            result["bars_to_target"] = j + 1
            result["target_timestamp"] = str(timestamps[j])
            return result
        if hit_i:
            result["outcome"] = "FAILURE"
            result["first_touch"] = "invalidation"
            result["bars_to_invalidation"] = j + 1
            result["invalidation_timestamp"] = str(timestamps[j])
            return result
    return result


def _mfe_mae(
    *,
    direction: str,
    entry: float,
    atr: float | None,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
) -> dict[str, Any]:
    if len(highs) == 0 or entry <= 0:
        return {
            "mfe_pct": None,
            "mae_pct": None,
            "mfe_atr": None,
            "mae_atr": None,
            "mfe_to_mae_ratio": None,
            "bars_to_mfe": None,
            "bars_to_mae": None,
            "close_return_pct_at_horizon": None,
        }
    if direction == "bullish":
        fav = (highs - entry) / entry * 100.0
        adv = (entry - lows) / entry * 100.0
        close_ret = (closes[-1] - entry) / entry * 100.0
    else:
        fav = (entry - lows) / entry * 100.0
        adv = (highs - entry) / entry * 100.0
        close_ret = (entry - closes[-1]) / entry * 100.0
    mfe = float(np.max(fav))
    mae = float(np.max(adv))
    bars_mfe = int(np.argmax(fav)) + 1
    bars_mae = int(np.argmax(adv)) + 1
    atr_v = float(atr) if atr and atr > 0 else None
    return {
        "mfe_pct": mfe,
        "mae_pct": mae,
        "mfe_atr": (mfe / 100.0 * entry / atr_v) if atr_v else None,
        "mae_atr": (mae / 100.0 * entry / atr_v) if atr_v else None,
        "mfe_to_mae_ratio": (mfe / mae) if mae > 1e-12 else None,
        "bars_to_mfe": bars_mfe,
        "bars_to_mae": bars_mae,
        "close_return_pct_at_horizon": float(close_ret),
    }


def evaluate_signal_outcomes(
    signals: pd.DataFrame,
    candles_5m: pd.DataFrame,
    cfg: ForecastValidationConfig,
) -> pd.DataFrame:
    """Evaluate every (signal, horizon, target) from forecast_active_from onward."""
    if signals.empty:
        return pd.DataFrame()

    c = candles_5m.copy().reset_index(drop=True)
    c["timestamp"] = pd.to_datetime(c["timestamp"], utc=True)
    ts_list = list(c["timestamp"])
    highs = c["high"].astype(float).to_numpy()
    lows = c["low"].astype(float).to_numpy()
    closes = c["close"].astype(float).to_numpy()
    # Map open timestamp -> index
    idx_by_ts = {pd.Timestamp(t): i for i, t in enumerate(ts_list)}

    rows: list[dict[str, Any]] = []
    for _, sig in signals.iterrows():
        active_from = pd.Timestamp(sig["forecast_active_from"])
        # First future bar: open timestamp == active_from (close of signal = open of next)
        start_i = idx_by_ts.get(active_from)
        if start_i is None:
            # If decision_time not exactly a bar open, take first timestamp >= active_from
            candidates = [i for i, t in enumerate(ts_list) if t >= active_from]
            if not candidates:
                continue
            start_i = candidates[0]

        # Guard: must not include signal bar
        sig_ts = pd.Timestamp(sig["detected_timestamp"])
        if ts_list[start_i] <= sig_ts:
            # advance to strictly after signal open
            start_i = next((i for i, t in enumerate(ts_list) if t > sig_ts), None)
            if start_i is None:
                continue

        entry = float(sig["close"])
        direction = str(sig["forecast_direction"])
        invalidation = sig.get("invalidation_price")
        targets = _targets_for_signal(sig.to_dict(), cfg)

        for horizon in cfg.horizons_bars:
            end_i = min(len(c), start_i + int(horizon))
            if end_i <= start_i:
                continue
            path_h = highs[start_i:end_i]
            path_l = lows[start_i:end_i]
            path_c = closes[start_i:end_i]
            path_t = ts_list[start_i:end_i]
            mfe = _mfe_mae(
                direction=direction,
                entry=entry,
                atr=sig.get("ATR"),
                highs=path_h,
                lows=path_l,
                closes=path_c,
            )
            for tgt in targets:
                touch = _first_touch_path(
                    direction=direction,
                    entry=entry,
                    target=float(tgt["target_price"]),
                    invalidation=float(invalidation) if invalidation is not None else None,
                    highs=path_h,
                    lows=path_l,
                    timestamps=path_t,
                    ambiguity_mode=cfg.ambiguity_mode,
                )
                # Conservative primary for ambiguous
                primary = touch["outcome"]
                if touch["ambiguous"] and cfg.ambiguity_mode == "conservative":
                    primary_success_flag = False
                else:
                    primary_success_flag = primary == "SUCCESS"

                rows.append(
                    {
                        **{k: sig[k] for k in sig.index if k in {
                            "signal_id", "symbol", "signal_type", "forecast_direction",
                            "detected_timestamp", "forecast_active_from", "development_or_oos",
                            "include_in_stats", "major_trend", "regime", "EMA_context",
                            "ADX", "trend_30m", "trend_4h", "HTF_alignment", "close", "ATR",
                            "invalidation_price",
                        }},
                        "horizon_bars": int(horizon),
                        "horizon_label": _horizon_label(horizon),
                        **tgt,
                        **touch,
                        "primary_outcome": primary,
                        "counted_success": bool(primary_success_flag and primary == "SUCCESS"),
                        **mfe,
                    }
                )
    return pd.DataFrame(rows)


def _horizon_label(bars: int) -> str:
    minutes = bars * 5
    if minutes < 60:
        return f"{minutes}m"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def summarize_outcomes(outcomes: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Build summary tables without optimizing/selecting best variants."""
    if outcomes.empty:
        empty = pd.DataFrame()
        return {
            "summary_by_signal": empty,
            "summary_by_horizon": empty,
            "summary_by_target": empty,
            "summary_by_regime": empty,
            "summary_by_htf_alignment": empty,
            "development_vs_oos": empty,
        }

    stats = outcomes.loc[outcomes["include_in_stats"] == True].copy()  # noqa: E712

    def _agg(group: pd.DataFrame) -> dict[str, Any]:
        n = len(group)
        # Deduplicate to one row per signal-horizon-target already; count outcomes
        succ = int((group["primary_outcome"] == "SUCCESS").sum())
        fail = int((group["primary_outcome"] == "FAILURE").sum())
        opn = int((group["primary_outcome"] == "OPEN").sum())
        amb = int((group["primary_outcome"] == "AMBIGUOUS").sum())
        decided = succ + fail + amb
        mfe = pd.to_numeric(group["mfe_pct"], errors="coerce").dropna()
        mae = pd.to_numeric(group["mae_pct"], errors="coerce").dropna()

        def pctile(s: pd.Series, p: float) -> float | None:
            return float(np.percentile(s, p)) if len(s) else None

        return {
            "signal_count": n,
            "success_count": succ,
            "failure_count": fail,
            "open_count": opn,
            "ambiguous_count": amb,
            "success_rate_excluding_open": (succ / decided) if decided else None,
            "success_rate_including_open": (succ / n) if n else None,
            "target_first_rate": float((group["first_touch"] == "target").mean()) if n else None,
            "invalidation_first_rate": float((group["first_touch"] == "invalidation").mean()) if n else None,
            "median_mfe_pct": float(mfe.median()) if len(mfe) else None,
            "mean_mfe_pct": float(mfe.mean()) if len(mfe) else None,
            "median_mae_pct": float(mae.median()) if len(mae) else None,
            "mean_mae_pct": float(mae.mean()) if len(mae) else None,
            "median_mfe_atr": float(pd.to_numeric(group["mfe_atr"], errors="coerce").median()),
            "median_mae_atr": float(pd.to_numeric(group["mae_atr"], errors="coerce").median()),
            "median_mfe_to_mae_ratio": float(pd.to_numeric(group["mfe_to_mae_ratio"], errors="coerce").median()),
            "median_bars_to_target": float(pd.to_numeric(group["bars_to_target"], errors="coerce").median()),
            "median_bars_to_invalidation": float(
                pd.to_numeric(group["bars_to_invalidation"], errors="coerce").median()
            ),
            "mfe_p10": pctile(mfe, 10),
            "mfe_p25": pctile(mfe, 25),
            "mfe_p50": pctile(mfe, 50),
            "mfe_p75": pctile(mfe, 75),
            "mfe_p90": pctile(mfe, 90),
            "mae_p10": pctile(mae, 10),
            "mae_p25": pctile(mae, 25),
            "mae_p50": pctile(mae, 50),
            "mae_p75": pctile(mae, 75),
            "mae_p90": pctile(mae, 90),
        }

    def _grouped(keys: list[str]) -> pd.DataFrame:
        rows = []
        if stats.empty:
            return pd.DataFrame()
        for key, g in stats.groupby(keys, dropna=False):
            if not isinstance(key, tuple):
                key = (key,)
            row = dict(zip(keys, key))
            row.update(_agg(g))
            rows.append(row)
        return pd.DataFrame(rows)

    return {
        "summary_by_signal": _grouped(["signal_type", "forecast_direction", "development_or_oos"]),
        "summary_by_horizon": _grouped(["signal_type", "horizon_bars", "development_or_oos"]),
        "summary_by_target": _grouped(["signal_type", "target_id", "horizon_bars", "development_or_oos"]),
        "summary_by_regime": _grouped(["signal_type", "regime", "development_or_oos"]),
        "summary_by_htf_alignment": _grouped(["signal_type", "HTF_alignment", "development_or_oos"]),
        "development_vs_oos": _grouped(["signal_type", "development_or_oos", "horizon_bars", "target_id"]),
    }


def hedge_relevance_diagnosis(outcomes: pd.DataFrame) -> dict[str, Any]:
    """Diagnostic only — no position simulation."""
    if outcomes.empty:
        return {}
    stats = outcomes.loc[outcomes["include_in_stats"] == True].copy()  # noqa: E712
    out: dict[str, Any] = {}
    for side, stype in (
        ("bullish", "BULLISH_EXTERNAL_BOS_AFTER_PULLBACK"),
        ("bearish", "BEARISH_EXTERNAL_BOS_AFTER_PULLBACK"),
    ):
        sub = stats.loc[stats["signal_type"] == stype]
        # Use a fixed diagnostic slice: horizon 48, percent targets
        diag = {}
        for pct, tid in ((0.25, "pct_0.25"), (0.50, "pct_0.50"), (1.00, "pct_1.00")):
            g = sub.loc[(sub["horizon_bars"] == 48) & (sub["target_id"] == tid)]
            if g.empty:
                # try float formatting variants
                g = sub.loc[(sub["horizon_bars"] == 48) & (sub["target_id"].astype(str).str.contains(str(pct)))]
            n = len(g)
            diag[f"reached_{pct:.2f}_pct_h48"] = {
                "n": n,
                "success_rate_ex_open": (
                    float((g["primary_outcome"] == "SUCCESS").sum() / max(
                        (g["primary_outcome"].isin(["SUCCESS", "FAILURE", "AMBIGUOUS"])).sum(), 1
                    ))
                    if n
                    else None
                ),
                "median_mae_before": float(pd.to_numeric(g["mae_pct"], errors="coerce").median()) if n else None,
                "median_bars_to_target": float(pd.to_numeric(g["bars_to_target"], errors="coerce").median())
                if n
                else None,
                "invalidation_first_rate": float((g["first_touch"] == "invalidation").mean()) if n else None,
            }
        # Early hedge harm proxy: SUCCESS on pct_0.50 with MAE first would be harm — use invalidation_first on path to target
        g50 = sub.loc[(sub["horizon_bars"] == 48) & (sub["target_id"] == "pct_0.50")]
        if g50.empty:
            g50 = sub.loc[(sub["horizon_bars"] == 48) & (sub["target_kind"] == "percent") & (sub["target_param"] == 0.5)]
        diag["early_reduce_likely_harmful_share"] = (
            float((g50["primary_outcome"] == "SUCCESS").mean()) if len(g50) else None
        )
        diag["note"] = (
            "early_reduce_likely_harmful_share ≈ share of paths that still hit +/−0.50% target "
            "before invalidation within 4h — reducing hedge early would forgo that move."
        )
        out[side] = diag
    return out
