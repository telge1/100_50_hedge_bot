"""Live universe from canonical JSON only."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from backtester.short.dashboard.research_charts.live_universe import (
    LIVE_UNIVERSE_PATH,
    load_live_universe_symbols,
)


@dataclass(frozen=True)
class UniverseMeta:
    path: Path
    sha256: str
    mtime_iso: str
    symbol_count: int
    symbols: tuple[str, ...]


def universe_meta(path: Path | None = None) -> UniverseMeta:
    target = path or LIVE_UNIVERSE_PATH
    raw = target.read_bytes()
    symbols = tuple(load_live_universe_symbols(target))
    mtime = datetime.fromtimestamp(target.stat().st_mtime, tz=timezone.utc).isoformat()
    return UniverseMeta(
        path=target,
        sha256=hashlib.sha256(raw).hexdigest(),
        mtime_iso=mtime,
        symbol_count=len(symbols),
        symbols=symbols,
    )


def log_universe_header(meta: UniverseMeta) -> str:
    return (
        f"live_universe path={meta.path} sha256={meta.sha256} "
        f"mtime={meta.mtime_iso} count={meta.symbol_count} symbols={list(meta.symbols)}"
    )


def load_universe(path: Path | None = None) -> list[str]:
    return list(load_live_universe_symbols(path))
