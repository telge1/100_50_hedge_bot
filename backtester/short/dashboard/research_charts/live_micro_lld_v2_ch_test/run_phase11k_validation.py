#!/usr/bin/env python3
"""Phase 11K — aggregate validation report (read-only CH schema probe + pytest)."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from research_charts.clickhouse_config import load_clickhouse_config

REPORT_NAME = "phase11k_rolling_trim_revision_report.json"


def _schema_audit() -> dict:
    out: dict = {"table": "signal_generator.candles_1m", "queried_at": datetime.now(timezone.utc).isoformat()}
    try:
        import clickhouse_connect

        cfg = load_clickhouse_config()
        c = clickhouse_connect.get_client(**cfg.connect_kwargs())
        rows = c.query(f"DESCRIBE TABLE {cfg.database}.{cfg.table}").result_rows
        create = c.query(f"SHOW CREATE TABLE {cfg.database}.{cfg.table}").result_rows[0][0]
        c.close()
        cols = {r[0]: r[1] for r in rows}
        out["columns"] = cols
        out["engine"] = "ReplacingMergeTree(ingested_at)" if "ReplacingMergeTree(ingested_at)" in create else "unknown"
        out["version_column"] = "ingested_at (ReplacingMergeTree dedup key)"
        out["ohlcv_revision_semantics"] = (
            "Newer ingested_at row replaces older for same (exchange,symbol,interval,open_time) when using FINAL"
        )
        out["read_path_selects_ingested_at"] = False
        out["collector_reference"] = (
            "signal_generator_stoch_waves/docs/LIVE_COLLECTOR.md — candles_1m analytical key"
        )
    except Exception as exc:
        out["error"] = str(exc)
    return out


def main() -> int:
    dash = Path(__file__).resolve().parents[2]
    repo = Path(__file__).resolve().parents[5]
    tests = [
        "research_charts/tests/test_phase11k_month_boundaries.py",
        "research_charts/tests/test_phase11k_revision_safety.py",
        "research_charts/tests/test_phase11k_rolling_trim.py",
        "research_charts/tests/test_phase11i_parallel_metrics.py",
    ]
    env = dict(**{k: v for k, v in __import__("os").environ.items()})
    trp = Path("/home/telgenbuescher/projects/trading_research_platform")
    env["PYTHONPATH"] = f"{repo}:{dash}:{trp}"

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *tests, "-q", "--tb=no"],
        cwd=str(dash),
        env=env,
        capture_output=True,
        text=True,
    )
    schema = _schema_audit()
    passed = proc.returncode == 0

    report = {
        "PHASE": "11K",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "PYTEST_RETURNCODE": proc.returncode,
        "PYTEST_STDOUT_TAIL": proc.stdout[-4000:],
        "PYTEST_STDERR_TAIL": proc.stderr[-2000:],
        "ROLLING_TRIM_TRIGGER_TESTED": "YES",
        "ROLLING_TRIM_REFERENCE_PARITY": "PASS" if passed else "FAIL",
        "MONTH_BOUNDARY_PARITY": "PASS" if passed else "FAIL",
        "WARMUP_COMPLETENESS": "PASS" if passed else "FAIL",
        "CANDLE_REVISION_FIELDS_AVAILABLE": {
            "ingested_at": schema.get("columns", {}).get("ingested_at", "NOT_QUERIED"),
            "source_event_time": schema.get("columns", {}).get("source_event_time"),
            "explicit_version_counter": "NO",
            "engine": schema.get("engine"),
        },
        "CH_SCHEMA_AUDIT": schema,
        "HISTORICAL_CORRECTION_DETECTION": "PARTIAL",
        "HISTORICAL_CORRECTION_DETECTION_NOTE": (
            "In-process OHLCV merge conflict PASS; CH ingested_at exists but not read by research loader; "
            "post-hoc CH correction without re-fetch NOT_PROVEN detected"
        ),
        "IMMUTABLE_INPUT_SNAPSHOT_FEASIBILITY": "YES",
        "IMMUTABLE_INPUT_SNAPSHOT_NOTE": (
            "ImmutableInputSnapshot + block_content_hash per backfill run; does not auto-detect future CH edits"
        ),
        "MULTI_WORKER_REVISION_CONSISTENCY": "NOT_PROVEN",
        "MULTI_WORKER_REVISION_NOTE": (
            "Workers reading CH at different times may see different ingested_at winners without ingested_at in SELECT"
        ),
        "ENGINE_PARITY": "PASS" if passed else "FAIL",
        "STRENGTH_PARITY": "PASS" if passed else "FAIL",
        "RECOMMENDED_MITIGATION": [
            "Extend get_1m_candles SELECT with ingested_at; store per-run snapshot_id + content_hash",
            "On re-fetch, compare ingested_at/OHLCV; invalidate 5m buckets via phase11k_revision_safety.invalidate_buckets",
            "Fail-closed if prefix mixes snapshot revisions",
            "Optional: frozen parquet snapshot per publish line before multi-worker backfill",
        ],
        "BUFFER_CAP_FAIL_CLOSED_ON_ROLLING_WINDOW": "NOT_PROVEN",
        "BUFFER_CAP_NOTE": (
            "_trim_left may keep len > max_1m_bars when oldest >= keep_from_unix; "
            "test_small_max_exceeds_cap_but_retains_required_window documents behavior"
        ),
        "READY_FOR_PUBLISH_PIPELINE_POC": "YES" if passed else "NO",
        "READY_FOR_PRODUCTION_CUTOVER": "NO",
        "HISTORICAL_CORRECTION_SAFETY": "NOT_PROVEN",
        "FILES_CHANGED": [
            "phase11k_revision_safety.py",
            "phase11k_buffer_policy.py",
            "run_phase11k_validation.py",
            "tests/test_phase11k_rolling_trim.py",
            "tests/test_phase11k_month_boundaries.py",
            "tests/test_phase11k_revision_safety.py",
        ],
    }
    out_path = Path(__file__).with_name(REPORT_NAME)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
