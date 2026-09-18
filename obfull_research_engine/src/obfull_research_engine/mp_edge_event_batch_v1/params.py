"""Frozen V2 semantics + batch parameters. No silent profit tuning."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from obfull_research_engine.mp_edge_event_study_v2.params import (
    DEFAULT_ABSORB_CONFIRMATION_BPS,
    DEFAULT_ABSORB_CONFIRMATION_MAX_S,
    DEFAULT_APPROACH_LOOKBACK_S,
    DEFAULT_APPROACH_ORIGIN_DISTANCE_BPS,
    DEFAULT_ALLOW_RETEST_FROM_BREAK_SIDE,
    DEFAULT_CONFLUENCE_TOLERANCE_BPS,
    DEFAULT_FAILED_BREAK_HORIZON_S,
    DEFAULT_MIN_EVENT_SEPARATION_S,
    DEFAULT_MIN_PENETRATION_BPS,
    DEFAULT_OUTCOME_HORIZONS_S,
    DEFAULT_RECLAIM_HOLD_S,
    DEFAULT_RECLAIM_TOLERANCE_BPS,
    DEFAULT_REQUIRE_CORRECT_APPROACH,
    DEFAULT_RESET_DISTANCE_BPS,
    DEFAULT_TOUCH_TOLERANCE_BPS,
    DEFAULT_TPSL_PAIRS,
    DEFAULT_TRUE_BREAK_ACCEPTANCE_S,
    DEFAULT_TRUE_BREAK_CONTINUATION_BPS,
    MP_EDGE_DEFINITION,
    MP_KIND,
    PILOT_TIMEFRAMES,
    PROFILE_SOURCE,
    SILVER_DATABASE_DEFAULT,
    PilotParamsV2,
    build_params_v2,
)

# Session UTC definitions (documented)
SESSION_UTC = {
    "Asia": (0, 8),      # [00:00, 08:00)
    "Europe": (8, 13),   # [08:00, 13:00)
    "US": (13, 24),      # [13:00, 24:00)
}

COST_SCENARIOS_BPS: tuple[float, ...] = (0.0, 8.0, 12.0)
EPISODE_COOLDOWNS_S: tuple[int, ...] = (300, 900, 1800)
MIN_EPOCH_DURATION_S = 7200.0  # 2h — matches prep continuous-window filter
EST_SECONDS_PER_MID_ROW = 50.0 / 385622.0  # from V2 pilot


FROZEN_V2_SEMANTICS: dict[str, Any] = {
    "package": "mp_edge_event_study_v2",
    "mp_kind": MP_KIND,
    "mp_edge_definition": MP_EDGE_DEFINITION,
    "profile_source": PROFILE_SOURCE,
    "timeframes": list(PILOT_TIMEFRAMES),
    "touch_tolerance_bps": DEFAULT_TOUCH_TOLERANCE_BPS,
    "confluence_tolerance_bps": DEFAULT_CONFLUENCE_TOLERANCE_BPS,
    "min_event_separation_s": DEFAULT_MIN_EVENT_SEPARATION_S,
    "reset_distance_bps": DEFAULT_RESET_DISTANCE_BPS,
    "min_penetration_bps": DEFAULT_MIN_PENETRATION_BPS,
    "reclaim_hold_s": DEFAULT_RECLAIM_HOLD_S,
    "reclaim_tolerance_bps": DEFAULT_RECLAIM_TOLERANCE_BPS,
    "failed_break_horizon_s": DEFAULT_FAILED_BREAK_HORIZON_S,
    "true_break_acceptance_s": DEFAULT_TRUE_BREAK_ACCEPTANCE_S,
    "true_break_continuation_bps": DEFAULT_TRUE_BREAK_CONTINUATION_BPS,
    "approach_lookback_s": DEFAULT_APPROACH_LOOKBACK_S,
    "approach_origin_distance_bps": DEFAULT_APPROACH_ORIGIN_DISTANCE_BPS,
    "require_correct_approach": DEFAULT_REQUIRE_CORRECT_APPROACH,
    "allow_retest_from_break_side": DEFAULT_ALLOW_RETEST_FROM_BREAK_SIDE,
    "absorb_confirmation_bps": DEFAULT_ABSORB_CONFIRMATION_BPS,
    "absorb_confirmation_max_s": DEFAULT_ABSORB_CONFIRMATION_MAX_S,
    "outcome_horizons_s": list(DEFAULT_OUTCOME_HORIZONS_S),
    "tpsl_pairs": [{"tp_bps": a, "sl_bps": b} for a, b in DEFAULT_TPSL_PAIRS],
    "trade_side_rules": {
        "UPPER+ABSORB": "SHORT",
        "UPPER+FAILED_BREAK": "SHORT",
        "UPPER+TRUE_BREAK": "LONG",
        "LOWER+ABSORB": "LONG",
        "LOWER+FAILED_BREAK": "LONG",
        "LOWER+TRUE_BREAK": "SHORT",
    },
}


def semantics_hash(payload: dict[str, Any] | None = None) -> str:
    data = payload if payload is not None else FROZEN_V2_SEMANTICS
    blob = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


SEMANTICS_HASH = semantics_hash()


@dataclass(frozen=True)
class BatchParams:
    symbol: str
    output_dir: Path
    silver_database: str
    cost_scenarios_bps: tuple[float, ...]
    episode_cooldowns_s: tuple[int, ...]
    runtime_limit_s: float
    check_only: bool
    resume: bool
    window_id: str | None
    max_windows: int | None
    min_epoch_duration_s: float
    semantics_hash: str = SEMANTICS_HASH

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["output_dir"] = str(self.output_dir)
        d["cost_scenarios_bps"] = list(self.cost_scenarios_bps)
        d["episode_cooldowns_s"] = list(self.episode_cooldowns_s)
        d["frozen_v2_semantics"] = FROZEN_V2_SEMANTICS
        d["session_utc"] = SESSION_UTC
        return d


def build_batch_params(
    *,
    symbol: str = "BTCUSDT",
    output_dir: str | Path,
    silver_database: str = SILVER_DATABASE_DEFAULT,
    cost_scenarios_bps: str | tuple[float, ...] = COST_SCENARIOS_BPS,
    episode_cooldowns_s: str | tuple[int, ...] = EPISODE_COOLDOWNS_S,
    runtime_limit_s: float = 3600.0,
    check_only: bool = False,
    resume: bool = False,
    window_id: str | None = None,
    max_windows: int | None = None,
    min_epoch_duration_s: float = MIN_EPOCH_DURATION_S,
) -> BatchParams:
    if isinstance(cost_scenarios_bps, str):
        costs = tuple(float(x.strip()) for x in cost_scenarios_bps.split(",") if x.strip())
    else:
        costs = tuple(float(x) for x in cost_scenarios_bps)
    if isinstance(episode_cooldowns_s, str):
        cds = tuple(int(float(x.strip())) for x in episode_cooldowns_s.split(",") if x.strip())
    else:
        cds = tuple(int(x) for x in episode_cooldowns_s)
    return BatchParams(
        symbol=str(symbol).upper(),
        output_dir=Path(output_dir),
        silver_database=str(silver_database),
        cost_scenarios_bps=costs,
        episode_cooldowns_s=cds,
        runtime_limit_s=float(runtime_limit_s),
        check_only=bool(check_only),
        resume=bool(resume),
        window_id=window_id,
        max_windows=int(max_windows) if max_windows is not None else None,
        min_epoch_duration_s=float(min_epoch_duration_s),
        semantics_hash=SEMANTICS_HASH,
    )


def v2_params_for_window(
    *,
    symbol: str,
    start_z: str,
    end_z: str,
    output_dir: Path,
    silver_database: str,
) -> PilotParamsV2:
    """Build PilotParamsV2 with frozen semantics (no silent overrides)."""
    return build_params_v2(
        symbol=symbol,
        start=start_z,
        end=end_z,
        output_dir=output_dir,
        silver_database=silver_database,
        touch_tolerance_bps=DEFAULT_TOUCH_TOLERANCE_BPS,
        confluence_tolerance_bps=DEFAULT_CONFLUENCE_TOLERANCE_BPS,
        min_event_separation_s=DEFAULT_MIN_EVENT_SEPARATION_S,
        reset_distance_bps=DEFAULT_RESET_DISTANCE_BPS,
        min_penetration_bps=DEFAULT_MIN_PENETRATION_BPS,
        reclaim_hold_s=DEFAULT_RECLAIM_HOLD_S,
        reclaim_tolerance_bps=DEFAULT_RECLAIM_TOLERANCE_BPS,
        failed_break_horizon_s=DEFAULT_FAILED_BREAK_HORIZON_S,
        true_break_acceptance_s=DEFAULT_TRUE_BREAK_ACCEPTANCE_S,
        true_break_continuation_bps=DEFAULT_TRUE_BREAK_CONTINUATION_BPS,
        approach_lookback_s=DEFAULT_APPROACH_LOOKBACK_S,
        approach_origin_distance_bps=DEFAULT_APPROACH_ORIGIN_DISTANCE_BPS,
        require_correct_approach=DEFAULT_REQUIRE_CORRECT_APPROACH,
        allow_retest_from_break_side=DEFAULT_ALLOW_RETEST_FROM_BREAK_SIDE,
        absorb_confirmation_bps=DEFAULT_ABSORB_CONFIRMATION_BPS,
        absorb_confirmation_max_s=DEFAULT_ABSORB_CONFIRMATION_MAX_S,
        outcome_horizons_s=DEFAULT_OUTCOME_HORIZONS_S,
        dry_run=False,
    )
