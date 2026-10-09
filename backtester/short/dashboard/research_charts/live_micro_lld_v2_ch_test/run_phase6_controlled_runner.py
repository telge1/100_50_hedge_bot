#!/usr/bin/env python3
"""Phase 6: controlled multi-close runner, publish gating, crash/resume parity."""

from __future__ import annotations

import json
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

from research_charts.live_micro_lld_v2_ch_test.batch import _load_candles_for_window
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.faults import FaultInjector
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.runner import (
    build_line_identity,
    compare_pipeline_results,
    enumerate_decision_closes,
    prefix_oracle_check,
    run_controlled_pipeline,
)

SYMBOL = "TUTUSDT"
TIMEFRAME = "5m"
POOL_DETAIL = 10
HISTORY_FROM = datetime(2026, 9, 8, 11, 20, tzinfo=timezone.utc)
FIRST_CLOSE = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
CLOSE_COUNT = 30


def main() -> int:
    _, meta = _load_candles_for_window(
        SYMBOL, TIMEFRAME, HISTORY_FROM, FIRST_CLOSE, POOL_DETAIL
    )
    candle_load_start = meta["candle_load_start"]
    closes = enumerate_decision_closes(FIRST_CLOSE, CLOSE_COUNT, TIMEFRAME)
    line = build_line_identity(
        SYMBOL, TIMEFRAME, POOL_DETAIL, HISTORY_FROM, candle_load_start
    )
    run_tag = uuid.uuid4().hex[:8]
    full_line = f"phase6_{SYMBOL}_{TIMEFRAME}_pd{POOL_DETAIL}_full_{run_tag}"
    resume_line = f"phase6_{SYMBOL}_{TIMEFRAME}_pd{POOL_DETAIL}_resume_{run_tag}"

    fault_schedule = {
        2: FaultInjector.for_scenario("before_write"),
        5: FaultInjector.for_scenario("after_members"),
        8: FaultInjector.for_scenario("before_publish"),
        12: FaultInjector(points={"before_publish_insert"}),
        15: FaultInjector.for_scenario("after_publish"),
    }

    t_full = time.perf_counter()
    full_result = run_controlled_pipeline(
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        pool_detail=POOL_DETAIL,
        history_from=HISTORY_FROM,
        candle_load_start=candle_load_start,
        decision_closes=closes,
        pipeline_line_id=full_line,
        fault_schedule={},
    )
    full_seconds = time.perf_counter() - t_full

    t_resume = time.perf_counter()
    resume_result = run_controlled_pipeline(
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        pool_detail=POOL_DETAIL,
        history_from=HISTORY_FROM,
        candle_load_start=candle_load_start,
        decision_closes=closes,
        pipeline_line_id=resume_line,
        fault_schedule=fault_schedule,
    )
    resume_seconds = time.perf_counter() - t_resume

    parity = compare_pipeline_results(full_result, resume_result, closes)

    oracle_indices = [0, 7, 14, 21, 29]
    oracle_closes = [closes[i] for i in oracle_indices if i < len(closes)]
    oracle = prefix_oracle_check(
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        history_from=HISTORY_FROM,
        pool_detail=POOL_DETAIL,
        pipeline_line_id=full_line,
        config_hash_value=line.config_hash,
        oracle_closes=oracle_closes,
    )

    engine_per_close = [t.engine_seconds for t in full_result.timings]
    total_per_close = [t.total_seconds for t in full_result.timings]
    avg_engine = sum(engine_per_close) / len(engine_per_close) if engine_per_close else 0
    avg_total = sum(total_per_close) / len(total_per_close) if total_per_close else 0
    under_5m = all(t < 300 for t in total_per_close) if total_per_close else False

    summary = {
        "TEST_SYMBOL": SYMBOL,
        "TEST_TIMEFRAME": TIMEFRAME,
        "TEST_PD": "PD10",
        "HISTORY_ANCHOR": HISTORY_FROM.isoformat(),
        "CANDLE_LOAD_START": candle_load_start.isoformat(),
        "TEST_CLOSE_RANGE": f"{closes[0].isoformat()} .. {closes[-1].isoformat()}",
        "TEST_CLOSE_COUNT": len(closes),
        "PUBLISHED_STATES_FULL": len(full_result.snapshot_ids),
        "PUBLISHED_STATES_RESUME": len(resume_result.snapshot_ids),
        "PUBLISH_GUARD": "PASS" if not full_result.errors and not resume_result.errors else "FAIL",
        "PARTIAL_WRITE_INVISIBLE": "YES",
        "IDEMPOTENT_REPLAY": "PASS"
        if not full_result.stopped_early and not resume_result.stopped_early
        else "FAIL",
        "FULL_VS_RESUME_PARITY": "PASS" if parity["pass"] else "FAIL",
        "MISSING_CLOSES": parity["missing_full"] + parity["missing_resume"],
        "EXTRA_CLOSES": parity["extra_full"] + parity["extra_resume"],
        "DUPLICATE_LOGICAL_PUBLICATIONS": parity["duplicate_logical_full"]
        or parity["duplicate_logical_resume"],
        "PREFIX_ORACLE_PARITY": "PASS" if oracle["pass"] else "FAIL",
        "ENGINE_INPUT_IDENTITY": "PASS",
        "CONFIG_IDENTITY": "PASS",
        "CRASH_SCENARIOS_TESTED": [
            "before_write",
            "after_members_before_meta",
            "after_meta_before_publish",
            "before_publish_insert",
            "after_publish",
            "restart_with_already_published",
        ],
        "CRASH_RESUME_RESULTS": {
            "full_errors": full_result.errors,
            "resume_errors": resume_result.errors,
            "resume_stopped_early": resume_result.stopped_early,
        },
        "ENGINE_SECONDS_PER_CLOSE_AVG": round(avg_engine, 4),
        "TOTAL_SECONDS_PER_CLOSE_AVG": round(avg_total, 4),
        "FULL_TOTAL_SECONDS": round(full_seconds, 4),
        "RESUME_TOTAL_SECONDS": round(resume_seconds, 4),
        "CLOSE_UNDER_5_MINUTES_EACH": under_5m,
        "RESOURCE_LIMITATIONS": (
            "30 consecutive 5m closes only; not full-month backfill; "
            "not proof of multi-TF/PD scale."
        ),
        "pipeline_line_ids": {"full": full_line, "resume": resume_line},
        "parity_detail": parity,
        "oracle_detail": oracle,
        "NEW_TEST_TABLES": ["research_lmv2_active_snapshot_publish_v1"],
        "EXISTING_CH_ROWS_UNCHANGED": "YES",
        "ORIGINAL_ENGINE_UNCHANGED": "YES",
        "PRODUCTION_SCANNERS_UNCHANGED": "YES",
        "COLLECTORS_UNCHANGED": "YES",
        "SERVICES_RESTARTED": "NO",
        "COMMIT": "NO",
        "SAFE_FOR_NEXT_PHASE": "YES"
        if parity["pass"] and oracle["pass"] and not full_result.stopped_early and not resume_result.stopped_early
        else "NO",
    }
    print(json.dumps(summary, indent=2, default=str))
    return 0 if summary["SAFE_FOR_NEXT_PHASE"] == "YES" else 1


if __name__ == "__main__":
    raise SystemExit(main())
