"""Persistent paper state / trades / events."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd


@dataclass
class OpenPositionState:
    trade_id: str
    cluster_id: int
    side: str
    entry_time: str
    entry_price: float
    entry_conf: str
    first_signal_tf: str
    plan_tf: str
    tp_pct: float
    sl_pct: float
    max_hold_min: int
    entry_i: int
    end_i: int
    cursor: int
    n_upgrades: int
    upgrade_tfs: list[str] = field(default_factory=list)
    conflict_seen: bool = False
    is_tier_a_entry: bool = True
    is_q4_entry: bool = True
    signal_time: str | None = None
    validation_mode: str = "REPLAY"
    tp_pct_initial: float = 0.0
    sl_pct_initial: float = 0.0


@dataclass
class SymbolState:
    symbol: str
    status: str = "FLAT"  # FLAT | OPEN_LONG | OPEN_SHORT
    last_processed_1m_ts: str | None = None
    last_signal_available_at: dict[str, str | None] = field(default_factory=dict)
    open_position: OpenPositionState | None = None
    entered_cluster_ids: list[int] = field(default_factory=list)
    n_signals_seen: int = 0
    n_entries: int = 0
    n_upgrades: int = 0
    n_closed: int = 0
    forward_coverage: str = "UNKNOWN"


@dataclass
class PaperState:
    strategy_version: str
    paper_start: str
    runner_created_at: str | None = None
    forward_capture_start: str | None = None
    conflict_exit_enabled: bool = True
    fee_pct: float = 0.11
    mode_last_run: str | None = None
    symbols: dict[str, SymbolState] = field(default_factory=dict)
    parity_status: str | None = None
    next_trade_seq: int = 1

    def ensure_symbol(self, symbol: str) -> SymbolState:
        if symbol not in self.symbols:
            self.symbols[symbol] = SymbolState(
                symbol=symbol,
                last_signal_available_at={tf: None for tf in ("15m", "30m", "1h", "4h")},
            )
        return self.symbols[symbol]


def state_to_dict(st: PaperState) -> dict[str, Any]:
    out: dict[str, Any] = {
        "strategy_version": st.strategy_version,
        "paper_start": st.paper_start,
        "runner_created_at": st.runner_created_at,
        "forward_capture_start": st.forward_capture_start,
        "conflict_exit_enabled": st.conflict_exit_enabled,
        "fee_pct": st.fee_pct,
        "mode_last_run": st.mode_last_run,
        "parity_status": st.parity_status,
        "next_trade_seq": st.next_trade_seq,
        "symbols": {},
    }
    for sym, ss in st.symbols.items():
        out["symbols"][sym] = asdict(ss)
    return out


def state_from_dict(d: dict[str, Any]) -> PaperState:
    st = PaperState(
        strategy_version=d["strategy_version"],
        paper_start=d["paper_start"],
        runner_created_at=d.get("runner_created_at"),
        forward_capture_start=d.get("forward_capture_start"),
        conflict_exit_enabled=bool(d.get("conflict_exit_enabled", True)),
        fee_pct=float(d.get("fee_pct", 0.11)),
        mode_last_run=d.get("mode_last_run"),
        parity_status=d.get("parity_status"),
        next_trade_seq=int(d.get("next_trade_seq", 1)),
    )
    for sym, sd in (d.get("symbols") or {}).items():
        op = sd.get("open_position")
        open_pos = OpenPositionState(**op) if op else None
        st.symbols[sym] = SymbolState(
            symbol=sym,
            status=sd.get("status", "FLAT"),
            last_processed_1m_ts=sd.get("last_processed_1m_ts"),
            last_signal_available_at=sd.get("last_signal_available_at")
            or {tf: None for tf in ("15m", "30m", "1h", "4h")},
            open_position=open_pos,
            entered_cluster_ids=list(sd.get("entered_cluster_ids") or []),
            n_signals_seen=int(sd.get("n_signals_seen", 0)),
            n_entries=int(sd.get("n_entries", 0)),
            n_upgrades=int(sd.get("n_upgrades", 0)),
            n_closed=int(sd.get("n_closed", 0)),
            forward_coverage=sd.get("forward_coverage", "UNKNOWN"),
        )
    return st


def load_state(path: Path) -> PaperState | None:
    if not path.exists():
        return None
    return state_from_dict(json.loads(path.read_text(encoding="utf-8")))


def save_state(path: Path, st: PaperState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state_to_dict(st), indent=2, default=str), encoding="utf-8")


def append_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, default=str) + "\n")


def load_trades(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame(columns=TRADE_COLUMNS)
    try:
        df = pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame(columns=TRADE_COLUMNS)
    if df.empty:
        return pd.DataFrame(columns=TRADE_COLUMNS)
    return df


def save_trades(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if df is None or df.empty:
        pd.DataFrame(columns=TRADE_COLUMNS).to_csv(path, index=False)
        return
    df.to_csv(path, index=False)


TRADE_COLUMNS = [
    "trade_id",
    "symbol",
    "side",
    "cluster_id",
    "first_signal_tf",
    "highest_tf_reached",
    "signal_time",
    "entry_time",
    "entry_price",
    "exit_time",
    "exit_price",
    "exit_reason",
    "tp_pct_initial",
    "sl_pct_initial",
    "tp_pct_final",
    "sl_pct_final",
    "upgrade_count",
    "upgrade_sequence",
    "holding_minutes",
    "gross_return_pct",
    "fee_pct",
    "net_return_pct",
    "same_bar_ambiguous",
    "validation_mode",
]
