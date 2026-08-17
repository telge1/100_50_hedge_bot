"""Candle universe vs signal demand (Gold-51 ingest, no Gold signals)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from signal_generator.bybit.live.candle_universe import (
    filter_signal_demand,
    load_candle_universe,
    load_universe_symbols,
    public_trade_symbols_from_universe,
    resolve_collector_symbol_sets,
    resolve_universes,
)
from signal_generator.bybit.live.collector import Live1mCollector
from signal_generator.bybit.live.demand_symbols import DemandSymbolStore
from signal_generator.bybit.live.live_universe import load_live_universe
from signal_generator.bybit.live.ws_kline import (
    SUBSCRIBE_CHUNK_SIZE,
    subscribe_arg_chunks,
    ws_tick_to_candle,
    parse_ws_kline_item,
)

ROOT = Path(__file__).resolve().parents[2]
GOLD_51 = ROOT / "config" / "universe_tradeable_51.json"


def test_gold_51_candle_universe_includes_btc():
    symbols = load_candle_universe(GOLD_51)
    assert len(symbols) == 51
    assert "BTCUSDT" in symbols
    assert "LITUSDT" in symbols
    assert "ADAUSDT" in symbols


def test_btc_blocked_from_signal_demand_and_live_universe():
    uni = load_live_universe(ROOT / "config" / "live_universe.json")
    assert "BTCUSDT" not in uni.symbols
    candles, signals = resolve_universes(
        candle_symbols=load_candle_universe(GOLD_51),
        signal_symbols=["ADAUSDT", "BTCUSDT", "ETHUSDT"],
    )
    assert "BTCUSDT" in candles
    assert "BTCUSDT" not in signals
    assert signals == ["ADAUSDT", "ETHUSDT"]
    assert set(signals).issubset(set(candles))


def test_union_keeps_extra_demand_on_candle_universe():
    candles, signals = resolve_universes(
        candle_symbols=["ETHUSDT", "BTCUSDT"],
        signal_symbols=["ADAUSDT"],
    )
    assert candles == ["ETHUSDT", "BTCUSDT", "ADAUSDT"]
    assert signals == ["ADAUSDT"]
    assert "BTCUSDT" not in signals


def test_legacy_collector_symbols_still_reject_btc():
    with pytest.raises(ValueError, match="BTCUSDT"):
        Live1mCollector(symbols=["APTUSDT", "BTCUSDT"], ch=MagicMock())


def test_candle_path_allows_btc_but_not_in_signal_worker():
    col = Live1mCollector(
        candle_symbols=["BTCUSDT", "ADAUSDT"],
        signal_symbols=["ADAUSDT", "BTCUSDT"],
        ch=MagicMock(),
        enable_signals=False,
    )
    assert col.candle_symbols == ["BTCUSDT", "ADAUSDT"]
    assert col.symbols == col.candle_symbols
    assert col.signal_symbols == ["ADAUSDT"]
    snap = col.health.to_dict()
    assert snap["candle_symbols"] == ["BTCUSDT", "ADAUSDT"]
    assert snap["signal_symbols"] == ["ADAUSDT"]
    assert "BTCUSDT" not in snap["signal_symbols"]


def test_demand_file_ada_unchanged_by_universe_helpers(tmp_path: Path):
    path = tmp_path / "demand_symbols.json"
    store = DemandSymbolStore(path=path)
    store.write(["ADAUSDT"], reason="ensure_symbol")
    before = path.read_text(encoding="utf-8")
    load_candle_universe(GOLD_51)
    resolve_universes(
        candle_symbols=load_candle_universe(GOLD_51),
        signal_symbols=store.read(),
    )
    assert path.read_text(encoding="utf-8") == before
    assert store.read() == ["ADAUSDT"]
    with pytest.raises(ValueError, match="signal demand"):
        store.write_singleton("BTCUSDT")


def test_subscribe_chunks_cover_all_51_including_btc():
    symbols = load_candle_universe(GOLD_51)
    chunks = subscribe_arg_chunks(symbols, chunk_size=SUBSCRIBE_CHUNK_SIZE)
    flat = [t for chunk in chunks for t in chunk]
    assert len(chunks) == 6
    assert all(len(c) <= 10 for c in chunks)
    assert f"kline.1.BTCUSDT" in flat
    assert len(flat) == 51
    assert len(set(flat)) == 51


@pytest.mark.asyncio
async def test_reconnect_resubscribes_all_candle_chunks():
    from signal_generator.bybit.live.ws_kline import BybitKlineWebSocket

    sent: list[list[str]] = []

    class FakeWs:
        async def send(self, raw: str) -> None:
            payload = json.loads(raw)
            sent.append(list(payload.get("args") or []))

    symbols = [f"S{i}USDT" for i in range(21)]
    ws = BybitKlineWebSocket(symbols, subscribe_chunk_size=10)
    ws._subscribe_chunks = subscribe_arg_chunks(symbols, chunk_size=10)
    ws._subscribe_chunk_index = 0
    fake = FakeWs()
    ws._ws_conn = fake
    await ws._send_next_subscribe_chunk(fake)
    await ws._handle_payload({"op": "subscribe", "success": True})
    await ws._handle_payload({"op": "subscribe", "success": True})
    assert [len(x) for x in sent] == [10, 10, 1]
    sent.clear()
    ws._subscribe_chunk_index = 0
    await ws._send_next_subscribe_chunk(fake)
    await ws._handle_payload({"op": "subscribe", "success": True})
    await ws._handle_payload({"op": "subscribe", "success": True})
    assert [len(x) for x in sent] == [10, 10, 1]


def test_confirm_false_still_not_a_candle():
    item = {
        "start": 1_704_067_200_000,
        "end": 1_704_067_259_999,
        "interval": "1",
        "open": "1",
        "high": "2",
        "low": "0.5",
        "close": "1.5",
        "volume": "10",
        "turnover": "15",
        "confirm": False,
        "timestamp": 1_704_067_210_000,
    }
    assert ws_tick_to_candle(parse_ws_kline_item(item, symbol="BTCUSDT")) is None


def test_filter_signal_demand_drops_btc_only():
    assert filter_signal_demand(["btcusdt", "ADAUSDT", "ADAUSDT"]) == ["ADAUSDT"]


def test_universe_symbols_are_exactly_51_without_xau():
    universe = load_universe_symbols(GOLD_51)
    assert len(universe) == 51
    assert len(set(universe)) == 51
    assert "XAUUSDT" not in universe
    pts = public_trade_symbols_from_universe(universe)
    assert pts == universe
    assert pts is not universe


def test_resolve_collector_sets_keeps_xau_on_candles_only():
    universe = load_universe_symbols(GOLD_51)
    sets = resolve_collector_symbol_sets(
        universe_symbols=universe,
        demand_symbols=["XAUUSDT"],
        enable_public_trades=True,
    )
    assert len(sets.universe_symbols) == 51
    assert len(sets.candle_symbols) == 52
    assert len(sets.public_trade_symbols) == 51
    assert sets.public_trade_symbols == sets.universe_symbols
    assert sets.public_trade_symbols is not sets.candle_symbols
    assert "XAUUSDT" in sets.candle_symbols
    assert "XAUUSDT" not in sets.public_trade_symbols
    assert set(sets.public_trade_symbols) == set(universe)
    assert set(sets.public_trade_symbols).issubset(set(sets.candle_symbols))


def test_demand_already_in_universe_does_not_duplicate_public_trades():
    universe = load_universe_symbols(GOLD_51)
    sets = resolve_collector_symbol_sets(
        universe_symbols=universe,
        demand_symbols=["ADAUSDT"],
        enable_public_trades=True,
    )
    assert len(sets.candle_symbols) == 51
    assert len(sets.public_trade_symbols) == 51
    assert sets.public_trade_symbols.count("ADAUSDT") == 1
    assert len(set(sets.public_trade_symbols)) == 51


def test_extra_demand_symbols_stay_off_public_trades():
    universe = load_universe_symbols(GOLD_51)
    sets = resolve_collector_symbol_sets(
        universe_symbols=universe,
        demand_symbols=["XAUUSDT", "FAKEGOLDUSDT"],
        enable_public_trades=True,
    )
    assert "XAUUSDT" in sets.candle_symbols
    assert "FAKEGOLDUSDT" in sets.candle_symbols
    assert len(sets.candle_symbols) == 53
    assert "XAUUSDT" not in sets.public_trade_symbols
    assert "FAKEGOLDUSDT" not in sets.public_trade_symbols
    assert len(sets.public_trade_symbols) == 51


def test_public_trades_flag_off_leaves_empty_public_set():
    universe = load_universe_symbols(GOLD_51)
    sets = resolve_collector_symbol_sets(
        universe_symbols=universe,
        demand_symbols=["XAUUSDT"],
        enable_public_trades=False,
    )
    assert len(sets.candle_symbols) == 52
    assert "XAUUSDT" in sets.candle_symbols
    assert sets.public_trade_symbols == ()


def test_enable_public_trades_without_universe_is_rejected():
    with pytest.raises(ValueError, match="candle-universe"):
        resolve_collector_symbol_sets(
            universe_symbols=None,
            demand_symbols=["XAUUSDT"],
            enable_public_trades=True,
        )


def test_xau_in_universe_source_is_rejected_for_public_trades():
    with pytest.raises(ValueError, match="XAUUSDT"):
        public_trade_symbols_from_universe(["DOGEUSDT", "XAUUSDT"])
