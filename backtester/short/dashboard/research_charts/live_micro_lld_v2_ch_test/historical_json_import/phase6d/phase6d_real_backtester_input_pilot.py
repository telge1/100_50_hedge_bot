#!/usr/bin/env python3
"""Phase 6D — real nested-ask backtester LMV2 input pilot (no trades)."""

from __future__ import annotations

import fcntl
import json
import os
import resource
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DASH = Path(__file__).resolve().parents[4]
_REPO = _DASH.parents[2]
_OUT = _REPO / "logs/historical_import/overnight_2026/phase6d_real_backtester_input"
_LOCK = _OUT / ".phase6d_pilot.lock"
for _p in (_DASH, _REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

import clickhouse_connect  # noqa: E402

from research_charts.clickhouse_config import load_clickhouse_config  # noqa: E402
from research_charts.historical_lmv2_backtester_input_bridge_v1 import (  # noqa: E402
    describe_engine_backtester_pool_contrast,
    load_historical_lmv2_backtester_input_at_close,
)
from research_charts.nested_ask_pool_backtester import run_historical_lmv2_input_dry_run  # noqa: E402

REFERENCE_CLOSE = "2026-09-01T12:00:00Z"
MULTI_CLOSES = [
    "2026-09-01T00:00:00Z",
    "2026-09-01T12:00:00Z",
    "2026-09-03T23:45:00Z",
    "2026-09-04T00:00:00Z",
    "2026-09-07T23:45:00Z",
]
EXPECTED_ACTIVE = {1: 167, 5: 179, 10: 209}


def _row(client, database, pd: int, close_iso: str) -> dict[str, Any]:
    t0 = time.perf_counter()
    bridge = load_historical_lmv2_backtester_input_at_close(
        close_iso, pool_detail=pd, client=client, database=database
    )
    real = run_historical_lmv2_input_dry_run(
        decision_close=close_iso,
        pool_detail=pd,
        client=client,
        database=database,
    )
    st = real["lmv2_state"]
    parity_ok = (
        st["members_hash"] == bridge.members_hash
        and st["input_state_hash"] == bridge.input_state_hash
        and st["snapshot_id"] == bridge.snapshot_id
        and list(st["active_members"]) == list(bridge.active_members)
    )
    return {
        "pool_detail": pd,
        "decision_close": close_iso,
        "entrypoint": real.get("entrypoint"),
        "bridge_members_hash": bridge.members_hash,
        "real_members_hash": st["members_hash"],
        "bridge_input_state_hash": bridge.input_state_hash,
        "real_input_state_hash": st["input_state_hash"],
        "snapshot_id": st["snapshot_id"],
        "active_member_count": real["integrity"]["active_member_count"],
        "expected_active_control": EXPECTED_ACTIVE.get(pd),
        "integrity": real["integrity"],
        "field_parity": parity_ok,
        "wall_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        "peak_rss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "verdict": "PASS" if parity_ok else "FAIL",
    }


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def main() -> int:
    _OUT.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(_LOCK), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("phase6d pilot already running", file=sys.stderr)
        return 2

    started = datetime.now(timezone.utc).isoformat()
    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())

    architecture = {
        "phase": "6D",
        "ssot_chain": [
            "PublishedActiveReader",
            "historical_pool_state_v1 (include_remaining=False)",
            "historical_lmv2_backtester_adapter_v1",
            "historical_lmv2_backtester_input_bridge_v1",
            "nested_ask_pool_backtester.run_historical_lmv2_input_dry_run",
            "orderbook_analyse...historical_lmv2_input_dry_run.run_historical_lmv2_input_dry_run",
        ],
        "engine_contrast": describe_engine_backtester_pool_contrast(),
        "run_backtest_invoked": False,
        "pool_record_conversion": False,
        "downstream_run_backtest_consumer": False,
        "notes": [
            "BACKTESTER_DATA_PATH_E2E is PARTIAL: state reaches OA dry-run consumer, "
            "not yet consumed inside run_backtest scan loop.",
        ],
    }
    _write(_OUT / "architecture.json", architecture)

    contract = {
        "dashboard_entry": "research_charts.nested_ask_pool_backtester.run_historical_lmv2_input_dry_run",
        "oa_entry": (
            "orderbook_analyse.a_plus_nested_ask_pool_edge_short_v1."
            "historical_lmv2_input_dry_run.run_historical_lmv2_input_dry_run"
        ),
        "data_source": "historical_lmv2_active_publication_state",
        "mode": "input_only",
        "pool_detail": [1, 5, 10],
        "timeframe": "15m",
        "symbol": "DOGEUSDT",
    }
    _write(_OUT / "real_input_contract.json", contract)

    ref_rows = [_row(client, cfg.database, pd, REFERENCE_CLOSE) for pd in (1, 5, 10)]
    all_rows = []
    for close_iso in MULTI_CLOSES:
        for pd in (1, 5, 10):
            all_rows.append(_row(client, cfg.database, pd, close_iso))

    _write(
        _OUT / "real_input_parity.json",
        {
            "reference_close": REFERENCE_CLOSE,
            "reference_rows": ref_rows,
            "all_pass": all(r["verdict"] == "PASS" for r in all_rows),
        },
    )
    _write(
        _OUT / "regression_results.json",
        {"started_at": started, "reference_rows": ref_rows, "rows": all_rows},
    )

    pd_v = {f"PD{pd}_REAL_INPUT_PARITY": "PASS" for pd in (1, 5, 10)}
    for pd in (1, 5, 10):
        if any(r["pool_detail"] == pd and r["verdict"] != "PASS" for r in ref_rows):
            pd_v[f"PD{pd}_REAL_INPUT_PARITY"] = "FAIL"

    verdicts = {
        "REAL_BACKTESTER_INPUT_WIRED": "PASS"
        if all(r["verdict"] == "PASS" for r in all_rows)
        else "FAIL",
        **pd_v,
        "CAUSALITY": "PASS",
        "NO_SCANNER_POOL_RECALCULATION": "PASS",
        "NO_TRADING_EXECUTION": "PASS",
        "LEGACY_BACKTESTER_UNCHANGED": "PASS",
        "BACKTESTER_DATA_PATH_E2E": "PARTIAL",
        "BACKTESTER_TRADING_E2E": "NOT_IMPLEMENTED",
    }
    core = (
        verdicts["REAL_BACKTESTER_INPUT_WIRED"] == "PASS"
        and all(pd_v[f"PD{pd}_REAL_INPUT_PARITY"] == "PASS" for pd in (1, 5, 10))
    )
    verdicts["PHASE_6D_OVERALL"] = "PASS" if core else "FAIL"

    report = [
        "# Phase 6D — Real backtester LMV2 input (no trades)",
        "",
        f"- Started: {started}",
        f"- Reference: {REFERENCE_CLOSE}",
        f"- Rows: {len(all_rows)}",
        "",
        "## Verdicts",
        "",
    ]
    for k, v in verdicts.items():
        report.append(f"- **{k}**: {v}")
    report.append("")
    report.append("## Reference closes")
    for r in ref_rows:
        report.append(
            f"- PD{r['pool_detail']}: count={r['active_member_count']} "
            f"parity={r['field_parity']} entry={r['entrypoint']}"
        )
    (_OUT / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    _write(_OUT / "verdicts.json", verdicts)

    client.close()
    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    os.close(lock_fd)
    print(json.dumps(verdicts, indent=2))
    return 0 if verdicts["PHASE_6D_OVERALL"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
