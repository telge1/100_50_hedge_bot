"""Regression: OB200 open-hour u-gap must not leave Walls/Levels empty."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

import research_charts.ob200_walls as ow


def _write_zst_ndjson(path: Path, rows: list[dict]) -> None:
    try:
        import zstandard as zstd
    except ImportError:
        pytest.skip("zstandard required")
    raw = b"".join((json.dumps(r) + "\n").encode() for r in rows)
    path.write_bytes(zstd.ZstdCompressor().compress(raw))


def test_list_open_segments_skips_empty_orphan(tmp_path: Path):
    sym = "BTCUSDT"
    day = tmp_path / sym / "2026" / "09" / "05"
    day.mkdir(parents=True)
    empty = day / f"{sym}_20260905T090000Z_open_ob200_v3.zst.tmp"
    empty.write_bytes(b"")
    good = day / f"{sym}_20260905T100000Z_open_ob200_v3.zst.tmp"
    _write_zst_ndjson(
        good,
        [
            {
                "type": "rotation_checkpoint",
                "ts": 1_700_000_000_000,
                "data": {
                    "b": [["100", "1"]],
                    "a": [["101", "1"]],
                    "u": 10,
                    "seq": 1,
                },
            }
        ],
    )
    opens = ow.list_open_segments([tmp_path], sym)
    assert len(opens) == 1
    assert opens[0].path == good


def test_replay_live_rest_fallback_on_open_u_gap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sym = "BTCUSDT"
    day = tmp_path / sym / "2026" / "09" / "05"
    day.mkdir(parents=True)
    # Closed seed ending just before open
    closed = day / f"{sym}_20260905T090000Z_20260905T100000Z_ob200_v3.zst"
    _write_zst_ndjson(
        closed,
        [
            {
                "type": "snapshot",
                "ts": 1_700_000_000_000,
                "data": {
                    "b": [["100", "2"], ["99", "1"]],
                    "a": [["101", "2"], ["102", "1"]],
                    "u": 10,
                    "seq": 1,
                },
            },
            {
                "type": "delta",
                "ts": 1_700_000_000_100,
                "data": {"b": [["100", "3"]], "a": [], "u": 11, "seq": 2},
            },
        ],
    )
    open_p = day / f"{sym}_20260905T100000Z_open_ob200_v3.zst.tmp"
    # rotation_checkpoint then gapped delta → archive replay invalid
    _write_zst_ndjson(
        open_p,
        [
            {
                "type": "rotation_checkpoint",
                "ts": 1_700_000_100_000,
                "data": {
                    "b": [["100", "3"], ["99", "1"]],
                    "a": [["101", "2"], ["102", "1"]],
                    "u": 12,
                    "seq": 3,
                },
            },
            {
                "type": "delta",
                "ts": 1_700_000_108_700,
                "data": {"b": [["100", "4"]], "a": [], "u": 100, "seq": 4},
            },
        ],
    )

    def fake_rest(symbol: str, *, limit: int = 200, timeout_s: float = 8.0):
        book = ow.MutableBook()
        book.apply_snapshot(
            {
                "b": [["200", "5"]],
                "a": [["201", "5"]],
                "u": 999,
                "seq": 9,
            }
        )
        return {
            "book": book,
            "book_ts": datetime(2026, 9, 5, 10, 15, tzinfo=timezone.utc),
            "segment": f"bybit_rest_orderbook_limit_{limit}",
            "events_applied": 1,
        }

    monkeypatch.setattr(ow, "_fetch_rest_orderbook", fake_rest)
    # Freeze "now" near open tip so live REST path triggers
    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 5, 10, 15, tzinfo=timezone.utc)

    monkeypatch.setattr(ow, "datetime", _FrozenDateTime)

    at = datetime(2026, 9, 5, 10, 15, tzinfo=timezone.utc)
    snap = ow.replay_book_as_of(sym, at, roots=[tmp_path])
    assert snap["source"] == "bybit_rest_orderbook_1000"
    assert snap["best_bid"] == Decimal("200")
    assert snap["best_ask"] == Decimal("201")
    assert snap["bid_levels"] == 1
    assert snap["ask_levels"] == 1
