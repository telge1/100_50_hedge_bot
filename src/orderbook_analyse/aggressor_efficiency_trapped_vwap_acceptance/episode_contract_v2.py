"""Acceptance episode / re-arm contract V2 — ex ante, no outcomes.

Episode = lifecycle of one causal trading opportunity on a matched edge
and acceptance direction. No fitted cooldown / no gap grid search.

outcome_used_for_episode_contract = false
outcome_used_for_deduplication = false
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from orderbook_analyse.aggressor_efficiency_flip.timeutil import ensure_utc, iso_z, parse_utc

ACTIVE_ACCEPTED = {"ACCEPTED_ABOVE", "ACCEPTED_BELOW"}
EPISODE_END_STATES = {
    "BREAK_RECLAIMED",
    "FAILED_BREAK",
    "CHOP_AROUND_EDGE",
    "NO_BREAK",
}


@dataclass
class OpenEpisode:
    episode_id_v2: str
    symbol: str
    matched_edge_id: str
    wall_side: str
    acceptance_direction: str  # ACCEPTED_ABOVE | ACCEPTED_BELOW
    started_at: datetime
    last_active_at: datetime
    n_merged_rows: int = 1
    closed: bool = False
    close_reason: Optional[str] = None
    closed_at: Optional[datetime] = None


def stable_hash(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode("utf-8"))
        h.update(b"|")
    return h.hexdigest()[:24]


def event_id_v2(
    *,
    symbol: str,
    matched_edge_id: str,
    decision_ts: datetime,
    direction: str,
) -> str:
    """Causal identity at decision time — no post-decision fields."""
    return "ev2_" + stable_hash(
        symbol.upper(),
        str(matched_edge_id),
        iso_z(ensure_utc(decision_ts)),
        str(direction).upper(),
    )


def episode_id_v2(
    *,
    symbol: str,
    matched_edge_id: str,
    acceptance_direction: str,
    wall_side: str,
    episode_start_ts: datetime,
) -> str:
    return "ep2_" + stable_hash(
        symbol.upper(),
        str(matched_edge_id),
        str(acceptance_direction),
        str(wall_side).upper(),
        iso_z(ensure_utc(episode_start_ts)),
    )


def entry_signal_id_v2(
    *,
    episode_id: str,
    acceptance_first_available_ts_v2: datetime,
) -> str:
    return "es2_" + stable_hash(episode_id, iso_z(ensure_utc(acceptance_first_available_ts_v2)))


def opposite_direction(state: str) -> Optional[str]:
    if state == "ACCEPTED_ABOVE":
        return "ACCEPTED_BELOW"
    if state == "ACCEPTED_BELOW":
        return "ACCEPTED_ABOVE"
    return None


class EpisodeTrackerV2:
    """Track open episodes per (symbol, edge_id, direction)."""

    def __init__(self) -> None:
        self._open: dict[tuple[str, str, str], OpenEpisode] = {}
        self.closed: list[dict[str, Any]] = []
        self.rearms: list[dict[str, Any]] = []
        self.merges: list[dict[str, Any]] = []

    def _key(self, symbol: str, edge_id: str, direction: str) -> tuple[str, str, str]:
        return (symbol.upper(), str(edge_id), str(direction))

    def observe_row(
        self,
        *,
        symbol: str,
        matched_edge_id: str,
        wall_side: str,
        decision_ts: datetime,
        acceptance_state_path: list[dict[str, Any]],
        entry_eligible: bool,
        acceptance_first_available_ts_v2: Optional[datetime],
        earliest_causal_entry_ts_v2: Optional[datetime],
        source_gap_seen: bool,
        old_event_id: str,
        event_id_v2_val: str,
    ) -> dict[str, Any]:
        """Apply one V1/V2 event row against the episode contract.

        Uses the second_checkpoints path to detect end/re-arm causally.
        """
        decision_ts = ensure_utc(decision_ts)
        direction = None
        if entry_eligible and acceptance_first_available_ts_v2 is not None:
            # direction from first eligible accepted checkpoint
            for row in acceptance_state_path:
                if row.get("entry_eligible") and row.get("acceptance_state_at_ts") in ACTIVE_ACCEPTED:
                    direction = row["acceptance_state_at_ts"]
                    break
        if direction is None:
            # non-entry row: may still close episodes via path end states
            close_info = self._scan_closes(
                symbol=symbol,
                matched_edge_id=matched_edge_id,
                wall_side=wall_side,
                path=acceptance_state_path,
                source_gap_seen=source_gap_seen,
            )
            return {
                "old_event_id": old_event_id,
                "event_id_v2": event_id_v2_val,
                "episode_id_v2": None,
                "entry_signal_id_v2": None,
                "entry_eligible_v2": False,
                "episode_action": "NO_ENTRY",
                "migration_class": "FINAL_STATE_ONLY_REMOVED"
                if not entry_eligible
                else "OTHER",
                "close_info": close_info,
            }

        key = self._key(symbol, matched_edge_id, direction)
        start_ts = ensure_utc(acceptance_first_available_ts_v2)
        open_ep = self._open.get(key)

        # Opposite open episode on same edge → close it
        opp = opposite_direction(direction)
        if opp:
            opp_key = self._key(symbol, matched_edge_id, opp)
            if opp_key in self._open:
                self._close(opp_key, at=start_ts, reason="opposite_acceptance")

        # Source gap on path while open → close/suspend
        if source_gap_seen and open_ep is not None and not open_ep.closed:
            self._close(key, at=start_ts, reason="source_gap_breaks_continuity")
            open_ep = None

        # Detect path-based close of prior episode before this entry
        self._scan_closes(
            symbol=symbol,
            matched_edge_id=matched_edge_id,
            wall_side=wall_side,
            path=acceptance_state_path,
            source_gap_seen=source_gap_seen,
        )
        open_ep = self._open.get(key)

        if open_ep is not None and not open_ep.closed:
            # Still active → merge; no new entry
            open_ep.n_merged_rows += 1
            open_ep.last_active_at = start_ts
            self.merges.append(
                {
                    "old_event_id": old_event_id,
                    "event_id_v2": event_id_v2_val,
                    "episode_id_v2": open_ep.episode_id_v2,
                    "reason": "same_edge_direction_still_active",
                }
            )
            return {
                "old_event_id": old_event_id,
                "event_id_v2": event_id_v2_val,
                "episode_id_v2": open_ep.episode_id_v2,
                "entry_signal_id_v2": None,
                "entry_eligible_v2": False,
                "episode_action": "MERGED",
                "migration_class": "MERGED_INTO_EXISTING_EPISODE",
                "acceptance_first_available_ts_v2": iso_z(start_ts),
                "earliest_causal_entry_ts_v2": iso_z(earliest_causal_entry_ts_v2)
                if earliest_causal_entry_ts_v2
                else None,
            }

        # Need re-arm if a prior closed episode existed for this key
        prior_closed = [
            e
            for e in self.closed
            if e["symbol"] == symbol.upper()
            and e["matched_edge_id"] == str(matched_edge_id)
            and e["acceptance_direction"] == direction
        ]
        is_rearm = len(prior_closed) > 0
        # Re-arm requires causal non-active gap already enforced by close + new ACCEPTED
        ep_id = episode_id_v2(
            symbol=symbol,
            matched_edge_id=matched_edge_id,
            acceptance_direction=direction,
            wall_side=wall_side,
            episode_start_ts=start_ts,
        )
        sig = entry_signal_id_v2(episode_id=ep_id, acceptance_first_available_ts_v2=start_ts)
        self._open[key] = OpenEpisode(
            episode_id_v2=ep_id,
            symbol=symbol.upper(),
            matched_edge_id=str(matched_edge_id),
            wall_side=str(wall_side).upper(),
            acceptance_direction=direction,
            started_at=start_ts,
            last_active_at=start_ts,
        )
        action = "NEW_REARM" if is_rearm else "NEW_EPISODE"
        if is_rearm:
            self.rearms.append(
                {
                    "episode_id_v2": ep_id,
                    "old_event_id": old_event_id,
                    "rearm_ts": iso_z(start_ts),
                    "prior_closed_n": len(prior_closed),
                }
            )
        return {
            "old_event_id": old_event_id,
            "event_id_v2": event_id_v2_val,
            "episode_id_v2": ep_id,
            "entry_signal_id_v2": sig,
            "entry_eligible_v2": True,
            "episode_action": action,
            "migration_class": "NEW_REARMED_EPISODE" if is_rearm else "CAUSAL_CHECKPOINT_RECOVERED",
            "acceptance_first_available_ts_v2": iso_z(start_ts),
            "earliest_causal_entry_ts_v2": iso_z(earliest_causal_entry_ts_v2)
            if earliest_causal_entry_ts_v2
            else iso_z(start_ts),
        }

    def _scan_closes(
        self,
        *,
        symbol: str,
        matched_edge_id: str,
        wall_side: str,
        path: list[dict[str, Any]],
        source_gap_seen: bool,
    ) -> list[dict[str, Any]]:
        closed = []
        for row in path:
            st = row.get("acceptance_state_at_ts")
            ts = parse_utc(row["checkpoint_ts"]) if row.get("checkpoint_ts") else None
            if row.get("data_status") == "SOURCE_GAP" or row.get("incomplete_scan"):
                for direction in list(ACTIVE_ACCEPTED):
                    key = self._key(symbol, matched_edge_id, direction)
                    if key in self._open and not self._open[key].closed:
                        self._close(key, at=ts or self._open[key].last_active_at, reason="source_gap")
                        closed.append({"key": key, "reason": "source_gap"})
            if st in EPISODE_END_STATES:
                for direction in list(ACTIVE_ACCEPTED):
                    key = self._key(symbol, matched_edge_id, direction)
                    if key in self._open and not self._open[key].closed:
                        self._close(
                            key,
                            at=ts or self._open[key].last_active_at,
                            reason=f"state_{st}",
                        )
                        closed.append({"key": key, "reason": f"state_{st}"})
        if source_gap_seen:
            for direction in list(ACTIVE_ACCEPTED):
                key = self._key(symbol, matched_edge_id, direction)
                if key in self._open and not self._open[key].closed:
                    self._close(key, at=self._open[key].last_active_at, reason="source_gap_seen")
                    closed.append({"key": key, "reason": "source_gap_seen"})
        return closed

    def _close(self, key: tuple[str, str, str], *, at: datetime, reason: str) -> None:
        ep = self._open.get(key)
        if ep is None or ep.closed:
            return
        ep.closed = True
        ep.close_reason = reason
        ep.closed_at = ensure_utc(at)
        self.closed.append(
            {
                "episode_id_v2": ep.episode_id_v2,
                "symbol": ep.symbol,
                "matched_edge_id": ep.matched_edge_id,
                "wall_side": ep.wall_side,
                "acceptance_direction": ep.acceptance_direction,
                "started_at": iso_z(ep.started_at),
                "closed_at": iso_z(ep.closed_at),
                "close_reason": reason,
                "n_merged_rows": ep.n_merged_rows,
            }
        )
        del self._open[key]

    def flush_open(self) -> None:
        for key in list(self._open.keys()):
            self._close(key, at=self._open[key].last_active_at, reason="end_of_cohort_flush")


EPISODE_CONTRACT_V2 = {
    "version": "FROZEN_HIGH_ACCEPTED_EPISODE_CONTRACT_V2",
    "outcome_used_for_episode_contract": False,
    "outcome_used_for_deduplication": False,
    "no_fitted_cooldown": True,
    "no_gap_grid_search": True,
    "diagnostic_60s_gap_not_used": True,
    "episode_key": [
        "symbol",
        "matched_edge_id",
        "acceptance_direction",
        "wall_side",
    ],
    "episode_start": (
        "first entry_eligible ACCEPTED checkpoint while no open episode "
        "for the same episode_key"
    ),
    "while_active": "repeated rows / checkpoints on same key do not create new entries",
    "episode_end_reasons": [
        "BREAK_RECLAIMED",
        "FAILED_BREAK",
        "CHOP_AROUND_EDGE",
        "NO_BREAK after prior activity",
        "SOURCE_GAP / incomplete_scan breaks continuity",
        "opposite ACCEPTED direction on same edge",
        "end_of_cohort_flush",
    ],
    "rearm": (
        "new entry_eligible ACCEPTED after prior episode for same key was closed; "
        "requires causal non-active interval enforced by close reasons above; "
        "no arbitrary short cooldown"
    ),
    "ids": {
        "event_id_v2": "sha256(symbol|edge_id|decision_ts|direction)[:24]",
        "episode_id_v2": "sha256(symbol|edge_id|direction|wall|episode_start_ts)[:24]",
        "entry_signal_id_v2": "sha256(episode_id|acceptance_first_available_ts_v2)[:24]",
    },
    "chunk_boundaries": "must not create episodes; IDs ignore chunk labels",
}
