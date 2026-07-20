"""C3.5D protected-break reclaim-fallback audit (offline, research-only).

Tests reclaim + short-timeout (+ optional -0.25R recovery) against B0/B1/M1/M4.
Reuses V_1LAG and exit-management helpers. No runtime changes. No commit.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.pullback_entry_c3_5d_apt_raw_audit import build_apt_d1_frame
from research.regime_scanner.pullback_entry_c3_5d_protected_break_exit_management_audit import (
    ExitResult,
    PathEvents,
    _bars_since,
    _mean,
    _median,
    _pine_float,
    _pine_int,
    _quantile,
    _ts,
    apply_fees_pct,
    bar_ohlc,
    build_path_events,
    exit_price_for_event,
    pnl_pct_from_price,
    pnl_r_from_pct,
    reclaim_local,
    scan_reclaims,
    simulate_B0,
    simulate_B1,
    simulate_M1_local_reclaim,
    simulate_M4_r_target,
    target_hit_pct,
    window_end,
)
from research.regime_scanner.pullback_entry_c3_5d_protected_carry_audit import (
    assign_effective_levels,
    ensure_ohlc,
    load_setups,
)
from research.regime_scanner.trend_pine_export import build_pine_header, validate_pine_script

PHASE = "C3.5D_RECLAIM_FALLBACK"
DEFAULT_APT_DIR = Path(
    "research/regime_scanner/results/phase_c3_5d_continuation_early_failure/apt_audit"
)
DEFAULT_OUT = DEFAULT_APT_DIR / "reclaim_fallback"
PINE_DIR = DEFAULT_APT_DIR / "pine_exit_levels"
MAIN_PINE = "C3_5D_APT_reclaim_fallback_audit.pine"

FEE_BPS_RT = (0, 10, 20)
FILL_MODES = ("close_only", "conservative_intrabar")
MINUS_025R = -0.25

CANDIDATES: list[dict[str, Any]] = [
    {"id": "C1", "timeout_bars": 2, "uses_minus_025r": False},
    {"id": "C2", "timeout_bars": 3, "uses_minus_025r": False},
    {"id": "C3", "timeout_bars": 4, "uses_minus_025r": False},
    {"id": "C4", "timeout_bars": 6, "uses_minus_025r": False},
    {"id": "C5", "timeout_bars": 2, "uses_minus_025r": True},
    {"id": "C6", "timeout_bars": 3, "uses_minus_025r": True},
    {"id": "C7", "timeout_bars": 4, "uses_minus_025r": True},
    {"id": "C8", "timeout_bars": 6, "uses_minus_025r": True},
]
BASELINES = ("B0_immediate_local", "B1_effective_break", "M1_local_reclaim", "M4_r_m0.25")
CAND_IDS = [c["id"] for c in CANDIDATES]


def minus_025r_hit(
    *,
    side: int,
    entry: float,
    r_unit: float,
    high: float,
    low: float,
    close: float,
    fill_mode: str,
) -> bool:
    """Recovery target: signed pnl_r >= -0.25 (same family as prior M4)."""
    tgt_pct = (MINUS_025R * r_unit / entry) * 100.0 if entry else float("nan")
    if not math.isfinite(tgt_pct):
        return False
    return target_hit_pct(
        side=side,
        entry=entry,
        high=high,
        low=low,
        close=close,
        target_pnl_pct=tgt_pct,
        fill_mode=fill_mode,
    )


def minus_025r_exit_price(
    *,
    side: int,
    entry: float,
    r_unit: float,
    high: float,
    low: float,
    close: float,
    fill_mode: str,
) -> float:
    tgt_pct = (MINUS_025R * r_unit / entry) * 100.0
    return exit_price_for_event(
        side=side,
        high=high,
        low=low,
        close=close,
        reason="r_target",
        fill_mode=fill_mode,
        target_pnl_pct=tgt_pct,
        entry=entry,
    )


def simulate_reclaim_fallback(
    ohlc: pd.DataFrame,
    ev: PathEvents,
    *,
    name: str,
    timeout_bars: int,
    uses_minus_025r: bool,
    fill_mode: str,
) -> ExitResult:
    """Causal walk after local break.

    Timeout: local_break = bar 0; N-bar timeout exits at close of local_break+N
    (same as prior exit-management max_bars).

    Same-bar priority (close_only / conservative_intrabar close signals):
        -0.25R -> Effective Break -> Local Reclaim -> Timeout
    """
    lb = int(ev.local_break_bar)  # type: ignore[arg-type]
    end = window_end(ev)
    last = min(end, lb + int(timeout_bars))

    for bi in range(lb + 1, last + 1):
        if bi not in ohlc.index:
            continue
        h, l, c = bar_ohlc(ohlc, bi)
        is_eff = ev.effective_break_bar is not None and bi == int(ev.effective_break_bar)
        hit_m025 = uses_minus_025r and minus_025r_hit(
            side=ev.side,
            entry=ev.entry_price,
            r_unit=ev.r_unit,
            high=h,
            low=l,
            close=c,
            fill_mode=fill_mode,
        )
        hit_rec = reclaim_local(side=ev.side, close=c, local=ev.local)

        if hit_m025:
            px = minus_025r_exit_price(
                side=ev.side,
                entry=ev.entry_price,
                r_unit=ev.r_unit,
                high=h,
                low=l,
                close=c,
                fill_mode=fill_mode,
            )
            return ExitResult(
                name,
                bi,
                px,
                "minus_025r",
                pnl_pct_from_price(side=ev.side, entry=ev.entry_price, exit_px=px),
            )
        if is_eff:
            px = exit_price_for_event(
                side=ev.side, high=h, low=l, close=c, reason="effective_break", fill_mode=fill_mode
            )
            return ExitResult(
                name,
                bi,
                px,
                "effective_break",
                pnl_pct_from_price(side=ev.side, entry=ev.entry_price, exit_px=px),
            )
        if hit_rec:
            px = exit_price_for_event(
                side=ev.side, high=h, low=l, close=c, reason="local_reclaim", fill_mode=fill_mode
            )
            return ExitResult(
                name,
                bi,
                px,
                "local_reclaim",
                pnl_pct_from_price(side=ev.side, entry=ev.entry_price, exit_px=px),
            )

    bi = last if last in ohlc.index else lb
    if ev.effective_break_bar is not None and last >= int(ev.effective_break_bar):
        bi = int(ev.effective_break_bar)
        reason = "effective_break"
    elif last == lb + int(timeout_bars):
        reason = "timeout"
    else:
        reason = "horizon_end"
        if int(ev.data_end) in ohlc.index:
            bi = int(ev.data_end)
    h, l, c = bar_ohlc(ohlc, bi)
    px = exit_price_for_event(side=ev.side, high=h, low=l, close=c, reason=reason, fill_mode=fill_mode)
    return ExitResult(
        name, bi, px, reason, pnl_pct_from_price(side=ev.side, entry=ev.entry_price, exit_px=px)
    )


def all_candidate_results(
    ohlc: pd.DataFrame, ev: PathEvents, fill_mode: str
) -> dict[str, ExitResult]:
    out: dict[str, ExitResult] = {
        "B0_immediate_local": simulate_B0(ohlc, ev, fill_mode),
        "B1_effective_break": simulate_B1(ohlc, ev, fill_mode),
        "M1_local_reclaim": simulate_M1_local_reclaim(ohlc, ev, fill_mode),
        "M4_r_m0.25": simulate_M4_r_target(ohlc, ev, fill_mode, MINUS_025R),
    }
    for spec in CANDIDATES:
        out[spec["id"]] = simulate_reclaim_fallback(
            ohlc,
            ev,
            name=spec["id"],
            timeout_bars=int(spec["timeout_bars"]),
            uses_minus_025r=bool(spec["uses_minus_025r"]),
            fill_mode=fill_mode,
        )
    return out


def _bucket(signed_pct: float) -> str:
    s = float(signed_pct)
    if s >= 0:
        return "winner_at_local"
    if s > -1:
        return "0_to_-1"
    if s > -2:
        return "-1_to_-2"
    if s > -3:
        return "-2_to_-3"
    if s > -5:
        return "-3_to_-5"
    return "worse_than_-5"


def _meta_for_candidate(name: str) -> tuple[int | None, bool | None]:
    if name == "M4_r_m0.25":
        return None, True
    if name in BASELINES:
        return None, False
    for spec in CANDIDATES:
        if spec["id"] == name:
            return int(spec["timeout_bars"]), bool(spec["uses_minus_025r"])
    return None, None


def enrich_row(
    ev: PathEvents,
    ohlc: pd.DataFrame,
    res: ExitResult,
    *,
    fill_mode: str,
    fee_bps: float,
    baselines: dict[str, ExitResult],
    timeout_bars: int | None,
    uses_minus_025r: bool | None,
) -> dict[str, Any]:
    lb = int(ev.local_break_bar)  # type: ignore[arg-type]
    _, _, lc = bar_ohlc(ohlc, lb)
    local_pnl = pnl_pct_from_price(side=ev.side, entry=ev.entry_price, exit_px=lc)
    rec = scan_reclaims(ohlc, ev)
    b0 = baselines["B0_immediate_local"]
    m1 = baselines["M1_local_reclaim"]
    m4 = baselines["M4_r_m0.25"]

    net = apply_fees_pct(res.pnl_pct_gross, fee_bps)
    b0_net = apply_fees_pct(b0.pnl_pct_gross, fee_bps)
    m1_net = apply_fees_pct(m1.pnl_pct_gross, fee_bps)
    m4_net = apply_fees_pct(m4.pnl_pct_gross, fee_bps)

    imp_b0 = net - b0_net
    imp_m1 = net - m1_net
    imp_m4 = net - m4_net

    ts_local = str(ohlc.loc[lb, "timestamp"]) if "timestamp" in ohlc.columns else None
    ts_exit = (
        str(ohlc.loc[int(res.exit_bar), "timestamp"])
        if res.exit_bar is not None and "timestamp" in ohlc.columns
        else None
    )
    rec_bar = rec["local_reclaim_bar"]
    rec_before = rec_bar is not None and res.exit_bar is not None and int(rec_bar) <= int(res.exit_bar)
    delayed = ev.effective_break_bar is None or int(ev.effective_break_bar) > lb
    h24 = lb <= ev.fill_bar + 23

    return {
        "symbol": "APTUSDT",
        "direction": ev.direction,
        "setup_id": ev.setup_id,
        "carry_source_setup_id": ev.carry_source_setup_id,
        "structure_leg_id": ev.leg_id,
        "entry_bar": ev.fill_bar,
        "entry_time": str(ev.fill_timestamp),
        "entry_price": ev.entry_price,
        "local_protected_level": ev.local,
        "effective_protected_level": ev.effective,
        "local_break_bar": lb,
        "local_break_time": ts_local,
        "local_break_price": lc,
        "local_break_pnl_pct": local_pnl,
        "local_break_pnl_r": pnl_r_from_pct(local_pnl, entry=ev.entry_price, r_unit=ev.r_unit),
        "local_break_winner": local_pnl >= 0,
        "adverse_bucket_at_local": _bucket(local_pnl),
        "local_reclaim_bar": rec_bar,
        "local_reclaim_time": (
            str(ohlc.loc[int(rec_bar), "timestamp"])
            if rec_bar is not None and "timestamp" in ohlc.columns
            else None
        ),
        "local_reclaim_price": (
            float(ohlc.loc[int(rec_bar), "close"]) if rec_bar is not None else None
        ),
        "local_reclaim_before_exit": rec_before,
        "effective_break_bar": ev.effective_break_bar,
        "effective_break_time": (
            str(ohlc.loc[int(ev.effective_break_bar), "timestamp"])
            if ev.effective_break_bar is not None and "timestamp" in ohlc.columns
            else None
        ),
        "candidate": res.candidate,
        "timeout_bars": timeout_bars,
        "uses_minus_025r": uses_minus_025r,
        "exit_bar": res.exit_bar,
        "exit_time": ts_exit,
        "exit_price": res.exit_price,
        "exit_reason": res.exit_reason,
        "bars_held_after_local": (int(res.exit_bar) - lb) if res.exit_bar is not None else None,
        "gross_pnl_pct": res.pnl_pct_gross,
        "net_pnl_pct": net,
        "gross_pnl_r": pnl_r_from_pct(res.pnl_pct_gross, entry=ev.entry_price, r_unit=ev.r_unit),
        "net_pnl_r": pnl_r_from_pct(net, entry=ev.entry_price, r_unit=ev.r_unit),
        "improvement_vs_B0_pct": imp_b0,
        "improvement_vs_B0_r": pnl_r_from_pct(imp_b0, entry=ev.entry_price, r_unit=ev.r_unit),
        "improvement_vs_M1_pct": imp_m1,
        "improvement_vs_M1_r": pnl_r_from_pct(imp_m1, entry=ev.entry_price, r_unit=ev.r_unit),
        "improvement_vs_M4_pct": imp_m4,
        "improvement_vs_M4_r": pnl_r_from_pct(imp_m4, entry=ev.entry_price, r_unit=ev.r_unit),
        "better_than_B0": bool(imp_b0 > 1e-12),
        "better_than_M1": bool(imp_m1 > 1e-12),
        "better_than_M4": bool(imp_m4 > 1e-12),
        "fee_bps": fee_bps,
        "fill_semantics": fill_mode,
        "delayed_case": delayed,
        "h24_local_break": h24,
        "h24_delayed": bool(delayed and h24),
    }


def _summary_row(
    g: pd.DataFrame, *, cand: str, fee: float, mode: str, scope: str, direction: str
) -> dict[str, Any]:
    n = len(g)
    imp_b0 = g["improvement_vs_B0_pct"].astype(float)
    imp_b0_r = g["improvement_vs_B0_r"].astype(float)
    imp_m1 = g["improvement_vs_M1_pct"].astype(float)
    imp_m4 = g["improvement_vs_M4_pct"].astype(float)
    bars = g["bars_held_after_local"].dropna().astype(float)

    def _cnt(reason: str) -> int:
        return int((g["exit_reason"] == reason).sum())

    return {
        "candidate": cand,
        "scope": scope,
        "direction": direction,
        "fee_bps": fee,
        "fill_semantics": mode,
        "n": n,
        "mean_net_pnl_pct": _mean(g["net_pnl_pct"].tolist()),
        "median_net_pnl_pct": _median(g["net_pnl_pct"].tolist()),
        "mean_net_pnl_r": _mean(g["net_pnl_r"].tolist()),
        "median_net_pnl_r": _median(g["net_pnl_r"].tolist()),
        "sum_net_pnl_r": float(np.nansum(g["net_pnl_r"].astype(float))),
        "win_rate": float((g["net_pnl_pct"] > 0).sum()) / n if n else None,
        "mean_improvement_vs_B0_pct": _mean(imp_b0.tolist()),
        "median_improvement_vs_B0_pct": _median(imp_b0.tolist()),
        "mean_improvement_vs_B0_r": _mean(imp_b0_r.tolist()),
        "median_improvement_vs_B0_r": _median(imp_b0_r.tolist()),
        "better_than_B0_count": int((imp_b0 > 1e-12).sum()),
        "equal_to_B0_count": int((imp_b0.abs() <= 1e-12).sum()),
        "worse_than_B0_count": int((imp_b0 < -1e-12).sum()),
        "better_than_M1_count": int((imp_m1 > 1e-12).sum()),
        "equal_to_M1_count": int((imp_m1.abs() <= 1e-12).sum()),
        "worse_than_M1_count": int((imp_m1 < -1e-12).sum()),
        "better_than_M4_count": int((imp_m4 > 1e-12).sum()),
        "equal_to_M4_count": int((imp_m4.abs() <= 1e-12).sum()),
        "worse_than_M4_count": int((imp_m4 < -1e-12).sum()),
        "max_extra_loss_vs_B0_r": float(np.nanmin(imp_b0_r)) if n else None,
        "p90_extra_loss_vs_B0_r": _quantile(imp_b0_r.tolist(), 0.10),
        "worst_case_improvement_vs_B0_pct": float(np.nanmin(imp_b0)) if n else None,
        "mean_bars_held_after_local": _mean(bars.tolist()),
        "median_bars_held_after_local": _median(bars.tolist()),
        "p90_bars_held_after_local": _quantile(bars.tolist(), 0.90),
        "local_reclaim_exit_count": _cnt("local_reclaim"),
        "minus_025r_exit_count": _cnt("minus_025r"),
        "timeout_exit_count": _cnt("timeout"),
        "effective_break_exit_count": _cnt("effective_break"),
        "horizon_end_exit_count": _cnt("horizon_end") + _cnt("data_end"),
    }


def summarize(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    scopes = {
        "all": df,
        "long": df[df["direction"] == "long"],
        "short": df[df["direction"] == "short"],
        "h24_delayed": df[df["h24_delayed"] == True],  # noqa: E712
        "full_path_delayed": df[df["delayed_case"] == True],  # noqa: E712
        "winner_at_local": df[df["local_break_winner"] == True],  # noqa: E712
        "loser_at_local": df[df["local_break_winner"] == False],  # noqa: E712
    }
    for scope_name, base in scopes.items():
        if base.empty:
            continue
        for (cand, fee, mode), g in base.groupby(["candidate", "fee_bps", "fill_semantics"]):
            rows.append(
                _summary_row(g, cand=str(cand), fee=float(fee), mode=str(mode), scope=scope_name, direction="all")
            )
        for direction, gd in base.groupby("direction"):
            for (cand, fee, mode), g in gd.groupby(["candidate", "fee_bps", "fill_semantics"]):
                rows.append(
                    _summary_row(
                        g,
                        cand=str(cand),
                        fee=float(fee),
                        mode=str(mode),
                        scope=scope_name,
                        direction=str(direction),
                    )
                )
    return pd.DataFrame(rows)


def build_recommendation(summary: pd.DataFrame, *, n_h24: int, n_full: int) -> dict[str, Any]:
    primary = summary[
        (summary["scope"] == "h24_delayed")
        & (summary["direction"] == "all")
        & (summary["fill_semantics"] == "close_only")
        & (summary["fee_bps"] == 10)
        & (summary["candidate"].isin(CAND_IDS))
    ].copy()

    best = None
    status = "RESEARCH_ONLY"
    reason = "Small APT h24-delayed sample; no runtime change."

    if not primary.empty:
        to_map = {c["id"]: int(c["timeout_bars"]) for c in CANDIDATES}
        m025_map = {c["id"]: bool(c["uses_minus_025r"]) for c in CANDIDATES}
        primary["to"] = primary["candidate"].map(to_map)
        primary["uses_m025"] = primary["candidate"].map(m025_map)
        primary["score"] = (
            primary["median_improvement_vs_B0_pct"].fillna(0)
            + 0.25 * (primary["better_than_B0_count"] - primary["worse_than_B0_count"])
            - 0.15 * primary["median_bars_held_after_local"].fillna(99)
            - 0.05 * primary["to"]
        )
        primary = primary.sort_values(["score", "uses_m025", "to"], ascending=[False, True, True])
        best = primary.iloc[0].to_dict()

        med = best.get("median_improvement_vs_B0_pct")
        better = int(best.get("better_than_B0_count") or 0)
        worse = int(best.get("worse_than_B0_count") or 0)
        worse_m1 = int(best.get("worse_than_M1_count") or 0)
        better_m1 = int(best.get("better_than_M1_count") or 0)
        med_bars = best.get("median_bars_held_after_local")

        ls = summary[
            (summary["scope"] == "h24_delayed")
            & (summary["candidate"] == best["candidate"])
            & (summary["fill_semantics"] == "close_only")
            & (summary["fee_bps"] == 10)
            & (summary["direction"].isin(["long", "short"]))
        ]
        long_ok = short_ok = False
        for _, row in ls.iterrows():
            m = row.get("median_improvement_vs_B0_pct")
            if row["direction"] == "long" and m is not None and float(m) >= 0:
                long_ok = True
            if row["direction"] == "short" and m is not None and float(m) >= 0:
                short_ok = True

        full = summary[
            (summary["scope"] == "full_path_delayed")
            & (summary["direction"] == "all")
            & (summary["candidate"] == best["candidate"])
            & (summary["fill_semantics"] == "close_only")
            & (summary["fee_bps"] == 10)
        ]
        full_med = None if full.empty else full.iloc[0].get("median_improvement_vs_B0_pct")

        if med is None or float(med) <= 0 or better <= worse:
            status = "REJECT_RECLAIM_FALLBACK"
            reason = (
                f"{best['candidate']} does not robustly beat B0 on h24-delayed "
                f"(median_imp={med}, better={better}, worse={worse})."
            )
        elif worse_m1 > better_m1:
            status = "RESEARCH_ONLY"
            reason = (
                f"{best['candidate']} beats B0 on median but does not improve on M1 "
                f"(better_m1={better_m1}, worse_m1={worse_m1})."
            )
        elif not (long_ok and short_ok):
            status = "RESEARCH_ONLY"
            reason = (
                f"{best['candidate']} not balanced long/short "
                f"(long_ok={long_ok}, short_ok={short_ok})."
            )
        elif full_med is not None and float(full_med) < -1e-9 and float(med) > 0:
            status = "RESEARCH_ONLY"
            reason = (
                f"{best['candidate']} h24 median positive but full-path median "
                f"({full_med}) conflicts."
            )
        elif med_bars is not None and float(med_bars) > 6:
            status = "RESEARCH_ONLY"
            reason = f"{best['candidate']} hold duration elevated (median_bars={med_bars})."
        elif (
            better > worse
            and float(med) > 0
            and long_ok
            and short_ok
            and better_m1 >= worse_m1
            and int(best.get("equal_to_M1_count") or 0) + better_m1 >= n_h24 * 0.7
        ):
            # Timeout-capped reclaim may match M1 often; PROMISING only if it is at least
            # as good as M1 on the majority of cases and not worse on net better/worse.
            status = "PROMISING_FOR_MULTI_SYMBOL_VALIDATION"
            reason = (
                f"{best['candidate']} improves median vs B0, holds short, and is "
                "competitive with M1 — validate multi-symbol."
            )
        else:
            status = "RESEARCH_ONLY"
            reason = (
                f"{best['candidate']} is the simplest short-timeout reclaim variant that "
                "beats B0 on median, but does not robustly beat M1 on APT h24 (n=10)."
            )

    return {
        "recommended_status": status,
        "best_research_candidate": None if best is None else best.get("candidate"),
        "best_candidate_stats": best,
        "runtime_change_recommended": False,
        "recommended_for_multi_symbol_validation": status == "PROMISING_FOR_MULTI_SYMBOL_VALIDATION",
        "reason": reason,
        "h24_sample_size": n_h24,
        "full_path_sample_size": n_full,
        "sample_size_warning": True,
        "v1lag_semantics_unchanged": True,
        "historical_maxmin_chain_used": False,
        "uses_future_information": False,
        "timeout_semantics": "local_break=bar0; N-bar timeout exits at close of local_break+N",
        "minus_025r_semantics": "recovery target pnl_r >= -0.25 (matches prior M4)",
        "same_bar_priority_close_only": "minus_025r > effective_break > local_reclaim > timeout",
        "phase": PHASE,
    }


def build_pine(h24: pd.DataFrame, *, candidate: str = "C2") -> str:
    if h24 is None or h24.empty or "candidate" not in h24.columns:
        lines = [
            *build_pine_header("C3.5D Reclaim Fallback Audit"),
            "// RESEARCH ONLY — empty set.",
            "plot(na, 'setup_id', display=display.data_window)",
            "",
        ]
        text = "\n".join(lines) + "\n"
        validate_pine_script(text)
        return text

    sub = h24[
        (h24["candidate"] == candidate)
        & (h24["fill_semantics"] == "close_only")
        & (h24["fee_bps"] == 10)
    ].drop_duplicates("setup_id").sort_values("entry_bar")
    if sub.empty:
        sub = h24.drop_duplicates("setup_id").head(10)
    if sub.empty:
        lines = [
            *build_pine_header("C3.5D Reclaim Fallback Audit"),
            "// RESEARCH ONLY — empty set.",
            "plot(na, 'setup_id', display=display.data_window)",
            "",
        ]
        text = "\n".join(lines) + "\n"
        validate_pine_script(text)
        return text

    n = len(sub)
    m025 = []
    for _, r in sub.iterrows():
        entry = float(r["entry_price"])
        local = float(r["local_protected_level"])
        ru = abs(entry - local)
        if ru <= 1e-12:
            m025.append(float("nan"))
            continue
        if str(r["direction"]) == "long":
            m025.append(entry - 0.25 * ru)
        else:
            m025.append(entry + 0.25 * ru)

    timeout_bars = 3
    if "timeout_bars" in sub.columns and pd.notna(sub.iloc[0]["timeout_bars"]):
        timeout_bars = int(sub.iloc[0]["timeout_bars"])

    lines = [
        *build_pine_header("C3.5D Reclaim Fallback Audit"),
        f"// RESEARCH ONLY — candidate {candidate}. V_1LAG unchanged. No live orders.",
        f"nSetups = {n}",
        f'maxVisible = input.int({min(n, 10)}, "Max visible", minval=1, maxval={max(n, 1)})',
        'lineHorizonBars = input.int(24, "Line bars", minval=4, maxval=100)',
        'showLocal = input.bool(true, "LOCAL")',
        'showEffective = input.bool(true, "EFFECTIVE")',
        'showM025 = input.bool(true, "-0.25R")',
        'showMarkers = input.bool(true, "Markers")',
        "",
        f"setupIds = array.from({', '.join(_pine_int(x) for x in sub['setup_id'])})",
        f"srcIds = array.from({', '.join(_pine_int(x) for x in sub['carry_source_setup_id'])})",
        f"sides = array.from({', '.join(_pine_int(1 if d == 'long' else -1) for d in sub['direction'])})",
        f"fillTimes = array.from({', '.join(_ts(x) for x in sub['entry_time'])})",
        f"entryPx = array.from({', '.join(_pine_float(x) for x in sub['entry_price'])})",
        f"localProt = array.from({', '.join(_pine_float(x) for x in sub['local_protected_level'])})",
        f"effProt = array.from({', '.join(_pine_float(x) for x in sub['effective_protected_level'])})",
        f"m025Px = array.from({', '.join(_pine_float(x) for x in m025)})",
        f"localBrSince = array.from({', '.join(_pine_int(_bars_since(r.entry_bar, r.local_break_bar)) for _, r in sub.iterrows())})",
        f"recSince = array.from({', '.join(_pine_int(_bars_since(r.entry_bar, r.local_reclaim_bar)) for _, r in sub.iterrows())})",
        f"hasRec = array.from({', '.join('1' if pd.notna(r.local_reclaim_bar) else '0' for _, r in sub.iterrows())})",
        f"effSince = array.from({', '.join(_pine_int(_bars_since(r.entry_bar, r.effective_break_bar)) for _, r in sub.iterrows())})",
        f"exitSince = array.from({', '.join(_pine_int(_bars_since(r.entry_bar, r.exit_bar)) for _, r in sub.iterrows())})",
        f"barsHeld = array.from({', '.join(_pine_int(x) for x in sub['bars_held_after_local'])})",
        f"pnlNet = array.from({', '.join(_pine_float(x) for x in sub['net_pnl_pct'])})",
        f"timeoutBars = {timeout_bars}",
        "",
        "var line[] locL = array.new_line()",
        "var line[] effL = array.new_line()",
        "var line[] m025L = array.new_line()",
        "var label[] labs = array.new_label()",
        "var bool drawn = false",
        "barMs = timeframe.in_seconds() * 1000",
        "clearAll() =>",
        "    if array.size(locL) > 0",
        "        for j = 0 to array.size(locL) - 1",
        "            line.delete(array.get(locL, j))",
        "        array.clear(locL)",
        "    if array.size(effL) > 0",
        "        for j = 0 to array.size(effL) - 1",
        "            line.delete(array.get(effL, j))",
        "        array.clear(effL)",
        "    if array.size(m025L) > 0",
        "        for j = 0 to array.size(m025L) - 1",
        "            line.delete(array.get(m025L, j))",
        "        array.clear(m025L)",
        "    if array.size(labs) > 0",
        "        for j = 0 to array.size(labs) - 1",
        "            label.delete(array.get(labs, j))",
        "        array.clear(labs)",
        "",
        "drawSetup(i) =>",
        "    t0 = array.get(fillTimes, i)",
        "    t1 = t0 + lineHorizonBars * barMs",
        "    ep = array.get(entryPx, i)",
        "    loc = array.get(localProt, i)",
        "    eff = array.get(effProt, i)",
        "    sid = array.get(setupIds, i)",
        "    side = array.get(sides, i)",
        "    array.push(labs, label.new(t0, ep, (side > 0 ? 'LONG #' : 'SHORT #') + str.tostring(sid), xloc=xloc.bar_time, style=side > 0 ? label.style_label_up : label.style_label_down, color=side > 0 ? color.teal : color.fuchsia, textcolor=color.white, size=size.small))",
        "    if showLocal",
        "        array.push(locL, line.new(t0, loc, t1, loc, xloc=xloc.bar_time, color=color.red, width=1))",
        "    if showEffective",
        "        array.push(effL, line.new(t0, eff, t1, eff, xloc=xloc.bar_time, color=color.maroon, width=2))",
        "        array.push(labs, label.new(t0, eff, 'EFF from #' + str.tostring(array.get(srcIds, i)), xloc=xloc.bar_time, style=label.style_label_left, color=color.maroon, textcolor=color.white, size=size.tiny))",
        "    if showM025",
        "        array.push(m025L, line.new(t0, array.get(m025Px, i), t1, array.get(m025Px, i), xloc=xloc.bar_time, color=color.purple, width=1, style=line.style_dotted))",
        "    if showMarkers",
        "        tlb = t0 + array.get(localBrSince, i) * barMs",
        "        array.push(labs, label.new(tlb, loc, 'LOCAL BREAK', xloc=xloc.bar_time, style=label.style_label_down, color=color.red, textcolor=color.white, size=size.tiny))",
        "        tto = tlb + timeoutBars * barMs",
        "        array.push(labs, label.new(tto, loc, 'TIMEOUT', xloc=xloc.bar_time, style=label.style_label_down, color=color.gray, textcolor=color.white, size=size.tiny))",
        "        if array.get(hasRec, i) == 1",
        "            tr = t0 + array.get(recSince, i) * barMs",
        "            array.push(labs, label.new(tr, loc, 'LOCAL RECLAIM', xloc=xloc.bar_time, style=label.style_label_up, color=color.orange, textcolor=color.black, size=size.tiny))",
        "        te = t0 + array.get(effSince, i) * barMs",
        "        array.push(labs, label.new(te, eff, 'EFFECTIVE BREAK', xloc=xloc.bar_time, style=label.style_label_down, color=color.maroon, textcolor=color.white, size=size.tiny))",
        "        tx = t0 + array.get(exitSince, i) * barMs",
        "        array.push(labs, label.new(tx, ep, 'EXIT ' + str.tostring(array.get(pnlNet, i), '#.##') + '% / +' + str.tostring(array.get(barsHeld, i)) + 'b', xloc=xloc.bar_time, style=label.style_label_up, color=color.blue, textcolor=color.white, size=size.tiny))",
        "",
        "if barstate.islastconfirmedhistory",
        "    if not drawn",
        "        clearAll()",
        "        startIdx = math.max(0, nSetups - maxVisible)",
        "        if startIdx <= nSetups - 1",
        "            for i = startIdx to nSetups - 1",
        "                drawSetup(i)",
        "        drawn := true",
        "",
        "plot(array.get(setupIds, nSetups - 1), 'setup_id', display=display.data_window)",
        "plot(array.get(srcIds, nSetups - 1), 'carry_source_setup_id', display=display.data_window)",
        "plot(array.get(localProt, nSetups - 1), 'local_protected', display=display.data_window)",
        "plot(array.get(effProt, nSetups - 1), 'effective_protected', display=display.data_window)",
        "plot(array.get(m025Px, nSetups - 1), 'minus_025r_level', display=display.data_window)",
        "plot(array.get(barsHeld, nSetups - 1), 'bars_after_local', display=display.data_window)",
        "plot(array.get(pnlNet, nSetups - 1), 'exit_pnl_net_pct', display=display.data_window)",
        "",
    ]
    text = "\n".join(lines) + "\n"
    validate_pine_script(text)
    return text


def run_audit(
    *,
    apt_dir: Path = DEFAULT_APT_DIR,
    output_dir: Path = DEFAULT_OUT,
    pine_dir: Path = PINE_DIR,
    frame: pd.DataFrame | None = None,
) -> dict[str, Any]:
    apt_dir = Path(apt_dir)
    output_dir = Path(output_dir)
    pine_dir = Path(pine_dir)

    forbidden = {
        (apt_dir / "protected_carry").resolve(),
        (apt_dir / "protected_break_path").resolve(),
        (apt_dir / "protected_break_exit_management").resolve(),
        apt_dir.resolve(),
    }
    if output_dir.resolve() in forbidden:
        raise RuntimeError(f"refusing to write into protected sibling/root: {output_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)
    pine_dir.mkdir(parents=True, exist_ok=True)

    fills = pd.read_csv(apt_dir / "fills.csv")
    if frame is None:
        frame, _, meta = build_apt_d1_frame()
    else:
        meta = {}
    ohlc = ensure_ohlc(frame)
    setups = load_setups(fills)
    assign_effective_levels(setups, ohlc)

    events: list[PathEvents] = []
    for s in setups:
        ev = build_path_events(s, ohlc)
        if ev is not None:
            events.append(ev)

    per_rows: list[dict[str, Any]] = []
    for ev in events:
        for fill_mode in FILL_MODES:
            results = all_candidate_results(ohlc, ev, fill_mode)
            baselines = {k: results[k] for k in BASELINES}
            for fee in FEE_BPS_RT:
                for name, res in results.items():
                    tb, um = _meta_for_candidate(name)
                    per_rows.append(
                        enrich_row(
                            ev,
                            ohlc,
                            res,
                            fill_mode=fill_mode,
                            fee_bps=float(fee),
                            baselines=baselines,
                            timeout_bars=tb,
                            uses_minus_025r=um,
                        )
                    )

    per = pd.DataFrame(per_rows)
    per = per.sort_values(["setup_id", "candidate", "fill_semantics", "fee_bps"]).reset_index(drop=True)
    summary = summarize(per)

    primary = per[
        (per["fill_semantics"] == "close_only")
        & (per["fee_bps"] == 10)
        & (per["delayed_case"] == True)  # noqa: E712
    ]
    h24 = primary[primary["h24_delayed"] == True].copy()  # noqa: E712
    full = primary.copy()

    def _cmp(df: pd.DataFrame, vs: str) -> pd.DataFrame:
        better_col = {"B0": "better_than_B0", "M1": "better_than_M1", "M4": "better_than_M4"}[vs]
        use = [
            "setup_id",
            "direction",
            "candidate",
            "exit_reason",
            "bars_held_after_local",
            "net_pnl_pct",
            f"improvement_vs_{vs}_pct",
            f"improvement_vs_{vs}_r",
            better_col,
            "h24_delayed",
            "fill_semantics",
            "fee_bps",
        ]
        return df[use].sort_values(["candidate", "setup_id"]).reset_index(drop=True)

    n_h24 = int(h24["setup_id"].nunique()) if not h24.empty else 0
    n_full = int(full["setup_id"].nunique()) if not full.empty else 0
    rec = build_recommendation(summary, n_h24=n_h24, n_full=n_full)
    best_c = rec.get("best_research_candidate") or "C2"

    ls_cmp = summary[
        (summary["scope"].isin(["h24_delayed", "full_path_delayed"]))
        & (summary["direction"].isin(["long", "short"]))
        & (summary["fill_semantics"] == "close_only")
        & (summary["fee_bps"] == 10)
    ].copy()

    exit_dist = (
        primary.groupby(["candidate", "exit_reason"]).size().reset_index(name="count")
        .sort_values(["candidate", "exit_reason"])
    )

    tmat = summary[
        (summary["scope"] == "h24_delayed")
        & (summary["direction"] == "all")
        & (summary["fill_semantics"] == "close_only")
        & (summary["fee_bps"] == 10)
        & (summary["candidate"].isin(CAND_IDS))
    ].copy()
    tmat["timeout_bars"] = tmat["candidate"].map(lambda c: next(int(s["timeout_bars"]) for s in CANDIDATES if s["id"] == c))
    tmat["uses_minus_025r"] = tmat["candidate"].map(
        lambda c: next(bool(s["uses_minus_025r"]) for s in CANDIDATES if s["id"] == c)
    )

    fee_sens = summary[
        (summary["scope"] == "h24_delayed")
        & (summary["direction"] == "all")
        & (summary["candidate"].isin(list(CAND_IDS) + list(BASELINES)))
    ][
        [
            "candidate",
            "fee_bps",
            "fill_semantics",
            "n",
            "median_improvement_vs_B0_pct",
            "mean_improvement_vs_B0_pct",
            "better_than_B0_count",
            "worse_than_B0_count",
            "median_bars_held_after_local",
            "max_extra_loss_vs_B0_r",
        ]
    ].sort_values(["candidate", "fill_semantics", "fee_bps"])

    tail_parts = []
    for cand in [best_c, "C1", "C2", "C5", "C6", "M1_local_reclaim", "M4_r_m0.25", "B1_effective_break"]:
        g = h24[h24["candidate"] == cand]
        if g.empty:
            continue
        best5 = g.nlargest(5, "improvement_vs_B0_pct").assign(tail_kind="best")
        worst5 = g.nsmallest(5, "improvement_vs_B0_pct").assign(tail_kind="worst")
        tail_parts.append(pd.concat([best5, worst5], ignore_index=True))
    tail = pd.concat(tail_parts, ignore_index=True) if tail_parts else pd.DataFrame()

    per.to_csv(output_dir / "reclaim_fallback_per_fill.csv", index=False)
    summary.to_csv(output_dir / "reclaim_fallback_summary.csv", index=False)
    _cmp(primary, "B0").to_csv(output_dir / "comparison_vs_b0.csv", index=False)
    _cmp(primary, "M1").to_csv(output_dir / "comparison_vs_m1.csv", index=False)
    _cmp(primary, "M4").to_csv(output_dir / "comparison_vs_m4.csv", index=False)
    h24[
        [
            "setup_id",
            "direction",
            "candidate",
            "exit_reason",
            "bars_held_after_local",
            "net_pnl_pct",
            "improvement_vs_B0_pct",
            "improvement_vs_M1_pct",
            "improvement_vs_M4_pct",
            "better_than_B0",
            "better_than_M1",
            "better_than_M4",
        ]
    ].sort_values(["candidate", "setup_id"]).to_csv(output_dir / "h24_delayed_comparison.csv", index=False)
    full[
        [
            "setup_id",
            "direction",
            "candidate",
            "exit_reason",
            "bars_held_after_local",
            "net_pnl_pct",
            "improvement_vs_B0_pct",
            "improvement_vs_M1_pct",
            "improvement_vs_M4_pct",
        ]
    ].sort_values(["candidate", "setup_id"]).to_csv(
        output_dir / "full_path_delayed_comparison.csv", index=False
    )
    ls_cmp.to_csv(output_dir / "long_short_comparison.csv", index=False)
    exit_dist.to_csv(output_dir / "exit_reason_distribution.csv", index=False)
    tail.to_csv(output_dir / "tail_risk_cases.csv", index=False)
    tmat.to_csv(output_dir / "timeout_matrix.csv", index=False)
    fee_sens.to_csv(output_dir / "fee_fill_sensitivity.csv", index=False)
    (output_dir / "recommendation.json").write_text(
        json.dumps(json_safe(rec), indent=2) + "\n", encoding="utf-8"
    )

    pine = build_pine(h24 if not h24.empty else primary, candidate=str(best_c))
    pine_path = pine_dir / MAIN_PINE
    pine_path.write_text(pine, encoding="utf-8")

    readme = "\n".join(
        [
            "# C3.5D Reclaim-Fallback Audit",
            "",
            "## Fragestellung",
            "Ist Local-Reclaim + kurzer Timeout (+ optional -0.25R) robuster als B0/B1/M1/M4?",
            "",
            "## Timeout-Semantik",
            "`local_break = bar 0`; N-Bar-Timeout = Exit am Close von `local_break + N`.",
            "",
            "## Same-Bar Prioritaet (close_only)",
            "`-0.25R -> Effective Break -> Local Reclaim -> Timeout`",
            "",
            "## -0.25R",
            "Recovery-Target `pnl_r >= -0.25` (wie M4 im Exit-Management-Audit).",
            "",
            f"- h24-delayed: `{n_h24}`",
            f"- full-path-delayed: `{n_full}`",
            f"- Status: `{rec['recommended_status']}` / best `{rec.get('best_research_candidate')}`",
            "",
            "Keine Runtime-Aenderung. V_1LAG unveraendert. Kein Commit.",
            "",
        ]
    )
    (output_dir / "README.md").write_text(readme + "\n", encoding="utf-8")

    audit = {
        "phase": PHASE,
        "status": "OK",
        "n_local_break_fills": len(events),
        "n_h24_delayed": n_h24,
        "n_full_path_delayed": n_full,
        "recommendation": rec,
        "pine_path": str(pine_path),
        "output_dir": str(output_dir),
        "candidates": CANDIDATES,
        "baselines": list(BASELINES),
        "v1lag_semantics_unchanged": True,
        "historical_maxmin_chain_used": False,
        "uses_future_information": False,
        "runtime_change_recommended": False,
        "no_commit": True,
        "parent_dirs_not_overwritten": [
            "protected_carry",
            "protected_break_path",
            "protected_break_exit_management",
        ],
        "data_meta": {k: meta[k] for k in meta if k != "frame15_meta"} if meta else {},
    }
    (output_dir / "audit_summary.json").write_text(
        json.dumps(json_safe(audit), indent=2) + "\n", encoding="utf-8"
    )
    return audit


def main() -> None:
    p = argparse.ArgumentParser(description="C3.5D reclaim-fallback audit")
    p.add_argument("--apt-dir", type=Path, default=DEFAULT_APT_DIR)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--pine-dir", type=Path, default=PINE_DIR)
    args = p.parse_args()
    audit = run_audit(apt_dir=args.apt_dir, output_dir=args.output_dir, pine_dir=args.pine_dir)
    print(json.dumps(json_safe(audit), indent=2))


if __name__ == "__main__":
    main()
