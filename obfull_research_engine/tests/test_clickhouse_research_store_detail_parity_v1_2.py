"""Unit tests for ClickHouse v1_2 detail parity (LC + checkpoint + decimals)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from obfull_research_engine.clickhouse_research_store_v1.detail_parity import (
    DetailParityError,
    assert_lc_epoch_consistent_with_checkpoints,
    change_type_from_sizes,
    compare_canonical_lc_lists,
    compare_checkpoints,
    dec,
    hash_canonical_lcs,
    map_level_kind,
    sort_canonical_lcs,
    canonical_lc_row,
)


def test_decimal_compare_avoids_binary_float_drift():
    from obfull_research_engine.clickhouse_research_store_v1.detail_parity import canon_dec_str

    assert dec("0.001") + dec("0.002") == dec("0.003")
    assert canon_dec_str("2.780") == canon_dec_str("2.78")
    assert canon_dec_str(Decimal("79769.9000")) == "79769.9"


def test_change_type_from_sizes_and_level_kind():
    assert change_type_from_sizes(0, 1) == "ADD"
    assert change_type_from_sizes(1, 0) == "DELETE"
    assert change_type_from_sizes(1, 2) == "UPDATE"
    assert map_level_kind("LEVEL_ADD", 0, 1.5) == "ADD"
    assert map_level_kind("LEVEL_REMOVE", 2, 0) == "DELETE"
    assert map_level_kind("LEVEL_INCREASE", 1, 2) == "UPDATE"
    assert map_level_kind("LEVEL_DECREASE", 2, 1) == "UPDATE"
    with pytest.raises(DetailParityError):
        map_level_kind("LEVEL_ADD", 1, 2)  # sizes say UPDATE


def test_canonical_sort_and_hash_stable():
    rows = [
        canonical_lc_row(
            event_time_ns=2,
            update_id=1,
            seq=1,
            side="ask",
            price="100.1",
            old_size="1",
            new_size="2",
            change_type="UPDATE",
        ),
        canonical_lc_row(
            event_time_ns=1,
            update_id=9,
            seq=1,
            side="bid",
            price="99.9",
            old_size="0",
            new_size="1",
            change_type="ADD",
        ),
    ]
    sorted_rows = sort_canonical_lcs(rows)
    assert [r["event_time_ns"] for r in sorted_rows] == [1, 2]
    h1 = hash_canonical_lcs(rows)
    h2 = hash_canonical_lcs(list(reversed(rows)))
    assert h1 == h2


def test_missing_extra_duplicate_detection():
    a = canonical_lc_row(
        event_time_ns=1,
        update_id=1,
        seq=1,
        side="bid",
        price="1",
        old_size="0",
        new_size="1",
        change_type="ADD",
    )
    b = canonical_lc_row(
        event_time_ns=2,
        update_id=2,
        seq=2,
        side="ask",
        price="2",
        old_size="1",
        new_size="0",
        change_type="DELETE",
    )
    cmp = compare_canonical_lc_lists([a], [a, b])
    assert cmp["ok"] is False
    assert cmp["extra_rows"] == 1
    assert cmp["clickhouse_rows"] == 2

    cmp2 = compare_canonical_lc_lists([a, b], [a])
    assert cmp2["missing_rows"] == 1

    cmp3 = compare_canonical_lc_lists([a, a], [a, a])
    assert cmp3["duplicate_reference_rows"] == 1
    assert cmp3["duplicate_clickhouse_rows"] == 1
    assert cmp3["ok"] is False  # duplicates fail ok


def test_exact_parity_hashes_match():
    rows = [
        canonical_lc_row(
            event_time_ns=10,
            update_id=5,
            seq=6,
            side="bid",
            price="79769.9",
            old_size="2.78",
            new_size="2.779",
            change_type="UPDATE",
        )
    ]
    cmp = compare_canonical_lc_lists(rows, list(rows))
    assert cmp["ok"] is True
    assert cmp["reference_hash"] == cmp["clickhouse_hash"]
    assert cmp["missing_rows"] == 0


def test_checkpoint_book_hash_and_relative_epochs():
    ref = [
        {
            "event_time_ns": 100,
            "update_id": 1,
            "seq": 10,
            "replay_epoch": 4,
            "n_bid_levels": 3,
            "n_ask_levels": 2,
            "book_hash": "abc",
            "identity_reset": True,
        },
        {
            "event_time_ns": 200,
            "update_id": 2,
            "seq": 20,
            "replay_epoch": 5,
            "n_bid_levels": 4,
            "n_ask_levels": 2,
            "book_hash": "def",
            "identity_reset": True,
        },
    ]
    ch = [
        {**ref[0], "replay_epoch": 1},
        {**ref[1], "replay_epoch": 2},
    ]
    out = compare_checkpoints(ref, ch)
    assert out["ok"] is True
    assert out["relative_epochs"] == [0, 1]


def test_false_epoch_bump_stops():
    ref = [
        {
            "event_time_ns": 100,
            "update_id": 1,
            "seq": 10,
            "replay_epoch": 4,
            "n_bid_levels": 1,
            "n_ask_levels": 1,
            "book_hash": "a",
            "identity_reset": True,
        },
        {
            "event_time_ns": 200,
            "update_id": 2,
            "seq": 20,
            "replay_epoch": 5,
            "n_bid_levels": 1,
            "n_ask_levels": 1,
            "book_hash": "b",
            "identity_reset": True,
        },
    ]
    # CH skips epoch bump (same epoch twice) → fail
    ch = [
        {**ref[0], "replay_epoch": 1},
        {**ref[1], "replay_epoch": 1},
    ]
    out = compare_checkpoints(ref, ch)
    assert out["ok"] is False
    assert "EPOCH" in str(out.get("reason"))


def test_lc_epoch_must_match_start_checkpoint():
    ck = [{"replay_epoch": 1}, {"replay_epoch": 2}]
    meta = [{"replay_epoch": 1, "event_time_ns": 1}, {"replay_epoch": 2, "event_time_ns": 2}]
    with pytest.raises(DetailParityError, match="EPOCH"):
        assert_lc_epoch_consistent_with_checkpoints(meta, ck)
