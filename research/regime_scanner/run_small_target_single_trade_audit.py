"""CLI: small-target single-trade exit audit (A6-Short vs STP B2×E1).

Frozen entry signals. Frozen 12 TP/SL combos. No A6/STP/runtime changes.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.candle_sources import load_regime_db_env_file
from research.regime_scanner.c35c_signal_store.path_store import C35cPathStore
from research.regime_scanner.mysql_candle_store.config import load_regime_db_config
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.small_target_single_trade.config import (
    A6_PARENT_LABEL,
    BASE_COST,
    EFFECTIVE_COSTS,
    EXIT_COMBOS,
    HORIZONS,
    PRIMARY_HORIZON,
    STRATEGY_A6,
    STRATEGY_STP,
    TP_VALUES,
    SL_VALUES,
    exit_combo_id,
    is_micro_target,
    matrix_rows,
)
from research.regime_scanner.small_target_single_trade.metrics import (
    MAJORS,
    TOP3,
    evaluate_gates,
    metrics_block,
    slice_pack,
)
from research.regime_scanner.small_target_single_trade.outcomes import (
    evaluate_outcome_params,
    short_tp_sl_prices,
)
from research.regime_scanner.small_target_single_trade.sequential import apply_sequential
from research.regime_scanner.small_target_single_trade.signal_sources import (
    attach_fill_bars,
    ensure_splits,
    load_a6_short_signals,
    load_frames_for_symbols,
    load_stp_b2e1_signals,
    parity_report,
)
from research.regime_scanner.trend_weakening_multi_bar_audit import assert_safe_output_dir

DEFAULT_OUT = Path("research/regime_scanner/results/small_target_single_trade_audit_20260722")
DEFAULT_ENV = Path(
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/"
    "research/regime_scanner/.env.regime_db"
)
DEFAULT_STP = Path("research/regime_scanner/results/short_trend_pullback_v1_20260722")


def _enrich_slices(df: pd.DataFrame, base: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    if df.empty:
        for k in (
            "dev_expectation",
            "validation_expectation",
            "oos_expectation",
            "common_window_expectation",
            "without_apt_expectation",
            "without_top1_expectation",
            "without_top3_expectation",
            "majors_expectation",
            "altcoins_expectation",
        ):
            out[k] = None
        return out
    for sp in ("dev", "validation", "oos"):
        out[f"{sp}_expectation"] = metrics_block(df[df.split.astype(str) == sp])["expectation"]
    # common window
    d = df.copy()
    d["day"] = pd.to_datetime(d["fill_timestamp"], utc=True).dt.floor("D")
    nsym = d["symbol"].nunique()
    day_n = d.groupby("day")["symbol"].nunique()
    keep = set(day_n[day_n >= max(2, nsym // 2)].index)
    out["common_window_expectation"] = metrics_block(d[d["day"].isin(keep)])["expectation"]
    out["without_apt_expectation"] = metrics_block(d[d.symbol != "APTUSDT"])["expectation"]
    cex = d.groupby(d.symbol.astype(str))["net_pnl_pct"].mean()
    top1 = cex.idxmax() if len(cex) else None
    out["without_top1_expectation"] = (
        metrics_block(d[d.symbol != top1])["expectation"] if top1 else None
    )
    out["without_top3_expectation"] = metrics_block(d[~d.symbol.isin(TOP3)])["expectation"]
    out["majors_expectation"] = metrics_block(d[d.symbol.isin(MAJORS)])["expectation"]
    out["altcoins_expectation"] = metrics_block(d[~d.symbol.isin(MAJORS)])["expectation"]
    return out


def build_outcomes_for_symbol(
    signals: pd.DataFrame,
    frame: pd.DataFrame,
    *,
    tp_values: tuple[float, ...],
    sl_values: tuple[float, ...],
    horizons: tuple[int, ...],
    costs: tuple[float, ...],
) -> list[dict[str, Any]]:
    highs = frame["high"].to_numpy(dtype=float)
    lows = frame["low"].to_numpy(dtype=float)
    closes = frame["close"].to_numpy(dtype=float)
    timestamps = list(pd.to_datetime(frame["timestamp"], utc=True))
    n = len(frame)
    rows: list[dict[str, Any]] = []
    for _, sig in signals.iterrows():
        fill_i = int(sig["fill_bar"])
        if fill_i < 0 or fill_i >= n:
            continue
        entry = float(sig["entry_price"])
        for tp in tp_values:
            for sl_mag in sl_values:
                sl = -float(sl_mag)
                tp_px, sl_px = short_tp_sl_prices(entry, tp, sl_mag)
                for h in horizons:
                    # compute once at base semantics, then apply costs
                    base = evaluate_outcome_params(
                        side=-1,
                        entry=entry,
                        highs=highs,
                        lows=lows,
                        closes=closes,
                        timestamps=timestamps,
                        fill_i=fill_i,
                        n_bars=n,
                        tp_pct=float(tp),
                        sl_pct=float(sl),
                        horizon_bars=int(h),
                        cost_pct=0.0,
                    )
                    for cost in costs:
                        net = float(base["gross_pnl_pct"]) - float(cost)
                        rows.append(
                            {
                                "strategy_source": sig["strategy_source"],
                                "signal_id": sig["signal_id"],
                                "signal_key": sig.get("signal_key"),
                                "symbol": sig["symbol"],
                                "split": sig.get("split"),
                                "trigger_timestamp": sig["trigger_timestamp"],
                                "fill_timestamp": sig["fill_timestamp"],
                                "entry_price": entry,
                                "tp_pct": float(tp),
                                "sl_pct": float(sl),
                                "sl_magnitude_pct": float(sl_mag),
                                "combo_id": exit_combo_id(tp, sl_mag),
                                "micro_target_diagnostic": is_micro_target(tp),
                                "horizon_bars": int(h),
                                "effective_cost_pct": float(cost),
                                "tp_price": tp_px,
                                "sl_price": sl_px,
                                **{k: base[k] for k in base if k != "net_pnl_pct" and k != "is_winner"},
                                "net_pnl_pct": net,
                                "is_winner": net > 0,
                            }
                        )
    return rows


def summarize_mode(trades: pd.DataFrame, *, mode: str) -> list[dict[str, Any]]:
    rows = []
    if trades.empty:
        return rows
    group_cols = ["strategy_source", "combo_id", "tp_pct", "sl_magnitude_pct", "horizon_bars", "effective_cost_pct"]
    for keys, g in trades.groupby(group_cols, sort=False):
        pack = slice_pack(g)
        pack = _enrich_slices(g, pack)
        row = {
            "mode": mode,
            "strategy_source": keys[0],
            "combo_id": keys[1],
            "tp_pct": keys[2],
            "sl_magnitude_pct": keys[3],
            "horizon_bars": keys[4],
            "effective_cost_pct": keys[5],
            **pack,
        }
        rows.append(row)
    return rows


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--a6-parent-run-label", default=A6_PARENT_LABEL)
    p.add_argument("--stp-results-dir", type=Path, default=DEFAULT_STP)
    p.add_argument("--stp-context", default="B2")
    p.add_argument("--stp-trigger", default="E1")
    p.add_argument("--data-source", default="mysql", choices=["mysql"])
    p.add_argument("--regime-db-env", type=Path, default=DEFAULT_ENV)
    p.add_argument("--tp-values", nargs="+", type=float, default=list(TP_VALUES))
    p.add_argument("--sl-values", nargs="+", type=float, default=list(SL_VALUES))
    p.add_argument("--horizons", nargs="+", type=int, default=list(HORIZONS))
    p.add_argument("--effective-costs", nargs="+", type=float, default=list(EFFECTIVE_COSTS))
    p.add_argument("--same-bar-policy", default="conservative_sl")
    p.add_argument("--independent", action="store_true", default=True)
    p.add_argument("--sequential", action="store_true", default=True)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    args = p.parse_args(argv)

    assert_safe_output_dir(args.output_dir)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    tp_values = tuple(float(x) for x in args.tp_values)
    sl_values = tuple(float(x) for x in args.sl_values)
    horizons = tuple(int(x) for x in args.horizons)
    costs = tuple(float(x) for x in args.effective_costs)
    if len(tp_values) * len(sl_values) != 12:
        raise SystemExit(f"expected 12 exit combos, got {len(tp_values)*len(sl_values)}")
    if set(tp_values) != set(TP_VALUES) or set(sl_values) != set(SL_VALUES):
        raise SystemExit("TP/SL values must match frozen matrix exactly")

    load_regime_db_env_file(Path(args.regime_db_env))
    store = C35cPathStore(load_regime_db_config())

    pd.DataFrame(matrix_rows()).to_csv(out_dir / "exit_matrix_definitions.csv", index=False)

    print("Loading A6 short…", flush=True)
    a6 = load_a6_short_signals(store, parent_label=args.a6_parent_run_label)
    print(f"A6 short n={len(a6)}", flush=True)
    print("Loading STP B2×E1…", flush=True)
    stp = load_stp_b2e1_signals(args.stp_results_dir, context=args.stp_context, trigger=args.stp_trigger)
    print(f"STP B2E1 n={len(stp)}", flush=True)

    # parity abort thresholds
    if abs(len(stp) - 661) > 5:
        raise SystemExit(f"STP B2E1 count mismatch: {len(stp)} vs expected ~661")
    if abs(len(a6) - 603) > 30:
        raise SystemExit(f"A6 short count unexpected: {len(a6)} vs ~603")

    symbols = sorted(set(a6["symbol"].tolist()) | set(stp["symbol"].tolist()))
    print(f"Loading {len(symbols)} frames…", flush=True)
    frames = load_frames_for_symbols(symbols)

    # attach fill bars + splits
    a6_parts = []
    stp_parts = []
    for sym in symbols:
        frame, meta, a0, a1 = frames[sym]
        if len(a6[a6.symbol == sym]):
            part = attach_fill_bars(a6[a6.symbol == sym].copy(), frame)
            part = ensure_splits(part, a0, a1)
            a6_parts.append(part)
        if len(stp[stp.symbol == sym]):
            part = attach_fill_bars(stp[stp.symbol == sym].copy(), frame)
            part = ensure_splits(part, a0, a1)
            stp_parts.append(part)
    a6 = pd.concat(a6_parts, ignore_index=True) if a6_parts else a6
    stp = pd.concat(stp_parts, ignore_index=True) if stp_parts else stp

    parity = parity_report(a6, stp)
    pd.DataFrame(parity).to_csv(out_dir / "signal_source_parity.csv", index=False)
    # abort on bad entry parity
    for src, df in ((STRATEGY_A6, a6), (STRATEGY_STP, stp)):
        miss = int((df["fill_bar"] < 0).sum())
        match = float(np.mean(df["entry_matches_open"])) if len(df) else 0
        if miss > 0 or match < 0.99:
            raise SystemExit(f"parity fail {src}: missing_fill={miss} entry_match={match}")

    # overlap diagnostics
    a6_keys = set(zip(a6.symbol, pd.to_datetime(a6.fill_timestamp, utc=True).dt.floor("min")))
    stp_keys = set(zip(stp.symbol, pd.to_datetime(stp.fill_timestamp, utc=True).dt.floor("min")))
    overlap_rows = [
        {
            "exact_min_overlap": len(a6_keys & stp_keys),
            "a6_n": len(a6_keys),
            "stp_n": len(stp_keys),
            "overlap_rate_vs_a6": len(a6_keys & stp_keys) / len(a6_keys) if a6_keys else None,
            "overlap_rate_vs_stp": len(a6_keys & stp_keys) / len(stp_keys) if stp_keys else None,
        }
    ]
    # ±1 / ±4 bar overlap approx via minute offsets
    def near_overlap(minutes: int) -> int:
        n = 0
        stp_map = {}
        for sym, ts in stp_keys:
            stp_map.setdefault(sym, []).append(ts)
        for sym, ts in a6_keys:
            for t2 in stp_map.get(sym, []):
                if abs((ts - t2).total_seconds()) <= minutes * 60:
                    n += 1
                    break
        return n

    overlap_rows[0]["overlap_within_1_bar"] = near_overlap(15)
    overlap_rows[0]["overlap_within_4_bars"] = near_overlap(60)
    pd.DataFrame(overlap_rows).to_csv(out_dir / "signal_overlap.csv", index=False)

    # outcomes
    all_rows: list[dict[str, Any]] = []
    for sym in symbols:
        frame, *_ = frames[sym]
        for df in (a6[a6.symbol == sym], stp[stp.symbol == sym]):
            if df.empty:
                continue
            t0 = time.time()
            rows = build_outcomes_for_symbol(
                df, frame, tp_values=tp_values, sl_values=sl_values, horizons=horizons, costs=costs
            )
            all_rows.extend(rows)
            print(f"{sym} {df.strategy_source.iloc[0]}: {len(rows)} outcomes in {time.time()-t0:.1f}s", flush=True)

    trades = pd.DataFrame(all_rows)
    trades.to_csv(out_dir / "trade_level_outcomes.csv", index=False)
    print(f"total outcome rows: {len(trades)}", flush=True)

    # independent = all
    indep = trades
    seq = apply_sequential(trades)
    seq_taken = seq[seq["taken_sequential"]].copy()

    indep_sum = summarize_mode(indep, mode="independent")
    seq_sum = summarize_mode(seq_taken, mode="sequential")
    pd.DataFrame(indep_sum).to_csv(out_dir / "independent_global_summary.csv", index=False)
    pd.DataFrame(seq_sum).to_csv(out_dir / "sequential_global_summary.csv", index=False)

    # primary H192 / cost 0.20 views
    primary_i = indep[(indep.horizon_bars == PRIMARY_HORIZON) & (indep.effective_cost_pct == BASE_COST)]
    primary_s = seq_taken[(seq_taken.horizon_bars == PRIMARY_HORIZON) & (seq_taken.effective_cost_pct == BASE_COST)]

    def dump_slices(df: pd.DataFrame, prefix: str) -> None:
        # by strategy
        rows = []
        for (src, combo), g in df.groupby(["strategy_source", "combo_id"]):
            rows.append({"strategy_source": src, "combo_id": combo, **_enrich_slices(g, slice_pack(g))})
        pd.DataFrame(rows).to_csv(out_dir / f"{prefix}_by_strategy_combo.csv", index=False)

    # required summaries on primary independent
    pd.DataFrame(
        [
            {"strategy_source": src, **_enrich_slices(g, slice_pack(g))}
            for src, g in primary_i.groupby("strategy_source")
        ]
    ).to_csv(out_dir / "summary_by_strategy.csv", index=False)

    combo_rows = []
    for (src, combo, tp, sl), g in primary_i.groupby(
        ["strategy_source", "combo_id", "tp_pct", "sl_magnitude_pct"]
    ):
        combo_rows.append(
            {
                "strategy_source": src,
                "combo_id": combo,
                "tp_pct": tp,
                "sl_magnitude_pct": sl,
                **_enrich_slices(g, slice_pack(g)),
            }
        )
    pd.DataFrame(combo_rows).to_csv(out_dir / "summary_by_exit_combo.csv", index=False)

    coin_rows = []
    for (src, combo, sym), g in primary_i.groupby(["strategy_source", "combo_id", "symbol"]):
        coin_rows.append({"strategy_source": src, "combo_id": combo, "symbol": sym, **metrics_block(g)})
    pd.DataFrame(coin_rows).to_csv(out_dir / "summary_by_coin.csv", index=False)

    eq_rows = []
    for (src, combo), g in primary_i.groupby(["strategy_source", "combo_id"]):
        sp = slice_pack(g)
        eq_rows.append(
            {
                "strategy_source": src,
                "combo_id": combo,
                "equal_coin_expectation": sp["equal_coin_expectation"],
                "median_coin_expectation": sp["median_coin_expectation"],
                "pct_coins_positive": sp["pct_coins_positive"],
            }
        )
    pd.DataFrame(eq_rows).to_csv(out_dir / "summary_equal_coin.csv", index=False)
    pd.DataFrame(eq_rows).to_csv(out_dir / "summary_median_coin.csv", index=False)

    split_rows = []
    for (src, combo, sp), g in primary_i.groupby(["strategy_source", "combo_id", "split"]):
        split_rows.append({"strategy_source": src, "combo_id": combo, "split": sp, **metrics_block(g)})
    pd.DataFrame(split_rows).to_csv(out_dir / "summary_by_split.csv", index=False)

    # common / without apt / top1 / top3 / majors
    def write_filter(name: str, fn) -> None:
        rows = []
        for (src, combo), g in primary_i.groupby(["strategy_source", "combo_id"]):
            rows.append({"strategy_source": src, "combo_id": combo, **metrics_block(fn(g))})
        pd.DataFrame(rows).to_csv(out_dir / name, index=False)

    def cw(g):
        d = g.copy()
        d["day"] = pd.to_datetime(d["fill_timestamp"], utc=True).dt.floor("D")
        nsym = d.symbol.nunique()
        days = d.groupby("day").symbol.nunique()
        keep = set(days[days >= max(2, nsym // 2)].index)
        return d[d.day.isin(keep)]

    write_filter("summary_common_window.csv", cw)
    write_filter("summary_without_apt.csv", lambda g: g[g.symbol != "APTUSDT"])
    write_filter("summary_without_top3.csv", lambda g: g[~g.symbol.isin(TOP3)])

    top1_rows = []
    for (src, combo), g in primary_i.groupby(["strategy_source", "combo_id"]):
        cex = g.groupby(g.symbol.astype(str)).net_pnl_pct.mean()
        top1 = cex.idxmax() if len(cex) else None
        top1_rows.append(
            {
                "strategy_source": src,
                "combo_id": combo,
                "excluded": top1,
                **metrics_block(g[g.symbol != top1] if top1 else g),
            }
        )
    pd.DataFrame(top1_rows).to_csv(out_dir / "summary_without_top1.csv", index=False)

    maj_rows = []
    for (src, combo), g in primary_i.groupby(["strategy_source", "combo_id"]):
        maj_rows.append({"strategy_source": src, "combo_id": combo, "bucket": "majors", **metrics_block(g[g.symbol.isin(MAJORS)])})
        maj_rows.append({"strategy_source": src, "combo_id": combo, "bucket": "altcoins", **metrics_block(g[~g.symbol.isin(MAJORS)])})
    pd.DataFrame(maj_rows).to_csv(out_dir / "summary_majors_vs_altcoins.csv", index=False)

    month_rows = []
    pi = primary_i.copy()
    pi["month"] = pd.to_datetime(pi.fill_timestamp, utc=True).dt.to_period("M").astype(str)
    for (src, combo, month), g in pi.groupby(["strategy_source", "combo_id", "month"]):
        month_rows.append({"strategy_source": src, "combo_id": combo, "month": month, **metrics_block(g)})
    pd.DataFrame(month_rows).to_csv(out_dir / "summary_by_month.csv", index=False)

    # first touch / time to touch on primary
    ft_rows = []
    for (src, combo), g in primary_i.groupby(["strategy_source", "combo_id"]):
        ft_rows.append(
            {
                "strategy_source": src,
                "combo_id": combo,
                "tp_first_rate": float(pd.to_numeric(g.tp_first, errors="coerce").mean()),
                "sl_first_rate": float(pd.to_numeric(g.sl_first, errors="coerce").mean()),
                "same_bar_rate": float(pd.to_numeric(g.same_bar_ambiguous, errors="coerce").mean()),
                "median_bars_to_tp": float(pd.to_numeric(g.bars_to_tp, errors="coerce").median()),
                "median_bars_to_sl": float(pd.to_numeric(g.bars_to_sl, errors="coerce").median()),
            }
        )
    pd.DataFrame(ft_rows).to_csv(out_dir / "first_touch_summary.csv", index=False)

    ttt_rows = []
    for (src, combo), g in primary_i.groupby(["strategy_source", "combo_id"]):
        bt = pd.to_numeric(g.bars_to_tp, errors="coerce")
        row = {"strategy_source": src, "combo_id": combo}
        for b in (1, 2, 4, 8, 16, 24, 48):
            row[f"tp_within_{b}"] = float((bt <= b).mean())
        # neither within horizons — use multi-horizon table
        for h in (24, 48, 96, 192):
            sub = indep[
                (indep.strategy_source == src)
                & (indep.combo_id == combo)
                & (indep.horizon_bars == h)
                & (indep.effective_cost_pct == BASE_COST)
            ]
            neither = (~sub.tp_reached.astype(bool)) & (~sub.sl_reached.astype(bool))
            row[f"neither_within_h{h}"] = float(neither.mean()) if len(sub) else None
        ttt_rows.append(row)
    pd.DataFrame(ttt_rows).to_csv(out_dir / "time_to_touch_summary.csv", index=False)

    # cost sensitivity
    cost_rows = []
    for (src, combo, cost), g in indep[indep.horizon_bars == PRIMARY_HORIZON].groupby(
        ["strategy_source", "combo_id", "effective_cost_pct"]
    ):
        cost_rows.append({"strategy_source": src, "combo_id": combo, "effective_cost_pct": cost, **slice_pack(g)})
    pd.DataFrame(cost_rows).to_csv(out_dir / "cost_sensitivity.csv", index=False)

    # gates + comparison on H192 cost0.20
    gate_rows = []
    cmp_rows = []
    indep_idx = {(r["strategy_source"], r["combo_id"]): r for r in indep_sum if r["horizon_bars"] == PRIMARY_HORIZON and r["effective_cost_pct"] == BASE_COST}
    seq_idx = {(r["strategy_source"], r["combo_id"]): r for r in seq_sum if r["horizon_bars"] == PRIMARY_HORIZON and r["effective_cost_pct"] == BASE_COST}
    cost_idx = {}
    for r in indep_sum:
        if r["horizon_bars"] != PRIMARY_HORIZON:
            continue
        cost_idx[(r["strategy_source"], r["combo_id"], r["effective_cost_pct"])] = r

    for src in (STRATEGY_A6, STRATEGY_STP):
        for tp, sl in EXIT_COMBOS:
            cid = exit_combo_id(tp, sl)
            indep_r = indep_idx.get((src, cid), {})
            seq_r = seq_idx.get((src, cid), {})
            c025 = cost_idx.get((src, cid, 0.25))
            c030 = cost_idx.get((src, cid, 0.30))
            gates = evaluate_gates(indep_r, seq_r, cost025=c025, cost030=c030, tp=tp)
            gate_rows.append({"strategy_source": src, "combo_id": cid, "tp_pct": tp, "sl_magnitude_pct": sl, **gates})

    # strategy comparison per combo
    for tp, sl in EXIT_COMBOS:
        cid = exit_combo_id(tp, sl)
        a = indep_idx.get((STRATEGY_A6, cid), {})
        b = indep_idx.get((STRATEGY_STP, cid), {})
        sa = seq_idx.get((STRATEGY_A6, cid), {})
        sb = seq_idx.get((STRATEGY_STP, cid), {})
        cmp_rows.append(
            {
                "combo_id": cid,
                "tp_pct": tp,
                "sl_magnitude_pct": sl,
                "a6_n": a.get("n"),
                "stp_n": b.get("n"),
                "a6_seq_n": sa.get("n"),
                "stp_seq_n": sb.get("n"),
                "a6_E": a.get("expectation"),
                "stp_E": b.get("expectation"),
                "delta_E": None if a.get("expectation") is None or b.get("expectation") is None else float(b["expectation"]) - float(a["expectation"]),
                "a6_pf": a.get("pf"),
                "stp_pf": b.get("pf"),
                "delta_pf": None if a.get("pf") is None or b.get("pf") is None else float(b["pf"]) - float(a["pf"]),
                "a6_sum": a.get("sum_pnl"),
                "stp_sum": b.get("sum_pnl"),
                "a6_dd": a.get("max_dd"),
                "stp_dd": b.get("max_dd"),
                "a6_streak": a.get("max_losing_streak"),
                "stp_streak": b.get("max_losing_streak"),
                "a6_equal": a.get("equal_coin_expectation"),
                "stp_equal": b.get("equal_coin_expectation"),
                "a6_median": a.get("median_coin_expectation"),
                "stp_median": b.get("median_coin_expectation"),
                "a6_dev": a.get("dev_expectation"),
                "stp_dev": b.get("dev_expectation"),
                "a6_val": a.get("validation_expectation"),
                "stp_val": b.get("validation_expectation"),
                "a6_oos": a.get("oos_expectation"),
                "stp_oos": b.get("oos_expectation"),
                "a6_cw": a.get("common_window_expectation"),
                "stp_cw": b.get("common_window_expectation"),
                "a6_pos_coins": a.get("pct_coins_positive"),
                "stp_pos_coins": b.get("pct_coins_positive"),
                "a6_wo_top3": a.get("without_top3_expectation"),
                "stp_wo_top3": b.get("without_top3_expectation"),
                "a6_c025": (cost_idx.get((STRATEGY_A6, cid, 0.25)) or {}).get("expectation"),
                "stp_c025": (cost_idx.get((STRATEGY_STP, cid, 0.25)) or {}).get("expectation"),
                "a6_c030": (cost_idx.get((STRATEGY_A6, cid, 0.30)) or {}).get("expectation"),
                "stp_c030": (cost_idx.get((STRATEGY_STP, cid, 0.30)) or {}).get("expectation"),
                "a6_seq_E": sa.get("expectation"),
                "stp_seq_E": sb.get("expectation"),
            }
        )

    pd.DataFrame(gate_rows).to_csv(out_dir / "candidate_gate_results.csv", index=False)
    pd.DataFrame(cmp_rows).to_csv(out_dir / "strategy_comparison.csv", index=False)

    # pick candidates
    passed = [g for g in gate_rows if g.get("pass")]
    best_a6 = None
    best_stp = None
    for src in (STRATEGY_A6, STRATEGY_STP):
        cands = [g for g in passed if g["strategy_source"] == src]
        if not cands:
            continue
        scored = []
        for g in cands:
            r = indep_idx.get((src, g["combo_id"]), {})
            scored.append((r.get("expectation") or -999, g))
        scored.sort(key=lambda x: x[0], reverse=True)
        if src == STRATEGY_A6:
            best_a6 = scored[0][1]
        else:
            best_stp = scored[0][1]

    overall = None
    for cand in (best_a6, best_stp):
        if cand is None:
            continue
        r = indep_idx.get((cand["strategy_source"], cand["combo_id"]), {})
        if overall is None or (r.get("expectation") or -999) > (indep_idx.get((overall["strategy_source"], overall["combo_id"]), {}).get("expectation") or -999):
            overall = cand

    track_rejected = overall is None

    # report
    lines = [
        "# Small-Target Single-Trade Audit 2026-07-22\n\n",
        "Frozen A6-Short vs STP B2×E1. No entry/filter/runtime changes.\n\n",
        f"- A6-Short n={len(a6)}\n",
        f"- STP B2×E1 n={len(stp)}\n",
        f"- Exit combos=12; horizons={list(horizons)}; costs={list(costs)}\n",
        f"- Track rejected: {track_rejected}\n",
        f"- Best A6: {None if best_a6 is None else best_a6.get('combo_id')}\n",
        f"- Best STP: {None if best_stp is None else best_stp.get('combo_id')}\n",
        f"- Overall: {None if overall is None else (overall.get('strategy_source'), overall.get('combo_id'))}\n",
        "\n## Primary H192 / cost 0.20 expectations\n",
    ]
    for r in sorted(indep_idx.values(), key=lambda x: (x.get("expectation") is None, -(x.get("expectation") or -999))):
        lines.append(
            f"- {r['strategy_source']} {r['combo_id']}: n={r.get('n')} E={r.get('expectation')} "
            f"PF={r.get('pf')} seqE={(seq_idx.get((r['strategy_source'], r['combo_id'])) or {}).get('expectation')} "
            f"coins+={r.get('pct_coins_positive')}\n"
        )
    (out_dir / "small_target_single_trade_report.md").write_text("".join(lines), encoding="utf-8")

    meta = {
        "a6_n": int(len(a6)),
        "stp_n": int(len(stp)),
        "n_outcome_rows": int(len(trades)),
        "exit_combos": 12,
        "horizons": list(horizons),
        "costs": list(costs),
        "same_bar_policy": args.same_bar_policy,
        "track_rejected": track_rejected,
        "best_a6": best_a6,
        "best_stp": best_stp,
        "overall_winner": overall,
        "overlap": overlap_rows[0],
        "db_persist": False,
        "entry_logic_changed": False,
        "a6_changed": False,
        "stp_changed": False,
        "runtime_changed": False,
        "auto_activate": False,
        "commit": False,
        "push": False,
    }
    (out_dir / "metadata.json").write_text(json.dumps(json_safe(meta), indent=2), encoding="utf-8")
    store.close()
    print(json.dumps(json_safe(meta), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
