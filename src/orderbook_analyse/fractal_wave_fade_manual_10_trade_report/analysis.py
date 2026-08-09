"""Build manual 10-trade audit report from final validated trades."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from orderbook_analyse.fractal_wave_fade_manual_10_trade_report import (
    AUDIT_VERSION,
    CASHOUT_RATE,
    COVERAGE_RATE,
    FEE_PCT,
    HIST_EQUITY,
    OUT_DIR_DEFAULT,
    PRIMARY_WINDOW_END,
    PRIMARY_WINDOW_START,
    REF_TRADES,
    START_ACTIVE,
    START_RESERVE,
    TARGET_N,
)
from orderbook_analyse.fractal_wave_fade_manual_10_trade_report.context import (
    build_context_index,
    context_for_trade,
)
from orderbook_analyse.fractal_wave_fade_manual_10_trade_report.equity import (
    attach_historical_full_path,
    simulate_local_equity,
)
from orderbook_analyse.fractal_wave_fade_manual_10_trade_report.export import write_report
from orderbook_analyse.fractal_wave_fade_manual_10_trade_report.select import (
    ensure_min_trades,
    load_trades,
    select_manual_sample,
)
from orderbook_analyse.fractal_wave_fade_manual_10_trade_report.verify import (
    levels_for_trade,
    load_1m_range,
    verify_trade,
)


def _fmt(x) -> str:
    t = pd.Timestamp(x)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return t.strftime("%Y-%m-%d %H:%M:%S UTC")


def run_manual_report(
    *,
    trades_path: Path = REF_TRADES,
    out_dir: Path = OUT_DIR_DEFAULT,
    hist_equity_path: Path = HIST_EQUITY,
) -> dict[str, Any]:
    trades = load_trades(trades_path)
    primary_start = pd.Timestamp(PRIMARY_WINDOW_START, tz="UTC")
    primary_end = pd.Timestamp(PRIMARY_WINDOW_END, tz="UTC")
    pool, win_start, win_end, win_note = ensure_min_trades(
        trades, primary_start=primary_start, primary_end=primary_end, min_n=TARGET_N
    )
    sample = select_manual_sample(pool, target_n=TARGET_N)

    # signal context index for needed (symbol, tf)
    pairs = sorted({(str(r.symbol), str(r.first_signal_tf)) for r in sample.itertuples()})
    symbols = sorted({p[0] for p in pairs})
    tfs = sorted({p[1] for p in pairs})
    print(f"[context] building waves for {len(pairs)} symbol/tf pairs …", flush=True)
    ctx_index = build_context_index(symbols, tfs)

    # candle cache per symbol covering sample span ±1d
    candle_cache: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        sub = sample[sample["symbol"] == sym]
        a = sub["entry_time"].min() - pd.Timedelta(hours=1)
        b = sub["exit_time"].max() + pd.Timedelta(hours=1)
        print(f"[verify] load 1m {sym} {_fmt(a)} → {_fmt(b)} …", flush=True)
        candle_cache[sym] = load_1m_range(sym, a, b)

    eq_rows, inv = simulate_local_equity(
        sample,
        start_active=START_ACTIVE,
        start_reserve=START_RESERVE,
        cashout_rate=CASHOUT_RATE,
        coverage_rate=COVERAGE_RATE,
    )
    eq_by_id = {int(r["trade_id"]): r for r in eq_rows}
    attach_historical_full_path(
        eq_rows,
        hist_equity_path,
        cashout_rate=CASHOUT_RATE,
        coverage_rate=COVERAGE_RATE,
    )

    rows: list[dict[str, Any]] = []
    for _, tr in sample.iterrows():
        levels = levels_for_trade(tr)
        ctx = context_for_trade(tr, ctx_index)
        candles = candle_cache[str(tr["symbol"])]
        ver = verify_trade(tr, candles, levels)
        eq = eq_by_id[int(tr["trade_id"])]
        rows.append(
            {
                "trade_id": int(tr["trade_id"]),
                "symbol": str(tr["symbol"]),
                "side": str(tr["side"]),
                "first_signal_tf": str(tr["first_signal_tf"]),
                "highest_tf_reached": str(tr["highest_tf_reached"]),
                "signal_time": tr["signal_time"],
                "signal_available_at": ctx["signal_available_at"],
                "entry_time": tr["entry_time"],
                "entry_price": float(tr["entry_price"]),
                **levels,
                "upgrade_count": int(tr["upgrade_count"]),
                "upgrade_sequence": str(tr["upgrade_sequence"]),
                "exit_time": tr["exit_time"],
                "exit_price": float(tr["exit_price"]),
                "exit_reason": str(tr["exit_reason"]),
                "gross_return_pct": float(tr["gross_return_pct"]),
                "fee_pct": float(tr["fee_pct"]),
                "net_return_pct": float(tr["net_return_pct"]),
                "holding_minutes": float(tr["holding_minutes"]),
                "wave_direction": ctx["wave_direction"],
                "fade_direction": ctx["fade_direction"],
                "tier": ctx["tier"],
                "trend_aligned": ctx["trend_aligned"],
                "directional_efficiency": ctx["directional_efficiency"],
                "q_bucket": ctx["q_bucket"],
                "context_match": ctx["context_match"],
                **eq,
                **ver,
            }
        )

    summary = {
        "audit_version": AUDIT_VERSION,
        "trades_path": str(trades_path),
        "window_start": _fmt(win_start),
        "window_end": _fmt(win_end),
        "window_note": win_note,
        "pool_n": int(len(pool)),
        "selected_n": int(len(rows)),
        "start_active": START_ACTIVE,
        "start_reserve": START_RESERVE,
        "cashout_rate": CASHOUT_RATE,
        "coverage_rate": COVERAGE_RATE,
        "fee_pct": FEE_PCT,
        "counts": {
            "long": int(sum(1 for r in rows if r["side"] == "LONG")),
            "short": int(sum(1 for r in rows if r["side"] == "SHORT")),
            "tp": int(sum(1 for r in rows if r["exit_reason"] == "TP")),
            "sl": int(sum(1 for r in rows if r["exit_reason"] == "SL")),
            "other_exit": int(
                sum(1 for r in rows if r["exit_reason"] not in ("TP", "SL"))
            ),
            "upgrades": int(sum(1 for r in rows if int(r["upgrade_count"]) > 0)),
            "winners": int(sum(1 for r in rows if float(r["net_return_pct"]) > 0)),
            "losers": int(sum(1 for r in rows if float(r["net_return_pct"]) <= 0)),
        },
        "verification": {
            "all_entry_pass": all(bool(r["entry_verified"]) for r in rows),
            "all_exit_pass": all(bool(r["exit_verified"]) for r in rows),
            "all_tp_sl_pass": all(bool(r["tp_sl_verified"]) for r in rows),
            "all_upgrade_pass": all(bool(r["upgrade_verified"]) for r in rows),
            "all_manual_pass": all(r["manual_audit_status"] == "PASS" for r in rows),
        },
        "accounting": {k: v for k, v in inv.items() if k != "checks_per_trade"},
        "accounting_invariants": inv["ACCOUNTING_INVARIANTS"],
        "final_local_equity": {
            "active": float(rows[-1]["active_after"]) if rows else START_ACTIVE,
            "reserve": float(rows[-1]["reserve_after"]) if rows else START_RESERVE,
            "total": float(rows[-1]["total_after"]) if rows else START_ACTIVE,
        },
        "selected_trade_ids": [int(r["trade_id"]) for r in rows],
    }

    paths = write_report(rows=rows, summary=summary, out_dir=out_dir)
    summary["paths"] = {k: str(v) for k, v in paths.items()}
    return {"rows": rows, "summary": summary, "paths": paths}
