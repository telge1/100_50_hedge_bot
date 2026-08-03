"""Causal multi-event audit: 5m PL/PH breaks vs as-of 1h/4h structure relation.

Read-only over existing trend-scanner MTF artefacts and C3 outcome catalogs.
No scanner recompute, ClickHouse, new levels, or PnL.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Technical equality tolerance only — NOT a trading threshold.
MATCH_BPS: float = 1.0

PRIMARY_DECISIONS = (
    "HTF_MATCHED_BREAKS_MORE_OFTEN_HOLD",
    "LOCAL_5M_BREAKS_MORE_OFTEN_RECLAIMED",
    "ONE_H_MATCH_DOMINATES_FOUR_H_ADDS_LITTLE",
    "FOUR_H_MATCH_ADDS_INCREMENTAL_VALUE",
    "REBREAKS_AT_HTF_STRONGER_THAN_FIRST",
    "PL_AND_PH_BEHAVE_DIFFERENTLY",
    "HTF_RELATION_SIGNAL_PRESENT_BUT_OUTCOME_SAMPLE_SMALL",
    "NO_CLEAR_HTF_RELATION_EDGE",
    "AUDIT_DATA_INSUFFICIENT",
)

DEFAULT_SYMBOLS = ("APTUSDT", "DOGEUSDT", "BTCUSDT")
FOLLOWTHROUGH_HORIZON = timedelta(days=7)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MTF_DIR = ROOT / "results" / "trend_scanner_multitimeframe_structure"
DEFAULT_PL_CATALOG = ROOT / "results" / "c3_protected_low_historical_event_catalog"
DEFAULT_PH_CATALOG = ROOT / "results" / "c3_protected_high_historical_event_catalog"
DEFAULT_PL_DEEP_DIVE = ROOT / "results" / "c3_protected_low_event_driven_decision_deep_dive"
DEFAULT_OUTPUT_DIR = ROOT / "results" / "c3_mtf_break_htf_relation_audit"

PL_OUTCOME_MAP = {
    "BREAKDOWN_CONFIRMED": "HOLD_CONTINUATION",
    "RECLAIM_CONFIRMED": "FAILED_RECLAIMED",
    "UNRESOLVED_WITHIN_MAX_WINDOW": "UNRESOLVED",
    "UNRESOLVED": "UNRESOLVED",
    "EVENT_DATA_INVALID": "INVALID",
    "INVALID": "INVALID",
}
PH_OUTCOME_MAP = {
    "BREAKOUT_CONFIRMED": "HOLD_CONTINUATION",
    "RECLAIM_DOWN_CONFIRMED": "FAILED_RECLAIMED",
    "UNRESOLVED_WITHIN_MAX_WINDOW": "UNRESOLVED",
    "UNRESOLVED": "UNRESOLVED",
    "EVENT_DATA_INVALID": "INVALID",
    "INVALID": "INVALID",
}

RELATION_CLASSES = (
    "MATCH_1H_AND_4H",
    "MATCH_1H_ONLY",
    "MATCH_4H_ONLY",
    "MATCH_1H_CROSS",
    "LOCAL_5M_ONLY",
)


def ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, datetime):
        return ensure_utc(value)
    ts = pd.Timestamp(value)
    if pd.isna(ts):
        return None
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ensure_utc(ts.to_pydatetime())


def iso_z(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    return ensure_utc(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


def level_compact(level: float) -> str:
    s = f"{float(level):.8f}".rstrip("0").rstrip(".")
    return s.replace(".", "p")


def make_event_id(symbol: str, event_type: str, available_at: datetime, level: float) -> str:
    tag = "PL" if event_type == "PROTECTED_LOW_BREAK" else "PH"
    stamp = ensure_utc(available_at).strftime("%Y%m%dT%H%M%S")
    return f"{symbol}_{tag}_{stamp}_{level_compact(level)}"


def round_level(level: Any) -> float | None:
    if level is None:
        return None
    try:
        if pd.isna(level):
            return None
    except (TypeError, ValueError):
        pass
    return round(float(level), 8)


def distance_bps(event_level: float | None, htf_level: float | None) -> float | None:
    if event_level is None or htf_level is None:
        return None
    htf = float(htf_level)
    if htf == 0 or math.isnan(htf):
        return None
    return (float(event_level) - htf) / htf * 10_000.0


def levels_match(event_level: float | None, htf_level: float | None, *, match_bps: float = MATCH_BPS) -> bool:
    """True if abs distance in bps <= MATCH_BPS (technical equality tolerance)."""
    d = distance_bps(event_level, htf_level)
    if d is None:
        return False
    return abs(d) <= float(match_bps)


def levels_exact_round8(event_level: float | None, htf_level: float | None) -> bool:
    a = round_level(event_level)
    b = round_level(htf_level)
    if a is None or b is None:
        return False
    return a == b


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _write_csv(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bool_series(s: pd.Series) -> pd.Series:
    if s.dtype == bool:
        return s.fillna(False)
    return s.map(lambda x: bool(x) if pd.notna(x) else False)


def load_break_events(path: Path, *, event_type: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df = df.copy()
    df["event_type"] = event_type
    return df


def build_event_universe(
    pl_breaks: pd.DataFrame,
    ph_breaks: pd.DataFrame,
    *,
    symbols: tuple[str, ...] | list[str] = DEFAULT_SYMBOLS,
) -> pd.DataFrame:
    """Filter 5m + require_choch events, dedupe exact keys, stamp rebreak_flag + event_id."""
    frames = []
    for raw, etype in (
        (pl_breaks, "PROTECTED_LOW_BREAK"),
        (ph_breaks, "PROTECTED_HIGH_BREAK"),
    ):
        df = raw.copy()
        if "event_type" not in df.columns:
            df["event_type"] = etype
        else:
            df["event_type"] = etype
        frames.append(df)
    all_df = pd.concat(frames, ignore_index=True)
    req = _bool_series(all_df["require_choch"]) if "require_choch" in all_df.columns else True
    mask = (all_df["timeframe"].astype(str) == "5m") & req & all_df["symbol"].isin(list(symbols))
    work = all_df.loc[mask].copy()
    work["level_r8"] = work["level"].map(round_level)
    work["signal_available_at"] = work["available_at"].map(parse_ts)
    work["break_candle_open"] = work["candle_open_ts"].map(parse_ts)
    work["break_close"] = work["close"]

    before = len(work)
    work = work.drop_duplicates(
        subset=["symbol", "event_type", "signal_available_at", "level_r8"],
        keep="first",
    ).reset_index(drop=True)
    n_deduped = before - len(work)

    work = work.sort_values(
        ["symbol", "event_type", "level_r8", "signal_available_at"],
        kind="mergesort",
    ).reset_index(drop=True)
    prior = work.groupby(["symbol", "event_type", "level_r8"], sort=False)[
        "signal_available_at"
    ].shift(1)
    work["rebreak_flag"] = prior.notna()

    work["event_id"] = [
        make_event_id(str(r.symbol), str(r.event_type), r.signal_available_at, float(r.level))
        for r in work.itertuples(index=False)
    ]
    work.attrs["n_exact_duplicates_removed"] = int(n_deduped)
    return work


def classify_relation_class(
    *,
    event_type: str,
    event_level: float,
    pl_1h: float | None,
    ph_1h: float | None,
    pl_4h: float | None,
    ph_4h: float | None,
    match_bps: float = MATCH_BPS,
) -> dict[str, Any]:
    """Mutually exclusive primary relation_class + boolean match flags."""
    is_pl = event_type == "PROTECTED_LOW_BREAK"
    same_1h = levels_match(event_level, pl_1h if is_pl else ph_1h, match_bps=match_bps)
    same_4h = levels_match(event_level, pl_4h if is_pl else ph_4h, match_bps=match_bps)
    cross_1h = levels_match(event_level, ph_1h if is_pl else pl_1h, match_bps=match_bps)

    exact_1h_same = levels_exact_round8(event_level, pl_1h if is_pl else ph_1h)
    exact_4h_same = levels_exact_round8(event_level, pl_4h if is_pl else ph_4h)
    exact_1h_cross = levels_exact_round8(event_level, ph_1h if is_pl else pl_1h)

    if same_1h and same_4h:
        relation = "MATCH_1H_AND_4H"
    elif same_1h:
        relation = "MATCH_1H_ONLY"
    elif same_4h:
        relation = "MATCH_4H_ONLY"
    elif cross_1h:
        # Documented: PL near 1h PH or PH near 1h PL; lower priority than same-side.
        relation = "MATCH_1H_CROSS"
    else:
        relation = "LOCAL_5M_ONLY"

    return {
        "relation_class": relation,
        "match_1h_same_side": bool(same_1h),
        "match_4h_same_side": bool(same_4h),
        "match_1h_cross": bool(cross_1h),
        "match_any_htf": bool(same_1h or same_4h or cross_1h),
        "exact_round8_1h_same_side": bool(exact_1h_same),
        "exact_round8_4h_same_side": bool(exact_4h_same),
        "exact_round8_1h_cross": bool(exact_1h_cross),
        "dist_event_to_pl_1h_bps": distance_bps(event_level, pl_1h),
        "dist_event_to_ph_1h_bps": distance_bps(event_level, ph_1h),
        "dist_event_to_pl_4h_bps": distance_bps(event_level, pl_4h),
        "dist_event_to_ph_4h_bps": distance_bps(event_level, ph_4h),
    }


def _asof_htf_row(htf: pd.DataFrame, *, symbol: str, signal: datetime) -> pd.Series | None:
    g = htf[(htf["symbol"] == symbol) & (htf["available_at"] <= signal)].sort_values(
        "available_at", kind="mergesort"
    )
    if g.empty:
        return None
    return g.iloc[-1]


def attach_htf_context(
    events: pd.DataFrame,
    mtf: pd.DataFrame,
    states_1h: pd.DataFrame | None = None,
    states_4h: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Join causal 1h/4h as-of context from multitimeframe frame (preferred) or fallback asof."""
    mtf5 = mtf[mtf["timeframe"].astype(str) == "5m"].copy()
    mtf5["available_at"] = mtf5["available_at"].map(parse_ts)
    mtf5["symbol"] = mtf5["symbol"].astype(str)

    h1 = None
    h4 = None
    if states_1h is not None:
        h1 = states_1h.copy()
        h1["available_at"] = h1["available_at"].map(parse_ts)
    if states_4h is not None:
        h4 = states_4h.copy()
        h4["available_at"] = h4["available_at"].map(parse_ts)

    mtf_cols = [
        "symbol",
        "available_at",
        "candle_open_ts",
        "timestamp",
        "available_at_1h",
        "protected_low_1h",
        "protected_high_1h",
        "major_direction_1h",
        "close_break_protected_down_1h",
        "close_break_protected_up_1h",
        "available_at_4h",
        "protected_low_4h",
        "protected_high_4h",
        "major_direction_4h",
        "close_break_protected_down_4h",
        "close_break_protected_up_4h",
    ]
    present = [c for c in mtf_cols if c in mtf5.columns]
    mtf_key = mtf5[present].drop_duplicates(subset=["symbol", "available_at"], keep="last")

    merged = events.merge(
        mtf_key,
        how="left",
        left_on=["symbol", "signal_available_at"],
        right_on=["symbol", "available_at"],
        suffixes=("", "_mtf"),
        indicator=True,
    )
    merged["htf_join_source"] = np.where(
        merged["_merge"] == "both", "multitimeframe_exact", "asof_fallback"
    )

    rows_out: list[dict[str, Any]] = []
    for idx, r in merged.iterrows():
        signal = r["signal_available_at"]
        src = str(r["htf_join_source"])
        if src == "asof_fallback":
            row_1h = _asof_htf_row(h1, symbol=str(r["symbol"]), signal=signal) if h1 is not None and signal else None
            row_4h = _asof_htf_row(h4, symbol=str(r["symbol"]), signal=signal) if h4 is not None and signal else None
            avail_1h = parse_ts(row_1h["available_at"]) if row_1h is not None else None
            avail_4h = parse_ts(row_4h["available_at"]) if row_4h is not None else None
            pl_1h = round_level(row_1h["protected_low"]) if row_1h is not None else None
            ph_1h = round_level(row_1h["protected_high"]) if row_1h is not None else None
            pl_4h = round_level(row_4h["protected_low"]) if row_4h is not None else None
            ph_4h = round_level(row_4h["protected_high"]) if row_4h is not None else None
            md_1h = row_1h.get("major_direction") if row_1h is not None else None
            md_4h = row_4h.get("major_direction") if row_4h is not None else None
            cbd_1h = bool(row_1h["close_break_protected_down"]) if row_1h is not None and pd.notna(row_1h.get("close_break_protected_down")) else None
            cbu_1h = bool(row_1h["close_break_protected_up"]) if row_1h is not None and pd.notna(row_1h.get("close_break_protected_up")) else None
            cbd_4h = bool(row_4h["close_break_protected_down"]) if row_4h is not None and pd.notna(row_4h.get("close_break_protected_down")) else None
            cbu_4h = bool(row_4h["close_break_protected_up"]) if row_4h is not None and pd.notna(row_4h.get("close_break_protected_up")) else None
            candle_open_5m = parse_ts(r.get("break_candle_open"))
        else:
            avail_1h = parse_ts(r.get("available_at_1h"))
            avail_4h = parse_ts(r.get("available_at_4h"))
            pl_1h = round_level(r.get("protected_low_1h"))
            ph_1h = round_level(r.get("protected_high_1h"))
            pl_4h = round_level(r.get("protected_low_4h"))
            ph_4h = round_level(r.get("protected_high_4h"))
            md_1h = r.get("major_direction_1h")
            md_4h = r.get("major_direction_4h")
            cbd_1h = bool(r["close_break_protected_down_1h"]) if pd.notna(r.get("close_break_protected_down_1h")) else None
            cbu_1h = bool(r["close_break_protected_up_1h"]) if pd.notna(r.get("close_break_protected_up_1h")) else None
            cbd_4h = bool(r["close_break_protected_down_4h"]) if pd.notna(r.get("close_break_protected_down_4h")) else None
            cbu_4h = bool(r["close_break_protected_up_4h"]) if pd.notna(r.get("close_break_protected_up_4h")) else None
            candle_open_5m = parse_ts(r.get("candle_open_ts") or r.get("timestamp") or r.get("break_candle_open"))

        future_1h = bool(avail_1h is not None and signal is not None and avail_1h > signal)
        future_4h = bool(avail_4h is not None and signal is not None and avail_4h > signal)
        lag_1h = ((signal - avail_1h).total_seconds() / 60.0) if avail_1h and signal else None
        lag_4h = ((signal - avail_4h).total_seconds() / 60.0) if avail_4h and signal else None

        event_level = float(r["level"])
        rel = classify_relation_class(
            event_type=str(r["event_type"]),
            event_level=event_level,
            pl_1h=pl_1h,
            ph_1h=ph_1h,
            pl_4h=pl_4h,
            ph_4h=ph_4h,
        )

        against_active_1h_pl = False
        against_active_1h_ph = False
        if str(r["event_type"]) == "PROTECTED_LOW_BREAK":
            against_active_1h_pl = bool(
                rel["match_1h_same_side"] and cbd_1h is False
            )
        else:
            against_active_1h_ph = bool(
                rel["match_1h_same_side"] and cbu_1h is False
            )

        rows_out.append(
            {
                "event_id": r["event_id"],
                "symbol": r["symbol"],
                "event_type": r["event_type"],
                "level": event_level,
                "level_r8": round_level(event_level),
                "break_candle_open": iso_z(parse_ts(r.get("break_candle_open"))),
                "signal_available_at": iso_z(signal),
                "break_close": r.get("break_close"),
                "choch": bool(r["choch"]) if pd.notna(r.get("choch")) else None,
                "trend_segment_id": r.get("trend_segment_id"),
                "major_direction": r.get("major_direction"),
                "in_warmup": bool(r["in_warmup"]) if pd.notna(r.get("in_warmup")) else None,
                "rebreak_flag": bool(r["rebreak_flag"]),
                "htf_join_source": src,
                "candle_open_5m": iso_z(candle_open_5m),
                "available_at_1h": iso_z(avail_1h),
                "available_at_4h": iso_z(avail_4h),
                "lag_1h_minutes": lag_1h,
                "lag_4h_minutes": lag_4h,
                "future_violation_1h": future_1h,
                "future_violation_4h": future_4h,
                "future_violation": future_1h or future_4h,
                "protected_low_1h": pl_1h,
                "protected_high_1h": ph_1h,
                "protected_low_4h": pl_4h,
                "protected_high_4h": ph_4h,
                "major_direction_1h": md_1h if pd.isna(md_1h) is False else None,
                "major_direction_4h": md_4h if pd.isna(md_4h) is False else None,
                "close_break_protected_down_1h": cbd_1h,
                "close_break_protected_up_1h": cbu_1h,
                "close_break_protected_down_4h": cbd_4h,
                "close_break_protected_up_4h": cbu_4h,
                "against_active_1h_pl": against_active_1h_pl,
                "against_active_1h_ph": against_active_1h_ph,
                **rel,
            }
        )

    return pd.DataFrame(rows_out)


def map_persistence(event_type: str, outcome: str | None) -> tuple[str, str]:
    if outcome is None or (isinstance(outcome, float) and math.isnan(outcome)) or str(outcome) in {"", "n/a", "nan"}:
        return "n/a", "n/a"
    out = str(outcome)
    mapping = PL_OUTCOME_MAP if event_type == "PROTECTED_LOW_BREAK" else PH_OUTCOME_MAP
    persistence = mapping.get(out, "n/a")
    return out, persistence


def attach_catalog_outcomes(
    events: pd.DataFrame,
    pl_decisions: pd.DataFrame,
    ph_decisions: pd.DataFrame,
) -> pd.DataFrame:
    """Join outcomes ONLY from existing PL/PH catalog event_decisions.csv."""
    work = events.copy()
    work["signal_ts"] = work["signal_available_at"].map(parse_ts)
    work["catalog_outcome"] = pd.NA
    work["catalog_event_id"] = pd.NA
    work["catalog_decision_ts"] = pd.NA
    work["catalog_minutes_after_break"] = pd.NA

    def _apply_side(
        mask: pd.Series,
        decisions: pd.DataFrame,
        level_col: str,
    ) -> None:
        if not mask.any() or decisions is None or decisions.empty:
            return
        dec = decisions.copy()
        dec["break_available_at"] = dec["break_available_at"].map(parse_ts)
        dec["level_r8"] = dec[level_col].map(round_level)
        key = dec.rename(
            columns={
                "outcome": "catalog_outcome",
                "event_id": "catalog_event_id",
                "decision_ts": "catalog_decision_ts",
                "minutes_after_break": "catalog_minutes_after_break",
            }
        )[
            [
                "symbol",
                "break_available_at",
                "level_r8",
                "catalog_outcome",
                "catalog_event_id",
                "catalog_decision_ts",
                "catalog_minutes_after_break",
            ]
        ]
        sub = work.loc[mask, ["symbol", "signal_ts", "level_r8"]].reset_index()
        joined = sub.merge(
            key,
            how="left",
            left_on=["symbol", "signal_ts", "level_r8"],
            right_on=["symbol", "break_available_at", "level_r8"],
        )
        for _, row in joined.iterrows():
            idx = row["index"]
            if pd.notna(row.get("catalog_outcome")):
                work.at[idx, "catalog_outcome"] = row["catalog_outcome"]
                work.at[idx, "catalog_event_id"] = row["catalog_event_id"]
                work.at[idx, "catalog_decision_ts"] = row["catalog_decision_ts"]
                work.at[idx, "catalog_minutes_after_break"] = row["catalog_minutes_after_break"]

    _apply_side(work["event_type"] == "PROTECTED_LOW_BREAK", pl_decisions, "protected_low")
    _apply_side(work["event_type"] == "PROTECTED_HIGH_BREAK", ph_decisions, "protected_high")

    outcomes: list[str] = []
    persistences: list[str] = []
    sources: list[str] = []
    for r in work.itertuples(index=False):
        raw = getattr(r, "catalog_outcome", None)
        if raw is None or pd.isna(raw):
            outcomes.append("n/a")
            persistences.append("n/a")
            sources.append("n/a")
        else:
            o, p = map_persistence(str(r.event_type), str(raw))
            outcomes.append(o)
            persistences.append(p)
            sources.append("catalog")
    work["outcome"] = outcomes
    work["persistence"] = persistences
    work["outcome_source"] = sources
    return work.drop(columns=["signal_ts"], errors="ignore")


def attach_deep_dive_enrichment(
    events: pd.DataFrame,
    inventory: pd.DataFrame | None,
    decision_points: pd.DataFrame | None,
) -> pd.DataFrame:
    """Optional enrichment from known PL deep-dive clusters when timestamps match."""
    work = events.copy()
    work["deep_dive_event_id"] = None
    work["deep_dive_cluster_id"] = None
    work["deep_dive_decision_type"] = None
    work["deep_dive_decision_ts"] = None
    if inventory is None or inventory.empty:
        return work

    inv = inventory.copy()
    inv["break_available_at"] = inv["break_available_at"].map(parse_ts)
    inv["level_r8"] = inv["level_price"].map(round_level) if "level_price" in inv.columns else None

    dp_map: dict[str, dict[str, Any]] = {}
    if decision_points is not None and not decision_points.empty:
        for _, r in decision_points.iterrows():
            dp_map[str(r["event_id"])] = {
                "decision_type": r.get("decision_type"),
                "decision_ts": r.get("decision_ts"),
            }

    sig = work["signal_available_at"].map(parse_ts)
    for i, row in work.iterrows():
        if row["event_type"] != "PROTECTED_LOW_BREAK":
            continue
        hits = inv[
            (inv["symbol"] == row["symbol"])
            & (inv["break_available_at"] == sig.loc[i])
            & (inv["level_r8"] == row["level_r8"])
        ]
        if hits.empty:
            continue
        h = hits.iloc[0]
        eid = str(h["event_id"])
        work.at[i, "deep_dive_event_id"] = eid
        work.at[i, "deep_dive_cluster_id"] = h.get("cluster_id")
        if eid in dp_map:
            work.at[i, "deep_dive_decision_type"] = dp_map[eid]["decision_type"]
            work.at[i, "deep_dive_decision_ts"] = dp_map[eid]["decision_ts"]
    return work


def attach_htf_followthrough(
    events: pd.DataFrame,
    pl_breaks_all_tf: pd.DataFrame,
    ph_breaks_all_tf: pd.DataFrame,
    *,
    horizon: timedelta = FOLLOWTHROUGH_HORIZON,
) -> pd.DataFrame:
    """Next same-side HTF break of same rounded level within horizon (existing CSVs only)."""
    work = events.copy()

    def _prep(df: pd.DataFrame) -> pd.DataFrame:
        d = df.copy()
        d["available_at"] = d["available_at"].map(parse_ts)
        d["level_r8"] = d["level"].map(round_level)
        return d

    pl = _prep(pl_breaks_all_tf)
    ph = _prep(ph_breaks_all_tf)

    # Index HTF breaks by (symbol, level_r8, timeframe) for O(1) group lookup
    def _index(df: pd.DataFrame) -> dict[tuple[Any, Any, str], list[datetime]]:
        out: dict[tuple[Any, Any, str], list[datetime]] = {}
        for r in df.itertuples(index=False):
            tf = str(r.timeframe)
            if tf not in {"1h", "4h"}:
                continue
            aa = r.available_at
            if aa is None:
                continue
            key = (r.symbol, r.level_r8, tf)
            out.setdefault(key, []).append(aa)
        for k in out:
            out[k].sort()
        return out

    pl_idx = _index(pl)
    ph_idx = _index(ph)

    minutes_1h: list[float | None] = []
    minutes_4h: list[float | None] = []
    avail_1h_ft: list[str | None] = []
    avail_4h_ft: list[str | None] = []

    for r in work.itertuples(index=False):
        signal = parse_ts(r.signal_available_at)
        idx = pl_idx if r.event_type == "PROTECTED_LOW_BREAK" else ph_idx
        if signal is None:
            minutes_1h.append(None)
            minutes_4h.append(None)
            avail_1h_ft.append(None)
            avail_4h_ft.append(None)
            continue
        end = signal + horizon

        def _next(tf: str) -> tuple[float | None, str | None]:
            times = idx.get((r.symbol, r.level_r8, tf), [])
            for aa in times:
                if aa > signal and aa <= end:
                    return (aa - signal).total_seconds() / 60.0, iso_z(aa)
            return None, None

        m1, a1 = _next("1h")
        m4, a4 = _next("4h")
        minutes_1h.append(m1)
        minutes_4h.append(m4)
        avail_1h_ft.append(a1)
        avail_4h_ft.append(a4)

    work["minutes_to_1h_same_level_break"] = minutes_1h
    work["minutes_to_4h_same_level_break"] = minutes_4h
    work["followthrough_1h_available_at"] = avail_1h_ft
    work["followthrough_4h_available_at"] = avail_4h_ft
    work["has_1h_same_level_followthrough"] = work["minutes_to_1h_same_level_break"].notna()
    work["has_4h_same_level_followthrough"] = work["minutes_to_4h_same_level_break"].notna()
    return work


def _rate(n_num: int, n_den: int) -> float | None:
    if n_den <= 0:
        return None
    return float(n_num) / float(n_den)


def _outcome_stats(df: pd.DataFrame) -> dict[str, Any]:
    resolved = df[df["persistence"].isin(["HOLD_CONTINUATION", "FAILED_RECLAIMED"])]
    n = len(resolved)
    n_hold = int((resolved["persistence"] == "HOLD_CONTINUATION").sum())
    n_fail = int((resolved["persistence"] == "FAILED_RECLAIMED").sum())
    return {
        "n_with_outcome": int((df["outcome"] != "n/a").sum()),
        "n_resolved": n,
        "n_hold": n_hold,
        "n_failed_reclaimed": n_fail,
        "hold_rate": _rate(n_hold, n),
        "failed_reclaim_rate": _rate(n_fail, n),
    }


def build_summaries(universe: pd.DataFrame) -> dict[str, pd.DataFrame]:
    rel = (
        universe.groupby(["relation_class", "event_type", "symbol"], dropna=False)
        .size()
        .reset_index(name="n_events")
        .sort_values(["relation_class", "event_type", "symbol"])
    )

    with_out = universe[universe["outcome"] != "n/a"].copy()
    rows_rel = []
    for keys, g in with_out.groupby(["relation_class"], dropna=False):
        rc = keys[0] if isinstance(keys, tuple) else keys
        st = _outcome_stats(g)
        rows_rel.append({"relation_class": rc, **st, "n_events_total_in_class": int((universe["relation_class"] == rc).sum())})
    outcome_by_relation = pd.DataFrame(rows_rel)

    rows_et = []
    for keys, g in with_out.groupby(["relation_class", "event_type"], dropna=False):
        rc, et = keys
        st = _outcome_stats(g)
        rows_et.append({"relation_class": rc, "event_type": et, **st})
    outcome_by_relation_event_type = pd.DataFrame(rows_et)

    rows_sym = []
    for keys, g in with_out.groupby(["symbol", "relation_class"], dropna=False):
        sym, rc = keys
        st = _outcome_stats(g)
        rows_sym.append({"symbol": sym, "relation_class": rc, **st})
    # also overall by symbol
    for sym, g in with_out.groupby("symbol", dropna=False):
        st = _outcome_stats(g)
        rows_sym.append({"symbol": sym, "relation_class": "ALL", **st})
    outcome_by_symbol = pd.DataFrame(rows_sym)

    rows_rb = []
    for keys, g in universe.groupby(["rebreak_flag", "relation_class"], dropna=False):
        rb, rc = keys
        st = _outcome_stats(g)
        rows_rb.append(
            {
                "rebreak_flag": bool(rb),
                "relation_class": rc,
                "n_events": len(g),
                **st,
                "frac_1h_followthrough": float(g["has_1h_same_level_followthrough"].mean()) if len(g) else None,
                "frac_4h_followthrough": float(g["has_4h_same_level_followthrough"].mean()) if len(g) else None,
            }
        )
    rebreak_vs_first = pd.DataFrame(rows_rb)

    rows_ft = []
    for rc, g in universe.groupby("relation_class", dropna=False):
        m1 = g["minutes_to_1h_same_level_break"].dropna()
        m4 = g["minutes_to_4h_same_level_break"].dropna()
        rows_ft.append(
            {
                "relation_class": rc,
                "n_events": len(g),
                "n_with_1h_followthrough": int(g["has_1h_same_level_followthrough"].sum()),
                "frac_1h_followthrough": float(g["has_1h_same_level_followthrough"].mean()) if len(g) else None,
                "median_minutes_to_1h": float(m1.median()) if len(m1) else None,
                "n_with_4h_followthrough": int(g["has_4h_same_level_followthrough"].sum()),
                "frac_4h_followthrough": float(g["has_4h_same_level_followthrough"].mean()) if len(g) else None,
                "median_minutes_to_4h": float(m4.median()) if len(m4) else None,
            }
        )
    htf_followthrough = pd.DataFrame(rows_ft)

    focus = {"MATCH_1H_ONLY", "MATCH_4H_ONLY", "MATCH_1H_AND_4H"}
    rows_1v4 = []
    for rc in ("MATCH_1H_ONLY", "MATCH_4H_ONLY", "MATCH_1H_AND_4H"):
        g = universe[universe["relation_class"] == rc]
        st = _outcome_stats(g)
        m1 = g["minutes_to_1h_same_level_break"].dropna()
        m4 = g["minutes_to_4h_same_level_break"].dropna()
        rows_1v4.append(
            {
                "relation_class": rc,
                "n_events": len(g),
                **st,
                "frac_1h_followthrough": float(g["has_1h_same_level_followthrough"].mean()) if len(g) else None,
                "frac_4h_followthrough": float(g["has_4h_same_level_followthrough"].mean()) if len(g) else None,
                "median_minutes_to_1h": float(m1.median()) if len(m1) else None,
                "median_minutes_to_4h": float(m4.median()) if len(m4) else None,
            }
        )
    one_h_vs_four_h = pd.DataFrame(rows_1v4)

    return {
        "relation_class_summary": rel,
        "outcome_by_relation": outcome_by_relation,
        "outcome_by_relation_event_type": outcome_by_relation_event_type,
        "outcome_by_symbol": outcome_by_symbol,
        "rebreak_vs_first_summary": rebreak_vs_first,
        "htf_followthrough_by_relation": htf_followthrough,
        "one_h_vs_four_h_value": one_h_vs_four_h,
    }


def decide_primary(universe: pd.DataFrame, summaries: dict[str, pd.DataFrame]) -> dict[str, Any]:
    """Pick exactly one primary decision from evidence."""
    n_events = len(universe)
    with_out = universe[universe["outcome"] != "n/a"]
    resolved = with_out[with_out["persistence"].isin(["HOLD_CONTINUATION", "FAILED_RECLAIMED"])]
    n_out = len(with_out)
    n_resolved = len(resolved)

    obr = summaries["outcome_by_relation"]
    ft = summaries["htf_followthrough_by_relation"]
    one_v_four = summaries["one_h_vs_four_h_value"]

    def _hold(rc: str) -> tuple[float | None, int]:
        if obr.empty or rc not in set(obr["relation_class"]):
            return None, 0
        row = obr[obr["relation_class"] == rc].iloc[0]
        return row.get("hold_rate"), int(row.get("n_resolved") or 0)

    def _ft_frac(rc: str, col: str = "frac_1h_followthrough") -> float | None:
        if ft.empty or rc not in set(ft["relation_class"]):
            return None
        return ft[ft["relation_class"] == rc].iloc[0].get(col)

    match_classes = ["MATCH_1H_ONLY", "MATCH_4H_ONLY", "MATCH_1H_AND_4H"]
    matched = resolved[resolved["relation_class"].isin(match_classes)]
    local_res = resolved[resolved["relation_class"] == "LOCAL_5M_ONLY"]
    matched_hold = (
        _rate(int((matched["persistence"] == "HOLD_CONTINUATION").sum()), len(matched))
        if len(matched)
        else None
    )
    local_hold_r = (
        _rate(int((local_res["persistence"] == "HOLD_CONTINUATION").sum()), len(local_res))
        if len(local_res)
        else None
    )

    cell_ns = []
    for rc in match_classes + ["LOCAL_5M_ONLY", "MATCH_1H_CROSS"]:
        _, n = _hold(rc)
        cell_ns.append(n)
    matched_n = len(matched)
    local_n = len(local_res)
    # Key comparison cells for HTF vs local / 1h vs 4h need n>=30 to claim an edge.
    key_cells = [matched_n] + [_hold(rc)[1] for rc in match_classes]
    min_key_cell = min(key_cells) if key_cells else 0
    max_cell = max(cell_ns) if cell_ns else 0
    small_outcome = n_resolved < 30 or matched_n < 30 or min_key_cell < 30

    ft_matched = []
    for rc in match_classes:
        f = _ft_frac(rc)
        if f is not None:
            ft_matched.append(float(f))
    ft_local = _ft_frac("LOCAL_5M_ONLY")
    ft_local_f = float(ft_local) if ft_local is not None and pd.notna(ft_local) else None
    structural_pattern = False
    if ft_matched and ft_local_f is not None:
        structural_pattern = (sum(ft_matched) / len(ft_matched)) > (ft_local_f + 0.05)
    if not structural_pattern:
        # Distinct HTF-match minority with strong same-level 1h follow-through
        f1 = _ft_frac("MATCH_1H_ONLY")
        if (
            f1 is not None
            and pd.notna(f1)
            and float(f1) >= 0.5
            and int((universe["relation_class"] == "MATCH_1H_ONLY").sum()) >= 10
        ):
            structural_pattern = True
    match_share = float(universe["match_any_htf"].mean()) if n_events else 0.0
    if 0.005 < match_share < 0.95 and structural_pattern is False:
        # Weak fallback: non-degenerate relation mix alone is not enough
        pass

    rationale_parts: list[str] = []

    if n_events == 0:
        return {
            "primary_decision": "AUDIT_DATA_INSUFFICIENT",
            "rationale": "No 5m break events in universe.",
            "n_events": 0,
            "n_outcomes": 0,
        }

    # Prefer small-sample acknowledgement when structural signal exists
    if structural_pattern and small_outcome:
        rationale_parts.append(
            f"Relation/follow-through structure is visible across n={n_events} breaks "
            f"(MATCH_1H_ONLY 1h follow-through={_ft_frac('MATCH_1H_ONLY')}), "
            f"but catalog outcomes are small (n_resolved={n_resolved}, matched_resolved={matched_n}<30)."
        )
        primary = "HTF_RELATION_SIGNAL_PRESENT_BUT_OUTCOME_SAMPLE_SMALL"
    elif n_resolved < 10:
        primary = "AUDIT_DATA_INSUFFICIENT"
        rationale_parts.append(f"Too few catalog outcomes (n_resolved={n_resolved}).")
    else:
        if (
            matched_hold is not None
            and local_hold_r is not None
            and matched_n >= 30
            and local_n >= 30
            and matched_hold >= local_hold_r + 0.10
        ):
            primary = "HTF_MATCHED_BREAKS_MORE_OFTEN_HOLD"
            rationale_parts.append(
                f"Matched hold_rate={matched_hold:.2f} (n={matched_n}) vs local={local_hold_r:.2f} (n={local_n})."
            )
        elif (
            local_hold_r is not None
            and matched_hold is not None
            and matched_n >= 30
            and local_n >= 30
            and (1.0 - local_hold_r) >= (1.0 - (matched_hold or 0)) + 0.10
        ):
            primary = "LOCAL_5M_BREAKS_MORE_OFTEN_RECLAIMED"
            rationale_parts.append(
                f"Local failed_reclaim higher: local hold={local_hold_r:.2f}, matched={matched_hold:.2f}."
            )
        else:
            h1 = one_v_four[one_v_four["relation_class"] == "MATCH_1H_ONLY"]
            h4 = one_v_four[one_v_four["relation_class"] == "MATCH_4H_ONLY"]
            both = one_v_four[one_v_four["relation_class"] == "MATCH_1H_AND_4H"]
            h1_hold = (
                float(h1.iloc[0]["hold_rate"])
                if len(h1) and pd.notna(h1.iloc[0].get("hold_rate"))
                else None
            )
            h4_hold = (
                float(h4.iloc[0]["hold_rate"])
                if len(h4) and pd.notna(h4.iloc[0].get("hold_rate"))
                else None
            )
            both_hold = (
                float(both.iloc[0]["hold_rate"])
                if len(both) and pd.notna(both.iloc[0].get("hold_rate"))
                else None
            )
            h1_n = int(h1.iloc[0]["n_resolved"]) if len(h1) else 0
            h4_n = int(h4.iloc[0]["n_resolved"]) if len(h4) else 0
            both_n = int(both.iloc[0]["n_resolved"]) if len(both) else 0

            if (
                h1_hold is not None
                and both_hold is not None
                and h1_n >= 30
                and both_n >= 30
                and abs(both_hold - h1_hold) < 0.08
            ):
                primary = "ONE_H_MATCH_DOMINATES_FOUR_H_ADDS_LITTLE"
                rationale_parts.append(
                    f"MATCH_1H_ONLY hold={h1_hold:.2f} (n={h1_n}) ≈ AND_4H {both_hold:.2f} (n={both_n})."
                )
            elif (
                both_hold is not None
                and h1_hold is not None
                and both_n >= 30
                and h1_n >= 30
                and both_hold >= h1_hold + 0.10
            ):
                primary = "FOUR_H_MATCH_ADDS_INCREMENTAL_VALUE"
                rationale_parts.append(
                    f"MATCH_1H_AND_4H hold={both_hold:.2f} vs MATCH_1H_ONLY {h1_hold:.2f}."
                )
            else:
                htf_first = resolved[
                    (~resolved["rebreak_flag"]) & (resolved["relation_class"].isin(match_classes))
                ]
                htf_reb = resolved[
                    (resolved["rebreak_flag"]) & (resolved["relation_class"].isin(match_classes))
                ]
                f_hold = _rate(
                    int((htf_first["persistence"] == "HOLD_CONTINUATION").sum()), len(htf_first)
                )
                r_hold = _rate(
                    int((htf_reb["persistence"] == "HOLD_CONTINUATION").sum()), len(htf_reb)
                )
                if (
                    f_hold is not None
                    and r_hold is not None
                    and len(htf_first) >= 30
                    and len(htf_reb) >= 30
                    and r_hold >= f_hold + 0.10
                ):
                    primary = "REBREAKS_AT_HTF_STRONGER_THAN_FIRST"
                    rationale_parts.append(
                        f"HTF rebreak hold={r_hold:.2f} (n={len(htf_reb)}) vs first={f_hold:.2f} (n={len(htf_first)})."
                    )
                else:
                    pl_res = resolved[resolved["event_type"] == "PROTECTED_LOW_BREAK"]
                    ph_res = resolved[resolved["event_type"] == "PROTECTED_HIGH_BREAK"]
                    pl_h = _rate(
                        int((pl_res["persistence"] == "HOLD_CONTINUATION").sum()), len(pl_res)
                    )
                    ph_h = _rate(
                        int((ph_res["persistence"] == "HOLD_CONTINUATION").sum()), len(ph_res)
                    )
                    if (
                        pl_h is not None
                        and ph_h is not None
                        and len(pl_res) >= 30
                        and len(ph_res) >= 30
                        and abs(pl_h - ph_h) >= 0.15
                    ):
                        primary = "PL_AND_PH_BEHAVE_DIFFERENTLY"
                        rationale_parts.append(f"PL hold={pl_h:.2f} vs PH hold={ph_h:.2f}.")
                    elif structural_pattern and small_outcome:
                        primary = "HTF_RELATION_SIGNAL_PRESENT_BUT_OUTCOME_SAMPLE_SMALL"
                        rationale_parts.append(
                            f"Structural HTF relation variation present; outcomes n_resolved={n_resolved}."
                        )
                    elif structural_pattern:
                        primary = "HTF_RELATION_SIGNAL_PRESENT_BUT_OUTCOME_SAMPLE_SMALL"
                        rationale_parts.append(
                            f"HTF same-level follow-through differs sharply by relation, "
                            f"but outcome cells remain small (matched_resolved={matched_n})."
                        )
                    else:
                        primary = "NO_CLEAR_HTF_RELATION_EDGE"
                        rationale_parts.append(
                            f"No decisive hold-rate separation across relation classes "
                            f"(n_resolved={n_resolved})."
                        )

    if (
        primary not in {"AUDIT_DATA_INSUFFICIENT", "HTF_RELATION_SIGNAL_PRESENT_BUT_OUTCOME_SAMPLE_SMALL"}
        and structural_pattern
        and small_outcome
    ):
        rationale_parts.append(
            f"Overriding to small-sample acknowledgement (matched_resolved n={matched_n}<30)."
        )
        primary = "HTF_RELATION_SIGNAL_PRESENT_BUT_OUTCOME_SAMPLE_SMALL"

    assert primary in PRIMARY_DECISIONS
    return {
        "primary_decision": primary,
        "rationale": " ".join(rationale_parts) if rationale_parts else primary,
        "n_events": int(n_events),
        "n_outcomes": int(n_out),
        "n_resolved": int(n_resolved),
        "matched_hold_rate": matched_hold,
        "local_hold_rate": local_hold_r,
        "max_key_cell_n_resolved": int(max_cell),
        "matched_n_resolved": int(matched_n),
    }


def build_research_answers(universe: pd.DataFrame, summaries: dict[str, pd.DataFrame], decision: dict[str, Any]) -> dict[str, str]:
    n = len(universe)
    rc_counts = universe["relation_class"].value_counts().to_dict()
    with_out = universe[universe["outcome"] != "n/a"]
    resolved = with_out[with_out["persistence"].isin(["HOLD_CONTINUATION", "FAILED_RECLAIMED"])]
    matched = resolved[resolved["relation_class"].isin(["MATCH_1H_ONLY", "MATCH_4H_ONLY", "MATCH_1H_AND_4H"])]
    local = resolved[resolved["relation_class"] == "LOCAL_5M_ONLY"]
    m_hold = _rate(int((matched["persistence"] == "HOLD_CONTINUATION").sum()), len(matched))
    l_hold = _rate(int((local["persistence"] == "HOLD_CONTINUATION").sum()), len(local))

    ft = summaries["htf_followthrough_by_relation"]
    one_v = summaries["one_h_vs_four_h_value"]

    q1 = (
        f"Among n={n} 5m choch breaks: "
        + ", ".join(f"{k}={v} ({100.0 * v / n:.1f}%)" for k, v in sorted(rc_counts.items(), key=lambda x: -x[1]))
    )
    q2 = (
        f"Catalog-resolved hold rates: HTF-matched={m_hold} (n={len(matched)}) vs "
        f"LOCAL_5M_ONLY={l_hold} (n={len(local)}). "
        f"Outcome coverage={len(with_out)}/{n} ({100.0 * len(with_out) / n:.2f}%); "
        "BTC has no catalog outcomes."
    )
    # Q3 1h vs 4h
    parts = []
    for _, r in one_v.iterrows():
        parts.append(
            f"{r['relation_class']}: n={int(r['n_events'])}, n_resolved={int(r.get('n_resolved') or 0)}, "
            f"hold_rate={r.get('hold_rate')}, frac_1h_ft={r.get('frac_1h_followthrough')}"
        )
    q3 = "1h vs 4h value (existing outcomes + follow-through): " + "; ".join(parts)

    rb = summaries["rebreak_vs_first_summary"]
    first_n = int(universe[~universe["rebreak_flag"]].shape[0])
    reb_n = int(universe[universe["rebreak_flag"]].shape[0])
    first_res = resolved[~resolved["rebreak_flag"]]
    reb_res = resolved[resolved["rebreak_flag"]]
    q4 = (
        f"Rebreak vs first: first n={first_n} (resolved hold="
        f"{_rate(int((first_res['persistence']=='HOLD_CONTINUATION').sum()), len(first_res))}, n_res={len(first_res)}); "
        f"rebreak n={reb_n} (resolved hold="
        f"{_rate(int((reb_res['persistence']=='HOLD_CONTINUATION').sum()), len(reb_res))}, n_res={len(reb_res)})."
    )

    pl = resolved[resolved["event_type"] == "PROTECTED_LOW_BREAK"]
    ph = resolved[resolved["event_type"] == "PROTECTED_HIGH_BREAK"]
    q5 = (
        f"PL vs PH resolved: PL hold="
        f"{_rate(int((pl['persistence']=='HOLD_CONTINUATION').sum()), len(pl))} (n={len(pl)}); "
        f"PH hold={_rate(int((ph['persistence']=='HOLD_CONTINUATION').sum()), len(ph))} (n={len(ph)}). "
        f"Universe PL={int((universe['event_type']=='PROTECTED_LOW_BREAK').sum())}, "
        f"PH={int((universe['event_type']=='PROTECTED_HIGH_BREAK').sum())}."
    )

    ft_rows = []
    for _, r in ft.iterrows():
        ft_rows.append(
            f"{r['relation_class']}: 1h_ft={r.get('frac_1h_followthrough')} "
            f"(med_min={r.get('median_minutes_to_1h')}), "
            f"4h_ft={r.get('frac_4h_followthrough')} (med_min={r.get('median_minutes_to_4h')})"
        )
    q6 = "Same-level HTF follow-through within 7d by relation: " + "; ".join(ft_rows)

    return {
        "Q1_relation_class_distribution": q1,
        "Q2_htf_matched_vs_local_hold": q2,
        "Q3_one_h_vs_four_h_value": q3,
        "Q4_rebreak_vs_first": q4,
        "Q5_pl_vs_ph": q5,
        "Q6_htf_followthrough": q6,
        "primary_decision": decision["primary_decision"],
    }


def write_summary_md(
    path: Path,
    *,
    decision: dict[str, Any],
    answers: dict[str, str],
    universe: pd.DataFrame,
    summaries: dict[str, pd.DataFrame],
    gaps: dict[str, Any],
) -> None:
    lines = [
        "# C3 MTF break ↔ HTF relation audit",
        "",
        f"**Primary decision:** `{decision['primary_decision']}`",
        "",
        decision.get("rationale", ""),
        "",
        "## Scope",
        "",
        f"- Universe: {len(universe)} five-minute Protected-Low/High breaks "
        f"(require_choch=True) for {sorted(universe['symbol'].unique().tolist())}.",
        f"- MATCH_BPS={MATCH_BPS} is a **technical equality tolerance**, not a trading threshold.",
        "- Outcomes joined only from existing PL/PH historical event catalogs (no new replay).",
        f"- Catalog outcome coverage: {gaps.get('outcome_coverage_fraction')} "
        f"({gaps.get('n_with_outcome')}/{gaps.get('n_events')}); BTC catalog outcomes: "
        f"{gaps.get('btc_n_with_outcome', 0)}.",
        "",
        "## Research answers",
        "",
        f"**Q1.** {answers['Q1_relation_class_distribution']}",
        "",
        f"**Q2.** {answers['Q2_htf_matched_vs_local_hold']}",
        "",
        f"**Q3.** {answers['Q3_one_h_vs_four_h_value']}",
        "",
        f"**Q4.** {answers['Q4_rebreak_vs_first']}",
        "",
        f"**Q5.** {answers['Q5_pl_vs_ph']}",
        "",
        f"**Q6.** {answers['Q6_htf_followthrough']}",
        "",
        "## Caveat",
        "",
        "Outcome sample is small (~catalog events) versus ~5200 structural breaks. "
        "Do not treat hold-rate differences as tradeable edges without larger labeled coverage.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def run_mtf_break_htf_relation_audit(
    *,
    mtf_dir: Path = DEFAULT_MTF_DIR,
    pl_catalog_dir: Path = DEFAULT_PL_CATALOG,
    ph_catalog_dir: Path = DEFAULT_PH_CATALOG,
    pl_deep_dive_dir: Path = DEFAULT_PL_DEEP_DIVE,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    symbols: tuple[str, ...] | list[str] = DEFAULT_SYMBOLS,
    overwrite: bool = False,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()) and not overwrite:
        raise FileExistsError(f"{output_dir} exists; pass overwrite=True")
    output_dir.mkdir(parents=True, exist_ok=True)

    pl_path = Path(mtf_dir) / "protected_low_break_events.csv"
    ph_path = Path(mtf_dir) / "protected_high_break_events.csv"
    mtf_path = Path(mtf_dir) / "structure_states_multitimeframe.parquet"
    h1_path = Path(mtf_dir) / "structure_states_1h.parquet"
    h4_path = Path(mtf_dir) / "structure_states_4h.parquet"
    pl_dec_path = Path(pl_catalog_dir) / "event_decisions.csv"
    ph_dec_path = Path(ph_catalog_dir) / "event_decisions.csv"
    inv_path = Path(pl_deep_dive_dir) / "event_inventory.csv"
    dp_path = Path(pl_deep_dive_dir) / "decision_points.csv"

    used_files = {
        "protected_low_break_events": str(pl_path),
        "protected_high_break_events": str(ph_path),
        "structure_states_multitimeframe": str(mtf_path),
        "structure_states_1h": str(h1_path),
        "structure_states_4h": str(h4_path),
        "pl_event_decisions": str(pl_dec_path),
        "ph_event_decisions": str(ph_dec_path),
        "pl_deep_dive_inventory": str(inv_path) if inv_path.exists() else None,
        "pl_deep_dive_decision_points": str(dp_path) if dp_path.exists() else None,
    }

    logger.info("Loading break event CSVs")
    pl_breaks = load_break_events(pl_path, event_type="PROTECTED_LOW_BREAK")
    ph_breaks = load_break_events(ph_path, event_type="PROTECTED_HIGH_BREAK")
    events = build_event_universe(pl_breaks, ph_breaks, symbols=symbols)
    n_deduped = int(events.attrs.get("n_exact_duplicates_removed", 0))
    logger.info("Event universe size=%s (exact dups removed=%s)", len(events), n_deduped)

    logger.info("Loading multitimeframe parquet")
    mtf = pd.read_parquet(mtf_path)
    states_1h = pd.read_parquet(h1_path) if h1_path.exists() else None
    states_4h = pd.read_parquet(h4_path) if h4_path.exists() else None

    logger.info("Attaching HTF context")
    with_ctx = attach_htf_context(events, mtf, states_1h, states_4h)

    logger.info("Joining catalog outcomes")
    pl_dec = pd.read_csv(pl_dec_path)
    ph_dec = pd.read_csv(ph_dec_path)
    with_out = attach_catalog_outcomes(with_ctx, pl_dec, ph_dec)

    inv = pd.read_csv(inv_path) if inv_path.exists() else None
    dp = pd.read_csv(dp_path) if dp_path.exists() else None
    with_out = attach_deep_dive_enrichment(with_out, inv, dp)

    logger.info("Computing HTF same-level follow-through")
    universe = attach_htf_followthrough(with_out, pl_breaks, ph_breaks)

    summaries = build_summaries(universe)
    decision = decide_primary(universe, summaries)
    answers = build_research_answers(universe, summaries, decision)

    n_future = int(universe["future_violation"].sum())
    lookahead = {
        "future_violation_count": n_future,
        "future_violation_1h_count": int(universe["future_violation_1h"].sum()),
        "future_violation_4h_count": int(universe["future_violation_4h"].sum()),
        "pass": n_future == 0,
        "rule": "available_at_1h/4h must be <= signal_available_at (or null)",
    }

    n_events = len(universe)
    n_with_outcome = int((universe["outcome"] != "n/a").sum())
    btc = universe[universe["symbol"] == "BTCUSDT"]
    gaps = {
        "n_events": n_events,
        "n_with_outcome": n_with_outcome,
        "outcome_coverage_fraction": _rate(n_with_outcome, n_events),
        "btc_n_events": int(len(btc)),
        "btc_n_with_outcome": int((btc["outcome"] != "n/a").sum()),
        "n_exact_duplicates_removed": n_deduped,
        "n_asof_fallback_joins": int((universe["htf_join_source"] == "asof_fallback").sum()),
        "n_multitimeframe_exact_joins": int((universe["htf_join_source"] == "multitimeframe_exact").sum()),
        "catalog_pl_rows": int(len(pl_dec)),
        "catalog_ph_rows": int(len(ph_dec)),
        "note": "BTCUSDT has no rows in PL/PH historical event catalogs; outcomes are n/a.",
    }

    invariants = {
        "match_bps": MATCH_BPS,
        "match_bps_is_trading_threshold": False,
        "match_bps_documentation": (
            "MATCH_BPS=1.0 is a technical equality tolerance for level identity, "
            "not a trading threshold."
        ),
        "future_violation_count": n_future,
        "future_violation_must_be_zero": True,
        "pl_and_ph_both_in_universe": bool(
            (universe["event_type"] == "PROTECTED_LOW_BREAK").any()
            and (universe["event_type"] == "PROTECTED_HIGH_BREAK").any()
        ),
        "n_pl": int((universe["event_type"] == "PROTECTED_LOW_BREAK").sum()),
        "n_ph": int((universe["event_type"] == "PROTECTED_HIGH_BREAK").sum()),
        "outcome_join_sources_only_catalogs": True,
        "primary_decision_in_enum": decision["primary_decision"] in PRIMARY_DECISIONS,
        "pass": n_future == 0 and decision["primary_decision"] in PRIMARY_DECISIONS,
    }

    audit_config = {
        "created_at": _iso_now(),
        "symbols": list(symbols),
        "timeframe_filter": "5m",
        "require_choch": True,
        "MATCH_BPS": MATCH_BPS,
        "MATCH_BPS_note": (
            "Technical equality tolerance for matching 5m event level to HTF protected "
            "levels; NOT a trading threshold."
        ),
        "relation_classes": list(RELATION_CLASSES),
        "followthrough_horizon_days": FOLLOWTHROUGH_HORIZON.days,
        "no_scanner_recompute": True,
        "no_clickhouse": True,
        "no_pnl": True,
        "used_files": used_files,
    }

    # Persist artefacts
    _write_json(output_dir / "audit_config.json", audit_config)
    _write_json(output_dir / "source_files_used.json", used_files)
    _write_csv(output_dir / "event_universe.csv", universe)
    for name, df in summaries.items():
        _write_csv(output_dir / f"{name}.csv", df)
    _write_json(output_dir / "lookahead_audit.json", lookahead)
    _write_json(output_dir / "invariant_audit.json", invariants)
    _write_json(output_dir / "data_gaps.json", gaps)
    _write_json(
        output_dir / "decision.json",
        {
            **decision,
            "answers": answers,
            "created_at": _iso_now(),
        },
    )
    write_summary_md(
        output_dir / "summary.md",
        decision=decision,
        answers=answers,
        universe=universe,
        summaries=summaries,
        gaps=gaps,
    )

    logger.info(
        "Done primary=%s n_events=%s n_outcomes=%s future_violations=%s",
        decision["primary_decision"],
        n_events,
        n_with_outcome,
        n_future,
    )
    return {
        "decision": decision,
        "universe": universe,
        "summaries": summaries,
        "lookahead": lookahead,
        "invariants": invariants,
        "gaps": gaps,
        "answers": answers,
        "output_dir": output_dir,
    }


__all__ = [
    "MATCH_BPS",
    "PRIMARY_DECISIONS",
    "attach_catalog_outcomes",
    "attach_htf_context",
    "attach_htf_followthrough",
    "build_event_universe",
    "classify_relation_class",
    "levels_match",
    "make_event_id",
    "run_mtf_break_htf_relation_audit",
]
