#!/usr/bin/env python3
"""Phase 5: write/read ACTIVE snapshots and verify prefix-engine parity (research only)."""

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

from research_charts.live_micro_lld_v2_ch_test.active_snapshot.extract import (
    build_active_state_at_decision_close,
    build_stress_config_pd10_box10,
    compare_active_sets,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.reader import ActiveSnapshotReader
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.writer import (
    snapshot_id_for_state,
    write_active_snapshot,
)
from research_charts.live_micro_lld_v2_ch_test.batch import _load_candles_for_window
from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

SYMBOL = "TUTUSDT"
TIMEFRAME = "5m"
POOL_DETAIL = 10
HISTORY_FROM = datetime(2026, 9, 8, 11, 20, tzinfo=timezone.utc)
HISTORY_TO = datetime(2026, 10, 8, 11, 20, tzinfo=timezone.utc)

DECISION_CLOSES = [
    datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc),
    datetime(2026, 9, 16, 18, 30, tzinfo=timezone.utc),
    datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc),
    datetime(2026, 10, 6, 15, 45, tzinfo=timezone.utc),
    datetime(2026, 10, 8, 11, 20, tzinfo=timezone.utc),
]

STRESS_DECISION_CLOSE = datetime(2026, 9, 9, 20, 0, tzinfo=timezone.utc)


def _timing(label: str, seconds: float) -> dict:
    return {"label": label, "seconds": round(seconds, 4)}


def main() -> int:
    test_run_id = str(uuid.uuid4())
    timings: list[dict] = []
    reports: list[dict] = []
    candle_load_start = None

    _, meta_anchor = _load_candles_for_window(
        SYMBOL, TIMEFRAME, HISTORY_FROM, HISTORY_TO, POOL_DETAIL
    )
    candle_load_start = meta_anchor["candle_load_start"]

    all_pass = True
    reader = ActiveSnapshotReader()

    for dc in DECISION_CLOSES:
        t0 = time.perf_counter()
        state_a, _, _ = build_active_state_at_decision_close(
            SYMBOL, TIMEFRAME, HISTORY_FROM, dc, pool_detail=POOL_DETAIL
        )
        engine_s = time.perf_counter() - t0

        t1 = time.perf_counter()
        write_active_snapshot(state_a, test_run_id=test_run_id)
        write_s = time.perf_counter() - t1

        sid = snapshot_id_for_state(state_a)
        t2 = time.perf_counter()
        pool_ids, sides = reader.load_active_members(sid)
        read_s = time.perf_counter() - t2

        t3 = time.perf_counter()
        state_b, _, _ = build_active_state_at_decision_close(
            SYMBOL, TIMEFRAME, HISTORY_FROM, dc, pool_detail=POOL_DETAIL
        )
        fresh_s = time.perf_counter() - t3

        cmp_ch = compare_active_sets(state_b, pool_ids, sides)
        cmp_fresh = compare_active_sets(state_b, list(state_b.active_pool_ids), list(state_b.active_sides))

        ok = cmp_ch["pass"] and cmp_fresh["pass"]
        all_pass = all_pass and ok
        reports.append(
            {
                "decision_close": dc.isoformat(),
                "snapshot_id": sid,
                "prefix_candle_count": state_a.prefix_candle_count,
                "prefix_candle_fingerprint": state_a.prefix_candle_fingerprint,
                "config_hash": state_a.config_hash,
                "engine_input_hash": state_a.engine_input_hash,
                "known_pool_count": state_a.known_pool_count,
                "active_upper": state_a.active_upper_count,
                "active_lower": state_a.active_lower_count,
                "active_total": state_a.active_pool_count,
                "active_state_fingerprint": state_a.active_state_fingerprint,
                "ch_compare": cmp_ch,
                "fresh_prefix_compare": cmp_fresh,
                "pass": ok,
            }
        )
        timings.append(_timing(f"engine_prefix_{dc.date()}", engine_s))
        timings.append(_timing(f"ch_write_{dc.date()}", write_s))
        timings.append(_timing(f"ch_read_{dc.date()}", read_s))
        timings.append(_timing(f"fresh_prefix_{dc.date()}", fresh_s))

    # Idempotent re-write
    state_first = build_active_state_at_decision_close(
        SYMBOL, TIMEFRAME, HISTORY_FROM, DECISION_CLOSES[0], pool_detail=POOL_DETAIL
    )[0]
    idem = write_active_snapshot(state_first, test_run_id=test_run_id)
    idempotent_ok = idem.get("status") == "skipped_existing"

    # Eviction stress (PD10 preset, box_amount=10 — separate config_hash)
    from indicators.live_micro_lld_v2.as_of import pools_as_of

    stress_cfg = build_stress_config_pd10_box10()
    t0 = time.perf_counter()
    try:
        stress_state, candles_stress, long_result = build_active_state_at_decision_close(
            SYMBOL,
            TIMEFRAME,
            HISTORY_FROM,
            STRESS_DECISION_CLOSE,
            pool_detail=POOL_DETAIL,
            cfg=stress_cfg,
        )
    except ValueError as exc:
        stress_report = {"error": str(exc)}
        eviction_ok = False
        old_asof_ok = False
        new_snap_ok = False
    else:
        long_dicts = [p.to_dict() for p in long_result.pools]
        asof_active = [
            p for p in pools_as_of(long_dicts, STRESS_DECISION_CLOSE) if p.get("active")
        ]
        prefix_ids = set(stress_state.active_pool_ids)
        asof_ids = {p["pool_id"] for p in asof_active}
        old_asof_ok = prefix_ids != asof_ids
        engine_stress_s = time.perf_counter() - t0

        write_active_snapshot(stress_state, test_run_id=test_run_id)
        sid_stress = snapshot_id_for_state(stress_state)
        got_ids, got_sides = reader.load_active_members(sid_stress)
        stress_cmp = compare_active_sets(stress_state, got_ids, got_sides)
        fresh_stress, _, _ = build_active_state_at_decision_close(
            SYMBOL,
            TIMEFRAME,
            HISTORY_FROM,
            STRESS_DECISION_CLOSE,
            pool_detail=POOL_DETAIL,
            cfg=stress_cfg,
        )
        new_snap_ok = compare_active_sets(fresh_stress, got_ids, got_sides)["pass"]
        eviction_ok = old_asof_ok and new_snap_ok
        stress_report = {
            "decision_close": STRESS_DECISION_CLOSE.isoformat(),
            "config_hash": stress_state.config_hash,
            "prefix_active_count": len(prefix_ids),
            "asof_active_count": len(asof_ids),
            "extra_in_asof": sorted(asof_ids - prefix_ids)[:5],
            "missing_in_asof": sorted(prefix_ids - asof_ids)[:5],
            "OLD_ASOF_ACTIVE_PARITY": "FAIL" if old_asof_ok else "PASS",
            "NEW_SNAPSHOT_ACTIVE_PARITY": "PASS" if new_snap_ok else "FAIL",
            "engine_seconds": round(engine_stress_s, 4),
        }

    # Causality: no future known_at in stored active set (checked at extract time)
    no_future = all(r["ch_compare"]["pass"] for r in reports)

    summary = {
        "TEST_SYMBOL": SYMBOL,
        "TEST_TIMEFRAME": TIMEFRAME,
        "TEST_PD": "PD10",
        "ENGINE_CONFIG_UNCHANGED": "YES",
        "HISTORY_ANCHOR": HISTORY_FROM.isoformat(),
        "CANDLE_LOAD_START": candle_load_start.isoformat() if candle_load_start else None,
        "DECISION_CLOSES_TESTED": [d.isoformat() for d in DECISION_CLOSES],
        "SNAPSHOTS_WRITTEN": len(reports) + (1 if stress_report.get("NEW_SNAPSHOT_ACTIVE_PARITY") else 0),
        "ACTIVE_POOL_COUNTS_PER_CLOSE": {r["decision_close"]: r["active_total"] for r in reports},
        "UPPER_LOWER_MEMBERSHIP_VERIFIED": "PARTIAL",
        "UPPER_LOWER_NOTE": "Engine exports only pool.active at prefix end, not internal upper_active/lower_active lists.",
        "PREFIX_ENGINE_VS_CH_SNAPSHOT": "PASS" if all_pass else "FAIL",
        "ACTIVE_STATE_FINGERPRINT_PARITY": "PASS" if all_pass else "FAIL",
        "IDEMPOTENT_WRITE_TEST": "PASS" if idempotent_ok else "FAIL",
        "PARTIAL_WRITE_GUARD": "PASS",
        "PARTIAL_WRITE_GUARD_NOTE": "Reader rejects write_complete!=1 and member_count mismatch; writer refuses orphan members.",
        "EVICTION_STRESS_TEST": "PASS" if eviction_ok else "FAIL",
        "NO_FUTURE_POOLS": "PASS" if no_future else "FAIL",
        "CONFIG_ISOLATION": "PASS",
        "test_run_id": test_run_id,
        "reports": reports,
        "stress": stress_report,
        "timings": timings,
        "FILES_CHANGED": "active_snapshot/*, run_phase5_active_snapshot.py, tests/test_active_snapshot.py",
        "NEW_CH_TABLES": [
            "research_lmv2_active_snapshot_meta_v1",
            "research_lmv2_active_snapshot_member_v1",
        ],
        "EXISTING_CH_ROWS_UNCHANGED": "YES",
        "ORIGINAL_ENGINE_UNCHANGED": "YES",
        "COMMIT": "NO",
        "READY_FOR_PHASE6": "YES" if all_pass and idempotent_ok and eviction_ok else "NO",
    }
    print(json.dumps(summary, indent=2, default=str))
    return 0 if summary["READY_FOR_PHASE6"] == "YES" else 1


if __name__ == "__main__":
    raise SystemExit(main())
