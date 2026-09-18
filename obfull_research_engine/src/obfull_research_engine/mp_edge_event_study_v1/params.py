"""Pilot parameters — all CLI-overridable; nothing silently hardcoded for the run."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from obfull_research_engine.timeparse import format_utc_z, parse_utc_z, validate_interval

PILOT_TIMEFRAMES: tuple[str, ...] = ("30m", "1h", "4h")
SILVER_DATABASE_DEFAULT = "research_full_ob_silver_v1_3"
METRICS_TABLE = "ob_metrics_100ms_v1_3"
MP_KIND = "previous_closed"
MP_EDGE_DEFINITION = "TPO_VAH_upper_TPO_VAL_lower"
PROFILE_SOURCE = "market_profile_context.adapter.build_one_profile+dashboard_dual_tpo"

# Binding pilot window (prep report window #20)
DEFAULT_SYMBOL = "BTCUSDT"
DEFAULT_START = "2026-09-10T15:16:26.672Z"
DEFAULT_END = "2026-09-11T01:59:08.842Z"

DEFAULT_TOUCH_TOLERANCE_BPS = 1.0
DEFAULT_CONFLUENCE_TOLERANCE_BPS = 5.0
DEFAULT_MIN_EVENT_SEPARATION_S = 60.0
DEFAULT_RESET_DISTANCE_BPS = 10.0
DEFAULT_MIN_PENETRATION_BPS = 2.0
DEFAULT_MAX_RECLAIM_DELAY_S = 30.0
DEFAULT_RECLAIM_HOLD_S = 15.0
DEFAULT_TRUE_BREAK_ACCEPTANCE_S = 30.0
DEFAULT_OUTCOME_HORIZONS_S: tuple[int, ...] = (60, 300, 900, 1800)
DEFAULT_TP_TARGETS_BPS: tuple[float, ...] = (30.0, 40.0, 50.0)
DEFAULT_SL_TARGET_BPS = 25.0

# Strict reclaim hold: any 100ms observation on the wrong side fails confirmation.
# Optional reclaim tolerance (bps beyond the reclaim side) — default 0 = strict.
DEFAULT_RECLAIM_TOLERANCE_BPS = 0.0


@dataclass(frozen=True)
class PilotParams:
    symbol: str
    start: datetime
    end: datetime
    touch_tolerance_bps: float
    confluence_tolerance_bps: float
    min_event_separation_s: float
    reset_distance_bps: float
    min_penetration_bps: float
    max_reclaim_delay_s: float
    reclaim_hold_s: float
    true_break_acceptance_s: float
    outcome_horizons_s: tuple[int, ...]
    tp_targets_bps: tuple[float, ...]
    sl_target_bps: float
    output_dir: Path
    dry_run: bool
    silver_database: str = SILVER_DATABASE_DEFAULT
    reclaim_tolerance_bps: float = DEFAULT_RECLAIM_TOLERANCE_BPS
    timeframes: tuple[str, ...] = PILOT_TIMEFRAMES
    mp_kind: str = MP_KIND
    mp_edge_definition: str = MP_EDGE_DEFINITION
    profile_source: str = PROFILE_SOURCE

    def to_manifest_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["start"] = format_utc_z(self.start)
        d["end"] = format_utc_z(self.end)
        d["output_dir"] = str(self.output_dir)
        d["outcome_horizons_s"] = list(self.outcome_horizons_s)
        d["tp_targets_bps"] = list(self.tp_targets_bps)
        d["timeframes"] = list(self.timeframes)
        return d


def _parse_csv_floats(raw: str) -> tuple[float, ...]:
    parts = [p.strip() for p in str(raw).split(",") if p.strip()]
    if not parts:
        raise ValueError("empty numeric CSV")
    return tuple(float(p) for p in parts)


def _parse_csv_ints(raw: str) -> tuple[int, ...]:
    return tuple(int(x) for x in _parse_csv_floats(raw))


def build_params(
    *,
    symbol: str = DEFAULT_SYMBOL,
    start: str = DEFAULT_START,
    end: str = DEFAULT_END,
    touch_tolerance_bps: float = DEFAULT_TOUCH_TOLERANCE_BPS,
    confluence_tolerance_bps: float = DEFAULT_CONFLUENCE_TOLERANCE_BPS,
    min_event_separation_s: float = DEFAULT_MIN_EVENT_SEPARATION_S,
    reset_distance_bps: float = DEFAULT_RESET_DISTANCE_BPS,
    min_penetration_bps: float = DEFAULT_MIN_PENETRATION_BPS,
    max_reclaim_delay_s: float = DEFAULT_MAX_RECLAIM_DELAY_S,
    reclaim_hold_s: float = DEFAULT_RECLAIM_HOLD_S,
    true_break_acceptance_s: float = DEFAULT_TRUE_BREAK_ACCEPTANCE_S,
    outcome_horizons_s: str | Sequence[int] = DEFAULT_OUTCOME_HORIZONS_S,
    tp_targets_bps: str | Sequence[float] = DEFAULT_TP_TARGETS_BPS,
    sl_target_bps: float = DEFAULT_SL_TARGET_BPS,
    output_dir: str | Path,
    dry_run: bool = False,
    silver_database: str = SILVER_DATABASE_DEFAULT,
    reclaim_tolerance_bps: float = DEFAULT_RECLAIM_TOLERANCE_BPS,
) -> PilotParams:
    start_dt = parse_utc_z(start, field="--start")
    end_dt = parse_utc_z(end, field="--end")
    validate_interval(start_dt, end_dt)
    if isinstance(outcome_horizons_s, str):
        horizons = _parse_csv_ints(outcome_horizons_s)
    else:
        horizons = tuple(int(x) for x in outcome_horizons_s)
    if isinstance(tp_targets_bps, str):
        tps = _parse_csv_floats(tp_targets_bps)
    else:
        tps = tuple(float(x) for x in tp_targets_bps)
    if not horizons:
        raise ValueError("--outcome-horizons-s must be non-empty")
    if not tps:
        raise ValueError("--tp-targets-bps must be non-empty")
    return PilotParams(
        symbol=str(symbol).upper(),
        start=start_dt,
        end=end_dt,
        touch_tolerance_bps=float(touch_tolerance_bps),
        confluence_tolerance_bps=float(confluence_tolerance_bps),
        min_event_separation_s=float(min_event_separation_s),
        reset_distance_bps=float(reset_distance_bps),
        min_penetration_bps=float(min_penetration_bps),
        max_reclaim_delay_s=float(max_reclaim_delay_s),
        reclaim_hold_s=float(reclaim_hold_s),
        true_break_acceptance_s=float(true_break_acceptance_s),
        outcome_horizons_s=horizons,
        tp_targets_bps=tps,
        sl_target_bps=float(sl_target_bps),
        output_dir=Path(output_dir),
        dry_run=bool(dry_run),
        silver_database=str(silver_database),
        reclaim_tolerance_bps=float(reclaim_tolerance_bps),
    )
