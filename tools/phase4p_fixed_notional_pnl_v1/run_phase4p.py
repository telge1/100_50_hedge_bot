#!/usr/bin/env python3
"""Phase 4P — fixed 1000 USDT notional PnL comparison (4H limit/OCO vs 4M market)."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parents[1]
PHASE4D = ROOT / "tools/phase4d_pool_landscape_backtester_v2"
PHASE4H = ROOT / "tools/phase4h_oco_pending_entry_v1"
PHASE4M = ROOT / "tools/phase4m_touch_rejection_market_backtester_v1"
RESULTS = ROOT / "results/phase4p_fixed_notional_pnl_v1"
REPORT = ROOT / "reports/DOGEUSDT_PHASE4P_FIXED_1000_USDT_PNL_BACKTEST_V1.md"

sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(PHASE4D))
sys.path.insert(0, str(PHASE4M))

from notional_pnl import (  # noqa: E402
    POSITION_NOTIONAL_USDT,
    ROUNDTRIP_FEE_PCT_LEGACY,
    ambiguous_scenarios,
    compute_trade_pnl,
    equity_curve_from_ledger,
    max_drawdown,
)
from signal_provider import load_all_signals  # noqa: E402
from trade_collectors import NormalizedTrade, collect_market_trades, collect_oco_trades  # noqa: E402


def load_prices() -> dict[str, dict]:
    path = ROOT / "results/phase3a_dynamic_pool_landscape_timeline_v1/landscape_timeline.csv"
    by_t = {}
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            by_t[row["candle_close_utc"]] = {
                "o": float(row["open"]),
                "h": float(row["high"]),
                "l": float(row["low"]),
                "c": float(row["close"]),
            }
    return by_t


def load_cfg() -> dict:
    return json.loads((PHASE4H / "strategy_config_oco_v1.json").read_text(encoding="utf-8"))


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    for r in rows:
        for k in r:
            if k not in fields:
                fields.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def ledger_row(t: NormalizedTrade, prices: dict, pilot_to: str) -> dict:
    ambiguous = t.execution_ambiguity != "NONE" or t.exit_reason in (
        "AMBIGUOUS_INTRABAR_EXIT",
        "AMBIGUOUS_ENTRY_EXIT_SEQUENCE",
    )
    open_at_end = t.exit_reason == "POSITION_OPEN_AT_DATA_END"
    mark_close = None
    if open_at_end and pilot_to in prices:
        mark_close = prices[pilot_to]["c"]

    pnl = compute_trade_pnl(
        t.direction,
        t.entry_price,
        t.exit_price,
        t.stop_loss_price,
        t.take_profit_price,
        ambiguous=ambiguous,
        open_at_end=open_at_end,
        mark_close=mark_close,
    )

    return {
        "strategy_variant": t.strategy_variant,
        "trade_id": t.trade_id,
        "direction": t.direction,
        "signal_utc": t.signal_utc,
        "entry_utc": t.entry_utc,
        "exit_utc": t.exit_utc or "",
        "entry_price": t.entry_price,
        "exit_price": t.exit_price if t.exit_price is not None else "",
        "entry_pool_bounds": f"{t.entry_pool_lower}–{t.entry_pool_upper}",
        "entry_pool_origin_ids": t.entry_pool_origin_ids,
        "tp_pool_bounds": (
            f"{t.tp_pool_bottom}–{t.tp_pool_top}"
            if t.tp_pool_bottom is not None and t.tp_pool_top is not None
            else ""
        ),
        "tp_pool_origin_ids": t.tp_pool_origin_ids,
        "stop_loss_price": t.stop_loss_price,
        "take_profit_price": t.take_profit_price,
        "quantity_doge": pnl.quantity_doge,
        "position_notional_usdt": POSITION_NOTIONAL_USDT,
        "entry_fee_usdt": pnl.entry_fee_usdt,
        "exit_fee_usdt": pnl.exit_fee_usdt if pnl.exit_fee_usdt is not None else "",
        "gross_pnl_usdt": pnl.gross_pnl_usdt if pnl.gross_pnl_usdt is not None else "",
        "net_pnl_usdt": pnl.net_pnl_usdt if pnl.net_pnl_usdt is not None else "",
        "gross_return_pct": pnl.gross_return_pct if pnl.gross_return_pct is not None else "",
        "net_return_pct": pnl.net_return_pct if pnl.net_return_pct is not None else "",
        "risk_usdt": pnl.risk_usdt,
        "reward_usdt": pnl.reward_usdt,
        "realized_R_multiple": pnl.realized_R_multiple if pnl.realized_R_multiple is not None else "",
        "exit_reason": t.exit_reason,
        "execution_ambiguity": t.execution_ambiguity if not ambiguous else "AMBIGUOUS_SL_TP_SEQUENCE",
        "eligibility_status": "STRATEGY_ELIGIBILITY_UNRESOLVED|TP_POOL_ELIGIBILITY_UNRESOLVED",
        "strategy_validation_status": "TECHNICAL_PNL_COMPARISON_ONLY",
        "candidate_id": t.candidate_id,
        "unrealized_pnl_usdt": pnl.unrealized_pnl_usdt if pnl.unrealized_pnl_usdt is not None else "",
    }


def summarize(variant: str, ledger: list[dict], signal_count: int) -> dict:
    closed = [r for r in ledger if r["exit_reason"] in ("SL", "TP") and r["execution_ambiguity"] == "NONE"]
    ambiguous = [r for r in ledger if r["execution_ambiguity"] != "NONE"]
    open_end = [r for r in ledger if r["exit_reason"] == "POSITION_OPEN_AT_DATA_END"]
    tp_n = sum(1 for r in closed if r["exit_reason"] == "TP")
    sl_n = sum(1 for r in closed if r["exit_reason"] == "SL")
    gross = sum(float(r["gross_pnl_usdt"]) for r in closed if r["gross_pnl_usdt"] != "")
    net = sum(float(r["net_pnl_usdt"]) for r in closed if r["net_pnl_usdt"] != "")
    fees = sum(
        float(r["entry_fee_usdt"]) + (float(r["exit_fee_usdt"]) if r["exit_fee_usdt"] != "" else 0.0)
        for r in closed
    )
    wins = [float(r["net_pnl_usdt"]) for r in closed if float(r["net_pnl_usdt"]) > 0]
    losses = [float(r["net_pnl_usdt"]) for r in closed if float(r["net_pnl_usdt"]) < 0]
    winrate = len(wins) / len(closed) * 100 if closed else 0.0
    profit_factor = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else ""
    r_vals = [float(r["realized_R_multiple"]) for r in closed if r["realized_R_multiple"] != ""]
    avg_r = sum(r_vals) / len(r_vals) if r_vals else ""
    curve = equity_curve_from_ledger(ledger)
    dd_u, dd_p = max_drawdown(curve)
    return {
        "strategy_variant": variant,
        "signal_count": signal_count,
        "positions_opened": len(ledger),
        "closed_tp": tp_n,
        "closed_sl": sl_n,
        "ambiguous_trades": len(ambiguous),
        "open_at_window_end": len(open_end),
        "gross_pnl_usdt": gross,
        "total_fees_usdt": fees,
        "net_pnl_usdt": net,
        "avg_win_usdt": sum(wins) / len(wins) if wins else "",
        "avg_loss_usdt": sum(losses) / len(losses) if losses else "",
        "winrate_closed_pct": winrate,
        "profit_factor": profit_factor,
        "avg_R_multiple": avg_r,
        "max_drawdown_usdt": dd_u,
        "max_drawdown_pct": dd_p,
        "max_concurrent_positions": 1,
        "comparison_class": "TECHNICAL_PNL_COMPARISON",
    }


def trade_differences(limit_l: list[dict], market_l: list[dict]) -> list[dict]:
    by_cid_l = {r["candidate_id"]: r for r in limit_l}
    by_cid_m = {r["candidate_id"]: r for r in market_l}
    keys = sorted(set(by_cid_l) | set(by_cid_m))
    rows = []
    for cid in keys:
        a = by_cid_l.get(cid)
        b = by_cid_m.get(cid)
        rows.append(
            {
                "candidate_id": cid,
                "in_limit_oco": bool(a),
                "in_market": bool(b),
                "limit_entry_price": a["entry_price"] if a else "",
                "market_entry_price": b["entry_price"] if b else "",
                "entry_price_delta": (
                    float(b["entry_price"]) - float(a["entry_price"]) if a and b else ""
                ),
                "limit_exit_price": a.get("exit_price", "") if a else "",
                "market_exit_price": b.get("exit_price", "") if b else "",
                "limit_net_pnl_usdt": a.get("net_pnl_usdt", "") if a else "",
                "market_net_pnl_usdt": b.get("net_pnl_usdt", "") if b else "",
            }
        )
    return rows


def market_signal_count(cfg: dict, prices: dict, signals: list) -> int:
    sys.path.insert(0, str(PHASE4M))
    from market_engine import WatchSpec, run_market_backtest  # noqa: E402
    from pool_zone_tracker import LifecycleIndex, anchor_from_signal, zone_status_at_bar  # noqa: E402
    from strategy_rules import plan_long, plan_short  # noqa: E402
    from exit_simulator import simulate_exit  # noqa: E402

    watches = [
        WatchSpec(
            watch_key=s.candidate_id,
            candidate_id=s.candidate_id,
            direction=s.direction,
            watch_from_utc=s.signal_close_utc,
            pool_lower=s.pool_lower,
            pool_upper=s.pool_upper,
            pool_identity=s.pool_identity,
            landscape_intervals=s.landscape_intervals,
            upper_tp_rows=s.upper_tp_rows,
        )
        for s in signals
    ]
    out = run_market_backtest(
        cfg,
        watches,
        prices,
        LifecycleIndex.load(),
        plan_short,
        plan_long,
        simulate_exit,
        zone_status_at_bar,
        anchor_from_signal,
    )
    return len(out["rejection_confirmations"])


def build_report(summ_l: dict, summ_m: dict) -> str:
    return "\n".join(
        [
            "# DOGEUSDT Phase 4P — Fixed 1000 USDT PnL Backtest V1",
            "",
            "**VERDICT:** `FIXED_NOTIONAL_PNL_PARTIAL`",
            "",
            f"Position notional: **{POSITION_NOTIONAL_USDT} USDT** (no compounding, no leverage).",
            f"Fees: **0.055% per side** (entry + exit notional). Legacy engines still report "
            f"`net_pnl_pct` using flat **{ROUNDTRIP_FEE_PCT_LEGACY}%** roundtrip — USDT ledger uses split model.",
            "",
            "**Classification:** `TECHNICAL_PNL_COMPARISON` — not `FULL_USER_STRATEGY_VALIDATION` "
            "(STRATEGY_ELIGIBILITY_UNRESOLVED, TP_POOL_ELIGIBILITY_UNRESOLVED).",
            "",
            "## Limit/OCO (4H)",
            "",
            f"- Gross PnL (closed, unambiguous): **{summ_l['gross_pnl_usdt']:.4f} USDT**",
            f"- Net PnL: **{summ_l['net_pnl_usdt']:.4f} USDT**",
            f"- Fees: **{summ_l['total_fees_usdt']:.4f} USDT**",
            f"- TP / SL / ambiguous / open: {summ_l['closed_tp']} / {summ_l['closed_sl']} / "
            f"{summ_l['ambiguous_trades']} / {summ_l['open_at_window_end']}",
            f"- Max drawdown (realized equity): **{summ_l['max_drawdown_usdt']:.4f} USDT** "
            f"({summ_l['max_drawdown_pct']:.4f}%)",
            "",
            "## Touch/Rejection Market (4M)",
            "",
            f"- Gross PnL: **{summ_m['gross_pnl_usdt']:.4f} USDT**",
            f"- Net PnL: **{summ_m['net_pnl_usdt']:.4f} USDT**",
            f"- Fees: **{summ_m['total_fees_usdt']:.4f} USDT**",
            f"- TP / SL / ambiguous / open: {summ_m['closed_tp']} / {summ_m['closed_sl']} / "
            f"{summ_m['ambiguous_trades']} / {summ_m['open_at_window_end']}",
            f"- Max drawdown: **{summ_m['max_drawdown_usdt']:.4f} USDT** ({summ_m['max_drawdown_pct']:.4f}%)",
            "",
            "Market fills: next 5m open, **slippage not modeled**.",
            "",
            f"`results/phase4p_fixed_notional_pnl_v1/`",
            "",
            "**STOP**",
        ]
    )


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    cfg = load_cfg()
    prices = load_prices()
    signals = load_all_signals(cfg)

    oco_trades = collect_oco_trades(cfg, prices, signals)
    mkt_trades = collect_market_trades(cfg, prices, signals)

    ledger_limit = [ledger_row(t, prices, cfg["pilot_to_utc"]) for t in oco_trades]
    ledger_market = [ledger_row(t, prices, cfg["pilot_to_utc"]) for t in mkt_trades]

    ambiguous_rows = []
    for r in ledger_limit + ledger_market:
        if r["execution_ambiguity"] != "NONE":
            amb = ambiguous_scenarios(
                r["direction"],
                float(r["entry_price"]),
                float(r["stop_loss_price"]),
                float(r["take_profit_price"]),
            )
            ambiguous_rows.append({**r, **amb})

    open_rows = [r for r in ledger_limit + ledger_market if r["exit_reason"] == "POSITION_OPEN_AT_DATA_END"]

    summ_l = summarize("LIMIT_OCO_4H", ledger_limit, len(signals))
    summ_m = summarize("TOUCH_REJECTION_MARKET_4M", ledger_market, market_signal_count(cfg, prices, signals))
    comparison = [summ_l, summ_m]

    write_csv(RESULTS / "trade_ledger_limit.csv", ledger_limit)
    write_csv(RESULTS / "trade_ledger_market.csv", ledger_market)
    write_csv(RESULTS / "pnl_comparison.csv", comparison)
    write_csv(RESULTS / "equity_curve_limit.csv", equity_curve_from_ledger(ledger_limit))
    write_csv(RESULTS / "equity_curve_market.csv", equity_curve_from_ledger(ledger_market))
    write_csv(RESULTS / "ambiguous_trades.csv", ambiguous_rows)
    write_csv(RESULTS / "open_positions_at_end.csv", open_rows)
    write_csv(RESULTS / "trade_level_differences.csv", trade_differences(ledger_limit, ledger_market))

    md = [
        "# Phase 4P PnL summary",
        "",
        f"limit net (closed): {summ_l['net_pnl_usdt']}",
        f"market net (closed): {summ_m['net_pnl_usdt']}",
        "",
        "See pnl_comparison.csv for full metrics.",
    ]
    (RESULTS / "pnl_summary.md").write_text("\n".join(md), encoding="utf-8")
    REPORT.write_text(build_report(summ_l, summ_m), encoding="utf-8")

    print(json.dumps({"verdict": "FIXED_NOTIONAL_PNL_PARTIAL", "limit_trades": len(ledger_limit), "market_trades": len(ledger_market)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
