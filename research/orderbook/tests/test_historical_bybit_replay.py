"""Unit tests for historical Bybit orderbook replay (no full-day I/O)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from research.orderbook.historical_bybit_replay import (
    HistoricalBybitReplayer,
    OrderBook,
    ReplayError,
    SequenceStatus,
    apply_levels_trace,
    parse_ob_line,
    replay_symbol_day,
)


def _msg(
    *,
    typ: str,
    ts: int,
    u: int,
    seq: int,
    bids: list | None = None,
    asks: list | None = None,
    symbol: str = "APTUSDT",
    cts: int | None = None,
) -> str:
    return json.dumps(
        {
            "topic": f"orderbook.200.{symbol}",
            "type": typ,
            "ts": ts,
            "cts": ts - 5 if cts is None else cts,
            "data": {
                "s": symbol,
                "b": bids or [],
                "a": asks or [],
                "u": u,
                "seq": seq,
            },
        }
    )


def test_snapshot_initializes_book(tmp_path: Path) -> None:
    path = tmp_path / "2026-01-06_APTUSDT_ob200.data"
    path.write_text(
        _msg(
            typ="snapshot",
            ts=1000,
            u=1,
            seq=10,
            bids=[["1.10", "5"], ["1.09", "3"]],
            asks=[["1.11", "4"], ["1.12", "2"]],
        )
        + "\n"
    )
    # day_file_path layout
    day_dir = tmp_path / "APTUSDT" / "2026-01-06"
    day_dir.mkdir(parents=True)
    final = day_dir / "2026-01-06_APTUSDT_ob200.data"
    final.write_text(path.read_text())

    result = replay_symbol_day(
        "APTUSDT", "2026-01-06", 1000, data_root=tmp_path
    )
    assert result.bid_level_count == 2
    assert result.ask_level_count == 2
    assert result.best_bid == "1.10"
    assert result.best_ask == "1.11"
    assert result.last_update_id == 1
    assert result.deltas_applied == 0
    assert result.sequence_status == SequenceStatus.CLEAN


def test_bid_ask_update_and_delete(tmp_path: Path) -> None:
    day_dir = tmp_path / "APTUSDT" / "2026-01-06"
    day_dir.mkdir(parents=True)
    lines = [
        _msg(
            typ="snapshot",
            ts=1000,
            u=1,
            seq=10,
            bids=[["1.10", "5"], ["1.09", "3"]],
            asks=[["1.11", "4"]],
        ),
        _msg(
            typ="delta",
            ts=1001,
            u=2,
            seq=11,
            bids=[["1.10", "7"]],  # update
            asks=[["1.12", "1"]],  # insert
        ),
        _msg(
            typ="delta",
            ts=1002,
            u=3,
            seq=12,
            bids=[["1.09", "0"]],  # delete
            asks=[["1.11", "0"]],  # delete
        ),
    ]
    (day_dir / "2026-01-06_APTUSDT_ob200.data").write_text("\n".join(lines) + "\n")
    result = replay_symbol_day("APTUSDT", "2026-01-06", 1002, data_root=tmp_path)
    assert result.best_bid == "1.10"
    assert dict(result.bid_levels)["1.10"] == "7"
    assert "1.09" not in dict(result.bid_levels)
    assert result.best_ask == "1.12"
    assert "1.11" not in dict(result.ask_levels)
    assert result.deltas_applied == 2


def test_new_price_added(tmp_path: Path) -> None:
    day_dir = tmp_path / "APTUSDT" / "2026-01-06"
    day_dir.mkdir(parents=True)
    lines = [
        _msg(typ="snapshot", ts=1, u=1, seq=1, bids=[["1.0", "1"]], asks=[["1.1", "1"]]),
        _msg(typ="delta", ts=2, u=2, seq=2, bids=[["0.9", "9"]], asks=[]),
    ]
    (day_dir / "2026-01-06_APTUSDT_ob200.data").write_text("\n".join(lines) + "\n")
    result = replay_symbol_day("APTUSDT", "2026-01-06", 2, data_root=tmp_path)
    assert result.bid_level_count == 2
    assert dict(result.bid_levels)["0.9"] == "9"


def test_midstream_snapshot_reset(tmp_path: Path) -> None:
    day_dir = tmp_path / "APTUSDT" / "2026-01-06"
    day_dir.mkdir(parents=True)
    lines = [
        _msg(typ="snapshot", ts=1, u=1, seq=1, bids=[["1.0", "1"]], asks=[["2.0", "1"]]),
        _msg(typ="delta", ts=2, u=2, seq=2, bids=[["1.0", "5"]], asks=[]),
        _msg(
            typ="snapshot",
            ts=3,
            u=100,
            seq=50,
            bids=[["3.0", "8"]],
            asks=[["4.0", "9"]],
        ),
    ]
    (day_dir / "2026-01-06_APTUSDT_ob200.data").write_text("\n".join(lines) + "\n")
    result = replay_symbol_day("APTUSDT", "2026-01-06", 3, data_root=tmp_path)
    assert result.best_bid == "3.0"
    assert result.best_ask == "4.0"
    assert result.bid_level_count == 1
    assert result.ask_level_count == 1
    assert result.last_update_id == 100
    assert result.sequence_status == SequenceStatus.RESET_SEEN
    assert result.sequence_diagnostics.midstream_snapshot_resets == 1


def test_target_stops_and_no_future(tmp_path: Path) -> None:
    day_dir = tmp_path / "APTUSDT" / "2026-01-06"
    day_dir.mkdir(parents=True)
    lines = [
        _msg(typ="snapshot", ts=10, u=1, seq=1, bids=[["1.0", "1"]], asks=[["1.1", "1"]]),
        _msg(typ="delta", ts=20, u=2, seq=2, bids=[["1.0", "2"]], asks=[]),
        _msg(typ="delta", ts=30, u=3, seq=3, bids=[["1.0", "3"]], asks=[]),  # future
    ]
    (day_dir / "2026-01-06_APTUSDT_ob200.data").write_text("\n".join(lines) + "\n")
    result = replay_symbol_day("APTUSDT", "2026-01-06", 20, data_root=tmp_path)
    assert result.last_applied_message_ts_ms == 20
    assert dict(result.bid_levels)["1.0"] == "2"
    assert result.deltas_applied == 1
    assert result.invariants.last_applied_le_target


def test_determinism(tmp_path: Path) -> None:
    day_dir = tmp_path / "APTUSDT" / "2026-01-06"
    day_dir.mkdir(parents=True)
    lines = [
        _msg(
            typ="snapshot",
            ts=1,
            u=1,
            seq=1,
            bids=[["1.0", "1"], ["0.9", "2"]],
            asks=[["1.1", "3"], ["1.2", "4"]],
        ),
        _msg(typ="delta", ts=2, u=2, seq=2, bids=[["1.0", "9"]], asks=[["1.1", "0"]]),
    ]
    (day_dir / "2026-01-06_APTUSDT_ob200.data").write_text("\n".join(lines) + "\n")
    a = replay_symbol_day("APTUSDT", "2026-01-06", 2, data_root=tmp_path)
    b = replay_symbol_day("APTUSDT", "2026-01-06", 2, data_root=tmp_path)
    assert a.fingerprint() == b.fingerprint()


def test_malformed_line_skipped(tmp_path: Path) -> None:
    day_dir = tmp_path / "APTUSDT" / "2026-01-06"
    day_dir.mkdir(parents=True)
    lines = [
        _msg(typ="snapshot", ts=1, u=1, seq=1, bids=[["1.0", "1"]], asks=[["1.1", "1"]]),
        "NOT_JSON",
        _msg(typ="delta", ts=2, u=2, seq=2, bids=[["1.0", "2"]], asks=[]),
    ]
    (day_dir / "2026-01-06_APTUSDT_ob200.data").write_text("\n".join(lines) + "\n")
    result = replay_symbol_day("APTUSDT", "2026-01-06", 2, data_root=tmp_path)
    assert result.sequence_diagnostics.malformed_lines == 1
    assert dict(result.bid_levels)["1.0"] == "2"


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ReplayError, match="missing orderbook file"):
        replay_symbol_day("APTUSDT", "2099-01-01", 1, data_root=tmp_path)


def test_parse_ob_line_fields() -> None:
    msg = parse_ob_line(
        _msg(typ="delta", ts=5, u=9, seq=99, bids=[["1", "0"]], asks=[["2", "3"]]),
        source_line=7,
    )
    assert msg.message_type == "delta"
    assert msg.update_id == 9
    assert msg.cross_sequence == 99
    assert msg.bids[0] == (Decimal("1"), Decimal("0"))


def test_apply_levels_trace_update_delete() -> None:
    book = OrderBook()
    snap = parse_ob_line(
        _msg(typ="snapshot", ts=1, u=1, seq=1, bids=[["1.0", "5"]], asks=[["1.1", "4"]]),
        source_line=1,
    )
    book.apply_snapshot(snap)
    delta = parse_ob_line(
        _msg(typ="delta", ts=2, u=2, seq=2, bids=[["1.0", "0"], ["0.9", "1"]], asks=[["1.1", "8"]]),
        source_line=2,
    )
    rows = apply_levels_trace(book, delta)
    by_price = {(r["side"], r["price"]): r for r in rows}
    assert by_price[("bid", "1.0")]["before"] == "5"
    assert by_price[("bid", "1.0")]["delta_qty"] == "0"
    assert by_price[("bid", "1.0")]["after"] is None
    assert by_price[("bid", "0.9")]["before"] is None
    assert by_price[("bid", "0.9")]["after"] == "1"
    assert by_price[("ask", "1.1")]["before"] == "4"
    assert by_price[("ask", "1.1")]["after"] == "8"


def test_possible_gap_status(tmp_path: Path) -> None:
    day_dir = tmp_path / "APTUSDT" / "2026-01-06"
    day_dir.mkdir(parents=True)
    lines = [
        _msg(typ="snapshot", ts=1, u=1, seq=1, bids=[["1.0", "1"]], asks=[["1.1", "1"]]),
        _msg(typ="delta", ts=2, u=3, seq=5, bids=[["1.0", "2"]], asks=[]),  # gap u 1->3
    ]
    (day_dir / "2026-01-06_APTUSDT_ob200.data").write_text("\n".join(lines) + "\n")
    result = replay_symbol_day("APTUSDT", "2026-01-06", 2, data_root=tmp_path)
    assert result.sequence_status == SequenceStatus.POSSIBLE_GAP
    assert result.sequence_diagnostics.u_gap_count == 1


def test_delta_before_snapshot_raises() -> None:
    book = OrderBook()
    delta = parse_ob_line(
        _msg(typ="delta", ts=1, u=2, seq=2, bids=[["1.0", "1"]], asks=[]),
        source_line=1,
    )
    with pytest.raises(ReplayError, match="delta before snapshot"):
        book.apply_delta(delta)


def test_replayer_apply_message_counts() -> None:
    replayer = HistoricalBybitReplayer()
    snap = parse_ob_line(
        _msg(typ="snapshot", ts=1, u=1, seq=1, bids=[["1.0", "1"]], asks=[["1.1", "1"]]),
        source_line=1,
    )
    delta = parse_ob_line(
        _msg(typ="delta", ts=2, u=2, seq=2, bids=[["1.0", "2"]], asks=[]),
        source_line=2,
    )
    replayer.apply_message(snap)
    replayer.apply_message(delta)
    assert replayer.diag.deltas_applied == 1
    assert replayer.diag.snapshots_seen == 1
    assert replayer.book.bids[Decimal("1.0")] == Decimal("2")
