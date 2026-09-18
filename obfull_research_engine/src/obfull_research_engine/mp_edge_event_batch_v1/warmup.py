"""MP warmup bounds — profiles may use pre-window closed periods within same epoch."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from obfull_research_engine.market_profile_context.windows import period_s
from obfull_research_engine.mp_edge_event_study_v1.util import as_utc, dt_to_ns, format_ns_z, ns_to_dt
from obfull_research_engine.timeparse import format_utc_z

from .params import PILOT_TIMEFRAMES


@dataclass
class WarmupPlan:
    window_id: str
    epoch_safe_start_ns: int
    analysis_start_ns: int
    analysis_end_ns: int
    outcome_end_ns: int
    mp_warmup_start_ns: int
    mp_warmup_clamped: bool
    timeframes_available_at_start: list[str]
    timeframes_missing_at_start: list[str]
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["analysis_start"] = format_ns_z(self.analysis_start_ns)
        d["analysis_end"] = format_ns_z(self.analysis_end_ns)
        d["outcome_end"] = format_ns_z(self.outcome_end_ns)
        d["mp_warmup_start"] = format_ns_z(self.mp_warmup_start_ns)
        d["epoch_safe_start"] = format_ns_z(self.epoch_safe_start_ns)
        return d


def plan_warmup(
    *,
    window_id: str,
    analysis_start_ns: int,
    analysis_end_ns: int,
    epoch_safe_start_ns: int,
    timeframes: tuple[str, ...] = PILOT_TIMEFRAMES,
) -> WarmupPlan:
    """Warmup = max(period) before analysis_start, clamped to epoch_safe_start.

    Events only in [analysis_start, analysis_end).
    Outcomes only until analysis_end (same safe window) → CENSORED if incomplete.
    """
    analysis_start = ns_to_dt(analysis_start_ns)
    max_period = max(period_s(tf) for tf in timeframes)
    desired = analysis_start - timedelta(seconds=max_period)
    desired_ns = dt_to_ns(desired)
    clamped = desired_ns < epoch_safe_start_ns
    warmup_ns = max(desired_ns, epoch_safe_start_ns)

    available: list[str] = []
    missing: list[str] = []
    for tf in timeframes:
        # previous_closed available at analysis_start iff a full period ended at/before start
        # and that period start ≥ epoch_safe_start (data to build profile)
        need = period_s(tf)
        # profile [T-P, T) available at T ≤ analysis_start requires T-P ≥ epoch_safe_start
        # for the latest closed: floor(analysis_start) as T
        from obfull_research_engine.market_profile_context.windows import previous_closed_bounds

        ps, pe = previous_closed_bounds(analysis_start, tf)
        if dt_to_ns(ps) >= epoch_safe_start_ns and dt_to_ns(pe) <= analysis_start_ns:
            available.append(tf)
        else:
            missing.append(tf)

    notes = ""
    if clamped:
        notes = "warmup_clamped_to_epoch_safe_start"
    if missing:
        notes = (notes + ";" if notes else "") + f"missing_tf_at_start={','.join(missing)}"

    return WarmupPlan(
        window_id=window_id,
        epoch_safe_start_ns=int(epoch_safe_start_ns),
        analysis_start_ns=int(analysis_start_ns),
        analysis_end_ns=int(analysis_end_ns),
        outcome_end_ns=int(analysis_end_ns),
        mp_warmup_start_ns=int(warmup_ns),
        mp_warmup_clamped=bool(clamped),
        timeframes_available_at_start=available,
        timeframes_missing_at_start=missing,
        notes=notes,
    )
