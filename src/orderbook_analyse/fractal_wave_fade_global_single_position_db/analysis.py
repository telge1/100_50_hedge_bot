"""Orchestrate DOGE+APT global-single vs per-symbol comparison."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_cycle_wave_analysis.load_mysql import load_mysql_ohlcv_tf
from orderbook_analyse.fractal_signal_confluence_db.signals import (
    build_symbol_signals,
    frozen_eff_edges_all_signal_tfs,
    resolve_entries,
)
from orderbook_analyse.fractal_wave_fade_global_single_position_db import (
    AUDIT_VERSION,
    DEFINITIONS_DOC,
    ENV_FILE,
    EQUITY_FRACTIONS,
    PRIMARY_FEE,
    SLIP_FEE,
    START_EQUITY,
    STRESS_FEE,
    SYMBOLS,
    TIE_BREAK_DOC,
)
from orderbook_analyse.fractal_wave_fade_global_single_position_db.coverage import (
    coverage_frame,
    inventory_coverage,
)
from orderbook_analyse.fractal_wave_fade_global_single_position_db.equity import (
    additive_summary,
    annotate_trade_equities,
    equity_curve_frame,
    summarize_fraction,
)
from orderbook_analyse.fractal_wave_fade_global_single_position_db.global_engine import (
    build_global_event_frame,
    prepare_symbol_universe,
    run_global_single_position,
)
from orderbook_analyse.fractal_wave_fade_strategy_backtest_db.engine import (
    SymbolBooks,
    run_symbol_backtest,
)
from orderbook_analyse.trend_scanner_mysql_feather_parity.load import load_env_file


def _books(symbol: str, end: pd.Timestamp) -> SymbolBooks:
    """Full 1m history through common_end (no start slice — preserves T0 causality)."""
    c1 = load_mysql_ohlcv_tf(symbol=symbol, timeframe="1m", env_file=ENV_FILE)
    ts = pd.to_datetime(c1["timestamp"], utc=True)
    # keep all history up to window end so pre-window confirmations resolve to true T0
    c1 = c1.loc[ts <= end].reset_index(drop=True)
    return SymbolBooks(
        high=c1["high"].astype(float).to_numpy(),
        low=c1["low"].astype(float).to_numpy(),
        close=c1["close"].astype(float).to_numpy(),
        opens=c1["open"].astype(float).to_numpy(),
        open_times=c1["timestamp"].to_numpy(dtype="datetime64[ns]"),
    )


def _normalize_old_trades(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for t in trades:
        rec = dict(t)
        rec["gross_return_pct"] = float(rec.pop("gross_return"))
        rec["fee_pct"] = float(rec.pop("fees"))
        rec["net_return_pct"] = float(rec.pop("net_return"))
        rec["upgrade_count"] = int(rec.pop("number_of_upgrades"))
        rec["holding_minutes"] = float(rec.pop("holding_time_min"))
        rec["signal_time"] = pd.Timestamp(rec.get("entry_time"))
        out.append(rec)
    out.sort(key=lambda r: (pd.Timestamp(r["exit_time"]), pd.Timestamp(r["entry_time"])))
    for i, r in enumerate(out, start=1):
        r["trade_id"] = i
    return out


def _strategy_metrics(trades_df: pd.DataFrame, funnel: dict[str, Any]) -> dict[str, Any]:
    add = additive_summary(trades_df)
    m: dict[str, Any] = {
        **add,
        "total_signals": funnel.get("total_signals"),
        "executed_trades": funnel.get("executed_trades", add["trades"]),
        "suppressed_signals": funnel.get("suppressed_signals"),
        "suppression_rate": funnel.get("suppression_rate"),
        "upgrade_count": int(trades_df["upgrade_count"].sum()) if not trades_df.empty else 0,
        "upgrade_rate": float((trades_df["upgrade_count"] > 0).mean()) if not trades_df.empty else None,
    }
    if trades_df.empty:
        m.update(
            {
                "doge_trades": 0,
                "apt_trades": 0,
                "long": 0,
                "short": 0,
                "first_signal_tf": {},
                "highest_tf_reached": {},
                "exit_reason": {},
            }
        )
        return m
    m["doge_trades"] = int((trades_df["symbol"] == "DOGEUSDT").sum())
    m["apt_trades"] = int((trades_df["symbol"] == "APTUSDT").sum())
    m["long"] = int((trades_df["side"] == "LONG").sum())
    m["short"] = int((trades_df["side"] == "SHORT").sum())
    m["first_signal_tf"] = {
        str(k): int(v) for k, v in trades_df["first_signal_tf"].value_counts().items()
    }
    m["highest_tf_reached"] = {
        str(k): int(v) for k, v in trades_df["highest_tf_reached"].value_counts().items()
    }
    m["exit_reason"] = {
        str(k): int(v) for k, v in trades_df["exit_reason"].value_counts().items()
    }
    return m


def _best_worst_segments(trades_df: pd.DataFrame) -> dict[str, Any]:
    if trades_df.empty:
        return {"best": None, "worst": None}
    rows = []
    for (sym, side), g in trades_df.groupby(["symbol", "side"]):
        nets = g["net_return_pct"].astype(float)
        rows.append(
            {
                "symbol": sym,
                "side": side,
                "trades": int(len(g)),
                "expectancy": float(nets.mean()),
                "cumulative_net": float(nets.sum()),
                "win_rate": float((nets > 0).mean()),
            }
        )
    rows.sort(key=lambda r: r["expectancy"], reverse=True)
    return {"best": rows[0], "worst": rows[-1], "all": rows}


def _months_span(start: pd.Timestamp, end: pd.Timestamp) -> float:
    days = (pd.Timestamp(end) - pd.Timestamp(start)).total_seconds() / 86400.0
    return max(days / 30.4375, 1e-9)


def _decide(
    old_add: dict[str, Any],
    new_add: dict[str, Any],
    new_funnel: dict[str, Any],
    frac100: dict[str, Any],
) -> str:
    """Primary decision from expectancy / PF / DD / suppression / equity stability."""
    oe = old_add.get("expectancy")
    ne = new_add.get("expectancy")
    opf = old_add.get("profit_factor")
    npf = new_add.get("profit_factor")
    odd = old_add.get("max_drawdown_additive") or 0.0
    ndd = new_add.get("max_drawdown_additive") or 0.0
    oc = old_add.get("cumulative_additive_net") or 0.0
    nc = new_add.get("cumulative_additive_net") or 0.0

    if ne is None or (new_add.get("trades") or 0) < 30:
        return "GLOBAL_SINGLE_POSITION_NO_EDGE"
    if ne <= 0 or (npf is not None and npf < 1.0):
        return "GLOBAL_SINGLE_POSITION_NO_EDGE"

    # edge retained if still clearly positive
    retains = ne > 0.05 and (npf is None or npf >= 1.15)

    # improve if better risk-adjusted: higher exp or PF with not-worse DD, or much better DD
    improves = False
    if oe is not None and opf is not None and npf is not None:
        dd_better = ndd > odd  # less negative
        exp_better = ne >= oe * 0.98
        pf_better = npf >= opf * 0.98
        if dd_better and (ne >= oe or npf >= opf) and (exp_better or pf_better):
            improves = True
        if ne > oe * 1.05 and npf >= opf and ndd >= odd * 1.05:
            improves = True

    # hurts if expectancy/PF drop a lot or additive edge collapses vs old
    hurts = False
    if oe is not None and oe > 0:
        if ne < oe * 0.7 or (npf is not None and opf is not None and npf < opf * 0.75):
            hurts = True
        if oc > 0 and nc < oc * 0.35:
            hurts = True

    # large suppression with weaker metrics → hurts
    sr = new_funnel.get("suppression_rate") or 0.0
    if sr > 0.55 and oe is not None and ne < oe * 0.85:
        hurts = True

    # equity path: if 100% fraction ends below start with positive expectancy — still edge but fragile
    if frac100.get("end_equity", START_EQUITY) < START_EQUITY * 0.5 and retains:
        hurts = True

    if improves and not hurts:
        return "GLOBAL_SINGLE_POSITION_IMPROVES_STRATEGY"
    if hurts and not retains:
        return "GLOBAL_SINGLE_POSITION_NO_EDGE"
    if hurts:
        return "GLOBAL_SINGLE_POSITION_HURTS_EDGE"
    if retains:
        return "GLOBAL_SINGLE_POSITION_RETAINS_EDGE"
    return "GLOBAL_SINGLE_POSITION_NO_EDGE"


def run_analysis(*, fee_pct: float = PRIMARY_FEE) -> dict[str, Any]:
    load_env_file(ENV_FILE)
    print(DEFINITIONS_DOC, flush=True)
    print("[coverage] inventory MySQL DOGE+APT …", flush=True)
    inv = inventory_coverage(SYMBOLS)
    if not inv["complete"]:
        raise RuntimeError("No complete common coverage window for DOGE+APT required TFs")
    common_start = inv["common_start"]
    common_end = inv["common_end"]
    print(f"[coverage] common {common_start} → {common_end}", flush=True)

    print("[edges] APT-IS quartiles …", flush=True)
    edges = frozen_eff_edges_all_signal_tfs()

    signals: dict[str, pd.DataFrame] = {}
    books: dict[str, SymbolBooks] = {}
    prepared: dict[str, tuple] = {}
    clusters_by_symbol: dict[str, list] = {}

    for sym in SYMBOLS:
        print(f"[load] {sym} …", flush=True)
        sig = build_symbol_signals(sym, edges)
        books[sym] = _books(sym, common_end)
        sig = resolve_entries(sig, books[sym].open_times, books[sym].opens)
        sig = sig[sig["entry_valid"]].copy()
        # Only true T0 entries inside the common coverage window (no remapping)
        et = pd.to_datetime(sig["entry_time"], utc=True)
        sig = sig[(et >= common_start) & (et <= common_end)].copy().reset_index(drop=True)
        signals[sym] = sig
        df, clusters, sig_to_cluster = prepare_symbol_universe(sym, sig, tier_a_only=True)
        prepared[sym] = (df, clusters, sig_to_cluster)
        clusters_by_symbol[sym] = clusters
        print(
            f"  entry-valid={len(sig)} tier_a={len(df)} clusters={len(clusters)} "
            f"1m={len(books[sym].close)}",
            flush=True,
        )

    events = build_global_event_frame(prepared)
    print(f"[events] global Tier-A signals={len(events)}", flush=True)

    print("[run] GLOBAL_SINGLE …", flush=True)
    glo = run_global_single_position(
        events,
        books,
        clusters_by_symbol,
        fee_pct=fee_pct,
        upgrade_policy="P5A",
        conflict_exit=True,
        window_end=common_end,
    )
    new_trades = pd.DataFrame(glo["trades"]) if glo["trades"] else pd.DataFrame()
    if not new_trades.empty:
        new_trades = annotate_trade_equities(new_trades, start=START_EQUITY)
    new_metrics = _strategy_metrics(new_trades, glo["funnel"])

    print("[run] OLD per-symbol max-1 …", flush=True)
    old_trades_raw: list[dict[str, Any]] = []
    old_suppressed = 0
    old_universe = 0
    for sym in SYMBOLS:
        res = run_symbol_backtest(
            sym,
            signals[sym],
            books[sym],
            tier_a_only=True,
            upgrade_policy="P5A",
            conflict_exit=True,
            fee_pct=fee_pct,
            extra_4h=False,
        )
        old_trades_raw.extend(res["trades"])
        old_suppressed += int(res["funnel"]["signals_suppressed_while_open"])
        old_universe += int(res["funnel"]["universe_signals"])
    old_trades_list = _normalize_old_trades(old_trades_raw)
    old_trades = pd.DataFrame(old_trades_list) if old_trades_list else pd.DataFrame()
    old_funnel = {
        "total_signals": int(len(events)),
        "executed_trades": int(len(old_trades)),
        "suppressed_signals": int(old_suppressed),
        "suppression_rate": (float(old_suppressed / len(events)) if len(events) else None),
    }
    # Note: old engine counts suppressed clusters once; still comparable order-of-magnitude
    old_metrics = _strategy_metrics(old_trades, old_funnel)

    months = _months_span(common_start, common_end)
    old_add = additive_summary(old_trades)
    new_add = additive_summary(new_trades)
    old_add["trades_per_month"] = (old_add["trades"] / months) if months else None
    new_add["trades_per_month"] = (new_add["trades"] / months) if months else None

    frac_summaries = {}
    eq_curves = {}
    for f in EQUITY_FRACTIONS:
        tag = f"{int(round(f * 100))}"
        frac_summaries[tag] = summarize_fraction(
            new_trades,
            fraction=f,
            start=START_EQUITY,
            window_start=common_start,
            window_end=common_end,
        )
        eq_curves[tag] = equity_curve_frame(new_trades, fraction=f, start=START_EQUITY)

    # fee stress on global (additive metrics only)
    stress = {}
    for fee, label in ((SLIP_FEE, "0.13"), (STRESS_FEE, "0.15")):
        print(f"[run] GLOBAL fee stress {label}% …", flush=True)
        g2 = run_global_single_position(
            events,
            books,
            clusters_by_symbol,
            fee_pct=fee,
            upgrade_policy="P5A",
            conflict_exit=True,
            window_end=common_end,
        )
        t2 = pd.DataFrame(g2["trades"]) if g2["trades"] else pd.DataFrame()
        stress[label] = additive_summary(t2)

    segments = _best_worst_segments(new_trades)
    decision = _decide(old_add, new_add, glo["funnel"], frac_summaries["100"])

    comparison = pd.DataFrame(
        [
            {
                "mode": "OLD_PER_SYMBOL_MAX1",
                "trades": old_add["trades"],
                "expectancy": old_add["expectancy"],
                "profit_factor": old_add["profit_factor"],
                "cumulative_additive_net": old_add["cumulative_additive_net"],
                "max_drawdown_additive": old_add["max_drawdown_additive"],
                "suppressed_signals": old_funnel["suppressed_signals"],
                "trades_per_month": old_add["trades_per_month"],
                "win_rate": old_add.get("win_rate"),
            },
            {
                "mode": "NEW_GLOBAL_SINGLE",
                "trades": new_add["trades"],
                "expectancy": new_add["expectancy"],
                "profit_factor": new_add["profit_factor"],
                "cumulative_additive_net": new_add["cumulative_additive_net"],
                "max_drawdown_additive": new_add["max_drawdown_additive"],
                "suppressed_signals": glo["funnel"]["suppressed_signals"],
                "trades_per_month": new_add["trades_per_month"],
                "win_rate": new_add.get("win_rate"),
            },
        ]
    )

    # ~2000% research figure context: prior COMBINED DOGE+BTC additive cum ~2009 units
    research_2000_note = {
        "prior_combined_doge_btc_additive_cum_approx": 2009.0,
        "this_run_old_doge_apt_additive_cum": old_add["cumulative_additive_net"],
        "this_run_new_global_additive_cum": new_add["cumulative_additive_net"],
        "global_vs_old_ratio": (
            float(new_add["cumulative_additive_net"] / old_add["cumulative_additive_net"])
            if old_add["cumulative_additive_net"]
            else None
        ),
        "verdict": (
            "SURVIVES_APPROX"
            if (new_add["cumulative_additive_net"] or 0) >= 0.7 * (old_add["cumulative_additive_net"] or 0)
            and (new_add["cumulative_additive_net"] or 0) > 500
            else (
                "CLEARLY_LOWER"
                if (new_add["cumulative_additive_net"] or 0)
                < 0.5 * max(old_add["cumulative_additive_net"] or 0, 1)
                else "REDUCED"
            )
        ),
    }

    suppressed_df = pd.DataFrame(glo["suppressed"]) if glo["suppressed"] else pd.DataFrame()

    return {
        "audit_version": AUDIT_VERSION,
        "definitions": DEFINITIONS_DOC,
        "tie_break": TIE_BREAK_DOC,
        "data_source": "MySQL market_candles",
        "env_file": str(ENV_FILE),
        "fee_pct_primary": fee_pct,
        "coverage": inv,
        "coverage_df": coverage_frame(inv),
        "common_start": common_start,
        "common_end": common_end,
        "decision": decision,
        "old_metrics": old_metrics,
        "new_metrics": new_metrics,
        "old_additive": old_add,
        "new_additive": new_add,
        "fraction_summaries": frac_summaries,
        "equity_curves": eq_curves,
        "trades_df": new_trades,
        "old_trades_df": old_trades,
        "suppressed_df": suppressed_df,
        "comparison_df": comparison,
        "segments": segments,
        "fee_stress": stress,
        "research_2000_note": research_2000_note,
        "funnel_new": glo["funnel"],
        "funnel_old": old_funnel,
        "start_equity": START_EQUITY,
    }
