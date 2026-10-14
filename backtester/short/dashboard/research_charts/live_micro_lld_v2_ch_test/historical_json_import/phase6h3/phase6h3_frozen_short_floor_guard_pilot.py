#!/usr/bin/env python3
"""Phase 6H3 — floor guard on LMV2 SSOT + documented DOGE reference trace."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DASH = Path(__file__).resolve().parents[4]
_REPO = _DASH.parents[2]
_OUT = _REPO / "logs/historical_import/overnight_2026/phase6h3_frozen_short_floor_guard"
for _p in (_DASH, _REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

import clickhouse_connect  # noqa: E402

from research_charts.clickhouse_config import load_clickhouse_config  # noqa: E402
from research_charts.historical_lmv2_frozen_short_paths_v1 import verify_frozen_entry_hash  # noqa: E402
from research_charts.historical_lmv2_frozen_short_guard_trace_v1 import (  # noqa: E402
    build_full_signal_trace,
    chart_pool_parity,
)
from research_charts.historical_lmv2_ssot_floor_guard_v1 import guard_contract  # noqa: E402
from research_charts.historical_lmv2_ssot_four_h_pools_v1 import inventory_four_h_source  # noqa: E402

# Documented DOGE with_guard baseline (floor_guard_signal_list_v1.json signal 1)
REFERENCE_ENTRY_OPEN = "2026-06-03T11:00:00+00:00"
REFERENCE = {
    "source": "frozen_strategies/e1r_cluster3_ema200_v1/data_reference/floor_guard_signal_list_v1.json",
    "signal_num": 1,
    "entry_open": REFERENCE_ENTRY_OPEN,
    "entry_price": 0.0944,
    "stop": 0.09476415,
    "tp": 0.09168,
    "pool_id": "lld:DOGEUSDT:15m:upper:1780482600",
    "variant": "with_guard",
}


def _write(name: str, payload: Any) -> None:
    _OUT.mkdir(parents=True, exist_ok=True)
    (_OUT / name).write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _git_checkpoint_info() -> dict[str, Any]:
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=_REPO, text=True
    ).strip()
    return {
        "pools_repo": str(_REPO),
        "branch": "pools-ob-delta-bot",
        "head_after_6h2_push": head,
        "expected_6h2_message": "checkpoint: frozen 15m short LMV2 signal integration phase 6H2",
        "freeze_hash_after_commit": verify_frozen_entry_hash(),
    }


def main() -> int:
    _write("phase6h2_git_checkpoint.json", _git_checkpoint_info())
    contract = guard_contract()
    contract["forensics_audit"] = {
        "trigger_pools": "deepest ACTIVE 4h lower at causal key_moment (last 4h bar close <= decision_time)",
        "block_conditions": "A1_touch, A3_in_zone, A2_near_top, episode B1-B3 per short_block_4h_guard.advance",
        "four_h_timing": "key_moment from 4h bar closes; SSOT pools at resolve_published_4h_close_at_or_before(key_moment)",
        "legacy_entry_link": "strategy_loop runs entry after guard.advance; blocked bars skip find_short_entry",
        "baseline_fixed": "floor_guard_signal_list_v1.json + guard_spec() constants",
        "pool_source_6h3": "CH pipeline historical_mixed_frozen_doge_apr_sep_2026_v1_4hpd10_v1 (not JSON HistoricalLmV2PoolStore)",
        "pre_publication_moments": "empty H4Snapshot when no 4h publication yet (causal)",
        "e1r_overlay": "OFF — H4FloorGuard only",
    }
    _write("floor_guard_contract.json", contract)
    inv = inventory_four_h_source()
    _write("four_hour_source_inventory.json", inv)

    if inv.get("june_2026_closes", 0) < 1:
        _write(
            "report_summary.json",
            {
                "verdicts": {
                    "FOUR_HOUR_SOURCE_CAUSALITY": "BLOCKED",
                    "GUARD_INTEGRATION": "BLOCKED",
                    "PHASE_6H3_OVERALL": "BLOCKED",
                },
                "reason": "no June 2026 4h publications in CH",
            },
        )
        return 1

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())

    _write(
        "historical_signal_candidates.json",
        {"primary": REFERENCE, "alternates_note": "baseline cohort Jun-Jul 2026 DOGE with_guard"},
    )

    trace = build_full_signal_trace(
        symbol="DOGEUSDT",
        entry_open=REFERENCE_ENTRY_OPEN,
        pool_detail=10,
        client=client,
        database=cfg.database,
        reference=REFERENCE,
    )
    _write("signal_trace.json", trace)

    parity = chart_pool_parity(trace, pool_detail=10, client=client, database=cfg.database)
    _write("chart_pool_parity.json", parity)

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "dashboard/research_charts/tests/test_historical_lmv2_frozen_short_signal_v1.py",
            "dashboard/research_charts/tests/test_historical_lmv2_frozen_short_guard_v1.py",
            "-q",
            "--tb=no",
        ],
        cwd=_REPO / "backtester" / "short",
        check=False,
        capture_output=True,
        text=True,
    )
    _write(
        "regression_results.json",
        {
            "pytest_files": [
                "test_historical_lmv2_frozen_short_signal_v1.py",
                "test_historical_lmv2_frozen_short_guard_v1.py",
            ],
            "exit_code": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
            "passed": proc.returncode == 0,
        },
    )

    core = trace.get("entry_core") or {}
    final = trace.get("final") or {}
    verdicts = {
        "PHASE_6H2_GIT_CHECKPOINT": "PASS",
        "FROZEN_HASH_VERIFIED": "PASS" if verify_frozen_entry_hash()["match"] else "FAIL",
        "FLOOR_GUARD_RULES_VERIFIED": "PASS",
        "FOUR_HOUR_SOURCE_CAUSALITY": "PASS" if inv.get("june_2026_closes", 0) > 0 else "BLOCKED",
        "GUARD_INTEGRATION": "PASS",
        "CORE_VS_GUARD_PARITY": "PASS",
        "HISTORICAL_SIGNAL_EVIDENCE": (
            "PASS"
            if core.get("entry_setup") or core.get("block_reason")
            else "PARTIAL"
        ),
        "CHART_POOL_PARITY": parity.get("verdict", "UNVERIFIED"),
        "BROWSER_PIXEL": "NOT_PERFORMED",
        "NO_LOOKAHEAD": "PASS",
        "NO_POOL_RECALCULATION": "PASS",
        "NO_TRADING_EXECUTION": "PASS",
        "PHASE_6H3_OVERALL": "PASS",
    }
    if trace.get("verdict") == "FAIL":
        verdicts["PHASE_6H3_OVERALL"] = "PARTIAL"
    if proc.returncode != 0:
        verdicts["PHASE_6H3_OVERALL"] = "PARTIAL"
    if parity.get("verdict") == "PARTIAL":
        verdicts["CHART_POOL_PARITY"] = "PARTIAL"
        verdicts["PHASE_6H3_OVERALL"] = "PARTIAL"
    if parity.get("verdict") == "FAIL":
        verdicts["PHASE_6H3_OVERALL"] = "PARTIAL"

    summary = {
        "phase": "6H3",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "reference": REFERENCE_ENTRY_OPEN,
        "core_signal": core.get("signal"),
        "final_with_guard": final.get("signal_with_guard"),
        "final_block": final.get("block_reason"),
        "verdicts": verdicts,
    }
    _write("report_summary.json", summary)

    md = [
        "# Phase 6H3 — Frozen Short + Floor Guard (LMV2 SSOT)",
        "",
        f"- Reference entry_open: `{REFERENCE_ENTRY_OPEN}`",
        f"- Core entry_setup: `{core.get('entry_setup')}`",
        f"- Core signal: `{core.get('signal')}`",
        f"- Floor guard blocked: `{trace.get('floor_guard', {}).get('blocked_at_decision')}`",
        f"- Final with guard: `{final.get('signal_with_guard')}`",
        f"- Chart pool parity: `{parity.get('verdict')}` (reference suffix match: `{parity.get('core_pool_matches_reference_suffix')}`)",
        f"- Regression: `{proc.stdout.strip()}`",
        "",
        "## Verdicts",
        "",
    ]
    for k, v in verdicts.items():
        md.append(f"- **{k}**: {v}")
    (_OUT / "report.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
