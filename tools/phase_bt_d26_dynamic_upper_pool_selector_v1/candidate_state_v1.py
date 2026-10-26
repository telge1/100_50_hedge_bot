"""Causal state machine for one dynamic upper candidate per close (BT-D26)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from candidate_identity_v1 import (
    ResolvedUpperBand,
    resolve_tracked_upper_band,
    tracked_still_active,
)


STATE_NO_CANDIDATE = "NO_CANDIDATE"
STATE_CANDIDATE_SELECTED = "CANDIDATE_SELECTED"
STATE_CANDIDATE_STILL_VALID = "CANDIDATE_STILL_VALID"
STATE_CANDIDATE_INVALIDATED = "CANDIDATE_INVALIDATED"
STATE_NEXT_CANDIDATE_SELECTED = "NEXT_CANDIDATE_SELECTED"


@dataclass
class BarTransition:
    decision_utc: str
    state: str
    emit_setup: bool
    selection_reason: str | None
    invalidation_reason: str | None = None
    previous: dict[str, Any] | None = None
    current: dict[str, Any] | None = None


@dataclass
class DynamicCandidateStateV1:
    tracked: ResolvedUpperBand | None = None
    transitions: list[BarTransition] = field(default_factory=list)
    unresolved_events: list[dict[str, Any]] = field(default_factory=list)
    _unresolved_logged: set[tuple[str, str]] = field(default_factory=set)

    def _band_dict(self, b: ResolvedUpperBand | None) -> dict[str, Any] | None:
        if b is None:
            return None
        return {
            "pool_lower": b.pool_lower,
            "pool_upper": b.pool_upper,
            "pool_key": b.pool_key,
            "origin_set_key": b.origin_set_key,
            "upper_origin_ids": b.origin_ids,
        }

    def _reconcile_tracked(
        self,
        config: dict[str, Any],
        snapshot: dict[str, Any],
        decision_utc: str,
    ) -> str | None:
        """Refresh bounds from snapshot; return invalidation reason if tracked died."""
        if self.tracked is None:
            return None
        prev = self.tracked
        resolution = resolve_tracked_upper_band(
            config,
            snapshot,
            pool_lo=prev.pool_lower,
            pool_hi=prev.pool_upper,
            origin_ids=prev.origin_ids,
        )
        if resolution.reason == "ORIGIN_SET_AMBIGUOUS_MULTIPLE_BANDS":
            amb_key = (prev.origin_set_key, resolution.reason)
            if amb_key not in self._unresolved_logged:
                self._unresolved_logged.add(amb_key)
                self.unresolved_events.append(
                    {
                        "decision_utc": decision_utc,
                        "reason": resolution.reason,
                        "origin_set_key": prev.origin_set_key,
                        "matches": resolution.ambiguous_matches,
                    }
                )
            if tracked_still_active(
                config,
                snapshot,
                pool_lo=prev.pool_lower,
                pool_hi=prev.pool_upper,
                origin_ids=prev.origin_ids,
            ):
                return None
            self.tracked = None
            return "POOL_ORIGIN_INACTIVE"

        if resolution.ok and resolution.band is not None:
            self.tracked = resolution.band
            return None

        if tracked_still_active(
            config,
            snapshot,
            pool_lo=prev.pool_lower,
            pool_hi=prev.pool_upper,
            origin_ids=prev.origin_ids,
        ):
            return None

        self.tracked = None
        return "POOL_ORIGIN_INACTIVE"

    def process_bar(
        self,
        config: dict[str, Any],
        *,
        decision_utc: str,
        snapshot: dict[str, Any],
        selected: ResolvedUpperBand | None,
    ) -> BarTransition:
        inv_from_reconcile = self._reconcile_tracked(config, snapshot, decision_utc)
        prev = self.tracked

        if selected is None:
            invalidation_reason = inv_from_reconcile
            if prev is not None and invalidation_reason is None:
                if not tracked_still_active(
                    config,
                    snapshot,
                    pool_lo=prev.pool_lower,
                    pool_hi=prev.pool_upper,
                    origin_ids=prev.origin_ids,
                ):
                    invalidation_reason = "POOL_ORIGIN_INACTIVE"
                else:
                    invalidation_reason = "NO_LONGER_STRUCTURALLY_ELIGIBLE"
            if invalidation_reason:
                self.tracked = None
                tr = BarTransition(
                    decision_utc=decision_utc,
                    state=STATE_CANDIDATE_INVALIDATED,
                    emit_setup=False,
                    selection_reason=None,
                    invalidation_reason=invalidation_reason,
                    previous=self._band_dict(prev),
                    current=None,
                )
                self.transitions.append(tr)
                return tr
            tr = BarTransition(
                decision_utc=decision_utc,
                state=STATE_NO_CANDIDATE,
                emit_setup=False,
                selection_reason=None,
                previous=None,
                current=None,
            )
            self.transitions.append(tr)
            return tr

        if prev is None:
            had_invalidation = inv_from_reconcile is not None
            state = STATE_NEXT_CANDIDATE_SELECTED if had_invalidation else STATE_CANDIDATE_SELECTED
            reason = (
                "RESELECT_AFTER_INVALIDATION_BT_D21_LOWEST"
                if had_invalidation
                else "FIRST_DYNAMIC_CANDIDATE_BT_D21_LOWEST"
            )
            self.tracked = selected
            tr = BarTransition(
                decision_utc=decision_utc,
                state=state,
                emit_setup=True,
                selection_reason=reason,
                invalidation_reason=inv_from_reconcile,
                previous=self._band_dict(prev) if had_invalidation else None,
                current=self._band_dict(selected),
            )
            self.transitions.append(tr)
            return tr

        if selected.pool_key == prev.pool_key:
            self.tracked = selected
            tr = BarTransition(
                decision_utc=decision_utc,
                state=STATE_CANDIDATE_STILL_VALID,
                emit_setup=False,
                selection_reason="TRACKED_CANDIDATE_UNCHANGED",
                previous=self._band_dict(prev),
                current=self._band_dict(selected),
            )
            self.transitions.append(tr)
            return tr

        tr = BarTransition(
            decision_utc=decision_utc,
            state=STATE_NEXT_CANDIDATE_SELECTED,
            emit_setup=True,
            selection_reason="LOWER_ELIGIBLE_UPPER_POOL_CHANGED",
            invalidation_reason=inv_from_reconcile or "SUPERSEDED_BY_LOWER_ELIGIBLE_POOL",
            previous=self._band_dict(prev),
            current=self._band_dict(selected),
        )
        self.tracked = selected
        self.transitions.append(tr)
        return tr
