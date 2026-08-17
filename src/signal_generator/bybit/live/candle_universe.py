"""Candle universe vs legacy signal demand.

Candle ingest may include BTCUSDT. The existing (non-Gold) signal worker must not.
``config/live_universe.json`` and demand_symbols keep their BTC blocks.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

SIGNAL_BLOCKED_SYMBOLS = frozenset({"BTCUSDT"})
PUBLIC_TRADE_BLOCKED_SYMBOLS = frozenset({"XAUUSDT"})

DEFAULT_CANDLE_UNIVERSE_PATH = (
    Path(__file__).resolve().parents[4] / "config" / "universe_tradeable_51.json"
)


def default_candle_universe_path() -> Path:
    return DEFAULT_CANDLE_UNIVERSE_PATH


def normalize_symbols(symbols: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in symbols:
        sym = str(item).strip().upper()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        ordered.append(sym)
    return ordered


def load_candle_universe(path: Path | None = None) -> list[str]:
    """Load Gold-51 (or any) candle symbol list. BTCUSDT is allowed here."""
    path = path or default_candle_universe_path()
    raw = json.loads(path.read_text(encoding="utf-8"))
    symbols = normalize_symbols(raw.get("symbols") or [])
    if not symbols:
        raise ValueError(f"candle universe has empty symbols: {path}")
    return symbols


def filter_signal_demand(symbols: Sequence[str]) -> list[str]:
    """Legacy signal worker set: drop BTCUSDT, keep order."""
    return [s for s in normalize_symbols(symbols) if s not in SIGNAL_BLOCKED_SYMBOLS]


def assert_no_signal_btc(symbols: Sequence[str]) -> None:
    blocked = [s for s in normalize_symbols(symbols) if s in SIGNAL_BLOCKED_SYMBOLS]
    if blocked:
        raise ValueError(
            "BTCUSDT must not run in the legacy signal worker; "
            "it is allowed only on the candle universe"
        )


def resolve_universes(
    *,
    candle_symbols: Sequence[str],
    signal_symbols: Sequence[str],
) -> tuple[list[str], list[str]]:
    """Return (candle_universe, signal_demand).

    Candle universe is the union of requested candle symbols and signal demand
    (so chart demand is never dropped from ingest). Signal demand never includes
    SIGNAL_BLOCKED_SYMBOLS.
    """
    candles = normalize_symbols(candle_symbols)
    signals = filter_signal_demand(signal_symbols)
    assert_no_signal_btc(signals)
    extra = [s for s in signals if s not in set(candles)]
    return candles + extra, signals


@dataclass(frozen=True, slots=True)
class CollectorSymbolSets:
    """Separated collector universes. Public trades never reuse the candle list."""

    universe_symbols: tuple[str, ...]
    candle_symbols: tuple[str, ...]
    signal_symbols: tuple[str, ...]
    public_trade_symbols: tuple[str, ...]


def load_universe_symbols(path: Path | None = None) -> tuple[str, ...]:
    """SoT for Gold-51 / public trades: ``universe_tradeable_51.json`` as a new tuple."""
    return tuple(load_candle_universe(path))


def public_trade_symbols_from_universe(
    universe_symbols: Sequence[str],
) -> tuple[str, ...]:
    """Independent public-trade set from the 51-coin SoT, never from candle union."""
    symbols = tuple(normalize_symbols(universe_symbols))
    blocked = [s for s in symbols if s in PUBLIC_TRADE_BLOCKED_SYMBOLS]
    if blocked:
        raise ValueError(
            "XAUUSDT must not be subscribed for public trades: " + ",".join(blocked)
        )
    return symbols


def resolve_collector_symbol_sets(
    *,
    universe_symbols: Sequence[str] | None,
    demand_symbols: Sequence[str],
    enable_public_trades: bool,
) -> CollectorSymbolSets:
    """CLI/runtime symbol split: candle union vs public-trade universe.

    ``universe_symbols`` is the SoT from ``universe_tradeable_51.json``.
    Demand (e.g. XAUUSDT) may join candles/signals only. Public trades copy
    the universe tuple; they are never derived from ``candle_symbols``.
    """
    if universe_symbols is None:
        if enable_public_trades:
            raise ValueError(
                "--enable-public-trades requires --candle-universe "
                "(config/universe_tradeable_51.json); public trades must not "
                "use the candle/demand union"
            )
        signals = tuple(filter_signal_demand(demand_symbols))
        return CollectorSymbolSets(
            universe_symbols=(),
            candle_symbols=signals,
            signal_symbols=signals,
            public_trade_symbols=(),
        )

    universe = tuple(normalize_symbols(universe_symbols))
    candles, signals = resolve_universes(
        candle_symbols=universe,
        signal_symbols=demand_symbols,
    )
    public_trades: tuple[str, ...]
    if enable_public_trades:
        public_trades = public_trade_symbols_from_universe(universe)
    else:
        public_trades = ()
    return CollectorSymbolSets(
        universe_symbols=universe,
        candle_symbols=tuple(candles),
        signal_symbols=tuple(signals),
        public_trade_symbols=public_trades,
    )
