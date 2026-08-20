"""Tests for OB200 NDJSON file orderbook event source."""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from orderbook_analyse.ob_data_source.ndjson_parse import (
    Ob200ParseError,
    parse_ob200_line,
    parse_ob200_obj,
)
from orderbook_analyse.ob_data_source.ob200_file_source import (
    Ob200FileOrderBookEventSource,
    Ob200FileSourceError,
)
from orderbook_analyse.orderbook_replay import OrderBookReplayer

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "ob200"


def _ts(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def _write_day(root: Path, day: str, symbol: str, lines: list[str]) -> Path:
    d = root / f"{day}_{symbol}_ob200.data"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{day}_{symbol}_ob200.data"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


SNAP = (
    '{"topic":"orderbook.200.APTUSDT","type":"snapshot","ts":1784851201000,'
    '"cts":1784851200900,"data":{"s":"APTUSDT","b":[["0.6174","10"],["0.6173","5"]],'
    '"a":[["0.6175","8"],["0.6176","2"]],"u":100,"seq":1000}}'
)
DELTA_BID = (
    '{"topic":"orderbook.200.APTUSDT","type":"delta","ts":1784851202000,'
    '"cts":1784851201900,"data":{"s":"APTUSDT","b":[["0.6174","12"]],"a":[],'
    '"u":101,"seq":1001}}'
)
DELTA_ASK = (
    '{"topic":"orderbook.200.APTUSDT","type":"delta","ts":1784851203000,'
    '"cts":1784851202900,"data":{"s":"APTUSDT","b":[],"a":[["0.6175","0"]],'
    '"u":102,"seq":1002}}'
)
DELTA_ZERO = (
    '{"topic":"orderbook.200.APTUSDT","type":"delta","ts":1784851204000,'
    '"cts":1784851203900,"data":{"s":"APTUSDT","b":[["0.6173","0"]],"a":[],'
    '"u":103,"seq":1003}}'
)


def test_snapshot_expands_to_book_level_events() -> None:
    msg = parse_ob200_line(SNAP, expected_symbol="APTUSDT")
    events = msg.to_book_level_events()
    assert msg.message_type == "snapshot"
    assert len(events) == 4
    bids = [e for e in events if e.side == "bid"]
    asks = [e for e in events if e.side == "ask"]
    assert bids[0].price == Decimal("0.6174")
    assert bids[0].level_index == 0
    assert bids[1].level_index == 1
    assert asks[0].price == Decimal("0.6175")
    assert events[0].update_id == 100
    assert events[0].cross_sequence == 1000


def test_bid_delta() -> None:
    msg = parse_ob200_line(DELTA_BID)
    events = msg.to_book_level_events()
    assert len(events) == 1
    assert events[0].side == "bid"
    assert events[0].quantity == Decimal("12")


def test_ask_delta_and_qty_zero_event() -> None:
    msg = parse_ob200_line(DELTA_ASK)
    events = msg.to_book_level_events()
    assert len(events) == 1
    assert events[0].side == "ask"
    assert events[0].quantity == Decimal("0")


def test_qty_zero_delivered() -> None:
    msg = parse_ob200_line(DELTA_ZERO)
    ev = msg.to_book_level_events()[0]
    assert ev.quantity == Decimal("0")
    assert ev.price == Decimal("0.6173")


def test_snapshot_replaces_book_via_existing_replayer() -> None:
    r = OrderBookReplayer()
    s1 = parse_ob200_line(SNAP)
    r.apply_message("snapshot", s1.update_id, s1.cross_sequence, s1.exchange_ts, s1.to_book_level_events())
    assert r.book.best_bid() == Decimal("0.6174")
    snap2 = parse_ob200_obj(
        {
            "type": "snapshot",
            "ts": 1784851205000,
            "cts": 1784851204900,
            "data": {
                "s": "APTUSDT",
                "b": [["0.50", "1"]],
                "a": [["0.51", "1"]],
                "u": 200,
                "seq": 2000,
            },
        }
    )
    r.apply_message(
        "snapshot",
        snap2.update_id,
        snap2.cross_sequence,
        snap2.exchange_ts,
        snap2.to_book_level_events(),
    )
    assert r.book.best_bid() == Decimal("0.50")
    assert Decimal("0.6174") not in r.book.bids


def test_level_index() -> None:
    msg = parse_ob200_line(SNAP)
    bids = [e for e in msg.to_book_level_events() if e.side == "bid"]
    assert [e.level_index for e in bids] == [0, 1]


def test_symbol_mismatch_hard_fail() -> None:
    with pytest.raises(Ob200ParseError, match="symbol mismatch"):
        parse_ob200_line(SNAP, expected_symbol="BTCUSDT")


def test_invalid_json_hard_fail(tmp_path: Path) -> None:
    _write_day(tmp_path, "2026-07-24", "APTUSDT", [SNAP, "{not-json", DELTA_BID])
    src = Ob200FileOrderBookEventSource(tmp_path, strict=True)
    start = _ts(1784851201000)
    end = _ts(1784851203000)
    with pytest.raises(Ob200FileSourceError, match="invalid JSON"):
        list(src.iter_events("APTUSDT", start, end))


def test_delta_before_snapshot_hard_fail(tmp_path: Path) -> None:
    _write_day(tmp_path, "2026-07-24", "APTUSDT", [DELTA_BID])
    src = Ob200FileOrderBookEventSource(tmp_path)
    start = _ts(1784851201000)
    end = _ts(1784851203000)
    with pytest.raises(Ob200FileSourceError, match="delta before snapshot"):
        src.find_bootstrap("APTUSDT", start, end)


def test_boundary_dedupe_once(tmp_path: Path) -> None:
    # day1 ends with snap; day2 starts with identical snap then delta
    day1 = [SNAP, DELTA_BID]
    # identical boundary snapshot as SNAP but later reused at day boundary with same key
    boundary = SNAP  # same key
    day2_delta = (
        '{"topic":"orderbook.200.APTUSDT","type":"delta","ts":1784851202000,'
        '"cts":1784851201900,"data":{"s":"APTUSDT","b":[["0.6172","1"]],"a":[],'
        '"u":101,"seq":1001}}'
    )
    # For two-day window need contiguous dates; use same messages with day folders
    # day1: snap u=100, delta u=101
    # day2: duplicate snap u=100 + continue? After dedupe of snap, next must be u=102
    # Better: day1 ends with snap S; day2 starts with S then delta u=S.u+1
    snap_end = (
        '{"type":"snapshot","ts":1784937600000,"cts":1784937600000,'
        '"data":{"s":"APTUSDT","b":[["0.6","1"]],"a":[["0.61","1"]],"u":150,"seq":2000}}'
    )
    d1 = [
        SNAP,
        DELTA_BID,
        (
            '{"type":"delta","ts":1784851203000,"cts":1784851202900,'
            '"data":{"s":"APTUSDT","b":[],"a":[["0.6176","1"]],"u":102,"seq":1002}}'
        ),
        # ... jump via reseat snapshot at end of day
        snap_end,
    ]
    d2 = [
        snap_end,  # duplicate boundary
        (
            '{"type":"delta","ts":1784937601000,"cts":1784937600900,'
            '"data":{"s":"APTUSDT","b":[["0.6","2"]],"a":[],"u":151,"seq":2001}}'
        ),
    ]
    _write_day(tmp_path, "2026-07-24", "APTUSDT", d1)
    _write_day(tmp_path, "2026-07-25", "APTUSDT", d2)
    src = Ob200FileOrderBookEventSource(tmp_path, boundary_dedupe=True)
    start = _ts(1784851201000)
    end = _ts(1784937601000)
    counter = [0]
    msgs = list(src.iter_messages("APTUSDT", start, end, dedupe_counter=counter))
    assert counter[0] == 1
    snaps = [m for m in msgs if m.message_type == "snapshot" and m.update_id == 150]
    assert len(snaps) == 1


def test_same_u_seq_different_type_not_deduped(tmp_path: Path) -> None:
    # delta then snapshot with same u/seq — both kept
    lines = [
        SNAP,
        DELTA_BID,
        (
            '{"type":"delta","ts":1784851202500,"cts":1784851202400,'
            '"data":{"s":"APTUSDT","b":[["0.6171","1"]],"a":[],"u":102,"seq":1002}}'
        ),
        (
            '{"type":"snapshot","ts":1784851202600,"cts":1784851202500,'
            '"data":{"s":"APTUSDT","b":[["0.6170","1"]],"a":[["0.6171","1"]],'
            '"u":102,"seq":1002}}'
        ),
    ]
    _write_day(tmp_path, "2026-07-24", "APTUSDT", lines)
    src = Ob200FileOrderBookEventSource(tmp_path)
    start = _ts(1784851201000)
    end = _ts(1784851203000)
    msgs = list(src.iter_messages("APTUSDT", start, end))
    assert sum(1 for m in msgs if m.update_id == 102) == 2
    types = [m.message_type for m in msgs if m.update_id == 102]
    assert types == ["delta", "snapshot"]


def test_ts_ms_to_utc() -> None:
    msg = parse_ob200_line(SNAP)
    assert msg.exchange_ts == datetime(2026, 7, 24, 0, 0, 1, tzinfo=timezone.utc)
    assert msg.matching_engine_ts is not None


def test_warmup_and_no_emit_before_start(tmp_path: Path) -> None:
    _write_day(tmp_path, "2026-07-24", "APTUSDT", [SNAP, DELTA_BID, DELTA_ASK, DELTA_ZERO])
    src = Ob200FileOrderBookEventSource(tmp_path)
    start = _ts(1784851202500)
    end = _ts(1784851204000)
    boot = src.find_bootstrap("APTUSDT", start, end)
    assert boot.exchange_ts < start
    events = list(src.iter_events("APTUSDT", start, end))
    assert events[0].exchange_ts < start  # warmup present
    # causal sample emission: only when sample_ts >= start
    from orderbook_analyse.orderbook_replay import group_messages

    r = OrderBookReplayer()
    samples: list[datetime] = []
    next_sample = start
    for mt, u, seq, ts, levels in group_messages(events):
        while next_sample <= end and ts > next_sample:
            if r.book.has_snapshot and next_sample >= start:
                samples.append(next_sample)
            next_sample = datetime.fromtimestamp(
                next_sample.timestamp() + 1.0, tz=timezone.utc
            )
        r.apply_message(mt, u, seq, ts, levels)
    assert samples
    assert all(s >= start for s in samples)


def test_missing_start_day_invalid(tmp_path: Path) -> None:
    _write_day(tmp_path, "2026-07-25", "APTUSDT", [SNAP])
    src = Ob200FileOrderBookEventSource(tmp_path)
    cov = src.coverage(
        "APTUSDT",
        datetime(2026, 7, 24, tzinfo=timezone.utc),
        datetime(2026, 7, 24, 1, tzinfo=timezone.utc),
    )
    assert cov.valid is False
    assert "missing day" in cov.reason


def test_missing_snapshot_invalid(tmp_path: Path) -> None:
    # file with only deltas — find_bootstrap fails
    _write_day(tmp_path, "2026-07-24", "APTUSDT", [DELTA_BID])
    src = Ob200FileOrderBookEventSource(tmp_path)
    cov = src.coverage("APTUSDT", _ts(1784851201000), _ts(1784851203000))
    assert cov.valid is False


def test_u_backwards_invalid(tmp_path: Path) -> None:
    bad = (
        '{"type":"delta","ts":1784851202000,"cts":1784851201900,'
        '"data":{"s":"APTUSDT","b":[["0.1","1"]],"a":[],"u":99,"seq":1001}}'
    )
    _write_day(tmp_path, "2026-07-24", "APTUSDT", [SNAP, bad])
    src = Ob200FileOrderBookEventSource(tmp_path)
    with pytest.raises(Ob200FileSourceError, match="update_id gap"):
        list(src.iter_messages("APTUSDT", _ts(1784851201000), _ts(1784851203000)))


def test_seq_backwards_invalid(tmp_path: Path) -> None:
    bad = (
        '{"type":"delta","ts":1784851202000,"cts":1784851201900,'
        '"data":{"s":"APTUSDT","b":[["0.1","1"]],"a":[],"u":101,"seq":900}}'
    )
    _write_day(tmp_path, "2026-07-24", "APTUSDT", [SNAP, bad])
    src = Ob200FileOrderBookEventSource(tmp_path)
    with pytest.raises(Ob200FileSourceError, match="cross_sequence backwards"):
        list(src.iter_messages("APTUSDT", _ts(1784851201000), _ts(1784851203000)))


def test_streaming_uses_iterator_not_read_text() -> None:
    src = inspect.getsource(Ob200FileOrderBookEventSource.iter_messages)
    assert "read_text" not in src
    assert "readlines" not in src
    assert "open(" in src
    assert "yield" in src


def test_real_fixture_smoke() -> None:
    assert FIXTURE_ROOT.exists()
    src = Ob200FileOrderBookEventSource(FIXTURE_ROOT)
    start = datetime(2026, 7, 24, 0, 0, 5, tzinfo=timezone.utc)
    end = datetime(2026, 7, 24, 0, 0, 30, tzinfo=timezone.utc)
    cov = src.coverage("APTUSDT", start, end)
    assert cov.valid is True
    r = OrderBookReplayer()
    n_msg = 0
    for msg in src.iter_messages("APTUSDT", start, end):
        r.apply_message(
            msg.message_type,
            msg.update_id,
            msg.cross_sequence,
            msg.exchange_ts,
            msg.to_book_level_events(),
        )
        n_msg += 1
    assert n_msg >= 2
    assert r.book.has_snapshot
    assert r.book.best_bid() is not None
    assert r.book.best_ask() is not None
