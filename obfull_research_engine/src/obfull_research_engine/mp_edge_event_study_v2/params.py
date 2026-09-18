"""V2 pilot parameters."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from obfull_research_engine.timeparse import format_utc_z, parse_utc_z, validate_interval

PILOT_TIMEFRAMES: tuple[str, ...] = ("30m", "1h", "4h")
SILVER_DATABASE_DEFAULT = "research_full_ob_silver_v1_3"
MP_KIND = "previous_closed"
MP_EDGE_DEFINITION = "TPO_VAH_upper_TPO_VAL_lower"
PROFILE_SOURCE = "market_profile_context.adapter.build_one_profile+dashboard_dual_tpo"

DEFAULT_SYMBOL = "BTCUSDT"
DEFAULT_START = "2026-09-10T15:16:26.672Z"
DEFAULT_END = "2026-09-11T01:59:08.842Z"

DEFAULT_TOUCH_TOLERANCE_BPS = 1.0
DEFAULT_CONFLUENCE_TOLERANCE_BPS = 5.0
DEFAULT_MIN_EVENT_SEPARATION_S = 60.0
DEFAULT_RESET_DISTANCE_BPS = 10.0
DEFAULT_MIN_PENETRATION_BPS = 2.0
DEFAULT_RECLAIM_HOLD_S = 15.0
DEFAULT_RECLAIM_TOLERANCE_BPS = 0.0
DEFAULT_FAILED_BREAK_HORIZON_S = 120.0
DEFAULT_TRUE_BREAK_ACCEPTANCE_S = 60.0
DEFAULT_TRUE_BREAK_CONTINUATION_BPS = 3.0
DEFAULT_APPROACH_LOOKBACK_S = 30.0
DEFAULT_APPROACH_ORIGIN_DISTANCE_BPS = 2.0
DEFAULT_REQUIRE_CORRECT_APPROACH = True
DEFAULT_ALLOW_RETEST_FROM_BREAK_SIDE = False
DEFAULT_ABSORB_CONFIRMATION_BPS = 3.0
DEFAULT_ABSORB_CONFIRMATION_MAX_S = 60.0
DEFAULT_OUTCOME_HORIZONS_S: tuple[int, ...] = (60, 300, 900, 1800)

# Diagnostic TP/SL pairs (not optimized for profit)
DEFAULT_TPSL_PAIRS: tuple[tuple[float, float], ...] = (
    (5.0, 10.0),
    (10.0, 10.0),
    (15.0, 10.0),
    (20.0, 15.0),
    (25.0, 15.0),
    (30.0, 25.0),
    (40.0, 25.0),
    (50.0, 25.0),
)


@dataclass(frozen=True)
class PilotParamsV2:
    symbol: str
    start: datetime
    end: datetime
    touch_tolerance_bps: float
    confluence_tolerance_bps: float
    min_event_separation_s: float
    reset_distance_bps: float
    min_penetration_bps: float
    reclaim_hold_s: float
    reclaim_tolerance_bps: float
    failed_break_horizon_s: float
    true_break_acceptance_s: float
    true_break_continuation_bps: float
    approach_lookback_s: float
    approach_origin_distance_bps: float
    require_correct_approach: bool
    allow_retest_from_break_side: bool
    absorb_confirmation_bps: float
    absorb_confirmation_max_s: float
    outcome_horizons_s: tuple[int, ...]
    tpsl_pairs: tuple[tuple[float, float], ...]
    output_dir: Path
    dry_run: bool
    silver_database: str = SILVER_DATABASE_DEFAULT
    timeframes: tuple[str, ...] = PILOT_TIMEFRAMES
    mp_kind: str = MP_KIND
    mp_edge_definition: str = MP_EDGE_DEFINITION
    profile_source: str = PROFILE_SOURCE
    v1_run_dir: str = "obfull_research_engine/runs/mp_edge_event_pilot_v1_20260916"

    def to_manifest_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["start"] = format_utc_z(self.start)
        d["end"] = format_utc_z(self.end)
        d["output_dir"] = str(self.output_dir)
        d["outcome_horizons_s"] = list(self.outcome_horizons_s)
        d["tpsl_pairs"] = [{"tp_bps": a, "sl_bps": b} for a, b in self.tpsl_pairs]
        d["timeframes"] = list(self.timeframes)
        return d


def _parse_csv_floats(raw: str) -> tuple[float, ...]:
    parts = [p.strip() for p in str(raw).split(",") if p.strip()]
    return tuple(float(p) for p in parts)


def _parse_csv_ints(raw: str) -> tuple[int, ...]:
    return tuple(int(x) for x in _parse_csv_floats(raw))


def build_params_v2(
    *,
    symbol: str = DEFAULT_SYMBOL,
    start: str = DEFAULT_START,
    end: str = DEFAULT_END,
    touch_tolerance_bps: float = DEFAULT_TOUCH_TOLERANCE_BPS,
    confluence_tolerance_bps: float = DEFAULT_CONFLUENCE_TOLERANCE_BPS,
    min_event_separation_s: float = DEFAULT_MIN_EVENT_SEPARATION_S,
    reset_distance_bps: float = DEFAULT_RESET_DISTANCE_BPS,
    min_penetration_bps: float = DEFAULT_MIN_PENETRATION_BPS,
    reclaim_hold_s: float = DEFAULT_RECLAIM_HOLD_S,
    reclaim_tolerance_bps: float = DEFAULT_RECLAIM_TOLERANCE_BPS,
    failed_break_horizon_s: float = DEFAULT_FAILED_BREAK_HORIZON_S,
    true_break_acceptance_s: float = DEFAULT_TRUE_BREAK_ACCEPTANCE_S,
    true_break_continuation_bps: float = DEFAULT_TRUE_BREAK_CONTINUATION_BPS,
    approach_lookback_s: float = DEFAULT_APPROACH_LOOKBACK_S,
    approach_origin_distance_bps: float = DEFAULT_APPROACH_ORIGIN_DISTANCE_BPS,
    require_correct_approach: bool = DEFAULT_REQUIRE_CORRECT_APPROACH,
    allow_retest_from_break_side: bool = DEFAULT_ALLOW_RETEST_FROM_BREAK_SIDE,
    absorb_confirmation_bps: float = DEFAULT_ABSORB_CONFIRMATION_BPS,
    absorb_confirmation_max_s: float = DEFAULT_ABSORB_CONFIRMATION_MAX_S,
    outcome_horizons_s: str | Sequence[int] = DEFAULT_OUTCOME_HORIZONS_S,
    output_dir: str | Path,
    dry_run: bool = False,
    silver_database: str = SILVER_DATABASE_DEFAULT,
    v1_run_dir: str = "obfull_research_engine/runs/mp_edge_event_pilot_v1_20260916",
) -> PilotParamsV2:
    start_dt = parse_utc_z(start, field="--start")
    end_dt = parse_utc_z(end, field="--end")
    validate_interval(start_dt, end_dt)
    if isinstance(outcome_horizons_s, str):
        horizons = _parse_csv_ints(outcome_horizons_s)
    else:
        horizons = tuple(int(x) for x in outcome_horizons_s)
    return PilotParamsV2(
        symbol=str(symbol).upper(),
        start=start_dt,
        end=end_dt,
        touch_tolerance_bps=float(touch_tolerance_bps),
        confluence_tolerance_bps=float(confluence_tolerance_bps),
        min_event_separation_s=float(min_event_separation_s),
        reset_distance_bps=float(reset_distance_bps),
        min_penetration_bps=float(min_penetration_bps),
        reclaim_hold_s=float(reclaim_hold_s),
        reclaim_tolerance_bps=float(reclaim_tolerance_bps),
        failed_break_horizon_s=float(failed_break_horizon_s),
        true_break_acceptance_s=float(true_break_acceptance_s),
        true_break_continuation_bps=float(true_break_continuation_bps),
        approach_lookback_s=float(approach_lookback_s),
        approach_origin_distance_bps=float(approach_origin_distance_bps),
        require_correct_approach=bool(require_correct_approach),
        allow_retest_from_break_side=bool(allow_retest_from_break_side),
        absorb_confirmation_bps=float(absorb_confirmation_bps),
        absorb_confirmation_max_s=float(absorb_confirmation_max_s),
        outcome_horizons_s=horizons,
        tpsl_pairs=DEFAULT_TPSL_PAIRS,
        output_dir=Path(output_dir),
        dry_run=bool(dry_run),
        silver_database=str(silver_database),
        v1_run_dir=str(v1_run_dir),
    )
