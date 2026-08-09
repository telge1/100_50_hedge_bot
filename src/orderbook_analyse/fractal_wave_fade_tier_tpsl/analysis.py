"""Orchestrate tier × TP/SL generalization for DOGE/BTC."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_failure_multitimeframe.outcomes import load_1m
from orderbook_analyse.fractal_wave_fade_tier_tpsl import (
    AUDIT_VERSION,
    FEE_PCT,
    LARGE_ADVERSE,
    LARGE_TARGETS,
    MAX_HOLD_MIN,
    METHOD_DOC,
    REACH_LEVELS,
    REFERENCE_COMBOS,
    SHORT_H_MIN,
    SIGNAL_TFS,
    SL_GRID,
    SYMBOLS,
    TP_GRID,
)
from orderbook_analyse.fractal_wave_fade_tier_tpsl.simulate import (
    build_paths,
    large_move_success,
    load_events,
    mfe_mae_summary,
    prep_path_levels,
    reachability,
    resolve_entry_indices,
    resolve_tpsl,
    run_combo_on_paths,
    sample_flag,
    summarize_trades,
)


def _filter_paths(paths: list[dict], *, tier: str | None = None, side: str | None = None) -> list[dict]:
    out = []
    for p in paths:
        if not p["valid"]:
            continue
        if tier is not None and tier != "ALL" and p.get("tier") != tier:
            continue
        if side is not None and side != "COMBINED" and p.get("side") != side:
            continue
        out.append(p)
    return out


def _frontier(rows: list[dict]) -> list[dict]:
    """Mark diagnostic candidates among positive-expectancy combos."""
    pos = [r for r in rows if (r.get("expectancy") or -999) > 0 and r.get("sample_flag") == "OK"]
    if not pos:
        return []
    out = []

    def mark(r: dict, label: str) -> dict:
        x = dict(r)
        x["frontier_label"] = label
        return x

    out.append(mark(max(pos, key=lambda r: r.get("expectancy") or -999), "HIGHEST_EXPECTANCY"))
    pf = [r for r in pos if r.get("profit_factor") is not None]
    if pf:
        out.append(mark(max(pf, key=lambda r: r.get("profit_factor") or -999), "HIGHEST_PF"))
    dd = [r for r in pos if r.get("max_drawdown") is not None]
    if dd:
        out.append(
            mark(max(dd, key=lambda r: r.get("max_drawdown") or -1e9), "LOWEST_DD_POS_EXPECTANCY")
        )
    pf1 = [r for r in pos if (r.get("profit_factor") or 0) > 1]
    if pf1:
        out.append(
            mark(max(pf1, key=lambda r: r.get("expectancy") or -999), "HIGHEST_EXP_PF_GT1")
        )
    # mild DD bound: |maxDD| <= 50 (additive % points sum) as soft research bound
    mild = [r for r in pos if r.get("max_drawdown") is not None and abs(r["max_drawdown"]) <= 50]
    if mild:
        out.append(
            mark(max(mild, key=lambda r: r.get("expectancy") or -999), "HIGHEST_EXP_MILD_DD")
        )
    return out


def decide_wide_tp(reach_rows: list[dict], ref_rows: list[dict]) -> str:
    """Based on reach + reference combos with TP>=2."""
    high_tf_ok = 0
    low_tf_ok = 0
    for tf in SIGNAL_TFS:
        # Tier A reach >=2% and >=4%
        r2 = next(
            (
                r
                for r in reach_rows
                if r.get("timeframe") == tf
                and r.get("tier") == "A"
                and r.get("level_pct") == 2.0
                and r.get("side") == "COMBINED"
            ),
            None,
        )
        r4 = next(
            (
                r
                for r in reach_rows
                if r.get("timeframe") == tf
                and r.get("tier") == "A"
                and r.get("level_pct") == 4.0
                and r.get("side") == "COMBINED"
            ),
            None,
        )
        refs = [
            r
            for r in ref_rows
            if r.get("timeframe") == tf
            and r.get("tier") == "A"
            and r.get("side") == "COMBINED"
            and (r.get("tp_pct") or 0) >= 2.0
            and (r.get("expectancy") or 0) > 0
            and (r.get("profit_factor") or 0) > 1
        ]
        if tf in ("1h", "4h") and r2 and (r2.get("reach_rate") or 0) >= 0.35 and refs:
            high_tf_ok += 1
        if tf in ("15m", "30m") and r2 and (r2.get("reach_rate") or 0) >= 0.35 and refs:
            low_tf_ok += 1
    if high_tf_ok >= 2 and low_tf_ok >= 1:
        return "WIDE_TP_WAVE_FADE_HAS_EDGE"
    if high_tf_ok >= 1 and low_tf_ok == 0:
        # Wide TPs only look viable on higher TFs → primary: moderate overall
        return "ONLY_MODERATE_TP_IS_VIABLE"
    # primary wide_tp decision
    ref_pos = [
        r
        for r in ref_rows
        if r.get("tier") == "A"
        and r.get("side") == "COMBINED"
        and (r.get("tp_pct") or 0) >= 2
        and (r.get("expectancy") or 0) > 0
        and (r.get("profit_factor") or 0) > 1
    ]
    by_tf = {tf: 0 for tf in SIGNAL_TFS}
    for r in ref_pos:
        by_tf[r["timeframe"]] = by_tf.get(r["timeframe"], 0) + 1
    n_tf = sum(1 for v in by_tf.values() if v > 0)
    if n_tf >= 3:
        return "WIDE_TP_WAVE_FADE_HAS_EDGE"
    if n_tf >= 1 and (by_tf.get("1h", 0) + by_tf.get("4h", 0)) > (
        by_tf.get("15m", 0) + by_tf.get("30m", 0)
    ):
        return "ONLY_MODERATE_TP_IS_VIABLE"
    # check moderate (TP<=1.5) positive widely
    mod = [
        r
        for r in ref_rows
        if r.get("tier") == "A"
        and r.get("side") == "COMBINED"
        and (r.get("tp_pct") or 0) <= 1.5
        and (r.get("expectancy") or 0) > 0
        and (r.get("profit_factor") or 0) > 1
    ]
    if len({r["timeframe"] for r in mod}) >= 3:
        return "ONLY_MODERATE_TP_IS_VIABLE"
    if n_tf == 0:
        return "WIDE_TP_DOES_NOT_ADD_VALUE"
    return "ONLY_MODERATE_TP_IS_VIABLE"


def decide_large_tp(reach_rows: list[dict], mfe_rows: list[dict], ref_rows: list[dict]) -> str:
    def tier_a_reach(tf: str, lvl: float) -> float:
        rates = [
            x.get("reach_rate") or 0
            for x in reach_rows
            if x.get("timeframe") == tf
            and x.get("tier") == "A"
            and x.get("level_pct") == lvl
            and x.get("side") == "COMBINED"
            and x.get("symbol") in SYMBOLS
        ]
        return float(np.mean(rates)) if rates else 0.0

    def tier_a_mfe_med(tf: str) -> float:
        vals = [
            x.get("mfe_median") or 0
            for x in mfe_rows
            if x.get("timeframe") == tf
            and x.get("tier") == "A"
            and x.get("side") == "COMBINED"
            and x.get("symbol") in SYMBOLS
        ]
        return float(np.mean(vals)) if vals else 0.0

    hi = (
        tier_a_reach("4h", 4.0) >= 0.30
        and tier_a_mfe_med("4h") >= 2.0
        and tier_a_reach("1h", 2.0) >= 0.30
    )
    lo = tier_a_reach("15m", 2.0) >= 0.30 and tier_a_reach("30m", 2.0) >= 0.30
    if hi and lo:
        return "MULTIPERCENT_TP_IS_REALISTIC"
    if hi:
        return "MULTIPERCENT_TP_ONLY_ON_HIGHER_TF"
    return "MULTIPERCENT_TP_NOT_REALISTIC"


def decide_tier_a(ref_rows: list[dict]) -> str:
    lifts = []
    for r in ref_rows:
        if r.get("tier") != "A" or r.get("side") != "COMBINED":
            continue
        if r.get("sample_flag") != "OK":
            continue
        base = next(
            (
                x
                for x in ref_rows
                if x.get("symbol") == r.get("symbol")
                and x.get("timeframe") == r.get("timeframe")
                and x.get("tier") == "ALL"
                and x.get("side") == "COMBINED"
                and x.get("tp_pct") == r.get("tp_pct")
                and x.get("sl_pct") == r.get("sl_pct")
            ),
            None,
        )
        if not base:
            continue
        lifts.append((r.get("expectancy") or 0) - (base.get("expectancy") or 0))
    if len(lifts) < 8:
        return "TIER_A_NO_ROBUST_TPSL_VALUE"
    pos = sum(1 for x in lifts if x > 0.01)
    big = sum(1 for x in lifts if x > 0.03)
    if big >= max(5, len(lifts) // 3) and pos > len(lifts) / 2:
        return "TIER_A_MATERIALLY_IMPROVES_TPSL"
    if pos > len(lifts) / 2:
        return "TIER_A_IMPROVES_QUALITY_ONLY"
    return "TIER_A_NO_ROBUST_TPSL_VALUE"


def recommend_ranges(
    reach_rows: list[dict],
    mfe_rows: list[dict],
    ref_rows: list[dict],
) -> dict[str, dict]:
    out = {}
    suggestions = {
        "15m": {"tp": (0.5, 1.0), "sl": (0.75, 1.5)},
        "30m": {"tp": (1.0, 2.0), "sl": (1.0, 2.0)},
        "1h": {"tp": (1.5, 3.0), "sl": (1.0, 2.0)},
        "4h": {"tp": (2.0, 5.0), "sl": (1.5, 3.0)},
    }
    for tf in SIGNAL_TFS:
        # adjust by evidence: keep only TPs where Tier A reach>=25% and some ref positive
        ok_tps = []
        for lvl in (0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 6.0):
            rr = next(
                (
                    r
                    for r in reach_rows
                    if r.get("timeframe") == tf
                    and r.get("tier") == "A"
                    and r.get("level_pct") == lvl
                    and r.get("side") == "COMBINED"
                ),
                None,
            )
            if rr and (rr.get("reach_rate") or 0) >= 0.25:
                # any positive ref with this tp on DOGE or BTC tier A
                refs = [
                    r
                    for r in ref_rows
                    if r.get("timeframe") == tf
                    and r.get("tier") == "A"
                    and r.get("side") == "COMBINED"
                    and abs((r.get("tp_pct") or 0) - lvl) < 1e-9
                    and (r.get("expectancy") or 0) > 0
                ]
                if refs or lvl <= suggestions[tf]["tp"][1]:
                    ok_tps.append(lvl)
        mfe = next(
            (
                r
                for r in mfe_rows
                if r.get("timeframe") == tf and r.get("tier") == "A" and r.get("side") == "COMBINED"
            ),
            None,
        )
        out[tf] = {
            "recommended_tp_range": suggestions[tf]["tp"],
            "recommended_sl_range": suggestions[tf]["sl"],
            "evidence_reachable_tps_ge25pct": ok_tps,
            "tier_a_mfe_median": None if not mfe else mfe.get("mfe_median"),
            "note": "research candidate only",
        }
    return out


def run_analysis() -> dict[str, Any]:
    print(METHOD_DOC, flush=True)
    events = load_events()
    print(f"[events] n={len(events)}", flush=True)

    signal_tiers = (
        events.groupby(["symbol", "timeframe", "tier", "side"], dropna=False)
        .size()
        .reset_index(name="n")
        .to_dict("records")
    )
    # coverage
    coverage_rows = []
    for sym in SYMBOLS:
        for tf in SIGNAL_TFS:
            sub = events[(events.symbol == sym) & (events.timeframe == tf)]
            n_all = len(sub)
            for tier in ("A", "B", "C", "D", "MIXED"):
                n = int((sub.tier == tier).sum())
                coverage_rows.append(
                    {
                        "symbol": sym,
                        "timeframe": tf,
                        "tier": tier,
                        "n": n,
                        "retained_fraction": n / n_all if n_all else None,
                        "signals_per_month_approx": None,
                    }
                )
            et = pd.to_datetime(sub["entry_time"], utc=True)
            if len(et):
                months = max(1.0, (et.max() - et.min()).days / 30.44)
                for row in coverage_rows:
                    if row["symbol"] == sym and row["timeframe"] == tf:
                        row["signals_per_month_approx"] = row["n"] / months
                        row["signals_per_year_approx"] = row["n"] / months * 12

    grid_rows: list[dict] = []
    ref_rows: list[dict] = []
    long_short_rows: list[dict] = []
    reach_rows: list[dict] = []
    mfe_rows: list[dict] = []
    large_rows: list[dict] = []
    frontier_rows: list[dict] = []
    tier_comp: list[dict] = []
    monthly_rows: list[dict] = []
    cross_rows: list[dict] = []
    short_h_rows: list[dict] = []

    for sym in SYMBOLS:
        print(f"\n===== {sym} =====", flush=True)
        c1 = load_1m(symbol=sym)
        high = c1["high"].astype(float).to_numpy()
        low = c1["low"].astype(float).to_numpy()
        close = c1["close"].astype(float).to_numpy()
        opens = c1["open"].astype(float).to_numpy()
        open_times = c1["timestamp"].to_numpy(dtype="datetime64[ns]")

        for tf in SIGNAL_TFS:
            print(f"[tf] {sym} {tf}", flush=True)
            sub = events[(events.symbol == sym) & (events.timeframe == tf)].copy()
            sub = resolve_entry_indices(sub, open_times, opens)
            # BTC already limited by 1m coverage in events
            max_hold = MAX_HOLD_MIN[tf]
            paths = build_paths(
                sub,
                high=high,
                low=low,
                close=close,
                open_times=open_times,
                max_hold_min=max_hold,
            )
            # chronological
            paths = sorted(
                [p for p in paths if p["valid"]],
                key=lambda p: p["entry_time"],
            )
            for p in paths:
                prep_path_levels(p, TP_GRID, SL_GRID)
            print(f"[path] valid={len(paths)} max_hold={max_hold}", flush=True)

            for tier in ("ALL", "A", "B", "C", "D", "MIXED"):
                for side in ("COMBINED", "LONG", "SHORT"):
                    pp = _filter_paths(paths, tier=None if tier == "ALL" else tier, side=side)
                    # MFE/MAE + reach + large-move once per tier/side
                    mfe_rows.append(
                        mfe_mae_summary(
                            pp, symbol=sym, timeframe=tf, tier=tier, side=side, max_hold_min=max_hold
                        )
                    )
                    for lvl in REACH_LEVELS:
                        reach_rows.append(
                            reachability(
                                pp,
                                lvl,
                                symbol=sym,
                                timeframe=tf,
                                tier=tier,
                                side=side,
                            )
                        )
                    if side == "COMBINED":
                        for tgt in LARGE_TARGETS:
                            for adv in LARGE_ADVERSE:
                                large_rows.append(
                                    large_move_success(
                                        pp,
                                        target=tgt,
                                        max_adverse=adv,
                                        symbol=sym,
                                        timeframe=tf,
                                        tier=tier,
                                    )
                                )

                    # full TP×SL grid for every tier × side (LONG/SHORT/COMBINED)
                    combos = [(t, s) for t in TP_GRID for s in SL_GRID]
                    tier_grid_bucket = []
                    for tp, sl in combos:
                        nets, exits, holds = run_combo_on_paths(pp, tp=tp, sl=sl, policy="SL_FIRST")
                        row = summarize_trades(
                            nets,
                            exits,
                            holds,
                            symbol=sym,
                            timeframe=tf,
                            tier=tier,
                            side=side,
                            tp_pct=tp,
                            sl_pct=sl,
                            policy="SL_FIRST",
                            max_hold_min=max_hold,
                        )
                        # ambiguous only for reference combos (cost control)
                        if (
                            (tp, sl) in REFERENCE_COMBOS
                            and side == "COMBINED"
                            and tier == "A"
                        ):
                            amb = 0
                            for p in pp:
                                s1 = resolve_tpsl(p, tp_pct=tp, sl_pct=sl, policy="SL_FIRST")
                                s2 = resolve_tpsl(p, tp_pct=tp, sl_pct=sl, policy="TP_FIRST")
                                if s1.get("ambiguous") or (
                                    s1["exit_type"] != s2["exit_type"]
                                    and s1["exit_type"] in ("TP", "SL")
                                    and s2["exit_type"] in ("TP", "SL")
                                ):
                                    amb += 1
                            row["ambiguous_count"] = amb
                            row["ambiguous_rate"] = amb / len(nets) if len(nets) else None
                        grid_rows.append(row)
                        tier_grid_bucket.append(row)
                        if (tp, sl) in REFERENCE_COMBOS:
                            ref_rows.append(row)
                        if side != "COMBINED":
                            long_short_rows.append(row)

                    if side == "COMBINED" and tier in ("ALL", "A", "B"):
                        frontier_rows.extend(_frontier(tier_grid_bucket))

                    # short-horizon sensitivity on reference for COMBINED ALL/A only
                    if side == "COMBINED" and tier in ("ALL", "A"):
                        short_h = SHORT_H_MIN[tf]
                        pp_short = []
                        for p in pp:
                            hold = p["hold_min"]
                            m = hold <= short_h
                            if not np.any(m):
                                continue
                            cut = int(np.where(m)[0][-1]) + 1
                            q = {
                                "valid": True,
                                "fav": p["fav"][:cut],
                                "adv": p["adv"][:cut],
                                "raw": p["raw"][:cut],
                                "hold_min": p["hold_min"][:cut],
                                "_prepped": False,
                            }
                            prep_path_levels(q, TP_GRID, SL_GRID)
                            pp_short.append(q)
                        for tp, sl in REFERENCE_COMBOS:
                            nets, exits, holds = run_combo_on_paths(
                                pp_short, tp=tp, sl=sl, policy="SL_FIRST"
                            )
                            short_h_rows.append(
                                summarize_trades(
                                    nets,
                                    exits,
                                    holds,
                                    symbol=sym,
                                    timeframe=tf,
                                    tier=tier,
                                    side=side,
                                    tp_pct=tp,
                                    sl_pct=sl,
                                    max_hold_min=short_h,
                                    horizon_mode="SHORT_MAIN_H",
                                )
                            )

            # monthly stability for key reference combos on Tier A + ALL
            print(f"[monthly] {sym} {tf}", flush=True)
            for tier in ("ALL", "A"):
                pp = _filter_paths(paths, tier=None if tier == "ALL" else tier, side="COMBINED")
                for tp, sl in (
                    (1.0, 1.0),
                    (1.5, 1.5),
                    (2.0, 2.0),
                    (3.0, 2.0),
                    (4.0, 2.0),
                    (6.0, 3.0),
                ):
                    month_map: dict[str, list] = {}
                    for p in pp:
                        sim = resolve_tpsl(p, tp_pct=tp, sl_pct=sl, policy="SL_FIRST")
                        if sim["exit_type"] == "INVALID":
                            continue
                        mo = pd.Timestamp(p["entry_time"]).strftime("%Y-%m")
                        month_map.setdefault(mo, []).append(sim["net"])
                    for mo, nets_l in sorted(month_map.items()):
                        nets = np.asarray(nets_l, dtype=float)
                        monthly_rows.append(
                            {
                                "symbol": sym,
                                "timeframe": tf,
                                "tier": tier,
                                "tp_pct": tp,
                                "sl_pct": sl,
                                "month": mo,
                                "n": int(len(nets)),
                                "expectancy": float(np.mean(nets)),
                                "median_net": float(np.median(nets)),
                                "cumulative_net": float(np.sum(nets)),
                                "win_rate": float(np.mean(nets > 0)),
                                "sample_flag": sample_flag(len(nets)),
                                "profit_factor": (
                                    float(nets[nets > 0].sum() / abs(nets[nets < 0].sum()))
                                    if np.any(nets > 0) and np.any(nets < 0)
                                    else None
                                ),
                            }
                        )
                    if pp:
                        mid = pp[len(pp) // 2]["entry_time"]
                        for half, subset in (
                            ("FIRST_HALF", [p for p in pp if p["entry_time"] <= mid]),
                            ("SECOND_HALF", [p for p in pp if p["entry_time"] > mid]),
                        ):
                            nets, exits, holds = run_combo_on_paths(
                                subset, tp=tp, sl=sl, policy="SL_FIRST"
                            )
                            monthly_rows.append(
                                summarize_trades(
                                    nets,
                                    exits,
                                    holds,
                                    symbol=sym,
                                    timeframe=tf,
                                    tier=tier,
                                    tp_pct=tp,
                                    sl_pct=sl,
                                    period=half,
                                )
                            )

    # tier comparison vs ALL on reference combos
    for r in ref_rows:
        if r.get("tier") == "ALL" or r.get("side") != "COMBINED":
            continue
        base = next(
            (
                x
                for x in ref_rows
                if x.get("symbol") == r.get("symbol")
                and x.get("timeframe") == r.get("timeframe")
                and x.get("tier") == "ALL"
                and x.get("side") == "COMBINED"
                and x.get("tp_pct") == r.get("tp_pct")
                and x.get("sl_pct") == r.get("sl_pct")
            ),
            None,
        )
        if not base:
            continue
        tier_comp.append(
            {
                "symbol": r.get("symbol"),
                "timeframe": r.get("timeframe"),
                "tier": r.get("tier"),
                "tp_pct": r.get("tp_pct"),
                "sl_pct": r.get("sl_pct"),
                "n_tier": r.get("n"),
                "n_all": base.get("n"),
                "expectancy_lift": (r.get("expectancy") or 0) - (base.get("expectancy") or 0),
                "pf_tier": r.get("profit_factor"),
                "pf_all": base.get("profit_factor"),
                "dd_tier": r.get("max_drawdown"),
                "dd_all": base.get("max_drawdown"),
                "tp_rate_lift": (r.get("tp_rate") or 0) - (base.get("tp_rate") or 0),
            }
        )

    # cross-symbol consistency on reference Tier A/ALL
    for tf in SIGNAL_TFS:
        for tier in ("ALL", "A", "B"):
            for tp, sl in REFERENCE_COMBOS:
                doge = next(
                    (
                        r
                        for r in ref_rows
                        if r.get("symbol") == "DOGEUSDT"
                        and r.get("timeframe") == tf
                        and r.get("tier") == tier
                        and r.get("side") == "COMBINED"
                        and r.get("tp_pct") == tp
                        and r.get("sl_pct") == sl
                    ),
                    None,
                )
                btc = next(
                    (
                        r
                        for r in ref_rows
                        if r.get("symbol") == "BTCUSDT"
                        and r.get("timeframe") == tf
                        and r.get("tier") == tier
                        and r.get("side") == "COMBINED"
                        and r.get("tp_pct") == tp
                        and r.get("sl_pct") == sl
                    ),
                    None,
                )
                if not doge or not btc:
                    continue

                def pos(r: dict) -> bool:
                    return (
                        (r.get("expectancy") or 0) > 0
                        and (r.get("profit_factor") or 0) > 1
                        and r.get("sample_flag") == "OK"
                    )

                dp, bp = pos(doge), pos(btc)
                if dp and bp:
                    tag = "REPLICATES_POSITIVE"
                elif dp != bp:
                    tag = "MIXED"
                elif not dp and not bp:
                    tag = "CONTRADICTS"
                else:
                    tag = "MIXED"
                cross_rows.append(
                    {
                        "timeframe": tf,
                        "tier": tier,
                        "tp_pct": tp,
                        "sl_pct": sl,
                        "DOGE_expectancy": doge.get("expectancy"),
                        "BTC_expectancy": btc.get("expectancy"),
                        "DOGE_pf": doge.get("profit_factor"),
                        "BTC_pf": btc.get("profit_factor"),
                        "DOGE_dd": doge.get("max_drawdown"),
                        "BTC_dd": btc.get("max_drawdown"),
                        "consistency": tag,
                    }
                )

    primary = decide_wide_tp(reach_rows, ref_rows)
    large_dec = decide_large_tp(reach_rows, mfe_rows, ref_rows)
    tier_dec = decide_tier_a(ref_rows)
    recs = recommend_ranges(reach_rows, mfe_rows, ref_rows)

    # reach summary 1/2/4/6
    reach_summary = []
    for tf in SIGNAL_TFS:
        for lvl in (1.0, 2.0, 4.0, 6.0):
            for sym in SYMBOLS:
                r = next(
                    (
                        x
                        for x in reach_rows
                        if x.get("symbol") == sym
                        and x.get("timeframe") == tf
                        and x.get("tier") == "A"
                        and x.get("level_pct") == lvl
                        and x.get("side") == "COMBINED"
                    ),
                    None,
                )
                if r:
                    reach_summary.append(r)

    return {
        "audit_version": AUDIT_VERSION,
        "fee_pct": FEE_PCT,
        "method": METHOD_DOC.strip(),
        "signal_tiers": signal_tiers,
        "coverage": coverage_rows,
        "tpsl_grid": grid_rows,
        "tpsl_reference_combos": ref_rows,
        "tier_comparison": tier_comp,
        "tp_reachability": reach_rows,
        "mfe_mae_by_tier": mfe_rows,
        "large_move_matrix": large_rows,
        "tpsl_frontier": frontier_rows,
        "long_short_results": long_short_rows,
        "monthly_stability": monthly_rows,
        "cross_symbol_consistency": cross_rows,
        "short_horizon_sensitivity": short_h_rows,
        "reach_summary_tier_a": reach_summary,
        "tf_recommendations": recs,
        "decisions": {
            "primary": primary,
            "tier": tier_dec,
            "large_tp": large_dec,
        },
        "max_hold_min": dict(MAX_HOLD_MIN),
        "tp_grid": list(TP_GRID),
        "sl_grid": list(SL_GRID),
    }
