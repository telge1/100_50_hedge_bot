#!/usr/bin/env python3
"""Phase 6C — dry-run harness: LMV2 input bridge without trades or run_backtest."""

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
_OUT = _REPO / "logs/historical_import/overnight_2026/phase6c_backtester_input_bridge"
_LOCK = _OUT / ".phase6c_dry_run.lock"
for _p in (_DASH, _REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

import clickhouse_connect  # noqa: E402

from research_charts.clickhouse_config import load_clickhouse_config  # noqa: E402
from research_charts.historical_lmv2_backtester_adapter_v1 import (  # noqa: E402
    load_backtester_active_state_at_close,
)
from research_charts.historical_lmv2_backtester_input_bridge_v1 import (  # noqa: E402
    bridge_matches_adapter,
    describe_engine_backtester_pool_contrast,
    load_historical_lmv2_backtester_input_at_close,
)
from research_charts.historical_lmv2_pd_contract_v1 import resolve_historical_pd_config  # noqa: E402
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.published_reader import (  # noqa: E402
    PublishedActiveReader,
)

REFERENCE_CLOSE = "2026-09-01T12:00:00Z"
MULTI_CLOSES = [
    "2026-09-01T00:00:00Z",
    "2026-09-01T12:00:00Z",
    "2026-09-03T23:45:00Z",
    "2026-09-04T00:00:00Z",
    "2026-09-07T23:45:00Z",
]
EXPECTED_ACTIVE = {1: 167, 5: 179, 10: 209}


def _causality_checks(bridge) -> list[dict[str, Any]]:
    issues = []
    dc = bridge.decision_close
    for m in bridge.active_members:
        known = m.get("known_at")
        if known is not None and str(known) > dc:
            issues.append(
                {
                    "code": "KNOWN_AT_AFTER_DECISION_CLOSE",
                    "pool_id": m.get("pool_id"),
                    "known_at": known,
                    "decision_close": dc,
                }
            )
    return issues


def _dry_run_row(
    client,
    database,
    reader: PublishedActiveReader,
    pd: int,
    close_iso: str,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    hcfg = resolve_historical_pd_config(pd)
    adapted = load_backtester_active_state_at_close(
        close_iso,
        pool_detail=pd,
        client=client,
        database=database,
        reader=reader,
    )
    bridge = load_historical_lmv2_backtester_input_at_close(
        close_iso,
        pool_detail=pd,
        client=client,
        database=database,
        reader=reader,
    )
    bridge_repeat = load_historical_lmv2_backtester_input_at_close(
        close_iso,
        pool_detail=pd,
        client=client,
        database=database,
        reader=reader,
    )
    parity = bridge_matches_adapter(adapted, bridge)
    causality = _causality_checks(bridge)
    row: dict[str, Any] = {
        "pool_detail": pd,
        "decision_close": close_iso,
        "pipeline_line_id": hcfg.pipeline_line_id,
        "config_hash": hcfg.config_hash,
        "snapshot_id": bridge.snapshot_id,
        "active_state_fingerprint": bridge.active_state_fingerprint,
        "active_member_count": bridge.active_member_count,
        "expected_active_control": EXPECTED_ACTIVE.get(pd),
        "members_hash_adapter": adapted.members_hash,
        "members_hash_bridge": bridge.members_hash,
        "input_state_hash": bridge.input_state_hash,
        "input_state_hash_repeat": bridge_repeat.input_state_hash,
        "deterministic_repeat": bridge.input_state_hash == bridge_repeat.input_state_hash,
        "adapter_bridge_parity": parity,
        "availability_class": bridge.availability_class,
        "data_kind": bridge.data_kind,
        "causality_issues": causality,
        "decision_close_unix": bridge.decision_close_unix,
        "wall_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        "peak_rss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    }
    row["verdict"] = (
        "PASS"
        if parity["ok"]
        and row["deterministic_repeat"]
        and adapted.members_hash == bridge.members_hash
        and not causality
        else "FAIL"
    )
    return row


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def main() -> int:
    _OUT.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(_LOCK), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("phase6c dry-run already running (lock held)", file=sys.stderr)
        return 2

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    reader = PublishedActiveReader()
    started = datetime.now(timezone.utc).isoformat()

    architecture = {
        "phase": "6C",
        "ssot_chain": [
            "PublishedActiveReader + CH catalog",
            "historical_pool_state_v1.load_historical_pool_state_at_close(include_remaining=False)",
            "historical_lmv2_backtester_adapter_v1.load_backtester_active_state_at_close",
            "historical_lmv2_backtester_input_bridge_v1.load_historical_lmv2_backtester_input_at_close",
        ],
        "engine_backtester_contrast": describe_engine_backtester_pool_contrast(),
        "dry_run_invokes_run_backtest": False,
        "dry_run_invokes_trade_engine": False,
        "actual_backtester_consumer_wired": False,
        "notes": [
            "ACTUAL_BACKTESTER_INPUT_INTEGRATION requires wiring into "
            "orderbook_analyse backtest._pool_snapshot_at_end; not done in 6C.",
        ],
    }
    _write_json(_OUT / "architecture.json", architecture)

    input_contract = {
        "bridge_function": "load_historical_lmv2_backtester_input_at_close",
        "parameters": {
            "decision_close": "15m bar close UTC (str or datetime)",
            "symbol": "default DOGEUSDT from pd contract",
            "timeframe": "default 15m",
            "pool_detail": "1 | 5 | 10",
        },
        "output_type": "HistoricalLmv2BacktesterInputStateV1",
        "binding_fields": [
            "snapshot_id",
            "pipeline_line_id",
            "config_hash",
            "decision_close",
            "decision_close_unix",
            "active_members[].pool_id",
            "active_members[].side",
            "active_members[].lower_bound",
            "active_members[].upper_bound",
            "active_members[].strength",
            "active_members[].known_at",
            "active_members[].active_membership",
            "members_hash",
            "input_state_hash",
        ],
        "availability_class": "RESEARCH_HISTORICAL",
        "engine_pool_record_mapping": "NOT_CLAIMED",
    }
    _write_json(_OUT / "input_contract.json", input_contract)

    ref_rows = []
    for pd in (1, 5, 10):
        row = _dry_run_row(client, cfg.database, reader, pd, REFERENCE_CLOSE)
        ref_rows.append(row)
        _write_json(_OUT / f"dry_run_parity_pd{pd}_reference.json", row)

    all_rows: list[dict[str, Any]] = []
    for close_iso in MULTI_CLOSES:
        for pd in (1, 5, 10):
            all_rows.append(_dry_run_row(client, cfg.database, reader, pd, close_iso))

    regression = {
        "started_at": started,
        "reference_close": REFERENCE_CLOSE,
        "closes": MULTI_CLOSES,
        "reference_rows": ref_rows,
        "rows": all_rows,
    }
    _write_json(_OUT / "regression_results.json", regression)
    _write_json(
        _OUT / "dry_run_parity.json",
        {
            "reference_close": REFERENCE_CLOSE,
            "reference_rows": ref_rows,
            "multi_close_row_count": len(all_rows),
            "all_pass": all(r["verdict"] == "PASS" for r in all_rows),
        },
    )

    pd_verdicts = {f"PD{pd}_INPUT_PARITY": "PASS" for pd in (1, 5, 10)}
    for pd in (1, 5, 10):
        ref = next(r for r in ref_rows if r["pool_detail"] == pd)
        if ref["verdict"] != "PASS":
            pd_verdicts[f"PD{pd}_INPUT_PARITY"] = "FAIL"

    verdicts = {
        "PHASE_6C_BRIDGE": "PASS" if all(r["verdict"] == "PASS" for r in all_rows) else "FAIL",
        **pd_verdicts,
        "CAUSALITY": "PASS" if not any(r["causality_issues"] for r in all_rows) else "FAIL",
        "SNAPSHOT_IDENTITY": "PASS"
        if all(
            r["snapshot_id"] and r["members_hash_adapter"] == r["members_hash_bridge"]
            for r in all_rows
        )
        else "FAIL",
        "INPUT_DETERMINISM": "PASS"
        if all(r["deterministic_repeat"] for r in all_rows)
        else "FAIL",
        "ACTUAL_BACKTESTER_INPUT_INTEGRATION": "NOT_IMPLEMENTED",
        "BACKTESTER_E2E": "NOT_IMPLEMENTED",
    }
    core_ok = (
        verdicts["PHASE_6C_BRIDGE"] == "PASS"
        and verdicts["CAUSALITY"] == "PASS"
        and verdicts["SNAPSHOT_IDENTITY"] == "PASS"
        and verdicts["INPUT_DETERMINISM"] == "PASS"
        and all(pd_verdicts[f"PD{pd}_INPUT_PARITY"] == "PASS" for pd in (1, 5, 10))
    )
    verdicts["PHASE_6C_OVERALL"] = "PASS" if core_ok else "FAIL"

    report_lines = [
        "# Phase 6C — Historical LMV2 Backtester Input Bridge",
        "",
        f"- Started: {started}",
        f"- Reference close: {REFERENCE_CLOSE}",
        f"- Rows: {len(all_rows)} (5 closes × PD1/5/10)",
        "",
        "## Verdicts",
        "",
    ]
    for k, v in verdicts.items():
        report_lines.append(f"- **{k}**: {v}")
    report_lines.extend(
        [
            "",
            "## Reference close (PD1/5/10)",
            "",
        ]
    )
    for r in ref_rows:
        report_lines.append(
            f"- PD{r['pool_detail']}: count={r['active_member_count']} "
            f"(control {r['expected_active_control']}) hash={r['members_hash_bridge'][:16]}… "
            f"verdict={r['verdict']}"
        )
    report_lines.extend(
        [
            "",
            "## Integration boundary",
            "",
            "Engine backtester still uses `load_engine_pools_at` / `PoolRecord`. "
            "This phase delivers an isolated LMV2 input plane only; no `run_backtest` call.",
            "",
            "Availability is marked **RESEARCH_HISTORICAL** (publication replay at decision close).",
        ]
    )
    (_OUT / "report.md").write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    _write_json(_OUT / "verdicts.json", verdicts)

    client.close()
    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    os.close(lock_fd)
    print(json.dumps(verdicts, indent=2))
    return 0 if verdicts["PHASE_6C_OVERALL"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
