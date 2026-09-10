"""Filter states/candidates to usable coverage spans."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def assign_coverage_span_id(
    df: pd.DataFrame,
    usable_spans: list[dict[str, Any]],
    *,
    ts_col: str,
) -> pd.DataFrame:
    """Keep rows whose timestamp falls in a usable span; set coverage_span_id."""
    if df is None or df.empty:
        out = df.copy() if df is not None else pd.DataFrame()
        if "coverage_span_id" not in out.columns:
            out["coverage_span_id"] = []
        return out
    if not usable_spans:
        out = df.iloc[0:0].copy()
        out["coverage_span_id"] = []
        return out

    ts = pd.to_datetime(df[ts_col], utc=True)
    span_id = pd.Series([None] * len(df), index=df.index, dtype=object)
    keep = pd.Series(False, index=df.index)
    for sp in usable_spans:
        a = _parse(sp["start"])
        b = _parse(sp["end"])
        # Prefer final (AVR-ready) eligible start; fall back to base eligible.
        elig = _parse(
            sp.get("final_analysis_eligible_start")
            or sp.get("analysis_eligible_start")
            or sp["start"]
        )
        m = (ts >= elig) & (ts < b)
        # Also allow rows in [a, elig) for state context but mark separately — for candidates use eligible
        span_id = span_id.where(~m, sp["span_id"])
        keep = keep | m
    out = df.loc[keep].copy()
    out["coverage_span_id"] = span_id.loc[keep].values
    return out


def filter_states_to_usable(
    state_df: pd.DataFrame,
    usable_spans: list[dict[str, Any]],
) -> pd.DataFrame:
    """Keep state seconds inside usable spans (full span, not only eligible)."""
    if state_df is None or state_df.empty or not usable_spans:
        return state_df.iloc[0:0].copy() if state_df is not None else pd.DataFrame()
    ts = pd.to_datetime(state_df["state_ts"], utc=True)
    keep = pd.Series(False, index=state_df.index)
    span_id = pd.Series([None] * len(state_df), index=state_df.index, dtype=object)
    for sp in usable_spans:
        a, b = _parse(sp["start"]), _parse(sp["end"])
        m = (ts >= a) & (ts < b)
        keep = keep | m
        span_id = span_id.where(~m, sp["span_id"])
    out = state_df.loc[keep].copy()
    out["coverage_span_id"] = span_id.loc[keep].values
    return out


def episodes_cross_span(episodes: pd.DataFrame) -> bool:
    if episodes is None or episodes.empty or "coverage_span_id" not in episodes.columns:
        return False
    if "episode_id" not in episodes.columns:
        return bool(episodes["coverage_span_id"].nunique(dropna=True) > 1 and len(episodes) > 0)
    mixed = episodes.groupby("episode_id")["coverage_span_id"].nunique(dropna=True)
    return bool((mixed > 1).any())
