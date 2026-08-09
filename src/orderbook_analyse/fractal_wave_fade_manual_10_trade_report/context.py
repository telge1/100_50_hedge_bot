"""Signal context for selected trades (wave fade / Tier A)."""

from __future__ import annotations

from typing import Any

import pandas as pd

from orderbook_analyse.fractal_all_wave_fade_generalization.annotate import annotate_waves_df
from orderbook_analyse.fractal_parent_lower_tf_quality_db.db_build import build_waves_from_db
from orderbook_analyse.fractal_signal_confluence_db.signals import frozen_eff_edges_all_signal_tfs
from orderbook_analyse.fractal_wave_fade_trend_filter.analysis import assign_trend_bucket


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def build_context_index(symbols: list[str], tfs: list[str]) -> dict[tuple[str, str], pd.DataFrame]:
    edges = frozen_eff_edges_all_signal_tfs()
    out: dict[tuple[str, str], pd.DataFrame] = {}
    for sym in symbols:
        for tf in tfs:
            w = build_waves_from_db(sym, tf)
            if w.empty:
                out[(sym, tf)] = pd.DataFrame()
                continue
            ann = annotate_waves_df(w, symbol=sym, timeframe=tf, quantile_edges=edges)
            ann["trend_bucket"] = assign_trend_bucket(ann)
            ann["is_tier_a"] = (ann["trend_bucket"].astype(str) == "TREND_ALIGNED") & (
                ann["eff_quantile"].astype(str) == "Q4"
            )
            ann["confirmation_available_at"] = pd.to_datetime(
                ann["confirmation_available_at"], utc=True
            )
            out[(sym, tf)] = ann
    return out


def context_for_trade(tr: pd.Series, index: dict[tuple[str, str], pd.DataFrame]) -> dict[str, Any]:
    """Match confirmation_available_at == signal_time (entry_conf in engine)."""
    sym = str(tr["symbol"])
    tf = str(tr["first_signal_tf"])
    side = str(tr["side"])
    # strategy definition
    wave_direction = "DOWN" if side == "LONG" else "UP"
    fade_direction = side

    base = {
        "wave_direction": wave_direction,
        "fade_direction": fade_direction,
        "tier": "A",
        "trend_aligned": "TREND_ALIGNED",
        "directional_efficiency": None,
        "q_bucket": "Q4",
        "signal_available_at": _ts(tr["signal_time"]),
        "context_match": "DERIVED_TIER_A_DEFAULT",
    }

    ann = index.get((sym, tf))
    if ann is None or ann.empty:
        return base

    sig_t = _ts(tr["signal_time"])
    hit = ann.loc[ann["confirmation_available_at"] == sig_t]
    if hit.empty:
        # nearest within 1 minute
        delta = (ann["confirmation_available_at"] - sig_t).abs()
        j = int(delta.idxmin()) if len(delta) else None
        if j is not None and delta.loc[j] <= pd.Timedelta(minutes=1):
            hit = ann.loc[[j]]
        else:
            return base

    row = hit.iloc[0]
    wd = str(row.get("direction", wave_direction))
    fade = str(row.get("side", "LONG" if wd == "DOWN" else "SHORT"))
    base.update(
        {
            "wave_direction": wd,
            "fade_direction": fade,
            "tier": "A" if bool(row.get("is_tier_a", True)) else "OTHER",
            "trend_aligned": str(row.get("trend_bucket", "TREND_ALIGNED")),
            "directional_efficiency": float(row["directional_efficiency"])
            if pd.notna(row.get("directional_efficiency"))
            else None,
            "q_bucket": str(row.get("eff_quantile", "Q4")),
            "context_match": "MATCHED_WAVE_AT_SIGNAL_TIME",
        }
    )
    return base
