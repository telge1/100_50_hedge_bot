"""Terminal pool ladder state machine (V2 contract)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .models import CandidateState, PoolRecord, ScannerCandidate
from .setups import _setup_id, is_green_reaction, is_red_reaction


@dataclass
class LadderEvent:
    event: str
    at: datetime
    pool_id: str | None = None
    sweep_low: float | None = None
    sweep_high: float | None = None
    reaction_high: float | None = None
    reaction_low: float | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": self.event,
            "at": self.at.isoformat(),
            "pool_id": self.pool_id,
            "sweep_low": self.sweep_low,
            "sweep_high": self.sweep_high,
            "reaction_high": self.reaction_high,
            "reaction_low": self.reaction_low,
            "detail": self.detail,
        }

    def semantic_key(self) -> tuple[Any, ...]:
        return (
            self.event,
            self.pool_id,
            round(self.sweep_low, 8) if self.sweep_low is not None else None,
            round(self.sweep_high, 8) if self.sweep_high is not None else None,
            self.detail,
        )


@dataclass
class TerminalLadderTracker:
    direction: str  # LONG | SHORT
    events: list[LadderEvent] = field(default_factory=list)
    reset_events: list[LadderEvent] = field(default_factory=list)
    duplicate_transitions_suppressed: int = 0
    raw_transition_calls: int = 0
    _last_pool_swept: str | None = None
    _last_reset_sweep_low: float | None = None
    _last_reset_sweep_high: float | None = None
    _seen_semantic_keys: set[tuple[Any, ...]] = field(default_factory=set)

    def record(self, ev: LadderEvent) -> None:
        self.raw_transition_calls += 1
        if ev.event == "LAST_RELEVANT_POOL_SWEPT" and ev.pool_id == self._last_pool_swept:
            self.duplicate_transitions_suppressed += 1
            return
        key = ev.semantic_key()
        if key in self._seen_semantic_keys:
            self.duplicate_transitions_suppressed += 1
            return
        self._seen_semantic_keys.add(key)
        if ev.event == "LAST_RELEVANT_POOL_SWEPT":
            self._last_pool_swept = ev.pool_id
        self.events.append(ev)

    def record_reset(self, *, at: datetime, sweep_low: float | None, sweep_high: float | None, detail: str) -> None:
        self.raw_transition_calls += 1
        if self.direction == "LONG" and sweep_low is not None:
            if self._last_reset_sweep_low is not None and sweep_low >= self._last_reset_sweep_low - 1e-12:
                self.duplicate_transitions_suppressed += 1
                return
            self._last_reset_sweep_low = sweep_low
        if self.direction == "SHORT" and sweep_high is not None:
            if self._last_reset_sweep_high is not None and sweep_high <= self._last_reset_sweep_high + 1e-12:
                self.duplicate_transitions_suppressed += 1
                return
            self._last_reset_sweep_high = sweep_high
        ev = LadderEvent(
            event="REACTION_STATE_RESET",
            at=at,
            sweep_low=sweep_low,
            sweep_high=sweep_high,
            detail=detail,
        )
        key = ev.semantic_key()
        if key in self._seen_semantic_keys:
            self.duplicate_transitions_suppressed += 1
            return
        self._seen_semantic_keys.add(key)
        self.reset_events.append(ev)
        self.events.append(ev)

    def audit_summary(self) -> dict[str, Any]:
        unique_resets = len(self.reset_events)
        return {
            "raw_transition_calls": self.raw_transition_calls,
            "unique_semantic_transitions": len(self.events),
            "duplicate_transitions_suppressed": self.duplicate_transitions_suppressed,
            "unique_resets": unique_resets,
        }


def pools_swept_on_bar(
    pools: list[PoolRecord],
    *,
    direction: str,
    low: float,
    high: float,
    approach_at: datetime,
) -> list[PoolRecord]:
    out: list[PoolRecord] = []
    for p in pools:
        if not p.is_known_before(approach_at):
            continue
        if direction == "LONG" and p.side == "BID" and low <= p.upper_edge:
            out.append(p)
        elif direction == "SHORT" and p.side == "ASK" and high >= p.lower_edge:
            out.append(p)
    return out


def build_terminal_candidate(
    *,
    symbol: str,
    direction: str,
    entry_pool: PoolRecord,
    target_pool: PoolRecord,
    approach_at: datetime,
    sweep_low: float | None,
    sweep_high: float | None,
    terminal_class: str,
) -> ScannerCandidate:
    setup_type = "A_PLUS_TERMINAL_POOL_LONG" if direction == "LONG" else "A_PLUS_TERMINAL_POOL_SHORT"
    sid = _setup_id(
        symbol=symbol,
        setup_type=setup_type,
        entry_pool_id=entry_pool.pool_id,
        approach_at=approach_at,
        confirmation_at=None,
    )
    return ScannerCandidate(
        setup_id=sid,
        setup_type=setup_type,
        symbol=symbol,
        direction=direction,
        state=CandidateState.WAITING_FOR_1M_CONFIRMATION,
        entry_pool=entry_pool,
        target_pool=target_pool,
        approach_at=approach_at,
        sweep_low=sweep_low,
        sweep_high=sweep_high,
        terminal_ladder_state="WAIT_FOR_REACTION",
        htf_context={"terminal_pool_class": terminal_class},
        episode_id=f"{setup_type}:{entry_pool.pool_id}",
    )


def try_terminal_reclaim(
    cand: ScannerCandidate,
    *,
    open_px: float,
    close: float,
    high: float,
    low: float,
) -> bool:
    if cand.direction == "LONG":
        if cand.reaction_high is None and is_green_reaction(open_px, close):
            cand.reaction_high = high
        reclaim = max(cand.reaction_high or high, cand.entry_pool.near_edge)
        cand.reclaim_level = reclaim
        return cand.reaction_high is not None and close > reclaim
    if cand.reaction_low is None and is_red_reaction(open_px, close):
        cand.reaction_low = low
    reclaim = min(cand.reaction_low or low, cand.entry_pool.near_edge)
    cand.reclaim_level = reclaim
    return cand.reaction_low is not None and close < reclaim
