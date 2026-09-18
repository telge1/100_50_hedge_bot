"""Pilot parameters for ema_trend_live_analyzer_v1."""

from __future__ import annotations

from dataclasses import dataclass

from . import (
    FIRST_CANDIDATE_ELIGIBLE_SECONDS,
    FIRST_EARLY_EVIDENCE_SECONDS,
    FULL_OB_OBSERVATION_SECONDS,
    OUTCOME_HORIZON_SECONDS,
)


@dataclass(frozen=True)
class PilotParams:
    observation_seconds: float = float(FULL_OB_OBSERVATION_SECONDS)
    outcome_horizon_seconds: float = float(OUTCOME_HORIZON_SECONDS)
    first_early_evidence_seconds: float = float(FIRST_EARLY_EVIDENCE_SECONDS)
    first_candidate_eligible_seconds: float = float(FIRST_CANDIDATE_ELIGIBLE_SECONDS)
    band_ticks: int = 5
    max_distance_bps: float = 15.0
    max_wall_candidates_per_side: int = 8
    # Symbol-dependent; must be supplied per case — no BTC 0.1 hardcode in call sites.
    default_tick_size: float | None = None
    pt_poll_interval_sec: float = 0.25
    fanout_poll_batch: int = 256
    fanout_heartbeat_sec: float = 10.0
    archive_duration_seconds: float = float(FULL_OB_OBSERVATION_SECONDS)


DEFAULT_BAND_TICKS = 5
DEFAULT_MAX_DISTANCE_BPS = 15.0
DEFAULT_MAX_WALL_CANDIDATES_PER_SIDE = 8

# Research outcome metrics (NOT trading thresholds) — documented for 6h evaluator.
REACH_PCT = 0.41
COST_TAKER_PCT = 0.08
COST_MAKER_PCT = 0.12
MFE_MAE_UNIT = "percent"
NEUTRAL_OUTCOME_SEPARATE_FROM_CANDIDATE = True
