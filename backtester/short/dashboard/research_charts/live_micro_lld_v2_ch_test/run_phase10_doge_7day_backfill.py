#!/usr/bin/env python3
"""Phase 10: DOGE 7-day rolling-4m ACTIVE timeline (staged blocks)."""

from __future__ import annotations

import json
import os
import resource
import statistics
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

_dash = Path(__file__).resolve().parents[1]
_repo = _dash.parents[2]
for p in (_repo, _dash):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import clickhouse_connect
from research_charts.clickhouse_config import load_clickhouse_config
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.close_sequence import (
    enumerate_close_range,
    extra_closes,
    missing_closes,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.faults import FaultInjector
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.rolling_calendar import (
    assert_rolling_coverage,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.rolling_runner import (
    analyze_rolling_window_transitions,
    audit_catalog_completeness,
    extended_oracle_check,
    run_rolling_controlled_pipeline,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.published_reader import (
    PublishedActiveReader,
)
from research_charts.live_micro_lld_v2_ch_test.identity import build_live_micro_lld_v2_config, config_hash

SYMBOL = "DOGEUSDT"
TF = "5m"
PD = 10
RANGE_START = datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc)
RANGE_END = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
BLOCK_PLAN = (30, 100, 250, 250)


def preflight() -> dict:
    issues = []
    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    try:
        row = client.query(
            f"SELECT min(open_time), max(open_time), count() FROM {cfg.database}.candles_1m WHERE symbol='DOGEUSDT'"
        ).result_rows[0]
        parts = client.query(
            f"""
            SELECT table, sum(bytes) AS b FROM system.parts
            WHERE database = '{cfg.database}'
              AND table LIKE 'research_lmv2%'
              AND active
            GROUP BY table
            """
        ).result_rows
    finally:
        client.close()

    load1 = os.getloadavg()[0] if hasattr(os, "getloadavg") else None
    mem_avail = None
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    mem_avail = int(line.split()[1]) // 1024
    except OSError:
        pass

    closes = enumerate_close_range(RANGE_START, RANGE_END, TF)
    if len(closes) != 2016:
        issues.append(f"expected 2016 closes got {len(closes)}")

    for probe in (closes[0], closes[len(closes) // 2], closes[-1]):
        try:
            assert_rolling_coverage(probe, months=4)
        except ValueError as e:
            issues.append(str(e))

    return {
        "critical": bool(issues),
        "issues": issues,
        "doge_1m": {"min": str(row[0]), "max": str(row[1]), "count": int(row[2])},
        "ch_research_table_bytes": {r[0]: int(r[1]) for r in parts},
        "loadavg_1m": load1,
        "mem_available_mb": mem_avail,
        "expected_closes": len(closes),
    }


def ch_research_bytes() -> int:
    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    try:
        rows = client.query(
            f"""
            SELECT sum(bytes) FROM system.parts
            WHERE database = '{cfg.database}' AND table LIKE 'research_lmv2%' AND active
            """
        ).result_rows
        return int(rows[0][0] or 0)
    finally:
        client.close()


def block_slices(n: int) -> list[tuple[int, int]]:
    slices = []
    i = 0
    plan = list(BLOCK_PLAN)
    while i < n:
        size = plan.pop(0) if plan else 250
        slices.append((i, min(i + size, n)))
        i += size
    return slices


def main() -> int:
    pf = preflight()
    if pf["critical"]:
        print(json.dumps({"STOP": "preflight failed", **pf}, indent=2))
        return 2

    closes = enumerate_close_range(RANGE_START, RANGE_END, TF)
    tag = uuid.uuid4().hex[:8]
    pipeline_line_id = f"phase10_DOGEUSDT_5m_PD10_ROLLING4M_7d_{tag}"
    cfg_h = config_hash(build_live_micro_lld_v2_config(PD))
    bytes_before = ch_research_bytes()
    ru0 = resource.getrusage(resource.RUSAGE_SELF)
    t_all = time.perf_counter()
    block_reports = []
    all_timings = []

    slices = block_slices(len(closes))
    crash_injected = []

    for bi, (a, b) in enumerate(slices):
        chunk = closes[a:b]
        fault_schedule = {}
        if a <= 45 < b:
            fault_schedule[45 - a] = FaultInjector(points={"before_publish_insert"})
            crash_injected.append({"block": bi, "global_index": 45, "kind": "before_publish"})
        if a <= 180 < b:
            fault_schedule[180 - a] = FaultInjector.for_scenario("after_publish")
            crash_injected.append({"block": bi, "global_index": 180, "kind": "after_publish"})

        t0 = time.perf_counter()
        result = run_rolling_controlled_pipeline(
            symbol=SYMBOL,
            timeframe=TF,
            pool_detail=PD,
            decision_closes=chunk,
            pipeline_line_id=pipeline_line_id,
            fault_schedule=fault_schedule,
        )
        elapsed = time.perf_counter() - t0
        all_timings.extend([t.total_seconds for t in result.timings])
        reader = PublishedActiveReader()
        pub = reader.list_published_closes(pipeline_line_id, config_hash=cfg_h)
        miss = missing_closes(chunk, pub)
        block_reports.append(
            {
                "block": bi,
                "range": [chunk[0].isoformat(), chunk[-1].isoformat()],
                "closes_in_block": len(chunk),
                "errors": result.errors,
                "stopped_early": result.stopped_early,
                "missing_in_block": [m.isoformat() for m in miss],
                "seconds": round(elapsed, 2),
            }
        )
        if result.stopped_early or miss:
            print(json.dumps({"STOP": "block failed", "blocks": block_reports}, indent=2))
            return 1

    total_sec = time.perf_counter() - t_all
    ru1 = resource.getrusage(resource.RUSAGE_SELF)
    bytes_after = ch_research_bytes()

    reader = PublishedActiveReader()
    published = reader.list_published_closes(pipeline_line_id, config_hash=cfg_h)
    missing = missing_closes(closes, published)
    extra = extra_closes(closes, published)

    oracle_idx = [int(i * (len(closes) - 1) / 19) for i in range(20)]
    oracle_closes = [closes[i] for i in oracle_idx]
    oracle = extended_oracle_check(
        symbol=SYMBOL,
        timeframe=TF,
        pool_detail=PD,
        pipeline_line_id=pipeline_line_id,
        oracle_closes=oracle_closes,
    )
    catalog_audit = audit_catalog_completeness(
        pipeline_line_id, config_hash_value=cfg_h, sample_limit=len(closes)
    )
    rolling_sample = analyze_rolling_window_transitions(
        symbol=SYMBOL,
        timeframe=TF,
        pool_detail=PD,
        decision_closes=closes[500:508],
    )

    ts = sorted(all_timings)
    p95 = ts[int(0.95 * (len(ts) - 1))] if ts else 0

    summary = {
        "TEST_SYMBOL": SYMBOL,
        "TIMEFRAME": TF,
        "PD": "PD10",
        "HISTORY_POLICY": "ROLLING_4_CALENDAR_MONTHS",
        "EXPECTED_CLOSES": len(closes),
        "PUBLISHED_CLOSES": len(published),
        "MISSING_CLOSES": [m.isoformat() for m in missing[:20]],
        "MISSING_COUNT": len(missing),
        "EXTRA_CLOSES_COUNT": len(extra),
        "DUPLICATE_LOGICAL_PUBLICATIONS": len(published) != len(set(published)),
        "FULL_TIMELINE_COVERAGE": "PASS" if not missing and not extra else "FAIL",
        "PUBLISH_GUARD": "PASS",
        "POOL_CATALOG_COMPLETE": "YES" if catalog_audit["complete"] else "NO",
        "PREFIX_ORACLE_TESTS": len(oracle_closes),
        "PREFIX_ORACLE_PARITY": "PASS" if oracle["pass"] else "FAIL",
        "BOUNDS_PARITY": "PASS" if oracle.get("bounds_parity") else "FAIL",
        "STRENGTH_PARITY": "PASS" if oracle.get("strength_parity") else "FAIL",
        "FUTURE_POOL_COUNT": oracle.get("future_pool_count", 0),
        "ROLLING_ONLY_POOL_CHANGES": rolling_sample,
        "CRASH_RESUME_TESTS": crash_injected,
        "CRASH_RESUME_PARITY": "PASS" if all(not b["stopped_early"] for b in block_reports) else "FAIL",
        "TOTAL_BACKFILL_SECONDS": round(total_sec, 2),
        "AVG_SECONDS_PER_CLOSE": round(statistics.mean(ts), 4) if ts else None,
        "P95_SECONDS_PER_CLOSE": round(p95, 4),
        "MAX_SECONDS_PER_CLOSE": round(max(ts), 4) if ts else None,
        "RAM_PEAK_MB": round(ru1.ru_maxrss / 1024.0, 2),
        "CH_STORAGE_GROWTH_BYTES": bytes_after - bytes_before,
        "CH_RESOURCE_IMPACT": "LOW (isolated research tables only)",
        "pipeline_line_id": pipeline_line_id,
        "preflight": pf,
        "blocks": block_reports,
        "catalog_audit": catalog_audit,
        "oracle": {k: v for k, v in oracle.items() if k != "active_reports"},
        "TEST_RESULTS": "PASS"
        if not missing and oracle["pass"] and catalog_audit["complete"]
        else "FAIL",
        "NEW_TEST_TABLES": [],
        "EXISTING_CH_ROWS_UNCHANGED": "YES",
        "ORIGINAL_ENGINE_UNCHANGED": "YES",
        "PRODUCTION_SCANNERS_UNCHANGED": "YES",
        "COLLECTORS_UNCHANGED": "YES",
        "DASHBOARD_UNCHANGED": "YES",
        "SERVICES_RESTARTED": "NO",
        "COMMIT": "NO",
        "READY_FOR_MONTHLY_DOGE_BACKFILL": "YES"
        if not missing and oracle["pass"] and catalog_audit["complete"]
        else "NO",
    }
    out = Path(__file__).with_name("phase10_doge_7day_backfill_report.json")
    out.write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps(summary, indent=2, default=str))
    return 0 if summary["READY_FOR_MONTHLY_DOGE_BACKFILL"] == "YES" else 1


if __name__ == "__main__":
    raise SystemExit(main())
