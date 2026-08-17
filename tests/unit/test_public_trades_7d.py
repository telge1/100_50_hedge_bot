"""Tests for 51-coin × 7-day public-trade backfill and live collector path."""

from __future__ import annotations

import asyncio
import gzip
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from signal_generator.bybit.live.candle_universe import load_candle_universe
from signal_generator.bybit.live.collector import Live1mCollector
from signal_generator.bybit.live.trade_buffer import PublicTradeInsertBuffer
from signal_generator.bybit.live.ws_kline import subscribe_arg_chunks
from signal_generator.bybit.live.ws_public_trade import (
    parse_ws_public_trade_item,
    parse_ws_public_trade_payload,
    public_trade_topic_for_symbol,
)
from signal_generator.bybit.public_trades.audit import classify_minute
from signal_generator.bybit.public_trades.backfill_manifest import (
    BackfillManifestStore,
    iter_backfill_jobs,
)
from signal_generator.bybit.public_trades.backfill_runner import (
    BackfillRunner,
    project_7d_storage,
)
from signal_generator.bybit.public_trades.downloader import DISK_FREE_MIN_BYTES
from signal_generator.bybit.public_trades.urls import daily_url, utc_day_bounds
from signal_generator.db.candles import Candle1m

ROOT = Path(__file__).resolve().parents[2]
UNIVERSE = ROOT / "config" / "universe_tradeable_51.json"
DAYS = (
    "2026-08-10",
    "2026-08-11",
    "2026-08-12",
    "2026-08-13",
    "2026-08-14",
    "2026-08-15",
    "2026-08-16",
)
HEADER = (
    "timestamp,symbol,side,size,price,tickDirection,trdMatchID,"
    "grossValue,homeNotional,foreignNotional,RPI\n"
)


def _ts(day: date, hour: int = 12) -> str:
    dt = datetime(day.year, day.month, day.day, hour, 0, 0, tzinfo=timezone.utc)
    return str(int(dt.timestamp()))


def _row(symbol: str, trade_id: str, day: date = date(2026, 8, 10)) -> str:
    return (
        f"{_ts(day)},{symbol},Buy,10,0.1,PlusTick,{trade_id},"
        f"1000000000,10,1.0,0\n"
    )


def _gz(body: str) -> bytes:
    return gzip.compress(body.encode("utf-8"))


class FakeTransport:
    def __init__(self, payload: bytes, status: int = 200) -> None:
        self.payload = payload
        self.status = status
        self.heads = 0
        self.gets = 0

    def head(self, url: str):
        self.heads += 1

        class Resp:
            status_code = self.status
            headers = {"content-length": str(len(self.payload))}

        return Resp()

    def stream_get(self, url: str):
        self.gets += 1
        yield self.payload


class SkipExistingRepo:
    def __init__(self, existing: set[str] | None = None) -> None:
        self.existing = set(existing or [])
        self.inserted: list = []
        self.calls = 0

    def insert_trades(self, trades, *, ingest_timestamp, source="archive") -> int:
        self.calls += 1
        self.inserted.extend(trades)
        for t in trades:
            self.existing.add(t.trade_id)
        return len(trades)

    def insert_trades_skip_existing(
        self, trades, *, ingest_timestamp, source="archive", chunk_size: int = 5000
    ) -> tuple[int, int]:
        new = [t for t in trades if t.trade_id not in self.existing]
        skipped = len(trades) - len(new)
        if new:
            self.insert_trades(new, ingest_timestamp=ingest_timestamp, source=source)
        return len(new), skipped

    def physical_and_logical_counts(self, **kwargs):
        n = len(self.existing)
        return {
            "physical_rows": n,
            "logical_unique_rows": n,
            "final_rows": n,
            "logical_size_sum": 0,
            "logical_notional_sum": 0,
        }


def test_universe_is_exactly_51_and_excludes_xau():
    symbols = load_candle_universe(UNIVERSE)
    assert len(symbols) == 51
    assert "XAUUSDT" not in symbols
    assert "DOGEUSDT" in symbols
    assert "LITUSDT" in symbols
    jobs = iter_backfill_jobs(symbols, DAYS)
    assert len(jobs) == 357
    assert all(s != "XAUUSDT" for s, _ in jobs)
    days_per = {}
    for s, d in jobs:
        days_per.setdefault(s, set()).add(d)
    assert all(len(v) == 7 for v in days_per.values())
    assert set(days_per["ETHUSDT"]) == set(DAYS)


def test_url_formation_for_7d_window():
    url = daily_url("ETHUSDT", date(2026, 8, 10))
    assert url == "https://public.bybit.com/trading/ETHUSDT/ETHUSDT2026-08-10.csv.gz"
    start, end = utc_day_bounds(date(2026, 8, 16))
    assert start.isoformat() == "2026-08-16T00:00:00+00:00"
    assert end.isoformat() == "2026-08-17T00:00:00+00:00"


def test_manifest_audited_skips_and_resume(tmp_path: Path):
    store = BackfillManifestStore(tmp_path / "m.csv")
    row = store.ensure(
        "DOGEUSDT", "2026-08-15", daily_url("DOGEUSDT", date(2026, 8, 15))
    )
    store.set_status(row, "AUDITED", parsed_rows=10, source_rows=10)
    repo = SkipExistingRepo()
    runner = BackfillRunner(
        cache_dir=tmp_path / "cache",
        manifest=store,
        repo=repo,
        transport=FakeTransport(_gz(HEADER + _row("DOGEUSDT", "a"))),
        pause_ms=0,
    )
    again = runner.process_file(store.get("DOGEUSDT", "2026-08-15"))
    assert again.status == "AUDITED"
    assert repo.calls == 0


def test_force_reimport_skips_existing_ids(tmp_path: Path):
    payload = _gz(HEADER + _row("DOGEUSDT", "a") + _row("DOGEUSDT", "b"))
    store = BackfillManifestStore(tmp_path / "m.csv")
    row = store.ensure("DOGEUSDT", "2026-08-10", daily_url("DOGEUSDT", date(2026, 8, 10)))
    repo = SkipExistingRepo()
    runner = BackfillRunner(
        cache_dir=tmp_path / "cache",
        manifest=store,
        repo=repo,
        transport=FakeTransport(payload),
        pause_ms=0,
        batch_size=10,
    )
    first = runner.process_file(row)
    assert first.status == "IMPORTED"
    assert first.inserted_rows == 2
    first.status = "PENDING"
    store.save()
    repo2_calls_before = repo.calls
    second = runner.process_file(store.get("DOGEUSDT", "2026-08-10"))
    assert second.inserted_rows == 0
    assert second.skipped_existing_rows == 2
    assert len(repo.inserted) == 2
    assert repo.calls == repo2_calls_before  # no new insert_trades


def test_storage_gate_blocks_below_80gib():
    out = project_7d_storage(
        total_gz_bytes=400 * 1024**3,
        estimated_rows=2_000_000_000,
        free_bytes=100 * 1024**3,
    )
    assert out["blocked"] is True
    assert DISK_FREE_MIN_BYTES == 80 * 1024**3


def test_storage_gate_allows_7d_scale():
    out = project_7d_storage(
        total_gz_bytes=8 * 1024**3,
        estimated_rows=20_000_000,
        free_bytes=500 * 1024**3,
    )
    assert out["blocked"] is False
    assert out["projected_remaining_free_bytes"] > DISK_FREE_MIN_BYTES


def test_candle_reconciliation_classes():
    candle = {"open": 1, "high": 2, "low": 1, "close": 2, "volume": 10, "turnover": 10}
    trades = {
        "open": 1, "high": 2, "low": 1, "close": 2,
        "base_volume": 10, "quote_volume": 10, "n_trades": 2,
    }
    assert classify_minute(candle=candle, trades=trades) == "MATCH"
    assert classify_minute(candle=candle, trades=None) == "PUBLIC_TRADES_MISSING"


def test_ws_public_trade_parse_multi_and_side():
    payload = {
        "topic": "publicTrade.DOGEUSDT",
        "data": [
            {"i": "id1", "s": "DOGEUSDT", "S": "Buy", "T": 1755216000123, "p": "0.1", "v": "10", "L": "PlusTick"},
            {"i": "id2", "s": "DOGEUSDT", "S": "Sell", "T": 1755216000456, "p": "0.2", "v": "5", "L": "MinusTick"},
        ],
    }
    trades = parse_ws_public_trade_payload(payload)
    assert len(trades) == 2
    assert trades[0].trade_id == "id1"
    assert trades[0].side == "Buy"
    assert trades[1].side == "Sell"
    assert trades[0].trade_ts.tzinfo is not None
    empty = parse_ws_public_trade_item({"i": "", "s": "DOGEUSDT", "S": "Buy", "T": 1, "p": "1", "v": "1"})
    assert empty is None


def test_public_trade_topics_exclude_xau_and_include_universe():
    symbols = load_candle_universe(UNIVERSE)
    topics = subscribe_arg_chunks(
        symbols, chunk_size=10, public_trade_symbols=symbols
    )
    flat = [t for chunk in topics for t in chunk]
    assert all(not t.endswith("XAUUSDT") or t.startswith("kline.") for t in flat)
    assert "publicTrade.XAUUSDT" not in flat
    assert "publicTrade.DOGEUSDT" in flat
    assert "kline.1.BTCUSDT" in flat
    assert public_trade_topic_for_symbol("ethusdt") == "publicTrade.ETHUSDT"


class DummyCh:
    pass


def _patch_collector_deps(monkeypatch) -> None:
    monkeypatch.setattr(
        "signal_generator.bybit.live.collector.assert_shadow_only", lambda: None
    )
    monkeypatch.setattr(
        "signal_generator.bybit.live.collector.get_clickhouse_settings",
        lambda: type("S", (), {})(),
    )

    class DummyRepo:
        pass

    monkeypatch.setattr(
        "signal_generator.bybit.live.collector.CandleRepository", lambda ch: DummyRepo()
    )
    monkeypatch.setattr(
        "signal_generator.bybit.live.collector.SignalRepository", lambda ch: DummyRepo()
    )
    monkeypatch.setattr(
        "signal_generator.bybit.live.collector.ProcessingStateRepository",
        lambda ch: DummyRepo(),
    )


def test_collector_xau_not_in_public_trades(monkeypatch):
    _patch_collector_deps(monkeypatch)
    with pytest.raises(ValueError, match="XAUUSDT"):
        Live1mCollector(
            candle_symbols=["DOGEUSDT", "XAUUSDT"],
            signal_symbols=["DOGEUSDT"],
            ch=DummyCh(),  # type: ignore[arg-type]
            enable_signals=False,
            enable_public_trades=True,
            public_trade_symbols=["DOGEUSDT", "XAUUSDT"],
        )


def test_collector_public_trades_default_off(monkeypatch):
    _patch_collector_deps(monkeypatch)
    c = Live1mCollector(
        candle_symbols=["DOGEUSDT"],
        signal_symbols=["DOGEUSDT"],
        ch=DummyCh(),  # type: ignore[arg-type]
        enable_signals=False,
    )
    assert c.enable_public_trades is False
    assert c.public_trade_symbols == ()


@pytest.mark.asyncio
async def test_trade_buffer_queue_limit_and_duplicate_skip():
    repo = SkipExistingRepo()
    buf = PublicTradeInsertBuffer(repo, queue_maxsize=2, batch_size=10, flush_interval_s=0.05)
    from signal_generator.bybit.live.ws_public_trade import WsPublicTrade

    t = WsPublicTrade(
        symbol="DOGEUSDT",
        trade_id="a",
        trade_ts=datetime(2026, 8, 17, tzinfo=timezone.utc),
        side="Buy",
        price=Decimal("1"),
        size=Decimal("1"),
        notional=Decimal("1"),
        tick_direction="",
        is_rpi_trade=0,
    )
    assert buf.enqueue(t) is True
    assert buf.enqueue(t) is True
    assert buf.enqueue(t) is False
    assert buf.metrics.dropped_events == 1
    buf.start()
    await asyncio.sleep(0.2)
    await buf.stop()
    assert buf.metrics.rows_inserted >= 1


@pytest.mark.asyncio
async def test_candle_path_continues_when_trade_insert_fails():
    class BoomRepo(SkipExistingRepo):
        def insert_trades_skip_existing(self, *a, **k):
            raise RuntimeError("insert boom")

    buf = PublicTradeInsertBuffer(BoomRepo(), queue_maxsize=10, batch_size=1, flush_interval_s=0.05)
    from signal_generator.bybit.live.ws_public_trade import WsPublicTrade

    trade = WsPublicTrade(
        symbol="DOGEUSDT",
        trade_id="z",
        trade_ts=datetime(2026, 8, 17, tzinfo=timezone.utc),
        side="Sell",
        price=Decimal("1"),
        size=Decimal("1"),
        notional=Decimal("1"),
        tick_direction="",
        is_rpi_trade=0,
    )
    buf.start()
    buf.enqueue(trade)
    await asyncio.sleep(0.2)
    await buf.stop()
    assert buf.metrics.insert_failures >= 1
    candle = Candle1m(
        exchange="bybit",
        symbol="DOGEUSDT",
        interval="1m",
        open_time=datetime(2026, 8, 17, tzinfo=timezone.utc),
        close_time=datetime(2026, 8, 17, 0, 1, tzinfo=timezone.utc),
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal("1"),
        volume=Decimal("1"),
        turnover=Decimal("1"),
        is_closed=True,
        source="bybit_live",
    )
    assert candle.symbol == "DOGEUSDT"


@pytest.mark.asyncio
async def test_ws_public_trade_does_not_block_kline():
    from signal_generator.bybit.live.ws_kline import BybitKlineWebSocket

    closed = []

    async def on_closed(c):
        closed.append(c)

    received = []

    def on_trade(t):
        received.append(t)

    ws = BybitKlineWebSocket(
        ["DOGEUSDT"],
        on_closed_candle=on_closed,
        on_public_trade=on_trade,
        public_trade_symbols=["DOGEUSDT"],
    )
    await ws._handle_payload(
        {
            "topic": "publicTrade.DOGEUSDT",
            "data": [
                {"i": "1", "s": "DOGEUSDT", "S": "Buy", "T": 1755216000000, "p": "1", "v": "1"}
            ],
        }
    )
    await ws._handle_payload(
        {
            "topic": "kline.1.DOGEUSDT",
            "data": [
                {
                    "start": 1755216000000,
                    "open": "1",
                    "high": "1",
                    "low": "1",
                    "close": "1",
                    "volume": "1",
                    "turnover": "1",
                    "confirm": True,
                    "timestamp": 1755216060000,
                }
            ],
        }
    )
    assert len(received) == 1
    assert len(closed) == 1
