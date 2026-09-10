"""Dashboard vs engine AVR parity helpers."""

from __future__ import annotations

from typing import Any

import pandas as pd


def parity_report(
    engine_df: pd.DataFrame,
    reference_df: pd.DataFrame,
    *,
    float_tol: float = 1e-6,
) -> dict[str, Any]:
    """Compare two AVR 1s frames keyed by state_ts_unix."""
    a = engine_df.set_index("state_ts_unix")
    b = reference_df.set_index("state_ts_unix")
    keys = sorted(set(a.index) & set(b.index))
    only_a = sorted(set(a.index) - set(b.index))
    only_b = sorted(set(b.index) - set(a.index))
    mismatches: list[dict[str, Any]] = []
    state_mismatch = 0
    numeric_mismatch = 0
    exact = 0
    compare_cols_num = [
        "avr_buy_notional",
        "avr_sell_notional",
        "avr_delta_notional",
        "avr_trade_count",
        "avr_price_velocity_bps",
        "avr_up_velocity_percentile",
        "avr_down_velocity_percentile",
    ]
    for k in keys:
        ra, rb = a.loc[k], b.loc[k]
        row_ok = True
        if str(ra["avr_state"]) != str(rb["avr_state"]):
            state_mismatch += 1
            row_ok = False
            if len(mismatches) < 20:
                mismatches.append(
                    {
                        "state_ts_unix": int(k),
                        "field": "avr_state",
                        "engine": ra["avr_state"],
                        "reference": rb["avr_state"],
                    }
                )
        if int(ra["available_at_unix"]) != int(rb["available_at_unix"]):
            row_ok = False
            if len(mismatches) < 20:
                mismatches.append(
                    {
                        "state_ts_unix": int(k),
                        "field": "available_at_unix",
                        "engine": int(ra["available_at_unix"]),
                        "reference": int(rb["available_at_unix"]),
                    }
                )
        for c in compare_cols_num:
            va, vb = ra.get(c), rb.get(c)
            if pd.isna(va) and pd.isna(vb):
                continue
            if pd.isna(va) or pd.isna(vb) or abs(float(va) - float(vb)) > float_tol:
                numeric_mismatch += 1
                row_ok = False
                if len(mismatches) < 20:
                    mismatches.append(
                        {
                            "state_ts_unix": int(k),
                            "field": c,
                            "engine": va,
                            "reference": vb,
                        }
                    )
        if row_ok:
            exact += 1
    ok = (
        state_mismatch == 0
        and numeric_mismatch == 0
        and not only_a
        and not only_b
        and len(keys) == len(engine_df) == len(reference_df)
    )
    return {
        "ok": ok,
        "float_tol": float_tol,
        "n_engine": int(len(engine_df)),
        "n_reference": int(len(reference_df)),
        "n_compared": int(len(keys)),
        "n_exact_rows": exact,
        "n_state_mismatch": state_mismatch,
        "n_numeric_mismatch": numeric_mismatch,
        "missing_in_reference": only_a[:20],
        "missing_in_engine": only_b[:20],
        "mismatches_first_20": mismatches,
        "note": "Reference is a second independent classify pass over the same Dashboard buckets.",
    }
