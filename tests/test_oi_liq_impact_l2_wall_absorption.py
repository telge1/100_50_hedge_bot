"""Focused offline tests for F3 wall-absorption discovery."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from orderbook_analyse.oi_liq_impact_l2.wall_absorption.audit import (
    run_data_availability_audit,
)
from orderbook_analyse.oi_liq_impact_l2.wall_absorption.clusters import (
    build_flush_clusters,
    cluster_sensitivity_counts,
)
from orderbook_analyse.oi_liq_impact_l2.wall_absorption.discovery import (
    run_wall_absorption_discovery,
)
from orderbook_analyse.oi_liq_impact_l2.wall_absorption.lifecycle import (
    LevelDelta,
    WallLifecycleTracker,
    comparison_group_from_state,
)
from orderbook_analyse.oi_liq_impact_l2.wall_absorption.wall_candidates import (
    build_wall_candidates,
)
from orderbook_analyse.orderbook_replay import OrderBookState


def _candidate(minute: str, direction: str, candidate_id: str) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "symbol": "BTCUSDT",
        "minute": minute,
        "decision_at": minute,
        "direction": direction,
    }


def _write_f1_bundle(tmp_path: Path) -> Path:
    f1 = tmp_path / "f1"
    f1.mkdir(parents=True)
    minutes = pd.date_range("2026-08-20T12:33:00Z", periods=6, freq="1min", tz="UTC")
    rows = []
    for minute in minutes:
        for direction in ("LONG", "SHORT"):
            rows.append(
                {
                    "symbol": "BTCUSDT",
                    "minute": minute.isoformat().replace("+00:00", "Z"),
                    "direction": direction,
                    "directional_flush_observed": direction == "LONG"
                    and minute.minute in {33, 34, 36},
                }
            )
    pd.DataFrame(rows).to_csv(f1 / "minute_features.csv", index=False)
    candidates = [
        _candidate("2026-08-20T12:33:00Z", "LONG", "oildisc:a"),
        _candidate("2026-08-20T12:34:00Z", "LONG", "oildisc:b"),
        _candidate("2026-08-20T12:36:00Z", "LONG", "oildisc:c"),
        _candidate("2026-08-20T12:35:00Z", "SHORT", "oildisc:d"),
    ]
    pd.DataFrame(candidates).to_csv(f1 / "flush_candidates.csv", index=False)
    manifest = {
        "format_version": "oi_liq_impact_l2_discovery/v2",
        "counts": {"candidate_rows": len(candidates)},
    }
    (f1 / "discovery_manifest.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )
    return f1


def _book_with_levels() -> OrderBookState:
    book = OrderBookState()
    book.has_snapshot = True
    book.bids = {
        Decimal("100"): Decimal("5"),
        Decimal("99"): Decimal("20"),
        Decimal("98"): Decimal("3"),
    }
    book.asks = {
        Decimal("101"): Decimal("4"),
        Decimal("102"): Decimal("15"),
        Decimal("103"): Decimal("2"),
    }
    return book


def test_audit_blocks_when_no_per_level_source(tmp_path: Path) -> None:
    audit = run_data_availability_audit(
        files_root=tmp_path / "missing",
        client=None,
        query_clickhouse=False,
    )
    assert audit.passed is False
    assert audit.verdict == "BTC_F3_WALL_ABSORPTION_DISCOVERY_BLOCKED"
    assert "orderbook_features_1s_v2" in audit.block_reason


def test_long_uses_bid_walls_short_uses_ask_walls() -> None:
    book = _book_with_levels()
    long_candidates = build_wall_candidates(
        cluster_id="c1", symbol="BTCUSDT", direction="LONG", book=book
    )
    short_candidates = build_wall_candidates(
        cluster_id="c1", symbol="BTCUSDT", direction="SHORT", book=book
    )
    assert all(c.wall_price <= Decimal("100") for c in long_candidates)
    assert all(c.wall_price >= Decimal("101") for c in short_candidates)


def test_primary_wall_anchor_is_largest_qty_then_nearest() -> None:
    book = _book_with_levels()
    candidates = build_wall_candidates(
        cluster_id="c1", symbol="BTCUSDT", direction="LONG", book=book
    )
    primary = [c for c in candidates if c.is_primary_anchor]
    assert len(primary) == 1
    assert primary[0].wall_price == Decimal("99")
    assert primary[0].wall_qty == Decimal("20")


def test_wall_ranking_is_deterministic() -> None:
    book = _book_with_levels()
    first = build_wall_candidates(
        cluster_id="c1", symbol="BTCUSDT", direction="LONG", book=book
    )
    second = build_wall_candidates(
        cluster_id="c1", symbol="BTCUSDT", direction="LONG", book=book
    )
    assert [c.sort_key for c in first] == [c.sort_key for c in second]


def test_carried_forward_does_not_count_as_refill() -> None:
    tracker = WallLifecycleTracker(
        cluster_id="c1",
        direction="LONG",
        wall_price=Decimal("99"),
        initial_qty=Decimal("20"),
    )
    book = _book_with_levels()
    tracker.observe_second(
        second="2026-08-20T12:33:01Z",
        book=book,
        last_price=Decimal("99.5"),
        delta=LevelDelta(carried_forward=True),
    )
    assert tracker.cumulative_added == Decimal("0")
    assert tracker.cumulative_removed == Decimal("0")
    assert tracker.rows[-1]["genuine_added"] == 0.0


def test_genuine_removal_not_traded_through() -> None:
    tracker = WallLifecycleTracker(
        cluster_id="c1",
        direction="LONG",
        wall_price=Decimal("99"),
        initial_qty=Decimal("20"),
    )
    book = _Book_after_removal()
    tracker.observe_second(
        second="2026-08-20T12:33:02Z",
        book=book,
        last_price=Decimal("99.5"),
        delta=LevelDelta(removed=Decimal("5")),
    )
    assert tracker.state == "WALL_PARTIALLY_CONSUMED"


def _Book_after_removal() -> OrderBookState:
    book = _book_with_levels()
    book.bids[Decimal("99")] = Decimal("15")
    return book


def test_trade_through_detected_when_level_missing_and_price_crosses() -> None:
    tracker = WallLifecycleTracker(
        cluster_id="c1",
        direction="LONG",
        wall_price=Decimal("99"),
        initial_qty=Decimal("20"),
    )
    book = _book_with_levels()
    book.bids.pop(Decimal("99"))
    tracker.observe_second(
        second="2026-08-20T12:33:03Z",
        book=book,
        last_price=Decimal("98.5"),
        delta=LevelDelta(removed=Decimal("20")),
    )
    assert tracker.state == "WALL_TRADED_THROUGH"


def test_sequence_gap_aborts_lifecycle() -> None:
    tracker = WallLifecycleTracker(
        cluster_id="c1",
        direction="LONG",
        wall_price=Decimal("99"),
        initial_qty=Decimal("20"),
    )
    tracker.observe_second(
        second="2026-08-20T12:33:04Z",
        book=_book_with_levels(),
        last_price=Decimal("99.5"),
        delta=LevelDelta(sequence_gap=True),
    )
    assert tracker.aborted is True
    assert tracker.state == "WALL_DATA_ABORT"


def test_cluster_assignment_is_deterministic() -> None:
    candidates = [
        _candidate("2026-08-20T12:33:00Z", "LONG", "oildisc:a"),
        _candidate("2026-08-20T12:34:00Z", "LONG", "oildisc:b"),
        _candidate("2026-08-20T12:36:00Z", "LONG", "oildisc:c"),
    ]
    clusters = build_flush_clusters(candidates, gap_minutes=1)
    assert len(clusters) == 2
    assert clusters[0].primary_candidate_id == "oildisc:a"
    assert clusters[1].primary_candidate_id == "oildisc:c"


def test_overlapping_candidates_are_not_deleted() -> None:
    candidates = [
        _candidate("2026-08-20T12:33:00Z", "LONG", "oildisc:a"),
        _candidate("2026-08-20T12:34:00Z", "LONG", "oildisc:b"),
    ]
    clusters = build_flush_clusters(candidates, gap_minutes=1)
    assert sum(len(c.candidate_ids) for c in clusters) == 2


def test_cluster_sensitivity_is_descriptive_only() -> None:
    candidates = [
        _candidate("2026-08-20T12:33:00Z", "LONG", "oildisc:a"),
        _candidate("2026-08-20T12:34:00Z", "LONG", "oildisc:b"),
        _candidate("2026-08-20T12:36:00Z", "LONG", "oildisc:c"),
    ]
    rows = cluster_sensitivity_counts(candidates)
    assert {row["gap_minutes"] for row in rows} == {1, 2, 3, 5}


def test_absorption_metrics_avoid_division_by_zero() -> None:
    tracker = WallLifecycleTracker(
        cluster_id="c1",
        direction="LONG",
        wall_price=Decimal("99"),
        initial_qty=Decimal("0"),
    )
    tracker.observe_second(
        second="2026-08-20T12:33:05Z",
        book=_book_with_levels(),
        last_price=Decimal("99.5"),
        delta=LevelDelta(added=Decimal("1")),
    )
    assert tracker.rows[-1]["removal_ratio"] is None


def test_comparison_groups_do_not_use_outcomes() -> None:
    assert comparison_group_from_state("WALL_REFILLED", False) == "WALL_HELD_OR_REFILLED"
    assert (
        comparison_group_from_state("WALL_TRADED_THROUGH", False)
        == "WALL_REMOVED_OR_TRADED_THROUGH"
    )


def test_blocked_discovery_writes_audit_and_clusters(tmp_path: Path) -> None:
    f1 = _write_f1_bundle(tmp_path)
    out = tmp_path / "f3"
    result = run_wall_absorption_discovery(
        f1_dir=f1,
        f2_dir=None,
        output_dir=out,
        files_root=tmp_path / "missing",
        query_clickhouse=False,
    )
    assert result.verdict == "BTC_F3_WALL_ABSORPTION_DISCOVERY_BLOCKED"
    audit = json.loads((out / "data_availability_audit.json").read_text())
    manifest = json.loads((out / "wall_discovery_manifest.json").read_text())
    clusters = pd.read_csv(out / "flush_clusters.csv")
    assert audit["passed"] is False
    assert manifest["verdict"] == "BTC_F3_WALL_ABSORPTION_DISCOVERY_BLOCKED"
    assert len(clusters) == 2


def test_output_hashes_are_stable(tmp_path: Path) -> None:
    f1 = _write_f1_bundle(tmp_path)
    out1 = tmp_path / "f3a"
    out2 = tmp_path / "f3b"
    run_wall_absorption_discovery(
        f1_dir=f1,
        output_dir=out1,
        files_root=tmp_path / "missing",
        query_clickhouse=False,
    )
    run_wall_absorption_discovery(
        f1_dir=f1,
        output_dir=out2,
        files_root=tmp_path / "missing",
        query_clickhouse=False,
    )
    h1 = hashlib.sha256((out1 / "flush_clusters.csv").read_bytes()).hexdigest()
    h2 = hashlib.sha256((out2 / "flush_clusters.csv").read_bytes()).hexdigest()
    assert h1 == h2


def test_audit_passes_with_plain_ob200_files(tmp_path: Path) -> None:
    symbol = "BTCUSDT"
    day = tmp_path / symbol / "2026-08-20"
    day.mkdir(parents=True)
    data_file = day / "2026-08-20_BTCUSDT_ob200.data"
    data_file.write_text('{"topic":"orderbook.200.BTCUSDT","type":"snapshot","ts":1,"data":{"s":"BTCUSDT","b":[["100","1"]],"a":[["101","1"]],"u":1,"seq":1}}\n')
    for day_name in ("2026-08-21", "2026-08-22", "2026-08-23", "2026-08-24"):
        d = tmp_path / symbol / day_name
        d.mkdir(parents=True)
        (d / f"{day_name}_{symbol}_ob200.data").write_text(data_file.read_text())
    audit = run_data_availability_audit(
        files_root=tmp_path,
        client=None,
        query_clickhouse=False,
    )
    assert audit.passed is True


def test_primary_candidate_is_first_chronological_not_outcome_based() -> None:
    clusters = build_flush_clusters(
        [
            _candidate("2026-08-20T12:36:00Z", "LONG", "oildisc:late"),
            _candidate("2026-08-20T12:33:00Z", "LONG", "oildisc:early"),
        ],
        gap_minutes=5,
    )
    assert len(clusters) == 1
    assert clusters[0].primary_candidate_id == "oildisc:early"


def test_first_second_without_predecessor_has_no_refill_ratio() -> None:
    tracker = WallLifecycleTracker(
        cluster_id="c1",
        direction="LONG",
        wall_price=Decimal("99"),
        initial_qty=Decimal("20"),
    )
    tracker.observe_second(
        second="2026-08-20T12:33:00Z",
        book=_book_with_levels(),
        last_price=Decimal("99.5"),
        delta=LevelDelta(added=Decimal("1")),
    )
    assert tracker.rows[-1]["refill_to_consumption_ratio"] is None


def test_blocked_run_does_not_emit_wall_lifecycle_or_reclaim_outputs(tmp_path: Path) -> None:
    f1 = _write_f1_bundle(tmp_path)
    out = tmp_path / "f3"
    run_wall_absorption_discovery(
        f1_dir=f1,
        output_dir=out,
        files_root=tmp_path / "missing",
        query_clickhouse=False,
    )
    assert not (out / "wall_lifecycle_1s.csv").exists()
    assert not (out / "wall_reclaim_events.csv").exists()
