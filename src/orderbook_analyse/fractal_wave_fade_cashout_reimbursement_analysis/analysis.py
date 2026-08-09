"""Orchestrate cashout + reimbursement analysis."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_wave_fade_cashout_reimbursement_analysis import (
    AUDIT_VERSION,
    CASHOUT_RATES,
    COVERAGE_RATES,
    DEFINITIONS_DOC,
    EXPECTED_N_TRADES,
    KNOWN_WORST_SL_END,
    KNOWN_WORST_SL_START,
    REF_TRADES,
    START_ACTIVE,
)
from orderbook_analyse.fractal_wave_fade_cashout_reimbursement_analysis.simulate import (
    simulate,
    simulate_cashout_only,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def load_trades() -> pd.DataFrame:
    df = pd.read_csv(_repo_root() / REF_TRADES)
    for c in ("entry_time", "exit_time", "signal_time"):
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], utc=True)
    return df.sort_values(["exit_time", "trade_id"]).reset_index(drop=True)


def _max_streak(df: pd.DataFrame, pred) -> tuple[int, dict | None]:
    start = None
    best = (0, None)
    streaks = []
    for i, row in df.iterrows():
        ok = bool(pred(row))
        if ok:
            if start is None:
                start = i
        elif start is not None:
            streaks.append((start, i - 1))
            start = None
    if start is not None:
        streaks.append((start, len(df) - 1))
    if not streaks:
        return 0, None
    a, b = max(streaks, key=lambda ab: (ab[1] - ab[0], -df.iloc[ab[0] : ab[1] + 1]["net_return_pct"].sum()))
    seg = df.iloc[a : b + 1]
    info = {
        "length": int(b - a + 1),
        "start_trade_id": int(seg.iloc[0]["trade_id"]),
        "end_trade_id": int(seg.iloc[-1]["trade_id"]),
        "start_time": pd.Timestamp(seg.iloc[0]["exit_time"]).isoformat(),
        "end_time": pd.Timestamp(seg.iloc[-1]["exit_time"]).isoformat(),
        "sum_net_return_pct": float(seg["net_return_pct"].sum()),
        "trade_ids": seg["trade_id"].astype(int).tolist(),
    }
    return info["length"], info


def _streak_detail(path: pd.DataFrame, trade_ids: list[int]) -> pd.DataFrame:
    p = path[path["trade_id"].isin(trade_ids)].sort_values(["exit_time", "trade_id"])
    return p


def _streak_summary(path: pd.DataFrame, trade_ids: list[int], cashout_rate: float, coverage: float) -> dict[str, Any]:
    seg = _streak_detail(path, trade_ids)
    if seg.empty:
        return {}
    first, last = seg.iloc[0], seg.iloc[-1]
    fully = int(seg["loss_fully_covered"].sum())
    partial = int(seg["loss_partially_covered"].sum())
    # first empty after reimbursement
    empty_at = None
    for i, r in seg.iterrows():
        if float(r["reserve_after"]) <= 1e-12 and float(r["reserve_before"]) > 1e-12:
            empty_at = int(r["trade_id"])
            break
        if float(r["reserve_after"]) <= 1e-12 and empty_at is None and float(r["reimbursement_amount"]) > 0:
            empty_at = int(r["trade_id"])
            break
    a0 = float(first["active_before"])
    r0 = float(first["reserve_before"])
    t0 = a0 + r0
    a1 = float(last["active_after"])
    r1 = float(last["reserve_after"])
    t1 = a1 + r1
    return {
        "cashout_rate_pct": int(round(cashout_rate * 100)),
        "coverage_rate_pct": int(round(coverage * 100)),
        "active_before": a0,
        "reserve_before": r0,
        "total_before": t0,
        "active_after": a1,
        "reserve_after": r1,
        "total_after": t1,
        "fully_covered_sl": fully,
        "partially_covered_sl": partial,
        "n_sl": int(len(seg)),
        "reserve_empty_from_trade_id": empty_at,
        "active_dd_pct": float((a1 / a0 - 1.0) * 100.0) if a0 else None,
        "total_dd_pct": float((t1 / t0 - 1.0) * 100.0) if t0 else None,
        "sum_net_return_pct": float(seg["net_return_pct"].sum()),
        "total_reimbursed_in_streak": float(seg["reimbursement_amount"].sum()),
    }


def run_analysis() -> dict[str, Any]:
    print(DEFINITIONS_DOC, flush=True)
    trades = load_trades()
    assert len(trades) == EXPECTED_N_TRADES

    max_sl, worst_sl = _max_streak(trades, lambda r: str(r["exit_reason"]) == "SL")
    max_lose, worst_lose = _max_streak(trades, lambda r: float(r["net_return_pct"]) < 0)
    print(f"[streaks] max_SL={max_sl} max_losing={max_lose}", flush=True)
    assert worst_sl is not None
    # prefer known streak if matches length 10
    if (
        worst_sl["start_trade_id"] != KNOWN_WORST_SL_START
        or worst_sl["end_trade_id"] != KNOWN_WORST_SL_END
    ):
        # still use detected longest
        print(
            f"[note] detected worst SL {worst_sl['start_trade_id']}-{worst_sl['end_trade_id']} "
            f"(known ref {KNOWN_WORST_SL_START}-{KNOWN_WORST_SL_END})",
            flush=True,
        )

    matrix_rows = []
    paths_store = {}
    loss_rows = []
    streak_impact_rows = []
    streak_detail_frames = []
    depletion_rows = []

    # Primary matrix: cashout × coverage with ALL_NEGATIVE + 100% focus
    for cr in CASHOUT_RATES:
        for cov in ((1.0,) if cr == 0.0 else COVERAGE_RATES):
            print(f"[sim] cashout={int(cr*100)}% coverage={int(cov*100)}%", flush=True)
            res = simulate(trades, cashout_rate=cr, coverage_rate=cov, reimburse_mode="ALL_NEGATIVE")
            key = (cr, cov, "ALL_NEGATIVE")
            paths_store[key] = res["path"]
            s = res["summary"]
            matrix_rows.append(
                {
                    "cashout_rate_pct": s["cashout_rate_pct"],
                    "coverage_rate_pct": s["coverage_rate_pct"],
                    "reimburse_mode": s["reimburse_mode"],
                    "end_active": s["end_active"],
                    "end_reserve": s["end_reserve"],
                    "end_total_wealth": s["end_total_wealth"],
                    "active_return_pct": s["active_return_pct"],
                    "total_wealth_return_pct": s["total_wealth_return_pct"],
                    "active_max_dd_pct": s["active_max_dd_pct"],
                    "active_max_dd_usdt": s["active_max_dd_usdt"],
                    "total_max_dd_pct": s["total_max_dd_pct"],
                    "total_max_dd_usdt": s["total_max_dd_usdt"],
                    "fully_reimbursed": s["fully_reimbursed"],
                    "partially_reimbursed": s["partially_reimbursed"],
                    "unreimbursed": s["unreimbursed"],
                    "n_losses": s["n_losses"],
                    "full_cover_rate": s["full_cover_rate"],
                    "usdt_reimbursement_coverage": s["usdt_reimbursement_coverage"],
                    "total_reimbursed_usdt": s["total_reimbursed_usdt"],
                    "total_unreimbursed_usdt": s["total_unreimbursed_usdt"],
                    "reserve_hit_zero_events": s["reserve_hit_zero_events"],
                    "reserve_zero_share": s["reserve_zero_share"],
                    "max_consecutive_reserve_empty_trades": s["max_consecutive_reserve_empty_trades"],
                    "reserve_p10": s["reserve_p10"],
                    "reserve_p25": s["reserve_p25"],
                    "reserve_p50": s["reserve_p50"],
                    "reserve_p75": s["reserve_p75"],
                    "reserve_p90": s["reserve_p90"],
                    "peak_time_active": s["dd_active"]["peak_time"],
                    "trough_time_active": s["dd_active"]["trough_time"],
                    "recovery_time_active": s["dd_active"]["recovery_time"],
                    "trades_to_recovery_active": s["dd_active"]["trades_to_recovery"],
                }
            )
            depletion_rows.append(
                {
                    "cashout_rate_pct": s["cashout_rate_pct"],
                    "coverage_rate_pct": s["coverage_rate_pct"],
                    "reserve_hit_zero_events": s["reserve_hit_zero_events"],
                    "reserve_zero_share": s["reserve_zero_share"],
                    "reserve_positive_share": s["reserve_positive_share"],
                    "max_consecutive_reserve_empty_trades": s["max_consecutive_reserve_empty_trades"],
                    "reserve_p10": s["reserve_p10"],
                    "reserve_p50": s["reserve_p50"],
                    "reserve_p90": s["reserve_p90"],
                }
            )
            # loss rows sample: all reimbursements for primary 100% coverage variants
            if cov == 1.0:
                path = res["path"]
                losses = path[path["raw_trade_pnl"] < 0].copy()
                losses["variant"] = f"c{s['cashout_rate_pct']}_cov{s['coverage_rate_pct']}"
                loss_rows.append(losses)

                det = _streak_detail(path, worst_sl["trade_ids"])
                det = det.copy()
                det["cashout_rate_pct"] = s["cashout_rate_pct"]
                det["coverage_rate_pct"] = s["coverage_rate_pct"]
                streak_detail_frames.append(det)
                streak_impact_rows.append(
                    _streak_summary(path, worst_sl["trade_ids"], cr, cov)
                )

    # SL_ONLY sensitivity at 30%/100%
    print("[sim] SL_ONLY sensitivity 30%/100% …", flush=True)
    sl_only = simulate(trades, cashout_rate=0.30, coverage_rate=1.0, reimburse_mode="SL_ONLY")
    s = sl_only["summary"]
    matrix_rows.append(
        {
            "cashout_rate_pct": 30,
            "coverage_rate_pct": 100,
            "reimburse_mode": "SL_ONLY",
            "end_active": s["end_active"],
            "end_reserve": s["end_reserve"],
            "end_total_wealth": s["end_total_wealth"],
            "active_return_pct": s["active_return_pct"],
            "total_wealth_return_pct": s["total_wealth_return_pct"],
            "active_max_dd_pct": s["active_max_dd_pct"],
            "active_max_dd_usdt": s["active_max_dd_usdt"],
            "total_max_dd_pct": s["total_max_dd_pct"],
            "total_max_dd_usdt": s["total_max_dd_usdt"],
            "fully_reimbursed": s["fully_reimbursed"],
            "partially_reimbursed": s["partially_reimbursed"],
            "unreimbursed": s["unreimbursed"],
            "n_losses": s["n_losses"],
            "full_cover_rate": s["full_cover_rate"],
            "usdt_reimbursement_coverage": s["usdt_reimbursement_coverage"],
            "total_reimbursed_usdt": s["total_reimbursed_usdt"],
            "total_unreimbursed_usdt": s["total_unreimbursed_usdt"],
            "reserve_hit_zero_events": s["reserve_hit_zero_events"],
            "reserve_zero_share": s["reserve_zero_share"],
            "max_consecutive_reserve_empty_trades": s["max_consecutive_reserve_empty_trades"],
            "reserve_p10": s["reserve_p10"],
            "reserve_p25": s["reserve_p25"],
            "reserve_p50": s["reserve_p50"],
            "reserve_p75": s["reserve_p75"],
            "reserve_p90": s["reserve_p90"],
            "peak_time_active": s["dd_active"]["peak_time"],
            "trough_time_active": s["dd_active"]["trough_time"],
            "recovery_time_active": s["dd_active"]["recovery_time"],
            "trades_to_recovery_active": s["dd_active"]["trades_to_recovery"],
        }
    )

    # Comparison cashout-only vs reimbursement (100% coverage)
    cmp_rows = []
    for cr in CASHOUT_RATES:
        only = simulate_cashout_only(trades, cr)
        reimb = paths_store.get((cr, 1.0, "ALL_NEGATIVE"))
        if reimb is None:
            reimb_res = simulate(trades, cashout_rate=cr, coverage_rate=1.0)
            reimb = reimb_res["path"]
            so = reimb_res["summary"]
        else:
            # rebuild summary from matrix
            so = next(
                r
                for r in matrix_rows
                if r["cashout_rate_pct"] == int(round(cr * 100))
                and r["coverage_rate_pct"] == 100
                and r["reimburse_mode"] == "ALL_NEGATIVE"
            )
        oo = only["summary"]
        cmp_rows.append(
            {
                "cashout_rate_pct": int(round(cr * 100)),
                "variant": "cashout_only",
                "end_active": oo["end_active"],
                "end_reserve": oo["end_reserve"],
                "end_total_wealth": oo["end_total_wealth"],
                "active_max_dd_pct": oo["active_max_dd_pct"],
                "total_max_dd_pct": oo["total_max_dd_pct"],
                "reserve_hit_zero_events": oo["reserve_hit_zero_events"],
            }
        )
        cmp_rows.append(
            {
                "cashout_rate_pct": int(round(cr * 100)),
                "variant": "cashout_plus_reimbursement_100",
                "end_active": so["end_active"] if isinstance(so, dict) else so.end_active,
                "end_reserve": so["end_reserve"],
                "end_total_wealth": so["end_total_wealth"],
                "active_max_dd_pct": so["active_max_dd_pct"],
                "total_max_dd_pct": so["total_max_dd_pct"],
                "reserve_hit_zero_events": so["reserve_hit_zero_events"],
            }
        )

    # equity paths: primary 100% coverage all cashout rates
    eq_frames = []
    for cr in CASHOUT_RATES:
        key = (cr, 1.0, "ALL_NEGATIVE")
        if key not in paths_store:
            paths_store[key] = simulate(trades, cashout_rate=cr, coverage_rate=1.0)["path"]
        eq_frames.append(
            paths_store[key].assign(
                cashout_rate_pct=int(round(cr * 100)), coverage_rate_pct=100
            )
        )

    # baseline controls
    nets = trades["net_return_pct"].astype(float)
    controls = {
        "n_trades": int(len(trades)),
        "tp": int((trades["exit_reason"] == "TP").sum()),
        "sl": int((trades["exit_reason"] == "SL").sum()),
        "expectancy": float(nets.mean()),
        "profit_factor": float(
            nets[nets > 0].sum() / abs(nets[nets < 0].sum())
        ),
        "max_consecutive_sl": max_sl,
        "max_consecutive_losing_trades": max_lose,
        "worst_sl_streak": worst_sl,
        "worst_losing_streak": worst_lose,
    }

    # 0% parity
    p0 = paths_store[(0.0, 1.0, "ALL_NEGATIVE")]
    parity = True
    if "equity_after_100" in trades.columns:
        ref = trades.sort_values(["exit_time", "trade_id"]).reset_index(drop=True)
        parity = bool(
            np.allclose(
                p0["active_after"].astype(float),
                ref["equity_after_100"].astype(float),
                rtol=1e-9,
                atol=1.0,
            )
        )

    interpretation = (
        "Reimbursement moves prior cashed profits back into ACTIVE after losses; "
        "it does not erase economic loss — TOTAL_WEALTH still falls by the raw trade PnL. "
        "Whether reserves absorb typical SL streaks depends on cashout rate and prior "
        "reserve balance; high cashout builds buffer but slows compounding."
    )

    return {
        "audit_version": AUDIT_VERSION,
        "definitions": DEFINITIONS_DOC,
        "controls": controls,
        "parity_0pct": parity,
        "matrix": pd.DataFrame(matrix_rows),
        "equity_paths": pd.concat(eq_frames, ignore_index=True),
        "loss_reimbursements": pd.concat(loss_rows, ignore_index=True) if loss_rows else pd.DataFrame(),
        "worst_10_sl_streak_detail": pd.concat(streak_detail_frames, ignore_index=True)
        if streak_detail_frames
        else pd.DataFrame(),
        "worst_sl_streak_impact": pd.DataFrame(streak_impact_rows),
        "reserve_depletion_statistics": pd.DataFrame(depletion_rows),
        "comparison_cashout_only_vs_reimbursement": pd.DataFrame(cmp_rows),
        "interpretation": interpretation,
        "start_active": START_ACTIVE,
    }
