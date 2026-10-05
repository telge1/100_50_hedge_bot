"""Per-symbol state + disk persistence for long shadow scanner."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from bot.long_v1_live_scanner.management import ShadowTrade


class Readiness(str, Enum):
    WARMING = "WARMING"
    LIVE_READY = "LIVE_READY"
    WAITING_FOR_DATA = "WAITING_FOR_DATA"
    DATA_MISSING = "DATA_MISSING"


@dataclass
class SymbolState:
    symbol: str
    readiness: Readiness = Readiness.WARMING
    last_processed_15m_close: datetime | None = None
    data_missing_reason: str | None = None

    candles15: Any = None
    bars15: list[dict] = field(default_factory=list)
    by_open: dict = field(default_factory=dict)
    lld_cache: dict = field(default_factory=dict)
    lld_cfg: Any = None

    seen_pool_ids: set[str] = field(default_factory=set)
    open_trades: list[ShadowTrade] = field(default_factory=list)
    closed_trades: list[ShadowTrade] = field(default_factory=list)


@dataclass
class PersistedState:
    version: int = 1
    dry_run_start_utc: str | None = None
    symbols: dict[str, dict] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "version": self.version,
            "dry_run_start_utc": self.dry_run_start_utc,
            "symbols": self.symbols,
        }

    @classmethod
    def from_json(cls, data: dict) -> PersistedState:
        return cls(
            version=int(data.get("version", 1)),
            dry_run_start_utc=data.get("dry_run_start_utc"),
            symbols=dict(data.get("symbols") or {}),
        )


def _dt_to_str(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    return ts.isoformat()


def _str_to_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s)


def symbol_snapshot(st: SymbolState) -> dict:
    open_rows = [t.to_dict() for t in st.open_trades if t.status == "OPEN"]
    return {
        "readiness": st.readiness.value,
        "last_processed_15m_close": _dt_to_str(st.last_processed_15m_close),
        "seen_pool_ids": sorted(st.seen_pool_ids),
        "open_trades": open_rows,
        "closed_trades": [t.to_dict() for t in st.closed_trades[-50:]],
    }


def apply_symbol_snapshot(st: SymbolState, snap: dict) -> None:
    lp = snap.get("last_processed_15m_close")
    st.last_processed_15m_close = _str_to_dt(lp)
    st.seen_pool_ids = set(snap.get("seen_pool_ids") or [])
    st.open_trades = [ShadowTrade.from_dict(x) for x in snap.get("open_trades") or []]
    st.closed_trades = [ShadowTrade.from_dict(x) for x in snap.get("closed_trades") or []]
    try:
        st.readiness = Readiness(snap.get("readiness", Readiness.WARMING.value))
    except ValueError:
        st.readiness = Readiness.WARMING


def load_persisted(path: Path) -> PersistedState:
    if not path.is_file():
        return PersistedState()
    return PersistedState.from_json(json.loads(path.read_text(encoding="utf-8")))


def save_persisted(path: Path, ps: PersistedState, states: dict[str, SymbolState]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    for sym, st in states.items():
        ps.symbols[sym] = symbol_snapshot(st)
    path.write_text(json.dumps(ps.to_json(), indent=2), encoding="utf-8")
