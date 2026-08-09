"""Orchestrate multi-coin overlap / idle-fill audit."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from orderbook_analyse.fractal_wave_fade_multicoin_overlap import (
    AUDIT_VERSION,
    OUT_DIR_DEFAULT,
)
from orderbook_analyse.fractal_wave_fade_multicoin_overlap.blocked import (
    blocked_direction_mix,
    blocked_hold_buckets,
)
from orderbook_analyse.fractal_wave_fade_multicoin_overlap.capital import (
    m1_single,
    m2_shared,
    m3_parallel_scaled,
    parallel_occupancy,
)
from orderbook_analyse.fractal_wave_fade_multicoin_overlap.correlation import signal_correlation
from orderbook_analyse.fractal_wave_fade_multicoin_overlap.data import (
    build_independent_trades,
    document_sources,
    load_common_window,
    load_global_trades,
    split_by_symbol,
)
from orderbook_analyse.fractal_wave_fade_multicoin_overlap.intervals import (
    entry_during_other_active,
    idle_fill_stats,
    near_sim_buckets,
    near_simultaneous,
    single_coin_stats,
    timeline_state_stats,
)
from orderbook_analyse.fractal_wave_fade_multicoin_overlap.scheduler import (
    parallel_all,
    schedule_shared_slot,
)


def _decide(payload: dict[str, Any]) -> dict[str, Any]:
    fill_a = payload["idle_fill_apt_by_doge"]["idle_fill_ratio"] or 0.0
    fill_d = payload["idle_fill_doge_by_apt"]["idle_fill_ratio"] or 0.0
    both_pct = payload["timeline"]["pct_both_active"]
    flat_pct = payload["timeline"]["pct_both_flat"]
    block_rate = payload["shared_apt_first"]["block_rate"] or 0.0
    m1 = payload["capital"]["M1_SINGLE_APT"]
    m2 = payload["capital"]["M2_SHARED_SLOT_APT_FIRST"]
    m3 = payload["capital"]["M3_PARALLEL_50_50"]
    m3_unscaled = m3.get("unscaled_net_return") or 0.0

    # efficiency: shared vs APT
    m2_vs_m1_pnl = (m2["net_return_additive"] / m1["net_return_additive"] - 1.0) if m1["net_return_additive"] else None
    m2_vs_m1_day = None
    if m1.get("pnl_per_day") and m2.get("pnl_per_day"):
        m2_vs_m1_day = m2["pnl_per_day"] / m1["pnl_per_day"] - 1.0

    # unscaled parallel vs scaled: if unscaled >> scaled, profit mostly from more capital
    leverage_illusion = (m3_unscaled / m3["net_return_additive"] - 1.0) if m3["net_return_additive"] else None

    coinc60 = None
    sc = payload["signal_corr"]
    if len(sc):
        row = sc[sc["bucket_minutes"] == 60]
        if len(row):
            coinc60 = float(row.iloc[0]["apt_coincidence_pct"])

    reasons = []
    if fill_a >= 0.45 and both_pct <= 25 and (m2_vs_m1_pnl or 0) > 0.15:
        decision = "SECOND_COIN_STRONGLY_IMPROVES_UTILIZATION"
        reasons.append("high idle fill, moderate overlap, shared-slot PnL clearly above APT-only")
    elif fill_a >= 0.25 and both_pct <= 40 and (m2_vs_m1_pnl or 0) > 0.05:
        decision = "SECOND_COIN_MODERATELY_IMPROVES_UTILIZATION"
        reasons.append("material idle fill with remaining conflicts; shared slot still helps")
    elif both_pct >= 35 or (coinc60 or 0) >= 55 and fill_a < 0.25:
        decision = "SECOND_COIN_MOSTLY_OVERLAPS_PRIMARY"
        reasons.append("large simultaneous activity / coincidence, little unique idle fill")
    elif (m3_unscaled > m1["net_return_additive"] * 1.3) and (m3["net_return_additive"] <= m2["net_return_additive"] * 1.05):
        decision = "SECOND_COIN_ADDS_TRADES_BUT_NOT_CAPITAL_EFFICIENCY"
        reasons.append("unscaled parallel looks better mainly via dual notional")
    elif fill_a < 0.15 and (m2_vs_m1_pnl or 0) < 0.02:
        decision = "SECOND_COIN_DOES_NOT_ADD_VALUE"
        reasons.append("little fill and no capital-efficient gain")
    else:
        # default moderate if any positive fill + shared gain
        if fill_a >= 0.2 and (m2_vs_m1_pnl or 0) >= 0:
            decision = "SECOND_COIN_MODERATELY_IMPROVES_UTILIZATION"
            reasons.append("default: meaningful fill with non-negative shared-slot edge")
        else:
            decision = "SECOND_COIN_MOSTLY_OVERLAPS_PRIMARY"
            reasons.append("default: limited capital-efficient improvement")

    return {
        "decision": decision,
        "reasons": reasons,
        "fill_apt_idle_by_doge": fill_a,
        "fill_doge_idle_by_apt": fill_d,
        "pct_both_active": both_pct,
        "pct_both_flat": flat_pct,
        "shared_block_rate": block_rate,
        "m2_vs_m1_pnl_pct": (m2_vs_m1_pnl * 100.0) if m2_vs_m1_pnl is not None else None,
        "m2_vs_m1_pnl_per_day_pct": (m2_vs_m1_day * 100.0) if m2_vs_m1_day is not None else None,
        "parallel_unscaled_vs_scaled_premium_pct": (leverage_illusion * 100.0) if leverage_illusion is not None else None,
        "apt_coincidence_60m_pct": coinc60,
    }


def run_analysis(*, out_dir: Path = OUT_DIR_DEFAULT, force_rebuild: bool = False) -> dict[str, Any]:
    span_start, span_end = load_common_window()
    independent = build_independent_trades(force=force_rebuild)
    by = split_by_symbol(independent)
    apt, doge = by["APTUSDT"], by["DOGEUSDT"]
    global_df = load_global_trades()
    sources = document_sources(independent, global_df)

    print("[stats] single-coin baselines …", flush=True)
    single_rows = [
        single_coin_stats(apt, label="APT_INDEPENDENT", span_start=span_start, span_end=span_end),
        single_coin_stats(doge, label="DOGE_INDEPENDENT", span_start=span_start, span_end=span_end),
        single_coin_stats(global_df, label="GLOBAL_SHARED_REFERENCE", span_start=span_start, span_end=span_end),
    ]

    print("[timeline] overlap states …", flush=True)
    timeline = timeline_state_stats(apt, doge, span_start=span_start, span_end=span_end)
    occ = parallel_occupancy(apt, doge, span_start=span_start, span_end=span_end)

    fill_ad = idle_fill_stats(
        apt, doge, primary_label="APTUSDT", secondary_label="DOGEUSDT",
        span_start=span_start, span_end=span_end,
    )
    fill_da = idle_fill_stats(
        doge, apt, primary_label="DOGEUSDT", secondary_label="APTUSDT",
        span_start=span_start, span_end=span_end,
    )

    doge_entries_while_apt = entry_during_other_active(doge, apt)
    apt_entries_while_doge = entry_during_other_active(apt, doge)

    near = near_simultaneous(apt, doge)
    near_b = near_sim_buckets(near)

    print("[sched] shared slot APT_FIRST / DOGE_FIRST …", flush=True)
    shared_a = schedule_shared_slot(independent, tie_break="APT_FIRST")
    shared_d = schedule_shared_slot(independent, tie_break="DOGE_FIRST")
    parallel = parallel_all(independent)

    # compare shared to global reference count
    shared_vs_global = {
        "global_trades": int(len(global_df)),
        "shared_apt_first_executed": shared_a["executed"],
        "shared_doge_first_executed": shared_d["executed"],
        "note": (
            "Shared-slot of independent trades approximates global max-1; "
            "counts may differ slightly due to event/cluster sequencing vs trade-list scheduling."
        ),
    }

    print("[capital] M1/M2/M3 …", flush=True)
    capital = {
        "M1_SINGLE_APT": m1_single(apt, span_start, span_end, model="M1_SINGLE_APT"),
        "M1_SINGLE_DOGE": m1_single(doge, span_start, span_end, model="M1_SINGLE_DOGE"),
        "M2_SHARED_SLOT_APT_FIRST": m2_shared(shared_a["executed_df"], span_start, span_end),
        "M2_SHARED_SLOT_DOGE_FIRST": m2_shared(shared_d["executed_df"], span_start, span_end),
        "M3_PARALLEL_50_50": m3_parallel_scaled(independent, span_start=span_start, span_end=span_end),
        "M3_PARALLEL_UNSCALED_DUAL_NOTIONAL": {
            "model": "M3_UNSCALED_ORACLE_DUAL_NOTIONAL",
            "warning": "Uses 2× capital when both open — NOT fair vs 100% base",
            "net_return_additive": parallel["net_pnl_additive"],
            "executed_trades": parallel["executed"],
        },
    }

    corr = signal_correlation(apt, doge)
    blk_buckets = blocked_hold_buckets(shared_a["blocked_df"])
    blk_mix = blocked_direction_mix(shared_a["blocked_df"])

    # extras vs single
    extra_vs_apt = shared_a["executed"] - len(apt)
    extra_vs_doge = shared_a["executed"] - len(doge)

    payload: dict[str, Any] = {
        "audit_version": AUDIT_VERSION,
        "span_start": span_start,
        "span_end": span_end,
        "sources": sources,
        "single_coin_stats": pd.DataFrame(single_rows),
        "timeline": timeline,
        "occupancy": occ,
        "idle_fill_apt_by_doge": fill_ad,
        "idle_fill_doge_by_apt": fill_da,
        "entry_overlap_counts": {
            "doge_entries_while_apt_active": doge_entries_while_apt,
            "apt_entries_while_doge_active": apt_entries_while_doge,
        },
        "near_simultaneous": near,
        "near_sim_buckets": near_b,
        "shared_apt_first": {k: v for k, v in shared_a.items() if k not in ("executed_df", "blocked_df")},
        "shared_doge_first": {k: v for k, v in shared_d.items() if k not in ("executed_df", "blocked_df")},
        "shared_executed_apt_first": shared_a["executed_df"],
        "shared_blocked_apt_first": shared_a["blocked_df"],
        "shared_executed_doge_first": shared_d["executed_df"],
        "shared_blocked_doge_first": shared_d["blocked_df"],
        "parallel": {k: v for k, v in parallel.items() if k != "executed_df"},
        "shared_vs_global": shared_vs_global,
        "capital": capital,
        "signal_corr": corr,
        "blocked_buckets": blk_buckets,
        "blocked_mix": blk_mix,
        "extra_trades_shared_vs_apt": extra_vs_apt,
        "extra_trades_shared_vs_doge": extra_vs_doge,
        "independent": independent,
        "out_dir": out_dir,
    }
    # strip non-serializable from capital m3
    if "scaled_trades_df" in capital["M3_PARALLEL_50_50"]:
        payload["m3_scaled_trades"] = capital["M3_PARALLEL_50_50"].pop("scaled_trades_df")

    payload["decision"] = _decide(payload)
    return payload
