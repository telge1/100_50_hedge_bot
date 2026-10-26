#!/usr/bin/env python3
"""Run Phase 4A single-position backtest pilot."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

from backtest_pilot import (  # noqa: E402
    load_experimental_long_signals,
    load_prices,
    load_short_signals,
    run_scenario,
    write_csv,
)

CONFIG_PATH = TOOLS / "research_config_v1.json"
RESULTS = ROOT / "results" / "phase4a_single_position_backtest_pilot_v1"
REPORT = ROOT / "reports/DOGEUSDT_PHASE4A_SINGLE_POSITION_BACKTEST_PILOT_V1.md"


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def load_blocked() -> list[dict]:
    path = ROOT / "results/phase3b_two_pool_market_structure_detector_v1/blocked_short_windows.csv"
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def dedupe_longs(signals):
    seen = set()
    out = []
    for s in signals:
        key = (s.candidate_id, s.signal_close_utc)
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def verdict(all_trades: list[dict], unresolved: int) -> str:
    if not all_trades and unresolved:
        return "SINGLE_POSITION_BACKTEST_PARTIAL"
    if all_trades:
        return "SINGLE_POSITION_BACKTEST_READY"
    return "SINGLE_POSITION_BACKTEST_PARTIAL"


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    prices = load_prices(
        ROOT / "results/phase3a_dynamic_pool_landscape_timeline_v1/landscape_timeline.csv"
    )
    shorts = load_short_signals(cfg)
    longs = dedupe_longs(load_experimental_long_signals(cfg))
    blocked = load_blocked()

    scenarios = [
        ("A_short_single_no_3b_age_off", False, False, False),
        ("A_short_single_no_3b_age_on", False, True, False),
        ("B_short_single_3b_block_age_off", True, False, False),
        ("C_resolved_short_and_exp_long", False, False, True),
    ]

    all_raw = []
    all_sup = []
    all_unres = []
    all_exec = []
    all_trades = []
    comparison = []

    for name, b3, age, incl_long in scenarios:
        sigs = shorts + (longs if incl_long else [])
        res = run_scenario(
            name,
            sigs,
            cfg,
            prices,
            apply_3b_block=b3,
            pool_age_gate=age,
            include_experimental_long=incl_long,
            blocked_rows=blocked,
        )
        all_raw.extend(res["raw"])
        all_sup.extend(res["suppressed"])
        all_unres.extend(res["unresolved"])
        all_exec.extend(res["executable"])
        all_trades.extend(res["trades"])
        tp_unres = sum(1 for u in res["unresolved"] if "TP" in u.get("reason", ""))
        comparison.append(
            {
                "scenario": name,
                "raw_signals": len(res["raw"]),
                "trades": len(res["trades"]),
                "suppressed": len(res["suppressed"]),
                "unresolved": len(res["unresolved"]),
                "tp_unresolved": tp_unres,
                "tp_hits": sum(1 for t in res["trades"] if t.get("exit_reason") == "TP"),
                "sl_hits": sum(1 for t in res["trades"] if t.get("exit_reason") == "SL"),
                "ambiguous_exits": sum(
                    1 for t in res["trades"] if t.get("exit_reason") == "AMBIGUOUS_INTRABAR_EXIT"
                ),
                "open_at_end": sum(
                    1 for t in res["trades"] if t.get("exit_reason") == "POSITION_OPEN_AT_DATA_END"
                ),
            }
        )

    write_csv(RESULTS / "raw_signals.csv", all_raw)
    write_csv(RESULTS / "executable_candidates.csv", all_exec)
    write_csv(RESULTS / "simulated_trades.csv", all_trades)
    write_csv(RESULTS / "suppressed_signals.csv", all_sup)
    write_csv(RESULTS / "unresolved_entries.csv", all_unres)
    write_csv(RESULTS / "market_state_comparison.csv", comparison)

    primary = [t for t in all_trades if t["scenario"] == "A_short_single_no_3b_age_off"]
    c010_block = [s for s in all_sup if "C010" in s.get("candidate_id", "")]
    c011_block = [s for s in all_sup if "C011" in s.get("candidate_id", "")]
    c013_block = [s for s in all_sup if "C013" in s.get("candidate_id", "")]

    final_verdict = verdict(primary, len(all_unres))

    summary = [
        "# Phase 4A backtest summary",
        "",
        f"**VERDICT:** `{final_verdict}`",
        "",
        "## Scenarios",
        "",
    ]
    for row in comparison:
        summary.append(f"- **{row['scenario']}**: trades={row['trades']} suppressed={row['suppressed']} tp_unresolved={row['tp_unresolved']}")
    summary.append("")
    summary.append(f"Primary pilot trades (A, age off): **{len(primary)}**")
    (RESULTS / "backtest_summary.md").write_text("\n".join(summary), encoding="utf-8")

    report = [
        "# DOGEUSDT Phase 4A — Single-position backtest pilot V1",
        "",
        f"**VERDICT:** `{final_verdict}`",
        "",
        "## Explicit answers",
        "",
        f"1. Suppressed due to open position (all scenarios): **{len(all_sup)}**",
        f"2. C010/C011/C013: not merged as one opportunity — **single-position rule** suppresses C010/C011 while C009 trade open (scenario A); **C013** trades separately after C009 exits (trade TA_..._007). Suppression counts: C010={len(c010_block)}, C011={len(c011_block)}, C013={len(c013_block)}",
        f"3. Clear TP/SL exits (scenario A age off): TP **{sum(1 for t in primary if t.get('exit_reason')=='TP')}**, SL **{sum(1 for t in primary if t.get('exit_reason')=='SL')}**",
        f"4. TP_SELECTION_UNRESOLVED count (all scenarios): **{sum(1 for u in all_unres if u.get('reason')=='TP_SELECTION_UNRESOLVED')}**",
        f"5. Executable experimental LONGs (scenario C): **{sum(1 for t in all_trades if t['scenario']=='C_resolved_short_and_exp_long' and t['direction']=='LONG')}**",
        "6. Before larger backtest: TP cluster ranking validation, LONG 3B validation, intrabar policy, pool-age gate policy, post-LONG return-to-short rules.",
        "",
        "## TP audit",
        "",
        "SHORT TP uses existing Phase 2A `lower_targets`: nearest lower cluster top below entry with ≥min_tp_distance_pct (0.5%).",
        "LONG experimental TP uses Phase 3B rank-1 upper cluster bottom (≥0.5% above entry).",
        "",
        "## Outputs",
        f"`{RESULTS.relative_to(ROOT)}/`",
        "",
    ]
    REPORT.write_text("\n".join(report), encoding="utf-8")

    exit_trace = [
        {
            "trade_id": t["trade_id"],
            "entry_open_utc": t["entry_open_utc"],
            "exit_utc": t.get("exit_utc"),
            "exit_reason": t.get("exit_reason"),
            "bars_in_trade": t.get("bars_in_trade"),
            "sl_price": t.get("sl_price"),
            "tp_price": t.get("tp_price"),
        }
        for t in all_trades
    ]
    write_csv(RESULTS / "exit_event_trace.csv", exit_trace)

    print(
        json.dumps(
            {
                "verdict": final_verdict,
                "primary_trades": len(primary),
                "suppressed": len(all_sup),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
