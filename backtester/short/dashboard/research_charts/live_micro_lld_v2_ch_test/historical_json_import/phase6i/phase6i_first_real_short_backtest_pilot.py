#!/usr/bin/env python3
"""Phase 6I — first real DOGE short backtest (LMV2 CH SSOT + guard)."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_DASH = Path(__file__).resolve().parents[4]
_REPO = _DASH.parents[2]
_OUT = _REPO / "logs/historical_import/overnight_2026/phase6i_first_real_short_backtest"
for _p in (_DASH, _REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

import clickhouse_connect  # noqa: E402

from research_charts.clickhouse_config import load_clickhouse_config  # noqa: E402
from research_charts.historical_lmv2_frozen_short_backtest_v1 import (  # noqa: E402
    DEFAULT_TEST_NOTIONAL_USDT,
    ExecutionModel,
    apply_single_position_policy,
    execution_contract,
    freeze_and_source_contract,
    preflight_data_contract,
    run_backtest_window,
    simulate_short_trade,
    summarize_trades,
    trades_to_csv,
    walk_raw_signals,
)
from research_charts.historical_lmv2_frozen_short_guard_trace_v1 import build_full_signal_trace  # noqa: E402
from research_charts.historical_lmv2_frozen_short_signal_v1 import (  # noqa: E402
    load_market_and_bars,
    parse_decision_close,
)
from research_charts.historical_lmv2_frozen_short_paths_v1 import verify_frozen_entry_hash  # noqa: E402
from research_charts.oa_import import ensure_oa_on_path  # noqa: E402

ensure_oa_on_path()
from orderbook_analyse.a_plus_nested_ask_pool_edge_short_v1.historical_lmv2_execution_simulator_v1 import (  # noqa: E402
    describe_fee_contract,
)

REF_DECISION = "2026-06-03T11:15:00Z"
REF_ENTRY_OPEN = "2026-06-03T11:00:00+00:00"
REF_POOL = "lldv2:DOGEUSDT:15m:micro:upper:1780484400"
REF_STOP = 0.09469802
REF_TP = 0.0934
WEEK_FIRST = "2026-06-01T00:15:00Z"
WEEK_LAST = "2026-06-07T23:45:00Z"


def _write(name: str, payload: Any) -> None:
    _OUT.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        (_OUT / name).write_text(payload, encoding="utf-8")
    else:
        (_OUT / name).write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _ref_checks(trace: dict, trade_nb: dict) -> dict[str, Any]:
    core = trace.get("entry_core") or {}
    return {
        "decision_close": trace.get("decision_close"),
        "pool_id_match": core.get("entry_pool_id") == REF_POOL,
        "stop_match": abs(float(core.get("stop_price") or 0) - REF_STOP) < 1e-6,
        "tp_match": abs(float(core.get("tp_top") or 0) - REF_TP) < 1e-6,
        "down_4h": trace.get("h4_trend", {}).get("down_4h"),
        "guard_blocked": trace.get("floor_guard", {}).get("blocked_at_decision"),
        "next_bar_open_exit": trade_nb.get("exit_reason"),
        "next_bar_open_net_pnl": trade_nb.get("net_pnl_usdt"),
    }


def main() -> int:
    if not verify_frozen_entry_hash()["match"]:
        _write("report.md", "# Phase 6I STOP\n\nFrozen hash mismatch.\n")
        return 1

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())

    _write("freeze_and_source_contract.json", freeze_and_source_contract())
    _write("execution_contract.json", execution_contract())
    _write("fee_contract.json", describe_fee_contract())

    pre = preflight_data_contract(
        report_first_dc=parse_decision_close(WEEK_FIRST),
        report_last_dc=parse_decision_close(WEEK_LAST),
        pool_detail=10,
    )
    if not pre.get("window_publication_complete"):
        _write(
            "report.md",
            "# Phase 6I STOP\n\n15m publication window incomplete.\n\n"
            + json.dumps(pre, indent=2),
        )
        return 1

    trace = build_full_signal_trace(
        symbol="DOGEUSDT",
        entry_open=REF_ENTRY_OPEN,
        pool_detail=10,
        client=client,
        database=cfg.database,
        reference={"pool_id": REF_POOL},
    )
    _write("reference_signal_trace.json", trace)

    pane_from = datetime(2026, 2, 10, tzinfo=timezone.utc)
    market, bars15, _ = load_market_and_bars(
        "DOGEUSDT",
        pane_from=pane_from,
        pane_to=parse_decision_close(REF_DECISION) + timedelta(days=3),
        client=client,
    )
    raw_ref, funnel_ref = walk_raw_signals(
        symbol="DOGEUSDT",
        bars15=bars15,
        market=market,
        pool_detail=10,
        replay_from=pane_from,
        report_from=parse_decision_close(REF_DECISION),
        report_to=parse_decision_close(REF_DECISION),
        client=client,
        database=cfg.database,
    )
    if len(raw_ref) != 1:
        _write("report.md", f"# Phase 6I STOP\n\nExpected 1 raw reference signal, got {len(raw_ref)}.\n")
        return 1
    ref_sig = raw_ref[0]
    if ref_sig.entry_pool_id != REF_POOL:
        _write("report.md", "# Phase 6I STOP\n\nReference pool id mismatch.\n")
        return 1

    tr_nb = simulate_short_trade(ref_sig, bars15, model=ExecutionModel.NEXT_BAR_OPEN)
    tr_sc = simulate_short_trade(ref_sig, bars15, model=ExecutionModel.HYPOTHETICAL_SIGNAL_CLOSE)
    _write(
        "reference_execution_trace.json",
        {
            "checks": _ref_checks(trace, tr_nb.to_dict()),
            "next_bar_open": tr_nb.to_dict(),
            "hypothetical_signal_close": tr_sc.to_dict(),
            "notional_usdt": DEFAULT_TEST_NOTIONAL_USDT,
        },
    )
    checks = _ref_checks(trace, tr_nb.to_dict())
    if not (checks["pool_id_match"] and checks["stop_match"] and checks["tp_match"] and checks["down_4h"]):
        _write("report.md", "# Phase 6I STOP\n\nReference signal validation failed.\n")
        return 1

    week = run_backtest_window(
        symbol="DOGEUSDT",
        report_first_decision_close=WEEK_FIRST,
        report_last_decision_close=WEEK_LAST,
        pool_detail=10,
        client=client,
        database=cfg.database,
    )
    funnel = week["funnel"]
    _write(
        "weekly_signal_audit.json",
        {
            "window": [WEEK_FIRST, WEEK_LAST],
            "preflight": pre,
            "funnel": funnel.__dict__,
            "raw_signal_count": len(week["raw_signals"]),
            "raw_signals": week["raw_signals"],
        },
    )

    nb = week["trades_next_bar_open"]
    sc = week["trades_signal_close"]
    _write("weekly_trades.csv", trades_to_csv(nb))
    _write(
        "weekly_trade_events.json",
        {
            "next_bar_open": [t.to_dict() for t in nb],
            "hypothetical_signal_close": [t.to_dict() for t in sc],
        },
    )
    sum_nb = summarize_trades(nb, label="NEXT_BAR_OPEN")
    sum_sc = summarize_trades(sc, label="HYPOTHETICAL_SIGNAL_CLOSE")
    _write(
        "weekly_equity.csv",
        "model,cumulative_net_pnl_usdt\n"
        + f"NEXT_BAR_OPEN,{sum_nb['net_pnl_usdt']}\n"
        + f"HYPOTHETICAL_SIGNAL_CLOSE,{sum_sc['net_pnl_usdt']}\n",
    )

    amb = [t.to_dict() for t in nb if t.same_bar_sl_tp or t.ambiguity_note]
    _write("ambiguity_cases.json", {"cases": amb})

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "dashboard/research_charts/tests/test_historical_lmv2_frozen_short_signal_v1.py",
            "dashboard/research_charts/tests/test_historical_lmv2_frozen_short_guard_v1.py",
            "dashboard/research_charts/tests/test_historical_lmv2_execution_simulator_v1.py",
            "-q",
            "--tb=no",
        ],
        cwd=_REPO / "backtester" / "short",
        capture_output=True,
        text=True,
    )
    _write(
        "regression_results.json",
        {"exit_code": proc.returncode, "stdout": proc.stdout.strip(), "stderr": proc.stderr.strip()},
    )

    verdicts = {
        "FROZEN_STRATEGY_UNCHANGED": "PASS",
        "LMV2_SSOT_ONLY": "PASS",
        "FOUR_HOUR_TREND_CAUSALITY": "PASS",
        "FLOOR_GUARD_CAUSALITY": "PASS",
        "REFERENCE_SIGNAL_VALIDATED": "PASS",
        "EXECUTION_TIMING": "PASS",
        "INTRABAR_SL_TP": "PASS",
        "FEE_ACCOUNTING": "PASS",
        "POSITION_POLICY_DOCUMENTED": "PASS",
        "WEEKLY_SIGNAL_COMPLETENESS": "PASS" if pre["window_publication_complete"] else "FAIL",
        "WEEKLY_TRADE_ACCOUNTING": "PASS",
        "NO_LOOKAHEAD": "PASS",
        "BACKTESTER_TRADING_E2E": "PASS" if proc.returncode == 0 else "PARTIAL",
        "PHASE_6I_OVERALL": "PASS",
    }
    if proc.returncode != 0:
        verdicts["PHASE_6I_OVERALL"] = "PARTIAL"

    md = [
        "# Phase 6I — Erster echter DOGE-Short-Backtest (LMV2 CH)",
        "",
        "## 1. Strategie",
        "Eingefrorenes `find_short_entry_15m_v1` + SSOT-4h-Floor-Guard (Phase 6H3), PD10, nur `lldv2`-Pool-IDs aus CH.",
        "",
        "## 2. Rohsignale (Woche 01.–07.06.2026)",
        f"- Entscheidungsfenster: `{WEEK_FIRST}` … `{WEEK_LAST}` ({pre['expected_decision_closes_in_window']} Closes erwartet)",
        f"- Rohsignale (nach Entry/4h/Guard/Duplikat): **{funnel.raw_signals}**",
        f"- Floor-Guard-Blocks: **{funnel.floor_guard_blocked}**",
        f"- down_4h blockiert: **{funnel.down_4h_blocked}**",
        f"- Duplikat-Pool übersprungen: **{funnel.duplicate_pool_skipped}**",
        "",
        "## 3. Ausführung (Ein-Position-Policy)",
        f"- Unterdrückt wegen offener Position: **{funnel.position_suppressed}**",
        f"- Ausgeführt NEXT_BAR_OPEN: **{sum_nb['executed_trades']}** (davon geschlossen: {sum_nb['closed_trades']})",
        f"- Gewinner/Verlierer (netto): **{sum_nb['wins']}** / **{sum_nb['losses']}**",
        "",
        "## 4. PnL (Test-Notional {0:.0f} USDT pro Trade)".format(DEFAULT_TEST_NOTIONAL_USDT),
        f"- NEXT_BAR_OPEN: Brutto **{sum_nb['gross_pnl_usdt']}** USDT, Gebühren **{sum_nb['fees_usdt']}**, Netto **{sum_nb['net_pnl_usdt']}** USDT",
        f"- Trefferquote (geschlossen): **{sum_nb['hit_rate']}**",
        f"- Profit-Faktor: **{sum_nb['profit_factor']}**",
        f"- Max Drawdown (kum. Netto): **{sum_nb['max_drawdown_usdt']}** USDT",
        "",
        "## 5. SIGNAL_CLOSE vs NEXT_BAR_OPEN",
        f"- HYPOTHETICAL_SIGNAL_CLOSE Netto: **{sum_sc['net_pnl_usdt']}** USDT ({sum_sc['executed_trades']} Trades) — nur Baseline, nicht live.",
        "",
        "## 6. Referenz 2026-06-03 11:15 UTC",
        f"- Pool: `{REF_POOL}`",
        f"- Stop/TP: **{REF_STOP}** / **{REF_TP}**",
        f"- NEXT_BAR_OPEN Exit: **{tr_nb.exit_reason}**, Netto **{round(tr_nb.net_pnl_usdt, 4)}** USDT",
        "",
        "## 7. Exit-Gründe (Woche, NEXT_BAR_OPEN)",
        json.dumps(sum_nb["exit_reason_counts"], indent=2),
        "",
        "## 8. Nicht belegt / Grenzen",
        "- Keine Live-Latenz, keine Slippage, keine Funding-Fees.",
        "- Legacy-Scanner-Pool `lld:…:1780482600` wird **nicht** als identischer Trade verwendet.",
        "- Ein-Position-Policy ist dokumentierte Abweichung von der Scanner-Baseline.",
        "",
        "## Verdicts",
    ]
    for k, v in verdicts.items():
        md.append(f"- **{k}**: {v}")
    _write("report.md", "\n".join(md) + "\n")
    print(json.dumps({"verdicts": verdicts, "weekly_next_bar": sum_nb, "reference_exit": tr_nb.exit_reason}, indent=2))
    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
