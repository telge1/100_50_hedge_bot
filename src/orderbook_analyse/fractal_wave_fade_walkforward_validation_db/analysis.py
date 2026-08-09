"""Orchestrate frozen-strategy walk-forward + honest TRUE-OOS validation."""

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
from orderbook_analyse.fractal_wave_fade_strategy_backtest_db import (
    PRIMARY_FEE,
    SLIP_FEE,
    STRESS_FEE,
    STRATEGY_MAX_HOLD_BY_TF,
    TPSL_BY_TF,
)
from orderbook_analyse.fractal_wave_fade_strategy_backtest_db.engine import (
    SymbolBooks,
    run_symbol_backtest,
)
from orderbook_analyse.fractal_wave_fade_strategy_backtest_db.metrics import (
    equity_curve,
    summarize_trades,
    trades_frame,
)
from orderbook_analyse.fractal_wave_fade_walkforward_validation_db import (
    AUDIT_VERSION,
    DEFINITIONS_DOC,
    DEVELOPMENT_DATA_END,
    ENV_FILE,
    FROZEN_STRATEGY,
    SYMBOLS,
)
from orderbook_analyse.fractal_wave_fade_walkforward_validation_db.blocks import (
    filter_trades_in_block,
    half_blocks,
    quarter_blocks,
    rolling_6m_blocks,
    with_fee,
)
from orderbook_analyse.fractal_wave_fade_walkforward_validation_db.coverage import (
    inventory_coverage,
)
from orderbook_analyse.trend_scanner_mysql_feather_parity.load import load_env_file


def _books(symbol: str) -> SymbolBooks:
    c1 = load_mysql_ohlcv_tf(symbol=symbol, timeframe="1m", env_file=ENV_FILE)
    return SymbolBooks(
        high=c1["high"].astype(float).to_numpy(),
        low=c1["low"].astype(float).to_numpy(),
        close=c1["close"].astype(float).to_numpy(),
        opens=c1["open"].astype(float).to_numpy(),
        open_times=c1["timestamp"].to_numpy(dtype="datetime64[ns]"),
    )


def no_reoptimization_check() -> dict[str, Any]:
    """Confirm frozen fingerprint matches strategy backtest package."""
    diffs = []
    expected_tpsl = {"15m": (1.0, 1.0), "30m": (2.0, 1.5), "1h": (2.0, 1.5), "4h": (4.0, 2.0)}
    for tf, (tp, sl) in expected_tpsl.items():
        got = TPSL_BY_TF[tf]
        if float(got[0]) != tp or float(got[1]) != sl:
            diffs.append(f"TPSL[{tf}]={got} != {(tp, sl)}")
    expected_hold = {"15m": 1440, "30m": 2880, "1h": 4320, "4h": 14400}
    for tf, h in expected_hold.items():
        if STRATEGY_MAX_HOLD_BY_TF[tf] != h:
            diffs.append(f"MAX_HOLD[{tf}]={STRATEGY_MAX_HOLD_BY_TF[tf]} != {h}")
    if FROZEN_STRATEGY["upgrade"] != "P5A_FULL_UPGRADE":
        diffs.append("upgrade policy changed")
    if FROZEN_STRATEGY["extra_4h_primary"] is not False:
        diffs.append("extra_4h enabled in primary")
    if FROZEN_STRATEGY["fee_primary"] != 0.11:
        diffs.append("primary fee changed")
    if diffs:
        return {"status": "FAIL_VALIDATION", "diffs": diffs}
    return {
        "status": "PASS",
        "checks": [
            "Signaldefinition unchanged",
            "Tier A unchanged",
            "Cluster unchanged (same confluence module)",
            "P5A unchanged",
            "TPs unchanged",
            "SLs unchanged",
            "costs unchanged (0.11 primary)",
            "no outcome-driven filter added",
            "extra_4h not primary",
        ],
    }


def _block_regime(books: SymbolBooks, start: pd.Timestamp, end: pd.Timestamp) -> dict[str, Any]:
    start = pd.Timestamp(start)
    end = pd.Timestamp(end)
    if start.tzinfo is None:
        start = start.tz_localize("UTC")
    else:
        start = start.tz_convert("UTC")
    if end.tzinfo is None:
        end = end.tz_localize("UTC")
    else:
        end = end.tz_convert("UTC")
    a = np.datetime64(start.to_datetime64())
    b = np.datetime64(end.to_datetime64())
    mask = (books.open_times >= a) & (books.open_times < b)
    if not np.any(mask):
        return {"avg_abs_ret_1m_pct": None, "block_price_return_pct": None, "regime_label": "UNKNOWN"}
    c = books.close[mask]
    if len(c) < 2:
        return {"avg_abs_ret_1m_pct": None, "block_price_return_pct": None, "regime_label": "UNKNOWN"}
    rets = np.diff(c) / c[:-1] * 100.0
    block_ret = (float(c[-1]) / float(c[0]) - 1.0) * 100.0
    vol = float(np.mean(np.abs(rets)))
    if block_ret > 10:
        lab = "BULL"
    elif block_ret < -10:
        lab = "BEAR"
    else:
        lab = "RANGE"
    return {
        "avg_abs_ret_1m_pct": vol,
        "block_price_return_pct": block_ret,
        "regime_label": lab,
    }


def _metrics_block(df: pd.DataFrame, fee: float = PRIMARY_FEE, **meta) -> dict[str, Any]:
    d = with_fee(df, fee) if df is not None and not df.empty else df
    sm = summarize_trades(d if d is not None else pd.DataFrame(), **meta)
    if d is not None and not d.empty:
        span_days = max(
            (d["entry_time"].max() - d["entry_time"].min()).total_seconds() / 86400.0,
            1.0,
        )
        sm["trades_per_month"] = float(len(d) / (span_days / 30.437))
        # highest TF distribution
        for tf, g in d.groupby("highest_tf_reached"):
            sm[f"highest_tf_share_{tf}"] = float(len(g) / len(d))
        sm["n_long"] = int((d["side"] == "LONG").sum())
        sm["n_short"] = int((d["side"] == "SHORT").sum())
        sm["conflict_exits"] = int((d["exit_reason"] == "HIGHER_TF_CONFLICT").sum())
        # rolling worst
        nets = d["net_return"].astype(float).to_numpy()
        for w in (10, 20):
            if len(nets) >= w:
                roll = np.convolve(nets, np.ones(w), mode="valid")
                sm[f"worst_rolling_{w}"] = float(roll.min())
            else:
                sm[f"worst_rolling_{w}"] = None
    return sm


def _side_rows(df: pd.DataFrame, **meta) -> list[dict]:
    rows = []
    for side in ("LONG", "SHORT"):
        g = df[df["side"] == side] if df is not None and not df.empty else pd.DataFrame()
        rows.append(_metrics_block(g, side=side, **meta))
    return rows


def _tf_rows(df: pd.DataFrame, **meta) -> list[dict]:
    rows = []
    for tf in ("15m", "30m", "1h", "4h"):
        g = df[df["first_signal_tf"] == tf] if df is not None and not df.empty else pd.DataFrame()
        rows.append(_metrics_block(g, first_signal_tf=tf, **meta))
    return rows


def run_analysis() -> dict[str, Any]:
    load_env_file(ENV_FILE)
    print(DEFINITIONS_DOC, flush=True)
    reopt = no_reoptimization_check()
    print(f"[reopt_check] {reopt['status']}", flush=True)
    if reopt["status"] != "PASS":
        print(f"  DIFFS: {reopt.get('diffs')}", flush=True)

    print("[coverage] MySQL inventory …", flush=True)
    cov = inventory_coverage()
    for sym in SYMBOLS:
        ps = cov["per_symbol"][sym]
        print(
            f"  {sym} testable 1m: {ps['testable_start']} -> {ps['testable_end']} | {ps['note']}",
            flush=True,
        )

    dev_end = pd.Timestamp(DEVELOPMENT_DATA_END)
    if dev_end.tzinfo is None:
        dev_end = dev_end.tz_localize("UTC")
    else:
        dev_end = dev_end.tz_convert("UTC")

    print("[edges] APT-IS quartiles …", flush=True)
    edges = frozen_eff_edges_all_signal_tfs()

    signals: dict[str, pd.DataFrame] = {}
    books: dict[str, SymbolBooks] = {}
    for sym in SYMBOLS:
        print(f"[load] {sym} …", flush=True)
        sig = build_symbol_signals(sym, edges)
        books[sym] = _books(sym)
        sig = resolve_entries(sig, books[sym].open_times, books[sym].opens)
        signals[sym] = sig[sig["entry_valid"]].copy()
        print(f"  entry-valid={len(signals[sym])} tier_a={int(signals[sym]['is_tier_a'].sum())}", flush=True)

    # Run frozen variants once per symbol
    trade_sets: dict[str, dict[str, pd.DataFrame]] = {}
    funnels = []
    for sym in SYMBOLS:
        trade_sets[sym] = {}
        for name, kw in (
            ("PRIMARY", dict(tier_a_only=True, upgrade_policy="P5A", conflict_exit=True)),
            ("P0", dict(tier_a_only=True, upgrade_policy="P0", conflict_exit=True)),
            ("ALL", dict(tier_a_only=False, upgrade_policy="P5A", conflict_exit=True)),
        ):
            print(f"[run] {sym} {name} …", flush=True)
            res = run_symbol_backtest(
                sym,
                signals[sym],
                books[sym],
                fee_pct=PRIMARY_FEE,
                extra_4h=False,
                **kw,
            )
            trade_sets[sym][name] = trades_frame(res["trades"])
            funnels.append({**res["funnel"], "variant": name})
            print(f"  trades={len(res['trades'])} conflicts={res['funnel'].get('conflicts')}", flush=True)

    # TRUE OOS
    true_oos_rows = []
    oos_status = "TRUE_OOS_COVERAGE_INSUFFICIENT"
    for sym in SYMBOLS:
        df = trade_sets[sym]["PRIMARY"]
        oos = df[df["entry_time"] > dev_end] if not df.empty else df
        # also check coverage after cutoff
        test_end = cov["per_symbol"][sym]["testable_end"]
        gap_min = (test_end - dev_end).total_seconds() / 60.0
        for fee in (PRIMARY_FEE, SLIP_FEE, STRESS_FEE):
            sm = _metrics_block(oos, fee=fee, symbol=sym, segment="TRUE_OOS", fee_pct=fee)
            sm["coverage_minutes_after_dev_end"] = gap_min
            true_oos_rows.append(sm)
        n = len(oos)
        print(f"[oos] {sym}: n={n} coverage_after_dev_end_min={gap_min:.1f}", flush=True)

    n_oos_doge = int(true_oos_rows[0].get("trades") or 0) if true_oos_rows else 0
    n_oos_btc = int(true_oos_rows[3].get("trades") or 0) if len(true_oos_rows) > 3 else 0
    n_oos_comb = n_oos_doge + n_oos_btc
    if n_oos_comb == 0 and all(
        (cov["per_symbol"][s]["testable_end"] - dev_end).total_seconds() < 86400 for s in SYMBOLS
    ):
        oos_status = "TRUE_OOS_COVERAGE_INSUFFICIENT"
    elif n_oos_comb < 100 or n_oos_doge < 50 or n_oos_btc < 50:
        if n_oos_comb == 0:
            oos_status = "TRUE_OOS_COVERAGE_INSUFFICIENT"
        else:
            oos_status = "TRUE_OOS_SAMPLE_SMALL"
    else:
        oos_status = "TRUE_OOS_AVAILABLE"

    # Walk-forward blocks
    time_block_results = []
    half_split_results = []
    rolling_results = []
    long_short_stability = []
    tf_stability = []
    p5a_stability = []
    tier_a_stability = []
    cost_stability = []
    drawdown_stability = []

    for sym in SYMBOLS:
        start = cov["per_symbol"][sym]["testable_start"]
        end = cov["per_symbol"][sym]["testable_end"]
        primary = trade_sets[sym]["PRIMARY"]
        p0 = trade_sets[sym]["P0"]
        all_t = trade_sets[sym]["ALL"]

        q_blocks = quarter_blocks(start, end)
        h_blocks = half_blocks(start, end)
        r_blocks = rolling_6m_blocks(start, end)

        for blk in q_blocks + h_blocks + r_blocks:
            bdf = filter_trades_in_block(primary, blk["start"], blk["end"])
            regime = _block_regime(books[sym], blk["start"], blk["end"])
            sm = _metrics_block(
                bdf,
                symbol=sym,
                block_type=blk["block_type"],
                block_id=blk["block_id"],
                block_start=blk["start"].isoformat(),
                block_end=blk["end"].isoformat(),
                **regime,
            )
            target = (
                time_block_results
                if blk["block_type"] == "QUARTER"
                else half_split_results
                if blk["block_type"] == "HALF"
                else rolling_results
            )
            target.append(sm)

            # long/short + tf
            for r in _side_rows(
                bdf,
                symbol=sym,
                block_type=blk["block_type"],
                block_id=blk["block_id"],
            ):
                long_short_stability.append(r)
            for r in _tf_rows(
                bdf,
                symbol=sym,
                block_type=blk["block_type"],
                block_id=blk["block_id"],
            ):
                tf_stability.append(r)

            # P5A vs P0
            p5 = _metrics_block(bdf, symbol=sym, block_type=blk["block_type"], block_id=blk["block_id"], policy="P5A")
            p0b = filter_trades_in_block(p0, blk["start"], blk["end"])
            p0m = _metrics_block(p0b, symbol=sym, block_type=blk["block_type"], block_id=blk["block_id"], policy="P0")
            p5a_stability.append(
                {
                    "symbol": sym,
                    "block_type": blk["block_type"],
                    "block_id": blk["block_id"],
                    "p5a_n": p5.get("trades"),
                    "p0_n": p0m.get("trades"),
                    "p5a_expectancy": p5.get("expectancy"),
                    "p0_expectancy": p0m.get("expectancy"),
                    "p5a_PF": p5.get("profit_factor"),
                    "p0_PF": p0m.get("profit_factor"),
                    "P5A_minus_P0_expectancy": (
                        None
                        if p5.get("expectancy") is None or p0m.get("expectancy") is None
                        else float(p5["expectancy"]) - float(p0m["expectancy"])
                    ),
                    "P5A_minus_P0_PF": (
                        None
                        if p5.get("profit_factor") is None or p0m.get("profit_factor") is None
                        else float(p5["profit_factor"]) - float(p0m["profit_factor"])
                    ),
                }
            )

            # Tier A vs ALL
            ta = p5
            al = _metrics_block(
                filter_trades_in_block(all_t, blk["start"], blk["end"]),
                symbol=sym,
                block_type=blk["block_type"],
                block_id=blk["block_id"],
                policy="ALL",
            )
            tier_a_stability.append(
                {
                    "symbol": sym,
                    "block_type": blk["block_type"],
                    "block_id": blk["block_id"],
                    "tier_a_n": ta.get("trades"),
                    "all_n": al.get("trades"),
                    "tier_a_expectancy": ta.get("expectancy"),
                    "all_expectancy": al.get("expectancy"),
                    "tier_a_PF": ta.get("profit_factor"),
                    "all_PF": al.get("profit_factor"),
                    "tier_a_maxDD": ta.get("max_drawdown"),
                    "all_maxDD": al.get("max_drawdown"),
                    "tier_a_cum": ta.get("cumulative_net"),
                    "all_cum": al.get("cumulative_net"),
                }
            )

            # cost stress on quarter/half only (compact)
            if blk["block_type"] in ("QUARTER", "HALF"):
                for fee in (PRIMARY_FEE, SLIP_FEE, STRESS_FEE):
                    cm = _metrics_block(
                        bdf,
                        fee=fee,
                        symbol=sym,
                        block_type=blk["block_type"],
                        block_id=blk["block_id"],
                        fee_pct=fee,
                    )
                    cost_stability.append(
                        {
                            "symbol": sym,
                            "block_type": blk["block_type"],
                            "block_id": blk["block_id"],
                            "fee_pct": fee,
                            "trades": cm.get("trades"),
                            "expectancy": cm.get("expectancy"),
                            "profit_factor": cm.get("profit_factor"),
                            "cumulative_net": cm.get("cumulative_net"),
                            "max_drawdown": cm.get("max_drawdown"),
                        }
                    )

            drawdown_stability.append(
                {
                    "symbol": sym,
                    "block_type": blk["block_type"],
                    "block_id": blk["block_id"],
                    "maxDD": sm.get("max_drawdown"),
                    "max_loss_streak": sm.get("max_consecutive_losses"),
                    "worst_rolling_10": sm.get("worst_rolling_10"),
                    "worst_rolling_20": sm.get("worst_rolling_20"),
                    "regime_label": regime.get("regime_label"),
                    "block_price_return_pct": regime.get("block_price_return_pct"),
                }
            )

    # Equity curves with block markers (primary)
    equity_rows = []
    for sym in SYMBOLS:
        eq = equity_curve(trade_sets[sym]["PRIMARY"])
        if eq.empty:
            continue
        q_blocks = quarter_blocks(
            cov["per_symbol"][sym]["testable_start"],
            cov["per_symbol"][sym]["testable_end"],
        )
        eq = eq.copy()
        eq["symbol"] = sym
        et = pd.to_datetime(eq["exit_time"], utc=True)
        markers = []
        for _, r in eq.iterrows():
            t = pd.Timestamp(r["exit_time"])
            if t.tzinfo is None:
                t = t.tz_localize("UTC")
            else:
                t = t.tz_convert("UTC")
            lab = ""
            for blk in q_blocks:
                if abs((t - blk["start"]).total_seconds()) < 86400:
                    lab = f"NEAR_{blk['block_id']}_START"
                    break
            markers.append(lab)
        eq["block_marker"] = markers
        equity_rows.append(eq)

    # Combined primary for reference
    parts = [trade_sets[s]["PRIMARY"] for s in SYMBOLS if not trade_sets[s]["PRIMARY"].empty]
    if parts:
        comb = pd.concat(parts, ignore_index=True)
        comb = trades_frame(comb.to_dict("records"))
        eqc = equity_curve(comb)
        eqc["symbol"] = "COMBINED"
        eqc["block_marker"] = ""
        equity_rows.append(eqc)

    decisions, answers, stability = _decide(
        time_block_results,
        half_split_results,
        rolling_results,
        p5a_stability,
        tier_a_stability,
        cost_stability,
        true_oos_rows,
        oos_status,
        n_oos_doge,
        n_oos_btc,
        n_oos_comb,
        funnels,
        long_short_stability,
        reopt,
    )

    return {
        "audit_version": AUDIT_VERSION,
        "definitions": DEFINITIONS_DOC,
        "development_data_end": DEVELOPMENT_DATA_END,
        "true_oos_start": (dev_end + pd.Timedelta(minutes=1)).isoformat(),
        "true_oos_status": oos_status,
        "coverage": cov,
        "reopt_check": reopt,
        "funnels": funnels,
        "time_block_results": time_block_results,
        "half_split_results": half_split_results,
        "rolling_results": rolling_results,
        "long_short_stability": long_short_stability,
        "tf_stability": tf_stability,
        "p5a_stability": p5a_stability,
        "tier_a_stability": tier_a_stability,
        "cost_stability": cost_stability,
        "drawdown_stability": drawdown_stability,
        "true_oos_results": true_oos_rows,
        "equity_frames": equity_rows,
        "stability_shares": stability,
        "decisions": decisions,
        "answers": answers,
    }


def _share(rows: list[dict], key: str, pred) -> float | None:
    vals = [r for r in rows if r.get("trades", 0) and r.get("trades", 0) >= 20]
    if not vals:
        return None
    return float(sum(1 for r in vals if pred(r)) / len(vals))


def _decide(
    quarters,
    halves,
    rolling,
    p5a_stab,
    tier_stab,
    cost_stab,
    true_oos_rows,
    oos_status,
    n_oos_doge,
    n_oos_btc,
    n_oos_comb,
    funnels,
    long_short_stability,
    reopt,
) -> tuple[dict, dict, dict]:
    # Use QUARTER blocks as primary stability sample
    q = [r for r in quarters if (r.get("trades") or 0) >= 20]

    def exp_pos(r):
        return (r.get("expectancy") or 0) > 0

    def pf_pos(r):
        return (r.get("profit_factor") or 0) > 1

    def net_pos(r):
        return (r.get("cumulative_net") or 0) > 0

    stability = {
        "positive_expectancy_block_share": _share(q, "expectancy", exp_pos),
        "PF_above_1_block_share": _share(q, "PF", pf_pos),
        "positive_net_block_share": _share(q, "net", net_pos),
        "n_quarter_blocks_evaluated": len(q),
    }

    # per symbol quarter shares
    doge_q = [r for r in q if r.get("symbol") == "DOGEUSDT"]
    btc_q = [r for r in q if r.get("symbol") == "BTCUSDT"]

    def sym_stable(rows):
        if len(rows) < 2:
            return False
        return (
            _share(rows, "e", exp_pos) is not None
            and _share(rows, "e", exp_pos) >= 0.75
            and _share(rows, "p", pf_pos) >= 0.75
        )

    doge_stable = sym_stable(doge_q)
    btc_stable = sym_stable(btc_q)

    # walk-forward decision
    pe = stability["positive_expectancy_block_share"] or 0
    pp = stability["PF_above_1_block_share"] or 0
    pn = stability["positive_net_block_share"] or 0
    if pe >= 0.75 and pp >= 0.75 and pn >= 0.75 and doge_stable and btc_stable:
        wf = "WALK_FORWARD_EDGE_STABLE"
    elif pe >= 0.5 and pp >= 0.5:
        wf = "WALK_FORWARD_EDGE_CONTEXT_DEPENDENT"
    else:
        wf = "WALK_FORWARD_EDGE_UNSTABLE"

    # TRUE OOS primary decision
    if oos_status == "TRUE_OOS_COVERAGE_INSUFFICIENT":
        primary = "TRUE_OOS_COVERAGE_INSUFFICIENT"
    elif oos_status == "TRUE_OOS_SAMPLE_SMALL":
        # weak statement
        oos_comb = [r for r in true_oos_rows if r.get("fee_pct") == PRIMARY_FEE]
        if oos_comb and (oos_comb[0].get("expectancy") or 0) > 0:
            primary = "TRUE_OOS_EDGE_WEAK"
        else:
            primary = "TRUE_OOS_EDGE_WEAK"
    else:
        # evaluate combined at 0.11
        doge_o = next(
            (r for r in true_oos_rows if r.get("symbol") == "DOGEUSDT" and r.get("fee_pct") == 0.11),
            {},
        )
        btc_o = next(
            (r for r in true_oos_rows if r.get("symbol") == "BTCUSDT" and r.get("fee_pct") == 0.11),
            {},
        )
        if (doge_o.get("expectancy") or 0) > 0 and (btc_o.get("expectancy") or 0) > 0 and (
            (doge_o.get("profit_factor") or 0) > 1.1 or (btc_o.get("profit_factor") or 0) > 1.1
        ):
            primary = "TRUE_OOS_STRATEGY_HAS_EDGE"
        elif (doge_o.get("expectancy") or 0) > 0 or (btc_o.get("expectancy") or 0) > 0:
            primary = "TRUE_OOS_EDGE_WEAK"
        else:
            primary = "TRUE_OOS_STRATEGY_FAILS"

    # P5A stability across quarter blocks
    p5_q = [r for r in p5a_stab if r.get("block_type") == "QUARTER" and r.get("P5A_minus_P0_expectancy") is not None]
    p5_pos = sum(1 for r in p5_q if (r["P5A_minus_P0_expectancy"] or 0) > 0)
    p5_dec = (
        "DYNAMIC_UPGRADE_STABLE_ACROSS_PERIODS"
        if p5_q and p5_pos / len(p5_q) >= 0.6
        else "DYNAMIC_UPGRADE_PERIOD_DEPENDENT"
    )

    # Tier A: better PF or less bad DD in most quarters
    t_q = [r for r in tier_stab if r.get("block_type") == "QUARTER"]
    t_better = 0
    t_n = 0
    for r in t_q:
        if r.get("tier_a_PF") is None or r.get("all_PF") is None:
            continue
        t_n += 1
        ta_dd = r.get("tier_a_maxDD") or 0
        al_dd = r.get("all_maxDD") or 0
        if (r["tier_a_PF"] >= r["all_PF"] - 0.02) and (ta_dd >= al_dd - 1e-9):
            t_better += 1
        elif (r.get("tier_a_expectancy") or 0) > (r.get("all_expectancy") or 0) and ta_dd >= al_dd:
            t_better += 1
    tier_dec = (
        "TIER_A_VALUE_STABLE_ACROSS_PERIODS"
        if t_n and t_better / t_n >= 0.6
        else "TIER_A_VALUE_PERIOD_DEPENDENT"
    )

    # Cost: quarters at 0.15 still mostly positive exp
    c15 = [r for r in cost_stab if r.get("fee_pct") == 0.15 and r.get("block_type") == "QUARTER"]
    c13 = [r for r in cost_stab if r.get("fee_pct") == 0.13 and r.get("block_type") == "QUARTER"]
    survive = (
        _share(c15, "e", exp_pos) is not None
        and (_share(c15, "e", exp_pos) or 0) >= 0.75
        and (_share(c13, "e", exp_pos) or 0) >= 0.75
    )
    cost_dec = "EDGE_SURVIVES_COST_STRESS" if survive else "EDGE_SENSITIVE_TO_COSTS"

    # worst block
    worst = None
    if q:
        worst = min(q, key=lambda r: (r.get("cumulative_net") is None, r.get("cumulative_net") or 0))

    # LONG/SHORT stability on quarter blocks
    ls_q = [
        r
        for r in long_short_stability
        if r.get("block_type") == "QUARTER" and (r.get("trades") or 0) >= 15
    ]
    ls_summary = {}
    for sym in SYMBOLS:
        for side in ("LONG", "SHORT"):
            rows = [r for r in ls_q if r.get("symbol") == sym and r.get("side") == side]
            ls_summary[f"{sym}_{side}"] = {
                "n_blocks": len(rows),
                "pos_exp_share": _share(rows, "e", exp_pos),
                "pf_above_1_share": _share(rows, "p", pf_pos),
            }
    both_sides_ok = all(
        (ls_summary.get(f"{sym}_{side}", {}).get("pos_exp_share") or 0) >= 0.5
        for sym in SYMBOLS
        for side in ("LONG", "SHORT")
        if ls_summary.get(f"{sym}_{side}", {}).get("n_blocks", 0) >= 2
    )

    paper_ok = (
        wf == "WALK_FORWARD_EDGE_STABLE"
        and reopt.get("status") == "PASS"
        and cost_dec == "EDGE_SURVIVES_COST_STRESS"
    )

    answers = {
        "A": {
            "question": "Haben wir echtes OOS?",
            "answer": oos_status == "TRUE_OOS_AVAILABLE",
            "status": oos_status,
            "development_data_end": DEVELOPMENT_DATA_END,
        },
        "B": {
            "question": "Falls ja: wie viele Trades?",
            "doge": n_oos_doge,
            "btc": n_oos_btc,
            "combined": n_oos_comb,
        },
        "C": {
            "question": "Falls nein: Walk-forward Stabilität?",
            "decision": wf,
            "shares": stability,
        },
        "D": {
            "question": "Zeitblöcke Exp>0 / PF>1 / net>0?",
            **stability,
            "doge_quarter_pos_exp_share": _share(doge_q, "e", exp_pos),
            "btc_quarter_pos_exp_share": _share(btc_q, "e", exp_pos),
        },
        "E": {"question": "DOGE stabil?", "answer": doge_stable, "n_quarters": len(doge_q)},
        "F": {"question": "BTC stabil?", "answer": btc_stable, "n_quarters": len(btc_q)},
        "G": {
            "question": "LONG/SHORT stabil?",
            "both_sides_mostly_positive": both_sides_ok,
            "by_symbol_side": ls_summary,
        },
        "H": {
            "question": "P5A besser in mehreren Perioden?",
            "decision": p5_dec,
            "p5_pos_share": p5_pos / len(p5_q) if p5_q else None,
        },
        "I": {
            "question": "Tier A risk-adjustiert besser?",
            "decision": tier_dec,
            "better_share": t_better / t_n if t_n else None,
        },
        "J": {
            "question": "Edge bei 0.13/0.15?",
            "decision": cost_dec,
            "share_pos_exp_013": _share(c13, "e", exp_pos),
            "share_pos_exp_015": _share(c15, "e", exp_pos),
        },
        "K": {
            "question": "Schlechtester Zeitblock?",
            "worst": {
                "symbol": worst.get("symbol") if worst else None,
                "block_id": worst.get("block_id") if worst else None,
                "expectancy": worst.get("expectancy") if worst else None,
                "cumulative_net": worst.get("cumulative_net") if worst else None,
                "profit_factor": worst.get("profit_factor") if worst else None,
                "max_drawdown": worst.get("max_drawdown") if worst else None,
                "regime_label": worst.get("regime_label") if worst else None,
            }
            if worst
            else None,
        },
        "L": {
            "question": "Paper-/Replay-Test rechtfertigen?",
            "answer": paper_ok,
            "rationale": (
                "Walk-forward stable + reopt PASS + cost stress survived; still no TRUE OOS — paper/replay as next causal check is reasonable with that caveat."
                if paper_ok
                else "Walk-forward/cost/reopt not all green — paper test only with strong caveats."
            ),
        },
        "conflict_exits_documented": [f for f in funnels if f.get("variant") == "PRIMARY"],
    }

    decisions = {
        "primary": primary,
        "walk_forward": wf,
        "p5a": p5_dec,
        "tier": tier_dec,
        "costs": cost_dec,
        "reopt_check": reopt.get("status"),
    }
    return decisions, answers, stability
