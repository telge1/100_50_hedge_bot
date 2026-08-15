"""Evaluation helpers joining v2 monitor output with v3 decisive-break."""

from __future__ import annotations

from typing import Any

import pandas as pd

from research.regime_scanner.tem_structure_break.decisive_break import (
    extract_v2_arm_from_events,
    run_decisive_break,
)
from research.regime_scanner.tem_structure_break.decisive_models import (
    DECISIVE_RULE_ID,
    SIGNAL_VERSION_V3,
    STABILIZE_4H_BARS,
    DecisiveRuntime,
)
from research.regime_scanner.tem_structure_break.eval_common import (
    TradeSpec,
    cycle_at_ts,
    lead_hours,
    summarize_trade,
)
from research.regime_scanner.tem_structure_break.monitor import MonitorRuntime, SIGNAL_VERSION


def summarize_decisive(
    spec: TradeSpec,
    v2_rt: MonitorRuntime,
    dec_rt: DecisiveRuntime,
    *,
    frame,
    cycles: dict | None = None,
    explosion: dict | None = None,
    v2_summary: dict[str, Any] | None = None,
    bucket: str | None = None,
) -> dict[str, Any]:
    cycles = cycles or {}
    v2 = v2_summary or summarize_trade(
        spec, v2_rt, frame=frame, cycles=cycles, explosion=explosion
    )
    conf_ts = dec_rt.confirmed_ts
    pend_ts = dec_rt.pending_ts
    c4 = v2.get("cycle4_ts")
    c5 = v2.get("cycle5_ts")
    exp = v2.get("mtm_explosion_ts")
    lead_c4 = lead_hours(conf_ts, c4)
    lead_c5 = lead_hours(conf_ts, c5)
    lead_exp = lead_hours(conf_ts, exp)
    v2_inv = v2.get("final_invalidation_ts")
    return {
        **{k: v2.get(k) for k in v2},
        "v3_holdout_bucket": bucket,
        "v2_final_invalidation_ts": v2_inv,
        "v2_signal_version": SIGNAL_VERSION,
        "decisive_signal_version": SIGNAL_VERSION_V3,
        "decisive_rule_id": DECISIVE_RULE_ID,
        "decisive_state": dec_rt.state.value,
        "decisive_level_ready_ts": None
        if dec_rt.level is None
        else dec_rt.level.confirmed_ts,
        "decisive_level_type": None if dec_rt.level is None else dec_rt.level.level_type,
        "decisive_level_value": None if dec_rt.level is None else dec_rt.level.value,
        "decisive_level_source": None if dec_rt.level is None else dec_rt.level.source,
        "decisive_lower_high_ts": dec_rt.last_lower_high_ts,
        "decisive_break_pending_ts": pend_ts,
        "decisive_break_ts": pend_ts,
        "decisive_confirmation_ts": conf_ts,
        "decisive_reclaim_ts": dec_rt.reclaim_ts,
        "decisive_reason": dec_rt.reason,
        "hours_v2_to_decisive": lead_hours(v2_inv, conf_ts)
        if v2_inv
        else lead_hours(v2.get("first_break_ts"), conf_ts),
        "cycle_at_decisive": cycle_at_ts(cycles, frame, conf_ts),
        "decisive_before_cycle4": None if lead_c4 is None else lead_c4 > 0,
        "decisive_before_cycle5": None if lead_c5 is None else lead_c5 > 0,
        "decisive_before_explosion": None if lead_exp is None else lead_exp > 0,
        "lead_hours_decisive_vs_cycle4": lead_c4,
        "lead_hours_decisive_vs_cycle5": lead_c5,
        "lead_hours_decisive_vs_explosion": lead_exp,
        "stabilize_bars": dec_rt.stabilize_bars,
        "has_decisive_break": bool(conf_ts),
        "decisive_later_than_v2": bool(
            conf_ts
            and v2_inv
            and lead_hours(v2_inv, conf_ts) is not None
            and lead_hours(v2_inv, conf_ts) > 0
        ),
    }


def run_v2_then_decisive(
    spec: TradeSpec,
    cache,
    *,
    cycles: dict | None = None,
    explosion: dict | None = None,
    stabilize_bars: int = STABILIZE_4H_BARS,
    bucket: str | None = None,
) -> tuple[MonitorRuntime, DecisiveRuntime, dict[str, Any]]:
    from research.regime_scanner.tem_structure_break.eval_common import run_spec

    v2_rt, frames = run_spec(spec, cache)
    arm_ts, arm_lvl = extract_v2_arm_from_events(v2_rt.events)
    end_dec = None
    if spec.end_bar is not None and 0 <= int(spec.end_bar) < len(frames.frame_5m):
        end_dec = str(
            pd.Timestamp(frames.frame_5m.iloc[int(spec.end_bar)]["timestamp"])
            + pd.Timedelta(minutes=5)
        )
    dec_rt = run_decisive_break(
        frames.h4,
        v2_first_break_ts=arm_ts,
        v2_break_level=arm_lvl,
        stabilize_bars=stabilize_bars,
        end_close_decision=end_dec,
    )
    summary = summarize_decisive(
        spec,
        v2_rt,
        dec_rt,
        frame=frames.frame_5m,
        cycles=cycles,
        explosion=explosion,
        bucket=bucket,
    )
    return v2_rt, dec_rt, summary
