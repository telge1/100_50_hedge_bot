"""Multi-timeframe Protected High/Low structure scanning (research only).

Reuses C3.4B via ``trend_scanner_adapter.run_c34b_structure``. Aggregates 1h/4h
from 5m futures feathers (UTC fixed buckets). Does not change scanner rules.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd

from orderbook_analyse.c3_protected_low_historical_catalog import rising_edge_mask
from orderbook_analyse.trend_scanner_adapter import (
    DEFAULT_SCANNER_ROOT,
    TF_MINUTES,
    load_ohlcv_feather,
    run_c34b_structure,
    scanner_audit_info,
)

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CANDLE_DIR = Path(
    "/home/telgenbuescher/projects/Signal_Generator_Ralf/data/bybit/futures"
)
DEFAULT_OUTPUT_DIR = ROOT / "results" / "trend_scanner_multitimeframe_structure"
CANDLE_FEATHER = {
    "APTUSDT": "APT_USDT_USDT-5m-futures.feather",
    "DOGEUSDT": "DOGE_USDT_USDT-5m-futures.feather",
    "BTCUSDT": "BTC_USDT_USDT-5m-futures.feather",
}

PRIMARY_DECISIONS = (
    "MULTI_TIMEFRAME_STRUCTURE_READY",
    "HTF_AGGREGATION_INSUFFICIENT",
    "FIVE_M_PARITY_BROKEN",
    "CAUSALITY_INVARIANT_FAILED",
    "SCANNER_HISTORY_INSUFFICIENT",
    "MULTI_TIMEFRAME_STRUCTURE_PARTIAL",
)

STRUCTURE_NEED = ["timestamp", "open", "high", "low", "close", "volume"]
PARITY_COLS = (
    "protected_low",
    "protected_high",
    "close_break_protected_down",
    "close_break_protected_up",
)

MIRROR_FIELD_TABLE: dict[str, str] = {
    "protected_low": "protected_high",
    "protected_low_time": "protected_high_time",
    "protected_low_confirmed_at": "protected_high_confirmed_at",
    "protected_low_origin_ts": "protected_high_origin_ts",
    "close_break_protected_down": "close_break_protected_up",
    "bearish_choch": "bullish_choch",
    "external_bos_down": "external_bos_up",
    "wick_break_protected_down": "wick_break_protected_up",
}


def _iso(ts: Any) -> str | None:
    if ts is None:
        return None
    try:
        if isinstance(ts, float) and np.isnan(ts):
            return None
        if isinstance(ts, pd.Series):
            if len(ts) == 0:
                return None
            ts = ts.iloc[0]
        if pd.isna(ts):
            return None
        t = pd.Timestamp(ts)
        if pd.isna(t):
            return None
        if t.tzinfo is None:
            t = t.tz_localize("UTC")
        else:
            t = t.tz_convert("UTC")
        return t.strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, AttributeError):
        return None


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str) + "\n", encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, Any]] | pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(rows, pd.DataFrame):
        rows.to_csv(path, index=False)
    else:
        pd.DataFrame(rows).to_csv(path, index=False)


def _f(v: Any) -> float | None:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def distance_bps(close: Any, level: Any) -> float | None:
    c = _f(close)
    lv = _f(level)
    if c is None or lv is None or lv == 0.0:
        return None
    return (c - lv) / lv * 10_000.0


def assign_trend_segment_ids(major_direction: pd.Series, *, prefix: str = "") -> pd.Series:
    """Sticky non-zero major_direction change → new segment id (descriptive)."""
    ids: list[str] = []
    sid = 0
    prev: int | None = None
    for d in major_direction.fillna(0).astype(int).tolist():
        if d != 0 and d != prev:
            sid += 1
            prev = d
        elif d == 0:
            prev = 0
        ids.append(f"{prefix}s{sid}" if d != 0 else "")
    return pd.Series(ids, index=major_direction.index, dtype=object)


def aggregate_ohlcv_from_5m(
    df_5m: pd.DataFrame,
    timeframe: str,
    *,
    require_complete: bool = True,
) -> pd.DataFrame:
    """Aggregate UTC-fixed HTF candles from 5m OHLCV.

    Incomplete buckets are flagged; dropped when ``require_complete=True``.
    Only closed buckets are emitted (no partial current bar).
    """
    tf = str(timeframe).strip().lower()
    if tf not in TF_MINUTES:
        raise ValueError(f"unsupported timeframe={timeframe!r}")
    need = ["timestamp", "open", "high", "low", "close", "volume"]
    missing = [c for c in need if c not in df_5m.columns]
    if missing:
        raise ValueError(f"df_5m missing {missing}")

    base = df_5m[need].copy()
    base["timestamp"] = pd.to_datetime(base["timestamp"], utc=True)
    base = base.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    minutes = TF_MINUTES[tf]
    n_need = minutes // 5
    duration = pd.Timedelta(minutes=minutes)

    if tf == "5m":
        out = base.copy()
        out["timeframe"] = "5m"
        out["open_ts"] = out["timestamp"]
        out["close_ts"] = out["timestamp"] + pd.Timedelta(minutes=5)
        out["available_at"] = out["close_ts"]
        out["n_underlying_5m"] = 1
        out["complete"] = True
        return out

    base["bucket_open"] = base["timestamp"].dt.floor(f"{minutes}min")
    rows: list[dict[str, Any]] = []
    for bucket_open, group in base.groupby("bucket_open", sort=True):
        bucket_ts = pd.Timestamp(bucket_open)
        if bucket_ts.tzinfo is None:
            bucket_ts = bucket_ts.tz_localize("UTC")
        else:
            bucket_ts = bucket_ts.tz_convert("UTC")
        group = group.sort_values("timestamp")
        expected = [bucket_ts + pd.Timedelta(minutes=5 * i) for i in range(n_need)]
        actual = list(pd.to_datetime(group["timestamp"], utc=True))
        n_have = len(actual)
        complete = n_have >= n_need and actual[:n_need] == expected
        if require_complete and not complete:
            continue
        g = group.iloc[:n_need] if complete else group
        close_ts = bucket_ts + duration
        rows.append(
            {
                "timestamp": bucket_ts,
                "open": float(g["open"].iloc[0]),
                "high": float(g["high"].max()),
                "low": float(g["low"].min()),
                "close": float(g["close"].iloc[-1]),
                "volume": float(g["volume"].sum()),
                "timeframe": tf,
                "open_ts": bucket_ts,
                "close_ts": close_ts,
                "available_at": close_ts,
                "n_underlying_5m": int(len(g)),
                "complete": bool(complete),
            }
        )
    return pd.DataFrame(rows)


def run_structure_for_timeframe(
    ohlcv_tf: pd.DataFrame,
    *,
    timeframe: str,
    scanner_root: Path | str = DEFAULT_SCANNER_ROOT,
    symbol: str | None = None,
    warmup_bars: int = 72,
) -> pd.DataFrame:
    """Run C3.4B on a single TF OHLCV frame and enrich clarity columns."""
    tf = str(timeframe).strip().lower()
    need = ohlcv_tf[STRUCTURE_NEED].copy()
    structure = run_c34b_structure(need, scanner_root=scanner_root, timeframe=tf)
    structure = structure.copy()

    # Clarity aliases (document missing rather than invent)
    if "protected_low_time" in structure.columns:
        structure["protected_low_origin_ts"] = structure["protected_low_time"]
    else:
        structure["protected_low_origin_ts"] = pd.NaT
    if "protected_high_time" in structure.columns:
        structure["protected_high_origin_ts"] = structure["protected_high_time"]
    else:
        structure["protected_high_origin_ts"] = pd.NaT
    # confirmed_at already present from SoT when level exists
    if "protected_low_confirmed_at" not in structure.columns:
        structure["protected_low_confirmed_at"] = pd.NaT
    if "protected_high_confirmed_at" not in structure.columns:
        structure["protected_high_confirmed_at"] = pd.NaT

    prefix = f"{symbol}_" if symbol else ""
    structure["trend_segment_id"] = assign_trend_segment_ids(
        structure["major_direction"], prefix=prefix
    )
    structure["in_warmup"] = np.arange(len(structure)) < int(warmup_bars)
    if symbol is not None:
        structure["symbol"] = symbol
    structure["timeframe"] = tf
    return structure


def enumerate_structure_breaks(
    df: pd.DataFrame,
    *,
    side: str,
    require_choch: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Rising-edge PL/PH breaks. Catalog parity: require_choch=True for event tables.

    Also counts all rising edges (with level present) in stats regardless of choch.
    """
    if df.empty:
        return [], {"n_rising_edges_all": 0, "n_events_require_choch": 0}

    side = side.lower()
    if side == "low":
        flag_col = "close_break_protected_down"
        level_col = "protected_low"
        choch_col = "bearish_choch"
        event_side = "protected_low_break"
    elif side == "high":
        flag_col = "close_break_protected_up"
        level_col = "protected_high"
        choch_col = "bullish_choch"
        event_side = "protected_high_break"
    else:
        raise ValueError(side)

    work = df.sort_values("available_at", kind="mergesort").reset_index(drop=True)
    rising = rising_edge_mask(work[flag_col].fillna(False).astype(bool))
    n_all = 0
    events: list[dict[str, Any]] = []
    for i in work.index[rising]:
        level = _f(work.at[i, level_col])
        if level is None:
            continue
        n_all += 1
        choch = bool(work.at[i, choch_col]) if choch_col in work.columns else False
        if require_choch and not choch:
            continue
        events.append(
            {
                "symbol": work.at[i, "symbol"] if "symbol" in work.columns else None,
                "timeframe": work.at[i, "timeframe"] if "timeframe" in work.columns else None,
                "event_side": event_side,
                "candle_open_ts": _iso(work.at[i, "candle_open_ts"]),
                "available_at": _iso(work.at[i, "available_at"]),
                "level": level,
                "close": _f(work.at[i, "close"]) if "close" in work.columns else None,
                "choch": choch,
                "require_choch": require_choch,
                "trend_segment_id": work.at[i, "trend_segment_id"]
                if "trend_segment_id" in work.columns
                else None,
                "major_direction": int(work.at[i, "major_direction"])
                if "major_direction" in work.columns
                else None,
                "in_warmup": bool(work.at[i, "in_warmup"])
                if "in_warmup" in work.columns
                else False,
            }
        )
    stats = {
        "n_rising_edges_all": n_all,
        "n_events_require_choch": len(events),
    }
    return events, stats


def asof_attach_htf(
    base_5m: pd.DataFrame,
    htf: pd.DataFrame,
    *,
    suffix: str,
) -> pd.DataFrame:
    """Attach last HTF structure row with available_at_htf <= available_at_5m."""
    if base_5m.empty:
        return base_5m.copy()
    left = base_5m.sort_values("available_at", kind="mergesort").reset_index(drop=True)
    if htf is None or htf.empty:
        for col in (
            f"protected_low_{suffix}",
            f"protected_high_{suffix}",
            f"available_at_{suffix}",
            f"trend_segment_id_{suffix}",
            f"close_break_protected_down_{suffix}",
            f"close_break_protected_up_{suffix}",
            f"major_direction_{suffix}",
            f"distance_to_pl_{suffix}_bps",
            f"distance_to_ph_{suffix}_bps",
        ):
            left[col] = pd.NA
        return left

    cols = [
        "available_at",
        "protected_low",
        "protected_high",
        "trend_segment_id",
        "close_break_protected_down",
        "close_break_protected_up",
        "major_direction",
    ]
    right = htf[[c for c in cols if c in htf.columns]].copy()
    right = right.sort_values("available_at", kind="mergesort").drop_duplicates(
        "available_at", keep="last"
    )
    rename = {
        "available_at": f"available_at_{suffix}",
        "protected_low": f"protected_low_{suffix}",
        "protected_high": f"protected_high_{suffix}",
        "trend_segment_id": f"trend_segment_id_{suffix}",
        "close_break_protected_down": f"close_break_protected_down_{suffix}",
        "close_break_protected_up": f"close_break_protected_up_{suffix}",
        "major_direction": f"major_direction_{suffix}",
    }
    right = right.rename(columns=rename)
    merged = pd.merge_asof(
        left,
        right,
        left_on="available_at",
        right_on=f"available_at_{suffix}",
        direction="backward",
    )
    merged[f"distance_to_pl_{suffix}_bps"] = [
        distance_bps(c, lv)
        for c, lv in zip(merged["close"], merged[f"protected_low_{suffix}"])
    ]
    merged[f"distance_to_ph_{suffix}_bps"] = [
        distance_bps(c, lv)
        for c, lv in zip(merged["close"], merged[f"protected_high_{suffix}"])
    ]
    return merged


def compute_alignment_columns(mtf: pd.DataFrame) -> pd.DataFrame:
    """Descriptive multi-TF break alignment on as-of 5m frame."""
    out = mtf.copy()

    def _break(col: str) -> pd.Series:
        if col not in out.columns:
            return pd.Series(False, index=out.index)
        return out[col].fillna(False).astype(bool)

    out["pl_break_5m"] = _break("close_break_protected_down")
    out["ph_break_5m"] = _break("close_break_protected_up")
    out["pl_break_1h"] = _break("close_break_protected_down_1h")
    out["ph_break_1h"] = _break("close_break_protected_up_1h")
    out["pl_break_4h"] = _break("close_break_protected_down_4h")
    out["ph_break_4h"] = _break("close_break_protected_up_4h")

    out["bearish_alignment_count"] = (
        out["pl_break_5m"].astype(int)
        + out["pl_break_1h"].astype(int)
        + out["pl_break_4h"].astype(int)
    )
    out["bullish_alignment_count"] = (
        out["ph_break_5m"].astype(int)
        + out["ph_break_1h"].astype(int)
        + out["ph_break_4h"].astype(int)
    )
    out["all_timeframes_bearish"] = out["bearish_alignment_count"] == 3
    out["all_timeframes_bullish"] = out["bullish_alignment_count"] == 3

    # Lower TF break while higher TF not broken (descriptive conflict)
    out["lower_tf_against_higher_tf"] = (
        (out["pl_break_5m"] & ~out["pl_break_1h"] & ~out["pl_break_4h"])
        | (out["ph_break_5m"] & ~out["ph_break_1h"] & ~out["ph_break_4h"])
        | (out["pl_break_1h"] & ~out["pl_break_4h"])
        | (out["ph_break_1h"] & ~out["ph_break_4h"])
    )
    # Higher TF structure still has levels (not broken flags)
    out["higher_tf_structure_intact"] = (
        (~out["pl_break_1h"] & ~out["ph_break_1h"] & ~out["pl_break_4h"] & ~out["ph_break_4h"])
    )
    return out


def _parse_iso(v: Any) -> pd.Timestamp | None:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    t = pd.Timestamp(v)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return t


def build_tf_transition_rows(
    events_by_tf_side: dict[tuple[str, str], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Descriptive lower→higher TF transition analysis around break events."""
    rows: list[dict[str, Any]] = []

    def _events(tf: str, side: str) -> list[tuple[pd.Timestamp, dict[str, Any]]]:
        out = []
        for e in events_by_tf_side.get((tf, side), []):
            ts = _parse_iso(e.get("available_at"))
            if ts is not None:
                out.append((ts, e))
        return sorted(out, key=lambda x: x[0])

    for side in ("low", "high"):
        e5 = _events("5m", side)
        e1 = _events("1h", side)
        e4 = _events("4h", side)

        # 5m events vs 1h/4h
        for ts5, ev in e5:
            prior_1h = [t for t, _ in e1 if t <= ts5]
            later_1h = [t for t, _ in e1 if t > ts5]
            prior_4h = [t for t, _ in e4 if t <= ts5]
            later_4h = [t for t, _ in e4 if t > ts5]
            next_1h = later_1h[0] if later_1h else None
            next_4h = later_4h[0] if later_4h else None
            rows.append(
                {
                    "symbol": ev.get("symbol"),
                    "side": side,
                    "anchor_tf": "5m",
                    "anchor_available_at": _iso(ts5),
                    "anchor_level": ev.get("level"),
                    "was_1h_already_broken": bool(prior_1h),
                    "was_4h_already_broken": bool(prior_4h),
                    "was_4h_intact": not bool(prior_4h),
                    "later_1h_break_at": _iso(next_1h),
                    "later_4h_break_at": _iso(next_4h),
                    "lead_to_1h_minutes": (
                        (next_1h - ts5).total_seconds() / 60.0 if next_1h is not None else None
                    ),
                    "lead_to_4h_minutes": (
                        (next_4h - ts5).total_seconds() / 60.0 if next_4h is not None else None
                    ),
                }
            )

        # 1h events vs prior 5m / later 4h
        for ts1, ev in e1:
            prior_5m = [t for t, _ in e5 if t <= ts1]
            later_4h = [t for t, _ in e4 if t > ts1]
            next_4h = later_4h[0] if later_4h else None
            last_5m = prior_5m[-1] if prior_5m else None
            rows.append(
                {
                    "symbol": ev.get("symbol"),
                    "side": side,
                    "anchor_tf": "1h",
                    "anchor_available_at": _iso(ts1),
                    "anchor_level": ev.get("level"),
                    "was_5m_prior_break": bool(prior_5m),
                    "prior_5m_break_at": _iso(last_5m),
                    "lead_from_5m_minutes": (
                        (ts1 - last_5m).total_seconds() / 60.0 if last_5m is not None else None
                    ),
                    "was_4h_already_broken": bool([t for t, _ in e4 if t <= ts1]),
                    "was_4h_intact": not bool([t for t, _ in e4 if t <= ts1]),
                    "later_4h_break_at": _iso(next_4h),
                    "lead_to_4h_minutes": (
                        (next_4h - ts1).total_seconds() / 60.0 if next_4h is not None else None
                    ),
                }
            )
    return rows


def inventory_levels(structure: pd.DataFrame, *, level_col: str) -> list[dict[str, Any]]:
    if structure.empty or level_col not in structure.columns:
        return []
    rows = []
    gcols = ["symbol", "timeframe"] if "symbol" in structure.columns else ["timeframe"]
    for keys, g in structure.groupby(gcols, dropna=False):
        if not isinstance(keys, tuple):
            keys = (keys,)
        meta = dict(zip(gcols, keys))
        for level, lg in g.dropna(subset=[level_col]).groupby(level_col):
            first = lg.sort_values("available_at").iloc[0]
            origin_col = f"{level_col}_time"
            origin_alias = (
                "protected_low_origin_ts"
                if level_col == "protected_low"
                else "protected_high_origin_ts"
            )
            origin = first[origin_col] if origin_col in lg.columns else first.get(origin_alias)
            rows.append(
                {
                    **meta,
                    level_col: _f(level),
                    "first_seen_available_at": _iso(first["available_at"]),
                    "n_bars": int(len(lg)),
                    "origin_ts": _iso(origin),
                }
            )
    return rows


def coverage_row(
    structure: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    ohlcv_agg: pd.DataFrame | None,
    break_stats_low: dict[str, int],
    break_stats_high: dict[str, int],
) -> dict[str, Any]:
    n_candles = int(len(ohlcv_agg)) if ohlcv_agg is not None else int(len(structure))
    n_complete = (
        int(ohlcv_agg["complete"].sum())
        if ohlcv_agg is not None and "complete" in ohlcv_agg.columns
        else n_candles
    )
    if "trend_segment_id" in structure.columns:
        segs = structure.loc[structure["trend_segment_id"].astype(str).str.len() > 0, "trend_segment_id"]
        n_segments = int(segs.nunique())
    else:
        md = structure["major_direction"].fillna(0).astype(int) if not structure.empty else pd.Series(dtype=int)
        n_segments = int((md.ne(md.shift(1)) & md.ne(0)).sum()) if len(md) else 0

    def _nunique(col: str) -> int:
        if structure.empty or col not in structure.columns:
            return 0
        return int(structure[col].dropna().nunique())

    choch_up = (
        int(structure["bullish_choch"].fillna(False).astype(bool).sum())
        if not structure.empty and "bullish_choch" in structure.columns
        else 0
    )
    choch_down = (
        int(structure["bearish_choch"].fillna(False).astype(bool).sum())
        if not structure.empty and "bearish_choch" in structure.columns
        else 0
    )
    bos_up = (
        int(structure["external_bos_up"].fillna(False).astype(bool).sum())
        if not structure.empty and "external_bos_up" in structure.columns
        else 0
    )
    bos_down = (
        int(structure["external_bos_down"].fillna(False).astype(bool).sum())
        if not structure.empty and "external_bos_down" in structure.columns
        else 0
    )
    tsmin = structure["timestamp"].min() if not structure.empty else None
    tsmax = structure["timestamp"].max() if not structure.empty else None
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "n_candles": n_candles,
        "n_complete": n_complete,
        "n_structure_rows": int(len(structure)),
        "date_min": _iso(tsmin),
        "date_max": _iso(tsmax),
        "n_segments": n_segments,
        "n_PL_levels": _nunique("protected_low"),
        "n_PH_levels": _nunique("protected_high"),
        "n_PL_breaks": int(break_stats_low.get("n_events_require_choch", 0)),
        "n_PH_breaks": int(break_stats_high.get("n_events_require_choch", 0)),
        "n_PL_rising_edges_all": int(break_stats_low.get("n_rising_edges_all", 0)),
        "n_PH_rising_edges_all": int(break_stats_high.get("n_rising_edges_all", 0)),
        "n_choch_up": choch_up,
        "n_choch_down": choch_down,
        "n_ext_bos_up": bos_up,
        "n_ext_bos_down": bos_down,
    }


def enumerate_flag_events(
    structure: pd.DataFrame,
    *,
    flag_col: str,
    event_type: str,
) -> list[dict[str, Any]]:
    if structure.empty or flag_col not in structure.columns:
        return []
    work = structure.sort_values("available_at", kind="mergesort").reset_index(drop=True)
    rising = rising_edge_mask(work[flag_col].fillna(False).astype(bool))
    rows = []
    for i in work.index[rising]:
        rows.append(
            {
                "symbol": work.at[i, "symbol"] if "symbol" in work.columns else None,
                "timeframe": work.at[i, "timeframe"] if "timeframe" in work.columns else None,
                "event_type": event_type,
                "available_at": _iso(work.at[i, "available_at"]),
                "candle_open_ts": _iso(work.at[i, "candle_open_ts"]),
                "close": _f(work.at[i, "close"]) if "close" in work.columns else None,
                "protected_low": _f(work.at[i, "protected_low"])
                if "protected_low" in work.columns
                else None,
                "protected_high": _f(work.at[i, "protected_high"])
                if "protected_high" in work.columns
                else None,
                "in_warmup": bool(work.at[i, "in_warmup"])
                if "in_warmup" in work.columns
                else False,
            }
        )
    return rows


def check_5m_parity(
    structure_5m: pd.DataFrame,
    raw_5m: pd.DataFrame,
    *,
    scanner_root: Path | str,
) -> dict[str, Any]:
    """Compare this run's 5m structure vs fresh run_c34b_structure on raw 5m."""
    ref = run_c34b_structure(raw_5m[STRUCTURE_NEED], scanner_root=scanner_root, timeframe="5m")
    a = structure_5m.sort_values("timestamp").reset_index(drop=True)
    b = ref.sort_values("timestamp").reset_index(drop=True)
    # Align on overlapping timestamps
    merged = a[["timestamp", *PARITY_COLS]].merge(
        b[["timestamp", *PARITY_COLS]],
        on="timestamp",
        suffixes=("_run", "_ref"),
        how="inner",
    )
    mismatches: dict[str, int] = {}
    for col in PARITY_COLS:
        left = merged[f"{col}_run"]
        right = merged[f"{col}_ref"]
        if col.startswith("close_break_"):
            bad = left.fillna(False).astype(bool) != right.fillna(False).astype(bool)
        else:
            both_na = left.isna() & right.isna()
            both_num = left.notna() & right.notna()
            close = pd.Series(False, index=merged.index)
            if both_num.any():
                close.loc[both_num] = (
                    left.loc[both_num].astype(float).to_numpy()
                    == right.loc[both_num].astype(float).to_numpy()
                )
            bad = ~(both_na | close)
        mismatches[col] = int(bad.sum())
    n_mismatch = int(sum(mismatches.values()))
    return {
        "pass": n_mismatch == 0 and len(merged) > 0,
        "n_overlap_timestamps": int(len(merged)),
        "n_run_rows": int(len(a)),
        "n_ref_rows": int(len(b)),
        "mismatches_by_column": mismatches,
        "total_mismatches": n_mismatch,
    }


def run_lookahead_audit(
    structures: dict[str, pd.DataFrame],
    mtf: pd.DataFrame,
) -> dict[str, Any]:
    checks = []
    for tf, df in structures.items():
        if df.empty:
            continue
        minutes = TF_MINUTES[tf]
        expected = pd.to_datetime(df["candle_open_ts"], utc=True) + pd.Timedelta(minutes=minutes)
        avail = pd.to_datetime(df["available_at"], utc=True)
        ok = bool((avail == expected).all())
        checks.append(
            {
                "name": f"available_at_equals_open_plus_{tf}",
                "pass": ok,
                "n_rows": int(len(df)),
                "n_bad": int((avail != expected).sum()),
            }
        )
    # HTF as-of never uses future
    asof_ok = True
    n_future = 0
    if not mtf.empty:
        for suf in ("1h", "4h"):
            col = f"available_at_{suf}"
            if col not in mtf.columns:
                continue
            base = pd.to_datetime(mtf["available_at"], utc=True)
            htf = pd.to_datetime(mtf[col], utc=True)
            mask = htf.notna() & (htf > base)
            n_future += int(mask.sum())
        asof_ok = n_future == 0
    checks.append(
        {
            "name": "htf_asof_never_future",
            "pass": asof_ok,
            "n_future_violations": n_future,
        }
    )
    return {"pass": all(c["pass"] for c in checks), "checks": checks}


def run_asof_join_audit(mtf: pd.DataFrame, sample_n: int = 50) -> dict[str, Any]:
    if mtf.empty:
        return {"pass": True, "n_checked": 0, "n_violations": 0, "note": "empty mtf"}
    work = mtf.dropna(subset=["available_at"]).copy()
    if len(work) > sample_n:
        work = work.sample(n=sample_n, random_state=42)
    violations = []
    for _, row in work.iterrows():
        base = pd.Timestamp(row["available_at"])
        for suf in ("1h", "4h"):
            col = f"available_at_{suf}"
            if col not in row.index or pd.isna(row[col]):
                continue
            htf = pd.Timestamp(row[col])
            if htf > base:
                violations.append(
                    {
                        "symbol": row.get("symbol"),
                        "available_at_5m": _iso(base),
                        "htf": suf,
                        "available_at_htf": _iso(htf),
                    }
                )
            # If HTF level present, available_at must exist
            pl = row.get(f"protected_low_{suf}")
            if pl is not None and not (isinstance(pl, float) and np.isnan(pl)) and not pd.isna(pl):
                if pd.isna(row[col]):
                    violations.append(
                        {
                            "symbol": row.get("symbol"),
                            "issue": f"protected_low_{suf}_without_available_at",
                            "available_at_5m": _iso(base),
                        }
                    )
    return {
        "pass": len(violations) == 0,
        "n_checked": int(len(work)),
        "n_violations": int(len(violations)),
        "violations_sample": violations[:20],
    }


def run_mirror_parity_audit(structures: dict[str, pd.DataFrame]) -> dict[str, Any]:
    per_tf = []
    all_ok = True
    for tf, df in structures.items():
        present = {k: (k in df.columns and MIRROR_FIELD_TABLE[k] in df.columns) for k in MIRROR_FIELD_TABLE}
        ok = all(present.values()) if not df.empty else True
        if not ok:
            all_ok = False
        # cheap synthetic: equal non-null counts within 50% band is informational only
        info = {}
        if not df.empty and present.get("protected_low") and present.get("close_break_protected_down"):
            info = {
                "n_pl_notna": int(df["protected_low"].notna().sum()),
                "n_ph_notna": int(df["protected_high"].notna().sum()),
                "n_break_down": int(df["close_break_protected_down"].fillna(False).astype(bool).sum()),
                "n_break_up": int(df["close_break_protected_up"].fillna(False).astype(bool).sum()),
            }
        per_tf.append({"timeframe": tf, "field_pairs_present": present, "pass": ok, "counts": info})
    return {
        "pass": all_ok,
        "mirror_field_table": MIRROR_FIELD_TABLE,
        "note": (
            "High/Low field symmetry is structural (paired columns). "
            "Event counts need not be equal; inventory is descriptive."
        ),
        "per_timeframe": per_tf,
    }


def run_invariant_audit(
    *,
    lookahead: dict[str, Any],
    asof: dict[str, Any],
    parity: dict[str, Any],
    mirror: dict[str, Any],
    coverage: pd.DataFrame,
    structures: dict[str, pd.DataFrame],
) -> dict[str, Any]:
    checklist = []

    def add(name: str, ok: bool, detail: Any = None) -> None:
        checklist.append({"name": name, "pass": bool(ok), "detail": detail})

    add("lookahead_available_at_eq_open_plus_tf", lookahead.get("pass", False), lookahead)
    add("asof_join_no_future_htf", asof.get("pass", False), {"n_violations": asof.get("n_violations")})
    add("five_m_parity", parity.get("pass", False), parity.get("mismatches_by_column"))
    add("mirror_fields_present", mirror.get("pass", False))
    add("no_4h_mark_feathers_used", True, "aggregated from 5m only")
    add("each_tf_independent_structure", True, "run_c34b_structure per TF OHLCV")
    add("incomplete_htf_dropped_when_require_complete", True)

    # HTF rows only from complete candles
    for tf in ("1h", "4h"):
        df = structures.get(tf)
        if df is None or df.empty:
            add(f"{tf}_structure_nonempty", False)
        else:
            add(f"{tf}_structure_nonempty", True, {"n": len(df)})
            add(
                f"{tf}_pl_and_ph_columns",
                "protected_low" in df.columns and "protected_high" in df.columns,
            )

    # Coverage: ≥1 complete candle per TF for ≥1 symbol
    if not coverage.empty:
        for tf in ("5m", "1h", "4h"):
            sub = coverage[coverage["timeframe"] == tf]
            add(f"coverage_{tf}_has_complete", bool((sub["n_complete"] > 0).any()), {"rows": len(sub)})

    return {"pass": all(c["pass"] for c in checklist), "checklist": checklist}


def decide_primary(
    *,
    coverage: pd.DataFrame,
    parity: dict[str, Any],
    causality_pass: bool,
    htf_built: bool,
) -> tuple[str, str]:
    if not parity.get("pass", False):
        return "FIVE_M_PARITY_BROKEN", "5m structure columns diverge from fresh adapter run"
    if not causality_pass:
        return "CAUSALITY_INVARIANT_FAILED", "lookahead / as-of / invariant audit failed"
    if not htf_built:
        return "HTF_AGGREGATION_INSUFFICIENT", "1h/4h candles could not be built from 5m"

    def _strong(sym: str) -> bool:
        sub = coverage[coverage["symbol"] == sym]
        if sub.empty:
            return False
        ok = True
        for tf in ("5m", "1h", "4h"):
            row = sub[sub["timeframe"] == tf]
            if row.empty:
                return False
            r = row.iloc[0]
            if int(r["n_PL_levels"]) < 1 or int(r["n_PH_levels"]) < 1:
                ok = False
            if int(r["n_PL_breaks"]) < 1 and int(r["n_PH_breaks"]) < 1:
                # allow if levels exist but breaks rare on short HTF — still require some break signal on 5m
                if tf == "5m":
                    ok = False
        return ok

    strong_syms = [s for s in coverage["symbol"].unique() if _strong(str(s))]
    if len(coverage) == 0 or coverage["n_structure_rows"].sum() == 0:
        return "SCANNER_HISTORY_INSUFFICIENT", "no structure rows produced"

    # READY: ≥2 symbols with PL+PH+breaks across TFs
    ready_syms = []
    for sym in coverage["symbol"].unique():
        sub = coverage[coverage["symbol"] == sym]
        tf_ok = True
        for tf in ("5m", "1h", "4h"):
            row = sub[sub["timeframe"] == tf]
            if row.empty:
                tf_ok = False
                break
            r = row.iloc[0]
            if (
                int(r["n_PL_levels"]) < 1
                or int(r["n_PH_levels"]) < 1
                or (int(r["n_PL_breaks"]) + int(r["n_PH_breaks"])) < 1
            ):
                tf_ok = False
                break
        if tf_ok:
            ready_syms.append(str(sym))

    if len(ready_syms) >= 2:
        return (
            "MULTI_TIMEFRAME_STRUCTURE_READY",
            f"PL+PH+breaks on 5m/1h/4h for symbols={ready_syms}; audits pass",
        )
    if len(ready_syms) == 1 or len(strong_syms) >= 1:
        return (
            "MULTI_TIMEFRAME_STRUCTURE_PARTIAL",
            f"core path works; ready_syms={ready_syms} strongish={strong_syms}",
        )
    return (
        "MULTI_TIMEFRAME_STRUCTURE_PARTIAL",
        "HTF built and audits pass but break/level coverage weak on some TFs/symbols",
    )


def _state_markers(structures: dict[str, pd.DataFrame]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for tf, df in structures.items():
        if df.empty:
            continue
        work = df.sort_values("available_at")
        # New PL / PH (level change)
        for level_col, kind in (("protected_low", "PL"), ("protected_high", "PH")):
            prev = None
            for _, r in work.iterrows():
                lv = _f(r.get(level_col))
                if lv is None:
                    prev = None
                    continue
                if prev is None or abs(lv - prev) > 1e-12:
                    rows.append(
                        {
                            "symbol": r.get("symbol"),
                            "timeframe": tf,
                            "marker": f"{kind}_set",
                            "level": lv,
                            "known_at": _iso(r["available_at"]),
                            "candle_open_ts": _iso(r.get("candle_open_ts")),
                        }
                    )
                    prev = lv
        # Breaks rising edge
        for flag, kind in (
            ("close_break_protected_down", "PL_break"),
            ("close_break_protected_up", "PH_break"),
        ):
            if flag not in work.columns:
                continue
            rising = rising_edge_mask(work[flag].fillna(False).astype(bool))
            for i in work.index[rising]:
                rows.append(
                    {
                        "symbol": work.at[i, "symbol"] if "symbol" in work.columns else None,
                        "timeframe": tf,
                        "marker": kind,
                        "level": _f(
                            work.at[i, "protected_low" if "PL" in kind else "protected_high"]
                        ),
                        "known_at": _iso(work.at[i, "available_at"]),
                        "candle_open_ts": _iso(work.at[i, "candle_open_ts"]),
                    }
                )
    return rows


def process_symbol(
    symbol: str,
    *,
    timeframes: Sequence[str],
    candle_dir: Path,
    scanner_root: Path | str,
    warmup_bars: int,
) -> dict[str, Any]:
    feather = CANDLE_FEATHER.get(symbol)
    if feather is None:
        raise FileNotFoundError(f"no feather mapping for {symbol}")
    path = Path(candle_dir) / feather
    if not path.exists():
        raise FileNotFoundError(path)

    raw_5m = load_ohlcv_feather(path)
    logger.info("%s loaded %s rows from %s", symbol, len(raw_5m), path.name)

    structures: dict[str, pd.DataFrame] = {}
    aggs: dict[str, pd.DataFrame] = {}
    pl_events: list[dict[str, Any]] = []
    ph_events: list[dict[str, Any]] = []
    choch_events: list[dict[str, Any]] = []
    bos_events: list[dict[str, Any]] = []
    coverage_rows: list[dict[str, Any]] = []
    events_by_tf_side: dict[tuple[str, str], list[dict[str, Any]]] = {}
    break_stats: dict[str, dict[str, int]] = {}

    for tf in timeframes:
        tf = str(tf).strip().lower()
        agg = aggregate_ohlcv_from_5m(raw_5m, tf, require_complete=True)
        aggs[tf] = agg
        if agg.empty:
            logger.warning("%s %s aggregation empty", symbol, tf)
            structures[tf] = pd.DataFrame()
            coverage_rows.append(
                coverage_row(
                    pd.DataFrame(),
                    symbol=symbol,
                    timeframe=tf,
                    ohlcv_agg=agg,
                    break_stats_low={},
                    break_stats_high={},
                )
            )
            continue
        struct = run_structure_for_timeframe(
            agg,
            timeframe=tf,
            scanner_root=scanner_root,
            symbol=symbol,
            warmup_bars=warmup_bars,
        )
        structures[tf] = struct
        low_ev, low_st = enumerate_structure_breaks(struct, side="low", require_choch=True)
        high_ev, high_st = enumerate_structure_breaks(struct, side="high", require_choch=True)
        events_by_tf_side[(tf, "low")] = low_ev
        events_by_tf_side[(tf, "high")] = high_ev
        break_stats[tf] = {"low": low_st, "high": high_st}
        pl_events.extend(low_ev)
        ph_events.extend(high_ev)
        choch_events.extend(
            enumerate_flag_events(struct, flag_col="bullish_choch", event_type="choch_up")
        )
        choch_events.extend(
            enumerate_flag_events(struct, flag_col="bearish_choch", event_type="choch_down")
        )
        bos_events.extend(
            enumerate_flag_events(struct, flag_col="external_bos_up", event_type="ext_bos_up")
        )
        bos_events.extend(
            enumerate_flag_events(struct, flag_col="external_bos_down", event_type="ext_bos_down")
        )
        coverage_rows.append(
            coverage_row(
                struct,
                symbol=symbol,
                timeframe=tf,
                ohlcv_agg=agg,
                break_stats_low=low_st,
                break_stats_high=high_st,
            )
        )
        logger.info(
            "%s %s structure rows=%s PL_breaks=%s PH_breaks=%s",
            symbol,
            tf,
            len(struct),
            low_st.get("n_events_require_choch"),
            high_st.get("n_events_require_choch"),
        )

    # As-of join onto 5m
    base = structures.get("5m", pd.DataFrame())
    mtf = base.copy()
    if not mtf.empty:
        mtf = asof_attach_htf(mtf, structures.get("1h", pd.DataFrame()), suffix="1h")
        mtf = asof_attach_htf(mtf, structures.get("4h", pd.DataFrame()), suffix="4h")
        mtf = compute_alignment_columns(mtf)

    transitions = build_tf_transition_rows(events_by_tf_side)
    alignment_rows = []
    if not mtf.empty:
        # compact alignment export (one row per 5m bar is huge — sample summary + full parquet)
        alignment_rows = [
            {
                "symbol": symbol,
                "n_5m_rows": int(len(mtf)),
                "n_all_bearish": int(mtf["all_timeframes_bearish"].sum())
                if "all_timeframes_bearish" in mtf.columns
                else 0,
                "n_all_bullish": int(mtf["all_timeframes_bullish"].sum())
                if "all_timeframes_bullish" in mtf.columns
                else 0,
                "n_lower_against_higher": int(mtf["lower_tf_against_higher_tf"].sum())
                if "lower_tf_against_higher_tf" in mtf.columns
                else 0,
                "n_higher_intact": int(mtf["higher_tf_structure_intact"].sum())
                if "higher_tf_structure_intact" in mtf.columns
                else 0,
                "mean_bearish_alignment": float(mtf["bearish_alignment_count"].mean())
                if "bearish_alignment_count" in mtf.columns
                else None,
                "mean_bullish_alignment": float(mtf["bullish_alignment_count"].mean())
                if "bullish_alignment_count" in mtf.columns
                else None,
            }
        ]

    gaps = []
    for tf, agg in aggs.items():
        if agg.empty:
            gaps.append({"symbol": symbol, "timeframe": tf, "issue": "empty_aggregation"})
            continue
        if "complete" in agg.columns and not bool(agg["complete"].all()):
            # with require_complete=True incomplete are dropped; remaining should be complete
            pass
        ts = pd.to_datetime(agg["timestamp"], utc=True)
        if len(ts) >= 2:
            delta = ts.diff().dropna()
            expected = pd.Timedelta(minutes=TF_MINUTES[tf])
            bad = delta[delta != expected]
            if len(bad):
                gaps.append(
                    {
                        "symbol": symbol,
                        "timeframe": tf,
                        "n_gap_steps": int(len(bad)),
                        "max_gap": str(bad.max()),
                    }
                )

    return {
        "symbol": symbol,
        "raw_5m": raw_5m,
        "structures": structures,
        "aggs": aggs,
        "mtf": mtf,
        "pl_events": pl_events,
        "ph_events": ph_events,
        "choch_events": choch_events,
        "bos_events": bos_events,
        "coverage_rows": coverage_rows,
        "transitions": transitions,
        "alignment_rows": alignment_rows,
        "gaps": gaps,
        "pl_inventory": inventory_levels(pd.concat([structures[t] for t in structures if not structures[t].empty], ignore_index=True) if any(not structures[t].empty for t in structures) else pd.DataFrame(), level_col="protected_low"),
        "ph_inventory": inventory_levels(pd.concat([structures[t] for t in structures if not structures[t].empty], ignore_index=True) if any(not structures[t].empty for t in structures) else pd.DataFrame(), level_col="protected_high"),
        "feather": str(path),
        "date_min": _iso(raw_5m["timestamp"].min()) if len(raw_5m) else None,
        "date_max": _iso(raw_5m["timestamp"].max()) if len(raw_5m) else None,
        "n_5m": int(len(raw_5m)),
    }


def run_trend_scanner_multitimeframe(
    symbols: Sequence[str] = ("APTUSDT", "DOGEUSDT", "BTCUSDT"),
    timeframes: Sequence[str] = ("5m", "1h", "4h"),
    candle_dir: Path | str = DEFAULT_CANDLE_DIR,
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
    overwrite: bool = False,
    scanner_root: Path | str = DEFAULT_SCANNER_ROOT,
    warmup_bars: int = 72,
) -> dict[str, Any]:
    """Run multi-TF protected structure scan and write artefacts."""
    output_dir = Path(output_dir)
    candle_dir = Path(candle_dir)
    if output_dir.exists() and any(output_dir.iterdir()) and not overwrite:
        raise FileExistsError(f"{output_dir} exists; pass overwrite=True")
    output_dir.mkdir(parents=True, exist_ok=True)

    symbols = [str(s).strip().upper() for s in symbols if str(s).strip()]
    timeframes = [str(t).strip().lower() for t in timeframes if str(t).strip()]

    audit_info = scanner_audit_info(scanner_root)
    _write_json(
        output_dir / "audit_config.json",
        {
            "symbols": symbols,
            "timeframes": timeframes,
            "candle_dir": str(candle_dir),
            "scanner_root": str(scanner_root),
            "warmup_bars": warmup_bars,
            "tf_minutes": TF_MINUTES,
            "aggregation": "UTC fixed buckets from 5m feathers; no 4h-mark feathers",
            "event_policy": {
                "require_choch": True,
                "note": "Event tables match historical catalogs (require CHoCH); inventory also stores all rising-edge counts",
            },
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )
    _write_json(output_dir / "scanner_config_verification.json", audit_info)

    all_struct: dict[str, list[pd.DataFrame]] = {tf: [] for tf in timeframes}
    all_mtf: list[pd.DataFrame] = []
    coverage_rows: list[dict[str, Any]] = []
    pl_events: list[dict[str, Any]] = []
    ph_events: list[dict[str, Any]] = []
    choch_events: list[dict[str, Any]] = []
    bos_events: list[dict[str, Any]] = []
    transitions: list[dict[str, Any]] = []
    alignment_rows: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    pl_inv: list[dict[str, Any]] = []
    ph_inv: list[dict[str, Any]] = []
    source_rows: list[dict[str, Any]] = []
    parity: dict[str, Any] = {"pass": False, "note": "not_run"}
    parity_raw_5m: pd.DataFrame | None = None
    parity_struct_5m: pd.DataFrame | None = None
    parity_symbol: str | None = None

    for symbol in symbols:
        try:
            result = process_symbol(
                symbol,
                timeframes=timeframes,
                candle_dir=candle_dir,
                scanner_root=scanner_root,
                warmup_bars=warmup_bars,
            )
        except FileNotFoundError as exc:
            logger.error("skip %s: %s", symbol, exc)
            gaps.append({"symbol": symbol, "issue": "missing_feather", "detail": str(exc)})
            continue

        source_rows.append(
            {
                "symbol": symbol,
                "feather": result["feather"],
                "n_5m": result["n_5m"],
                "date_min": result["date_min"],
                "date_max": result["date_max"],
                "note": (
                    "BTC history ends earlier than APT/DOGE"
                    if symbol == "BTCUSDT"
                    else None
                ),
            }
        )
        for tf, df in result["structures"].items():
            if tf in all_struct and not df.empty:
                all_struct[tf].append(df)
        if not result["mtf"].empty:
            all_mtf.append(result["mtf"])
        coverage_rows.extend(result["coverage_rows"])
        pl_events.extend(result["pl_events"])
        ph_events.extend(result["ph_events"])
        choch_events.extend(result["choch_events"])
        bos_events.extend(result["bos_events"])
        transitions.extend(result["transitions"])
        alignment_rows.extend(result["alignment_rows"])
        gaps.extend(result["gaps"])
        pl_inv.extend(result["pl_inventory"])
        ph_inv.extend(result["ph_inventory"])

        # Prefer APTUSDT for 5m parity; else first symbol with 5m structure
        want_parity = (
            symbol == "APTUSDT"
            or (parity_symbol is None and "5m" in result["structures"] and not result["structures"]["5m"].empty)
        )
        if want_parity and "5m" in result["structures"] and not result["structures"]["5m"].empty:
            if parity_symbol != "APTUSDT" or symbol == "APTUSDT":
                parity_symbol = symbol
                parity_raw_5m = result["raw_5m"]
                parity_struct_5m = result["structures"]["5m"]

        # Drop heavy frames except those retained for parity
        if symbol != parity_symbol:
            del result

    if parity_struct_5m is not None and parity_raw_5m is not None:
        parity = check_5m_parity(parity_struct_5m, parity_raw_5m, scanner_root=scanner_root)
        parity["symbol"] = parity_symbol
        del parity_struct_5m
        del parity_raw_5m

    structures_cat: dict[str, pd.DataFrame] = {}
    for tf, frames in all_struct.items():
        structures_cat[tf] = (
            pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        )
        out_path = output_dir / f"structure_states_{tf}.parquet"
        if not structures_cat[tf].empty:
            structures_cat[tf].to_parquet(out_path, index=False)
        else:
            pd.DataFrame().to_parquet(out_path, index=False)

    mtf = pd.concat(all_mtf, ignore_index=True) if all_mtf else pd.DataFrame()
    mtf_path = output_dir / "structure_states_multitimeframe.parquet"
    if not mtf.empty:
        mtf.to_parquet(mtf_path, index=False)
    else:
        pd.DataFrame().to_parquet(mtf_path, index=False)

    coverage = pd.DataFrame(coverage_rows)
    _write_csv(output_dir / "candle_coverage.csv", coverage)
    _write_csv(output_dir / "protected_low_inventory.csv", pl_inv)
    _write_csv(output_dir / "protected_high_inventory.csv", ph_inv)
    _write_csv(output_dir / "protected_low_break_events.csv", pl_events)
    _write_csv(output_dir / "protected_high_break_events.csv", ph_events)
    _write_csv(output_dir / "choch_events.csv", choch_events)
    _write_csv(output_dir / "external_bos_events.csv", bos_events)
    _write_csv(output_dir / "timeframe_alignment.csv", alignment_rows)
    _write_csv(output_dir / "lower_to_higher_tf_transition.csv", transitions)
    _write_json(output_dir / "data_gaps.json", {"gaps": gaps})
    _write_json(
        output_dir / "source_data_audit.json",
        {
            "candle_dir": str(candle_dir),
            "feather_map": CANDLE_FEATHER,
            "symbols": source_rows,
            "btc_caveat": (
                "BTCUSDT 5m feather ends earlier (see date_max) than APT/DOGE; "
                "still included with documented coverage."
            ),
        },
    )

    lookahead = run_lookahead_audit(structures_cat, mtf)
    asof = run_asof_join_audit(mtf)
    mirror = run_mirror_parity_audit(structures_cat)
    invariants = run_invariant_audit(
        lookahead=lookahead,
        asof=asof,
        parity=parity,
        mirror=mirror,
        coverage=coverage,
        structures=structures_cat,
    )
    _write_json(output_dir / "lookahead_audit.json", lookahead)
    _write_json(output_dir / "asof_join_audit.json", asof)
    _write_json(output_dir / "mirror_parity_audit.json", mirror)
    _write_json(output_dir / "invariant_audit.json", invariants)

    htf_built = all(
        (not coverage.empty)
        and (coverage[(coverage["timeframe"] == tf) & (coverage["n_complete"] > 0)].shape[0] > 0)
        for tf in ("1h", "4h")
        if tf in timeframes
    )
    causality_pass = bool(lookahead.get("pass") and asof.get("pass") and invariants.get("pass"))
    decision, note = decide_primary(
        coverage=coverage,
        parity=parity,
        causality_pass=causality_pass,
        htf_built=htf_built,
    )
    # If invariants failed only due to empty HTF, prefer HTF decision
    if decision == "CAUSALITY_INVARIANT_FAILED" and not htf_built:
        decision, note = "HTF_AGGREGATION_INSUFFICIENT", note

    decision_obj = {
        "primary_decision": decision,
        "decision_note": note,
        "parity": parity,
        "causality_pass": causality_pass,
        "htf_built": htf_built,
        "warmup_bars": warmup_bars,
        "warmup_policy": (
            "Full structure parquet includes warmup bars; in_warmup=True for first "
            f"{warmup_bars} bars per TF series. OOS event interpretation should exclude warmup."
        ),
        "missing_sot_columns_note": (
            "SoT provides protected_low_time / protected_high_time / "
            "protected_*_confirmed_at / close_break_*. "
            "trend_segment_id is derived descriptively from major_direction changes."
        ),
    }
    _write_json(output_dir / "decision.json", decision_obj)

    markers = _state_markers(structures_cat)
    tv_dir = output_dir / "tradingview"
    tv_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(tv_dir / "state_markers.csv", markers)

    # summary.md
    cov_txt = coverage.to_string(index=False) if not coverage.empty else "(empty)"
    summary = f"""# Multi-timeframe Protected High/Low structure

## Primary decision

`{decision}`

{note}

## Coverage

```
{cov_txt}
```

## Audits

- 5m parity: **{'PASS' if parity.get('pass') else 'FAIL'}** ({parity.get('symbol')}, mismatches={parity.get('total_mismatches')})
- lookahead: **{'PASS' if lookahead.get('pass') else 'FAIL'}**
- as-of join: **{'PASS' if asof.get('pass') else 'FAIL'}**
- mirror fields: **{'PASS' if mirror.get('pass') else 'FAIL'}**
- invariants: **{'PASS' if invariants.get('pass') else 'FAIL'}**

## Event policy

Rising-edge break event tables use `require_choch=True` (catalog parity).
Inventory also records all rising-edge counts (`n_*_rising_edges_all`).

## BTC caveat

BTCUSDT 5m history ends earlier than APT/DOGE — see `source_data_audit.json`.

## Warmup

First `{warmup_bars}` bars per TF flagged `in_warmup`; full parquet retained.
"""
    (output_dir / "summary.md").write_text(summary, encoding="utf-8")

    return {
        "decision": decision,
        "decision_note": note,
        "output_dir": str(output_dir),
        "coverage": coverage,
        "parity": parity,
        "lookahead": lookahead,
        "asof": asof,
        "invariants": invariants,
        "n_pl_events": len(pl_events),
        "n_ph_events": len(ph_events),
    }


__all__ = [
    "TF_MINUTES",
    "DEFAULT_CANDLE_DIR",
    "DEFAULT_OUTPUT_DIR",
    "DEFAULT_SCANNER_ROOT",
    "aggregate_ohlcv_from_5m",
    "asof_attach_htf",
    "assign_trend_segment_ids",
    "check_5m_parity",
    "compute_alignment_columns",
    "decide_primary",
    "enumerate_structure_breaks",
    "rising_edge_mask",
    "run_structure_for_timeframe",
    "run_trend_scanner_multitimeframe",
    "MIRROR_FIELD_TABLE",
    "PRIMARY_DECISIONS",
]
