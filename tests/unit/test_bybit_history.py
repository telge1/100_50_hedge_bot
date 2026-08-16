"""Unit tests for Bybit history parsing, pagination, and normalization."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from signal_generator.bybit.history import (
    BybitApiError,
    BybitHistoryClient,
    HttpxTransport,
    chunk_batches,
    dedupe_by_open_time,
    expected_candle_count,
    filter_half_open,
    millis_to_utc,
    normalize_kline,
    parse_kline_row,
    sort_candles_asc,
    to_millis,
)
from signal_generator.db.candles import Candle1m


def _row(open_ms: int, *, o="1", h="2", l="0.5", c="1.5", v="10", t="20") -> list[str]:
    return [str(open_ms), o, h, l, c, v, t]


class FakeTransport:
    def __init__(self, pages: list[list[list[str]]], *, fail_times: int = 0) -> None:
        self.pages = list(pages)
        self.calls: list[dict[str, Any]] = []
        self.fail_times = fail_times
        self._fails = 0

    def get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(params))
        if self._fails < self.fail_times:
            self._fails += 1
            raise BybitApiError("transient")
        if not self.pages:
            return {"retCode": 0, "retMsg": "OK", "result": {"list": []}}
        return {"retCode": 0, "retMsg": "OK", "result": {"list": self.pages.pop(0)}}


def test_parse_kline_row_decimals_and_utc():
    open_ms = to_millis(datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc))
    parsed = parse_kline_row(_row(open_ms, o="0.07075", v="1105769", t="78257.28465"))
    assert parsed["open_time"] == datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
    assert parsed["close_time"] == datetime(2026, 8, 1, 0, 1, tzinfo=timezone.utc)
    assert parsed["open"] == Decimal("0.07075")
    assert parsed["volume"] == Decimal("1105769")
    assert parsed["turnover"] == Decimal("78257.28465")
    assert isinstance(parsed["open"], Decimal)


def test_utc_conversion_naive_treated_as_utc():
    naive = datetime(2026, 8, 1, 12, 0)
    ms = to_millis(naive)
    assert millis_to_utc(ms) == datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def test_half_open_window_semantics():
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    end = datetime(2026, 8, 1, 0, 3, tzinfo=timezone.utc)
    candles = []
    for i in range(5):
        ot = start + timedelta(minutes=i)
        candles.append(
            Candle1m(
                exchange="bybit",
                symbol="BTCUSDT",
                open_time=ot,
                close_time=ot + timedelta(minutes=1),
                open="1",
                high="1",
                low="1",
                close="1",
                volume="1",
                turnover="1",
                source="bybit_history",
            )
        )
    filtered = filter_half_open(candles, start, end)
    assert [c.open_time for c in filtered] == [
        start,
        start + timedelta(minutes=1),
        start + timedelta(minutes=2),
    ]
    assert expected_candle_count(start, end) == 3


def test_sort_asc_from_reverse_api_order():
    t0 = datetime(2026, 8, 1, tzinfo=timezone.utc)
    rows = [_row(to_millis(t0 + timedelta(minutes=i))) for i in (2, 0, 1)]
    candles = [normalize_kline(r, symbol="DOGEUSDT") for r in rows]
    assert all(c is not None for c in candles)
    sorted_c = sort_candles_asc(candles)  # type: ignore[arg-type]
    assert [c.open_time for c in sorted_c] == [t0, t0 + timedelta(minutes=1), t0 + timedelta(minutes=2)]


def test_pagination_no_overlap_no_gap():
    """Three pages of 2 candles covering 6 minutes without overlap/gap."""
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    end = start + timedelta(minutes=6)
    # newest-first pages as Bybit returns
    page1 = [_row(to_millis(start + timedelta(minutes=i))) for i in (5, 4)]
    page2 = [_row(to_millis(start + timedelta(minutes=i))) for i in (3, 2)]
    page3 = [_row(to_millis(start + timedelta(minutes=i))) for i in (1, 0)]
    transport = FakeTransport([page1, page2, page3])
    client = BybitHistoryClient(transport=transport, request_pause_s=0)
    candles = client.fetch_closed_1m("APTUSDT", start, end, limit=2)
    assert len(candles) == 6
    times = [c.open_time for c in candles]
    assert times == [start + timedelta(minutes=i) for i in range(6)]
    assert len({t for t in times}) == 6
    # end cursor moved backwards each page
    assert transport.calls[0]["end"] == to_millis(end) - 1
    assert transport.calls[1]["end"] == to_millis(start + timedelta(minutes=4)) - 1
    assert transport.calls[2]["end"] == to_millis(start + timedelta(minutes=2)) - 1


def test_duplicate_input_deduped():
    t0 = datetime(2026, 8, 1, tzinfo=timezone.utc)
    c1 = normalize_kline(_row(to_millis(t0), v="1"), symbol="BTCUSDT")
    c2 = normalize_kline(_row(to_millis(t0), v="2"), symbol="BTCUSDT")
    assert c1 and c2
    out = dedupe_by_open_time([c1, c2])
    assert len(out) == 1
    assert out[0].volume == Decimal("2")


def test_candle_close_time_is_open_plus_60s():
    t0 = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
    c = normalize_kline(_row(to_millis(t0)), symbol="BTCUSDT")
    assert c is not None
    assert c.close_time - c.open_time == timedelta(seconds=60)


def test_ohlc_mapping_fields():
    t0 = datetime(2026, 8, 1, tzinfo=timezone.utc)
    c = normalize_kline(
        _row(to_millis(t0), o="10", h="12", l="9", c="11", v="100", t="1100"),
        symbol="BTCUSDT",
    )
    assert c is not None
    assert c.exchange == "bybit"
    assert c.symbol == "BTCUSDT"
    assert c.interval == "1m"
    assert c.source == "bybit_history"
    assert c.is_closed is True
    assert c.open == Decimal("10")
    assert c.high == Decimal("12")
    assert c.low == Decimal("9")
    assert c.close == Decimal("11")
    assert c.volume == Decimal("100")
    assert c.turnover == Decimal("1100")


def test_empty_api_response():
    client = BybitHistoryClient(transport=FakeTransport([]), request_pause_s=0)
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    assert client.fetch_closed_1m("BTCUSDT", start, end) == []


def test_api_error_retries_then_raises():
    transport = HttpxTransport(max_retries=2, backoff_base=0.01)

    class Boom:
        def get(self, *a, **k):
            raise BybitApiError("nope")

        def close(self):
            return None

    transport.client = Boom()  # type: ignore[assignment]
    with pytest.raises(BybitApiError, match="failed after retries"):
        transport.get_json("https://example.invalid", {"a": 1})


def test_unclosed_candle_skipped():
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    c = normalize_kline(
        _row(to_millis(future)),
        symbol="BTCUSDT",
        as_of=datetime.now(timezone.utc),
    )
    assert c is None


def test_batch_insert_chunking():
    t0 = datetime(2026, 8, 1, tzinfo=timezone.utc)
    candles = [
        normalize_kline(_row(to_millis(t0 + timedelta(minutes=i))), symbol="X")
        for i in range(5)
    ]
    assert all(candles)
    batches = chunk_batches(candles, 2)  # type: ignore[arg-type]
    assert [len(b) for b in batches] == [2, 2, 1]
    with pytest.raises(ValueError):
        chunk_batches(candles, 0)  # type: ignore[arg-type]


def test_half_open_excludes_end_boundary_in_fetch():
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    end = start + timedelta(minutes=2)
    # Include a candle at end (should be excluded) and one before start
    rows = [
        _row(to_millis(end)),  # == end → exclude
        _row(to_millis(start + timedelta(minutes=1))),
        _row(to_millis(start)),
        _row(to_millis(start - timedelta(minutes=1))),  # before start → exclude
    ]
    transport = FakeTransport([rows])
    client = BybitHistoryClient(transport=transport, request_pause_s=0)
    candles = client.fetch_closed_1m("BTCUSDT", start, end, limit=10)
    assert [c.open_time for c in candles] == [start, start + timedelta(minutes=1)]
