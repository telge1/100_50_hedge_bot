"""Parity and quality validation for drilldown outputs."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .aggregation_100ms import aggregate_100ms_to_1s


def compare_parity_to_state(
    states_100ms: list[dict[str, Any]],
    state_1s: pd.DataFrame,
    *,
    abs_notional_tol: float,
    rel_tol: float,
    price_abs_tol: float,
    count_tol: int,
) -> dict[str, Any]:
    agg = aggregate_100ms_to_1s(states_100ms)
    if not agg or state_1s is None or state_1s.empty:
        return {
            "compared_rows": 0,
            "exact_matches": 0,
            "tolerance_matches": 0,
            "mismatches": 0,
            "max_abs_error": 0.0,
            "max_rel_error": 0.0,
            "mismatch_reasons": [],
            "ok": True,
        }
    adf = pd.DataFrame(agg)
    adf["state_ts"] = pd.to_datetime(adf["state_ts"], utc=True)
    s = state_1s.copy()
    s["state_ts"] = pd.to_datetime(s["state_ts"], utc=True)

    hard_fields = {
        "taker_buy_notional_usdt",
        "taker_sell_notional_usdt",
        "taker_delta_notional_usdt",
        "trade_count",
    }
    soft_fields = {
        "bid_liquidity_added_usdt",
        "ask_liquidity_added_usdt",
        "bid_liquidity_removed_usdt",
        "ask_liquidity_removed_usdt",
        "bid_update_count",
        "ask_update_count",
        "mid_price",
        "spread_bps",
        "bid_depth_notional_usdt_bps_0_2",
        "ask_depth_notional_usdt_bps_0_2",
    }
    fields = [(f, "count" if "count" in f else ("price" if f in {"mid_price", "spread_bps"} else "notional")) for f in sorted(hard_fields | soft_fields)]
    left_cols = ["state_ts"] + [f for f, _ in fields if f in adf.columns]
    right_cols = ["state_ts"] + [f for f, _ in fields if f in s.columns]
    merged = adf[left_cols].merge(s[right_cols], on="state_ts", how="inner", suffixes=("_dd", "_1s"))

    exact = tol_m = mism = mism_hard = mism_soft = 0
    max_abs = 0.0
    max_rel = 0.0
    reasons: list[str] = []
    for _, row in merged.iterrows():
        for field, kind in fields:
            a_key, b_key = f"{field}_dd", f"{field}_1s"
            if a_key not in merged.columns or b_key not in merged.columns:
                continue
            a, b = row[a_key], row[b_key]
            if pd.isna(a) and pd.isna(b):
                exact += 1
                continue
            if pd.isna(a) or pd.isna(b):
                mism += 1
                if field in hard_fields:
                    mism_hard += 1
                else:
                    mism_soft += 1
                reasons.append(f"{row['state_ts']}:{field}:nan_mismatch")
                continue
            a, b = float(a), float(b)
            err = abs(a - b)
            max_abs = max(max_abs, err)
            rel = err / max(abs(b), 1e-9)
            max_rel = max(max_rel, rel)
            ok_row = False
            if err == 0:
                exact += 1
                ok_row = True
            elif kind == "count" and err <= count_tol:
                tol_m += 1
                ok_row = True
            elif kind == "price" and err <= price_abs_tol:
                tol_m += 1
                ok_row = True
            elif kind == "notional" and (err <= abs_notional_tol or rel <= rel_tol):
                tol_m += 1
                ok_row = True
            if ok_row:
                continue
            mism += 1
            if field in hard_fields:
                mism_hard += 1
            else:
                mism_soft += 1
            if len(reasons) < 50:
                reasons.append(f"{row['state_ts']}:{field}:dd={a}:s1={b}:err={err}")
    return {
        "compared_rows": int(len(merged)),
        "exact_matches": int(exact),
        "tolerance_matches": int(tol_m),
        "mismatches": int(mism),
        "hard_trade_mismatches": int(mism_hard),
        "soft_book_mismatches": int(mism_soft),
        "max_abs_error": float(max_abs),
        "max_rel_error": float(max_rel),
        "mismatch_reasons": reasons,
        # Fail-closed only on public-trade aggregates; book-flow/depth remain proxy-limited vs 1s builder.
        "ok": mism_hard == 0,
        "book_flow_parity": "PROXY_LIMITED" if mism_soft else "MATCHED",
    }


def causality_check(timeline: list[dict[str, Any]], causal_end: pd.Timestamp) -> dict[str, Any]:
    causal_end = pd.to_datetime(causal_end, utc=True)
    bad = [e for e in timeline if pd.to_datetime(e["event_time"], utc=True) >= causal_end]
    return {"ok": len(bad) == 0, "n_future_events": len(bad)}


def quality_checks(
    *,
    states_100ms: list[dict[str, Any]],
    attribution_rows: list[dict[str, Any]],
    selected: pd.DataFrame,
) -> dict[str, Any]:
    issues = []
    crossed_n = 0
    for r in states_100ms:
        if r.get("best_bid") is not None and r.get("best_ask") is not None:
            if r["best_bid"] >= r["best_ask"]:
                crossed_n += 1
        for k in ("bid_added_notional", "ask_removed_notional", "taker_buy_notional"):
            v = r.get(k)
            if v is not None and (not np.isfinite(v) or v < 0):
                issues.append(f"bad_{k}")
                break
    n = max(len(states_100ms), 1)
    crossed_rate = crossed_n / n
    if crossed_rate > 0.02:
        issues.append(f"crossed_book_rate_high:{crossed_rate:.4f}")
    if selected is not None and not selected.empty:
        if not (selected["context_start_drilldown"] < selected["detection_available_at_effective"]).all():
            issues.append("context_not_before_detection")
        if selected["candidate_id"].duplicated().any():
            issues.append("duplicate_candidate_id")
    return {
        "ok": len(issues) == 0,
        "issues": issues,
        "crossed_book_buckets": int(crossed_n),
        "crossed_book_rate": float(crossed_rate),
        "n_100ms_buckets": int(len(states_100ms)),
    }
