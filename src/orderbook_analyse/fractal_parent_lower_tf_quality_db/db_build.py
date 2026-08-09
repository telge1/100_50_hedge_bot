"""Build waves + Tier-A parents + lower-TF as-of state from MySQL only."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_all_wave_fade_generalization.annotate import annotate_waves_df
from orderbook_analyse.fractal_cycle_wave_analysis.indicators import attach_indicators
from orderbook_analyse.fractal_cycle_wave_analysis.load_mysql import load_mysql_ohlcv_tf
from orderbook_analyse.fractal_cycle_wave_analysis.waves import segment_stoch_waves
from orderbook_analyse.fractal_directional_control.load_join import asof_last_completed
from orderbook_analyse.fractal_parent_lower_tf_quality_db import (
    APT_IS_END,
    ENV_FILE,
    LOWER_TFS,
    PARENT_TFS,
)
from orderbook_analyse.fractal_wave_fade_trend_filter.analysis import assign_trend_bucket
from orderbook_analyse.trend_scanner_mysql_feather_parity.load import load_env_file


def phase_from_dir_zone(direction: Any, zone: Any) -> str | None:
    d, z = str(direction), str(zone)
    mapping = {
        ("UP", "LOW"): "LOW_UP",
        ("UP", "MID"): "MID_UP",
        ("UP", "HIGH"): "HIGH_UP",
        ("DOWN", "HIGH"): "HIGH_DOWN",
        ("DOWN", "MID"): "MID_DOWN",
        ("DOWN", "LOW"): "LOW_DOWN",
    }
    return mapping.get((d, z))


def build_waves_from_db(symbol: str, timeframe: str) -> pd.DataFrame:
    load_env_file(ENV_FILE)
    raw = load_mysql_ohlcv_tf(symbol=symbol, timeframe=timeframe, env_file=ENV_FILE)
    if raw.empty:
        return pd.DataFrame()
    ind = attach_indicators(raw)
    waves = segment_stoch_waves(ind)
    if waves.empty:
        return waves
    waves["symbol"] = symbol
    waves["timeframe"] = timeframe
    for c in ("start_available_at", "end_available_at"):
        if c in waves.columns:
            waves[c] = pd.to_datetime(waves[c], utc=True)
    return waves.sort_values("end_available_at").reset_index(drop=True)


def frozen_eff_edges_from_apt_db() -> dict[tuple[str, str, str], dict[float, float]]:
    """Recompute frozen APT-IS efficiency quartile edges from MySQL (no CSV)."""
    load_env_file(ENV_FILE)
    is_end = pd.Timestamp(APT_IS_END)
    edges: dict[tuple[str, str, str], dict[float, float]] = {}
    for tf in PARENT_TFS:
        w = build_waves_from_db("APTUSDT", tf)
        w = w[w["end_available_at"] <= is_end]
        for direction, g in w.groupby(w["direction"].astype(str)):
            s = g["directional_efficiency"].astype(float).dropna()
            q = s.quantile([0.25, 0.5, 0.75])
            edges[(tf, str(direction), "directional_efficiency")] = {
                0.25: float(q.loc[0.25]),
                0.5: float(q.loc[0.5]),
                0.75: float(q.loc[0.75]),
            }
            s2 = g["signed_price_move_pct"].astype(float).dropna()
            q2 = s2.quantile([0.25, 0.5, 0.75])
            edges[(tf, str(direction), "signed_price_move_pct")] = {
                0.25: float(q2.loc[0.25]),
                0.5: float(q2.loc[0.5]),
                0.75: float(q2.loc[0.75]),
            }
    return edges


def build_tier_a_parents(
    symbol: str,
    timeframe: str,
    edges: dict[tuple[str, str, str], dict[float, float]],
    waves: pd.DataFrame | None = None,
) -> pd.DataFrame:
    w = waves if waves is not None else build_waves_from_db(symbol, timeframe)
    if w.empty:
        return pd.DataFrame()
    ann = annotate_waves_df(w, symbol=symbol, timeframe=timeframe, quantile_edges=edges)
    ann["trend_bucket"] = assign_trend_bucket(ann)
    tier_a = ann[
        (ann["trend_bucket"].astype(str) == "TREND_ALIGNED")
        & (ann["eff_quantile"].astype(str) == "Q4")
    ].copy()
    return tier_a.reset_index(drop=True)


WAVE_ASOF_COLS = ["direction", "stoch_zone_end", "stoch_k_end", "end_available_at"]


def assign_quality_class(side: str, zones: list[str], phases: list[str]) -> dict[str, Any]:
    """Deterministic a-priori rule (see QUALITY_RULE_DOC)."""
    exhausted = 0
    favorable = 0
    for z, ph in zip(zones, phases):
        z, ph = str(z), str(ph)
        if side == "SHORT":
            if z == "LOW":
                exhausted += 1
            if ph in ("HIGH_UP", "HIGH_DOWN", "MID_DOWN") or z == "HIGH":
                favorable += 1
        else:
            if z == "HIGH":
                exhausted += 1
            if ph in ("LOW_DOWN", "LOW_UP", "MID_UP") or z == "LOW":
                favorable += 1
    if exhausted == 0 and favorable >= 2:
        q = "A_PLUS_TIMING"
    elif exhausted >= 2:
        q = "A_MINUS_TIMING"
    else:
        q = "A_TIMING"
    return {
        "quality_class": q,
        "exhausted_count": int(exhausted),
        "favorable_count": int(favorable),
    }


def attach_lower_tf_quality(
    parents: pd.DataFrame,
    waves_by_tf: dict[str, pd.DataFrame],
    parent_tf: str,
) -> pd.DataFrame:
    out = parents.reset_index(drop=True).copy()
    times = pd.to_datetime(out["confirmation_available_at"], utc=True).to_numpy(
        dtype="datetime64[ns]"
    )
    lower = LOWER_TFS[parent_tf]
    zones_all: list[list[str]] = [[] for _ in range(len(out))]
    phases_all: list[list[str]] = [[] for _ in range(len(out))]

    for tf in lower:
        pref = f"ltf_{tf}"
        joined = asof_last_completed(waves_by_tf[tf], times, WAVE_ASOF_COLS, pref)
        for c in joined.columns:
            out[c] = joined[c].to_numpy()
        out[f"{pref}_zone"] = out[f"{pref}_stoch_zone_end"]
        out[f"{pref}_stoch_k"] = out[f"{pref}_stoch_k_end"]
        phases = [
            phase_from_dir_zone(d, z)
            for d, z in zip(out[f"{pref}_direction"], out[f"{pref}_zone"])
        ]
        out[f"{pref}_phase"] = phases
        for i, (z, ph) in enumerate(zip(out[f"{pref}_zone"], phases)):
            zones_all[i].append(z)
            phases_all[i].append(ph)

    quals = [
        assign_quality_class(str(side), zs, ps)
        for side, zs, ps in zip(out["side"], zones_all, phases_all)
    ]
    out["quality_class"] = [q["quality_class"] for q in quals]
    out["exhausted_count"] = [q["exhausted_count"] for q in quals]
    out["favorable_count"] = [q["favorable_count"] for q in quals]
    return out
