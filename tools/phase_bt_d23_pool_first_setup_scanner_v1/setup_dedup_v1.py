"""Deterministic setup deduplication (BT-D23) — not trading pool_signal_hold."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from pool_identity_v1 import parse_origin_id_set


def origin_set_identity_key(origin_ids: Any) -> str:
    parts = sorted(parse_origin_id_set(origin_ids))
    if not parts:
        return ""
    return "|".join(parts)


@dataclass(frozen=True)
class SetupFingerprint:
    """Reserved for band-bound tracking; dedup key is origin_set_identity_key."""

    pool_lower: float
    pool_upper: float


@dataclass
class DedupDecision:
    emit: bool
    reason: str | None = None


class SetupDedupTrackerV1:
    """
    Suppress repeated PLANNED setups for the same causal origin-set identity.

    Emit once per origin-set episode while `origin_set_v1` remains active in density.

    New setup when:
    - FIRST_STRUCTURAL_ELIGIBILITY — origin set not yet emitted in current episode
    - ORIGIN_SET_RETURNED — origin set became inactive in snapshot and later active again

    Suppressed:
    - UNCHANGED_ORIGIN_SET — same origin set still active; no re-listing each 5m close
    """

    def __init__(self) -> None:
        self._active_episodes: set[str] = set()

    def evaluate(self, origin_key: str, _fingerprint: SetupFingerprint) -> DedupDecision:
        if not origin_key:
            return DedupDecision(False, "MISSING_ORIGIN_IDENTITY")
        if origin_key in self._active_episodes:
            return DedupDecision(False, "UNCHANGED_ORIGIN_SET")
        return DedupDecision(True, "FIRST_STRUCTURAL_ELIGIBILITY")

    def commit(self, origin_key: str, _fingerprint: SetupFingerprint) -> None:
        if origin_key:
            self._active_episodes.add(origin_key)

    def end_bar_origin_inactive(
        self,
        config: dict,
        snapshot: dict,
        *,
        pool_still_active_for_pending: Callable[..., bool],
    ) -> None:
        for origin_key in list(self._active_episodes):
            if not origin_key:
                continue
            active = pool_still_active_for_pending(
                config,
                pool_lo=0.0,
                pool_hi=0.0,
                origin_ids=origin_key,
                snapshot=snapshot,
            )
            if not active:
                self._active_episodes.discard(origin_key)
