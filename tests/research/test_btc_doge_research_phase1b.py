from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from research.btc_doge_research.contracts import sanitize_json
from research.btc_doge_research.seam_root_cause_audit import (
    RAW_CANONICAL_FROM,
    assert_readonly_sql,
)
from research.btc_doge_research.seam_variants import (
    EventState,
    selected_variants,
)


UTC = timezone.utc
BUCKET = datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC)


def event(
    event_ms: int,
    receive_ms: int,
    update_id: int,
    *,
    bid: str = "100",
    ask: str = "101",
) -> EventState:
    bid_value, ask_value = Decimal(bid), Decimal(ask)
    return EventState(
        event_time=BUCKET + timedelta(milliseconds=event_ms),
        receive_time=BUCKET + timedelta(milliseconds=receive_ms),
        update_id=update_id,
        exchange_sequence=update_id * 10,
        raw_event_type="delta",
        mid=(bid_value + ask_value) / 2,
        best_bid=bid_value,
        best_ask=ask_value,
        spread=ask_value - bid_value,
        bid_qty_l50=Decimal("10"),
        ask_qty_l50=Decimal("12"),
        imbalance_l50=Decimal("-2") / Decimal("22"),
        source_file="segment.zst",
        source_record=update_id,
    )


def test_bucket_start_end_first_last_and_asof_selection() -> None:
    before = event(-100, -20, 1)
    first = event(100, 180, 2)
    last = event(900, 980, 3)
    variants = selected_variants([before, first, last], BUCKET)

    assert variants["FIRST_EVENT_IN_SECOND"][0] == first
    assert variants["LAST_EVENT_IN_SECOND"][0] == last
    assert variants["LAST_EVENT_AT_OR_BEFORE_SECOND_START"][0] == before
    assert variants["LAST_EVENT_AT_OR_BEFORE_SECOND_END"][0] == last
    assert variants["CURRENT_PHASE1_IMPLEMENTATION"][0] == last
    assert variants["NEAREST_EVENT_TO_SECOND_START"][0] == before
    assert variants["NEAREST_EVENT_TO_SECOND_END"][0] == last


def test_event_time_and_receive_time_cross_boundary_differ() -> None:
    early = event(800, 900, 1, bid="100", ask="101")
    late_receive = event(950, 1050, 2, bid="102", ask="103")
    variants = selected_variants([early, late_receive], BUCKET)

    assert variants["EVENT_TIME_LAST"][0] == late_receive
    assert variants["RECEIVE_TIME_LAST"][0] == early


def test_identical_timestamp_order_is_update_id_deterministic() -> None:
    high = event(500, 610, 12)
    low = event(500, 600, 11)
    variants = selected_variants([high, low], BUCKET)

    assert variants["FIRST_EVENT_IN_SECOND"][0] == low
    assert variants["LAST_EVENT_IN_SECOND"][0] == high


def test_carried_forward_uses_prior_state() -> None:
    prior = event(-100, -50, 1)
    variants = selected_variants([prior], BUCKET)

    selected, genuine = variants["CURRENT_PHASE1_IMPLEMENTATION"]
    assert selected == prior
    assert genuine is False
    assert variants["LAST_EVENT_IN_SECOND"][0] is None


def test_phase1b_sql_guard_allows_reads_and_blocks_all_writes() -> None:
    assert_readonly_sql("SELECT 1")
    assert_readonly_sql("WITH 1 AS x SELECT x")
    for sql in (
        "INSERT INTO x VALUES (1)",
        "CREATE TABLE x (a Int8)",
        "ALTER TABLE x DELETE WHERE 1",
        "OPTIMIZE TABLE x",
        "DROP TABLE x",
    ):
        with pytest.raises(PermissionError):
            assert_readonly_sql(sql)


def test_transition_boundary_and_semantics_version_are_explicit() -> None:
    parsed = datetime.fromisoformat(RAW_CANONICAL_FROM.replace("Z", "+00:00"))
    assert parsed == datetime(2026, 8, 24, 22, 47, 54, tzinfo=UTC)
    assert parsed.tzinfo is UTC


def test_no_hindsight_in_phase1b_contract() -> None:
    versions = ("ch_live_receive_asof_v1", "raw_ob200_event_time_eos_v1")
    assert all("HINDSIGHT" not in version.upper() for version in versions)


def test_nan_inf_sanitizing_remains_json_safe() -> None:
    assert sanitize_json({"nan": float("nan"), "inf": float("inf")}) == {
        "nan": None,
        "inf": None,
    }
