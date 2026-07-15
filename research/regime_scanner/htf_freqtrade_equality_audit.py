"""Read-only equality audit: Freqtrade HTF feathers vs 5m scanner aggregation.

Uses ``research.regime_scanner.timeframes.aggregate_candles`` for the same
bucket semantics as the regime scanner. Does not modify input feather files,
Freqtrade data, state-machine logic, or MySQL.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.timeframes import (
    BARS_PER_AGGREGATE,
    TIMEFRAME_MINUTES,
    aggregate_candles,
    ensure_utc_timestamp,
    timeframe_timedelta,
)

ABS_TOL = 1e-12
REL_TOL = 1e-10
MAX_MISMATCH_DETAIL_ROWS = 500
PRICE_COLS = ("open", "high", "low", "close")
OHLCV_COLS = ("open", "high", "low", "close", "volume")

EXPECTED_INPUT_SHA256 = {
    "5m": "cc0ac7797ddc1562f2fc5097221996fcebb7b166b7b17cb72679cfc47f27e37a",
    "15m": "f55e9a004e77c375aa87f40bc9eb8a69d7d060fa3aed91bb293339df09d3bfbd",
    "30m": "d10844d1036934f79e3983cd1f5af0c5daf159bacead3247b0032f6b7d7eb387",
}

AGGREGATION_SEMANTICS = {
    "bucket_label": "candle open time (UTC)",
    "bucket_start": "floor(5m_open, timeframe_minutes)",
    "candle_open_time": "bucket_open == output timestamp / date",
    "decision_time": "candle_open_time + timeframe",
    "closed": "only buckets with close_time <= decision_time are emitted",
    "label": "left-labeled / open-labeled (Freqtrade and scanner both use open)",
    "timezone": "UTC",
    "incomplete_edge_buckets": "excluded when any required 5m open is missing or misaligned",
    "missing_5m_candles": "bucket skipped entirely (never partial OHLC)",
    "ohlcv_rule": {
        "open": "first 5m open",
        "high": "max 5m high",
        "low": "min 5m low",
        "close": "last 5m close",
        "volume": "sum 5m volume",
    },
    "scanner_function": "research.regime_scanner.timeframes.aggregate_candles",
}


def json_safe(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (pd.Timestamp, np.datetime64)):
        return pd.Timestamp(obj).isoformat()
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        if np.isnan(obj) or np.isinf(obj):
            return None
        return float(obj)
    if isinstance(obj, (np.bool_, bool)):
        return bool(obj)
    if obj is None or isinstance(obj, (str, int)):
        return obj
    return str(obj)


def sha256_file(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def file_fingerprint(path: Path) -> dict[str, Any]:
    path = Path(path)
    df = load_ohlcv_feather(path)
    dates = df["date"]
    return {
        "path": str(path.resolve()),
        "exists": True,
        "size_bytes": int(path.stat().st_size),
        "rows": int(len(df)),
        "start": dates.iloc[0].isoformat() if len(df) else None,
        "end": dates.iloc[-1].isoformat() if len(df) else None,
        "sha256": sha256_file(path),
        "dtypes": {c: str(df[c].dtype) for c in df.columns},
    }


def load_ohlcv_feather(path: Path | str) -> pd.DataFrame:
    path = Path(path)
    df = pd.read_feather(path)
    if "date" not in df.columns and "timestamp" in df.columns:
        df = df.rename(columns={"timestamp": "date"})
    required = {"date", "open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"{path}: missing columns {sorted(missing)}")
    out = df.loc[:, list(required)].copy()
    out["date"] = pd.to_datetime(out["date"], utc=True)
    for col in OHLCV_COLS:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out.reset_index(drop=True)


def verify_expected_hashes(
    *,
    five_minute_file: Path,
    fifteen_minute_file: Path,
    thirty_minute_file: Path,
) -> dict[str, Any]:
    mapping = {
        "5m": Path(five_minute_file),
        "15m": Path(fifteen_minute_file),
        "30m": Path(thirty_minute_file),
    }
    report: dict[str, Any] = {"all_match": True, "files": {}}
    for key, path in mapping.items():
        actual = sha256_file(path)
        expected = EXPECTED_INPUT_SHA256[key]
        match = actual == expected
        report["files"][key] = {
            "path": str(path),
            "expected_sha256": expected,
            "actual_sha256": actual,
            "match": match,
        }
        if not match:
            report["all_match"] = False
    return report


def _series_integrity(dates: pd.Series, timeframe: str) -> dict[str, Any]:
    expected = timeframe_timedelta(timeframe)
    deltas = dates.diff().dropna()
    unexpected = deltas[deltas != expected]
    minutes = TIMEFRAME_MINUTES[timeframe]
    misaligned = [
        str(ts)
        for ts in dates
        if (int(ts.minute) % minutes) != 0 or int(ts.second) != 0 or int(ts.microsecond) != 0
    ]
    return {
        "duplicate_timestamps": int(dates.duplicated().sum()),
        "sorted": bool(dates.is_monotonic_increasing),
        "unexpected_interval_count": int(len(unexpected)),
        "misaligned_bucket_starts": int(len(misaligned)),
        "misaligned_bucket_start_samples": misaligned[:10],
    }


def values_equal_exact(a: float, b: float) -> bool:
    if pd.isna(a) and pd.isna(b):
        return True
    if pd.isna(a) or pd.isna(b):
        return False
    return bool(a == b)


def values_within_tol(a: float, b: float, *, abs_tol: float = ABS_TOL, rel_tol: float = REL_TOL) -> bool:
    if values_equal_exact(a, b):
        return True
    if pd.isna(a) or pd.isna(b):
        return False
    return bool(np.isclose(float(a), float(b), rtol=rel_tol, atol=abs_tol, equal_nan=False))


def abs_diff(a: float, b: float) -> float | None:
    if pd.isna(a) or pd.isna(b):
        return None
    return abs(float(a) - float(b))


def rel_diff(a: float, b: float) -> float | None:
    if pd.isna(a) or pd.isna(b):
        return None
    denom = max(abs(float(a)), abs(float(b)), 1e-30)
    return abs(float(a) - float(b)) / denom


def classify_mismatch_row(
    *,
    present_direct: bool,
    present_agg: bool,
    price_exact: dict[str, bool],
    volume_exact: bool,
    source_5m_count: int | None,
    expected_count: int,
    edge_bucket: bool = False,
) -> list[str]:
    cats: list[str] = []
    if edge_bucket:
        cats.append("edge_bucket")
    if present_direct and not present_agg:
        cats.append("missing_in_aggregated")
    elif present_agg and not present_direct:
        cats.append("missing_in_direct")
    elif present_direct and present_agg:
        price_bad = [c for c, ok in price_exact.items() if not ok]
        if source_5m_count is not None and int(source_5m_count) < int(expected_count):
            cats.append("incomplete_5m_bucket")
        if len(price_bad) >= 2 or (len(price_bad) >= 1 and not volume_exact):
            if len(price_bad) >= 2:
                cats.append("multiple_value_mismatch")
            for col in price_bad:
                cats.append(f"{col}_mismatch")
            if not volume_exact:
                cats.append("volume_mismatch")
        elif len(price_bad) == 1:
            cats.append(f"{price_bad[0]}_mismatch")
        elif not volume_exact:
            cats.append("volume_mismatch")
    # Deterministic unique order.
    order = [
        "missing_in_direct",
        "missing_in_aggregated",
        "open_mismatch",
        "high_mismatch",
        "low_mismatch",
        "close_mismatch",
        "volume_mismatch",
        "multiple_value_mismatch",
        "incomplete_5m_bucket",
        "edge_bucket",
        "timestamp_alignment_mismatch",
    ]
    seen: set[str] = set()
    out: list[str] = []
    for name in order:
        if name in cats and name not in seen:
            out.append(name)
            seen.add(name)
    for name in sorted(cats):
        if name not in seen:
            out.append(name)
            seen.add(name)
    return out


def aggregate_from_canonical_5m(
    candles_5m: pd.DataFrame,
    timeframe: str,
) -> pd.DataFrame:
    """Aggregate complete HTF buckets using scanner ``aggregate_candles``."""
    key = str(timeframe).strip().lower()
    base = candles_5m.rename(columns={"date": "timestamp"}).copy()
    if base.empty:
        return pd.DataFrame(
            columns=[
                "date",
                "decision_time",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "source_5m_count",
                "bucket_complete",
            ]
        )
    last_open = ensure_utc_timestamp(base["timestamp"].iloc[-1])
    # Decision after the last 5m bar is fully closed.
    decision_time = last_open + timeframe_timedelta("5m")
    agg = aggregate_candles(base, key, decision_time)
    if agg.empty:
        return pd.DataFrame(
            columns=[
                "date",
                "decision_time",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "source_5m_count",
                "bucket_complete",
            ]
        )
    duration = timeframe_timedelta(key)
    expected = int(BARS_PER_AGGREGATE[key])
    out = agg.rename(columns={"timestamp": "date"}).copy()
    out["decision_time"] = out["date"] + duration
    out["source_5m_count"] = expected
    out["bucket_complete"] = True
    return out.reset_index(drop=True)


def _column_stats(
    joined: pd.DataFrame,
    col: str,
    *,
    abs_tol: float = ABS_TOL,
    rel_tol: float = REL_TOL,
) -> dict[str, Any]:
    dcol = f"direct_{col}"
    acol = f"aggregated_{col}"
    exact = 0
    within = 0
    outside = 0
    abs_diffs: list[float] = []
    rel_diffs: list[float] = []
    first_bad: str | None = None
    for _, row in joined.iterrows():
        a = row[dcol]
        b = row[acol]
        ad = abs_diff(a, b)
        rd = rel_diff(a, b)
        if ad is not None:
            abs_diffs.append(ad)
        if rd is not None:
            rel_diffs.append(rd)
        if values_equal_exact(a, b):
            exact += 1
            within += 1
        elif values_within_tol(a, b, abs_tol=abs_tol, rel_tol=rel_tol):
            within += 1
            if first_bad is None:
                first_bad = str(row["date"])
        else:
            outside += 1
            if first_bad is None:
                first_bad = str(row["date"])
    abs_arr = np.asarray(abs_diffs, dtype=float) if abs_diffs else np.asarray([0.0])
    rel_arr = np.asarray(rel_diffs, dtype=float) if rel_diffs else np.asarray([0.0])
    return {
        "exact_equal": int(exact),
        "within_tolerance": int(within),
        "outside_tolerance": int(outside),
        "max_abs_diff": float(np.max(abs_arr)) if len(abs_diffs) else 0.0,
        "median_abs_diff": float(np.median(abs_arr)) if len(abs_diffs) else 0.0,
        "p99_abs_diff": float(np.quantile(abs_arr, 0.99)) if len(abs_diffs) else 0.0,
        "max_rel_diff": float(np.max(rel_arr)) if len(rel_diffs) else 0.0,
        "first_mismatch_timestamp": first_bad,
    }


def _monthly_stats(joined: pd.DataFrame, missing_direct: pd.DataFrame, missing_agg: pd.DataFrame) -> list[dict[str, Any]]:
    frames: list[pd.DataFrame] = []
    if not joined.empty:
        j = joined.copy()
        j["year_month"] = j["date"].dt.strftime("%Y-%m")
        frames.append(j)
    months: set[str] = set()
    if frames:
        months |= set(frames[0]["year_month"].unique())
    for df in (missing_direct, missing_agg):
        if not df.empty:
            months |= set(pd.to_datetime(df["date"], utc=True).dt.strftime("%Y-%m"))
    rows_out: list[dict[str, Any]] = []
    for ym in sorted(months):
        j_ym = joined.loc[joined["date"].dt.strftime("%Y-%m") == ym] if not joined.empty else joined
        md = missing_direct.loc[
            pd.to_datetime(missing_direct["date"], utc=True).dt.strftime("%Y-%m") == ym
        ] if not missing_direct.empty else missing_direct
        ma = missing_agg.loc[
            pd.to_datetime(missing_agg["date"], utc=True).dt.strftime("%Y-%m") == ym
        ] if not missing_agg.empty else missing_agg
        exact_ohlc = 0
        price_mm = 0
        vol_mm = 0
        for _, row in j_ym.iterrows():
            price_ok = all(
                values_equal_exact(row[f"direct_{c}"], row[f"aggregated_{c}"]) for c in PRICE_COLS
            )
            vol_ok = values_equal_exact(row["direct_volume"], row["aggregated_volume"])
            if price_ok:
                exact_ohlc += 1
            else:
                price_mm += 1
            if not vol_ok:
                vol_mm += 1
        rows_out.append(
            {
                "year_month": ym,
                "matched_buckets": int(len(j_ym)),
                "exact_ohlc_matches": int(exact_ohlc),
                "price_mismatch_buckets": int(price_mm),
                "volume_mismatch_buckets": int(vol_mm),
                "missing_direct": int(len(md)),
                "missing_aggregated": int(len(ma)),
            }
        )
    return rows_out


@dataclass(frozen=True)
class TimeframeCompareResult:
    timeframe: str
    summary: dict[str, Any]
    mismatches: pd.DataFrame


def compare_timeframe(
    *,
    timeframe: str,
    candles_5m: pd.DataFrame,
    direct: pd.DataFrame,
    abs_tol: float = ABS_TOL,
    rel_tol: float = REL_TOL,
    max_mismatch_rows: int = MAX_MISMATCH_DETAIL_ROWS,
) -> TimeframeCompareResult:
    key = str(timeframe).strip().lower()
    expected_count = int(BARS_PER_AGGREGATE[key])
    duration = timeframe_timedelta(key)

    agg = aggregate_from_canonical_5m(candles_5m, key)
    direct = direct.copy()
    direct["date"] = pd.to_datetime(direct["date"], utc=True)

    five_start = ensure_utc_timestamp(candles_5m["date"].iloc[0])
    five_last_open = ensure_utc_timestamp(candles_5m["date"].iloc[-1])
    five_last_close = five_last_open + timeframe_timedelta("5m")
    # Last complete HTF open whose close_time <= five_last_close.
    last_complete_open = None
    if not agg.empty:
        last_complete_open = ensure_utc_timestamp(agg["date"].iloc[-1])

    direct_start = ensure_utc_timestamp(direct["date"].iloc[0]) if len(direct) else five_start
    direct_end = ensure_utc_timestamp(direct["date"].iloc[-1]) if len(direct) else five_last_open

    # Common open-timestamp window for primary equality.
    common_start = max(five_start, direct_start)
    common_end_open = min(
        last_complete_open if last_complete_open is not None else five_last_open,
        direct_end,
    )

    agg_common = agg.loc[(agg["date"] >= common_start) & (agg["date"] <= common_end_open)].copy()
    direct_common = direct.loc[
        (direct["date"] >= common_start) & (direct["date"] <= common_end_open)
    ].copy()

    direct_before = direct.loc[direct["date"] < five_start].copy()
    # After last complete aggregatable open from 5m (or after 5m series end).
    cutoff_after = (
        last_complete_open + duration
        if last_complete_open is not None
        else five_last_close
    )
    # Direct-only after 5m end: opens strictly after last complete agg open.
    if last_complete_open is not None:
        direct_after = direct.loc[direct["date"] > last_complete_open].copy()
    else:
        direct_after = direct.copy()

    integrity_direct = _series_integrity(direct["date"], key)
    integrity_agg = _series_integrity(agg["date"], key) if not agg.empty else {
        "duplicate_timestamps": 0,
        "sorted": True,
        "unexpected_interval_count": 0,
        "misaligned_bucket_starts": 0,
        "misaligned_bucket_start_samples": [],
    }
    integrity_direct_common = _series_integrity(direct_common["date"], key) if len(direct_common) else {
        "duplicate_timestamps": 0,
        "sorted": True,
        "unexpected_interval_count": 0,
        "misaligned_bucket_starts": 0,
        "misaligned_bucket_start_samples": [],
    }

    agg_idx = agg_common.set_index("date", drop=False)
    dir_idx = direct_common.set_index("date", drop=False)
    shared = sorted(set(agg_idx.index) & set(dir_idx.index))
    only_agg = sorted(set(agg_idx.index) - set(dir_idx.index))
    only_dir = sorted(set(dir_idx.index) - set(agg_idx.index))

    joined_rows: list[dict[str, Any]] = []
    mismatch_rows: list[dict[str, Any]] = []
    for ts in shared:
        a = agg_idx.loc[ts]
        d = dir_idx.loc[ts]
        # Handle rare duplicate index edge by taking first.
        if isinstance(a, pd.DataFrame):
            a = a.iloc[0]
        if isinstance(d, pd.DataFrame):
            d = d.iloc[0]
        row: dict[str, Any] = {
            "date": ensure_utc_timestamp(ts),
            "direct_open": float(d["open"]),
            "aggregated_open": float(a["open"]),
            "direct_high": float(d["high"]),
            "aggregated_high": float(a["high"]),
            "direct_low": float(d["low"]),
            "aggregated_low": float(a["low"]),
            "direct_close": float(d["close"]),
            "aggregated_close": float(a["close"]),
            "direct_volume": float(d["volume"]),
            "aggregated_volume": float(a["volume"]),
            "source_5m_count": int(a["source_5m_count"]),
        }
        joined_rows.append(row)
        price_exact = {c: values_equal_exact(row[f"direct_{c}"], row[f"aggregated_{c}"]) for c in PRICE_COLS}
        volume_exact = values_equal_exact(row["direct_volume"], row["aggregated_volume"])
        if not all(price_exact.values()) or not volume_exact:
            cats = classify_mismatch_row(
                present_direct=True,
                present_agg=True,
                price_exact=price_exact,
                volume_exact=volume_exact,
                source_5m_count=int(a["source_5m_count"]),
                expected_count=expected_count,
            )
            mismatch_rows.append(_mismatch_export_row(key, row, cats))

    for ts in only_dir:
        d = dir_idx.loc[ts]
        if isinstance(d, pd.DataFrame):
            d = d.iloc[0]
        row = {
            "date": ensure_utc_timestamp(ts),
            "direct_open": float(d["open"]),
            "aggregated_open": np.nan,
            "direct_high": float(d["high"]),
            "aggregated_high": np.nan,
            "direct_low": float(d["low"]),
            "aggregated_low": np.nan,
            "direct_close": float(d["close"]),
            "aggregated_close": np.nan,
            "direct_volume": float(d["volume"]),
            "aggregated_volume": np.nan,
            "source_5m_count": None,
        }
        cats = classify_mismatch_row(
            present_direct=True,
            present_agg=False,
            price_exact={c: False for c in PRICE_COLS},
            volume_exact=False,
            source_5m_count=None,
            expected_count=expected_count,
        )
        if integrity_direct_common["misaligned_bucket_starts"] and (
            int(ensure_utc_timestamp(ts).minute) % TIMEFRAME_MINUTES[key] != 0
        ):
            cats = list(dict.fromkeys(cats + ["timestamp_alignment_mismatch"]))
        mismatch_rows.append(_mismatch_export_row(key, row, cats))

    for ts in only_agg:
        a = agg_idx.loc[ts]
        if isinstance(a, pd.DataFrame):
            a = a.iloc[0]
        row = {
            "date": ensure_utc_timestamp(ts),
            "direct_open": np.nan,
            "aggregated_open": float(a["open"]),
            "direct_high": np.nan,
            "aggregated_high": float(a["high"]),
            "direct_low": np.nan,
            "aggregated_low": float(a["low"]),
            "direct_close": np.nan,
            "aggregated_close": float(a["close"]),
            "direct_volume": np.nan,
            "aggregated_volume": float(a["volume"]),
            "source_5m_count": int(a["source_5m_count"]),
        }
        cats = classify_mismatch_row(
            present_direct=False,
            present_agg=True,
            price_exact={c: False for c in PRICE_COLS},
            volume_exact=False,
            source_5m_count=int(a["source_5m_count"]),
            expected_count=expected_count,
        )
        mismatch_rows.append(_mismatch_export_row(key, row, cats))

    joined = pd.DataFrame(joined_rows)
    if not joined.empty:
        joined = joined.sort_values("date").reset_index(drop=True)

    missing_direct_df = pd.DataFrame(
        [{"date": ensure_utc_timestamp(ts)} for ts in only_agg]
    )
    missing_agg_df = pd.DataFrame(
        [{"date": ensure_utc_timestamp(ts)} for ts in only_dir]
    )

    col_stats = {}
    if not joined.empty:
        for col in OHLCV_COLS:
            col_stats[col] = _column_stats(joined, col, abs_tol=abs_tol, rel_tol=rel_tol)
    else:
        for col in OHLCV_COLS:
            col_stats[col] = {
                "exact_equal": 0,
                "within_tolerance": 0,
                "outside_tolerance": 0,
                "max_abs_diff": 0.0,
                "median_abs_diff": 0.0,
                "p99_abs_diff": 0.0,
                "max_rel_diff": 0.0,
                "first_mismatch_timestamp": None,
            }

    n_shared = len(shared)
    exact_ohlc = 0
    exact_volume = 0
    exact_full = 0
    within_ohlc = 0
    if not joined.empty:
        for _, row in joined.iterrows():
            pe = all(values_equal_exact(row[f"direct_{c}"], row[f"aggregated_{c}"]) for c in PRICE_COLS)
            pw = all(
                values_within_tol(row[f"direct_{c}"], row[f"aggregated_{c}"], abs_tol=abs_tol, rel_tol=rel_tol)
                for c in PRICE_COLS
            )
            ve = values_equal_exact(row["direct_volume"], row["aggregated_volume"])
            if pe:
                exact_ohlc += 1
            if pw:
                within_ohlc += 1
            if ve:
                exact_volume += 1
            if pe and ve:
                exact_full += 1

    mismatches_df = pd.DataFrame(mismatch_rows)
    if not mismatches_df.empty:
        mismatches_df = mismatches_df.sort_values(["date", "mismatch_categories"]).reset_index(drop=True)

    truncated = False
    detail_df = mismatches_df
    if len(mismatches_df) > max_mismatch_rows:
        truncated = True
        detail_df = mismatches_df.iloc[:max_mismatch_rows].copy()

    first_mismatch = None
    if not mismatches_df.empty:
        first = mismatches_df.iloc[0]
        first_mismatch = {
            "date": str(first["date"]),
            "mismatch_categories": first["mismatch_categories"],
            "open_abs_diff": first.get("open_abs_diff"),
            "high_abs_diff": first.get("high_abs_diff"),
            "low_abs_diff": first.get("low_abs_diff"),
            "close_abs_diff": first.get("close_abs_diff"),
            "volume_abs_diff": first.get("volume_abs_diff"),
        }

    cause = _assess_causes(
        n_shared=n_shared,
        exact_full=exact_full,
        exact_ohlc=exact_ohlc,
        exact_volume=exact_volume,
        only_dir=len(only_dir),
        only_agg=len(only_agg),
        col_stats=col_stats,
        n_direct_after=len(direct_after),
        n_direct_before=len(direct_before),
        integrity_direct_common=integrity_direct_common,
    )

    summary = {
        "timeframe": key,
        "aggregation_semantics": AGGREGATION_SEMANTICS,
        "common_window": {
            "start": common_start.isoformat(),
            "end_open": common_end_open.isoformat() if common_end_open is not None else None,
            "note": "primary comparison uses complete aggregated opens within [start, end_open]",
        },
        "five_minute_span": {
            "start": five_start.isoformat(),
            "last_open": five_last_open.isoformat(),
            "last_close": five_last_close.isoformat(),
            "last_complete_htf_open": last_complete_open.isoformat() if last_complete_open else None,
        },
        "counts": {
            "aggregated_complete_total": int(len(agg)),
            "aggregated_in_common_window": int(len(agg_common)),
            "direct_total": int(len(direct)),
            "direct_in_common_window": int(len(direct_common)),
            "shared_timestamps": int(n_shared),
            "missing_in_direct": int(len(only_agg)),
            "missing_in_aggregated": int(len(only_dir)),
            "direct_only_before_5m_start": int(len(direct_before)),
            "direct_only_after_5m_end": int(len(direct_after)),
            "mismatch_rows_total": int(len(mismatches_df)),
            "mismatch_detail_rows_written": int(len(detail_df)),
            "mismatch_detail_truncated": bool(truncated),
        },
        "integrity_direct_full": integrity_direct,
        "integrity_aggregated_full": integrity_agg,
        "integrity_direct_common": integrity_direct_common,
        "match_rates": {
            "exact_ohlc_rate": (exact_ohlc / n_shared) if n_shared else None,
            "within_tol_ohlc_rate": (within_ohlc / n_shared) if n_shared else None,
            "exact_volume_rate": (exact_volume / n_shared) if n_shared else None,
            "exact_full_ohlcv_rate": (exact_full / n_shared) if n_shared else None,
            "exact_ohlc_matches": int(exact_ohlc),
            "within_tol_ohlc_matches": int(within_ohlc),
            "exact_volume_matches": int(exact_volume),
            "exact_full_ohlcv_matches": int(exact_full),
        },
        "column_stats": col_stats,
        "first_mismatch": first_mismatch,
        "monthly": _monthly_stats(joined, missing_direct_df, missing_agg_df),
        "cause_assessment": cause,
        "tolerances": {"abs_tol": abs_tol, "rel_tol": rel_tol},
        "direct_only_after_5m_end_start": (
            ensure_utc_timestamp(direct_after["date"].iloc[0]).isoformat()
            if len(direct_after)
            else None
        ),
        "direct_only_after_5m_end_end": (
            ensure_utc_timestamp(direct_after["date"].iloc[-1]).isoformat()
            if len(direct_after)
            else None
        ),
        "cutoff_after_note": cutoff_after.isoformat(),
    }
    # attach detail frame for writer
    summary["_detail_mismatch_df_rows"] = int(len(detail_df))
    return TimeframeCompareResult(timeframe=key, summary=summary, mismatches=detail_df)


def _mismatch_export_row(timeframe: str, row: dict[str, Any], cats: list[str]) -> dict[str, Any]:
    def _d(a: Any, b: Any) -> float | None:
        return abs_diff(a, b)

    return {
        "timeframe": timeframe,
        "date": ensure_utc_timestamp(row["date"]).isoformat(),
        "direct_open": row["direct_open"],
        "aggregated_open": row["aggregated_open"],
        "direct_high": row["direct_high"],
        "aggregated_high": row["aggregated_high"],
        "direct_low": row["direct_low"],
        "aggregated_low": row["aggregated_low"],
        "direct_close": row["direct_close"],
        "aggregated_close": row["aggregated_close"],
        "direct_volume": row["direct_volume"],
        "aggregated_volume": row["aggregated_volume"],
        "open_abs_diff": _d(row["direct_open"], row["aggregated_open"]),
        "high_abs_diff": _d(row["direct_high"], row["aggregated_high"]),
        "low_abs_diff": _d(row["direct_low"], row["aggregated_low"]),
        "close_abs_diff": _d(row["direct_close"], row["aggregated_close"]),
        "volume_abs_diff": _d(row["direct_volume"], row["aggregated_volume"]),
        "source_5m_count": row.get("source_5m_count"),
        "mismatch_categories": "|".join(cats),
    }


def _assess_causes(
    *,
    n_shared: int,
    exact_full: int,
    exact_ohlc: int,
    exact_volume: int,
    only_dir: int,
    only_agg: int,
    col_stats: dict[str, Any],
    n_direct_after: int,
    n_direct_before: int,
    integrity_direct_common: dict[str, Any],
) -> dict[str, Any]:
    notes: list[dict[str, str]] = []
    if n_shared and exact_full == n_shared and only_dir == 0 and only_agg == 0:
        notes.append(
            {
                "status": "nachgewiesen",
                "claim": "Im gemeinsamen Fenster sind alle Shared-Buckets OHLCV exakt identisch.",
            }
        )
    if n_direct_after:
        notes.append(
            {
                "status": "nachgewiesen",
                "claim": (
                    f"{n_direct_after} direkte HTF-Candles liegen nach dem letzten "
                    "vollständig aus 5m aggregierbaren Bucket (längere HTF-Download-Serie)."
                ),
            }
        )
    if n_direct_before:
        notes.append(
            {
                "status": "nachgewiesen",
                "claim": f"{n_direct_before} direkte HTF-Candles liegen vor dem 5m-Start.",
            }
        )
    if only_dir or only_agg:
        notes.append(
            {
                "status": "nachgewiesen" if (only_dir or only_agg) else "unklar",
                "claim": f"Timestamp-Set-Differenz common window: missing_direct={only_agg}, missing_aggregated={only_dir}.",
            }
        )
    price_outside = sum(int(col_stats[c]["outside_tolerance"]) for c in PRICE_COLS)
    vol_outside = int(col_stats["volume"]["outside_tolerance"])
    if price_outside == 0 and vol_outside and exact_ohlc == n_shared:
        notes.append(
            {
                "status": "wahrscheinlich",
                "claim": "Nur Volumen weicht ab; OHLC exakt — mögliche Exchange-HTF- vs. 5m-Summen-Differenz oder Float.",
            }
        )
    if price_outside:
        notes.append(
            {
                "status": "unklar",
                "claim": (
                    f"OHLC außerhalb Toleranz in {price_outside} Spalten-Treffern "
                    "(mögliche Exchange-native HTF vs. 5m-Aggregation oder Datenrevision)."
                ),
            }
        )
    float_only = False
    if n_shared and exact_ohlc < n_shared:
        within = min(int(col_stats[c]["within_tolerance"]) for c in PRICE_COLS)
        outside = max(int(col_stats[c]["outside_tolerance"]) for c in PRICE_COLS)
        if within == n_shared and outside == 0 and exact_ohlc < n_shared:
            float_only = True
            notes.append(
                {
                    "status": "wahrscheinlich",
                    "claim": "Preisdifferenzen nur innerhalb Float-Toleranz (1e-12/1e-10).",
                }
            )
    if integrity_direct_common.get("misaligned_bucket_starts"):
        notes.append(
            {
                "status": "nachgewiesen",
                "claim": "Direkte Serie enthält falsch ausgerichtete Bucket-Starts im Common-Window.",
            }
        )
    return {
        "notes": notes,
        "float_representation_only": float_only,
        "shared_exact_full": int(exact_full),
        "shared_exact_ohlc": int(exact_ohlc),
        "shared_exact_volume": int(exact_volume),
    }


def decide_canonical_source(results: dict[str, TimeframeCompareResult]) -> dict[str, Any]:
    """Return recommendation code and rationale."""
    tf_codes: dict[str, str] = {}
    any_extra_edge = False
    for tf, res in results.items():
        s = res.summary
        rates = s["match_rates"]
        counts = s["counts"]
        n = counts["shared_timestamps"]
        only = counts["missing_in_direct"] + counts["missing_in_aggregated"]
        exact_full = rates["exact_full_ohlcv_matches"]
        exact_ohlc = rates["exact_ohlc_matches"]
        outside_price = sum(int(s["column_stats"][c]["outside_tolerance"]) for c in PRICE_COLS)
        outside_vol = int(s["column_stats"]["volume"]["outside_tolerance"])
        float_only = bool(s["cause_assessment"].get("float_representation_only"))
        edge_tf = bool(
            counts["direct_only_after_5m_end"] or counts["direct_only_before_5m_start"]
        )
        any_extra_edge = any_extra_edge or edge_tf

        if n == 0:
            code = "INVESTIGATE"
        elif only == 0 and exact_full == n:
            code = "USE_5M_AGGREGATION" if edge_tf else "EQUIVALENT"
        elif only == 0 and outside_price == 0 and (exact_ohlc == n or float_only) and outside_vol == 0:
            code = "USE_5M_AGGREGATION" if edge_tf else "EQUIVALENT"
        elif only == 0 and outside_price == 0 and exact_ohlc == n and outside_vol > 0:
            code = "INVESTIGATE" if outside_vol > max(5, int(n * 0.001)) else "USE_5M_AGGREGATION"
        elif outside_price > 0 or only > max(5, int(n * 0.01)):
            code = "INVESTIGATE"
        else:
            code = "USE_5M_AGGREGATION"
        tf_codes[tf] = code

    codes = set(tf_codes.values())
    if codes == {"EQUIVALENT"}:
        overall = "EQUIVALENT"
        rationale = (
            "In the common window, timestamps and OHLCV match exactly "
            "(or only via harmless float representation)."
        )
    elif codes == {"USE_5M_AGGREGATION"} or codes == {"EQUIVALENT", "USE_5M_AGGREGATION"}:
        overall = "USE_5M_AGGREGATION"
        if any_extra_edge:
            rationale = (
                "Direct Freqtrade HTF agree with scanner 5m aggregation on the overlapping "
                "complete buckets; direct files also contain extra edge bars after the "
                "canonical 5m end (and/or minor residuals). Prefer deterministic HTF from "
                "the single canonical 5m source for research and future MySQL fill."
            )
        else:
            rationale = (
                "Direct HTF largely agree with 5m aggregation; reproducible research "
                "consistency from one 5m source is preferred."
            )
    elif "INVESTIGATE" in codes:
        overall = "INVESTIGATE"
        rationale = "Relevant mismatches, missing buckets, or unexplained differences remain."
    elif "USE_DIRECT_FREQTRADE_HTF" in codes:
        overall = "USE_DIRECT_FREQTRADE_HTF"
        rationale = "Direct HTF appears systematically more correct than 5m aggregation."
    else:
        overall = "INVESTIGATE"
        rationale = "Mixed timeframe outcomes."

    return {
        "decision": overall,
        "per_timeframe": tf_codes,
        "rationale": rationale,
        "mysql_preparation": {
            "market_candles_5m": "canonical 5m Bybit feather (already used)",
            "market_candles_15m": "deterministically aggregated from 5m (scanner semantics)",
            "market_candles_30m": "deterministically aggregated from 5m (scanner semantics)",
            "store_direct_htf_sha256_as_validation_metadata": True,
            "note": "Do not promote staging HTF into canonical folder until comparison accepted.",
        },
    }


def deterministic_hash(summary: dict[str, Any]) -> str:
    cleaned = {k: v for k, v in summary.items() if not str(k).startswith("_")}
    blob = json.dumps(json_safe(cleaned), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def write_readme(path: Path, summary: dict[str, Any]) -> None:
    dec = summary["decision"]
    lines = [
        "# HTF Freqtrade Equality Audit",
        "",
        f"- Decision: `{dec['decision']}`",
        f"- Deterministic hash: `{summary['deterministic_hash']}`",
        "",
        "## Aggregation semantics",
        "",
        "```text",
        json.dumps(AGGREGATION_SEMANTICS, indent=2),
        "```",
        "",
        "## Input fingerprints",
        "",
        "```json",
        json.dumps(json_safe(summary["inputs"]), indent=2),
        "```",
        "",
    ]
    for tf in ("15m", "30m"):
        s = summary["timeframes"][tf]
        lines.extend(
            [
                f"## {tf}",
                "",
                f"- Common window: `{s['common_window']['start']}` → `{s['common_window']['end_open']}`",
                f"- Shared buckets: {s['counts']['shared_timestamps']}",
                f"- Missing in direct: {s['counts']['missing_in_direct']}",
                f"- Missing in aggregated: {s['counts']['missing_in_aggregated']}",
                f"- Direct-only after 5m end: {s['counts']['direct_only_after_5m_end']}",
                f"- Exact OHLC rate: {s['match_rates']['exact_ohlc_rate']}",
                f"- Exact volume rate: {s['match_rates']['exact_volume_rate']}",
                f"- Exact full OHLCV rate: {s['match_rates']['exact_full_ohlcv_rate']}",
                f"- First mismatch: {s['first_mismatch']}",
                "",
            ]
        )
    lines.extend(
        [
            "## Recommendation rationale",
            "",
            dec["rationale"],
            "",
            "## MySQL preparation (conceptual only)",
            "",
            "```json",
            json.dumps(dec["mysql_preparation"], indent=2),
            "```",
            "",
            "Direct HTF files remain in staging only.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def run_audit(
    *,
    five_minute_file: Path | str,
    fifteen_minute_file: Path | str,
    thirty_minute_file: Path | str,
    output_dir: Path | str,
    abs_tol: float = ABS_TOL,
    rel_tol: float = REL_TOL,
    max_mismatch_rows: int = MAX_MISMATCH_DETAIL_ROWS,
    require_expected_hashes: bool = True,
) -> dict[str, Any]:
    five_minute_file = Path(five_minute_file)
    fifteen_minute_file = Path(fifteen_minute_file)
    thirty_minute_file = Path(thirty_minute_file)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    hash_check = verify_expected_hashes(
        five_minute_file=five_minute_file,
        fifteen_minute_file=fifteen_minute_file,
        thirty_minute_file=thirty_minute_file,
    )
    if require_expected_hashes and not hash_check["all_match"]:
        raise SystemExit(
            "Input SHA256 mismatch vs documented hashes; refusing silent continue:\n"
            + json.dumps(hash_check, indent=2)
        )

    # Capture size before load (read-only).
    inputs = {
        "5m": file_fingerprint(five_minute_file),
        "15m": file_fingerprint(fifteen_minute_file),
        "30m": file_fingerprint(thirty_minute_file),
        "hash_check": hash_check,
    }

    candles_5m = load_ohlcv_feather(five_minute_file)
    d15 = load_ohlcv_feather(fifteen_minute_file)
    d30 = load_ohlcv_feather(thirty_minute_file)

    # Post-load SHA must still match (files unchanged).
    after = {
        "5m": sha256_file(five_minute_file),
        "15m": sha256_file(fifteen_minute_file),
        "30m": sha256_file(thirty_minute_file),
    }
    inputs["sha256_after_read"] = after
    inputs["inputs_unchanged_after_read"] = all(
        after[k] == inputs[k]["sha256"] for k in ("5m", "15m", "30m")
    )

    results = {
        "15m": compare_timeframe(
            timeframe="15m",
            candles_5m=candles_5m,
            direct=d15,
            abs_tol=abs_tol,
            rel_tol=rel_tol,
            max_mismatch_rows=max_mismatch_rows,
        ),
        "30m": compare_timeframe(
            timeframe="30m",
            candles_5m=candles_5m,
            direct=d30,
            abs_tol=abs_tol,
            rel_tol=rel_tol,
            max_mismatch_rows=max_mismatch_rows,
        ),
    }
    decision = decide_canonical_source(results)

    summary: dict[str, Any] = {
        "audit": "htf_freqtrade_equality_audit",
        "aggregation_semantics": AGGREGATION_SEMANTICS,
        "inputs": inputs,
        "timeframes": {tf: res.summary for tf, res in results.items()},
        "decision": decision,
    }
    # Strip non-serializable helper keys if any.
    for tf in summary["timeframes"]:
        summary["timeframes"][tf] = {
            k: v for k, v in summary["timeframes"][tf].items() if not str(k).startswith("_")
        }
    summary["deterministic_hash"] = deterministic_hash(summary)

    (output_dir / "summary.json").write_text(
        json.dumps(json_safe(summary), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_readme(output_dir / "README_results.md", summary)

    for tf, res in results.items():
        out_csv = output_dir / f"mismatches_{tf}.csv"
        if res.mismatches.empty:
            # Always write header for determinism.
            cols = [
                "timeframe",
                "date",
                "direct_open",
                "aggregated_open",
                "direct_high",
                "aggregated_high",
                "direct_low",
                "aggregated_low",
                "direct_close",
                "aggregated_close",
                "direct_volume",
                "aggregated_volume",
                "open_abs_diff",
                "high_abs_diff",
                "low_abs_diff",
                "close_abs_diff",
                "volume_abs_diff",
                "source_5m_count",
                "mismatch_categories",
            ]
            pd.DataFrame(columns=cols).to_csv(out_csv, index=False)
        else:
            res.mismatches.to_csv(out_csv, index=False)

    # Final SHA confirmation of inputs.
    summary["inputs"]["sha256_after_write"] = {
        "5m": sha256_file(five_minute_file),
        "15m": sha256_file(fifteen_minute_file),
        "30m": sha256_file(thirty_minute_file),
    }
    # Rewrite summary with final after_write hashes but keep deterministic_hash stable:
    # hash excludes after_write by recomputing from pre-write body. We already hashed.
    # Store after_write separately without changing deterministic_hash.
    final = dict(summary)
    final["inputs"] = dict(summary["inputs"])
    final["inputs"]["sha256_after_write"] = {
        "5m": sha256_file(five_minute_file),
        "15m": sha256_file(fifteen_minute_file),
        "30m": sha256_file(thirty_minute_file),
    }
    final["inputs"]["inputs_unchanged_after_write"] = all(
        final["inputs"]["sha256_after_write"][k] == inputs[k]["sha256"] for k in ("5m", "15m", "30m")
    )
    # Keep deterministic_hash based on content without after_write to allow dual-run compare.
    body_for_hash = {
        k: v
        for k, v in final.items()
        if k != "deterministic_hash"
    }
    # Remove after_write from hashed body.
    body_for_hash = json_safe(body_for_hash)
    body_for_hash["inputs"] = {
        k: v
        for k, v in body_for_hash["inputs"].items()
        if k not in {"sha256_after_write", "inputs_unchanged_after_write"}
    }
    final["deterministic_hash"] = hashlib.sha256(
        json.dumps(body_for_hash, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    (output_dir / "summary.json").write_text(
        json.dumps(json_safe(final), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_readme(output_dir / "README_results.md", final)
    return final


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--five-minute-file",
        type=Path,
        default=Path(
            "/home/telgenbuescher/projects/Signal_Generator_Ralf/data/bybit/futures/"
            "APT_USDT_USDT-5m-futures.feather"
        ),
    )
    p.add_argument(
        "--fifteen-minute-file",
        type=Path,
        default=Path(
            "/home/telgenbuescher/projects/Signal_Generator_Ralf/data_apt_htf_staging/futures/"
            "APT_USDT_USDT-15m-futures.feather"
        ),
    )
    p.add_argument(
        "--thirty-minute-file",
        type=Path,
        default=Path(
            "/home/telgenbuescher/projects/Signal_Generator_Ralf/data_apt_htf_staging/futures/"
            "APT_USDT_USDT-30m-futures.feather"
        ),
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path("research/regime_scanner/results_htf_freqtrade_equality_audit"),
    )
    p.add_argument("--abs-tol", type=float, default=ABS_TOL)
    p.add_argument("--rel-tol", type=float, default=REL_TOL)
    p.add_argument("--max-mismatch-rows", type=int, default=MAX_MISMATCH_DETAIL_ROWS)
    p.add_argument(
        "--skip-expected-hash-check",
        action="store_true",
        help="Allow running when input SHA256 differs from documented inventory hashes.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    summary = run_audit(
        five_minute_file=args.five_minute_file,
        fifteen_minute_file=args.fifteen_minute_file,
        thirty_minute_file=args.thirty_minute_file,
        output_dir=args.output_dir,
        abs_tol=args.abs_tol,
        rel_tol=args.rel_tol,
        max_mismatch_rows=args.max_mismatch_rows,
        require_expected_hashes=not args.skip_expected_hash_check,
    )
    print(
        json.dumps(
            {
                "deterministic_hash": summary["deterministic_hash"],
                "decision": summary["decision"]["decision"],
                "per_timeframe": summary["decision"]["per_timeframe"],
                "output_dir": str(args.output_dir),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
