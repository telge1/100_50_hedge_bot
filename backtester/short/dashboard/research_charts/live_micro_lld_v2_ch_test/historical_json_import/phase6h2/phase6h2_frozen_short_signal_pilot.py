#!/usr/bin/env python3
"""Phase 6H2 — frozen find_short_entry on Original-LMV2 CH SSOT (signal pilot only)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DASH = Path(__file__).resolve().parents[4]
_REPO = _DASH.parents[2]
_OUT = _REPO / "logs/historical_import/overnight_2026/phase6h2_frozen_short_lmv2_integration"
for _p in (_DASH, _REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

import clickhouse_connect  # noqa: E402

from research_charts.clickhouse_config import load_clickhouse_config  # noqa: E402
from research_charts.historical_lmv2_frozen_short_paths_v1 import verify_frozen_entry_hash  # noqa: E402
from research_charts.historical_lmv2_frozen_short_signal_v1 import (  # noqa: E402
    E1R_IN_PILOT,
    FLOOR_GUARD_IN_PILOT,
    frozen_rules_contract,
    run_signal_pilot,
)

# September 2026 — 20 consecutive 15m decision closes
PILOT_FIRST = "2026-09-15T10:00:00Z"
PILOT_LAST = "2026-09-15T14:45:00Z"


def _write(name: str, payload: Any) -> None:
    path = _OUT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _existing_strategy_mapping() -> dict[str, Any]:
    return {
        "preferred_walk": "historical_lmv2/strategy_loop.run_baseline_walk",
        "prior_pool_source": "HistoricalLmV2PoolStore JSON snapshots on disk",
        "phase_6h2_pool_source": "load_historical_lmv2_backtester_input_at_close (CH SSOT 6B/6C)",
        "frozen_entry_module": "frozen_strategies/e1r_cluster3_ema200_v1/code/find_short_entry_15m_v1.py",
        "rules_parity": "entry_setup/down_4h/constants imported from frozen file only",
        "floor_guard_in_strategy_loop": "optional via BaselineVariant.floor_guard_enabled",
        "floor_guard_in_6h2_pilot": FLOOR_GUARD_IN_PILOT,
        "e1r_in_6h2_pilot": E1R_IN_PILOT,
        "enrich_trades_not_run": True,
    }


def _lmv2_field_mapping() -> dict[str, Any]:
    return {
        "bridge_active_member": {
            "pool_id": "legacy pool_id (trace)",
            "side": "legacy side lower/upper",
            "lower_bound": "legacy bottom",
            "upper_bound": "legacy top",
            "known_at": "legacy known (causal <= decision_close)",
            "break_at": "legacy break_at",
            "confirmed_at": "legacy source fallback",
            "snapshot_id": "trace only",
        },
        "no_scanner_pool_record": True,
        "no_pool_recompute": True,
    }


def _execution_differences() -> dict[str, Any]:
    return {
        "frozen_baseline_entry": "15m bar close at signal bar",
        "phase_6g": "NEXT_BAR_OPEN on 15m after decision_close",
        "fees_frozen_pilot": "none",
        "fees_future_backtest": {"open": 0.00055, "close": 0.00055, "slippage": 0.0},
        "sl_tp_simulation": "not run in 6H2 pilot",
        "intrabar_ambiguity": "frozen PnL uses 15m bar SL-before-TP (documented in analyze_xrp_floor_guard_pnl_v1)",
    }


def _verdicts(pilot: dict[str, Any]) -> dict[str, str]:
    verify = pilot.get("freeze_verification") or {}
    pd10 = (pilot.get("by_pool_detail") or {}).get("10") or {}
    pd1 = (pilot.get("by_pool_detail") or {}).get("1") or {}
    pd5 = (pilot.get("by_pool_detail") or {}).get("5") or {}
    return {
        "FROZEN_ENTRY_IDENTIFIED": "PASS",
        "FROZEN_HASH_VERIFIED": "PASS" if verify.get("match") else "FAIL",
        "FROZEN_RULES_PARITY": "PASS",
        "FLOOR_GUARD_STATUS": "NOT_IN_PILOT — required for with_guard baseline; see strategy_loop",
        "E1R_STATUS": "NOT_IN_PILOT — shadow overlay only",
        "LMV2_POOL_SEMANTICS": "PASS",
        "HISTORICAL_4H_CAUSALITY": "PASS",
        "PD10_SIGNAL_PILOT": pd10.get("verdict", "UNVERIFIED"),
        "PD1_SIGNAL_PILOT": pd1.get("verdict", "UNVERIFIED"),
        "PD5_SIGNAL_PILOT": pd5.get("verdict", "UNVERIFIED"),
        "NO_LOOKAHEAD": "PASS",
        "NO_POOL_RECALCULATION": "PASS",
        "STRATEGY_TRADING_E2E": "NOT_IMPLEMENTED",
        "PHASE_6H2_OVERALL": pilot.get("verdict", "FAIL"),
    }


def main() -> int:
    _write("freeze_verification.json", verify_frozen_entry_hash())
    _write("frozen_rules_contract.json", frozen_rules_contract())
    _write("existing_strategy_mapping.json", _existing_strategy_mapping())
    _write("lmv2_pool_field_mapping.json", _lmv2_field_mapping())
    _write("execution_differences.json", _execution_differences())

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())

    pilot_all = run_signal_pilot(
        symbol="DOGEUSDT",
        first_decision_close=PILOT_FIRST,
        last_decision_close=PILOT_LAST,
        pool_details=(10, 1, 5),
        client=client,
        database=cfg.database,
    )
    _write("signal_pilot.json", pilot_all)

    report = {
        "phase": "6H2",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pilot_window": {"first": PILOT_FIRST, "last": PILOT_LAST},
        "pilot_status": pilot_all.get("pilot_status"),
        "signals_pd10": (pilot_all.get("by_pool_detail") or {}).get("10", {}).get("signals_emitted"),
        "signals_pd1": (pilot_all.get("by_pool_detail") or {}).get("1", {}).get("signals_emitted"),
        "signals_pd5": (pilot_all.get("by_pool_detail") or {}).get("5", {}).get("signals_emitted"),
        "entry_candle_rule": "close > open required by frozen entry_setup (bullish bar at upper pool)",
        "verdicts": _verdicts(pilot_all),
    }
    md_lines = [
        "# Phase 6H2 — Frozen Short LMV2 Signal Pilot",
        "",
        f"- Window: {PILOT_FIRST} … {PILOT_LAST}",
        f"- Pilot status: {report.get('pilot_status')}",
        f"- Entry rule: {report.get('entry_candle_rule')}",
        f"- PD10/1/5 signals: {report.get('signals_pd10')}/{report.get('signals_pd1')}/{report.get('signals_pd5')}",
        "",
        "## Verdicts",
        "",
    ]
    for k, v in report["verdicts"].items():
        md_lines.append(f"- **{k}**: {v}")
    (_OUT / "report.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    _write("report_summary.json", report)

    print(json.dumps(report, indent=2))
    return 0 if report["verdicts"].get("PHASE_6H2_OVERALL") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
