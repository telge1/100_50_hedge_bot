"""Complete 5m footprint candles from Dashboard second buckets + parity."""

from __future__ import annotations

import bisect
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from . import CANDLE_5M_S
from .footprint import aggregate_avr_persistence, footprint_window_from_series
from .oi_liq import liq_window_from_states, oi_window_from_states


def build_complete_5m_candles(
    *,
    symbol: str,
    series: Any,
    avr_1s: pd.DataFrame | None,
    state_by_ts: dict[int, Any],
    feature_start_unix: int,
    feature_end_unix: int,
) -> pd.DataFrame:
    """One row per complete 5m candle with candle_end in (feature_start, feature_end].

    For [10:00, 11:00) → candle starts 10:00..10:55 (12 candles), available_at=candle_end.
    """
    symbol = symbol.upper()
    rows: list[dict[str, Any]] = []
    # first candle start aligned to feature_start floored to 5m
    cs = feature_start_unix - (feature_start_unix % CANDLE_5M_S)
    if cs < feature_start_unix:
        cs += CANDLE_5M_S
    # include candle that starts at feature_start
    cs = feature_start_unix - (feature_start_unix % CANDLE_5M_S)
    while cs + CANDLE_5M_S <= feature_end_unix:
        ce = cs + CANDLE_5M_S
        row = _candle_row(
            symbol=symbol,
            series=series,
            avr_1s=avr_1s,
            state_by_ts=state_by_ts,
            candle_start=cs,
            candle_end=ce,
            is_complete=True,
            is_partial=False,
        )
        rows.append(row)
        cs = ce
    return pd.DataFrame(rows)


def build_candle_at(
    *,
    symbol: str,
    series: Any,
    avr_1s: pd.DataFrame | None,
    state_by_ts: dict[int, Any],
    candle_start: int,
    candle_end: int,
    is_complete: bool,
    is_partial: bool,
) -> dict[str, Any]:
    return _candle_row(
        symbol=symbol,
        series=series,
        avr_1s=avr_1s,
        state_by_ts=state_by_ts,
        candle_start=candle_start,
        candle_end=candle_end,
        is_complete=is_complete,
        is_partial=is_partial,
    )


def _candle_row(
    *,
    symbol: str,
    series: Any,
    avr_1s: pd.DataFrame | None,
    state_by_ts: dict[int, Any],
    candle_start: int,
    candle_end: int,
    is_complete: bool,
    is_partial: bool,
) -> dict[str, Any]:
    w = candle_end - candle_start
    fp = footprint_window_from_series(series, available_at=candle_end, window_s=w, prefix="fp")
    avr = aggregate_avr_persistence(
        avr_1s, t_unix=float(candle_end), window_s=w, prefix="avr"
    )
    oi = oi_window_from_states(
        state_by_ts, t_unix=float(candle_end), window_s=w, prefix="candle"
    )
    liq = liq_window_from_states(
        state_by_ts, t_unix=float(candle_end), window_s=w, prefix="candle"
    )
    # Flatten oi/liq without candle_ prefix duplication for table contract
    oi_flat = {
        "oi_start": oi.get("candle_oi_start"),
        "oi_end": oi.get("candle_oi_end"),
        "oi_delta_abs": oi.get("candle_oi_delta_abs"),
        "oi_delta_pct": oi.get("candle_oi_delta_pct"),
        "oi_sample_count": oi.get("candle_oi_sample_count"),
        "oi_direction": oi.get("candle_oi_direction"),
        "oi_coverage_status": oi.get("candle_oi_coverage_status"),
    }
    liq_flat = {
        "liq_long_notional": liq.get("candle_liq_long_notional"),
        "liq_short_notional": liq.get("candle_liq_short_notional"),
        "liq_total_notional": liq.get("candle_liq_total_notional"),
        "liq_event_count": liq.get("candle_liq_event_count"),
        "liq_coverage_status": liq.get("candle_liq_coverage_status"),
    }
    return {
        "schema_version": "footprint_5m_context_v1",
        "symbol": symbol,
        "candle_start": datetime.fromtimestamp(candle_start, tz=timezone.utc),
        "candle_end": datetime.fromtimestamp(candle_end, tz=timezone.utc),
        "candle_start_unix": candle_start,
        "candle_end_unix": candle_end,
        "available_at": datetime.fromtimestamp(candle_end, tz=timezone.utc),
        "available_at_unix": candle_end,
        "is_complete": is_complete,
        "is_partial": is_partial,
        "elapsed_seconds": w,
        "expected_seconds": CANDLE_5M_S,
        "completion_frac": w / float(CANDLE_5M_S),
        **fp,
        **{k: v for k, v in avr.items() if k.startswith("avr_")},
        **oi_flat,
        **liq_flat,
    }


def parity_5m(
    engine_df: pd.DataFrame,
    reference_df: pd.DataFrame,
    *,
    float_tol: float = 1e-6,
) -> dict[str, Any]:
    """Compare engine vs independent second-pass 5m aggregation."""
    cols_num = [
        "fp_open",
        "fp_high",
        "fp_low",
        "fp_close",
        "fp_buy_notional",
        "fp_sell_notional",
        "fp_delta_notional",
        "fp_trade_count",
    ]
    e = engine_df.sort_values("candle_start_unix").reset_index(drop=True)
    r = reference_df.sort_values("candle_start_unix").reset_index(drop=True)
    mismatches: list[dict[str, Any]] = []
    n = min(len(e), len(r))
    exact = 0
    for i in range(n):
        er = e.iloc[i]
        rr = r.iloc[i]
        bad = False
        detail: dict[str, Any] = {"i": i, "candle_start_unix": int(er["candle_start_unix"])}
        if int(er["candle_start_unix"]) != int(rr["candle_start_unix"]):
            bad = True
            detail["start_mismatch"] = True
        if int(er["available_at_unix"]) != int(rr["available_at_unix"]):
            bad = True
            detail["available_at_mismatch"] = True
        for c in cols_num:
            a, b = er.get(c), rr.get(c)
            if a is None and b is None:
                continue
            if pd.isna(a) and pd.isna(b):
                continue
            try:
                if abs(float(a) - float(b)) > float_tol:
                    bad = True
                    detail[c] = {"engine": a, "ref": b}
            except (TypeError, ValueError):
                if a != b:
                    bad = True
                    detail[c] = {"engine": a, "ref": b}
        if bad:
            mismatches.append(detail)
        else:
            exact += 1
    return {
        "ok": len(mismatches) == 0 and len(e) == len(r),
        "n_engine": len(e),
        "n_reference": len(r),
        "n_compared": n,
        "n_exact_rows": exact,
        "n_mismatches": len(mismatches),
        "mismatches_first_20": mismatches[:20],
        "float_tol": float_tol,
    }
