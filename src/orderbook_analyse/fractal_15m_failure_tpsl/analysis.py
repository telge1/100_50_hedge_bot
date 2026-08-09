"""Orchestrate fixed TP/SL grid analysis for T0 failure entries."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_15m_failure_tpsl import (
    AUDIT_VERSION,
    ENTRY_DOC,
    FOCUS_COMBOS,
    METHOD_DOC,
    MIN_SAMPLE,
    SL_GRID,
    SYMBOL,
    TP_GRID,
)
from orderbook_analyse.fractal_15m_failure_tpsl.simulate import (
    build_trade_paths,
    first_touch_matrix,
    load_1m,
    load_t0_entries,
    mae_mfe_summary,
    run_grid,
    summarize_combo,
)


def decide_primary(combined_rows: list[dict]) -> str:
    """
    POSITIVE_EXPECTANCY: >=3 combos with mean_net>0, PF>1, n>=30, and focus set has >=1.
    TOO_SMALL: some positive but mean_net < 0.02 or weak PF / large DD.
    NOT_PROFITABLE: no robust positive expectancy after fees.
    """
    ok = [
        r
        for r in combined_rows
        if r.get("n", 0) >= MIN_SAMPLE
        and (r.get("mean_net_return") or 0) > 0
        and (r.get("profit_factor") or 0) > 1.0
        and (r.get("net_total_return") or 0) > 0
    ]
    focus_ok = [
        r
        for r in ok
        if (r.get("tp_pct"), r.get("sl_pct")) in FOCUS_COMBOS
    ]
    if len(ok) >= 3 and (focus_ok or len(ok) >= 5):
        strong = [r for r in ok if (r.get("mean_net_return") or 0) >= 0.02]
        if strong:
            return "FIXED_TPSL_HAS_POSITIVE_EXPECTANCY_AFTER_FEES"
        return "FIXED_TPSL_EDGE_TOO_SMALL_AFTER_FEES"
    if len(ok) >= 1:
        return "FIXED_TPSL_EDGE_TOO_SMALL_AFTER_FEES"
    return "FIXED_TPSL_NOT_PROFITABLE"


def decide_long_short(long_rows: list[dict], short_rows: list[dict]) -> str:
    def best(rows: list[dict]) -> tuple[float, float] | None:
        cand = [
            r
            for r in rows
            if r.get("n", 0) >= MIN_SAMPLE and (r.get("mean_net_return") or -999) > -999
        ]
        if not cand:
            return None
        b = max(cand, key=lambda r: (r.get("mean_net_return") or -999))
        return float(b["tp_pct"]), float(b["sl_pct"])

    bl, bs = best(long_rows), best(short_rows)
    if bl is None or bs is None:
        return "LONG_SHORT_SAME_TPSL_REASONABLE"
    if bl == bs:
        return "LONG_SHORT_SAME_TPSL_REASONABLE"
    # different if TP or SL differs by more than one grid step meaningfully
    if abs(bl[0] - bs[0]) >= 0.1 or abs(bl[1] - bs[1]) >= 0.2:
        return "LONG_SHORT_REQUIRE_DIFFERENT_TPSL"
    return "LONG_SHORT_SAME_TPSL_REASONABLE"


def decide_sl(combined_rows: list[dict]) -> str:
    """SL viable if positive expectancy combos exist with sl_rate not dominating destructively."""
    pos = [
        r
        for r in combined_rows
        if (r.get("mean_net_return") or 0) > 0 and (r.get("profit_factor") or 0) > 1
    ]
    if not pos:
        # check if tight SLs destroy
        tight = [r for r in combined_rows if r.get("sl_pct") in (0.15, 0.20)]
        if tight and np.mean([r.get("mean_net_return") or 0 for r in tight]) < -0.05:
            return "FIXED_SL_TOO_DESTRUCTIVE"
        return "FIXED_SL_TOO_DESTRUCTIVE"
    # if best need very wide SL and still weak — still viable if pos exists
    return "FIXED_SL_APPEARS_VIABLE"


def run_analysis() -> dict[str, Any]:
    print("[load] T0 entries", flush=True)
    entries = load_t0_entries()
    print(f"[load] n={len(entries)}", flush=True)

    print("[load] 1m candles", flush=True)
    c1 = load_1m()
    high = c1["high"].astype(float).to_numpy()
    low = c1["low"].astype(float).to_numpy()
    close = c1["close"].astype(float).to_numpy()
    open_times = c1["timestamp"].to_numpy(dtype="datetime64[ns]")

    print("[path] precompute trade paths", flush=True)
    paths = build_trade_paths(entries, high, low, close, open_times)
    print(f"[path] valid={sum(1 for p in paths if p['valid'])}", flush=True)

    print("[grid] SL_FIRST primary", flush=True)
    trades = run_grid(paths, policy="SL_FIRST")

    print("[grid] TP_FIRST sensitivity (focus only)", flush=True)
    # sensitivity: only focus combos to limit size
    sens_rows = []
    for tp, sl in FOCUS_COMBOS:
        for path in paths:
            ev = path["ev"]
            from orderbook_analyse.fractal_15m_failure_tpsl.simulate import resolve_on_path

            sim = resolve_on_path(path, tp_pct=tp, sl_pct=sl, policy="TP_FIRST")
            sens_rows.append(
                {
                    "wave_i": int(ev.wave_i),
                    "side": ev.side,
                    "tp_pct": tp,
                    "sl_pct": sl,
                    "policy": "TP_FIRST",
                    **sim,
                    "entry_time": ev.entry_time,
                }
            )
    sens = pd.DataFrame(sens_rows)

    print("[summary] grids", flush=True)
    combined_rows = []
    long_rows = []
    short_rows = []
    hold_rows = []
    for tp in TP_GRID:
        for sl in SL_GRID:
            sub = trades[(trades["tp_pct"] == tp) & (trades["sl_pct"] == sl)]
            for side, bucket in (
                ("COMBINED", combined_rows),
                ("LONG", long_rows),
                ("SHORT", short_rows),
            ):
                s = sub if side == "COMBINED" else sub[sub["side"] == side]
                m = summarize_combo(s, side=side, tp_pct=tp, sl_pct=sl, policy="SL_FIRST")
                bucket.append(m)
                hold_rows.append(
                    {
                        "side": side,
                        "tp_pct": tp,
                        "sl_pct": sl,
                        "median_hold_min": m.get("median_hold_min"),
                        "mean_hold_min": m.get("mean_hold_min"),
                        "median_hold_tp": m.get("median_hold_tp"),
                        "median_hold_sl": m.get("median_hold_sl"),
                        "median_hold_time": m.get("median_hold_time"),
                        "share_exit_le15": m.get("share_exit_le15"),
                        "share_exit_le30": m.get("share_exit_le30"),
                        "share_exit_le60": m.get("share_exit_le60"),
                        "share_exit_le120": m.get("share_exit_le120"),
                        "share_exit_le240": m.get("share_exit_le240"),
                    }
                )

    # TP_FIRST sensitivity summary for focus
    sens_summary = []
    for tp, sl in FOCUS_COMBOS:
        sub = sens[(sens["tp_pct"] == tp) & (sens["sl_pct"] == sl)]
        sens_summary.append(
            summarize_combo(sub, side="COMBINED", tp_pct=tp, sl_pct=sl, policy="TP_FIRST")
        )

    print("[diag] first touch / mae-mfe", flush=True)
    ft_rows = first_touch_matrix(paths)
    mae_rows = mae_mfe_summary(paths)

    # monthly stability for focus + best in-sample combined
    print("[diag] monthly stability", flush=True)
    best = max(combined_rows, key=lambda r: (r.get("mean_net_return") or -999))
    monthly_rows = []
    best_pair = (float(best["tp_pct"]), float(best["sl_pct"]))
    focus_plus: list[tuple[float, float, str]] = [
        (float(tp), float(sl), "FOCUS") for tp, sl in FOCUS_COMBOS
    ]
    if best_pair not in FOCUS_COMBOS:
        focus_plus.append((best_pair[0], best_pair[1], "BEST_INSAMPLE_DIAG"))
    else:
        # best coincides with a focus row — tag months as both for clarity
        focus_plus = [
            (tp, sl, "FOCUS+BEST_INSAMPLE_DIAG" if (tp, sl) == best_pair else "FOCUS")
            for tp, sl, _ in focus_plus
        ]
    trades["month"] = pd.to_datetime(trades["entry_time"], utc=True).dt.strftime("%Y-%m")
    for tp, sl, label in focus_plus:
        sub0 = trades[(trades["tp_pct"] == tp) & (trades["sl_pct"] == sl)]
        for month, sub in sub0.groupby("month"):
            m = summarize_combo(
                sub, side="COMBINED", tp_pct=tp, sl_pct=sl, month=month, combo_set=label
            )
            monthly_rows.append(m)

    # signal strength diagnostic on a mid focus combo TP0.25/SL0.40
    print("[diag] signal strength", flush=True)
    mid = trades[(trades["tp_pct"] == 0.25) & (trades["sl_pct"] == 0.40)].merge(
        entries[
            [
                "wave_i",
                "M15_signed_price_move_pct",
                "M15_directional_efficiency",
                "wave_duration_min",
                "M15_rsi_end",
                "M15_stoch_k_start",
                "M15_stoch_k_end",
                "partial_fail_streak_1m",
            ]
        ],
        on="wave_i",
        how="left",
    )
    strength_rows = []
    for feat in (
        "M15_directional_efficiency",
        "M15_signed_price_move_pct",
        "wave_duration_min",
        "partial_fail_streak_1m",
        "M15_rsi_end",
    ):
        try:
            mid[f"{feat}_q"] = pd.qcut(
                mid[feat].astype(float), 4, labels=["Q1", "Q2", "Q3", "Q4"], duplicates="drop"
            )
        except ValueError:
            mid[f"{feat}_q"] = "NA"
        for q, sub in mid.groupby(mid[f"{feat}_q"].astype(str)):
            strength_rows.append(
                {
                    "feature": feat,
                    "quantile": q,
                    "n": int(len(sub)),
                    "mean_net_return": float(sub["net_ret"].mean()),
                    "median_net_return": float(sub["net_ret"].median()),
                    "win_rate": float((sub["net_ret"] > 0).mean()),
                    "tp_rate": float((sub["exit_type"] == "TP").mean()),
                    "sl_rate": float((sub["exit_type"] == "SL").mean()),
                }
            )
        # winners vs losers feature means
        w = mid[mid["net_ret"] > 0][feat].astype(float)
        l = mid[mid["net_ret"] < 0][feat].astype(float)
        strength_rows.append(
            {
                "feature": feat,
                "quantile": "WINNERS_vs_LOSERS",
                "n": int(len(mid)),
                "winner_mean_feature": float(w.mean()) if len(w) else None,
                "loser_mean_feature": float(l.mean()) if len(l) else None,
                "diff_winner_minus_loser": float(w.mean() - l.mean()) if len(w) and len(l) else None,
            }
        )

    primary = decide_primary(combined_rows)
    ls_dec = decide_long_short(long_rows, short_rows)
    sl_dec = decide_sl(combined_rows)

    return {
        "audit_version": AUDIT_VERSION,
        "symbol": SYMBOL,
        "n_events": int(len(entries)),
        "best_insample_diag": {
            "tp_pct": best.get("tp_pct"),
            "sl_pct": best.get("sl_pct"),
            "mean_net_return": best.get("mean_net_return"),
            "profit_factor": best.get("profit_factor"),
            "note": "diagnostic only — not confirmed; needs unchanged OOS test",
        },
        "tpsl_grid_combined": combined_rows,
        "tpsl_grid_long": long_rows,
        "tpsl_grid_short": short_rows,
        "trade_results": trades,
        "first_touch_matrix": ft_rows,
        "mae_mfe_summary": mae_rows,
        "holding_time_summary": hold_rows,
        "monthly_stability": monthly_rows,
        "signal_strength_diagnostic": strength_rows,
        "tp_first_sensitivity_focus": sens_summary,
        "decisions": {
            "primary": primary,
            "long_short": ls_dec,
            "fixed_sl": sl_dec,
        },
        "method": {
            "entry": ENTRY_DOC.strip(),
            "general": METHOD_DOC.strip(),
            "tp_grid": list(TP_GRID),
            "sl_grid": list(SL_GRID),
            "fee_pct": 0.11,
            "ambiguous_primary": "SL_FIRST",
        },
    }
