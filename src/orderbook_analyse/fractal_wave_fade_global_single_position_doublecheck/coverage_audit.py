"""MySQL coverage / gap / duplicate / timezone inventory."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import text

from orderbook_analyse.fractal_cycle_wave_analysis import EXCHANGE
from orderbook_analyse.fractal_cycle_wave_analysis.load_mysql import load_mysql_ohlcv_tf
from orderbook_analyse.fractal_wave_fade_global_single_position_doublecheck import (
    COVERAGE_TFS,
    COMMON_END,
    COMMON_START,
    ENV_FILE,
    SYMBOLS,
    TF_BAR_MIN,
)
from orderbook_analyse.trend_scanner_mysql_feather_parity.load import load_env_file, _engine


def _ts_utc(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def raw_sql_stats(symbol: str, timeframe: str) -> dict[str, Any]:
    """Direct SQL stats including duplicates and is_closed mix (no drop_duplicates)."""
    load_env_file(ENV_FILE)
    eng = _engine()
    sql = text(
        """
        SELECT
          COUNT(*) AS n,
          COUNT(DISTINCT open_time) AS n_distinct_ot,
          MIN(open_time) AS min_ot,
          MAX(open_time) AS max_ot,
          SUM(CASE WHEN is_closed = 1 THEN 1 ELSE 0 END) AS n_closed,
          SUM(CASE WHEN is_closed = 0 OR is_closed IS NULL THEN 1 ELSE 0 END) AS n_open,
          SUM(CASE WHEN close_time IS NULL THEN 1 ELSE 0 END) AS n_null_close_time
        FROM market_candles
        WHERE exchange = :exchange
          AND symbol = :symbol
          AND timeframe = BINARY :timeframe
        """
    )
    with eng.connect() as conn:
        r = conn.execute(
            sql, {"exchange": EXCHANGE, "symbol": symbol, "timeframe": timeframe}
        ).mappings().one()
    n = int(r["n"] or 0)
    nd = int(r["n_distinct_ot"] or 0)
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "n": n,
        "n_distinct_open_time": nd,
        "duplicate_open_time_count": n - nd,
        "min_open_time": None if r["min_ot"] is None else _ts_utc(r["min_ot"]).isoformat(),
        "max_open_time": None if r["max_ot"] is None else _ts_utc(r["max_ot"]).isoformat(),
        "n_closed": int(r["n_closed"] or 0),
        "n_not_closed": int(r["n_open"] or 0),
        "n_null_close_time": int(r["n_null_close_time"] or 0),
    }


def gap_audit_frame(df: pd.DataFrame, timeframe: str) -> dict[str, Any]:
    """Quantify unexpected gaps vs expected bar step."""
    if df.empty or len(df) < 2:
        return {
            "n_bars": int(len(df)),
            "gap_count": 0,
            "max_gap_bars": 0,
            "total_missing_bars": 0,
            "non_monotonic": 0,
            "not_exact_step_count": 0,
        }
    ts = pd.to_datetime(df["timestamp"], utc=True).sort_values().reset_index(drop=True)
    mono = int((ts.diff().dropna() < pd.Timedelta(0)).sum())
    step = pd.Timedelta(minutes=int(TF_BAR_MIN[timeframe]))
    deltas = ts.diff().dropna()
    # exact step vs larger gaps
    not_exact = int((deltas != step).sum())
    gaps = deltas[deltas > step]
    missing = ((gaps / step) - 1).astype(float)
    return {
        "n_bars": int(len(ts)),
        "gap_count": int(len(gaps)),
        "max_gap_bars": float(missing.max()) if len(missing) else 0.0,
        "total_missing_bars": float(missing.sum()) if len(missing) else 0.0,
        "non_monotonic": mono,
        "not_exact_step_count": not_exact,
        "median_delta_min": float(deltas.dt.total_seconds().median() / 60.0),
    }


def audit_coverage() -> dict[str, Any]:
    rows = []
    frames: dict[tuple[str, str], pd.DataFrame] = {}
    issues: list[str] = []

    for sym in SYMBOLS:
        for tf in COVERAGE_TFS:
            sql_row = raw_sql_stats(sym, tf)
            df = load_mysql_ohlcv_tf(symbol=sym, timeframe=tf, env_file=ENV_FILE)
            frames[(sym, tf)] = df
            gaps = gap_audit_frame(df, tf)
            row = {**sql_row, **{f"gap_{k}": v for k, v in gaps.items()}}
            # timezone check on loaded frame
            if not df.empty:
                ts = df["timestamp"]
                ct = df["close_time"]
                row["timestamp_tz_aware"] = bool(getattr(ts.dt, "tz", None) is not None)
                row["close_time_tz_aware"] = bool(getattr(ct.dt, "tz", None) is not None)
                if not row["timestamp_tz_aware"]:
                    issues.append(f"{sym}/{tf}: timestamp not tz-aware")
                if sql_row["duplicate_open_time_count"] > 0:
                    issues.append(
                        f"{sym}/{tf}: {sql_row['duplicate_open_time_count']} duplicate open_time "
                        "(loader drops duplicates)"
                    )
                if sql_row["n_not_closed"] > 0:
                    # loader filters is_closed=1; note only
                    row["note_open_candles_in_db"] = sql_row["n_not_closed"]
            rows.append(row)

    cs = _ts_utc(COMMON_START)
    ce = _ts_utc(COMMON_END)
    # verify common window fully covered by each TF
    for sym in SYMBOLS:
        for tf in COVERAGE_TFS:
            df = frames[(sym, tf)]
            if df.empty:
                issues.append(f"{sym}/{tf}: empty")
                continue
            t0 = _ts_utc(df["timestamp"].min())
            t1 = _ts_utc(df["timestamp"].max())
            if t0 > cs:
                issues.append(f"{sym}/{tf}: starts after common_start ({t0} > {cs})")
            if t1 < ce:
                issues.append(f"{sym}/{tf}: ends before common_end ({t1} < {ce})")

    # recompute common intersection
    starts = [_ts_utc(frames[(s, tf)]["timestamp"].min()) for s in SYMBOLS for tf in COVERAGE_TFS]
    ends = [_ts_utc(frames[(s, tf)]["timestamp"].max()) for s in SYMBOLS for tf in COVERAGE_TFS]
    recomputed_start = max(starts)
    recomputed_end = min(ends)
    if recomputed_start != cs or recomputed_end != ce:
        # allow equal iso strings
        if recomputed_start.isoformat() != cs.isoformat() or recomputed_end.isoformat() != ce.isoformat():
            issues.append(
                f"common window mismatch: recomputed {recomputed_start}→{recomputed_end} "
                f"vs claimed {cs}→{ce}"
            )

    decision = "DATA_COVERAGE_VALID" if not issues else "DATA_COVERAGE_ISSUES_FOUND"
    return {
        "rows": rows,
        "issues": issues,
        "decision": decision,
        "claimed_common_start": cs.isoformat(),
        "claimed_common_end": ce.isoformat(),
        "recomputed_common_start": recomputed_start.isoformat(),
        "recomputed_common_end": recomputed_end.isoformat(),
        "frames": frames,
    }
