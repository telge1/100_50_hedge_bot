"""Shared variant-independent structure timeline for research audits.

Computes market structure, HTF context, impulse counters and scores once per
candle frame. Policy variants then replay only trend-state decisions on top of
the immutable per-bar prepared inputs.
"""

from __future__ import annotations

import hashlib
import pickle
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.config import RegimeScannerConfig, default_regime_scanner_config
from research.regime_scanner.swings import PivotVisibilityIndex, find_confirmed_pivots
from research.regime_scanner.trend_robustness_audit import install_htf_cache
from research.regime_scanner.trend_state_machine import (
    TrendRuntime,
    TrendStateConfig,
    TrendStateSnapshot,
    _enter,
    _finite,
    _propose_transition,
    _scores,
    _ts,
    _update_impulse_counters,
    _update_swing_age,
    build_snapshot,
    default_trend_state_config,
    update_turning_evidence,
    update_weakening_evidence,
)
from research.regime_scanner.trend_structure import (
    MarketStructureState,
    StructureEvent,
    TrendStructureConfig,
    copy_structure_state,
    update_market_structure,
)

# Audit instrumentation
SHARED_STRUCTURE_PASS_COUNT = 0
VARIANT_POLICY_REPLAY_COUNT = 0


@dataclass
class PreparedBar:
    """Immutable variant-independent inputs for one closed 5m bar."""

    bar_index: int
    decision_time: pd.Timestamp
    row: dict[str, Any]
    events_5m: list[StructureEvent]
    structure_5m: MarketStructureState
    structure_15m: MarketStructureState
    structure_30m: MarketStructureState
    last_15m_bucket: str | None
    last_30m_bucket: str | None
    consecutive_bearish_closes: int
    consecutive_bullish_closes: int
    bars_since_ll: int
    bars_since_hh: int
    scores: dict[str, float]
    structure_skipped: bool = False


@dataclass
class SharedReplayContext:
    frame: pd.DataFrame
    pivot_visibility: PivotVisibilityIndex
    pivot_end_by_bar: np.ndarray
    prepared_bars: list[PreparedBar] = field(default_factory=list)
    structure_pass_count: int = 1
    cache_key: str = ""


def _frame_cache_key(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "empty"
    first = pd.Timestamp(frame["timestamp"].iloc[0]).isoformat()
    last = pd.Timestamp(frame["timestamp"].iloc[-1]).isoformat()
    payload = f"{len(frame)}|{first}|{last}|{frame['close'].iloc[0]}|{frame['close'].iloc[-1]}"
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def build_shared_structure_timeline(
    frame: pd.DataFrame,
    *,
    scanner_cfg: RegimeScannerConfig | None = None,
    structure_cfg: TrendStructureConfig | None = None,
    score_cfg: TrendStateConfig | None = None,
) -> SharedReplayContext:
    """Single variant-independent structure pass over the full frame."""
    global SHARED_STRUCTURE_PASS_COUNT
    SHARED_STRUCTURE_PASS_COUNT += 1

    scfg = scanner_cfg or default_regime_scanner_config().with_timeframe("5m")
    struct_cfg = structure_cfg or default_trend_state_config().structure
    scores_cfg = score_cfg or default_trend_state_config()

    end_decision = _ts(frame["decision_time"].iloc[-1])
    install_htf_cache(frame, end_decision)

    pivots = find_confirmed_pivots(frame, config=scfg)
    pivot_visibility = PivotVisibilityIndex.build(pivots)
    pivot_end_by_bar = pivot_visibility.end_indices_for(frame["decision_time"])

    import research.regime_scanner.trend_state_machine as sm_mod

    update_htf = sm_mod._update_htf_structure

    struct_rt = TrendRuntime()
    prepared: list[PreparedBar] = []
    empty_candles = frame.iloc[:0]

    for bar_index, (_, row) in enumerate(frame.iterrows()):
        decision_ts = _ts(row["decision_time"])
        row_dict = row.to_dict()

        gap = False
        if struct_rt.last_decision_time is not None:
            delta_bars = int(
                round((decision_ts - struct_rt.last_decision_time) / pd.Timedelta(minutes=5))
            )
            if delta_bars > int(scores_cfg.max_gap_bars) + 1:
                gap = True

        warmup = bar_index + 1 < int(scores_cfg.min_warmup_5m_bars)
        if gap or warmup:
            prepared.append(
                PreparedBar(
                    bar_index=bar_index,
                    decision_time=decision_ts,
                    row=row_dict,
                    events_5m=[],
                    structure_5m=copy_structure_state(struct_rt.structure_5m),
                    structure_15m=copy_structure_state(struct_rt.structure_15m),
                    structure_30m=copy_structure_state(struct_rt.structure_30m),
                    last_15m_bucket=struct_rt.last_15m_bucket,
                    last_30m_bucket=struct_rt.last_30m_bucket,
                    consecutive_bearish_closes=struct_rt.consecutive_bearish_closes,
                    consecutive_bullish_closes=struct_rt.consecutive_bullish_closes,
                    bars_since_ll=struct_rt.bars_since_ll,
                    bars_since_hh=struct_rt.bars_since_hh,
                    scores={
                        "bearish_score": 0.0,
                        "bullish_score": 0.0,
                        "weakening_score": 0.0,
                        "bottoming_score": 0.0,
                    },
                    structure_skipped=True,
                )
            )
            struct_rt.last_decision_time = decision_ts
            continue

        end_idx = int(pivot_end_by_bar[bar_index])
        pivots_as_of = pivots[:end_idx]
        atr = _finite(row_dict.get("atr"))
        struct_rt.structure_5m, events_5m = update_market_structure(
            struct_rt.structure_5m,
            candle=row_dict,
            pivots=pivots_as_of,
            decision_time=decision_ts,
            atr=atr,
            cfg=struct_cfg,
            pivots_already_causal=True,
        )
        update_htf(
            struct_rt,
            candles_5m=empty_candles,
            decision_time=decision_ts,
            cfg=scores_cfg,
            scanner_cfg=scfg,
        )
        _update_impulse_counters(struct_rt, row_dict)
        _update_swing_age(struct_rt, events_5m)
        bar_scores = _scores(events_5m, struct_rt.structure_5m, row_dict, scores_cfg)

        prepared.append(
            PreparedBar(
                bar_index=bar_index,
                decision_time=decision_ts,
                row=row_dict,
                events_5m=list(events_5m),
                structure_5m=copy_structure_state(struct_rt.structure_5m),
                structure_15m=copy_structure_state(struct_rt.structure_15m),
                structure_30m=copy_structure_state(struct_rt.structure_30m),
                last_15m_bucket=struct_rt.last_15m_bucket,
                last_30m_bucket=struct_rt.last_30m_bucket,
                consecutive_bearish_closes=struct_rt.consecutive_bearish_closes,
                consecutive_bullish_closes=struct_rt.consecutive_bullish_closes,
                bars_since_ll=struct_rt.bars_since_ll,
                bars_since_hh=struct_rt.bars_since_hh,
                scores=dict(bar_scores),
                structure_skipped=False,
            )
        )
        struct_rt.last_decision_time = decision_ts

    return SharedReplayContext(
        frame=frame,
        pivot_visibility=pivot_visibility,
        pivot_end_by_bar=pivot_end_by_bar,
        prepared_bars=prepared,
        structure_pass_count=1,
        cache_key=_frame_cache_key(frame),
    )


def load_or_build_shared_context(
    frame: pd.DataFrame,
    *,
    cache_dir: Path | None = None,
    force_rebuild: bool = False,
) -> SharedReplayContext:
    """Build shared context once; reuse pickled artifact on identical frame."""
    key = _frame_cache_key(frame)
    cache_path = None if cache_dir is None else cache_dir / f"shared_structure_{key}.pkl"
    if cache_path is not None and cache_path.is_file() and not force_rebuild:
        with cache_path.open("rb") as fh:
            ctx = pickle.load(fh)
        if isinstance(ctx, SharedReplayContext) and ctx.cache_key == key:
            return ctx
    ctx = build_shared_structure_timeline(frame)
    if cache_path is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        with cache_path.open("wb") as fh:
            pickle.dump(ctx, fh, protocol=pickle.HIGHEST_PROTOCOL)
    return ctx


def step_trend_state_from_prepared(
    rt: TrendRuntime,
    *,
    prepared: PreparedBar,
    cfg: TrendStateConfig,
) -> tuple[TrendRuntime, TrendStateSnapshot, list[StructureEvent]]:
    """Policy-only trend step: consumes precomputed structure, never rebuilds it."""
    decision_ts = prepared.decision_time
    row = prepared.row
    bar_index = prepared.bar_index

    if rt.last_decision_time is not None:
        delta_bars = int(
            round((decision_ts - rt.last_decision_time) / pd.Timedelta(minutes=5))
        )
        if delta_bars > int(cfg.max_gap_bars) + 1:
            rt.state = "unavailable"
            rt.unavailable_reason = "data_gap"
            rt.age_5m_bars = 0
            snap = build_snapshot(
                rt,
                decision_time=decision_ts,
                events=[],
                scores={
                    "bearish_score": 0,
                    "bullish_score": 0,
                    "weakening_score": 0,
                    "bottoming_score": 0,
                },
                reasons=["data_gap"],
                cfg=cfg,
            )
            rt.last_decision_time = decision_ts
            return rt, snap, []

    if bar_index + 1 < int(cfg.min_warmup_5m_bars):
        rt.state = "unavailable"
        rt.unavailable_reason = "warmup"
        rt.last_decision_time = decision_ts
        snap = build_snapshot(
            rt,
            decision_time=decision_ts,
            events=[],
            scores={
                "bearish_score": 0,
                "bullish_score": 0,
                "weakening_score": 0,
                "bottoming_score": 0,
            },
            reasons=["warmup"],
            cfg=cfg,
        )
        return rt, snap, []

    if rt.state == "unavailable" and rt.unavailable_reason in {"warmup", "data_gap"}:
        rt.state = "neutral"
        rt.unavailable_reason = None
        rt.entered_at = decision_ts
        rt.age_5m_bars = 0
        rt.previous_state = "unavailable"

    if prepared.structure_skipped:
        events_5m: list[StructureEvent] = []
        scores = {
            "bearish_score": 0.0,
            "bullish_score": 0.0,
            "weakening_score": 0.0,
            "bottoming_score": 0.0,
        }
    else:
        rt.structure_5m = copy_structure_state(prepared.structure_5m)
        rt.structure_15m = copy_structure_state(prepared.structure_15m)
        rt.structure_30m = copy_structure_state(prepared.structure_30m)
        rt.last_15m_bucket = prepared.last_15m_bucket
        rt.last_30m_bucket = prepared.last_30m_bucket
        rt.consecutive_bearish_closes = prepared.consecutive_bearish_closes
        rt.consecutive_bullish_closes = prepared.consecutive_bullish_closes
        rt.bars_since_ll = prepared.bars_since_ll
        rt.bars_since_hh = prepared.bars_since_hh
        events_5m = prepared.events_5m
        scores = prepared.scores

    evidence_notes = update_weakening_evidence(rt, events=events_5m, cfg=cfg)
    turning_notes = update_turning_evidence(
        rt, events=events_5m, cfg=cfg, decision_time=decision_ts
    )
    proposed, reasons = _propose_transition(
        rt, events=events_5m, row=row, cfg=cfg, decision_time=decision_ts
    )
    if evidence_notes or turning_notes:
        reasons = [*evidence_notes, *turning_notes, *reasons]
    if proposed is not None and proposed != rt.state:
        reasons = _enter(rt, proposed, decision_time=decision_ts, reasons=reasons)
    else:
        rt.age_5m_bars += 1
        if not reasons:
            reasons = ["hold"]

    rt.last_decision_time = decision_ts
    snap = build_snapshot(
        rt,
        decision_time=decision_ts,
        events=events_5m,
        scores=scores,
        reasons=reasons,
        cfg=cfg,
    )
    return rt, snap, events_5m


def reset_audit_counters() -> None:
    global SHARED_STRUCTURE_PASS_COUNT, VARIANT_POLICY_REPLAY_COUNT
    SHARED_STRUCTURE_PASS_COUNT = 0
    VARIANT_POLICY_REPLAY_COUNT = 0
    import research.regime_scanner.swings as swings_mod

    swings_mod.FILTER_PIVOTS_AS_OF_CALLS = 0
