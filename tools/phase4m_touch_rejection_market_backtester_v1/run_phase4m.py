#!/usr/bin/env python3
"""Phase 4M — touch/rejection market entry backtester vs limit/OCO baseline (read-only)."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parents[1]
PHASE4D = ROOT / "tools/phase4d_pool_landscape_backtester_v2"
RESULTS = ROOT / "results/phase4m_touch_rejection_market_backtester_v1"
REPORT = ROOT / "reports/DOGEUSDT_PHASE4M_TOUCH_REJECTION_MARKET_BACKTESTER_V1.md"
PHASE4D_RESULTS = ROOT / "results/phase4d_pool_landscape_backtester_v2"

sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(PHASE4D))

from engine import run_backtest  # noqa: E402
from exit_simulator import simulate_exit  # noqa: E402
from market_engine import WatchSpec, run_market_backtest  # noqa: E402
from pool_zone_tracker import LifecycleIndex, anchor_from_signal, zone_status_at_bar  # noqa: E402
from signal_provider import load_all_signals  # noqa: E402
from strategy_rules import plan_long, plan_short  # noqa: E402


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
    return json.loads((PHASE4D / "strategy_config_v2.json").read_text(encoding="utf-8"))


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


def signals_to_watches(signals) -> list[WatchSpec]:
    out: list[WatchSpec] = []
    for sig in signals:
        out.append(
            WatchSpec(
                watch_key=sig.candidate_id,
                candidate_id=sig.candidate_id,
                direction=sig.direction,
                watch_from_utc=sig.signal_close_utc,
                pool_lower=sig.pool_lower,
                pool_upper=sig.pool_upper,
                pool_identity=sig.pool_identity,
                landscape_intervals=sig.landscape_intervals,
                upper_tp_rows=sig.upper_tp_rows,
            )
        )
    return out


def limit_baseline(cfg: dict, prices: dict) -> dict:
    """Re-run 4D engine in-process; does not modify 4D artifacts on disk."""
    signals = load_all_signals(cfg)
    lifecycle = LifecycleIndex.load()
    return run_backtest(cfg, signals, prices, lifecycle=lifecycle)


def comparison_rows(limit_trades: list[dict], market_trades: list[dict], market_out: dict) -> list[dict]:
    touches = market_out["pool_touch_events"]
    upper_t = sum(1 for e in touches if e["direction"] == "SHORT")
    lower_t = sum(1 for e in touches if e["direction"] == "LONG")
    short_rej = sum(1 for e in market_out["rejection_confirmations"] if e["direction"] == "SHORT")
    long_rej = sum(1 for e in market_out["rejection_confirmations"] if e["direction"] == "LONG")
    fills = [i for i in market_out["market_entry_intents"] if i.get("market_entry_intent") == "MARKET_FILL"]
    return [
        {
            "metric": "limit_mode_trades",
            "limit_oco_v2": len(limit_trades),
            "touch_rejection_market_v1": len(market_trades),
        },
        {
            "metric": "upper_pool_touches",
            "limit_oco_v2": "N/A",
            "touch_rejection_market_v1": upper_t,
        },
        {
            "metric": "lower_pool_touches",
            "limit_oco_v2": "N/A",
            "touch_rejection_market_v1": lower_t,
        },
        {
            "metric": "short_rejections_confirmed",
            "limit_oco_v2": "N/A",
            "touch_rejection_market_v1": short_rej,
        },
        {
            "metric": "long_rejections_confirmed",
            "limit_oco_v2": "N/A",
            "touch_rejection_market_v1": long_rej,
        },
        {
            "metric": "pool_watch_aborts",
            "limit_oco_v2": "N/A",
            "touch_rejection_market_v1": len(market_out["pool_watch_aborts"]),
        },
        {
            "metric": "market_entry_intents_filled",
            "limit_oco_v2": "N/A",
            "touch_rejection_market_v1": len(fills),
        },
        {
            "metric": "unresolved_decisions",
            "limit_oco_v2": "",
            "touch_rejection_market_v1": len(market_out["unresolved_strategy_decisions"]),
        },
    ]


def build_report(cfg: dict, comp: list[dict], market_out: dict, limit_trades: list[dict]) -> str:
    fills = [i for i in market_out["market_entry_intents"] if i.get("market_entry_intent") == "MARKET_FILL"]
    short_rej = sum(1 for e in market_out["rejection_confirmations"] if e["direction"] == "SHORT")
    long_rej = sum(1 for e in market_out["rejection_confirmations"] if e["direction"] == "LONG")
    return "\n".join(
        [
            "# DOGEUSDT Phase 4M — Touch/Rejection Market Backtester V1",
            "",
            "**VERDICT:** `TOUCH_REJECTION_MARKET_BACKTESTER_PARTIAL`",
            "",
            "Active mode for this run: **touch/rejection market** (`NEXT_5M_OPEN_DIAGNOSTIC`). "
            "Limit/OCO V2 re-run in-process only; on-disk Phase 4D/4H artifacts unchanged.",
            "",
            "## Entry logic",
            "",
            "- SHORT: touch `high >= upper_pool_lower`; confirm later `close < upper_pool_lower` (strict).",
            "- LONG: touch `low <= lower_pool_upper`; confirm later `close > lower_pool_upper` (strict).",
            "- No candle-color gate; edge close on pool boundary does not confirm.",
            "- Fill at **next 5m open** after confirm close; no intrabar execution.",
            "",
            "## SL/TP",
            "",
            f"- Unchanged V2 rules (`sl_above_upper_pool_pct={cfg['sl_above_upper_pool_pct']}`, "
            f"`min_tp_distance_pct={cfg['min_tp_distance_pct']}`).",
            "- `TP_POOL_ELIGIBILITY_UNRESOLVED` not auto-cleared.",
            "- All market trades: `performance_validated=false`.",
            "",
            "## Pilot counts",
            "",
            f"- Short rejections confirmed: **{short_rej}**",
            f"- Long rejections confirmed: **{long_rej}**",
            f"- Diagnostic market fills: **{len(fills)}**",
            f"- Limit-mode trades (4D re-run): **{len(limit_trades)}**",
            "",
            "## Limit vs market (fills)",
            "",
            "Limit fills at pool edge when bracketed; market fills at next open after rejection — "
            "expect different entry times/prices on the same candidate pools.",
            "",
            "## Blocking eligibility",
            "",
            "- `STRATEGY_ELIGIBILITY_UNRESOLVED` (no Phase 4K hard filters).",
            "- `TP_POOL_ELIGIBILITY_UNRESOLVED` for strategy proof.",
            "- Landscape intervals for TP still Phase 2A/3B snapshot (not full per-bar SSOT slice).",
            "",
            f"`results/phase4m_touch_rejection_market_backtester_v1/`",
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
    watches = signals_to_watches(signals)
    lifecycle = LifecycleIndex.load()

    market_out = run_market_backtest(
        cfg,
        watches,
        prices,
        lifecycle,
        plan_short,
        plan_long,
        simulate_exit,
        zone_status_at_bar,
        anchor_from_signal,
    )

    limit_out = limit_baseline(cfg, prices)
    limit_trades = limit_out["simulated_trades"]

    comp = comparison_rows(limit_trades, market_out["simulated_market_trades"], market_out)
    write_csv(RESULTS / "pool_touch_events.csv", market_out["pool_touch_events"])
    write_csv(RESULTS / "rejection_confirmations.csv", market_out["rejection_confirmations"])
    write_csv(RESULTS / "market_entry_intents.csv", market_out["market_entry_intents"])
    write_csv(RESULTS / "simulated_market_trades.csv", market_out["simulated_market_trades"])
    write_csv(RESULTS / "limit_vs_market_comparison.csv", comp)
    unresolved = market_out["unresolved_strategy_decisions"] + market_out.get("pool_watch_aborts", [])
    write_csv(RESULTS / "unresolved_strategy_decisions.csv", unresolved)

    summary = [
        "# Phase 4M market backtest summary",
        "",
        f"pilot: {cfg['pilot_from_utc']} .. {cfg['pilot_to_utc']}",
        f"touch_events: {len(market_out['pool_touch_events'])}",
        f"rejections: {len(market_out['rejection_confirmations'])}",
        f"market_trades: {len(market_out['simulated_market_trades'])}",
        f"limit_trades_rerun: {len(limit_trades)}",
        "",
        "Fill model: NEXT_5M_OPEN_DIAGNOSTIC, slippage_modeled=false.",
        "Not a validated strategy performance proof.",
    ]
    (RESULTS / "market_backtest_summary.md").write_text("\n".join(summary), encoding="utf-8")
    REPORT.write_text(
        build_report(cfg, comp, market_out, limit_trades),
        encoding="utf-8",
    )

    print(
        json.dumps(
            {
                "verdict": "TOUCH_REJECTION_MARKET_BACKTESTER_PARTIAL",
                "rejections": len(market_out["rejection_confirmations"]),
                "fills": len(market_out["simulated_market_trades"]),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
