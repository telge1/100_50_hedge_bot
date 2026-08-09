"""Orchestrate parent Tier-A × lower-TF context analysis."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_failure_multitimeframe.outcomes import load_1m
from orderbook_analyse.fractal_parent_signal_lower_tf_context import (
    AUDIT_VERSION,
    COUNT_TFS,
    FEE_PCT,
    FIXED_TPSL,
    HORIZONS,
    LOWER_TFS,
    METHOD_DOC,
    PARENT_TFS,
    SYMBOLS,
)
from orderbook_analyse.fractal_parent_signal_lower_tf_context.context import (
    add_counts,
    attach_lower_tf_context,
    load_symbol_waves,
    load_tier_a_parents,
    path_outcomes,
    phase_sequence_label,
    propagation_times,
    sample_flag,
    simulate_tpsl,
    summarize_group,
    summarize_tpsl_nets,
)
from orderbook_analyse.fractal_wave_fade_tier_tpsl.simulate import resolve_entry_indices

MAX_HOLD = {"1h": 72 * 60, "4h": 10 * 24 * 60}


def _consistency(doge: dict | None, btc: dict | None, *, key: str = "mean_dir_ret") -> str:
    if not doge or not btc:
        return "INSUFFICIENT"
    if doge.get("sample_flag") != "OK" or btc.get("sample_flag") != "OK":
        return "INSUFFICIENT"
    dv = doge.get(key)
    bv = btc.get(key)
    if dv is None or bv is None:
        return "INSUFFICIENT"
    # both improve vs baseline? handled outside; here same sign of lift
    if dv > 0 and bv > 0:
        return "REPLICATES"
    if (dv > 0) != (bv > 0):
        return "MIXED"
    return "CONTRADICTS"


def run_analysis() -> dict[str, Any]:
    print(METHOD_DOC, flush=True)
    events = load_tier_a_parents()
    print(f"[events Tier A 1h/4h] n={len(events)}", flush=True)

    parent_rows: list[dict] = []
    single_rows: list[dict] = []
    exhausted_rows: list[dict] = []
    ready_rows: list[dict] = []
    seq_rows: list[dict] = []
    prop_rows: list[dict] = []
    tpsl_rows: list[dict] = []
    long_short_rows: list[dict] = []
    cross_rows: list[dict] = []

    enriched_frames: list[pd.DataFrame] = []

    for sym in SYMBOLS:
        print(f"\n===== {sym} =====", flush=True)
        needed = set()
        for ptf in PARENT_TFS:
            needed.update(LOWER_TFS[ptf])
        waves = load_symbol_waves(sym, tuple(sorted(needed, key=lambda x: {"1m": 0, "5m": 1, "15m": 2, "30m": 3, "1h": 4}.get(x, 9))))

        c1 = load_1m(symbol=sym)
        high = c1["high"].astype(float).to_numpy()
        low = c1["low"].astype(float).to_numpy()
        close = c1["close"].astype(float).to_numpy()
        opens = c1["open"].astype(float).to_numpy()
        open_times = c1["timestamp"].to_numpy(dtype="datetime64[ns]")

        for ptf in PARENT_TFS:
            print(f"[parent] {sym} {ptf}", flush=True)
            sub = events[(events.symbol == sym) & (events.timeframe == ptf)].copy()
            sub = resolve_entry_indices(sub, open_times, opens)
            lower = LOWER_TFS[ptf]
            ctx = attach_lower_tf_context(sub, waves, lower)
            ctx = add_counts(ctx, ptf)
            ctx["phase_sequence"] = [phase_sequence_label(r, ptf) for _, r in ctx.iterrows()]

            horizons = HORIZONS[ptf]
            outcome_rows = []
            prop_local = []
            tpsl_local = {combo: [] for combo in FIXED_TPSL[ptf]}

            for ev in ctx.itertuples(index=False):
                if not bool(getattr(ev, "entry_valid", False)):
                    continue
                ei = int(ev.entry_i)
                epx = float(ev.entry_price)
                side = str(ev.side)
                po = path_outcomes(
                    entry_i=ei,
                    entry_px=epx,
                    side=side,
                    high=high,
                    low=low,
                    close=close,
                    open_times=open_times,
                    horizons=horizons,
                )
                row = {**ev._asdict(), **po}
                outcome_rows.append(row)

                # propagation diagnostic
                pr = propagation_times(
                    pd.Timestamp(ev.confirmation_available_at),
                    side,
                    waves,
                    lower,
                    max_look_min=MAX_HOLD[ptf],
                )
                prop_local.append(
                    {
                        "symbol": sym,
                        "timeframe": ptf,
                        "side": side,
                        "entry_time": str(ev.entry_time),
                        **pr,
                    }
                )

                for tp, sl in FIXED_TPSL[ptf]:
                    sim = simulate_tpsl(
                        entry_i=ei,
                        entry_px=epx,
                        side=side,
                        high=high,
                        low=low,
                        close=close,
                        open_times=open_times,
                        tp_pct=tp,
                        sl_pct=sl,
                        max_hold_min=MAX_HOLD[ptf],
                    )
                    tpsl_local[(tp, sl)].append({**row, **sim, "tp_pct": tp, "sl_pct": sl})

            odf = pd.DataFrame(outcome_rows)
            if odf.empty:
                continue
            enriched_frames.append(odf)
            prop_rows.extend(prop_local)

            # export slim parent signal rows
            keep_cols = [
                c
                for c in odf.columns
                if c.startswith("ltf_")
                or c
                in (
                    "symbol",
                    "timeframe",
                    "side",
                    "direction",
                    "confirmation_available_at",
                    "entry_time",
                    "entry_price",
                    "exhausted_count",
                    "ready_count",
                    "phase_sequence",
                    "tier",
                )
                or c.startswith("dir_ret_")
                or c.startswith("mfe_")
                or c.startswith("mae_")
                or c.startswith("reach_")
                or c.startswith("time_to_")
                or c in ("path_mfe", "path_mae")
            ]
            for rec in odf[keep_cols].to_dict("records"):
                parent_rows.append(rec)

            primary_h = horizons[0]

            # --- single TF conditioning ---
            for side in ("LONG", "SHORT"):
                sdf = odf[odf.side == side]
                for ltf in lower:
                    phase_col = f"ltf_{ltf}_phase"
                    zone_col = f"ltf_{ltf}_zone"
                    rel_col = f"ltf_{ltf}_rel"
                    for dim, col in (("phase", phase_col), ("zone", zone_col), ("rel", rel_col)):
                        if col not in sdf.columns:
                            continue
                        for val, g in sdf.groupby(col, dropna=False):
                            for h in horizons:
                                row = summarize_group(
                                    g,
                                    h,
                                    symbol=sym,
                                    parent_tf=ptf,
                                    side=side,
                                    lower_tf=ltf,
                                    dim=dim,
                                    value=str(val),
                                )
                                single_rows.append(row)
                                if h == primary_h:
                                    long_short_rows.append(row)

            # --- exhausted / ready counts ---
            for side in ("LONG", "SHORT"):
                sdf = odf[odf.side == side]
                for count_name in ("exhausted_count", "ready_count"):
                    for val, g in sdf.groupby(count_name):
                        for h in horizons:
                            row = summarize_group(
                                g,
                                h,
                                symbol=sym,
                                parent_tf=ptf,
                                side=side,
                                bucket=count_name,
                                count=int(val),
                            )
                            if count_name == "exhausted_count":
                                exhausted_rows.append(row)
                            else:
                                ready_rows.append(row)

            # --- phase sequences ---
            for side in ("LONG", "SHORT"):
                sdf = odf[odf.side == side]
                for val, g in sdf.groupby("phase_sequence"):
                    for h in horizons:
                        seq_rows.append(
                            summarize_group(
                                g,
                                h,
                                symbol=sym,
                                parent_tf=ptf,
                                side=side,
                                sequence=str(val),
                            )
                        )

            # --- fixed TPSL by context ---
            for (tp, sl), trades in tpsl_local.items():
                tdf = pd.DataFrame(trades)
                for side in ("LONG", "SHORT", "COMBINED"):
                    sub_t = tdf if side == "COMBINED" else tdf[tdf.side == side]
                    # by exhausted count
                    for val, g in sub_t.groupby("exhausted_count"):
                        nets = g["net"].astype(float).to_numpy()
                        exits = g["exit_type"].astype(str).to_numpy()
                        tpsl_rows.append(
                            summarize_tpsl_nets(
                                nets,
                                exits,
                                symbol=sym,
                                parent_tf=ptf,
                                side=side,
                                tp_pct=tp,
                                sl_pct=sl,
                                context="exhausted_count",
                                context_value=int(val),
                            )
                        )
                    # by ready count
                    for val, g in sub_t.groupby("ready_count"):
                        nets = g["net"].astype(float).to_numpy()
                        exits = g["exit_type"].astype(str).to_numpy()
                        tpsl_rows.append(
                            summarize_tpsl_nets(
                                nets,
                                exits,
                                symbol=sym,
                                parent_tf=ptf,
                                side=side,
                                tp_pct=tp,
                                sl_pct=sl,
                                context="ready_count",
                                context_value=int(val),
                            )
                        )
                    # by primary lower TF zone (30m for 1h, 1h for 4h)
                    primary_ltf = "30m" if ptf == "1h" else "1h"
                    zcol = f"ltf_{primary_ltf}_zone"
                    if zcol in sub_t.columns:
                        for val, g in sub_t.groupby(zcol):
                            nets = g["net"].astype(float).to_numpy()
                            exits = g["exit_type"].astype(str).to_numpy()
                            tpsl_rows.append(
                                summarize_tpsl_nets(
                                    nets,
                                    exits,
                                    symbol=sym,
                                    parent_tf=ptf,
                                    side=side,
                                    tp_pct=tp,
                                    sl_pct=sl,
                                    context=f"{primary_ltf}_zone",
                                    context_value=str(val),
                                )
                            )
                    # baseline all
                    nets = sub_t["net"].astype(float).to_numpy()
                    exits = sub_t["exit_type"].astype(str).to_numpy()
                    tpsl_rows.append(
                        summarize_tpsl_nets(
                            nets,
                            exits,
                            symbol=sym,
                            parent_tf=ptf,
                            side=side,
                            tp_pct=tp,
                            sl_pct=sl,
                            context="ALL",
                            context_value="ALL",
                        )
                    )

    # cross-symbol: exhausted 0 vs max on primary horizon
    for ptf in PARENT_TFS:
        h = HORIZONS[ptf][0]
        for side in ("LONG", "SHORT"):
            for count in range(0, len(COUNT_TFS[ptf]) + 1):
                doge = next(
                    (
                        r
                        for r in exhausted_rows
                        if r.get("symbol") == "DOGEUSDT"
                        and r.get("parent_tf") == ptf
                        and r.get("side") == side
                        and r.get("count") == count
                        and r.get("horizon_min") == h
                    ),
                    None,
                )
                btc = next(
                    (
                        r
                        for r in exhausted_rows
                        if r.get("symbol") == "BTCUSDT"
                        and r.get("parent_tf") == ptf
                        and r.get("side") == side
                        and r.get("count") == count
                        and r.get("horizon_min") == h
                    ),
                    None,
                )
                cross_rows.append(
                    {
                        "hypothesis": "exhausted_count",
                        "parent_tf": ptf,
                        "side": side,
                        "count": count,
                        "horizon_min": h,
                        "DOGE_mean_dir_ret": None if not doge else doge.get("mean_dir_ret"),
                        "BTC_mean_dir_ret": None if not btc else btc.get("mean_dir_ret"),
                        "DOGE_n": None if not doge else doge.get("n"),
                        "BTC_n": None if not btc else btc.get("n"),
                        "consistency": _consistency(doge, btc),
                    }
                )
            # zone HIGH vs LOW on 30m/1h
            primary_ltf = "30m" if ptf == "1h" else "1h"
            for zone in ("HIGH", "MID", "LOW"):
                doge = next(
                    (
                        r
                        for r in single_rows
                        if r.get("symbol") == "DOGEUSDT"
                        and r.get("parent_tf") == ptf
                        and r.get("side") == side
                        and r.get("lower_tf") == primary_ltf
                        and r.get("dim") == "zone"
                        and r.get("value") == zone
                        and r.get("horizon_min") == h
                    ),
                    None,
                )
                btc = next(
                    (
                        r
                        for r in single_rows
                        if r.get("symbol") == "BTCUSDT"
                        and r.get("parent_tf") == ptf
                        and r.get("side") == side
                        and r.get("lower_tf") == primary_ltf
                        and r.get("dim") == "zone"
                        and r.get("value") == zone
                        and r.get("horizon_min") == h
                    ),
                    None,
                )
                cross_rows.append(
                    {
                        "hypothesis": f"{primary_ltf}_zone",
                        "parent_tf": ptf,
                        "side": side,
                        "zone": zone,
                        "horizon_min": h,
                        "DOGE_mean_dir_ret": None if not doge else doge.get("mean_dir_ret"),
                        "BTC_mean_dir_ret": None if not btc else btc.get("mean_dir_ret"),
                        "DOGE_hit_rate": None if not doge else doge.get("hit_rate"),
                        "BTC_hit_rate": None if not btc else btc.get("hit_rate"),
                        "DOGE_n": None if not doge else doge.get("n"),
                        "BTC_n": None if not btc else btc.get("n"),
                        "consistency": _consistency(doge, btc),
                    }
                )

    decisions, answers = _decide_and_answer(
        exhausted_rows, ready_rows, single_rows, tpsl_rows, seq_rows, cross_rows
    )

    return {
        "audit_version": AUDIT_VERSION,
        "fee_pct": FEE_PCT,
        "method": METHOD_DOC.strip(),
        "parent_signals_with_lower_tf": parent_rows,
        "single_lower_tf_phase_results": single_rows,
        "exhausted_count_results": exhausted_rows,
        "ready_count_results": ready_rows,
        "phase_sequence_results": seq_rows,
        "propagation_timing": prop_rows,
        "fixed_tpsl_by_lower_context": tpsl_rows,
        "long_short_results": long_short_rows,
        "cross_symbol_consistency": cross_rows,
        "decisions": decisions,
        "answers": answers,
    }


def _decide_and_answer(
    exhausted_rows,
    ready_rows,
    single_rows,
    tpsl_rows,
    seq_rows,
    cross_rows,
) -> tuple[dict[str, str], dict[str, Any]]:
    answers: dict[str, Any] = {}

    def exh(sym, ptf, side, count, h):
        return next(
            (
                r
                for r in exhausted_rows
                if r.get("symbol") == sym
                and r.get("parent_tf") == ptf
                and r.get("side") == side
                and r.get("count") == count
                and r.get("horizon_min") == h
            ),
            None,
        )

    def zone_row(sym, ptf, side, ltf, zone, h):
        return next(
            (
                r
                for r in single_rows
                if r.get("symbol") == sym
                and r.get("parent_tf") == ptf
                and r.get("side") == side
                and r.get("lower_tf") == ltf
                and r.get("dim") == "zone"
                and r.get("value") == zone
                and r.get("horizon_min") == h
            ),
            None,
        )

    # A: 1h SHORT exhausted 3 vs 0
    h = 240
    a_doge0 = exh("DOGEUSDT", "1h", "SHORT", 0, h)
    a_doge3 = exh("DOGEUSDT", "1h", "SHORT", 3, h)
    a_btc0 = exh("BTCUSDT", "1h", "SHORT", 0, h)
    a_btc3 = exh("BTCUSDT", "1h", "SHORT", 3, h)
    answers["A_1h_SHORT_all_LOW"] = {
        "DOGE_count0": a_doge0,
        "DOGE_count3": a_doge3,
        "BTC_count0": a_btc0,
        "BTC_count3": a_btc3,
        "verdict": _exh_verdict(a_doge0, a_doge3, a_btc0, a_btc3),
    }

    # B: 1h LONG exhausted 3 vs 0 (HIGH)
    b_d0 = exh("DOGEUSDT", "1h", "LONG", 0, h)
    b_d3 = exh("DOGEUSDT", "1h", "LONG", 3, h)
    b_b0 = exh("BTCUSDT", "1h", "LONG", 0, h)
    b_b3 = exh("BTCUSDT", "1h", "LONG", 3, h)
    answers["B_1h_LONG_all_HIGH"] = {
        "DOGE_count0": b_d0,
        "DOGE_count3": b_d3,
        "BTC_count0": b_b0,
        "BTC_count3": b_b3,
        "verdict": _exh_verdict(b_d0, b_d3, b_b0, b_b3),
    }

    # C: how many before drop
    answers["C_counts_1h"] = {}
    for side in ("SHORT", "LONG"):
        series = []
        for c in range(0, 4):
            d = exh("DOGEUSDT", "1h", side, c, h)
            b = exh("BTCUSDT", "1h", side, c, h)
            series.append(
                {
                    "count": c,
                    "DOGE_mean": None if not d else d.get("mean_dir_ret"),
                    "BTC_mean": None if not b else b.get("mean_dir_ret"),
                    "DOGE_n": None if not d else d.get("n"),
                    "BTC_n": None if not b else b.get("n"),
                }
            )
        answers["C_counts_1h"][side] = series

    # D ready all rare?
    ready_all = [
        r
        for r in ready_rows
        if r.get("parent_tf") == "1h"
        and r.get("horizon_min") == h
        and r.get("count") == 3
        and r.get("symbol") == "DOGEUSDT"
    ]
    answers["D_ready_all_lower"] = ready_all

    # E best phase on 30m for 1h
    answers["E_best_30m_phase_1h"] = {}
    for side in ("SHORT", "LONG"):
        cands = [
            r
            for r in single_rows
            if r.get("parent_tf") == "1h"
            and r.get("side") == side
            and r.get("lower_tf") == "30m"
            and r.get("dim") == "phase"
            and r.get("horizon_min") == h
            and r.get("sample_flag") == "OK"
        ]
        by_phase: dict[str, list] = {}
        for r in cands:
            by_phase.setdefault(str(r.get("value")), []).append(r)
        scored = []
        for ph, rows in by_phase.items():
            doge = next((x for x in rows if x.get("symbol") == "DOGEUSDT"), None)
            btc = next((x for x in rows if x.get("symbol") == "BTCUSDT"), None)
            if doge and btc:
                scored.append(
                    (
                        (doge.get("mean_dir_ret") or 0) + (btc.get("mean_dir_ret") or 0),
                        ph,
                        doge.get("mean_dir_ret"),
                        btc.get("mean_dir_ret"),
                    )
                )
        scored.sort(reverse=True)
        answers["E_best_30m_phase_1h"][side] = scored[:3]

    # F 4h
    h4 = 720
    answers["F_4h"] = {}
    for side in ("SHORT", "LONG"):
        series = []
        for c in range(0, 5):
            d = exh("DOGEUSDT", "4h", side, c, h4)
            b = exh("BTCUSDT", "4h", side, c, h4)
            series.append(
                {
                    "count": c,
                    "DOGE_mean": None if not d else d.get("mean_dir_ret"),
                    "BTC_mean": None if not b else b.get("mean_dir_ret"),
                    "DOGE_n": None if not d else d.get("n"),
                    "BTC_n": None if not b else b.get("n"),
                }
            )
        answers["F_4h"][side] = series

    # decisions
    hurt_votes = 0
    help_votes = 0
    for side, high_count in (("SHORT", 3), ("LONG", 3)):
        for sym in SYMBOLS:
            a0 = exh(sym, "1h", side, 0, h)
            aN = exh(sym, "1h", side, high_count, h)
            if not a0 or not aN:
                continue
            if (a0.get("mean_dir_ret") or 0) - (aN.get("mean_dir_ret") or 0) > 0.15:
                hurt_votes += 1
            elif (aN.get("mean_dir_ret") or 0) - (a0.get("mean_dir_ret") or 0) > 0.15:
                help_votes += 1

    # also zone LOW vs HIGH for SHORT on 30m
    zone_hurt = 0
    for sym in SYMBOLS:
        hi = zone_row(sym, "1h", "SHORT", "30m", "HIGH", h)
        lo = zone_row(sym, "1h", "SHORT", "30m", "LOW", h)
        if hi and lo and (hi.get("mean_dir_ret") or 0) - (lo.get("mean_dir_ret") or 0) > 0.1:
            zone_hurt += 1

    if hurt_votes >= 2 and zone_hurt >= 1:
        ext_dec = "LOWER_TFS_ALREADY_EXTENDED_HURT_ENTRY"
    else:
        ext_dec = "LOWER_TFS_ALREADY_EXTENDED_DO_NOT_HURT"

    # ready alignment improves?
    ready_improve = 0
    for side in ("SHORT", "LONG"):
        for sym in SYMBOLS:
            r0 = next(
                (
                    r
                    for r in ready_rows
                    if r.get("symbol") == sym
                    and r.get("parent_tf") == "1h"
                    and r.get("side") == side
                    and r.get("count") == 0
                    and r.get("horizon_min") == h
                ),
                None,
            )
            r3 = next(
                (
                    r
                    for r in ready_rows
                    if r.get("symbol") == sym
                    and r.get("parent_tf") == "1h"
                    and r.get("side") == side
                    and r.get("count") == 3
                    and r.get("horizon_min") == h
                ),
                None,
            )
            if r0 and r3 and (r3.get("mean_dir_ret") or 0) - (r0.get("mean_dir_ret") or 0) > 0.15:
                ready_improve += 1
    ready_dec = (
        "MULTI_TF_READY_ALIGNMENT_IMPROVES_ENTRY"
        if ready_improve >= 2
        else "MULTI_TF_READY_ALIGNMENT_NOT_REQUIRED"
    )

    # primary
    if ext_dec.endswith("HURT_ENTRY") and ready_improve >= 1:
        primary = "LOWER_TF_PHASE_MATERIALLY_IMPROVES_PARENT_SIGNAL"
        g_rec = "QUALITY_RANK"
    elif ext_dec.endswith("HURT_ENTRY") or ready_improve >= 1:
        primary = "LOWER_TF_PHASE_ADDS_TIMING_CONTEXT"
        g_rec = "TIMING_ONLY"
    else:
        # check any consistent cross zone effect
        reps = [
            r
            for r in cross_rows
            if r.get("hypothesis", "").endswith("_zone") and r.get("consistency") == "REPLICATES"
        ]
        if len(reps) >= 4:
            primary = "LOWER_TF_PHASE_ADDS_TIMING_CONTEXT"
            g_rec = "TIMING_ONLY"
        else:
            primary = "LOWER_TF_PHASE_NO_ROBUST_VALUE"
            g_rec = "IGNORE"

    answers["G_recommendation"] = g_rec

    # TPSL note: exhausted 0 vs 3 for fixed combos
    answers["TPSL_exhausted"] = [
        r
        for r in tpsl_rows
        if r.get("context") == "exhausted_count"
        and r.get("side") == "COMBINED"
        and r.get("sample_flag") == "OK"
    ][:40]

    decisions = {
        "primary": primary,
        "extended": ext_dec,
        "ready": ready_dec,
    }
    return decisions, answers


def _exh_verdict(a0, aN, b0, bN) -> str:
    lifts = []
    for x0, xN in ((a0, aN), (b0, bN)):
        if x0 and xN and x0.get("mean_dir_ret") is not None and xN.get("mean_dir_ret") is not None:
            lifts.append((x0["mean_dir_ret"] - xN["mean_dir_ret"], x0.get("n"), xN.get("n")))
    if len(lifts) < 2:
        return "INSUFFICIENT"
    # positive lift => count0 better than countN => extended hurts
    if all(L[0] > 0.1 for L in lifts):
        return "EXTENDED_HURTS_BOTH"
    if all(L[0] < -0.1 for L in lifts):
        return "EXTENDED_HELPS_BOTH"
    if any(L[0] > 0.1 for L in lifts) and any(L[0] < -0.1 for L in lifts):
        return "MIXED"
    return "NO_CLEAR_EFFECT"
