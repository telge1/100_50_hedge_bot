"""Load and validate ``config/live_universe.json`` (sole live symbol source)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from signal_generator.bybit.history import HttpTransport, HttpxTransport
from signal_generator.bybit.universe import (
    INSTRUMENTS_URL,
    filter_active_linear_usdt_perpetuals,
)

DEFAULT_LIVE_UNIVERSE_PATH = Path(__file__).resolve().parents[4] / "config" / "live_universe.json"


def default_live_universe_path() -> Path:
    return DEFAULT_LIVE_UNIVERSE_PATH


@dataclass(slots=True)
class LiveUniverse:
    exchange: str
    category: str
    interval: str
    symbols: list[str]
    updated_at: str | None = None
    notes: str | None = None
    path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "exchange": self.exchange,
            "category": self.category,
            "interval": self.interval,
            "symbols": list(self.symbols),
            "updated_at": self.updated_at,
            "notes": self.notes,
            "path": str(self.path) if self.path else None,
        }


@dataclass(slots=True)
class SymbolValidationResult:
    symbol: str
    ok: bool
    reason: str = ""


def load_live_universe(path: Path | None = None) -> LiveUniverse:
    """Parse live universe JSON. Symbols are uppercased; BTCUSDT is rejected."""
    path = path or default_live_universe_path()
    raw = json.loads(path.read_text(encoding="utf-8"))
    symbols = [str(s).upper() for s in (raw.get("symbols") or [])]
    if not symbols:
        raise ValueError(f"live universe has empty symbols: {path}")
    if "BTCUSDT" in symbols:
        raise ValueError(
            f"BTCUSDT must not appear in live universe ({path}); remove it explicitly"
        )
    # Deduplicate preserving order
    seen: set[str] = set()
    ordered: list[str] = []
    for s in symbols:
        if s not in seen:
            seen.add(s)
            ordered.append(s)
    return LiveUniverse(
        exchange=str(raw.get("exchange") or "bybit"),
        category=str(raw.get("category") or "linear"),
        interval=str(raw.get("interval") or "1m"),
        symbols=ordered,
        updated_at=raw.get("updated_at"),
        notes=raw.get("notes"),
        path=path,
    )


def _paginate_instruments(transport: HttpTransport) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"category": "linear", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        payload = transport.get_json(INSTRUMENTS_URL, params)
        result = payload.get("result") or {}
        rows = result.get("list") or []
        out.extend(rows)
        cursor = result.get("nextPageCursor") or None
        if not cursor:
            break
    return out


def validate_live_symbols(
    symbols: Sequence[str],
    *,
    transport: HttpTransport | None = None,
    tradable: set[str] | None = None,
) -> list[SymbolValidationResult]:
    """Validate each symbol is an active linear USDT perpetual.

    Invalid symbols are reported as ERROR results; callers keep running valid ones.
    """
    symbols = [s.upper() for s in symbols]
    if tradable is None:
        transport = transport or HttpxTransport()
        instruments = _paginate_instruments(transport)
        filtered = filter_active_linear_usdt_perpetuals(instruments)
        tradable = {str(x["symbol"]).upper() for x in filtered}

    out: list[SymbolValidationResult] = []
    for sym in symbols:
        if sym == "BTCUSDT":
            out.append(
                SymbolValidationResult(sym, False, "BTCUSDT excluded from live universe policy")
            )
            continue
        if sym not in tradable:
            out.append(
                SymbolValidationResult(
                    sym, False, "not an active Bybit linear USDT perpetual"
                )
            )
            continue
        out.append(SymbolValidationResult(sym, True, "ok"))
    return out


def partition_valid_symbols(
    results: Sequence[SymbolValidationResult],
) -> tuple[list[str], list[SymbolValidationResult]]:
    valid = [r.symbol for r in results if r.ok]
    invalid = [r for r in results if not r.ok]
    return valid, invalid
