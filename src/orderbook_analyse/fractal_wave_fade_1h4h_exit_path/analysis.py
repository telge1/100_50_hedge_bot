"""Orchestrate 1h/4h Tier-A exit/path research."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_failure_multitimeframe.outcomes import load_1m
from orderbook_analyse.fractal_wave_fade_1h4h_exit_path import (
    ADV_LEVELS,
    AUDIT_VERSION,
    FAV_LEVELS,
    FEE_PCT,
    FEE_SEMANTICS,
    MAX_HOLD_MIN,
    METHOD_DOC,
    REFERENCE_SINGLE,
    SCALEOUT_SPECS,
    SIGNAL_TFS,
    SINGLE_GRID,
    SYMBOLS,
)
from orderbook_analyse.fractal_wave_fade_1h4h_exit_path.simulate import (
    build_paths,
    giveback_for_trades,
    load_tier_a_events,
    path_metrics,
    resolve_entry_indices,
    sample_flag,
    simulate_scaleout,
    simulate_single_tpsl,
    summarize_trade_list,
    target_before_adverse,
)


def _filter(paths: list[dict], *, side: str | None = None, symbol: str | None = None) -> list[dict]:
    out = []
    for p in paths:
        if symbol is not None and p["symbol"] != symbol:
            continue
        if side is not None and side != "COMBINED" and p["side"] != side:
            continue
        out.append(p)
    return out


def _consistency(doge: dict | None, btc: dict | None) -> str:
    if not doge or not btc:
        return "INSUFFICIENT"
    def ok(r: dict) -> bool:
        return (
            (r.get("expectancy") or 0) > 0
            and (r.get("profit_factor") or 0) > 1
            and r.get("sample_flag") == "OK"
        )
    do, bo = ok(doge), ok(btc)
    if do and bo:
        return "REPLICATES_POSITIVE"
    if do != bo:
        return "MIXED"
    return "CONTRADICTS"


def run_analysis() -> dict[str, Any]:
    print(METHOD_DOC, flush=True)
    events = load_tier_a_events()
    print(f"[events Tier A 1h/4h] n={len(events)}", flush=True)

    path_rows: list[dict] = []
    tba_rows: list[dict] = []
    single_rows: list[dict] = []
    scale_rows: list[dict] = []
    runner_rows: list[dict] = []
    capture_rows: list[dict] = []
    giveback_rows: list[dict] = []
    long_short_rows: list[dict] = []
    cross_rows: list[dict] = []
    stability_rows: list[dict] = []
    comparison_rows: list[dict] = []

    # cache trades per (tf, variant_key, symbol, side) for giveback / stability
    trade_cache: dict[tuple, list[dict]] = {}

    all_paths_by_tf: dict[str, list[dict]] = {tf: [] for tf in SIGNAL_TFS}

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
            max_hold = MAX_HOLD_MIN[tf]
            paths = build_paths(
                sub,
                high=high,
                low=low,
                close=close,
                open_times=open_times,
                max_hold_min=max_hold,
            )
            paths = sorted(paths, key=lambda p: p["entry_time"])
            print(f"[path] valid={len(paths)} max_hold={max_hold}", flush=True)
            all_paths_by_tf[tf].extend(paths)

            # path metrics + target-before-adverse
            for p in paths:
                path_rows.append(path_metrics(p))
            for side in ("COMBINED", "LONG", "SHORT"):
                pp = _filter(paths, side=side)
                for tgt in FAV_LEVELS:
                    for adv in ADV_LEVELS:
                        if tgt < adv:  # skip silly tiny target vs large adverse? keep all
                            pass
                        n = len(pp)
                        ok = sum(1 for p in pp if target_before_adverse(p, tgt, adv))
                        tba_rows.append(
                            {
                                "symbol": sym,
                                "timeframe": tf,
                                "side": side,
                                "target_pct": tgt,
                                "max_allowed_adverse_pct": adv,
                                "n": n,
                                "success_rate": ok / n if n else None,
                                "success_count": ok,
                                "sample_flag": sample_flag(n),
                            }
                        )

            # single TP/SL grid
            tps = SINGLE_GRID[tf]["tp"]
            sls = SINGLE_GRID[tf]["sl"]
            refs = set(REFERENCE_SINGLE[tf])
            for side in ("COMBINED", "LONG", "SHORT"):
                pp = _filter(paths, side=side)
                for tp in tps:
                    for sl in sls:
                        trades = [
                            simulate_single_tpsl(p, tp_pct=tp, sl_pct=sl)
                            for p in pp
                        ]
                        key = (tf, f"SINGLE_TP{tp:g}_SL{sl:g}", sym, side)
                        trade_cache[key] = trades
                        row = summarize_trade_list(
                            trades,
                            symbol=sym,
                            timeframe=tf,
                            side=side,
                            variant=f"SINGLE_TP{tp:g}_SL{sl:g}",
                            variant_family="SINGLE",
                            tp_pct=tp,
                            sl_pct=sl,
                            is_reference=(tp, sl) in refs,
                            max_hold_min=max_hold,
                        )
                        single_rows.append(row)
                        if side != "COMBINED":
                            long_short_rows.append(row)
                        capture_rows.append(
                            {
                                "symbol": sym,
                                "timeframe": tf,
                                "side": side,
                                "variant": row["variant"],
                                "n": row["n"],
                                "median_capture_ratio": row.get("median_capture_ratio"),
                                "q25_capture_ratio": row.get("q25_capture_ratio"),
                                "q75_capture_ratio": row.get("q75_capture_ratio"),
                                "expectancy": row.get("expectancy"),
                            }
                        )
                        for mmin in (2.0, 4.0, 6.0):
                            giveback_rows.append(
                                giveback_for_trades(
                                    trades,
                                    mmin,
                                    symbol=sym,
                                    timeframe=tf,
                                    side=side,
                                    variant=row["variant"],
                                )
                            )

            # scale-out + runner
            for vname, by_tf in SCALEOUT_SPECS.items():
                spec = by_tf[tf]
                for side in ("COMBINED", "LONG", "SHORT"):
                    pp = _filter(paths, side=side)
                    trades = [
                        simulate_scaleout(
                            p,
                            legs=spec["legs"],
                            sl_pct=spec["sl"],
                            be_after_first_tp=spec["be_after_first_tp"],
                        )
                        for p in pp
                    ]
                    key = (tf, vname, sym, side)
                    trade_cache[key] = trades
                    row = summarize_trade_list(
                        trades,
                        symbol=sym,
                        timeframe=tf,
                        side=side,
                        variant=vname,
                        variant_family="RUNNER" if vname == "RUNNER" else "SCALEOUT",
                        sl_pct=spec["sl"],
                        be_after_first_tp=spec["be_after_first_tp"],
                        legs=str(spec["legs"]),
                        max_hold_min=max_hold,
                    )
                    if vname == "RUNNER":
                        runner_rows.append(row)
                    else:
                        scale_rows.append(row)
                    if side != "COMBINED":
                        long_short_rows.append(row)
                    capture_rows.append(
                        {
                            "symbol": sym,
                            "timeframe": tf,
                            "side": side,
                            "variant": vname,
                            "n": row["n"],
                            "median_capture_ratio": row.get("median_capture_ratio"),
                            "q25_capture_ratio": row.get("q25_capture_ratio"),
                            "q75_capture_ratio": row.get("q75_capture_ratio"),
                            "expectancy": row.get("expectancy"),
                        }
                    )
                    for mmin in (2.0, 4.0, 6.0):
                        giveback_rows.append(
                            giveback_for_trades(
                                trades,
                                mmin,
                                symbol=sym,
                                timeframe=tf,
                                side=side,
                                variant=vname,
                            )
                        )

    # COMBINED symbols pool
    print("\n===== COMBINED symbols =====", flush=True)
    for tf in SIGNAL_TFS:
        paths = sorted(all_paths_by_tf[tf], key=lambda p: p["entry_time"])
        max_hold = MAX_HOLD_MIN[tf]
        for side in ("COMBINED",):
            pp = _filter(paths, side=side)
            # key TBA for F question already per symbol; add pooled TBA for 4h key probs
            for tgt in (4.0, 6.0, 8.0):
                for adv in (2.0, 3.0):
                    n = len(pp)
                    ok = sum(1 for p in pp if target_before_adverse(p, tgt, adv))
                    tba_rows.append(
                        {
                            "symbol": "COMBINED",
                            "timeframe": tf,
                            "side": side,
                            "target_pct": tgt,
                            "max_allowed_adverse_pct": adv,
                            "n": n,
                            "success_rate": ok / n if n else None,
                            "success_count": ok,
                            "sample_flag": sample_flag(n),
                        }
                    )
            for tp, sl in REFERENCE_SINGLE[tf]:
                trades = [simulate_single_tpsl(p, tp_pct=tp, sl_pct=sl) for p in pp]
                row = summarize_trade_list(
                    trades,
                    symbol="COMBINED",
                    timeframe=tf,
                    side=side,
                    variant=f"SINGLE_TP{tp:g}_SL{sl:g}",
                    variant_family="SINGLE",
                    tp_pct=tp,
                    sl_pct=sl,
                    is_reference=True,
                    max_hold_min=max_hold,
                )
                single_rows.append(row)
            for vname, by_tf in SCALEOUT_SPECS.items():
                spec = by_tf[tf]
                trades = [
                    simulate_scaleout(
                        p,
                        legs=spec["legs"],
                        sl_pct=spec["sl"],
                        be_after_first_tp=spec["be_after_first_tp"],
                    )
                    for p in pp
                ]
                row = summarize_trade_list(
                    trades,
                    symbol="COMBINED",
                    timeframe=tf,
                    side=side,
                    variant=vname,
                    variant_family="RUNNER" if vname == "RUNNER" else "SCALEOUT",
                    sl_pct=spec["sl"],
                    be_after_first_tp=spec["be_after_first_tp"],
                    max_hold_min=max_hold,
                )
                if vname == "RUNNER":
                    runner_rows.append(row)
                else:
                    scale_rows.append(row)

    # cross-symbol on fixed variants (COMBINED side)
    fixed_variants_by_tf: dict[str, list[str]] = {}
    for tf in SIGNAL_TFS:
        names = [f"SINGLE_TP{tp:g}_SL{sl:g}" for tp, sl in REFERENCE_SINGLE[tf]]
        names += list(SCALEOUT_SPECS.keys())
        fixed_variants_by_tf[tf] = names

    for tf in SIGNAL_TFS:
        for vname in fixed_variants_by_tf[tf]:
            doge = next(
                (
                    r
                    for r in (single_rows + scale_rows + runner_rows)
                    if r.get("symbol") == "DOGEUSDT"
                    and r.get("timeframe") == tf
                    and r.get("side") == "COMBINED"
                    and r.get("variant") == vname
                ),
                None,
            )
            btc = next(
                (
                    r
                    for r in (single_rows + scale_rows + runner_rows)
                    if r.get("symbol") == "BTCUSDT"
                    and r.get("timeframe") == tf
                    and r.get("side") == "COMBINED"
                    and r.get("variant") == vname
                ),
                None,
            )
            tag = _consistency(doge, btc)
            cross_rows.append(
                {
                    "timeframe": tf,
                    "variant": vname,
                    "DOGE_expectancy": None if not doge else doge.get("expectancy"),
                    "BTC_expectancy": None if not btc else btc.get("expectancy"),
                    "DOGE_pf": None if not doge else doge.get("profit_factor"),
                    "BTC_pf": None if not btc else btc.get("profit_factor"),
                    "DOGE_dd": None if not doge else doge.get("max_drawdown"),
                    "BTC_dd": None if not btc else btc.get("max_drawdown"),
                    "DOGE_capture": None if not doge else doge.get("median_capture_ratio"),
                    "BTC_capture": None if not btc else btc.get("median_capture_ratio"),
                    "DOGE_n": None if not doge else doge.get("n"),
                    "BTC_n": None if not btc else btc.get("n"),
                    "consistency": tag,
                }
            )

    # time stability: monthly for 1h, quarterly for 4h — reference singles + S1/S2/RUNNER
    stab_variants = {
        "1h": [
            "SINGLE_TP2_SL1.5",
            "SINGLE_TP3_SL2",
            "S1",
            "S2",
            "S3",
            "S4",
            "RUNNER",
        ],
        "4h": [
            "SINGLE_TP4_SL2",
            "SINGLE_TP6_SL3",
            "S1",
            "S2",
            "S3",
            "S4",
            "RUNNER",
        ],
    }
    for tf in SIGNAL_TFS:
        period = "M" if tf == "1h" else "Q"
        for sym in SYMBOLS:
            for vname in stab_variants[tf]:
                trades = trade_cache.get((tf, vname, sym, "COMBINED"))
                if not trades:
                    continue
                # attach entry times from paths — re-simulate with times from cache order
                # trade_cache trades lack entry_time; rebuild lightly
                paths = [
                    p
                    for p in all_paths_by_tf[tf]
                    if p["symbol"] == sym
                ]
                paths = sorted(paths, key=lambda p: p["entry_time"])
                if vname.startswith("SINGLE_"):
                    # parse TP/SL
                    # SINGLE_TP2_SL1.5
                    rest = vname.replace("SINGLE_TP", "")
                    tp_s, sl_s = rest.split("_SL")
                    tp, sl = float(tp_s), float(sl_s)
                    paired = [
                        (p, simulate_single_tpsl(p, tp_pct=tp, sl_pct=sl)) for p in paths
                    ]
                else:
                    spec = SCALEOUT_SPECS[vname][tf]
                    paired = [
                        (
                            p,
                            simulate_scaleout(
                                p,
                                legs=spec["legs"],
                                sl_pct=spec["sl"],
                                be_after_first_tp=spec["be_after_first_tp"],
                            ),
                        )
                        for p in paths
                    ]
                buckets: dict[str, list[float]] = {}
                for p, t in paired:
                    ts = pd.Timestamp(p["entry_time"])
                    key = f"{ts.year}-Q{(ts.month - 1) // 3 + 1}" if period == "Q" else ts.strftime("%Y-%m")
                    buckets.setdefault(key, []).append(t["net"])
                for key, nets_l in sorted(buckets.items()):
                    nets = np.asarray(nets_l, dtype=float)
                    wins = nets[nets > 0]
                    losses = nets[nets < 0]
                    stability_rows.append(
                        {
                            "symbol": sym,
                            "timeframe": tf,
                            "variant": vname,
                            "period": key,
                            "period_type": period,
                            "n": int(len(nets)),
                            "expectancy": float(np.mean(nets)),
                            "cumulative_net": float(np.sum(nets)),
                            "profit_factor": (
                                float(np.sum(wins) / abs(np.sum(losses)))
                                if len(wins) and len(losses) and np.sum(losses) != 0
                                else None
                            ),
                            "sample_flag": sample_flag(len(nets)),
                        }
                    )

    # comparison table: BEST SINGLE (diag within refs) + S1-S4 + RUNNER
    for tf in SIGNAL_TFS:
        ref_names = [f"SINGLE_TP{tp:g}_SL{sl:g}" for tp, sl in REFERENCE_SINGLE[tf]]
        # pick best single by mean of DOGE+BTC expectancy among refs with both PF>1 if possible
        candidates = []
        for vn in ref_names:
            d = next(
                (
                    r
                    for r in single_rows
                    if r.get("symbol") == "DOGEUSDT"
                    and r.get("timeframe") == tf
                    and r.get("side") == "COMBINED"
                    and r.get("variant") == vn
                ),
                None,
            )
            b = next(
                (
                    r
                    for r in single_rows
                    if r.get("symbol") == "BTCUSDT"
                    and r.get("timeframe") == tf
                    and r.get("side") == "COMBINED"
                    and r.get("variant") == vn
                ),
                None,
            )
            if d and b:
                score = (d.get("expectancy") or -999) + (b.get("expectancy") or -999)
                candidates.append((score, vn, d, b))
        best_single = max(candidates, key=lambda x: x[0]) if candidates else None
        variants_cmp = []
        if best_single:
            variants_cmp.append(("BEST_SINGLE_REF", best_single[1], best_single[2], best_single[3]))
        for vn in ("S1", "S2", "S3", "S4", "RUNNER"):
            d = next(
                (
                    r
                    for r in (scale_rows + runner_rows)
                    if r.get("symbol") == "DOGEUSDT"
                    and r.get("timeframe") == tf
                    and r.get("side") == "COMBINED"
                    and r.get("variant") == vn
                ),
                None,
            )
            b = next(
                (
                    r
                    for r in (scale_rows + runner_rows)
                    if r.get("symbol") == "BTCUSDT"
                    and r.get("timeframe") == tf
                    and r.get("side") == "COMBINED"
                    and r.get("variant") == vn
                ),
                None,
            )
            if d and b:
                variants_cmp.append((vn, vn, d, b))

        for label, vn, d, b in variants_cmp:
            comparison_rows.append(
                {
                    "timeframe": tf,
                    "variant_label": label,
                    "variant": vn,
                    "DOGE_expectancy": d.get("expectancy"),
                    "BTC_expectancy": b.get("expectancy"),
                    "DOGE_pf": d.get("profit_factor"),
                    "BTC_pf": b.get("profit_factor"),
                    "DOGE_maxDD": d.get("max_drawdown"),
                    "BTC_maxDD": b.get("max_drawdown"),
                    "DOGE_median_hold": d.get("median_hold_min"),
                    "BTC_median_hold": b.get("median_hold_min"),
                    "DOGE_mfe_capture": d.get("median_capture_ratio"),
                    "BTC_mfe_capture": b.get("median_capture_ratio"),
                    "cross_symbol_status": _consistency(d, b),
                }
            )

    decisions = _decide(
        comparison_rows,
        single_rows,
        scale_rows,
        runner_rows,
        capture_rows,
        tba_rows,
        giveback_rows,
    )
    answers = _answers(tba_rows, comparison_rows, single_rows, scale_rows, runner_rows)

    return {
        "audit_version": AUDIT_VERSION,
        "fee_pct": FEE_PCT,
        "fee_semantics": FEE_SEMANTICS.strip(),
        "method": METHOD_DOC.strip(),
        "path_metrics": path_rows,
        "target_before_adverse": tba_rows,
        "single_tpsl_results": single_rows,
        "scaleout_results": scale_rows,
        "runner_results": runner_rows,
        "mfe_capture": capture_rows,
        "giveback_analysis": giveback_rows,
        "long_short_results": long_short_rows,
        "cross_symbol_comparison": cross_rows,
        "time_stability": stability_rows,
        "comparison_table": comparison_rows,
        "decisions": decisions,
        "answers": answers,
        "max_hold_min": dict(MAX_HOLD_MIN),
    }


def _decide(
    comparison_rows,
    single_rows,
    scale_rows,
    runner_rows,
    capture_rows,
    tba_rows,
    giveback_rows,
) -> dict[str, str]:
    # Primary: compare BEST_SINGLE vs best scale vs runner on cross-symbol Exp+PF
    def score_label(tf: str, label: str) -> float:
        rows = [
            r
            for r in comparison_rows
            if r["timeframe"] == tf and r["variant_label"] == label
        ]
        if not rows:
            return -999.0
        r = rows[0]
        s = 0.0
        if (r.get("DOGE_expectancy") or 0) > 0 and (r.get("DOGE_pf") or 0) > 1:
            s += float(r["DOGE_expectancy"])
        if (r.get("BTC_expectancy") or 0) > 0 and (r.get("BTC_pf") or 0) > 1:
            s += float(r["BTC_expectancy"])
        if r.get("cross_symbol_status") == "REPLICATES_POSITIVE":
            s += 0.05
        return s

    winners = []
    for tf in SIGNAL_TFS:
        scores = {
            "SINGLE": score_label(tf, "BEST_SINGLE_REF"),
            "SCALE": max(score_label(tf, v) for v in ("S1", "S2", "S3", "S4")),
            "RUNNER": score_label(tf, "RUNNER"),
        }
        winners.append(max(scores, key=scores.get))  # type: ignore[arg-type]

    if winners[0] == winners[1]:
        primary = {
            "SINGLE": "SINGLE_TP_BEST",
            "SCALE": "SCALE_OUT_BEST",
            "RUNNER": "RUNNER_STRUCTURE_BEST",
        }[winners[0]]
    else:
        primary = "EXIT_STRUCTURE_CONTEXT_DEPENDENT"

    # BE value: S2 vs S1 and S4 vs S3 on expectancy and DD
    be_votes = []
    for tf in SIGNAL_TFS:
        for a, b in (("S2", "S1"), ("S4", "S3")):
            for sym in SYMBOLS:
                ra = next(
                    (
                        r
                        for r in scale_rows
                        if r.get("timeframe") == tf
                        and r.get("symbol") == sym
                        and r.get("side") == "COMBINED"
                        and r.get("variant") == a
                    ),
                    None,
                )
                rb = next(
                    (
                        r
                        for r in scale_rows
                        if r.get("timeframe") == tf
                        and r.get("symbol") == sym
                        and r.get("side") == "COMBINED"
                        and r.get("variant") == b
                    ),
                    None,
                )
                if not ra or not rb:
                    continue
                # better if higher exp OR (similar exp and better DD)
                exp_d = (ra.get("expectancy") or 0) - (rb.get("expectancy") or 0)
                dd_d = (ra.get("max_drawdown") or 0) - (rb.get("max_drawdown") or 0)  # less neg better
                cap_d = (ra.get("median_capture_ratio") or 0) - (rb.get("median_capture_ratio") or 0)
                if exp_d > 0.02 and dd_d >= -1:
                    be_votes.append("helps")
                elif exp_d < -0.02 or cap_d < -0.05:
                    be_votes.append("hurts")
                else:
                    be_votes.append("mixed")
    if be_votes.count("helps") >= be_votes.count("hurts") + 2:
        be_dec = "BREAKEVEN_AFTER_TP1_ADDS_VALUE"
    elif be_votes.count("hurts") >= be_votes.count("helps") + 2:
        be_dec = "BREAKEVEN_AFTER_TP1_HURTS_RUNNERS"
    else:
        be_dec = "BREAKEVEN_AFTER_TP1_MIXED"

    # Monetizable: 4h P(+4 before -2) and capture on wide single
    tba_ok = False
    for sym in SYMBOLS:
        r = next(
            (
                x
                for x in tba_rows
                if x.get("symbol") == sym
                and x.get("timeframe") == "4h"
                and x.get("side") == "COMBINED"
                and x.get("target_pct") == 4.0
                and x.get("max_allowed_adverse_pct") == 2.0
            ),
            None,
        )
        if r and (r.get("success_rate") or 0) >= 0.35:
            tba_ok = True
    wide = [
        r
        for r in single_rows
        if r.get("side") == "COMBINED"
        and r.get("is_reference")
        and (r.get("tp_pct") or 0) >= 4
        and (r.get("expectancy") or 0) > 0
        and (r.get("profit_factor") or 0) > 1
    ]
    if tba_ok and len(wide) >= 4:
        mon = "MULTIPERCENT_SWINGS_ARE_MONETIZABLE"
    else:
        mon = "MULTIPERCENT_MFE_NOT_EASILY_MONETIZABLE"

    return {"primary": primary, "breakeven": be_dec, "monetizable": mon}


def _answers(tba_rows, comparison_rows, single_rows, scale_rows, runner_rows) -> dict[str, Any]:
    def get_cmp(tf, label):
        return next(
            (r for r in comparison_rows if r["timeframe"] == tf and r["variant_label"] == label),
            None,
        )

    def tba(sym, tf, tgt, adv):
        return next(
            (
                r
                for r in tba_rows
                if r.get("symbol") == sym
                and r.get("timeframe") == tf
                and r.get("side") == "COMBINED"
                and r.get("target_pct") == tgt
                and r.get("max_allowed_adverse_pct") == adv
            ),
            None,
        )

    out: dict[str, Any] = {}
    # A/B from comparison
    for tf, qkey, single_band in (
        ("1h", "A_1h_single_2_3_vs_scaleout", "2-3%"),
        ("4h", "B_4h_single_4_6_vs_scaleout", "4-6%"),
    ):
        bs = get_cmp(tf, "BEST_SINGLE_REF")
        scales = [get_cmp(tf, v) for v in ("S1", "S2", "S3", "S4")]
        scales = [s for s in scales if s]
        best_scale = max(
            scales,
            key=lambda r: (r.get("DOGE_expectancy") or 0) + (r.get("BTC_expectancy") or 0),
        ) if scales else None
        if bs and best_scale:
            s_score = (bs.get("DOGE_expectancy") or 0) + (bs.get("BTC_expectancy") or 0)
            c_score = (best_scale.get("DOGE_expectancy") or 0) + (
                best_scale.get("BTC_expectancy") or 0
            )
            out[qkey] = {
                "best_single": bs.get("variant"),
                "best_scale": best_scale.get("variant"),
                "single_sum_exp": s_score,
                "scale_sum_exp": c_score,
                "verdict": "SINGLE_BETTER" if s_score >= c_score else "SCALEOUT_BETTER",
            }

    # C BE
    out["C_breakeven"] = {}
    for tf in SIGNAL_TFS:
        for a, b in (("S2", "S1"), ("S4", "S3")):
            out["C_breakeven"][f"{tf}_{a}_vs_{b}"] = {
                sym: {
                    "exp_delta": (
                        next(
                            r
                            for r in scale_rows
                            if r["symbol"] == sym
                            and r["timeframe"] == tf
                            and r["side"] == "COMBINED"
                            and r["variant"] == a
                        ).get("expectancy")
                        - next(
                            r
                            for r in scale_rows
                            if r["symbol"] == sym
                            and r["timeframe"] == tf
                            and r["side"] == "COMBINED"
                            and r["variant"] == b
                        ).get("expectancy")
                    ),
                    "capture_delta": (
                        (next(
                            r
                            for r in scale_rows
                            if r["symbol"] == sym
                            and r["timeframe"] == tf
                            and r["side"] == "COMBINED"
                            and r["variant"] == a
                        ).get("median_capture_ratio") or 0)
                        - (next(
                            r
                            for r in scale_rows
                            if r["symbol"] == sym
                            and r["timeframe"] == tf
                            and r["side"] == "COMBINED"
                            and r["variant"] == b
                        ).get("median_capture_ratio") or 0)
                    ),
                }
                for sym in SYMBOLS
            }

    # D runner
    out["D_runner"] = {
        tf: get_cmp(tf, "RUNNER") for tf in SIGNAL_TFS
    }

    # F path probs 4h
    out["F_4h_path_order"] = {
        f"P(+{tgt:g}_before_-{adv:g})_{sym}": None
        if not (r := tba(sym, "4h", float(tgt), float(adv)))
        else r.get("success_rate")
        for sym in SYMBOLS
        for tgt, adv in ((4, 2), (6, 2), (6, 3))
    }

    # G doge vs btc — from cross
    out["G_note"] = "See cross_symbol_comparison.csv; DOGE usually higher Exp/capture, BTC shorter history."
    return out
