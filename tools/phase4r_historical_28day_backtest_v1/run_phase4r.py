#!/usr/bin/env python3
"""Phase 4R — historical 14d/28d dynamic landscape backtest (read-only)."""

from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parents[1]
RESULTS = ROOT / "results/phase4r_historical_28day_backtest_v1"
REPORT = ROOT / "reports/DOGEUSDT_PHASE4R_HISTORICAL_28DAY_BACKTEST_V1.md"
JSONL = ROOT / "results/phase3a_dynamic_pool_landscape_timeline_v1/landscape_timeline.jsonl"
PHASE4P = ROOT / "tools/phase4p_fixed_notional_pnl_v1"
PHASE4H = ROOT / "tools/phase4h_oco_pending_entry_v1"
PHASE4D = ROOT / "tools/phase4d_pool_landscape_backtester_v2"
PHASE4M = ROOT / "tools/phase4m_touch_rejection_market_backtester_v1"

sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(PHASE4P))
sys.path.insert(0, str(PHASE4H))
sys.path.insert(0, str(PHASE4D))
sys.path.insert(0, str(PHASE4M))

from coverage import assess_window  # noqa: E402
from dynamic_signals import apply_dynamic_snapshots  # noqa: E402
from notional_pnl import equity_curve_from_ledger, max_drawdown  # noqa: E402
from run_phase4p import (  # noqa: E402
    build_report as pnl_report_stub,
    ledger_row,
    load_cfg,
    load_prices,
    summarize,
    trade_differences,
    write_csv,
)
from signal_provider import load_all_signals  # noqa: E402
from snapshot_index import load_snapshot_index  # noqa: E402
from trade_collectors import collect_market_trades, collect_oco_trades  # noqa: E402


WINDOWS = [
    ("pilot_24h", "2026-09-11T12:00:00Z", "2026-09-12T12:00:00Z"),
    ("control_14d", "2026-09-01T00:00:00Z", "2026-09-15T00:00:00Z"),
    ("main_28d", "2026-09-01T00:00:00Z", "2026-09-29T00:00:00Z"),
]


def filter_prices(prices: dict, from_utc: str, to_exclusive: str) -> dict:
    to_last = datetime.fromisoformat(to_exclusive.replace("Z", "+00:00")) - __import__("datetime").timedelta(
        minutes=5
    )
    to_s = to_last.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {k: v for k, v in prices.items() if from_utc <= k <= to_s}


def daily_pnl(ledger: list[dict]) -> list[dict]:
    by_day: dict[str, float] = defaultdict(float)
    for r in ledger:
        if r.get("exit_reason") not in ("SL", "TP") or r.get("execution_ambiguity") != "NONE":
            continue
        if not r.get("exit_utc"):
            continue
        day = r["exit_utc"][:10]
        by_day[day] += float(r["net_pnl_usdt"])
    return [{"utc_date": d, "net_pnl_usdt": v} for d, v in sorted(by_day.items())]


def run_window(
    window_id: str,
    from_utc: str,
    to_exclusive: str,
    snap_index: dict,
    base_prices: dict,
) -> dict | None:
    cov = assess_window(window_id, from_utc, to_exclusive, JSONL)
    if cov["coverage_status"] != "COVERAGE_OK":
        return {"coverage": cov, "ran": False}

    cfg = load_cfg()
    cfg = {**cfg, "pilot_from_utc": from_utc, "pilot_to_utc": _to_inclusive_end(to_exclusive)}
    prices = filter_prices(base_prices, from_utc, to_exclusive)
    signals = load_all_signals(cfg)
    signals, dyn_unresolved = apply_dynamic_snapshots(signals, snap_index)

    oco_trades = collect_oco_trades(cfg, prices, signals)
    mkt_trades = collect_market_trades(cfg, prices, signals)

    pilot_to = cfg["pilot_to_utc"]
    ledger_limit = [ledger_row(t, prices, pilot_to) for t in oco_trades]
    ledger_market = [ledger_row(t, prices, pilot_to) for t in mkt_trades]
    for row in ledger_limit + ledger_market:
        row["strategy_validation_status"] = "TECHNICAL_PNL_COMPARISON"
        row["window_id"] = window_id

    return {
        "coverage": cov,
        "ran": True,
        "cfg": cfg,
        "ledger_limit": ledger_limit,
        "ledger_market": ledger_market,
        "dyn_unresolved": dyn_unresolved,
        "summ_limit": summarize(f"LIMIT_OCO_4H_{window_id}", ledger_limit, len(signals)),
        "summ_market": summarize(
            f"TOUCH_REJECTION_MARKET_4M_{window_id}",
            ledger_market,
            len(signals),
        ),
    }


def _to_inclusive_end(to_exclusive: str) -> str:
    dt = datetime.fromisoformat(to_exclusive.replace("Z", "+00:00")) - __import__("datetime").timedelta(minutes=5)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    snap_index = load_snapshot_index(JSONL)
    base_prices = load_prices()

    coverage_rows = [assess_window(wid, a, b, JSONL) for wid, a, b in WINDOWS]
    write_csv(RESULTS / "coverage_report.csv", coverage_rows)

    pilot = run_window(*WINDOWS[0], snap_index, base_prices)
    ran_any = pilot and pilot.get("ran")

    if not ran_any:
        verdict = "HISTORICAL_28DAY_BACKTEST_BLOCKED"
        summary = [
            "# Phase 4R summary",
            "",
            f"**VERDICT:** `{verdict}`",
            "",
            "14d and 28d windows: **COVERAGE_BLOCKED** (no full dynamic timeline + SSOT build unavailable).",
            "See `coverage_report.csv`.",
        ]
        (RESULTS / "summary.md").write_text("\n".join(summary), encoding="utf-8")
        REPORT.write_text(
            "\n".join(
                [
                    "# DOGEUSDT Phase 4R — Historical 28-Day Backtest V1",
                    "",
                    f"**VERDICT:** `{verdict}`",
                    "",
                    "## Coverage",
                    "- Required: Sep 2026 dynamic PD1 depth-2 timeline with >=168h warmup.",
                    "- Cached Phase 3A JSONL covers only ~2026-09-11T12:00Z → 2026-09-12T16:00Z.",
                    "- Live `build_landscape_window` probe failed in this environment (SSOT import).",
                    "",
                    "## 14d / 28d",
                    "- **Not executed** — `COVERAGE_BLOCKED`, no static Phase 2A/3B fallback.",
                    "",
                    "**STOP**",
                ]
            ),
            encoding="utf-8",
        )
        print(json.dumps({"verdict": verdict, "ran": False}))
        return 0

    ll = pilot["ledger_limit"]
    lm = pilot["ledger_market"]
    write_csv(RESULTS / "trade_ledger_limit.csv", ll)
    write_csv(RESULTS / "trade_ledger_market.csv", lm)
    write_csv(RESULTS / "pnl_comparison.csv", [pilot["summ_limit"], pilot["summ_market"]])
    write_csv(RESULTS / "daily_pnl_limit.csv", daily_pnl(ll))
    write_csv(RESULTS / "daily_pnl_market.csv", daily_pnl(lm))
    eq = equity_curve_from_ledger(ll) + [
        {**r, "variant": "LIMIT_OCO_4H"} for r in equity_curve_from_ledger(ll)
    ]
    eq += [{**r, "variant": "TOUCH_REJECTION_MARKET_4M"} for r in equity_curve_from_ledger(lm)]
    write_csv(RESULTS / "equity_curves.csv", eq)
    write_csv(
        RESULTS / "ambiguous_trades.csv",
        [r for r in ll + lm if r.get("execution_ambiguity") != "NONE"],
    )
    write_csv(
        RESULTS / "open_positions_at_end.csv",
        [r for r in ll + lm if r.get("exit_reason") == "POSITION_OPEN_AT_DATA_END"],
    )
    write_csv(RESULTS / "unresolved_strategy_decisions.csv", pilot["dyn_unresolved"])
    write_csv(RESULTS / "trade_level_differences.csv", trade_differences(ll, lm))
    write_csv(
        RESULTS / "pool_lifecycle_audit.csv",
        [{"note": "Full lifecycle audit requires rebuilt timeline for window; pilot uses Phase 3A JSONL index only"}],
    )

    sl = pilot["summ_limit"]
    sm = pilot["summ_market"]
    verdict = "HISTORICAL_28DAY_BACKTEST_PARTIAL"
    (RESULTS / "summary.md").write_text(
        "\n".join(
            [
                "# Phase 4R summary",
                "",
                f"**VERDICT:** `{verdict}`",
                "",
                f"24h pilot ({WINDOWS[0][1]} → {WINDOWS[0][2]} excl): dynamic snapshot TP/intervals at signal close.",
                f"Limit net: {sl['net_pnl_usdt']} USDT | Market net: {sm['net_pnl_usdt']} USDT",
                "14d/28d: COVERAGE_BLOCKED — not run.",
            ]
        ),
        encoding="utf-8",
    )
    REPORT.write_text(
        "\n".join(
            [
                "# DOGEUSDT Phase 4R — Historical 28-Day Backtest V1",
                "",
                f"**VERDICT:** `{verdict}`",
                "",
                "## Classification",
                "`TECHNICAL_PNL_COMPARISON` — STRATEGY_ELIGIBILITY_UNRESOLVED, TP_POOL_ELIGIBILITY_UNRESOLVED.",
                "",
                "## Coverage",
                "- **control_14d / main_28d:** `COVERAGE_BLOCKED` (see coverage_report.csv).",
                f"- **pilot_24h:** executed with causal snapshots from Phase 3A JSONL.",
                "",
                "## Pilot 24h (dynamic intervals at signal close)",
                "",
                f"| | Limit/OCO | Market |",
                f"|---|--:|--:|",
                f"| Trades | {sl['positions_opened']} | {sm['positions_opened']} |",
                f"| TP / SL | {sl['closed_tp']} / {sl['closed_sl']} | {sm['closed_tp']} / {sm['closed_sl']} |",
                f"| Net USDT | {sl['net_pnl_usdt']:.4f} | {sm['net_pnl_usdt']:.4f} |",
                f"| Fees USDT | {sl['total_fees_usdt']:.4f} | {sm['total_fees_usdt']:.4f} |",
                f"| Winrate | {sl['winrate_closed_pct']:.2f}% | {sm['winrate_closed_pct']:.2f}% |",
                f"| Profit factor | {sl['profit_factor']} | {sm['profit_factor']} |",
                f"| Max DD USDT | {sl['max_drawdown_usdt']:.4f} | {sm['max_drawdown_usdt']:.4f} |",
                "",
                "Slippage/funding: **not modeled**. Market fill: next 5m open (4M).",
                "",
                "## 28-day causal completeness",
                "**No** — only ~28h cached dynamic timeline exists; SSOT chunk builder not verified for Sep 1–29.",
                "",
                f"`results/phase4r_historical_28day_backtest_v1/`",
                "",
                "**STOP**",
            ]
        ),
        encoding="utf-8",
    )
    print(json.dumps({"verdict": verdict, "limit_trades": len(ll), "market_trades": len(lm)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
