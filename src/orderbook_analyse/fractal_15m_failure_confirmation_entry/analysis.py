"""Orchestrate post-confirmation entry timing analysis."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_15m_failure_confirmation_entry import (
    AUDIT_VERSION,
    ENTRY_DELAYS_MIN,
    ENTRY_PRICE_DOC,
    METHOD_DOC,
    MIN_SAMPLE,
    SYMBOL,
)
from orderbook_analyse.fractal_15m_failure_confirmation_entry.engine import (
    attach_micro_at_entry,
    build_delay_entries,
    first_touch_analysis,
    metrics,
    pullback_entries,
    wait_for_micro_realign,
)
from orderbook_analyse.fractal_15m_failure_confirmation_entry.events import (
    load_1m_ohlcv,
    load_confirmation_events,
    load_micro_waves,
)


def decide_primary(
    decay_rows: list[dict],
    micro_wait_rows: list[dict],
) -> str:
    """
    IMMEDIATE_BEST: T0 med60 >= all delays within 0.02 and hit60 competitive.
    SHORT_DELAY_IMPROVES: some delay in 1..5 improves med60 by >=0.03 and hit by >=1pp vs T0
      on BOTH sides (or combined with both sides non-worse).
    MICRO_REALIGN_IMPROVES: B/C/D beats A on med60+hit60 with fill rate >=50%.
    EDGE_DECAYS_TOO_FAST: T0 ok but by T+10 med60<=0 or hit drops sharply.
    NO_ROBUST: otherwise.
    """
    by_delay = {}
    for r in decay_rows:
        if r.get("side") != "COMBINED":
            continue
        by_delay[int(r["delay_min"])] = r
    if 0 not in by_delay:
        return "NO_ROBUST_POST_CONFIRMATION_ENTRY"
    t0 = by_delay[0]
    t0_med = t0.get("median_dir_ret_60m") or 0
    t0_hit = t0.get("hit_rate_60m") or 0

    # micro wait
    wait_by = {r["strategy"]: r for r in micro_wait_rows if r.get("side") == "COMBINED"}
    a = wait_by.get("A_immediate")
    micro_win = False
    if a and a.get("n", 0) >= MIN_SAMPLE:
        for key in ("B_wait_1m_realign", "C_wait_5m_realign", "D_wait_1m_and_5m"):
            b = wait_by.get(key)
            if not b or b.get("n", 0) < MIN_SAMPLE:
                continue
            fill = b.get("fill_rate")
            if fill is not None and fill < 0.5:
                continue
            if (b.get("hit_rate_60m") or 0) > (a.get("hit_rate_60m") or 0) + 0.01 and (
                b.get("median_dir_ret_60m") or 0
            ) > (a.get("median_dir_ret_60m") or 0) + 0.02:
                micro_win = True
                break

    delay_win = False
    for d in (1, 2, 3, 5):
        r = by_delay.get(d)
        if not r or r.get("n", 0) < MIN_SAMPLE:
            continue
        if (r.get("median_dir_ret_60m") or 0) >= t0_med + 0.03 and (
            r.get("hit_rate_60m") or 0
        ) >= t0_hit + 0.01:
            delay_win = True
            break

    t10 = by_delay.get(10)
    decays_fast = False
    if t10 and t0_med > 0:
        if (t10.get("median_dir_ret_60m") or 0) <= 0 or (
            (t0_hit - (t10.get("hit_rate_60m") or 0)) >= 0.05
            and (t10.get("median_dir_ret_60m") or 0) < t0_med - 0.05
        ):
            decays_fast = True

    if micro_win:
        return "MICRO_REALIGNMENT_IMPROVES_ENTRY"
    if delay_win:
        return "SHORT_POST_CONFIRMATION_DELAY_IMPROVES_ENTRY"
    if t0_med > 0 and t0_hit >= 0.52:
        if decays_fast:
            # still immediate may be best among delays
            better_exists = any(
                (by_delay[d].get("median_dir_ret_60m") or -999) > t0_med + 0.01
                for d in ENTRY_DELAYS_MIN
                if d != 0 and d in by_delay
            )
            if not better_exists:
                return "IMMEDIATE_FAILURE_CONFIRMATION_ENTRY_BEST"
            return "FAILURE_CONFIRMATION_EDGE_DECAYS_TOO_FAST"
        # check immediate is best or near-best
        max_other = max(
            (by_delay[d].get("median_dir_ret_60m") or -999)
            for d in ENTRY_DELAYS_MIN
            if d != 0 and d in by_delay
        )
        if t0_med >= max_other - 0.02:
            return "IMMEDIATE_FAILURE_CONFIRMATION_ENTRY_BEST"
        return "SHORT_POST_CONFIRMATION_DELAY_IMPROVES_ENTRY"
    if decays_fast:
        return "FAILURE_CONFIRMATION_EDGE_DECAYS_TOO_FAST"
    return "NO_ROBUST_POST_CONFIRMATION_ENTRY"


def decide_pullback(pb_summary: list[dict]) -> str:
    """
    ADDS_VALUE: some bucket fill>=40%, med60 >= imm med60 + 0.03, and opportunity-adjusted
      (fill*med60) >= 0.8 * imm_med60.
    COSTS_TOO_MANY: better med when filled but fill low / opp-adjusted worse.
    NO_CLEAR: otherwise.
    """
    imm = [r for r in pb_summary if r.get("bucket") == "IMMEDIATE_T0"]
    imm_med = (imm[0].get("median_dir_ret_60m") or 0) if imm else 0
    adds = False
    costs = False
    for r in pb_summary:
        if r.get("bucket") in (None, "IMMEDIATE_T0"):
            continue
        if r.get("side") != "COMBINED":
            continue
        fill = r.get("fill_rate") or 0
        med = r.get("median_dir_ret_60m") or 0
        opp = r.get("opportunity_adjusted_med60")
        if fill >= 0.4 and med >= imm_med + 0.03 and opp is not None and opp >= 0.8 * max(imm_med, 1e-9):
            adds = True
        if med > imm_med + 0.03 and fill < 0.4 and opp is not None and opp < 0.8 * max(imm_med, 1e-9):
            costs = True
    if adds:
        return "PULLBACK_ENTRY_ADDS_VALUE"
    if costs:
        return "PULLBACK_ENTRY_COSTS_TOO_MANY_TRADES"
    return "PULLBACK_ENTRY_NO_CLEAR_VALUE"


def run_analysis() -> dict[str, Any]:
    print("[load] confirmation events", flush=True)
    events = load_confirmation_events()
    print(f"[load] n={len(events)}", flush=True)

    print("[load] 1m ohlcv + micro waves", flush=True)
    c1 = load_1m_ohlcv()
    w1 = load_micro_waves("1m")
    w5 = load_micro_waves("5m")

    print("[delay] build entries", flush=True)
    delay = build_delay_entries(events, c1)
    print(f"[delay] rows={len(delay)}", flush=True)

    print("[micro] state at entry", flush=True)
    delay = attach_micro_at_entry(delay, w1, w5)

    # delay / edge decay summaries
    delay_rows = []
    decay_rows = []
    for delay_min in ENTRY_DELAYS_MIN:
        for side in ("LONG", "SHORT", "COMBINED"):
            sub = delay[delay["delay_min"] == delay_min]
            if side != "COMBINED":
                sub = sub[sub["side"] == side]
            m = metrics(sub, side=side, delay_min=delay_min)
            delay_rows.append(m)
            decay_rows.append(m)

    # micro alignment at T0
    micro_align_rows = []
    t0 = delay[delay["delay_min"] == 0]
    for side in ("LONG", "SHORT", "COMBINED"):
        base = t0 if side == "COMBINED" else t0[t0["side"] == side]
        for tf, col in (("1m", "m1_align"), ("5m", "m5_align")):
            for align in ("ALIGNED", "COUNTER", "MIXED"):
                sub = base[base[col] == align]
                micro_align_rows.append(
                    metrics(sub, side=side, timeframe=tf, align=align, delay_min=0)
                )

    print("[wait] micro realign strategies", flush=True)
    wait_df = wait_for_micro_realign(events, c1, w1, w5)
    wait_rows = []
    for strat in (
        "A_immediate",
        "B_wait_1m_realign",
        "C_wait_5m_realign",
        "D_wait_1m_and_5m",
    ):
        for side in ("LONG", "SHORT", "COMBINED"):
            sub_all = wait_df[wait_df["strategy"] == strat]
            if side != "COMBINED":
                sub_all = sub_all[sub_all["side"] == side]
            filled = sub_all[sub_all["filled"] == True]  # noqa: E712
            m = metrics(filled, side=side, strategy=strat)
            m["n_episodes"] = int(len(sub_all))
            m["n_filled"] = int(len(filled))
            m["fill_rate"] = float(len(filled) / len(sub_all)) if len(sub_all) else None
            m["median_wait_min"] = (
                float(filled["wait_min"].median()) if len(filled) and "wait_min" in filled else None
            )
            wait_rows.append(m)

    print("[pullback] entries", flush=True)
    pb = pullback_entries(events, c1)
    pb_rows = []
    # immediate reference
    imm = delay[delay["delay_min"] == 0]
    for side in ("LONG", "SHORT", "COMBINED"):
        sub = imm if side == "COMBINED" else imm[imm["side"] == side]
        m = metrics(sub, side=side, bucket="IMMEDIATE_T0")
        m["fill_rate"] = 1.0
        m["missed_rate"] = 0.0
        m["opportunity_adjusted_med60"] = m.get("median_dir_ret_60m")
        pb_rows.append(m)
    for bname, _, _ in (
        ("0_05", 0, 0),
        ("05_10", 0, 0),
        ("10_20", 0, 0),
        ("20_30", 0, 0),
        ("gt30", 0, 0),
    ):
        for side in ("LONG", "SHORT", "COMBINED"):
            sub_all = pb[pb["bucket"] == bname]
            if side != "COMBINED":
                sub_all = sub_all[sub_all["side"] == side]
            filled = sub_all[sub_all["filled"] == True]  # noqa: E712
            m = metrics(filled, side=side, bucket=bname)
            m["n_episodes"] = int(len(sub_all))
            m["n_filled"] = int(len(filled))
            m["fill_rate"] = float(len(filled) / len(sub_all)) if len(sub_all) else None
            m["missed_rate"] = 1.0 - m["fill_rate"] if m["fill_rate"] is not None else None
            m["median_wait_min"] = float(filled["wait_min"].median()) if len(filled) else None
            m["median_entry_improvement_pct"] = (
                float(filled["entry_improvement_pct"].median()) if len(filled) else None
            )
            med = m.get("median_dir_ret_60m")
            m["opportunity_adjusted_med60"] = (
                (m["fill_rate"] * med) if m["fill_rate"] is not None and med is not None else None
            )
            # missed winners: imm positive but pullback missed
            if len(sub_all):
                missed = sub_all[sub_all["missed"] == True]  # noqa: E712
                m["missed_with_imm_win60_rate"] = (
                    float((missed["imm_dir_ret_60m"].astype(float) > 0).mean())
                    if len(missed)
                    else None
                )
            pb_rows.append(m)

    print("[first-touch] T0", flush=True)
    ft = first_touch_analysis(delay, c1)
    ft_rows = []
    for lvl in (0.10, 0.20):
        for side in ("LONG", "SHORT", "COMBINED"):
            sub = ft[ft["level_pct"] == lvl]
            if side != "COMBINED":
                sub = sub[sub["side"] == side]
            n = len(sub)
            ft_rows.append(
                {
                    "side": side,
                    "level_pct": lvl,
                    "n": n,
                    "share_favorable_first": float((sub["first_touch"] == "favorable_first").mean())
                    if n
                    else None,
                    "share_adverse_first": float((sub["first_touch"] == "adverse_first").mean())
                    if n
                    else None,
                    "share_both_same_bar": float((sub["first_touch"] == "both_same_bar").mean())
                    if n
                    else None,
                    "share_none": float((sub["first_touch"] == "none").mean()) if n else None,
                    "sample_flag": "OK" if n >= MIN_SAMPLE else "SMALL_SAMPLE",
                }
            )

    # failure strength (known at confirmation)
    strength_rows = []
    t0m = delay[delay["delay_min"] == 0].merge(
        events[
            [
                "wave_i",
                "M15_signed_price_move_pct",
                "M15_directional_efficiency",
                "M15_favorable_move_pct",
                "M15_adverse_move_pct",
                "M15_rsi_end",
                "M15_stoch_k_start",
                "M15_stoch_k_end",
                "wave_duration_min",
                "partial_fail_streak_1m",
            ]
        ],
        on="wave_i",
        how="left",
    )
    for feat in (
        "M15_directional_efficiency",
        "M15_signed_price_move_pct",
        "wave_duration_min",
        "partial_fail_streak_1m",
        "M15_rsi_end",
    ):
        s = t0m[feat].astype(float)
        try:
            t0m[f"{feat}_q"] = pd.qcut(s, 4, labels=["Q1", "Q2", "Q3", "Q4"], duplicates="drop")
        except ValueError:
            t0m[f"{feat}_q"] = "NA"
        for side in ("LONG", "SHORT", "COMBINED"):
            base = t0m if side == "COMBINED" else t0m[t0m["side"] == side]
            for q, sub in base.groupby(base[f"{feat}_q"].astype(str)):
                strength_rows.append(
                    metrics(sub, side=side, feature=feat, quantile=q)
                )

    primary = decide_primary(decay_rows, wait_rows)
    pullback_dec = decide_pullback(pb_rows)

    # confirmation event export
    conf_export = events[
        [
            c
            for c in events.columns
            if c
            in {
                "wave_i",
                "symbol",
                "failure_type",
                "side",
                "expected_reversal",
                "confirmation_available_at",
                "wave_start_available_at",
                "wave_end_available_at",
                "M15_direction",
                "M15_signed_price_move_pct",
                "M15_directional_efficiency",
                "M15_favorable_move_pct",
                "M15_adverse_move_pct",
                "M15_rsi_end",
                "M15_stoch_k_start",
                "M15_stoch_k_end",
                "wave_duration_min",
                "partial_fail_streak_1m",
            }
        ]
    ].copy()

    return {
        "audit_version": AUDIT_VERSION,
        "symbol": SYMBOL,
        "n_events": int(len(events)),
        "confirmation_events": conf_export,
        "entry_delay_detail": delay,
        "entry_delay_results": delay_rows,
        "edge_decay": decay_rows,
        "micro_alignment_results": micro_align_rows,
        "micro_wait_strategy": wait_rows,
        "micro_wait_detail": wait_df,
        "pullback_entry_results": pb_rows,
        "pullback_detail": pb,
        "first_touch_results": ft_rows,
        "first_touch_detail": ft,
        "failure_strength_results": strength_rows,
        "decisions": {"primary": primary, "pullback": pullback_dec},
        "method": {
            "entry_price": ENTRY_PRICE_DOC.strip(),
            "general": METHOD_DOC.strip(),
        },
    }
