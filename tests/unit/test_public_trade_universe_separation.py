"""Regression: public-trade universe must not reuse the candle/demand union.

The failed live restart passed candle_syms (51 universe + demand XAUUSDT)
into Live1mCollector(public_trade_symbols=...). The XAUUSDT guard is correct
and must stay; the service must pass the 51-coin universe instead.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from signal_generator.bybit.live.candle_universe import load_universe_symbols
from signal_generator.bybit.live.collector import Live1mCollector
from signal_generator.bybit.live.ws_kline import (
    SUBSCRIBE_CHUNK_SIZE,
    subscribe_arg_chunks,
)

ROOT = Path(__file__).resolve().parents[2]
GOLD_51 = ROOT / "config" / "universe_tradeable_51.json"
SERVICE = ROOT / "scripts" / "run_live_collector_service.py"


def _load_service():
    spec = importlib.util.spec_from_file_location("run_live_collector_service", SERVICE)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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


class DummyCh:
    pass


def _failed_runtime_cli_args(mod):
    return mod.build_parser().parse_args(
        [
            "--candle-universe",
            str(GOLD_51),
            "--enable-public-trades",
        ]
    )


def test_cli_runtime_path_separates_candle_and_public_trade_sets(monkeypatch):
    """Exact failed restart path: 51-coin universe + demand XAU + flag on."""
    _patch_collector_deps(monkeypatch)
    mod = _load_service()
    args = _failed_runtime_cli_args(mod)
    universe = load_universe_symbols(GOLD_51)
    sets = mod.collector_symbol_sets_from_cli(args, ["XAUUSDT"])
    kwargs = mod.live_collector_symbol_kwargs(
        sets, enable_public_trades=args.enable_public_trades
    )

    assert args.enable_public_trades is True
    assert len(universe) == 51
    assert "XAUUSDT" not in universe
    assert len(sets.candle_symbols) == 52
    assert "XAUUSDT" in sets.candle_symbols
    assert kwargs["public_trade_symbols"] is not kwargs["candle_symbols"]
    assert tuple(kwargs["public_trade_symbols"]) == tuple(universe)
    assert "XAUUSDT" not in kwargs["public_trade_symbols"]
    assert len(kwargs["public_trade_symbols"]) == 51
    kwargs["candle_symbols"].append("MUTATEDUSDT")
    assert "MUTATEDUSDT" not in kwargs["public_trade_symbols"]
    kwargs["candle_symbols"].pop()

    collector = Live1mCollector(
        ch=DummyCh(),  # type: ignore[arg-type]
        enable_signals=False,
        **kwargs,
    )
    collector._assert_public_trade_subscription_set()
    assert collector.candle_symbols == list(sets.candle_symbols)
    assert "XAUUSDT" in collector.candle_symbols
    assert "XAUUSDT" in collector.symbols
    assert collector.public_trade_symbols == tuple(universe)
    assert collector.public_trade_symbols is not collector.candle_symbols
    assert collector.public_trade_symbols != tuple(collector.candle_symbols)
    ws_public = collector.public_trade_symbols or None
    assert ws_public == collector.public_trade_symbols
    assert ws_public is not collector.symbols

    chunks = subscribe_arg_chunks(
        collector.symbols,
        chunk_size=SUBSCRIBE_CHUNK_SIZE,
        public_trade_symbols=collector.public_trade_symbols,
    )
    again = subscribe_arg_chunks(
        collector.symbols,
        chunk_size=SUBSCRIBE_CHUNK_SIZE,
        public_trade_symbols=collector.public_trade_symbols,
    )
    assert chunks == again
    flat = [topic for chunk in chunks for topic in chunk]
    assert "kline.1.XAUUSDT" in flat
    assert "publicTrade.XAUUSDT" not in flat
    public_topics = [t for t in flat if t.startswith("publicTrade.")]
    kline_topics = [t for t in flat if t.startswith("kline.")]
    assert len(kline_topics) == 52
    assert len(public_topics) == 51
    assert len(set(public_topics)) == 51
    assert {t.split(".", 1)[1] for t in public_topics} == set(universe)


def test_old_candle_union_handoff_still_raises_xau_guard(monkeypatch):
    _patch_collector_deps(monkeypatch)
    mod = _load_service()
    args = _failed_runtime_cli_args(mod)
    sets = mod.collector_symbol_sets_from_cli(args, ["XAUUSDT"])
    kwargs = mod.live_collector_symbol_kwargs(
        sets, enable_public_trades=True
    )
    kwargs["public_trade_symbols"] = kwargs["candle_symbols"]
    with pytest.raises(ValueError, match="XAUUSDT must not be subscribed for public trades"):
        Live1mCollector(
            ch=DummyCh(),  # type: ignore[arg-type]
            enable_signals=False,
            **kwargs,
        )


def test_enable_public_trades_requires_explicit_universe(monkeypatch):
    _patch_collector_deps(monkeypatch)
    with pytest.raises(ValueError, match="public_trade_symbols is required"):
        Live1mCollector(
            candle_symbols=["DOGEUSDT", "XAUUSDT"],
            signal_symbols=["XAUUSDT"],
            ch=DummyCh(),  # type: ignore[arg-type]
            enable_signals=False,
            enable_public_trades=True,
        )


def test_flag_off_cli_runtime_stays_candle_only(monkeypatch):
    _patch_collector_deps(monkeypatch)
    mod = _load_service()
    args = mod.build_parser().parse_args(["--candle-universe", str(GOLD_51)])
    sets = mod.collector_symbol_sets_from_cli(args, ["XAUUSDT"])
    kwargs = mod.live_collector_symbol_kwargs(
        sets, enable_public_trades=args.enable_public_trades
    )
    assert args.enable_public_trades is False
    assert kwargs["public_trade_symbols"] is None
    assert len(kwargs["candle_symbols"]) == 52
    assert "XAUUSDT" in kwargs["candle_symbols"]
    collector = Live1mCollector(
        ch=DummyCh(),  # type: ignore[arg-type]
        enable_signals=False,
        **kwargs,
    )
    assert collector.enable_public_trades is False
    assert collector.public_trade_symbols == ()
    assert "XAUUSDT" in collector.candle_symbols
    collector._assert_public_trade_subscription_set()
    chunks = subscribe_arg_chunks(collector.symbols, chunk_size=SUBSCRIBE_CHUNK_SIZE)
    flat = [topic for chunk in chunks for topic in chunk]
    assert "kline.1.XAUUSDT" in flat
    assert all(not t.startswith("publicTrade.") for t in flat)


def test_cli_rejects_enable_without_candle_universe():
    mod = _load_service()
    args = mod.build_parser().parse_args(["--enable-public-trades"])
    with pytest.raises(ValueError, match="candle-universe"):
        mod.collector_symbol_sets_from_cli(args, ["XAUUSDT"])


def test_public_trade_set_matches_config_exactly():
    mod = _load_service()
    args = _failed_runtime_cli_args(mod)
    universe = load_universe_symbols(GOLD_51)
    sets = mod.collector_symbol_sets_from_cli(
        args, ["XAUUSDT", "FAKEGOLDUSDT", "ADAUSDT"]
    )
    assert tuple(sets.public_trade_symbols) == tuple(universe)
    assert len(sets.public_trade_symbols) == 51
    assert len(set(sets.public_trade_symbols)) == 51
    assert "XAUUSDT" not in sets.public_trade_symbols
    assert "FAKEGOLDUSDT" not in sets.public_trade_symbols
    assert sets.public_trade_symbols.count("ADAUSDT") == 1
    assert "XAUUSDT" in sets.candle_symbols
    assert "FAKEGOLDUSDT" in sets.candle_symbols
    assert len(sets.candle_symbols) == 53
