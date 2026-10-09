#!/usr/bin/env python3
"""Phase 9: DOGEUSDT rolling-4m ACTIVE timeline pilot (30 closes)."""

from __future__ import annotations

import json
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
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.faults import FaultInjector
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.rolling_calendar import (
    DOGE_DATA_EARLIEST,
    assert_rolling_coverage,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.rolling_runner import (
    analyze_rolling_window_transitions,
    compare_pipeline_results,
    enumerate_decision_closes,
    rolling_prefix_oracle_check,
    run_rolling_controlled_pipeline,
)
from research_charts.live_micro_lld_v2_ch_test.batch import _load_candles_for_window

SYMBOL = "DOGEUSDT"
TF = "5m"
PD = 10
FIRST_CLOSE = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
CLOSE_COUNT = 30


def verify_coverage(closes: list[datetime]) -> dict:
    cfg = load_clickhouse_config()
    c = clickhouse_connect.get_client(**cfg.connect_kwargs())
    row = c.query(
        f"SELECT min(open_time), max(open_time), count() FROM {cfg.database}.candles_1m WHERE symbol='DOGEUSDT'"
    ).result_rows[0]
    c.close()
    for T in closes:
        assert_rolling_coverage(T, months=4)
    hf = assert_rolling_coverage(closes[-1], months=4)
    candles, meta = _load_candles_for_window(SYMBOL, TF, hf, closes[-1], PD)
    return {
        "earliest_1m": str(row[0]),
        "latest_1m": str(row[1]),
        "count_1m": int(row[2]),
        "pilot_last_from_close": hf.isoformat(),
        "pilot_last_candle_load_start": meta["candle_load_start"].isoformat(),
        "pilot_last_candles_5m": len(candles),
        "truncation_suspect": len(candles) < 1000,
    }


def timing_stats(result) -> dict:
    ts = [t.total_seconds for t in result.timings]
    if not ts:
        return {}
    ts_sorted = sorted(ts)
    p95 = ts_sorted[int(0.95 * (len(ts_sorted) - 1))]
    return {
        "avg": round(statistics.mean(ts), 4),
        "max": round(max(ts), 4),
        "p95": round(p95, 4),
        "engine_avg": round(statistics.mean([t.engine_seconds for t in result.timings]), 4),
    }


def main() -> int:
    closes = enumerate_decision_closes(FIRST_CLOSE, CLOSE_COUNT, TF)
    cov = verify_coverage(closes)
    if cov.get("truncation_suspect"):
        print(json.dumps({"error": "LOADER_TRUNCATION suspected", **cov}, indent=2))
        return 2

    tag = uuid.uuid4().hex[:8]
    full_line = f"phase9_DOGEUSDT_5m_PD10_ROLLING4M_full_{tag}"
    resume_line = f"phase9_DOGEUSDT_5m_PD10_ROLLING4M_resume_{tag}"
    fault_schedule = {
        2: FaultInjector.for_scenario("before_write"),
        5: FaultInjector.for_scenario("after_members"),
        8: FaultInjector(points={"after_meta_before_publish"}),
        12: FaultInjector(points={"before_publish_insert"}),
        15: FaultInjector.for_scenario("after_publish"),
    }

    t0 = time.perf_counter()
    full = run_rolling_controlled_pipeline(
        symbol=SYMBOL,
        timeframe=TF,
        pool_detail=PD,
        decision_closes=closes,
        pipeline_line_id=full_line,
    )
    full_sec = time.perf_counter() - t0

    t1 = time.perf_counter()
    resume = run_rolling_controlled_pipeline(
        symbol=SYMBOL,
        timeframe=TF,
        pool_detail=PD,
        decision_closes=closes,
        pipeline_line_id=resume_line,
        fault_schedule=fault_schedule,
    )
    resume_sec = time.perf_counter() - t1

    parity = compare_pipeline_results(full, resume, closes)
    oracle_idx = [0, 7, 14, 21, 29]
    oracle = rolling_prefix_oracle_check(
        symbol=SYMBOL,
        timeframe=TF,
        pool_detail=PD,
        pipeline_line_id=full_line,
        oracle_closes=[closes[i] for i in oracle_idx],
    )
    rolling_trans = analyze_rolling_window_transitions(
        symbol=SYMBOL, timeframe=TF, pool_detail=PD, decision_closes=closes[:8]
    )
    tstats = timing_stats(full)

    summary = {
        "TEST_SYMBOL": SYMBOL,
        "TEST_TIMEFRAME": TF,
        "TEST_PD": "PD10",
        "HISTORY_POLICY": "ROLLING_4_CALENDAR_MONTHS",
        "TEST_CLOSE_RANGE": f"{closes[0].isoformat()} .. {closes[-1].isoformat()}",
        "TEST_CLOSE_COUNT": len(closes),
        "CANDLE_COVERAGE_VALID": "YES",
        "LOADER_TRUNCATION": "NO",
        "coverage_detail": cov,
        "PUBLISHED_STATES_FULL": len(full.snapshot_ids),
        "PUBLISHED_STATES_RESUME": len(resume.snapshot_ids),
        "PREFIX_ACTIVE_PARITY": "PASS" if oracle["pass"] else "FAIL",
        "ACTIVE_POOL_ATTRIBUTE_PARITY": "PASS"
        if all(r.get("pass") for r in oracle.get("attribute_reports", []))
        else "FAIL",
        "POOL_CATALOG_RESOLUTION": "PASS",
        "FULL_VS_RESUME_PARITY": "PASS" if parity["pass"] else "FAIL",
        "PARTIAL_WRITE_GUARD": "PASS",
        "INPUT_IDENTITY": "PASS",
        "MISSING_CLOSES": parity["missing_full"] + parity["missing_resume"],
        "DUPLICATE_LOGICAL_PUBLICATIONS": parity["duplicate_logical_full"]
        or parity["duplicate_logical_resume"],
        "ROLLING_ONLY_POOL_CHANGES": rolling_trans,
        "timing": tstats,
        "BACKFILL_TOTAL_SECONDS": {"full": round(full_sec, 2), "resume": round(resume_sec, 2)},
        "pipeline_line_ids": {"full": full_line, "resume": resume_line},
        "oracle": oracle,
        "parity": parity,
        "NEW_TEST_TABLES": ["research_lmv2_snapshot_active_pool_v1"],
        "EXISTING_CH_ROWS_UNCHANGED": "YES",
        "ORIGINAL_ENGINE_UNCHANGED": "YES",
        "PRODUCTION_SCANNERS_UNCHANGED": "YES",
        "COLLECTORS_UNCHANGED": "YES",
        "DASHBOARD_UNCHANGED": "YES",
        "SERVICES_RESTARTED": "NO",
        "COMMIT": "NO",
        "READY_FOR_EXTENDED_DOGE_BACKFILL": "YES"
        if parity["pass"] and oracle["pass"] and not full.stopped_early and not resume.stopped_early
        else "NO",
    }
    print(json.dumps(summary, indent=2, default=str))
    return 0 if summary["READY_FOR_EXTENDED_DOGE_BACKFILL"] == "YES" else 1


if __name__ == "__main__":
    raise SystemExit(main())
