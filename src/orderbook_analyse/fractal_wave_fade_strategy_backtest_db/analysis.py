"""Orchestrate full chronological wave-fade strategy backtests."""

from __future__ import annotations

from typing import Any

import pandas as pd

from orderbook_analyse.fractal_cycle_wave_analysis.load_mysql import load_mysql_ohlcv_tf
from orderbook_analyse.fractal_signal_confluence_db.signals import (
    build_symbol_signals,
    frozen_eff_edges_all_signal_tfs,
    resolve_entries,
)
from orderbook_analyse.fractal_wave_fade_strategy_backtest_db import (
    AUDIT_VERSION,
    DEFINITIONS_DOC,
    ENV_FILE,
    PRIMARY_FEE,
    SLIP_FEE,
    STRESS_FEE,
    SYMBOLS,
)
from orderbook_analyse.fractal_wave_fade_strategy_backtest_db.engine import (
    SymbolBooks,
    run_symbol_backtest,
)
from orderbook_analyse.fractal_wave_fade_strategy_backtest_db.metrics import (
    drawdown_episodes,
    equity_curve,
    half_split,
    monthly_meta,
    monthly_performance,
    overlap_stats,
    summarize_trades,
    trades_frame,
    yearly_performance,
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


def _run_pair(
    label: str,
    signals: dict[str, pd.DataFrame],
    books: dict[str, SymbolBooks],
    symbols: tuple[str, ...],
    *,
    tier_a_only: bool,
    upgrade_policy: str,
    conflict_exit: bool,
    fee_pct: float,
    extra_4h: bool = False,
) -> dict[str, Any]:
    per_sym = {}
    all_trades = []
    funnels = []
    for sym in symbols:
        res = run_symbol_backtest(
            sym,
            signals[sym],
            books[sym],
            tier_a_only=tier_a_only,
            upgrade_policy=upgrade_policy,
            conflict_exit=conflict_exit,
            fee_pct=fee_pct,
            extra_4h=extra_4h,
        )
        per_sym[sym] = res
        all_trades.extend(res["trades"])
        funnels.append({**res["funnel"], "variant": label})

    df = trades_frame(all_trades)
    summary = summarize_trades(df, variant=label, mode="+".join(symbols))
    eq = equity_curve(df)
    dds = drawdown_episodes(eq, top_n=5)
    if dds:
        summary["max_dd_duration_days"] = dds[0].get("duration_days")
        summary["max_dd_time_to_recovery_days"] = dds[0].get("time_to_recovery_days")

    long_df = df[df["side"] == "LONG"] if not df.empty else df
    short_df = df[df["side"] == "SHORT"] if not df.empty else df
    by_side = [
        summarize_trades(long_df, variant=label, side="LONG"),
        summarize_trades(short_df, variant=label, side="SHORT"),
    ]

    first_tf_rows = []
    highest_rows = []
    if not df.empty:
        for tf, g in df.groupby("first_signal_tf"):
            sm = summarize_trades(g, variant=label, first_signal_tf=tf)
            sm["highest_tf_upgrade_rate"] = float((g["number_of_upgrades"] > 0).mean())
            first_tf_rows.append(sm)
        for tf, g in df.groupby("highest_tf_reached"):
            highest_rows.append(summarize_trades(g, variant=label, highest_tf_reached=tf))

    monthly_rows = []
    yearly_rows = []
    half_rows = []
    for sym in symbols:
        sdf = df[df["symbol"] == sym] if not df.empty else df
        monthly_rows.extend(monthly_performance(sdf, sym))
        yearly_rows.extend(yearly_performance(sdf, sym))
        half_rows.extend(half_split(sdf, sym))

    overlap = None
    if set(symbols) >= {"DOGEUSDT", "BTCUSDT"}:
        overlap = overlap_stats(per_sym["DOGEUSDT"]["trades"], per_sym["BTCUSDT"]["trades"])

    return {
        "label": label,
        "summary": summary,
        "trades": all_trades,
        "equity": eq,
        "drawdowns": [{**d, "variant": label} for d in dds],
        "by_side": by_side,
        "first_tf": first_tf_rows,
        "highest_tf": highest_rows,
        "monthly": monthly_rows,
        "monthly_meta": {sym: monthly_meta([r for r in monthly_rows if r["symbol"] == sym]) for sym in symbols},
        "yearly": yearly_rows,
        "half": half_rows,
        "funnels": funnels,
        "overlap": overlap,
        "config": {
            "tier_a_only": tier_a_only,
            "upgrade_policy": upgrade_policy,
            "conflict_exit": conflict_exit,
            "fee_pct": fee_pct,
            "extra_4h": extra_4h,
            "symbols": list(symbols),
        },
    }


def run_analysis() -> dict[str, Any]:
    load_env_file(ENV_FILE)
    print(DEFINITIONS_DOC, flush=True)
    print("[edges] APT-IS quartiles from MySQL …", flush=True)
    edges = frozen_eff_edges_all_signal_tfs()

    signals: dict[str, pd.DataFrame] = {}
    books: dict[str, SymbolBooks] = {}
    for sym in SYMBOLS:
        print(f"[load] {sym} signals + 1m …", flush=True)
        sig = build_symbol_signals(sym, edges)
        books[sym] = _books(sym)
        sig = resolve_entries(sig, books[sym].open_times, books[sym].opens)
        signals[sym] = sig[sig["entry_valid"]].copy()
        print(
            f"  entry-valid={len(signals[sym])} tier_a={int(signals[sym]['is_tier_a'].sum())} "
            f"1m_bars={len(books[sym].close)}",
            flush=True,
        )

    variants: dict[str, dict] = {}

    def add(label: str, symbols: tuple[str, ...], **kw):
        print(f"[run] {label} …", flush=True)
        variants[label] = _run_pair(label, signals, books, symbols, **kw)
        s = variants[label]["summary"]
        print(
            f"  trades={s.get('trades')} exp={s.get('expectancy')} PF={s.get('profit_factor')} "
            f"cum={s.get('cumulative_net')} maxDD={s.get('max_drawdown')}",
            flush=True,
        )

    # Primary: Tier-A + P5A + conflict + TP4 + 0.11%
    primary_kw = dict(
        tier_a_only=True,
        upgrade_policy="P5A",
        conflict_exit=True,
        fee_pct=PRIMARY_FEE,
        extra_4h=False,
    )
    add("PRIMARY_DOGE", ("DOGEUSDT",), **primary_kw)
    add("PRIMARY_BTC", ("BTCUSDT",), **primary_kw)
    add("PRIMARY_COMBINED", ("DOGEUSDT", "BTCUSDT"), **primary_kw)

    # P0 vs P5A (Tier A + conflict)
    add(
        "P0_TIERA_CONFLICT_DOGE",
        ("DOGEUSDT",),
        tier_a_only=True,
        upgrade_policy="P0",
        conflict_exit=True,
        fee_pct=PRIMARY_FEE,
    )
    add(
        "P0_TIERA_CONFLICT_BTC",
        ("BTCUSDT",),
        tier_a_only=True,
        upgrade_policy="P0",
        conflict_exit=True,
        fee_pct=PRIMARY_FEE,
    )
    add(
        "P0_TIERA_CONFLICT_COMBINED",
        ("DOGEUSDT", "BTCUSDT"),
        tier_a_only=True,
        upgrade_policy="P0",
        conflict_exit=True,
        fee_pct=PRIMARY_FEE,
    )

    # Conflict ignore vs exit (P5A Tier A)
    add(
        "P5A_IGNORE_CONFLICT_DOGE",
        ("DOGEUSDT",),
        tier_a_only=True,
        upgrade_policy="P5A",
        conflict_exit=False,
        fee_pct=PRIMARY_FEE,
    )
    add(
        "P5A_IGNORE_CONFLICT_BTC",
        ("BTCUSDT",),
        tier_a_only=True,
        upgrade_policy="P5A",
        conflict_exit=False,
        fee_pct=PRIMARY_FEE,
    )
    add(
        "P5A_IGNORE_CONFLICT_COMBINED",
        ("DOGEUSDT", "BTCUSDT"),
        tier_a_only=True,
        upgrade_policy="P5A",
        conflict_exit=False,
        fee_pct=PRIMARY_FEE,
    )

    # ALL signals vs Tier A
    add(
        "ALL_P5A_CONFLICT_DOGE",
        ("DOGEUSDT",),
        tier_a_only=False,
        upgrade_policy="P5A",
        conflict_exit=True,
        fee_pct=PRIMARY_FEE,
    )
    add(
        "ALL_P5A_CONFLICT_BTC",
        ("BTCUSDT",),
        tier_a_only=False,
        upgrade_policy="P5A",
        conflict_exit=True,
        fee_pct=PRIMARY_FEE,
    )
    add(
        "ALL_P5A_CONFLICT_COMBINED",
        ("DOGEUSDT", "BTCUSDT"),
        tier_a_only=False,
        upgrade_policy="P5A",
        conflict_exit=True,
        fee_pct=PRIMARY_FEE,
    )

    # 4h TP6 sensitivity (primary config)
    add(
        "PRIMARY_4H_TP6_DOGE",
        ("DOGEUSDT",),
        tier_a_only=True,
        upgrade_policy="P5A",
        conflict_exit=True,
        fee_pct=PRIMARY_FEE,
        extra_4h=True,
    )
    add(
        "PRIMARY_4H_TP6_BTC",
        ("BTCUSDT",),
        tier_a_only=True,
        upgrade_policy="P5A",
        conflict_exit=True,
        fee_pct=PRIMARY_FEE,
        extra_4h=True,
    )
    add(
        "PRIMARY_4H_TP6_COMBINED",
        ("DOGEUSDT", "BTCUSDT"),
        tier_a_only=True,
        upgrade_policy="P5A",
        conflict_exit=True,
        fee_pct=PRIMARY_FEE,
        extra_4h=True,
    )

    # Cost sensitivity on primary combined + per symbol
    for fee, tag in ((SLIP_FEE, "FEE_013"), (STRESS_FEE, "FEE_015")):
        add(
            f"{tag}_DOGE",
            ("DOGEUSDT",),
            tier_a_only=True,
            upgrade_policy="P5A",
            conflict_exit=True,
            fee_pct=fee,
        )
        add(
            f"{tag}_BTC",
            ("BTCUSDT",),
            tier_a_only=True,
            upgrade_policy="P5A",
            conflict_exit=True,
            fee_pct=fee,
        )
        add(
            f"{tag}_COMBINED",
            ("DOGEUSDT", "BTCUSDT"),
            tier_a_only=True,
            upgrade_policy="P5A",
            conflict_exit=True,
            fee_pct=fee,
        )

    decisions, answers = _decide(variants)
    return {
        "audit_version": AUDIT_VERSION,
        "definitions": DEFINITIONS_DOC,
        "variants": variants,
        "decisions": decisions,
        "answers": answers,
        "coverage_note": (
            "BTCUSDT 1m coverage ends ~2021-12-09 in DB; DOGE has longer history. "
            "No OOS claim — research history reused."
        ),
    }


def _s(variants: dict, key: str) -> dict:
    return (variants.get(key) or {}).get("summary") or {}


def _decide(variants: dict) -> tuple[dict, dict]:
    doge = _s(variants, "PRIMARY_DOGE")
    btc = _s(variants, "PRIMARY_BTC")
    comb = _s(variants, "PRIMARY_COMBINED")

    def pos(s):
        return (s.get("expectancy") or 0) > 0 and (s.get("cumulative_net") or 0) > 0

    if pos(doge) and pos(btc) and (comb.get("profit_factor") or 0) >= 1.1 and (comb.get("expectancy") or 0) >= 0.05:
        primary = "FULL_CLUSTER_STRATEGY_HAS_EDGE"
    elif pos(comb) and (comb.get("expectancy") or 0) > 0:
        # positive but small
        if (comb.get("expectancy") or 0) < 0.05 or (comb.get("profit_factor") or 0) < 1.1:
            primary = "FULL_CLUSTER_STRATEGY_EDGE_TOO_SMALL"
        else:
            primary = "FULL_CLUSTER_STRATEGY_HAS_EDGE"
    elif not pos(comb):
        primary = "FULL_CLUSTER_STRATEGY_NOT_PROFITABLE"
    else:
        primary = "FULL_CLUSTER_STRATEGY_EDGE_TOO_SMALL"

    # P5A vs P0
    p5_score = 0
    for mode in ("DOGE", "BTC", "COMBINED"):
        a = _s(variants, f"PRIMARY_{mode}")
        b = _s(variants, f"P0_TIERA_CONFLICT_{mode}")
        if a and b and a.get("expectancy") is not None and b.get("expectancy") is not None:
            if a["expectancy"] > b["expectancy"] and (a.get("cumulative_net") or 0) >= (b.get("cumulative_net") or 0):
                p5_score += 1
            elif a["expectancy"] < b["expectancy"]:
                p5_score -= 1
    p5_dec = (
        "DYNAMIC_UPGRADE_IMPROVES_FULL_STRATEGY"
        if p5_score > 0
        else "DYNAMIC_UPGRADE_NO_FULL_STRATEGY_VALUE"
    )

    # Conflict
    c_score = 0
    for mode in ("DOGE", "BTC", "COMBINED"):
        a = _s(variants, f"PRIMARY_{mode}")
        b = _s(variants, f"P5A_IGNORE_CONFLICT_{mode}")
        better = False
        if a and b:
            if (a.get("expectancy") or -1e9) > (b.get("expectancy") or -1e9):
                better = True
            if (a.get("max_drawdown") or -1e9) > (b.get("max_drawdown") or -1e9):  # less neg better
                better = better or ((a.get("expectancy") or 0) >= (b.get("expectancy") or 0) - 1e-9)
            if better and (a.get("expectancy") or 0) >= (b.get("expectancy") or 0):
                c_score += 1
            elif (a.get("expectancy") or 0) < (b.get("expectancy") or 0):
                c_score -= 1
    conflict_dec = (
        "CONFLICT_EXIT_IMPROVES_FULL_STRATEGY"
        if c_score > 0
        else "CONFLICT_EXIT_NO_FULL_STRATEGY_VALUE"
    )

    # Tier A vs ALL — risk-adjusted: PF and maxDD and expectancy
    t_score = 0
    for mode in ("DOGE", "BTC", "COMBINED"):
        a = _s(variants, f"PRIMARY_{mode}")
        b = _s(variants, f"ALL_P5A_CONFLICT_{mode}")
        if not a or not b:
            continue
        # opportunity-adjusted: cumulative / rough opportunity — use expectancy & PF & DD
        a_score = (a.get("expectancy") or 0) + 0.1 * ((a.get("profit_factor") or 1) - 1) + 0.001 * (a.get("max_drawdown") or 0)
        b_score = (b.get("expectancy") or 0) + 0.1 * ((b.get("profit_factor") or 1) - 1) + 0.001 * (b.get("max_drawdown") or 0)
        # also compare cum/net per trade already in expectancy; prefer Tier A if better exp+PF even if less cum
        if (a.get("expectancy") or 0) > (b.get("expectancy") or 0) and (a.get("profit_factor") or 0) >= (b.get("profit_factor") or 0) - 0.05:
            t_score += 1
        elif (b.get("cumulative_net") or 0) > (a.get("cumulative_net") or 0) * 1.5 and (b.get("expectancy") or 0) > 0:
            t_score -= 1
    tier_dec = (
        "TIER_A_IMPROVES_RISK_ADJUSTED_STRATEGY"
        if t_score >= 0
        else "ALL_SIGNALS_BETTER_AT_STRATEGY_LEVEL"
    )

    # 4h
    four_votes = []
    for mode in ("DOGE", "BTC", "COMBINED"):
        a = _s(variants, f"PRIMARY_{mode}")
        b = _s(variants, f"PRIMARY_4H_TP6_{mode}")
        if a and b and a.get("expectancy") is not None and b.get("expectancy") is not None:
            four_votes.append(
                {
                    "mode": mode,
                    "d_exp": float(b["expectancy"]) - float(a["expectancy"]),
                    "d_pf": (float(b["profit_factor"] or 0) - float(a["profit_factor"] or 0)),
                    "d_dd": float(b.get("max_drawdown") or 0) - float(a.get("max_drawdown") or 0),
                }
            )
    if four_votes:
        mean_exp = sum(x["d_exp"] for x in four_votes) / len(four_votes)
        mean_pf = sum(x["d_pf"] for x in four_votes) / len(four_votes)
        mean_dd = sum(x["d_dd"] for x in four_votes) / len(four_votes)
        if mean_exp > 0 and mean_pf >= -0.05 and mean_dd >= -5:
            four_dec = "4H_TP6_SL3_PREFERRED"
        elif mean_exp < 0 and mean_pf <= 0:
            four_dec = "4H_TP4_SL2_PREFERRED"
        elif mean_pf < -0.05 or mean_dd < -5:
            four_dec = "4H_TP4_SL2_PREFERRED"
        else:
            four_dec = "4H_EXIT_RESULT_MIXED"
    else:
        four_dec = "4H_EXIT_RESULT_MIXED"
        mean_exp = mean_pf = mean_dd = None

    fee013 = _s(variants, "FEE_013_COMBINED")
    edge_at_013 = pos(fee013)

    # suppression
    funnel_primary = []
    for k in ("PRIMARY_DOGE", "PRIMARY_BTC"):
        funnel_primary.extend(variants[k]["funnels"])

    answers = {
        "A": {
            "question": "Ist die vollständige Strategie nach 0.11% Kosten positiv?",
            "answer": pos(comb),
            "combined_expectancy": comb.get("expectancy"),
            "combined_cum": comb.get("cumulative_net"),
            "decision": primary,
        },
        "B": {
            "question": "Expectancy / PF / maxDD / Trades/Monat?",
            "DOGE": {
                "expectancy": doge.get("expectancy"),
                "PF": doge.get("profit_factor"),
                "maxDD": doge.get("max_drawdown"),
                "trades": doge.get("trades"),
                "trades_per_month": (variants["PRIMARY_DOGE"].get("monthly_meta") or {}).get("DOGEUSDT", {}).get("trades_per_month"),
            },
            "BTC": {
                "expectancy": btc.get("expectancy"),
                "PF": btc.get("profit_factor"),
                "maxDD": btc.get("max_drawdown"),
                "trades": btc.get("trades"),
                "trades_per_month": (variants["PRIMARY_BTC"].get("monthly_meta") or {}).get("BTCUSDT", {}).get("trades_per_month"),
            },
            "COMBINED": {
                "expectancy": comb.get("expectancy"),
                "PF": comb.get("profit_factor"),
                "maxDD": comb.get("max_drawdown"),
                "trades": comb.get("trades"),
            },
        },
        "C": {
            "question": "DOGE und BTC beide positiv?",
            "doge_positive": pos(doge),
            "btc_positive": pos(btc),
        },
        "D": {
            "question": "LONG und SHORT beide positiv?",
            "sides": {
                "COMBINED": variants["PRIMARY_COMBINED"]["by_side"],
                "DOGE": variants["PRIMARY_DOGE"]["by_side"],
                "BTC": variants["PRIMARY_BTC"]["by_side"],
            },
        },
        "E": {
            "question": "Hilft P5A chronologisch?",
            "decision": p5_dec,
            "primary_vs_p0": {
                m: {
                    "p5a_exp": _s(variants, f"PRIMARY_{m}").get("expectancy"),
                    "p0_exp": _s(variants, f"P0_TIERA_CONFLICT_{m}").get("expectancy"),
                    "p5a_cum": _s(variants, f"PRIMARY_{m}").get("cumulative_net"),
                    "p0_cum": _s(variants, f"P0_TIERA_CONFLICT_{m}").get("cumulative_net"),
                    "p5a_maxDD": _s(variants, f"PRIMARY_{m}").get("max_drawdown"),
                    "p0_maxDD": _s(variants, f"P0_TIERA_CONFLICT_{m}").get("max_drawdown"),
                }
                for m in ("DOGE", "BTC", "COMBINED")
            },
        },
        "F": {
            "question": "Hilft Conflict Exit?",
            "decision": conflict_dec,
        },
        "G": {
            "question": "Tier A only oder ALL?",
            "decision": tier_dec,
        },
        "H": {
            "question": "4h TP4/SL2 oder TP6/SL3?",
            "decision": four_dec,
            "mean_d_exp": mean_exp,
            "mean_d_pf": mean_pf,
            "mean_d_dd": mean_dd,
        },
        "I": {
            "question": "Wie viele Signale wegen offenem Trade unterdrückt?",
            "funnels": funnel_primary,
        },
        "J": {
            "question": "Schlimmster DD / Losing Streak?",
            "DOGE": {
                "maxDD": doge.get("max_drawdown"),
                "max_consec_losses": doge.get("max_consecutive_losses"),
                "worst_roll_20": doge.get("worst_rolling_20_trade_return"),
                "top_dds": variants["PRIMARY_DOGE"]["drawdowns"][:3],
            },
            "BTC": {
                "maxDD": btc.get("max_drawdown"),
                "max_consec_losses": btc.get("max_consecutive_losses"),
                "worst_roll_20": btc.get("worst_rolling_20_trade_return"),
                "top_dds": variants["PRIMARY_BTC"]["drawdowns"][:3],
            },
            "COMBINED": {
                "maxDD": comb.get("max_drawdown"),
                "max_consec_losses": comb.get("max_consecutive_losses"),
                "worst_roll_20": comb.get("worst_rolling_20_trade_return"),
                "top_dds": variants["PRIMARY_COMBINED"]["drawdowns"][:3],
            },
        },
        "K": {
            "question": "Edge bei 0.13% Gesamtkosten?",
            "fee_013_combined_positive": edge_at_013,
            "fee_013": fee013,
            "fee_015": _s(variants, "FEE_015_COMBINED"),
        },
    }

    decisions = {
        "primary": primary,
        "p5a": p5_dec,
        "conflict": conflict_dec,
        "tier": tier_dec,
        "four_h": four_dec,
    }
    return decisions, answers
