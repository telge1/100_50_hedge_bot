"""Regression tests for exact coverage accounting and segment ordering."""

from __future__ import annotations

import json
from pathlib import Path

from obfull_research_engine.clickhouse_research_store_v1.coverage_accounting_v2 import (
    merge_intervals,
    partition_coverage,
    reconcile_old_accounting,
    record_apply_order_key,
    record_identity_key,
    segment_temporal_order_key,
)


def _segment(*, start: int, archive: int, receive: int, sha: str, instance: str = "a") -> dict:
    return {
        "segment_start_ns": start,
        "first_archive_time_ns": archive,
        "first_receive_time_ns": receive,
        "segment_sha256": sha,
        "archive_instance_id": instance,
    }


def test_exact_integer_ns_coverage_partition():
    result = partition_coverage(
        physical=[(0, 100)],
        safe=[(0, 40), (60, 100)],
        blind=[(40, 50)],
        unresolved=[(55, 60)],
    )
    assert result["safe_union_ns"] == 80
    assert result["blind_union_ns"] == 10
    assert result["boundary_excluded_union_ns"] == 5
    assert result["unresolved_union_ns"] == 5
    assert result["equation_difference_ns"] == 0


def test_old_489_second_difference_is_fully_reconciled():
    run_dir = Path(__file__).parents[1] / "runs" / "clickhouse_research_store_pilot_v1"
    audit = json.loads((run_dir / "btc_coverage_accounting_v2.json").read_text())
    actual = audit["old_489_second_reconciliation"]
    result = reconcile_old_accounting(
        old_physical_hull_ns=actual["old_convex_hull_physical_ns"],
        old_safe_sum_ns=actual["old_raw_safe_sum_ns"],
        old_blind_sum_ns=actual["old_signed_blind_sum_ns"],
        physical_union_ns=audit["coverage_equation"]["physical_union_ns"],
        safe_union_ns=audit["coverage_equation"]["safe_union_ns"],
        blind_union_ns=audit["coverage_equation"]["blind_union_ns"],
        boundary_excluded_union_ns=audit["coverage_equation"]["boundary_excluded_union_ns"],
    )
    assert result["old_unexplained_residual_ns"] == actual["old_unexplained_residual_ns"]
    assert result["identity_check_ns"] == 0


def test_adjacent_half_open_intervals_merge_without_double_counting():
    assert merge_intervals([(0, 10), (10, 20)]) == [(0, 20)]


def test_overlapping_safe_intervals_are_unioned():
    result = partition_coverage(physical=[(0, 20)], safe=[(0, 15), (10, 20)], blind=[])
    assert result["safe_union_ns"] == 20
    assert result["equation_difference_ns"] == 0


def test_overlapping_blind_intervals_are_unioned_and_clipped():
    result = partition_coverage(
        physical=[(0, 30)], safe=[(0, 5)], blind=[(5, 20), (10, 25), (25, 40)]
    )
    assert result["blind_union_ns"] == 25
    assert result["boundary_excluded_union_ns"] == 0


def test_partial_first_hour_is_not_expanded_to_slot_start():
    result = partition_coverage(physical=[(39, 100)], safe=[(40, 100)], blind=[])
    assert result["physical_union_ns"] == 61
    assert result["boundary_excluded_union_ns"] == 1


def test_duplicate_utc_slot_uses_causal_time_not_sha():
    early = _segment(start=0, archive=10, receive=9, sha="f" * 64)
    late = _segment(start=0, archive=20, receive=19, sha="0" * 64)
    assert sorted([late, early], key=segment_temporal_order_key) == [early, late]
    assert sorted([late, early], key=lambda row: row["segment_sha256"]) == [late, early]


def test_collector_restart_does_not_override_temporal_order():
    early = _segment(start=0, archive=10, receive=9, sha="b" * 64, instance="old")
    late = _segment(start=0, archive=20, receive=19, sha="a" * 64, instance="new")
    assert sorted([late, early], key=segment_temporal_order_key) == [early, late]


def test_deterministic_tie_breaker_is_sha_only_after_times():
    a = _segment(start=0, archive=10, receive=10, sha="a" * 64)
    b = _segment(start=0, archive=10, receive=10, sha="b" * 64)
    assert sorted([b, a], key=segment_temporal_order_key) == [a, b]


def test_record_identity_and_apply_order_are_separate():
    assert record_identity_key("sha", 7) == ("sha", 7)
    assert record_apply_order_key(3, 7) == (3, 7)


def test_episode1_committed_parity_remains_exact():
    report_path = (
        Path(__file__).parents[1]
        / "runs"
        / "clickhouse_research_store_pilot_v1"
        / "detail_parity_report.json"
    )
    report = json.loads(report_path.read_text())
    level = report["level_change_parity"]
    states = report["regressions"]["states_100ms"]
    assert level["reference_rows"] == level["clickhouse_rows"] == 30_939
    assert level["reference_hash"] == level["clickhouse_hash"]
    assert states["compared_buckets"] == 600
    assert states["status"] == "PARITY_EXACT"
