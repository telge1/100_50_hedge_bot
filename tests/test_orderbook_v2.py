"""Unit tests for orderbook_v2 parser, book reconstruction and features.

Tests cover:
- Snapshot processing
- Delta insert / update / delete (qty=0)
- New snapshot resets book
- Sequence gap invalidates book
- Recovery via new snapshot after gap
- Bid/ask sorting
- Spread and mid price
- Microprice
- Imbalance L5/L10/L25/L50
- BPS distance bands
- Largest level (wall)
- UTC second bucketing (no lookahead)
- Day boundary
- Idempotency (double insert produces same logical count)
- Damaged archive handling
- Carry-forward: single empty second, two consecutive, day boundary, stats
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from decimal import Decimal
import inspect
from pathlib import Path

import pytest

from orderbook_analyse.orderbook_v2.book import (
    BookState,
    apply_delta,
    apply_snapshot,
    sorted_asks,
    sorted_bids,
)
from orderbook_analyse.orderbook_v2.features import (
    _depth_by_bps,
    _depth_by_levels,
    _imbalance,
    _wall,
    compute_features,
)
from orderbook_analyse.orderbook_v2.parser import parse_day_zip

ZERO = Decimal("0")


# ─── Book reconstruction tests ─────────────────────────────────────────────

def make_snapshot(bids: list, asks: list, u: int = 1, seq: int = 100) -> dict:
    return {"b": bids, "a": asks, "u": u, "seq": seq}


def make_delta(bids: list, asks: list, u: int = 2, seq: int = 101) -> dict:
    return {"b": bids, "a": asks, "u": u, "seq": seq}


def test_snapshot_sets_book():
    snap = make_snapshot([["0.5", "100"], ["0.4", "200"]], [["0.6", "150"]], u=10)
    book = apply_snapshot(snap)
    assert book.is_valid
    assert book.bids[Decimal("0.5")] == Decimal("100")
    assert book.bids[Decimal("0.4")] == Decimal("200")
    assert book.asks[Decimal("0.6")] == Decimal("150")
    assert book.last_u == 10


def test_delta_insert():
    snap = make_snapshot([["0.5", "100"]], [["0.6", "150"]], u=10)
    book = apply_snapshot(snap)
    delta = make_delta([["0.45", "50"]], [], u=11)
    book2, warns = apply_delta(book, delta)
    assert book2.is_valid
    assert book2.bids[Decimal("0.45")] == Decimal("50")
    assert book2.bids[Decimal("0.5")] == Decimal("100")
    assert not warns


def test_delta_update():
    snap = make_snapshot([["0.5", "100"]], [["0.6", "150"]], u=10)
    book = apply_snapshot(snap)
    delta = make_delta([["0.5", "200"]], [], u=11)
    book2, warns = apply_delta(book, delta)
    assert book2.bids[Decimal("0.5")] == Decimal("200")
    assert not warns


def test_delta_delete_qty_zero():
    snap = make_snapshot([["0.5", "100"], ["0.4", "50"]], [["0.6", "150"]], u=10)
    book = apply_snapshot(snap)
    delta = make_delta([["0.5", "0"]], [], u=11)
    book2, warns = apply_delta(book, delta)
    assert Decimal("0.5") not in book2.bids
    assert Decimal("0.4") in book2.bids


def test_new_snapshot_resets_book():
    snap1 = make_snapshot([["0.5", "100"]], [["0.6", "150"]], u=10)
    book = apply_snapshot(snap1)
    # New snapshot completely replaces
    snap2 = make_snapshot([["0.9", "999"]], [["1.0", "888"]], u=20)
    book2 = apply_snapshot(snap2)
    assert Decimal("0.5") not in book2.bids
    assert book2.bids[Decimal("0.9")] == Decimal("999")
    assert book2.last_u == 20


def test_seq_gap_invalidates_book():
    snap = make_snapshot([["0.5", "100"]], [["0.6", "150"]], u=10)
    book = apply_snapshot(snap)
    # Gap: u=12 instead of expected 11
    delta = make_delta([], [], u=12)
    book2, warns = apply_delta(book, delta)
    assert not book2.is_valid
    assert any("seq_gap" in w for w in warns)


def test_recovery_after_gap():
    snap = make_snapshot([["0.5", "100"]], [["0.6", "150"]], u=10)
    book = apply_snapshot(snap)
    delta_bad = make_delta([], [], u=12)
    book2, _ = apply_delta(book, delta_bad)
    assert not book2.is_valid
    # Recovery via new snapshot
    snap2 = make_snapshot([["0.7", "200"]], [["0.8", "100"]], u=20)
    book3 = apply_snapshot(snap2)
    assert book3.is_valid
    assert book3.bids[Decimal("0.7")] == Decimal("200")


def test_bids_sorted_desc():
    snap = make_snapshot(
        [["0.3", "1"], ["0.5", "2"], ["0.4", "3"]], [["0.6", "10"]], u=1
    )
    book = apply_snapshot(snap)
    prices = [p for p, _ in sorted_bids(book)]
    assert prices == sorted(prices, reverse=True)


def test_asks_sorted_asc():
    snap = make_snapshot([["0.5", "10"]], [["0.7", "1"], ["0.6", "2"], ["0.9", "3"]], u=1)
    book = apply_snapshot(snap)
    prices = [p for p, _ in sorted_asks(book)]
    assert prices == sorted(prices)


def test_seq_dup_skipped():
    snap = make_snapshot([["0.5", "100"]], [["0.6", "150"]], u=10)
    book = apply_snapshot(snap)
    delta = make_delta([["0.5", "200"]], [], u=11)
    book2, _ = apply_delta(book, delta)
    # Duplicate: same u=11
    book3, warns = apply_delta(book2, make_delta([["0.5", "999"]], [], u=11))
    assert any("seq_dup" in w for w in warns)
    # qty should NOT have changed to 999
    assert book3.bids[Decimal("0.5")] == Decimal("200")


# ─── Features / metrics tests ──────────────────────────────────────────────

def _simple_book(n: int = 10, bid_base: float = 1.0, ask_base: float = 1.001) -> BookState:
    bids = {Decimal(str(round(bid_base - i * 0.001, 6))): Decimal("100") for i in range(n)}
    asks = {Decimal(str(round(ask_base + i * 0.001, 6))): Decimal("100") for i in range(n)}
    return BookState(bids=bids, asks=asks, last_u=1, last_seq=1, is_valid=True)


def test_spread_and_mid():
    book = _simple_book()
    bids = sorted_bids(book); asks = sorted_asks(book)
    best_bid = bids[0][0]; best_ask = asks[0][0]
    spread = best_ask - best_bid
    mid = (best_bid + best_ask) / Decimal("2")
    assert spread > ZERO
    assert best_bid < mid < best_ask


def test_microprice():
    bids = {Decimal("1.000"): Decimal("200")}
    asks = {Decimal("1.002"): Decimal("100")}
    book = BookState(bids=bids, asks=asks, last_u=1, last_seq=1, is_valid=True)
    row = compute_features(book, 0, 0, 0, 1, symbol="TEST")
    # microprice = (ask*bid_qty + bid*ask_qty) / (bid_qty + ask_qty)
    expected = (Decimal("1.002") * 200 + Decimal("1.000") * 100) / 300
    assert abs(row["microprice"] - expected) < Decimal("0.0000001")


def test_imbalance_formula():
    assert _imbalance(Decimal("100"), Decimal("100")) == ZERO
    assert _imbalance(Decimal("200"), Decimal("100")) > ZERO
    assert _imbalance(Decimal("0"), Decimal("0")) == ZERO
    val = _imbalance(Decimal("300"), Decimal("100"))
    assert val == Decimal("200") / Decimal("400")


def test_depth_by_levels():
    levels = [(Decimal("1.0"), Decimal("50")), (Decimal("0.9"), Decimal("30")),
              (Decimal("0.8"), Decimal("20"))]
    q5, n5 = _depth_by_levels(levels, 5)
    assert q5 == Decimal("100")
    q2, n2 = _depth_by_levels(levels, 2)
    assert q2 == Decimal("80")


def test_depth_by_bps():
    mid = Decimal("1.000")
    # 10 bps from 1.000 = 0.001
    bids = [(Decimal("0.9995"), Decimal("100")), (Decimal("0.998"), Decimal("50"))]
    q, n = _depth_by_bps(bids, mid, Decimal("10"), is_bid=True)
    assert q == Decimal("100")  # 0.998 is 20 bps away, excluded
    q25, _ = _depth_by_bps(bids, mid, Decimal("25"), is_bid=True)
    assert q25 == Decimal("150")


def test_wall_largest_level():
    mid = Decimal("1.000")
    levels = [(Decimal("0.999"), Decimal("100")), (Decimal("0.998"), Decimal("500")),
              (Decimal("0.990"), Decimal("200"))]
    p, q, n, bps, ratio = _wall(levels, mid)
    assert p == Decimal("0.998")  # largest qty in range
    assert q == Decimal("500")


def test_crossed_book_is_invalid():
    bids = {Decimal("1.005"): Decimal("100")}
    asks = {Decimal("1.000"): Decimal("100")}
    book = BookState(bids=bids, asks=asks, last_u=1, last_seq=1, is_valid=True)
    row = compute_features(book, 0, 0, 0, 1, symbol="TEST")
    assert row["is_valid"] == 0
    assert "crossed" in row["quality_flags"]


def test_empty_book_invalid():
    book = BookState(bids={}, asks={}, last_u=0, last_seq=0, is_valid=True)
    row = compute_features(book, 0, 0, 0, 0, symbol="TEST")
    assert row["is_valid"] == 0


# ─── Parser / bucketing tests ───────────────────────────────────────────────

def _make_zip(lines: list[str]) -> Path:
    """Build an in-memory ZIP, write to tempfile, return path."""
    inner_name = "2026-01-01_TEST_ob200.data"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        content = "\n".join(lines).encode()
        zf.writestr(inner_name, content)
    buf.seek(0)
    f = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    f.write(buf.read())
    f.close()
    return Path(f.name)


def _event(ts_ms: int, type_: str, bids: list, asks: list, u: int, seq: int = 0) -> str:
    return json.dumps({
        "topic": "orderbook.200.TEST", "type": type_, "ts": ts_ms, "cts": None,
        "data": {"s": "TEST", "b": bids, "a": asks, "u": u, "seq": seq},
    })


def test_utc_second_bucketing():
    """Events in the same second must produce a single row."""
    t = 1_700_000_000_000  # base second
    lines = [
        _event(t, "snapshot", [["1.000", "100"]], [["1.001", "50"]], u=1),
        _event(t + 100, "delta", [["1.000", "110"]], [], u=2),
        _event(t + 900, "delta", [["1.000", "120"]], [], u=3),
        _event(t + 1000, "snapshot", [["1.000", "200"]], [["1.002", "30"]], u=4),
    ]
    zp = _make_zip(lines)
    try:
        rows, stats = parse_day_zip(zp, symbol="TEST")
        # First second: bucket at t//1000*1000
        bucket0 = rows[0]["bucket_start"].timestamp() * 1000
        assert abs(bucket0 - (t // 1000) * 1000) < 1
        # Two seconds emitted
        assert len(rows) >= 2
    finally:
        os.unlink(zp)


def test_no_lookahead():
    """Feature for second T must not use events from second T+1."""
    t = 1_700_000_001_000  # second 1
    lines = [
        _event(t - 1000, "snapshot", [["1.000", "100"]], [["1.001", "50"]], u=1),
        _event(t - 100, "delta", [["1.000", "200"]], [], u=2),
        # Second boundary
        _event(t, "delta", [["1.000", "999"]], [], u=3),
    ]
    zp = _make_zip(lines)
    try:
        rows, _ = parse_day_zip(zp, symbol="TEST")
        # Row for first second should have best_bid=200, NOT 999
        first_row = rows[0]
        assert first_row["best_bid_qty"] == Decimal("200")
    finally:
        os.unlink(zp)


def test_seq_gap_marks_invalid():
    t = 1_700_000_000_000
    lines = [
        _event(t, "snapshot", [["1.000", "100"]], [["1.001", "50"]], u=1),
        _event(t + 100, "delta", [], [], u=5),  # gap: expected 2, got 5
    ]
    zp = _make_zip(lines)
    try:
        rows, stats = parse_day_zip(zp, symbol="TEST")
        assert stats.n_seq_gaps >= 1
    finally:
        os.unlink(zp)


def test_no_start_snapshot_invalid():
    t = 1_700_000_000_000
    lines = [
        _event(t, "delta", [["1.000", "100"]], [["1.001", "50"]], u=2),
    ]
    zp = _make_zip(lines)
    try:
        rows, stats = parse_day_zip(zp, symbol="TEST")
        # Row must be invalid (no initial snapshot)
        assert all(r["is_valid"] == 0 for r in rows)
        assert any("no_start_snapshot" in r["quality_flags"] for r in rows)
    finally:
        os.unlink(zp)


def test_damaged_archive():
    f = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    f.write(b"PK\x00\x00 not a valid zip at all garbage garbage")
    f.close()
    zp = Path(f.name)
    try:
        with pytest.raises(Exception):
            parse_day_zip(zp, symbol="TEST")
    finally:
        os.unlink(zp)


def test_day_boundary_two_files_independent():
    """Each day file is independent; we just check last second of file is emitted."""
    t = 1_700_000_000_000
    lines = [
        _event(t, "snapshot", [["1.000", "100"]], [["1.001", "50"]], u=1),
        _event(t + 999, "delta", [["1.000", "150"]], [], u=2),
        # Last event triggers emission of first second only when a new second arrives
        # or at file end
    ]
    zp = _make_zip(lines)
    try:
        rows, _ = parse_day_zip(zp, symbol="TEST")
        assert len(rows) >= 1
        # The last bucket must have been emitted at file end
        last_row = rows[-1]
        assert last_row["best_bid_qty"] == Decimal("150")
    finally:
        os.unlink(zp)


def test_imbalance_l5_l10():
    book = _simple_book(n=20, bid_base=1.0, ask_base=1.002)
    row = compute_features(book, 0, 0, 0, 1, symbol="TEST")
    # With equal qtys on both sides, imbalance should be ~0
    assert abs(row["imbalance_l5"]) < Decimal("0.001")
    assert abs(row["imbalance_l10"]) < Decimal("0.001")


def test_wall_ratio():
    mid = Decimal("1.000")
    levels = [(Decimal(str(round(1.0 - i * 0.001, 6))), Decimal("100")) for i in range(10)]
    # Make one level huge
    levels[3] = (levels[3][0], Decimal("1000"))
    p, q, n, bps, ratio = _wall(levels, mid)
    assert q == Decimal("1000")
    assert ratio > Decimal("1")


# ─── Carry-Forward tests (A–J) ──────────────────────────────────────────────

def test_cf_A_single_empty_second():
    """A: one empty second between two event seconds → carry-forward row emitted.

    Use day_start_ms so the window is exactly [t, t+2999] (3 seconds).
    """
    t = 1_700_000_000_000  # second 0
    lines = [
        _event(t,        "snapshot", [["1.000", "100"]], [["1.001", "50"]], u=1),
        # second 1 (t+1000) has NO events
        _event(t + 2000, "delta",    [["1.000", "110"]], [],                u=2),
    ]
    zp = _make_zip(lines)
    try:
        # day window = only 3 seconds for this test (override expected_seconds via day_start_ms
        # with a tiny window won't work directly, but we can check bucket presence and stats)
        rows, stats = parse_day_zip(zp, symbol="TEST", day_start_ms=t)
        buckets = {int(r["bucket_start"].timestamp() * 1000) for r in rows}
        assert t in buckets
        assert t + 1000 in buckets
        assert t + 2000 in buckets
        # The second t+1000 must be carry-forward
        cf_at_t1 = next(r for r in rows if int(r["bucket_start"].timestamp() * 1000) == t + 1000)
        assert "carried_forward" in cf_at_t1["quality_flags"]
        assert cf_at_t1["is_valid"] == 1
        assert cf_at_t1["processed_updates"] == 0
        # At least 1 carry-forward second counted (there are more from window fill, but at least t+1000)
        assert stats.carried_forward_seconds >= 1
        assert stats.event_seconds == 2
    finally:
        os.unlink(zp)


def test_cf_B_two_consecutive_empty_seconds():
    """B: two consecutive empty seconds → at least two carry-forward rows emitted."""
    t = 1_700_000_000_000
    lines = [
        _event(t,        "snapshot", [["1.000", "100"]], [["1.001", "50"]], u=1),
        # seconds 1 and 2 have NO events
        _event(t + 3000, "delta",    [["1.000", "120"]], [],                u=2),
    ]
    zp = _make_zip(lines)
    try:
        rows, stats = parse_day_zip(zp, symbol="TEST", day_start_ms=t)
        buckets = {int(r["bucket_start"].timestamp() * 1000) for r in rows}
        assert t + 1000 in buckets
        assert t + 2000 in buckets
        # Both must be carry-forward
        cf_t1 = next(r for r in rows if int(r["bucket_start"].timestamp() * 1000) == t + 1000)
        cf_t2 = next(r for r in rows if int(r["bucket_start"].timestamp() * 1000) == t + 2000)
        assert "carried_forward" in cf_t1["quality_flags"]
        assert "carried_forward" in cf_t2["quality_flags"]
        assert stats.carried_forward_seconds >= 2
    finally:
        os.unlink(zp)


def test_cf_C_no_gaps_no_carry_forward_within_events():
    """C: consecutive event seconds → no carry-forward between them (only after end of day)."""
    t = 1_700_000_000_000
    lines = [
        _event(t,        "snapshot", [["1.000", "100"]], [["1.001", "50"]], u=1),
        _event(t + 1000, "delta",    [["1.000", "110"]], [],                u=2),
        _event(t + 2000, "delta",    [["1.000", "120"]], [],                u=3),
    ]
    zp = _make_zip(lines)
    try:
        rows, stats = parse_day_zip(zp, symbol="TEST", day_start_ms=t)
        # Only the 3 event seconds should NOT be carry-forward
        event_rows = [r for r in rows if "carried_forward" not in r["quality_flags"]]
        assert len(event_rows) == 3
        assert stats.event_seconds == 3
        # Carry-forward rows come only AFTER t+2000 to fill out the day window
        cf_rows = [r for r in rows if "carried_forward" in r["quality_flags"]]
        for r in cf_rows:
            assert int(r["bucket_start"].timestamp() * 1000) > t + 2000
    finally:
        os.unlink(zp)


def test_cf_D_day_boundary():
    """D: next-day midnight event must not add a carry-forward row for that second."""
    # Day starts at t (second 0). Window = [t, t + 86399*1000].
    # A next-day event at t + 86400*1000 must NOT appear as carried_forward within the window.
    t = 1_700_000_000_000
    # Only two event seconds within the day, then a snapshot at next-day boundary
    lines = [
        _event(t,                   "snapshot", [["1.000", "100"]], [["1.001", "50"]], u=1),
        _event(t + 1000,            "delta",    [["1.000", "110"]], [],                u=2),
        _event(t + 86400 * 1000,    "snapshot", [["1.000", "200"]], [["1.001", "80"]], u=3),
    ]
    zp = _make_zip(lines)
    try:
        rows, stats = parse_day_zip(zp, symbol="TEST", day_start_ms=t)
        # Only rows within [t, t + 86399*1000] plus carry-forward rows for the rest of the day
        # The midnight event (t + 86400000) must not appear in the 86400-second window
        row_buckets = [int(r["bucket_start"].timestamp() * 1000) for r in rows]
        assert t + 86400 * 1000 not in row_buckets, "Next-day bucket must not appear in day window"
        # event_seconds + carried_forward_seconds should equal expected_seconds
        assert stats.event_seconds + stats.carried_forward_seconds == stats.expected_seconds
        assert stats.missing_seconds == 0
    finally:
        os.unlink(zp)


def test_cf_E_no_lookahead_in_carry_forward():
    """E: carry-forward row must not use state from the future event that ends the gap."""
    t = 1_700_000_000_000
    lines = [
        _event(t,        "snapshot", [["1.000", "100"]], [["1.001", "50"]], u=1),
        # second 1 empty
        _event(t + 2000, "delta",    [["1.000", "999"]], [],                u=2),  # future
    ]
    zp = _make_zip(lines)
    try:
        rows, stats = parse_day_zip(zp, symbol="TEST")
        cf_row = next(r for r in rows if "carried_forward" in r["quality_flags"])
        # carry-forward must use qty=100 (from second 0), NOT 999 (from second 2)
        assert cf_row["best_bid_qty"] == Decimal("100")
    finally:
        os.unlink(zp)


def test_cf_F_idempotent_reimport():
    """F: parsing same ZIP twice gives identical rows (deterministic output)."""
    t = 1_700_000_000_000
    lines = [
        _event(t,        "snapshot", [["1.000", "100"]], [["1.001", "50"]], u=1),
        _event(t + 2000, "delta",    [["1.000", "110"]], [],                u=2),
    ]
    zp = _make_zip(lines)
    try:
        rows1, stats1 = parse_day_zip(zp, symbol="TEST")
        rows2, stats2 = parse_day_zip(zp, symbol="TEST")
        assert len(rows1) == len(rows2)
        for r1, r2 in zip(rows1, rows2):
            assert r1["bucket_start"] == r2["bucket_start"]
            assert r1["best_bid_price"] == r2["best_bid_price"]
            assert r1["quality_flags"] == r2["quality_flags"]
        assert stats1.carried_forward_seconds == stats2.carried_forward_seconds
    finally:
        os.unlink(zp)


def test_cf_G_stats_missing_buckets_correctly_reported():
    """G: stats report missing_seconds=0 when carry-forward fills all gaps."""
    t = 1_700_000_000_000
    lines = [
        _event(t,        "snapshot", [["1.000", "100"]], [["1.001", "50"]], u=1),
        _event(t + 5000, "delta",    [["1.000", "110"]], [],                u=2),
    ]
    zp = _make_zip(lines)
    try:
        rows, stats = parse_day_zip(zp, symbol="TEST", day_start_ms=t)
        # 4 empty seconds between second 0 and second 5 (at minimum)
        assert stats.carried_forward_seconds >= 4
        assert stats.missing_seconds == 0
        # coverage_ratio must be 1.0
        assert abs(stats.coverage_ratio - 1.0) < 1e-9
        # The 4 specific gap seconds must all be carry-forward
        for offset in [1000, 2000, 3000, 4000]:
            cf = next(r for r in rows if int(r["bucket_start"].timestamp() * 1000) == t + offset)
            assert "carried_forward" in cf["quality_flags"]
    finally:
        os.unlink(zp)


def test_cf_H_stats_full_coverage_after_carry_forward():
    """H: emitted_seconds == expected_seconds after carry-forward."""
    t = 1_700_000_000_000
    lines = [
        _event(t,        "snapshot", [["1.000", "100"]], [["1.001", "50"]], u=1),
        _event(t + 3000, "delta",    [["1.000", "110"]], [],                u=2),
    ]
    zp = _make_zip(lines)
    try:
        rows, stats = parse_day_zip(zp, symbol="TEST")
        assert stats.emitted_seconds == stats.expected_seconds
        assert stats.missing_seconds == 0
    finally:
        os.unlink(zp)


def test_cf_I_event_metrics_zero_in_carry_forward():
    """I: event-based activity metrics in carry-forward rows are 0, not None."""
    t = 1_700_000_000_000
    lines = [
        _event(t,        "snapshot", [["1.000", "100"]], [["1.001", "50"]], u=1),
        _event(t + 2000, "delta",    [["1.000", "110"]], [],                u=2),
    ]
    zp = _make_zip(lines)
    try:
        rows, _ = parse_day_zip(zp, symbol="TEST")
        cf_row = next(r for r in rows if "carried_forward" in r["quality_flags"])
        # All event-based dynamics must be 0 (not None)
        assert cf_row["bid_qty_added"] == Decimal("0")
        assert cf_row["bid_qty_removed"] == Decimal("0")
        assert cf_row["ask_qty_added"] == Decimal("0")
        assert cf_row["ask_qty_removed"] == Decimal("0")
        assert cf_row["bid_add_count"] == 0
        assert cf_row["bid_remove_count"] == 0
        assert cf_row["ask_add_count"] == 0
        assert cf_row["ask_remove_count"] == 0
        assert cf_row["ofi"] == Decimal("0")
        assert cf_row["processed_updates"] == 0
    finally:
        os.unlink(zp)


def test_cf_J_state_features_match_previous_book():
    """J: state-based features in carry-forward match the last known book state."""
    t = 1_700_000_000_000
    # Snapshot at t: bid=1.000 qty=200, ask=1.002 qty=100
    lines = [
        _event(t,        "snapshot", [["1.000", "200"]], [["1.002", "100"]], u=1),
        # second t+1 is empty
        _event(t + 2000, "delta",    [["1.000", "300"]], [],                 u=2),
    ]
    zp = _make_zip(lines)
    try:
        rows, _ = parse_day_zip(zp, symbol="TEST")
        cf_row = next(r for r in rows if "carried_forward" in r["quality_flags"])
        # State-based features must reflect the book from second t (snapshot)
        assert cf_row["best_bid_price"] == Decimal("1.000")
        assert cf_row["best_bid_qty"] == Decimal("200")
        assert cf_row["best_ask_price"] == Decimal("1.002")
        assert cf_row["best_ask_qty"] == Decimal("100")
        expected_mid = (Decimal("1.000") + Decimal("1.002")) / Decimal("2")
        assert cf_row["mid_price"] == expected_mid
        assert cf_row["spread_abs"] == Decimal("0.002")
    finally:
        os.unlink(zp)


def test_parser_version_compute_features_writes_ob200_v2():
    from orderbook_analyse.orderbook_v2 import PARSER_VERSION

    book = _simple_book(n=20, bid_base=1.0, ask_base=1.002)
    row = compute_features(book, 0, 0, 0, 1, symbol="TEST")
    assert row["parser_version"] == PARSER_VERSION
    assert PARSER_VERSION == "ob200_v2"


def test_cf_and_event_rows_have_same_ob200_v2_parser_version():
    """Ensure parse_day_zip emits ob200_v2 for both event and carry-forward rows."""
    t = 1_700_000_000_000  # second 0
    lines = [
        _event(t, "snapshot", [["1.000", "100"]], [["1.001", "50"]], u=1),
        # second 1 empty → carried_forward at t+1000
        _event(t + 2000, "delta", [["1.000", "110"]], [], u=2),
    ]
    zp = _make_zip(lines)
    try:
        rows, _ = parse_day_zip(zp, symbol="TEST", day_start_ms=t)

        v = rows[0]["parser_version"]
        assert v == "ob200_v2"

        event_rows = [r for r in rows if "carried_forward" not in r["quality_flags"]]
        cf_rows = [r for r in rows if "carried_forward" in r["quality_flags"]]

        assert len(event_rows) >= 2  # snapshot second + last delta second
        assert len(cf_rows) >= 1

        assert all(r["parser_version"] == v for r in event_rows)
        assert all(r["parser_version"] == v for r in cf_rows)
    finally:
        os.unlink(zp)


def test_pilot_and_manifest_use_ob200_v2_parser_version():
    from orderbook_analyse.orderbook_v2 import PARSER_VERSION
    from orderbook_analyse.orderbook_v2 import pilot

    assert pilot.PARSER_VERSION == PARSER_VERSION
    assert PARSER_VERSION == "ob200_v2"

    # The manifest row should be constructed using the same PARSER_VERSION symbol.
    src = inspect.getsource(pilot.run_pilot)
    assert '"parser_version": PARSER_VERSION' in src


def test_v2_path_has_no_ob200_v1_constant_left():
    from orderbook_analyse.orderbook_v2 import features as v2_features

    src = inspect.getsource(v2_features)
    assert "ob200_v1" not in src


def test_v2_modules_import_without_clickhouse_connection(monkeypatch):
    """Importing the V2 package must not construct a ClickHouse client."""
    import sys
    import types

    fake = types.ModuleType("clickhouse_connect")

    def _boom(*_a, **_k):
        raise AssertionError("clickhouse_connect.get_client must not run on import")

    fake.get_client = _boom
    monkeypatch.setitem(sys.modules, "clickhouse_connect", fake)

    import importlib
    import orderbook_analyse.orderbook_v2 as pkg
    import orderbook_analyse.orderbook_v2.pilot as pilot_mod
    import orderbook_analyse.orderbook_v2.ch_client as ch_client
    import orderbook_analyse.orderbook_v2.ch_writer as ch_writer
    importlib.reload(pkg)
    importlib.reload(ch_client)
    importlib.reload(ch_writer)
    importlib.reload(pilot_mod)
    assert pkg.PARSER_VERSION == "ob200_v2"


def test_pilot_help_does_not_touch_clickhouse():
    import subprocess
    import sys

    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    proc = subprocess.run(
        [sys.executable, "-m", "orderbook_analyse.orderbook_v2.pilot", "--help"],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "--optimize-final" in proc.stdout
    assert "--skip-optimize-final" not in proc.stdout
    assert "OPTIMIZE" in proc.stdout


def test_missing_clickhouse_config_raises_clear_error(monkeypatch):
    from orderbook_analyse.orderbook_v2.ch_client import (
        ClickHouseConfigError,
        load_clickhouse_settings,
    )

    for name in (
        "CLICKHOUSE_HOST",
        "CLICKHOUSE_HTTP_PORT",
        "CLICKHOUSE_DATABASE",
        "CLICKHOUSE_USER",
        "CLICKHOUSE_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)

    try:
        load_clickhouse_settings(load_env_file=False)
        raise AssertionError("expected ClickHouseConfigError")
    except ClickHouseConfigError as exc:
        msg = str(exc)
        assert "CLICKHOUSE_HOST" in msg
        assert "CLICKHOUSE_USER" in msg
        assert "incomplete" in msg.lower() or "Missing" in msg


def test_clickhouse_factory_uses_env_not_hardcoded_secrets(monkeypatch):
    from orderbook_analyse.orderbook_v2 import ch_client as ch_mod

    monkeypatch.setenv("CLICKHOUSE_HOST", "ch.example.test")
    monkeypatch.setenv("CLICKHOUSE_HTTP_PORT", "8124")
    monkeypatch.setenv("CLICKHOUSE_DATABASE", "orderbook_analysis")
    monkeypatch.setenv("CLICKHOUSE_USER", "importer")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "not-a-real-secret-for-test")

    cfg = ch_mod.load_clickhouse_settings(load_env_file=False)
    assert cfg.host == "ch.example.test"
    assert cfg.http_port == 8124
    assert cfg.database == "orderbook_analysis"
    assert cfg.user == "importer"
    assert cfg.password == "not-a-real-secret-for-test"

    src = inspect.getsource(ch_mod)
    assert "password=" not in src.lower() or 'password=cfg.password' in src.replace(" ", "")
    assert "CLICKHOUSE_PASSWORD" in src
    assert "not-a-real-secret-for-test" not in src
    assert "127.0.0.1" not in src


def test_get_clickhouse_client_passes_settings(monkeypatch):
    from orderbook_analyse.orderbook_v2.ch_client import (
        ClickHouseSettings,
        get_clickhouse_client,
    )

    captured = {}

    class _Fake:
        @staticmethod
        def get_client(**kwargs):
            captured.update(kwargs)
            return "client"

    monkeypatch.setitem(__import__("sys").modules, "clickhouse_connect", _Fake)
    settings = ClickHouseSettings(
        host="h", http_port=9, database="d", user="u", password="p",
    )
    client = get_clickhouse_client(settings)
    assert client == "client"
    assert captured["host"] == "h"
    assert captured["port"] == 9
    assert captured["username"] == "u"
    assert captured["database"] == "d"
    assert captured["password"] == "p"


def test_optimize_is_opt_in_only():
    from orderbook_analyse.orderbook_v2 import pilot

    calls: list[str] = []

    class _Client:
        pass

    def _fake_optimize(_client):
        calls.append("optimize")

    original = pilot.optimize_tables
    try:
        pilot.optimize_tables = _fake_optimize  # type: ignore[method-assign]
        assert inspect.signature(pilot.run_pilot).parameters["optimize_final"].default is False
        assert pilot.maybe_optimize_tables(_Client(), dry_run=False, optimize_final=False) is False
        assert calls == []
        assert pilot.maybe_optimize_tables(_Client(), dry_run=True, optimize_final=True) is False
        assert calls == []
        assert pilot.maybe_optimize_tables(_Client(), dry_run=False, optimize_final=True) is True
        assert calls == ["optimize"]
    finally:
        pilot.optimize_tables = original


def test_cli_optimize_final_flag_wiring(monkeypatch):
    from orderbook_analyse.orderbook_v2 import pilot

    captured: dict = {}

    def _fake_run_pilot(**kwargs):
        captured.update(kwargs)
        return {"decision": "DRY"}

    monkeypatch.setattr(pilot, "run_pilot", _fake_run_pilot)
    monkeypatch.setattr(sys, "argv", ["pilot"])
    pilot.main()
    assert captured["optimize_final"] is False

    captured.clear()
    monkeypatch.setattr(sys, "argv", ["pilot", "--optimize-final"])
    pilot.main()
    assert captured["optimize_final"] is True


def test_pilot_does_not_import_oi_collector():
    from orderbook_analyse.orderbook_v2 import ch_client, pilot

    assert "oi_liquidation_collector" not in inspect.getsource(pilot)
    assert "oi_liquidation_collector" not in inspect.getsource(ch_client)
