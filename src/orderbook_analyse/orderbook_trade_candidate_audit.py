"""Causal orderbook trade-candidate audit (research only).

Strict causality: at candidate time ``t`` only data with timestamp ``<= t`` may
drive LONG / SHORT / NO_TRADE. Forward paths are used solely for outcome scoring.

Reuses wall movement, near-liquidity, liquidation and replay helpers — does not
duplicate bucket/wall detection. Not a live strategy and never places orders.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

import orjson

from orderbook_analyse.dynamic_wall_detector import (
    PROJECT_ROOT,
    WallDetectorParams,
    choose_bucket_size,
    connect_readonly,
    find_bootstrap_snapshot,
    infer_tick_size,
    load_events,
    parse_utc,
    reconstruct_with_samples,
    utc_now,
    write_csv,
)
from orderbook_analyse.liquidation_analysis import (
    LIQUIDATED_LONG,
    LIQUIDATED_SHORT,
    LIQUIDATION_THROUGH_ASK,
    LIQUIDATION_THROUGH_BID,
    LiquidationEvent,
    load_liquidations,
    load_trade_price_path,
    merge_price_paths,
    wall_relation_labels,
)
from orderbook_analyse.near_liquidity import (
    BULLISH_LIQUIDITY_SHIFT,
    BEARISH_LIQUIDITY_SHIFT,
    NEAR_ASK_MOVING_HIGHER,
    NEAR_ASK_MOVING_LOWER,
    NEAR_ASK_THINNING,
    NEAR_ASK_BUILDING,
    NearAskTransition,
    NearParams,
    NearSnapshotView,
    build_near_ask_transitions,
    detect_ask_ladder_sequences,
    summarize_near_regime,
)
from orderbook_analyse.orderbook_replay import ReplayError
from orderbook_analyse.wall_movement_tracker import (
    FALLING_ASK_CEILING,
    FALLING_BID_FLOOR,
    MovementParams,
    RISING_ASK_CEILING,
    RISING_BID_FLOOR,
    SequenceRecord,
    SnapshotRecord,
    TransitionRecord,
    WallView,
    build_sequences,
    build_snapshots_from_books,
    build_transitions,
    load_oi_at,
    load_trades_between,
)

logger = logging.getLogger(__name__)

LONG = "LONG"
SHORT = "SHORT"
NO_TRADE = "NO_TRADE"

NO_TRADE_TOO_WIDE_SL = "NO_TRADE_TOO_WIDE_SL"
NO_TRADE_INSUFFICIENT_CRV = "NO_TRADE_INSUFFICIENT_CRV"
NO_TRADE_LOW_SCORE = "NO_TRADE_LOW_SCORE"
NO_TRADE_CONTRADICTION = "NO_TRADE_CONTRADICTION"
NO_TRADE_COOLDOWN = "NO_TRADE_COOLDOWN"
NO_TRADE_NO_STRUCTURE = "NO_TRADE_NO_STRUCTURE"
NO_TRADE_NO_ENTRY_PRICE = "NO_TRADE_NO_ENTRY_PRICE"
NO_TRADE_ACTIVE_FALLING_ASK_BLOCKER = "NO_TRADE_ACTIVE_FALLING_ASK_BLOCKER"
NO_TRADE_RETEST_NOT_CONFIRMED = "NO_TRADE_RETEST_NOT_CONFIRMED"
NO_TRADE_RETEST_BROKEN = "NO_TRADE_RETEST_BROKEN"
NO_TRADE_RETEST_SL_TOO_WIDE = "NO_TRADE_RETEST_SL_TOO_WIDE"
NO_TRADE_RETEST_INSUFFICIENT_CRV = "NO_TRADE_RETEST_INSUFFICIENT_CRV"
NO_TRADE_FLOW_NOT_CONFIRMED = "NO_TRADE_FLOW_NOT_CONFIRMED"
NO_TRADE_CONFLICTING_STRUCTURE = "NO_TRADE_CONFLICTING_STRUCTURE"

SUPPORT_RETEST_RECLAIM_LONG = "SUPPORT_RETEST_RECLAIM_LONG"

TP1_HIT = "TP1_HIT"
TP2_HIT = "TP2_HIT"
SL_HIT = "SL_HIT"
TP1_THEN_SL = "TP1_THEN_SL"
TP1_THEN_TP2 = "TP1_THEN_TP2"
NEITHER_HIT = "NEITHER_HIT"
OPEN_AT_END = "OPEN_AT_END"
AMBIGUOUS_TP_SL_ORDER = "AMBIGUOUS_TP_SL_ORDER"
SL_FIRST_AMBIGUOUS = "SL_FIRST_AMBIGUOUS"

HORIZONS_SEC = (300, 600, 1200, 1800, 3600)


def _ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _fmt(value: Decimal | float | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, float):
        return f"{value:.6f}"
    return format(value, "f")


def _pct(numer: Decimal, denom: Decimal) -> float | None:
    if denom == 0:
        return None
    return float(numer / denom * Decimal(100))


@dataclass
class AuditParams:
    sample_seconds: int = 30
    target_bps: float = 10.0
    distance_max_pct: float = 3.0
    near_min_distance_pct: float = 0.10
    near_max_distance_pct: float = 1.50
    near_top_n: int = 3
    near_max_buckets: int = 15
    minimum_entry_score: int = 5
    entry_mode: str = "next-snapshot-mid"  # mid | next-trade | next-snapshot-mid
    sl_buffer_bps: float = 8.0
    min_sl_distance_pct: float = 0.20
    max_sl_distance_pct: float = 1.50
    tp_front_run_bps: float = 5.0
    min_crv_tp1: float = 1.20
    min_crv_tp2: float = 1.80
    cooldown_minutes: int = 5
    flow_lookback_snapshots: int = 2
    strong_delta_notional: float = 5000.0
    wall_relation_bps: float = 10.0
    structure_state_max_age_seconds: int = 180
    contradiction_state_max_age_seconds: int = 120
    reclaim_confirm_snapshots: int = 2
    retest_distance_bps: float = 12.0
    retest_max_undershoot_bps: float = 10.0
    retest_sl_buffer_bps: float = 5.0
    retest_max_sl_distance_pct: float = 0.60
    movement: MovementParams = field(default_factory=MovementParams)


@dataclass
class ScoreComponent:
    name: str
    points: int
    detail: str = ""

    def to_row(self, *, candidate_id: str) -> dict[str, Any]:
        return {
            "candidate_id": candidate_id,
            "component": self.name,
            "points": self.points,
            "detail": self.detail,
        }


@dataclass
class CandidateDecision:
    signal_time: datetime
    side: str  # LONG | SHORT | NO_TRADE*
    reason: str
    score: int
    components: list[ScoreComponent]
    snapshot_index: int
    rising_bid_shifts: int = 0
    falling_ask_shifts: int = 0
    auction_direction: str = "INCONCLUSIVE"
    short_term_bias: str = "INCONCLUSIVE"
    near_ask_class: str | None = None
    trade_delta: Decimal = Decimal("0")
    oi_change: Decimal | None = None
    mid: Decimal | None = None
    nearest_bid: Decimal | None = None
    nearest_ask: Decimal | None = None
    dominant_bid: Decimal | None = None
    dominant_ask: Decimal | None = None
    liquidation_confirm: str | None = None
    rising_bid_state_age_seconds: float | None = None
    falling_bid_state_age_seconds: float | None = None
    rising_ask_state_age_seconds: float | None = None
    falling_ask_state_age_seconds: float | None = None
    active_rising_bid_shifts: int = 0
    active_falling_ask_shifts: int = 0
    expired_falling_ask_shifts: int = 0
    active_structure_source_timestamp: datetime | None = None
    falling_ask_blocker_active: bool = False
    falling_ask_blocker_expired: bool = False
    falling_ask_reclaim_confirmed: bool = False
    falling_ask_reclaim_reason: str | None = None
    entry_setup_type: str | None = None
    retest_reference_type: str | None = None
    retest_reference_level: Decimal | None = None
    retest_first_touch_time: datetime | None = None
    retest_low: Decimal | None = None
    retest_undershoot_bps: float | None = None
    reclaim_start_time: datetime | None = None
    reclaim_confirm_time: datetime | None = None
    reclaim_snapshot_count: int = 0
    local_support_level: Decimal | None = None
    local_support_notional: Decimal | None = None
    static_tp1: Decimal | None = None
    static_tp2: Decimal | None = None
    dynamic_exit_trigger: str | None = None
    dynamic_exit_time: datetime | None = None
    dynamic_exit_price: Decimal | None = None
    dynamic_exit_r_multiple: float | None = None


@dataclass
class RetestState:
    reference_type: str
    reference_level: Decimal
    identified_time: datetime
    first_touch_time: datetime | None = None
    retest_low: Decimal | None = None
    undershoot_bps: float | None = None
    reclaim_start_time: datetime | None = None
    reclaim_confirm_time: datetime | None = None
    reclaim_snapshot_count: int = 0
    local_support_level: Decimal | None = None
    local_support_notional: Decimal | None = None
    flow_confirmed: bool = False
    broken: bool = False
    emitted: bool = False


class RetestTracker:
    """Causal state machine for broken near-ask resistance retests."""

    def __init__(self, params: AuditParams) -> None:
        self.params = params
        self.state: RetestState | None = None
        self.transitions: list[dict[str, Any]] = []
        self.context: list[dict[str, Any]] = []

    def process(
        self,
        snapshots: Sequence[SnapshotRecord],
        *,
        index: int,
    ) -> RetestState | None:
        snap = snapshots[index]
        previous = snapshots[index - 1] if index > 0 else None
        candidate_type = None
        candidate_level = None
        if previous is not None and previous.nearest_ask is not None:
            level = previous.nearest_ask.price
            if previous.mid_price <= level < snap.mid_price:
                candidate_type = "BROKEN_NEAR_ASK"
                candidate_level = level
        if (
            previous is not None
            and previous.nearest_bid is not None
            and snap.nearest_bid is not None
            and snap.nearest_bid.price > previous.nearest_bid.price
        ):
            bid_distance_bps = float(
                (snap.mid_price - snap.nearest_bid.price)
                / snap.nearest_bid.price
                * Decimal("10000")
            )
            if 0 <= bid_distance_bps <= self.params.retest_distance_bps:
                candidate_type = "LOCAL_BREAKOUT_SUPPORT"
                candidate_level = snap.nearest_bid.price

        if candidate_type is not None and candidate_level is not None:
            current = self.state
            replace_current = (
                current is None
                or current.broken
                or current.emitted
                or (
                    current.reclaim_confirm_time is not None
                    and snap.timestamp
                    > current.reclaim_confirm_time
                    + timedelta(seconds=self.params.structure_state_max_age_seconds)
                )
            )
            if replace_current:
                self.state = RetestState(
                    reference_type=candidate_type,
                    reference_level=candidate_level,
                    identified_time=snap.timestamp,
                )
                self.transitions.append(
                    {
                        "timestamp": snap.timestamp.isoformat(),
                        "transition": "RETEST_LEVEL_IDENTIFIED",
                        "reference_type": candidate_type,
                        "reference_level": _fmt(candidate_level),
                    }
                )

        state = self.state
        if state is None:
            return None
        level = state.reference_level
        distance_bps = float(abs(snap.mid_price - level) / level * Decimal("10000"))
        within_retest = distance_bps <= self.params.retest_distance_bps

        if state.first_touch_time is None and within_retest:
            state.first_touch_time = snap.timestamp
            state.retest_low = snap.mid_price
            self.transitions.append(
                {
                    "timestamp": snap.timestamp.isoformat(),
                    "transition": "RETEST_FIRST_TOUCH",
                    "reference_level": _fmt(level),
                    "mid": _fmt(snap.mid_price),
                }
            )
        if state.first_touch_time is not None and not state.broken:
            state.retest_low = min(state.retest_low or snap.mid_price, snap.mid_price)
            state.undershoot_bps = max(
                0.0,
                float((level - state.retest_low) / level * Decimal("10000")),
            )
            if state.undershoot_bps > self.params.retest_max_undershoot_bps:
                state.broken = True
                state.reclaim_snapshot_count = 0
                self.transitions.append(
                    {
                        "timestamp": snap.timestamp.isoformat(),
                        "transition": "RETEST_BROKEN",
                        "reference_level": _fmt(level),
                        "undershoot_bps": round(state.undershoot_bps, 6),
                    }
                )
            elif snap.mid_price >= level:
                if state.reclaim_snapshot_count == 0:
                    state.reclaim_start_time = snap.timestamp
                state.reclaim_snapshot_count += 1
                if previous is not None:
                    delta_improving = (
                        snap.trade_delta_notional > previous.trade_delta_notional
                        or snap.trade_delta_notional > 0
                    )
                    oi_confirm = (
                        snap.oi_change_since_prev is not None
                        and snap.oi_change_since_prev >= 0
                    )
                    absorption_confirm = (
                        snap.trade_delta_notional < 0
                        and abs(
                            float(
                                (snap.mid_price - previous.mid_price)
                                / previous.mid_price
                                * Decimal("100")
                            )
                        )
                        <= 0.02
                    )
                    state.flow_confirmed = (
                        state.flow_confirmed
                        or delta_improving
                        or oi_confirm
                        or absorption_confirm
                    )
                if (
                    state.reclaim_snapshot_count >= self.params.reclaim_confirm_snapshots
                    and state.reclaim_confirm_time is None
                ):
                    state.reclaim_confirm_time = snap.timestamp
                    self.transitions.append(
                        {
                            "timestamp": snap.timestamp.isoformat(),
                            "transition": "RECLAIM_CONFIRMED",
                            "reference_level": _fmt(level),
                            "snapshot_count": state.reclaim_snapshot_count,
                        }
                    )
            else:
                state.reclaim_snapshot_count = 0
                state.reclaim_start_time = None

            if snap.nearest_bid is not None and snap.nearest_bid.price < snap.mid_price:
                state.local_support_level = snap.nearest_bid.price
                state.local_support_notional = snap.nearest_bid.notional

        self.context.append(
            {
                "timestamp": snap.timestamp.isoformat(),
                "reference_level": _fmt(level),
                "mid": _fmt(snap.mid_price),
                "first_touch_time": None
                if state.first_touch_time is None
                else state.first_touch_time.isoformat(),
                "retest_low": _fmt(state.retest_low),
                "undershoot_bps": state.undershoot_bps,
                "reclaim_snapshot_count": state.reclaim_snapshot_count,
                "reclaim_confirm_time": None
                if state.reclaim_confirm_time is None
                else state.reclaim_confirm_time.isoformat(),
                "flow_confirmed": state.flow_confirmed,
                "broken": state.broken,
            }
        )
        return state


@dataclass
class AcceptedCandidate:
    candidate_id: str
    episode_id: str
    decision: CandidateDecision
    entry_time: datetime
    entry_price: Decimal
    stop_loss: Decimal
    take_profit_1: Decimal | None
    take_profit_2: Decimal | None
    sl_reference_type: str
    sl_reference_level: Decimal
    sl_buffer_bps: float
    stop_distance_pct: float
    stop_risk_per_unit: Decimal
    tp1_reference_type: str | None
    tp1_reference_level: Decimal | None
    tp1_distance_pct: float | None
    tp1_crv: float | None
    tp2_reference_type: str | None
    tp2_reference_level: Decimal | None
    tp2_distance_pct: float | None
    tp2_crv: float | None


def sequences_as_of(
    sequences: Sequence[SequenceRecord], *, as_of: datetime
) -> list[SequenceRecord]:
    t = _ensure_utc(as_of)
    return [s for s in sequences if _ensure_utc(s.sequence_end) <= t]


def transitions_as_of(
    transitions: Sequence[TransitionRecord], *, as_of: datetime
) -> list[TransitionRecord]:
    t = _ensure_utc(as_of)
    return [x for x in transitions if _ensure_utc(x.current_timestamp) <= t]


def near_tx_as_of(
    transitions: Sequence[NearAskTransition], *, as_of: datetime
) -> list[NearAskTransition]:
    t = _ensure_utc(as_of)
    return [x for x in transitions if _ensure_utc(x.current_timestamp) <= t]


def latest_sequence(
    sequences: Sequence[SequenceRecord], *, label: str, as_of: datetime
) -> SequenceRecord | None:
    matched = [
        s
        for s in sequences_as_of(sequences, as_of=as_of)
        if s.classification == label
    ]
    if not matched:
        return None
    return max(matched, key=lambda s: s.sequence_end)


def aged_sequence(
    sequences: Sequence[SequenceRecord],
    label: str,
    as_of: datetime,
    max_age_seconds: int,
) -> tuple[SequenceRecord | None, float | None, bool]:
    """Return latest causal sequence, its age, and whether it is inactive."""
    seq = latest_sequence(sequences, label=label, as_of=as_of)
    if seq is None:
        return None, None, True
    age = (_ensure_utc(as_of) - _ensure_utc(seq.sequence_end)).total_seconds()
    return seq, age, age > max_age_seconds


def recent_near_ask_class(
    near_tx: Sequence[NearAskTransition], *, as_of: datetime
) -> str | None:
    recent = near_tx_as_of(near_tx, as_of=as_of)
    if not recent:
        return None
    return recent[-1].classification


def cumulative_delta(
    snapshots: Sequence[SnapshotRecord], *, end_index: int, lookback: int
) -> Decimal:
    start = max(0, end_index - lookback + 1)
    total = Decimal("0")
    for snap in snapshots[start : end_index + 1]:
        total += snap.trade_delta_notional
    return total


def cumulative_oi_change(
    snapshots: Sequence[SnapshotRecord], *, end_index: int, lookback: int
) -> Decimal | None:
    start = max(0, end_index - lookback + 1)
    changes = [
        s.oi_change_since_prev
        for s in snapshots[start : end_index + 1]
        if s.oi_change_since_prev is not None
    ]
    if not changes:
        return None
    return sum(changes, Decimal("0"))


def price_trend(
    snapshots: Sequence[SnapshotRecord], *, end_index: int, lookback: int
) -> str:
    """Return UP / DOWN / FLAT using mid change over lookback."""
    if end_index <= 0:
        return "FLAT"
    start = max(0, end_index - lookback)
    a = snapshots[start].mid_price
    b = snapshots[end_index].mid_price
    if a <= 0:
        return "FLAT"
    chg = float((b - a) / a * 100)
    if chg > 0.02:
        return "UP"
    if chg < -0.02:
        return "DOWN"
    return "FLAT"


def build_regime_at(
    snapshots: Sequence[SnapshotRecord],
    near_views: Sequence[NearSnapshotView],
    near_tx: Sequence[NearAskTransition],
    ladder_seqs: Sequence[Any],
    *,
    index: int,
) -> dict[str, Any]:
    """Causal near-regime summary using only samples ``<= index``."""
    if index < 0:
        return {
            "auction_direction": "INCONCLUSIVE",
            "short_term_bias": "INCONCLUSIVE",
            "near_bid_direction": "INCONCLUSIVE",
            "near_ask_direction": "INCONCLUSIVE",
            "near_bid_strength_change": "INCONCLUSIVE",
            "near_ask_strength_change": "INCONCLUSIVE",
        }
    as_of = snapshots[index].timestamp
    causal_ladder = [
        s for s in ladder_seqs if _ensure_utc(s.sequence_end) <= _ensure_utc(as_of)
    ]
    causal_tx = near_tx_as_of(near_tx, as_of=as_of)
    return summarize_near_regime(
        snapshots[: index + 1],
        near_views[: index + 1],
        causal_tx,
        causal_ladder,
    )


def liquidations_as_of(
    events: Sequence[LiquidationEvent], *, as_of: datetime
) -> list[LiquidationEvent]:
    t = _ensure_utc(as_of)
    return [e for e in events if _ensure_utc(e.exchange_timestamp) <= t]


def liquidation_confirmation(
    events: Sequence[LiquidationEvent],
    *,
    side: str,
    as_of: datetime,
    mid: Decimal,
    nearest_bid: Decimal | None,
    nearest_ask: Decimal | None,
    wall_bps: float,
    lookback_seconds: int = 180,
) -> str | None:
    """Optional confirmation only — never sufficient alone for entry."""
    t = _ensure_utc(as_of)
    window_start = t - timedelta(seconds=lookback_seconds)
    recent = [
        e
        for e in liquidations_as_of(events, as_of=as_of)
        if _ensure_utc(e.exchange_timestamp) >= window_start
    ]
    if not recent:
        return None
    if side == LONG:
        for e in recent:
            if e.interpreted_position_side != LIQUIDATED_SHORT:
                continue
            labels = wall_relation_labels(
                liq_price=e.bankruptcy_price,
                nearest_bid=nearest_bid,
                nearest_ask=nearest_ask,
                bps=wall_bps,
            )
            if LIQUIDATION_THROUGH_ASK in labels:
                return "LIQUIDATED_SHORT+THROUGH_ASK"
            return "LIQUIDATED_SHORT"
    if side == SHORT:
        for e in recent:
            if e.interpreted_position_side != LIQUIDATED_LONG:
                continue
            labels = wall_relation_labels(
                liq_price=e.bankruptcy_price,
                nearest_bid=nearest_bid,
                nearest_ask=nearest_ask,
                bps=wall_bps,
            )
            if LIQUIDATION_THROUGH_BID in labels:
                return "LIQUIDATED_LONG+THROUGH_BID"
            return "LIQUIDATED_LONG"
    return None


def score_long(
    *,
    rising_bid_shifts: int,
    near_ask_class: str | None,
    auction: str,
    bias: str,
    delta: Decimal,
    oi_chg: Decimal | None,
    liq_confirm: str | None,
    falling_ask_shifts: int,
    falling_bid_shifts: int,
    params: AuditParams,
) -> tuple[int, list[ScoreComponent]]:
    comps: list[ScoreComponent] = []
    if rising_bid_shifts >= 2:
        comps.append(ScoreComponent("rising_bid_floor", 2, f"shifts={rising_bid_shifts}"))
    if near_ask_class == NEAR_ASK_MOVING_HIGHER:
        comps.append(ScoreComponent("near_ask_moving_higher", 2))
    if near_ask_class == NEAR_ASK_THINNING:
        comps.append(ScoreComponent("near_ask_thinning", 1))
    if auction == "HIGHER":
        comps.append(ScoreComponent("auction_higher", 2))
    if bias == BULLISH_LIQUIDITY_SHIFT:
        comps.append(ScoreComponent("bullish_bias", 1, bias))
    if delta > 0:
        comps.append(ScoreComponent("positive_delta", 1, _fmt(delta) or ""))
    if oi_chg is not None and oi_chg >= 0:
        comps.append(ScoreComponent("oi_stable_or_up", 1, _fmt(oi_chg) or ""))
    if liq_confirm:
        comps.append(ScoreComponent("short_liq_confirm", 1, liq_confirm))
    if falling_ask_shifts >= 2:
        comps.append(ScoreComponent("falling_ask_ceiling", -2, f"shifts={falling_ask_shifts}"))
    if falling_bid_shifts >= 1:
        comps.append(ScoreComponent("falling_bid_floor", -3, f"shifts={falling_bid_shifts}"))
    if delta < Decimal(str(-params.strong_delta_notional)) and oi_chg is not None and oi_chg > 0:
        comps.append(ScoreComponent("neg_delta_rising_oi", -2))
    return sum(c.points for c in comps), comps


def score_short(
    *,
    falling_ask_shifts: int,
    near_ask_class: str | None,
    auction: str,
    bias: str,
    delta: Decimal,
    oi_chg: Decimal | None,
    liq_confirm: str | None,
    rising_bid_shifts: int,
    bid_falling: bool,
    params: AuditParams,
) -> tuple[int, list[ScoreComponent]]:
    comps: list[ScoreComponent] = []
    if falling_ask_shifts >= 1 or near_ask_class == NEAR_ASK_MOVING_LOWER:
        comps.append(
            ScoreComponent(
                "falling_ask_or_near_ask_lower",
                2,
                f"ask_shifts={falling_ask_shifts}; near={near_ask_class}",
            )
        )
    if bid_falling:
        comps.append(ScoreComponent("bid_floor_falling", 2))
    if auction == "LOWER":
        comps.append(ScoreComponent("auction_lower", 2))
    if bias == BEARISH_LIQUIDITY_SHIFT:
        comps.append(ScoreComponent("bearish_bias", 1, bias))
    if delta < 0:
        comps.append(ScoreComponent("negative_delta", 1, _fmt(delta) or ""))
    if oi_chg is not None and oi_chg >= 0:
        comps.append(ScoreComponent("oi_stable_or_up", 1, _fmt(oi_chg) or ""))
    if liq_confirm:
        comps.append(ScoreComponent("long_liq_confirm", 1, liq_confirm))
    if rising_bid_shifts >= 2:
        comps.append(ScoreComponent("rising_bid_floor", -3, f"shifts={rising_bid_shifts}"))
    if near_ask_class == NEAR_ASK_MOVING_HIGHER:
        comps.append(ScoreComponent("near_ask_moving_higher", -2))
    if delta > Decimal(str(params.strong_delta_notional)) and oi_chg is not None and oi_chg > 0:
        comps.append(ScoreComponent("pos_delta_rising_oi", -2))
    return sum(c.points for c in comps), comps


def _evaluate_candidate_at_legacy(
    *,
    index: int,
    snapshots: Sequence[SnapshotRecord],
    near_views: Sequence[NearSnapshotView],
    sequences: Sequence[SequenceRecord],
    transitions: Sequence[TransitionRecord],
    near_tx: Sequence[NearAskTransition],
    ladder_seqs: Sequence[Any],
    liquidations: Sequence[LiquidationEvent],
    params: AuditParams,
) -> CandidateDecision:
    snap = snapshots[index]
    as_of = snap.timestamp
    regime = build_regime_at(
        snapshots, near_views, near_tx, ladder_seqs, index=index
    )
    auction = str(regime.get("auction_direction") or "INCONCLUSIVE")
    bias = str(regime.get("short_term_bias") or "INCONCLUSIVE")
    near_ask_dir = str(regime.get("near_ask_direction") or "INCONCLUSIVE")
    near_bid_dir = str(regime.get("near_bid_direction") or "INCONCLUSIVE")
    near_bid_str = str(regime.get("near_bid_strength_change") or "INCONCLUSIVE")
    near_ask_str = str(regime.get("near_ask_strength_change") or "INCONCLUSIVE")

    rising = latest_sequence(sequences, label=RISING_BID_FLOOR, as_of=as_of)
    falling_bid = latest_sequence(sequences, label=FALLING_BID_FLOOR, as_of=as_of)
    falling_ask = latest_sequence(sequences, label=FALLING_ASK_CEILING, as_of=as_of)
    rising_ask = latest_sequence(sequences, label=RISING_ASK_CEILING, as_of=as_of)

    rising_shifts = rising.number_of_shifts if rising else 0
    falling_bid_shifts = falling_bid.number_of_shifts if falling_bid else 0
    falling_ask_shifts = falling_ask.number_of_shifts if falling_ask else 0

    near_class = recent_near_ask_class(near_tx, as_of=as_of)
    delta = cumulative_delta(
        snapshots, end_index=index, lookback=params.flow_lookback_snapshots
    )
    oi_chg = cumulative_oi_change(
        snapshots, end_index=index, lookback=params.flow_lookback_snapshots
    )
    trend = price_trend(
        snapshots, end_index=index, lookback=params.flow_lookback_snapshots
    )

    nb = snap.nearest_bid.price if snap.nearest_bid else None
    na = snap.nearest_ask.price if snap.nearest_ask else None
    db = snap.dominant_bid.price if snap.dominant_bid else (
        snap.strongest_bid.price if snap.strongest_bid else None
    )
    da = snap.dominant_ask.price if snap.dominant_ask else (
        snap.strongest_ask.price if snap.strongest_ask else None
    )

    # --- LONG structure ---
    support_ok = rising_shifts >= 2 or (
        near_bid_str in {"STRONGER", "STABLE"} and near_bid_dir != "LOWER" and falling_bid_shifts == 0
    )
    ask_ok = near_class in {NEAR_ASK_MOVING_HIGHER, NEAR_ASK_THINNING} or near_ask_dir == "HIGHER"
    ask_block = near_class == NEAR_ASK_MOVING_LOWER
    flow_ok = trend in {"UP", "FLAT"} and delta > 0 and (oi_chg is None or oi_chg >= 0)
    auction_ok = auction == "HIGHER" or bias == BULLISH_LIQUIDITY_SHIFT
    long_contradiction = (
        falling_bid_shifts >= 1
        or bias == BEARISH_LIQUIDITY_SHIFT
        or (near_ask_dir == "LOWER" and near_ask_str == "STRONGER")
        or (
            delta < Decimal(str(-params.strong_delta_notional))
            and oi_chg is not None
            and oi_chg > 0
        )
    )
    liq_long = liquidation_confirmation(
        liquidations,
        side=LONG,
        as_of=as_of,
        mid=snap.mid_price,
        nearest_bid=nb,
        nearest_ask=na,
        wall_bps=params.wall_relation_bps,
    )
    long_structure = support_ok and ask_ok and not ask_block and flow_ok and auction_ok and not long_contradiction

    # --- SHORT structure ---
    resist_ok = falling_ask_shifts >= 1 or near_class == NEAR_ASK_MOVING_LOWER or (
        near_ask_str in {"STRONGER", "STABLE"} and near_ask_dir in {"LOWER", "STABLE"}
    )
    bid_ok = (
        falling_bid_shifts >= 1
        or near_bid_dir == "LOWER"
        or near_bid_str == "WEAKER"
    )
    short_flow_ok = trend in {"DOWN", "FLAT"} and delta < 0 and (oi_chg is None or oi_chg >= 0)
    short_auction_ok = auction == "LOWER" or bias == BEARISH_LIQUIDITY_SHIFT
    short_contradiction = (
        rising_shifts >= 2
        or bias == BULLISH_LIQUIDITY_SHIFT
        or (near_ask_dir == "HIGHER" and near_class == NEAR_ASK_THINNING)
        or (
            delta > Decimal(str(params.strong_delta_notional))
            and oi_chg is not None
            and oi_chg > 0
        )
    )
    liq_short = liquidation_confirmation(
        liquidations,
        side=SHORT,
        as_of=as_of,
        mid=snap.mid_price,
        nearest_bid=nb,
        nearest_ask=na,
        wall_bps=params.wall_relation_bps,
    )
    short_structure = (
        resist_ok
        and bid_ok
        and rising_shifts < 2
        and short_flow_ok
        and short_auction_ok
        and not short_contradiction
    )

    # Liquidation alone must not create entry
    if not long_structure and not short_structure:
        return CandidateDecision(
            signal_time=as_of,
            side=NO_TRADE,
            reason=NO_TRADE_NO_STRUCTURE,
            score=0,
            components=[],
            snapshot_index=index,
            rising_bid_shifts=rising_shifts,
            falling_ask_shifts=falling_ask_shifts,
            auction_direction=auction,
            short_term_bias=bias,
            near_ask_class=near_class,
            trade_delta=delta,
            oi_change=oi_chg,
            mid=snap.mid_price,
            nearest_bid=nb,
            nearest_ask=na,
            dominant_bid=db,
            dominant_ask=da,
            liquidation_confirm=liq_long or liq_short,
        )

    if long_structure and short_structure:
        return CandidateDecision(
            signal_time=as_of,
            side=NO_TRADE,
            reason=NO_TRADE_CONTRADICTION,
            score=0,
            components=[],
            snapshot_index=index,
            rising_bid_shifts=rising_shifts,
            falling_ask_shifts=falling_ask_shifts,
            auction_direction=auction,
            short_term_bias=bias,
            near_ask_class=near_class,
            trade_delta=delta,
            oi_change=oi_chg,
            mid=snap.mid_price,
            nearest_bid=nb,
            nearest_ask=na,
            dominant_bid=db,
            dominant_ask=da,
        )

    if long_structure:
        score, comps = score_long(
            rising_bid_shifts=rising_shifts,
            near_ask_class=near_class,
            auction=auction,
            bias=bias,
            delta=delta,
            oi_chg=oi_chg,
            liq_confirm=liq_long,
            falling_ask_shifts=falling_ask_shifts,
            falling_bid_shifts=falling_bid_shifts,
            params=params,
        )
        if score < params.minimum_entry_score:
            return CandidateDecision(
                signal_time=as_of,
                side=NO_TRADE,
                reason=NO_TRADE_LOW_SCORE,
                score=score,
                components=comps,
                snapshot_index=index,
                rising_bid_shifts=rising_shifts,
                falling_ask_shifts=falling_ask_shifts,
                auction_direction=auction,
                short_term_bias=bias,
                near_ask_class=near_class,
                trade_delta=delta,
                oi_change=oi_chg,
                mid=snap.mid_price,
                nearest_bid=nb,
                nearest_ask=na,
                dominant_bid=db,
                dominant_ask=da,
                liquidation_confirm=liq_long,
            )
        return CandidateDecision(
            signal_time=as_of,
            side=LONG,
            reason="LONG_STRUCTURE",
            score=score,
            components=comps,
            snapshot_index=index,
            rising_bid_shifts=rising_shifts,
            falling_ask_shifts=falling_ask_shifts,
            auction_direction=auction,
            short_term_bias=bias,
            near_ask_class=near_class,
            trade_delta=delta,
            oi_change=oi_chg,
            mid=snap.mid_price,
            nearest_bid=nb,
            nearest_ask=na,
            dominant_bid=db,
            dominant_ask=da,
            liquidation_confirm=liq_long,
        )

    score, comps = score_short(
        falling_ask_shifts=falling_ask_shifts,
        near_ask_class=near_class,
        auction=auction,
        bias=bias,
        delta=delta,
        oi_chg=oi_chg,
        liq_confirm=liq_short,
        rising_bid_shifts=rising_shifts,
        bid_falling=falling_bid_shifts >= 1 or near_bid_dir == "LOWER",
        params=params,
    )
    if score < params.minimum_entry_score:
        return CandidateDecision(
            signal_time=as_of,
            side=NO_TRADE,
            reason=NO_TRADE_LOW_SCORE,
            score=score,
            components=comps,
            snapshot_index=index,
            rising_bid_shifts=rising_shifts,
            falling_ask_shifts=falling_ask_shifts,
            auction_direction=auction,
            short_term_bias=bias,
            near_ask_class=near_class,
            trade_delta=delta,
            oi_change=oi_chg,
            mid=snap.mid_price,
            nearest_bid=nb,
            nearest_ask=na,
            dominant_bid=db,
            dominant_ask=da,
            liquidation_confirm=liq_short,
        )
    return CandidateDecision(
        signal_time=as_of,
        side=SHORT,
        reason="SHORT_STRUCTURE",
        score=score,
        components=comps,
        snapshot_index=index,
        rising_bid_shifts=rising_shifts,
        falling_ask_shifts=falling_ask_shifts,
        auction_direction=auction,
        short_term_bias=bias,
        near_ask_class=near_class,
        trade_delta=delta,
        oi_change=oi_chg,
        mid=snap.mid_price,
        nearest_bid=nb,
        nearest_ask=na,
        dominant_bid=db,
        dominant_ask=da,
        liquidation_confirm=liq_short,
    )


def evaluate_candidate_at(
    *,
    index: int,
    snapshots: Sequence[SnapshotRecord],
    near_views: Sequence[NearSnapshotView],
    sequences: Sequence[SequenceRecord],
    transitions: Sequence[TransitionRecord],
    near_tx: Sequence[NearAskTransition],
    ladder_seqs: Sequence[Any],
    liquidations: Sequence[LiquidationEvent],
    params: AuditParams,
    retest_state: RetestState | None = None,
) -> CandidateDecision:
    """Evaluate one snapshot using only causal, age-bounded state."""
    del transitions  # retained in the public interface for compatibility
    snap = snapshots[index]
    as_of = snap.timestamp
    regime = build_regime_at(
        snapshots, near_views, near_tx, ladder_seqs, index=index
    )
    auction = str(regime.get("auction_direction") or "INCONCLUSIVE")
    bias = str(regime.get("short_term_bias") or "INCONCLUSIVE")
    near_ask_dir = str(regime.get("near_ask_direction") or "INCONCLUSIVE")
    near_bid_dir = str(regime.get("near_bid_direction") or "INCONCLUSIVE")
    near_bid_str = str(regime.get("near_bid_strength_change") or "INCONCLUSIVE")
    near_ask_str = str(regime.get("near_ask_strength_change") or "INCONCLUSIVE")

    rising_bid, rising_bid_age, rising_bid_expired = aged_sequence(
        sequences,
        RISING_BID_FLOOR,
        as_of,
        params.structure_state_max_age_seconds,
    )
    rising_ask, rising_ask_age, rising_ask_expired = aged_sequence(
        sequences,
        RISING_ASK_CEILING,
        as_of,
        params.structure_state_max_age_seconds,
    )
    falling_bid, falling_bid_age, falling_bid_expired = aged_sequence(
        sequences,
        FALLING_BID_FLOOR,
        as_of,
        params.contradiction_state_max_age_seconds,
    )
    historical_falling_ask, falling_ask_age, falling_ask_expired = aged_sequence(
        sequences,
        FALLING_ASK_CEILING,
        as_of,
        params.contradiction_state_max_age_seconds,
    )

    active_rising_bid_shifts = (
        0 if rising_bid is None or rising_bid_expired else rising_bid.number_of_shifts
    )
    active_rising_ask_shifts = (
        0 if rising_ask is None or rising_ask_expired else rising_ask.number_of_shifts
    )
    active_falling_bid_shifts = (
        0 if falling_bid is None or falling_bid_expired else falling_bid.number_of_shifts
    )
    active_falling_ask_shifts = (
        0
        if historical_falling_ask is None or falling_ask_expired
        else historical_falling_ask.number_of_shifts
    )
    expired_falling_ask_shifts = (
        historical_falling_ask.number_of_shifts
        if historical_falling_ask is not None and falling_ask_expired
        else 0
    )

    near_class = recent_near_ask_class(near_tx, as_of=as_of)
    delta = cumulative_delta(
        snapshots, end_index=index, lookback=params.flow_lookback_snapshots
    )
    oi_chg = cumulative_oi_change(
        snapshots, end_index=index, lookback=params.flow_lookback_snapshots
    )
    trend = price_trend(
        snapshots, end_index=index, lookback=params.flow_lookback_snapshots
    )
    nb = snap.nearest_bid.price if snap.nearest_bid else None
    na = snap.nearest_ask.price if snap.nearest_ask else None
    db = snap.dominant_bid.price if snap.dominant_bid else (
        snap.strongest_bid.price if snap.strongest_bid else None
    )
    da = snap.dominant_ask.price if snap.dominant_ask else (
        snap.strongest_ask.price if snap.strongest_ask else None
    )

    reclaim_a = (
        retest_state is not None
        and retest_state.reclaim_snapshot_count >= params.reclaim_confirm_snapshots
        and auction == "HIGHER"
        and active_rising_bid_shifts >= 2
    )
    reclaim_b = (
        near_class == NEAR_ASK_MOVING_HIGHER
        and near_bid_dir in {"HIGHER", "STABLE", "INCONCLUSIVE"}
        and active_falling_bid_shifts == 0
    )
    lower_after_reclaim = False
    if retest_state is not None and retest_state.reclaim_start_time is not None:
        lower_after_reclaim = any(
            tx.classification == NEAR_ASK_MOVING_LOWER
            and _ensure_utc(tx.current_timestamp)
            >= _ensure_utc(retest_state.reclaim_start_time)
            for tx in near_tx_as_of(near_tx, as_of=as_of)
        )
    reclaim_c = (
        retest_state is not None
        and retest_state.reclaim_confirm_time is not None
        and snap.mid_price >= retest_state.reference_level
        and not lower_after_reclaim
    )
    reclaim_confirmed = reclaim_a or reclaim_b or reclaim_c
    reclaim_reason = (
        "A_STABLE_NEAR_ASK_AUCTION_HIGHER_RISING_BID"
        if reclaim_a
        else "B_NEAR_ASK_HIGHER_BID_STABLE_OR_RISING"
        if reclaim_b
        else "C_PRICE_RECLAIM_NO_NEW_LOWER_NEAR_ASK"
        if reclaim_c
        else None
    )
    falling_ask_blocker_active = active_falling_ask_shifts > 0
    falling_ask_blocker_expired = expired_falling_ask_shifts > 0

    source_times = [
        s.sequence_end
        for s, expired in (
            (rising_bid, rising_bid_expired),
            (rising_ask, rising_ask_expired),
            (falling_bid, falling_bid_expired),
            (historical_falling_ask, falling_ask_expired),
        )
        if s is not None and not expired
    ]

    common: dict[str, Any] = {
        "signal_time": as_of,
        "snapshot_index": index,
        "rising_bid_shifts": active_rising_bid_shifts,
        "falling_ask_shifts": (
            historical_falling_ask.number_of_shifts
            if historical_falling_ask is not None
            else 0
        ),
        "auction_direction": auction,
        "short_term_bias": bias,
        "near_ask_class": near_class,
        "trade_delta": delta,
        "oi_change": oi_chg,
        "mid": snap.mid_price,
        "nearest_bid": nb,
        "nearest_ask": na,
        "dominant_bid": db,
        "dominant_ask": da,
        "rising_bid_state_age_seconds": rising_bid_age,
        "falling_bid_state_age_seconds": falling_bid_age,
        "rising_ask_state_age_seconds": rising_ask_age,
        "falling_ask_state_age_seconds": falling_ask_age,
        "active_rising_bid_shifts": active_rising_bid_shifts,
        "active_falling_ask_shifts": active_falling_ask_shifts,
        "expired_falling_ask_shifts": expired_falling_ask_shifts,
        "active_structure_source_timestamp": max(source_times)
        if source_times
        else None,
        "falling_ask_blocker_active": falling_ask_blocker_active,
        "falling_ask_blocker_expired": falling_ask_blocker_expired,
        "falling_ask_reclaim_confirmed": reclaim_confirmed,
        "falling_ask_reclaim_reason": reclaim_reason,
    }
    if retest_state is not None:
        common.update(
            {
                "retest_reference_type": retest_state.reference_type,
                "retest_reference_level": retest_state.reference_level,
                "retest_first_touch_time": retest_state.first_touch_time,
                "retest_low": retest_state.retest_low,
                "retest_undershoot_bps": retest_state.undershoot_bps,
                "reclaim_start_time": retest_state.reclaim_start_time,
                "reclaim_confirm_time": retest_state.reclaim_confirm_time,
                "reclaim_snapshot_count": retest_state.reclaim_snapshot_count,
                "local_support_level": retest_state.local_support_level,
                "local_support_notional": retest_state.local_support_notional,
            }
        )

    liq_long = liquidation_confirmation(
        liquidations,
        side=LONG,
        as_of=as_of,
        mid=snap.mid_price,
        nearest_bid=nb,
        nearest_ask=na,
        wall_bps=params.wall_relation_bps,
    )
    liq_short = liquidation_confirmation(
        liquidations,
        side=SHORT,
        as_of=as_of,
        mid=snap.mid_price,
        nearest_bid=nb,
        nearest_ask=na,
        wall_bps=params.wall_relation_bps,
    )

    # Retest setup is evaluated before the legacy continuation setup. Pullback
    # delta may still be negative; confirmation requires improving flow.
    if retest_state is not None and retest_state.first_touch_time is not None:
        comps = [
            ScoreComponent("retest_level_identified", 2),
            ScoreComponent("support_held", 2 if not retest_state.broken else -4),
        ]
        if retest_state.reclaim_confirm_time is not None:
            comps.append(ScoreComponent("reclaim_confirmed", 2))
        if auction == "HIGHER":
            comps.append(ScoreComponent("auction_higher", 1))
        if bias == BULLISH_LIQUIDITY_SHIFT:
            comps.append(ScoreComponent("bullish_liquidity_shift", 1))
        if nb is not None and nb < snap.mid_price and near_bid_dir != "LOWER":
            comps.append(ScoreComponent("near_bid_support", 1))
        if retest_state.flow_confirmed:
            comps.append(ScoreComponent("delta_improving", 1))
        if oi_chg is not None and oi_chg >= 0:
            comps.append(ScoreComponent("oi_stable_or_up", 1))
        if falling_ask_blocker_active:
            comps.append(ScoreComponent("active_falling_ask", -3))
        if active_falling_bid_shifts:
            comps.append(ScoreComponent("new_bid_breakdown", -4))
        retest_score = sum(c.points for c in comps)
        if retest_state.broken:
            return CandidateDecision(
                side=NO_TRADE,
                reason=NO_TRADE_RETEST_BROKEN,
                score=retest_score,
                components=comps,
                **common,
            )
        if (
            retest_state.reclaim_confirm_time is None
            or retest_state.reclaim_snapshot_count < params.reclaim_confirm_snapshots
        ):
            return CandidateDecision(
                side=NO_TRADE,
                reason=NO_TRADE_RETEST_NOT_CONFIRMED,
                score=retest_score,
                components=comps,
                **common,
            )
        if falling_ask_blocker_active:
            return CandidateDecision(
                side=NO_TRADE,
                reason=NO_TRADE_ACTIVE_FALLING_ASK_BLOCKER,
                score=retest_score,
                components=comps,
                **common,
            )
        if active_falling_bid_shifts or bias == BEARISH_LIQUIDITY_SHIFT:
            return CandidateDecision(
                side=NO_TRADE,
                reason=NO_TRADE_CONFLICTING_STRUCTURE,
                score=retest_score,
                components=comps,
                **common,
            )
        if not retest_state.flow_confirmed:
            return CandidateDecision(
                side=NO_TRADE,
                reason=NO_TRADE_FLOW_NOT_CONFIRMED,
                score=retest_score,
                components=comps,
                **common,
            )
        if not retest_state.emitted and retest_score >= params.minimum_entry_score:
            retest_state.emitted = True
            common["entry_setup_type"] = SUPPORT_RETEST_RECLAIM_LONG
            return CandidateDecision(
                side=LONG,
                reason=SUPPORT_RETEST_RECLAIM_LONG,
                score=retest_score,
                components=comps,
                liquidation_confirm=liq_long,
                **common,
            )

    support_feature = active_rising_bid_shifts >= 2 or (
        near_bid_str in {"STRONGER", "STABLE"}
        and near_bid_dir != "LOWER"
        and active_falling_bid_shifts == 0
    )
    ask_feature = (
        near_class in {NEAR_ASK_MOVING_HIGHER, NEAR_ASK_THINNING}
        or near_ask_dir == "HIGHER"
    )
    bullish_raw = support_feature or ask_feature or auction == "HIGHER" or bias == BULLISH_LIQUIDITY_SHIFT
    bearish_raw = (
        active_falling_ask_shifts > 0
        or active_falling_bid_shifts > 0
        or near_class == NEAR_ASK_MOVING_LOWER
        or auction == "LOWER"
        or bias == BEARISH_LIQUIDITY_SHIFT
    )

    long_score, long_comps = score_long(
        rising_bid_shifts=active_rising_bid_shifts,
        near_ask_class=near_class,
        auction=auction,
        bias=bias,
        delta=delta,
        oi_chg=oi_chg,
        liq_confirm=liq_long,
        falling_ask_shifts=active_falling_ask_shifts,
        falling_bid_shifts=active_falling_bid_shifts,
        params=params,
    )
    short_score, short_comps = score_short(
        falling_ask_shifts=active_falling_ask_shifts,
        near_ask_class=near_class,
        auction=auction,
        bias=bias,
        delta=delta,
        oi_chg=oi_chg,
        liq_confirm=liq_short,
        rising_bid_shifts=active_rising_bid_shifts,
        bid_falling=active_falling_bid_shifts > 0 or near_bid_dir == "LOWER",
        params=params,
    )

    long_flow = trend in {"UP", "FLAT"} and delta > 0 and (oi_chg is None or oi_chg >= 0)
    short_flow = trend in {"DOWN", "FLAT"} and delta < 0 and (oi_chg is None or oi_chg >= 0)
    long_structure = (
        support_feature
        and ask_feature
        and long_flow
        and (auction == "HIGHER" or bias == BULLISH_LIQUIDITY_SHIFT)
        and active_falling_bid_shifts == 0
        and not falling_ask_blocker_active
        and bias != BEARISH_LIQUIDITY_SHIFT
    )
    short_structure = (
        bearish_raw
        and (active_falling_bid_shifts > 0 or near_bid_dir == "LOWER" or near_bid_str == "WEAKER")
        and short_flow
        and (auction == "LOWER" or bias == BEARISH_LIQUIDITY_SHIFT)
        and active_rising_bid_shifts < 2
        and bias != BULLISH_LIQUIDITY_SHIFT
    )

    if long_structure and short_structure:
        return CandidateDecision(
            side=NO_TRADE,
            reason=NO_TRADE_CONFLICTING_STRUCTURE,
            score=max(long_score, short_score),
            components=long_comps if long_score >= short_score else short_comps,
            **common,
        )
    if long_structure:
        if long_score >= params.minimum_entry_score:
            return CandidateDecision(
                side=LONG,
                reason="LONG_STRUCTURE",
                score=long_score,
                components=long_comps,
                liquidation_confirm=liq_long,
                **common,
            )
        return CandidateDecision(
            side=NO_TRADE,
            reason=NO_TRADE_LOW_SCORE,
            score=long_score,
            components=long_comps,
            **common,
        )
    if short_structure:
        if short_score >= params.minimum_entry_score:
            return CandidateDecision(
                side=SHORT,
                reason="SHORT_STRUCTURE",
                score=short_score,
                components=short_comps,
                liquidation_confirm=liq_short,
                **common,
            )
        return CandidateDecision(
            side=NO_TRADE,
            reason=NO_TRADE_LOW_SCORE,
            score=short_score,
            components=short_comps,
            **common,
        )

    if bullish_raw:
        if falling_ask_blocker_active:
            reason = NO_TRADE_ACTIVE_FALLING_ASK_BLOCKER
        elif active_falling_bid_shifts or bias == BEARISH_LIQUIDITY_SHIFT:
            reason = NO_TRADE_CONFLICTING_STRUCTURE
        elif not long_flow:
            reason = NO_TRADE_FLOW_NOT_CONFIRMED
        else:
            reason = NO_TRADE_LOW_SCORE
        return CandidateDecision(
            side=NO_TRADE,
            reason=reason,
            score=long_score,
            components=long_comps,
            **common,
        )
    if bearish_raw:
        reason = (
            NO_TRADE_CONFLICTING_STRUCTURE
            if active_rising_bid_shifts >= 2 or bias == BULLISH_LIQUIDITY_SHIFT
            else NO_TRADE_FLOW_NOT_CONFIRMED
            if not short_flow
            else NO_TRADE_LOW_SCORE
        )
        return CandidateDecision(
            side=NO_TRADE,
            reason=reason,
            score=short_score,
            components=short_comps,
            **common,
        )
    return CandidateDecision(
        side=NO_TRADE,
        reason=NO_TRADE_NO_STRUCTURE,
        score=0,
        components=[],
        **common,
    )


def resolve_entry_price(
    *,
    mode: str,
    signal_index: int,
    snapshots: Sequence[SnapshotRecord],
    trades: Sequence[tuple[datetime, Decimal]],
) -> tuple[datetime, Decimal] | None:
    signal = snapshots[signal_index]
    mode = mode.strip().lower().replace("_", "-")
    if mode == "mid":
        return signal.timestamp, signal.mid_price
    if mode == "next-snapshot-mid":
        if signal_index + 1 >= len(snapshots):
            return None
        nxt = snapshots[signal_index + 1]
        return nxt.timestamp, nxt.mid_price
    if mode == "next-trade":
        t0 = _ensure_utc(signal.timestamp)
        for ts, px in trades:
            if _ensure_utc(ts) > t0:
                return _ensure_utc(ts), px
        return None
    raise ValueError(f"unknown entry_mode={mode!r}")


def compute_stop_loss(
    *,
    side: str,
    entry: Decimal,
    nearest_bid: Decimal | None,
    nearest_ask: Decimal | None,
    dominant_bid: Decimal | None,
    dominant_ask: Decimal | None,
    params: AuditParams,
) -> tuple[str, str, Decimal, Decimal, float, Decimal] | str:
    """Return (ok_tuple) or reject reason string.

    ok_tuple = (None, sl_reference_type, sl_reference_level, stop_loss,
                stop_distance_pct, stop_risk_per_unit) — actually return dict-like via tuple.
    """
    buffer = Decimal(str(params.sl_buffer_bps)) / Decimal("10000")
    if side == LONG:
        ref_type = None
        ref = None
        if nearest_bid is not None and nearest_bid < entry:
            ref_type, ref = "nearest_bid_wall", nearest_bid
        elif dominant_bid is not None and dominant_bid < entry:
            ref_type, ref = "dominant_bid_floor", dominant_bid
        if ref is None or ref_type is None:
            return NO_TRADE_NO_STRUCTURE
        raw_sl = ref * (Decimal("1") - buffer)
        dist_pct = float((entry - raw_sl) / entry * 100)
        if dist_pct < params.min_sl_distance_pct:
            # widen to minimum
            raw_sl = entry * (Decimal("1") - Decimal(str(params.min_sl_distance_pct)) / Decimal(100))
            dist_pct = params.min_sl_distance_pct
        if dist_pct > params.max_sl_distance_pct:
            return NO_TRADE_TOO_WIDE_SL
        risk = entry - raw_sl
        return (ref_type, ref, raw_sl, dist_pct, risk)

    ref_type = None
    ref = None
    if nearest_ask is not None and nearest_ask > entry:
        ref_type, ref = "nearest_ask_wall", nearest_ask
    elif dominant_ask is not None and dominant_ask > entry:
        ref_type, ref = "dominant_ask_ceiling", dominant_ask
    if ref is None or ref_type is None:
        return NO_TRADE_NO_STRUCTURE
    raw_sl = ref * (Decimal("1") + buffer)
    dist_pct = float((raw_sl - entry) / entry * 100)
    if dist_pct < params.min_sl_distance_pct:
        raw_sl = entry * (Decimal("1") + Decimal(str(params.min_sl_distance_pct)) / Decimal(100))
        dist_pct = params.min_sl_distance_pct
    if dist_pct > params.max_sl_distance_pct:
        return NO_TRADE_TOO_WIDE_SL
    risk = raw_sl - entry
    return (ref_type, ref, raw_sl, dist_pct, risk)


def compute_retest_stop_loss(
    *,
    entry: Decimal,
    retest_low: Decimal | None,
    local_near_bid: Decimal | None,
    reclaim_level: Decimal | None,
    deeper_bid_floor: Decimal | None,
    params: AuditParams,
) -> tuple[str, Decimal, Decimal, float, Decimal] | str:
    """Compute a local retest stop without preferring a deep dominant floor."""
    candidates: list[tuple[str, Decimal]] = []
    if retest_low is not None and retest_low < entry:
        candidates.append(("RETEST_LOW", retest_low))
    if local_near_bid is not None and local_near_bid < entry:
        candidates.append(("LOCAL_NEAR_BID", local_near_bid))
    if candidates:
        ref_type, ref = min(candidates, key=lambda item: item[1])
    elif reclaim_level is not None and reclaim_level < entry:
        ref_type, ref = "RECLAIM_LEVEL", reclaim_level
    elif deeper_bid_floor is not None and deeper_bid_floor < entry:
        ref_type, ref = "DEEPER_BID_FLOOR", deeper_bid_floor
    else:
        return NO_TRADE_NO_STRUCTURE
    buffer = Decimal(str(params.retest_sl_buffer_bps)) / Decimal("10000")
    stop = ref * (Decimal("1") - buffer)
    distance_pct = float((entry - stop) / entry * Decimal("100"))
    if distance_pct > params.retest_max_sl_distance_pct:
        return NO_TRADE_RETEST_SL_TOO_WIDE
    return ref_type, ref, stop, distance_pct, entry - stop


def compute_take_profits(
    *,
    side: str,
    entry: Decimal,
    stop_loss: Decimal,
    nearest_bid: Decimal | None,
    nearest_ask: Decimal | None,
    dominant_bid: Decimal | None,
    dominant_ask: Decimal | None,
    params: AuditParams,
) -> tuple[dict[str, Any] | None, str | None]:
    """Return (tp_fields, reject_reason)."""
    front = Decimal(str(params.tp_front_run_bps)) / Decimal("10000")
    risk = abs(entry - stop_loss)
    if risk <= 0:
        return None, NO_TRADE_INSUFFICIENT_CRV

    def _crv(tp: Decimal) -> float:
        return float(abs(tp - entry) / risk)

    tp1 = tp2 = None
    tp1_type = tp2_type = None
    tp1_ref = tp2_ref = None

    if side == LONG:
        if nearest_ask is not None and nearest_ask > entry:
            tp1_type, tp1_ref = "nearest_ask_wall", nearest_ask
            tp1 = nearest_ask * (Decimal("1") - front)
        if dominant_ask is not None and dominant_ask > entry:
            if tp1_ref is None or dominant_ask > tp1_ref:
                tp2_type, tp2_ref = "dominant_ask_wall", dominant_ask
                tp2 = dominant_ask * (Decimal("1") - front)
    else:
        if nearest_bid is not None and nearest_bid < entry:
            tp1_type, tp1_ref = "nearest_bid_wall", nearest_bid
            tp1 = nearest_bid * (Decimal("1") + front)
        if dominant_bid is not None and dominant_bid < entry:
            if tp1_ref is None or dominant_bid < tp1_ref:
                tp2_type, tp2_ref = "dominant_bid_wall", dominant_bid
                tp2 = dominant_bid * (Decimal("1") + front)

    # Drop TP1 if CRV too low; try TP2
    use_tp1 = tp1 is not None and _crv(tp1) >= params.min_crv_tp1
    use_tp2 = tp2 is not None and _crv(tp2) >= params.min_crv_tp2
    if not use_tp1 and not use_tp2:
        # If TP1 exists but below min, still allow TP2-only when CRV ok
        if tp2 is not None and _crv(tp2) >= params.min_crv_tp1:
            use_tp2 = True
            tp2_type = tp2_type or "dominant_fallback"
        else:
            return None, NO_TRADE_INSUFFICIENT_CRV

    out: dict[str, Any] = {
        "take_profit_1": tp1 if use_tp1 else None,
        "tp1_reference_type": tp1_type if use_tp1 else None,
        "tp1_reference_level": tp1_ref if use_tp1 else None,
        "tp1_distance_pct": None
        if not use_tp1 or tp1 is None
        else _pct(abs(tp1 - entry), entry),
        "tp1_crv": None if not use_tp1 or tp1 is None else _crv(tp1),
        "take_profit_2": tp2 if use_tp2 else None,
        "tp2_reference_type": tp2_type if use_tp2 else None,
        "tp2_reference_level": tp2_ref if use_tp2 else None,
        "tp2_distance_pct": None
        if not use_tp2 or tp2 is None
        else _pct(abs(tp2 - entry), entry),
        "tp2_crv": None if not use_tp2 or tp2 is None else _crv(tp2),
    }
    if out["take_profit_1"] is None and out["take_profit_2"] is None:
        return None, NO_TRADE_INSUFFICIENT_CRV
    return out, None


def simulate_trade_outcome(
    *,
    side: str,
    entry_time: datetime,
    entry_price: Decimal,
    stop_loss: Decimal,
    take_profit_1: Decimal | None,
    take_profit_2: Decimal | None,
    price_path: Sequence[tuple[datetime, Decimal]],
    end: datetime,
    horizon_seconds: int | None = None,
) -> dict[str, Any]:
    """Walk chronological prices after entry; conservative on ambiguous order."""
    t0 = _ensure_utc(entry_time)
    t_end = _ensure_utc(end)
    if horizon_seconds is not None:
        t_end = min(t_end, t0 + timedelta(seconds=horizon_seconds))

    path = [( _ensure_utc(ts), px) for ts, px in price_path if t0 < _ensure_utc(ts) <= t_end]
    if not path:
        return {
            "outcome": OPEN_AT_END if horizon_seconds is None else NEITHER_HIT,
            "first_touch": None,
            "first_touch_time": None,
            "exit_price": _fmt(entry_price),
            "return_pct": 0.0,
            "realised_r_multiple": 0.0,
            "mfe_pct": 0.0,
            "mae_pct": 0.0,
            "time_to_mfe": None,
            "time_to_mae": None,
            "duration_seconds": 0,
            "half_tp_model_r": 0.0,
        }

    risk = abs(entry_price - stop_loss)
    mfe = mae = 0.0
    t_mfe = t_mae = None
    tp1_hit = False
    tp1_time = None
    first_touch = None
    first_touch_time = None
    exit_price = path[-1][1]
    outcome = OPEN_AT_END if horizon_seconds is None else NEITHER_HIT

    # Group by second for ambiguity detection when only snapshot granularity
    i = 0
    while i < len(path):
        ts, px = path[i]
        # same-timestamp bucket
        bucket = [(ts, px)]
        j = i + 1
        while j < len(path) and path[j][0] == ts:
            bucket.append(path[j])
            j += 1

        hit_sl = hit_tp1 = hit_tp2 = False
        for _, p in bucket:
            if side == LONG:
                up = float((p - entry_price) / entry_price * 100)
                dn = float((entry_price - p) / entry_price * 100)
                if up >= mfe:
                    mfe, t_mfe = up, ts
                if dn >= mae:
                    mae, t_mae = dn, ts
                if p <= stop_loss:
                    hit_sl = True
                if take_profit_1 is not None and p >= take_profit_1:
                    hit_tp1 = True
                if take_profit_2 is not None and p >= take_profit_2:
                    hit_tp2 = True
            else:
                up = float((entry_price - p) / entry_price * 100)
                dn = float((p - entry_price) / entry_price * 100)
                if up >= mfe:
                    mfe, t_mfe = up, ts
                if dn >= mae:
                    mae, t_mae = dn, ts
                if p >= stop_loss:
                    hit_sl = True
                if take_profit_1 is not None and p <= take_profit_1:
                    hit_tp1 = True
                if take_profit_2 is not None and p <= take_profit_2:
                    hit_tp2 = True

        # Ambiguity: SL and any TP in same timestamp bucket without finer order
        if hit_sl and (hit_tp1 or hit_tp2) and len(bucket) == 1:
            first_touch = SL_FIRST_AMBIGUOUS
            first_touch_time = ts
            exit_price = stop_loss
            outcome = AMBIGUOUS_TP_SL_ORDER
            break
        if hit_sl and (hit_tp1 or hit_tp2) and len(bucket) > 1:
            # reconstruct order within bucket
            ordered = sorted(bucket, key=lambda x: x[0])  # same ts — still ambiguous
            # walk prices in listed order as provided
            resolved = None
            for _, p in bucket:
                if side == LONG:
                    if p <= stop_loss:
                        resolved = ("SL", stop_loss)
                        break
                    if take_profit_2 is not None and p >= take_profit_2:
                        resolved = ("TP2", take_profit_2)
                        break
                    if take_profit_1 is not None and p >= take_profit_1:
                        resolved = ("TP1", take_profit_1)
                        # continue to see if more in bucket — keep first
                        break
                else:
                    if p >= stop_loss:
                        resolved = ("SL", stop_loss)
                        break
                    if take_profit_2 is not None and p <= take_profit_2:
                        resolved = ("TP2", take_profit_2)
                        break
                    if take_profit_1 is not None and p <= take_profit_1:
                        resolved = ("TP1", take_profit_1)
                        break
            if resolved is None:
                first_touch = SL_FIRST_AMBIGUOUS
                first_touch_time = ts
                exit_price = stop_loss
                outcome = AMBIGUOUS_TP_SL_ORDER
                break
            tag, exit_price = resolved
            first_touch = tag
            first_touch_time = ts
            if tag == "SL":
                outcome = TP1_THEN_SL if tp1_hit else SL_HIT
            elif tag == "TP2":
                outcome = TP1_THEN_TP2 if tp1_hit else TP2_HIT
            else:
                tp1_hit = True
                tp1_time = ts
                # don't exit yet unless no TP2
                if take_profit_2 is None:
                    outcome = TP1_HIT
                    break
                i = j
                continue
            break

        if hit_sl and not hit_tp1 and not hit_tp2:
            first_touch = "SL"
            first_touch_time = ts
            exit_price = stop_loss
            outcome = TP1_THEN_SL if tp1_hit else SL_HIT
            break
        if hit_tp2:
            first_touch = first_touch or "TP2"
            first_touch_time = first_touch_time or ts
            exit_price = take_profit_2  # type: ignore[assignment]
            outcome = TP1_THEN_TP2 if tp1_hit else TP2_HIT
            break
        if hit_tp1 and not tp1_hit:
            tp1_hit = True
            tp1_time = ts
            first_touch = first_touch or "TP1"
            first_touch_time = first_touch_time or ts
            if take_profit_2 is None:
                exit_price = take_profit_1  # type: ignore[assignment]
                outcome = TP1_HIT
                break
        i = j

    if outcome in {OPEN_AT_END, NEITHER_HIT} and tp1_hit and take_profit_1 is not None:
        # TP1 touched but never closed — treat as TP1_HIT diagnostic at last/tp1
        exit_price = take_profit_1
        outcome = TP1_HIT
        first_touch = first_touch or "TP1"
        first_touch_time = first_touch_time or tp1_time

    if side == LONG:
        ret = float((exit_price - entry_price) / entry_price * 100)
        r_mult = float((exit_price - entry_price) / risk) if risk else 0.0
    else:
        ret = float((entry_price - exit_price) / entry_price * 100)
        r_mult = float((entry_price - exit_price) / risk) if risk else 0.0

    # Hypothetical 50/50 TP1/TP2 model (diagnostic)
    half_r = r_mult
    if take_profit_1 is not None and take_profit_2 is not None and risk:
        if side == LONG:
            r1 = float((take_profit_1 - entry_price) / risk)
            r2 = float((take_profit_2 - entry_price) / risk)
        else:
            r1 = float((entry_price - take_profit_1) / risk)
            r2 = float((entry_price - take_profit_2) / risk)
        if outcome == TP1_THEN_TP2:
            half_r = 0.5 * r1 + 0.5 * r2
        elif outcome == TP1_THEN_SL:
            half_r = 0.5 * r1 + 0.5 * (-1.0)
        elif outcome == TP2_HIT:
            half_r = 0.5 * r1 + 0.5 * r2
        elif outcome == TP1_HIT:
            half_r = r1

    duration = int((first_touch_time - t0).total_seconds()) if first_touch_time else int(
        (path[-1][0] - t0).total_seconds()
    )
    return {
        "outcome": outcome,
        "first_touch": first_touch,
        "first_touch_time": None if first_touch_time is None else first_touch_time.isoformat(),
        "exit_price": _fmt(exit_price),
        "return_pct": round(ret, 6),
        "realised_r_multiple": round(r_mult, 6),
        "mfe_pct": round(mfe, 6),
        "mae_pct": round(mae, 6),
        "time_to_mfe": None if t_mfe is None else int((t_mfe - t0).total_seconds()),
        "time_to_mae": None if t_mae is None else int((t_mae - t0).total_seconds()),
        "duration_seconds": duration,
        "half_tp_model_r": round(half_r, 6),
    }


def dedupe_signal_episodes(
    decisions: Sequence[CandidateDecision],
    *,
    cooldown: timedelta,
) -> tuple[list[CandidateDecision], list[dict[str, Any]]]:
    """Collapse consecutive same-side signals; emit episode rows."""
    episodes: list[dict[str, Any]] = []
    kept: list[CandidateDecision] = []
    active_side: str | None = None
    episode_start: datetime | None = None
    last_accept_time: datetime | None = None
    episode_id = 0

    for d in decisions:
        if d.side not in {LONG, SHORT}:
            if active_side is not None:
                episodes.append(
                    {
                        "episode_id": f"E{episode_id:04d}",
                        "side": active_side,
                        "start": episode_start.isoformat() if episode_start else None,
                        "end": d.signal_time.isoformat(),
                        "end_reason": "neutral_or_reject",
                    }
                )
                active_side = None
                episode_start = None
            continue

        if active_side == d.side:
            # same episode — skip duplicate
            continue

        if last_accept_time is not None:
            if d.signal_time < last_accept_time + cooldown and active_side is None:
                # cooldown after closed episode
                # still allow if opposite? user: new episode if opposite OR cooldown passed
                pass

        if active_side is not None and active_side != d.side:
            episodes.append(
                {
                    "episode_id": f"E{episode_id:04d}",
                    "side": active_side,
                    "start": episode_start.isoformat() if episode_start else None,
                    "end": d.signal_time.isoformat(),
                    "end_reason": "opposite_bias",
                }
            )

        if last_accept_time is not None and active_side is None:
            if d.signal_time < last_accept_time + cooldown and (
                # only apply cooldown when same side would restart; opposite already handled
                True
            ):
                # If previous episode ended and we're restarting same side within cooldown, reject
                # We don't know previous side here easily — track last_side
                pass

        episode_id += 1
        active_side = d.side
        episode_start = d.signal_time
        last_accept_time = d.signal_time
        # attach episode id via reason field mutation — store on a copy dict later
        kept.append(d)
        episodes.append(
            {
                "episode_id": f"E{episode_id:04d}",
                "side": d.side,
                "start": d.signal_time.isoformat(),
                "end": None,
                "end_reason": "open",
                "signal_time": d.signal_time.isoformat(),
                "score": d.score,
            }
        )

    return kept, episodes


def apply_cooldown_filter(
    decisions: Sequence[CandidateDecision],
    *,
    cooldown_minutes: int,
) -> tuple[list[tuple[CandidateDecision, str]], list[CandidateDecision], list[dict[str, Any]]]:
    """Return (accepted_with_episode_id, rejected, episodes)."""
    cooldown = timedelta(minutes=cooldown_minutes)
    accepted: list[tuple[CandidateDecision, str]] = []
    rejected: list[CandidateDecision] = []
    episodes: list[dict[str, Any]] = []
    last_accepted_side: str | None = None
    last_accepted_time: datetime | None = None
    episode_open = False
    episode_id = 0
    open_episode: dict[str, Any] | None = None

    for d in decisions:
        if d.side not in {LONG, SHORT}:
            rejected.append(d)
            if episode_open and open_episode is not None:
                open_episode["end"] = d.signal_time.isoformat()
                open_episode["end_reason"] = d.reason
                episodes.append(open_episode)
                open_episode = None
                episode_open = False
                # keep last_accepted_side for cooldown on same-side restart
            continue

        # Active same-side episode → dedupe
        if episode_open and last_accepted_side == d.side:
            rejected.append(
                CandidateDecision(
                    signal_time=d.signal_time,
                    side=NO_TRADE,
                    reason="DEDUPED_SAME_EPISODE",
                    score=d.score,
                    components=d.components,
                    snapshot_index=d.snapshot_index,
                    mid=d.mid,
                    nearest_bid=d.nearest_bid,
                    nearest_ask=d.nearest_ask,
                    dominant_bid=d.dominant_bid,
                    dominant_ask=d.dominant_ask,
                )
            )
            continue

        # Restart same side after close → cooldown gate
        if (
            not episode_open
            and last_accepted_side == d.side
            and last_accepted_time is not None
            and d.signal_time < last_accepted_time + cooldown
        ):
            rejected.append(
                CandidateDecision(
                    signal_time=d.signal_time,
                    side=NO_TRADE,
                    reason=NO_TRADE_COOLDOWN,
                    score=d.score,
                    components=d.components,
                    snapshot_index=d.snapshot_index,
                    mid=d.mid,
                    nearest_bid=d.nearest_bid,
                    nearest_ask=d.nearest_ask,
                    dominant_bid=d.dominant_bid,
                    dominant_ask=d.dominant_ask,
                )
            )
            continue

        if episode_open and last_accepted_side != d.side and open_episode is not None:
            open_episode["end"] = d.signal_time.isoformat()
            open_episode["end_reason"] = "opposite_signal"
            episodes.append(open_episode)

        episode_id += 1
        eid = f"E{episode_id:04d}"
        open_episode = {
            "episode_id": eid,
            "side": d.side,
            "start": d.signal_time.isoformat(),
            "end": None,
            "end_reason": "open",
            "score": d.score,
        }
        accepted.append((d, eid))
        episode_open = True
        last_accepted_side = d.side
        last_accepted_time = d.signal_time

    if open_episode is not None:
        episodes.append(open_episode)
    return accepted, rejected, episodes


def decision_to_row(d: CandidateDecision, *, candidate_id: str | None = None) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "signal_time": d.signal_time.isoformat(),
        "side": d.side,
        "reason": d.reason,
        "score": d.score,
        "rising_bid_shifts": d.rising_bid_shifts,
        "falling_ask_shifts": d.falling_ask_shifts,
        "auction_direction": d.auction_direction,
        "short_term_bias": d.short_term_bias,
        "near_ask_class": d.near_ask_class,
        "trade_delta": _fmt(d.trade_delta),
        "oi_change": _fmt(d.oi_change),
        "mid": _fmt(d.mid),
        "nearest_bid": _fmt(d.nearest_bid),
        "nearest_ask": _fmt(d.nearest_ask),
        "dominant_bid": _fmt(d.dominant_bid),
        "dominant_ask": _fmt(d.dominant_ask),
        "liquidation_confirm": d.liquidation_confirm,
        "rising_bid_state_age_seconds": d.rising_bid_state_age_seconds,
        "falling_bid_state_age_seconds": d.falling_bid_state_age_seconds,
        "rising_ask_state_age_seconds": d.rising_ask_state_age_seconds,
        "falling_ask_state_age_seconds": d.falling_ask_state_age_seconds,
        "active_rising_bid_shifts": d.active_rising_bid_shifts,
        "active_falling_ask_shifts": d.active_falling_ask_shifts,
        "expired_falling_ask_shifts": d.expired_falling_ask_shifts,
        "active_structure_source_timestamp": None
        if d.active_structure_source_timestamp is None
        else d.active_structure_source_timestamp.isoformat(),
        "falling_ask_blocker_active": d.falling_ask_blocker_active,
        "falling_ask_blocker_expired": d.falling_ask_blocker_expired,
        "falling_ask_reclaim_confirmed": d.falling_ask_reclaim_confirmed,
        "falling_ask_reclaim_reason": d.falling_ask_reclaim_reason,
        "entry_setup_type": d.entry_setup_type,
        "retest_reference_type": d.retest_reference_type,
        "retest_reference_level": _fmt(d.retest_reference_level),
        "retest_first_touch_time": None
        if d.retest_first_touch_time is None
        else d.retest_first_touch_time.isoformat(),
        "retest_low": _fmt(d.retest_low),
        "retest_undershoot_bps": d.retest_undershoot_bps,
        "reclaim_start_time": None
        if d.reclaim_start_time is None
        else d.reclaim_start_time.isoformat(),
        "reclaim_confirm_time": None
        if d.reclaim_confirm_time is None
        else d.reclaim_confirm_time.isoformat(),
        "reclaim_snapshot_count": d.reclaim_snapshot_count,
        "local_support_level": _fmt(d.local_support_level),
        "local_support_notional": _fmt(d.local_support_notional),
        "static_tp1": _fmt(d.static_tp1),
        "static_tp2": _fmt(d.static_tp2),
        "dynamic_exit_trigger": d.dynamic_exit_trigger,
        "dynamic_exit_time": None
        if d.dynamic_exit_time is None
        else d.dynamic_exit_time.isoformat(),
        "dynamic_exit_price": _fmt(d.dynamic_exit_price),
        "dynamic_exit_r_multiple": d.dynamic_exit_r_multiple,
    }


def build_accepted_candidate(
    decision: CandidateDecision,
    *,
    candidate_id: str,
    episode_id: str,
    entry_time: datetime,
    entry_price: Decimal,
    params: AuditParams,
) -> AcceptedCandidate | CandidateDecision:
    if decision.entry_setup_type == SUPPORT_RETEST_RECLAIM_LONG:
        sl = compute_retest_stop_loss(
            entry=entry_price,
            retest_low=decision.retest_low,
            local_near_bid=decision.local_support_level or decision.nearest_bid,
            reclaim_level=decision.retest_reference_level,
            deeper_bid_floor=decision.dominant_bid,
            params=params,
        )
    else:
        sl = compute_stop_loss(
            side=decision.side,
            entry=entry_price,
            nearest_bid=decision.nearest_bid,
            nearest_ask=decision.nearest_ask,
            dominant_bid=decision.dominant_bid,
            dominant_ask=decision.dominant_ask,
            params=params,
        )
    if isinstance(sl, str):
        return replace(decision, side=NO_TRADE, reason=sl)
    ref_type, ref_level, stop, dist_pct, risk = sl
    tp_fields, tp_reject = compute_take_profits(
        side=decision.side,
        entry=entry_price,
        stop_loss=stop,
        nearest_bid=decision.nearest_bid,
        nearest_ask=decision.nearest_ask,
        dominant_bid=decision.dominant_bid,
        dominant_ask=decision.dominant_ask,
        params=params,
    )
    if tp_reject or tp_fields is None:
        reason = tp_reject or NO_TRADE_INSUFFICIENT_CRV
        if decision.entry_setup_type == SUPPORT_RETEST_RECLAIM_LONG:
            reason = NO_TRADE_RETEST_INSUFFICIENT_CRV
        return replace(
            decision,
            side=NO_TRADE,
            reason=reason,
        )
    decision.static_tp1 = tp_fields["take_profit_1"]
    decision.static_tp2 = tp_fields["take_profit_2"]
    return AcceptedCandidate(
        candidate_id=candidate_id,
        episode_id=episode_id,
        decision=decision,
        entry_time=entry_time,
        entry_price=entry_price,
        stop_loss=stop,
        take_profit_1=tp_fields["take_profit_1"],
        take_profit_2=tp_fields["take_profit_2"],
        sl_reference_type=ref_type,
        sl_reference_level=ref_level,
        sl_buffer_bps=params.sl_buffer_bps,
        stop_distance_pct=dist_pct,
        stop_risk_per_unit=risk,
        tp1_reference_type=tp_fields["tp1_reference_type"],
        tp1_reference_level=tp_fields["tp1_reference_level"],
        tp1_distance_pct=tp_fields["tp1_distance_pct"],
        tp1_crv=tp_fields["tp1_crv"],
        tp2_reference_type=tp_fields["tp2_reference_type"],
        tp2_reference_level=tp_fields["tp2_reference_level"],
        tp2_distance_pct=tp_fields["tp2_distance_pct"],
        tp2_crv=tp_fields["tp2_crv"],
    )


def accepted_to_row(c: AcceptedCandidate) -> dict[str, Any]:
    d = c.decision
    return {
        "candidate_id": c.candidate_id,
        "episode_id": c.episode_id,
        "signal_time": d.signal_time.isoformat(),
        "entry_time": c.entry_time.isoformat(),
        "side": d.side,
        "score": d.score,
        "entry_price": _fmt(c.entry_price),
        "stop_loss": _fmt(c.stop_loss),
        "take_profit_1": _fmt(c.take_profit_1),
        "take_profit_2": _fmt(c.take_profit_2),
        "sl_reference_type": c.sl_reference_type,
        "sl_reference_level": _fmt(c.sl_reference_level),
        "sl_buffer_bps": c.sl_buffer_bps,
        "stop_distance_pct": round(c.stop_distance_pct, 6),
        "stop_risk_per_unit": _fmt(c.stop_risk_per_unit),
        "tp1_reference_type": c.tp1_reference_type,
        "tp1_reference_level": _fmt(c.tp1_reference_level),
        "tp1_distance_pct": None if c.tp1_distance_pct is None else round(c.tp1_distance_pct, 6),
        "tp1_crv": None if c.tp1_crv is None else round(c.tp1_crv, 6),
        "tp2_reference_type": c.tp2_reference_type,
        "tp2_reference_level": _fmt(c.tp2_reference_level),
        "tp2_distance_pct": None if c.tp2_distance_pct is None else round(c.tp2_distance_pct, 6),
        "tp2_crv": None if c.tp2_crv is None else round(c.tp2_crv, 6),
        "auction_direction": d.auction_direction,
        "short_term_bias": d.short_term_bias,
        "near_ask_class": d.near_ask_class,
        "trade_delta": _fmt(d.trade_delta),
        "oi_change": _fmt(d.oi_change),
        "liquidation_confirm": d.liquidation_confirm,
        "mid_at_signal": _fmt(d.mid),
        "nearest_bid": _fmt(d.nearest_bid),
        "nearest_ask": _fmt(d.nearest_ask),
        "entry_setup_type": d.entry_setup_type,
        "retest_reference_type": d.retest_reference_type,
        "retest_reference_level": _fmt(d.retest_reference_level),
        "retest_first_touch_time": None
        if d.retest_first_touch_time is None
        else d.retest_first_touch_time.isoformat(),
        "retest_low": _fmt(d.retest_low),
        "retest_undershoot_bps": d.retest_undershoot_bps,
        "reclaim_start_time": None
        if d.reclaim_start_time is None
        else d.reclaim_start_time.isoformat(),
        "reclaim_confirm_time": None
        if d.reclaim_confirm_time is None
        else d.reclaim_confirm_time.isoformat(),
        "reclaim_snapshot_count": d.reclaim_snapshot_count,
        "local_support_level": _fmt(d.local_support_level),
        "local_support_notional": _fmt(d.local_support_notional),
        "static_tp1": _fmt(d.static_tp1),
        "static_tp2": _fmt(d.static_tp2),
        "dynamic_exit_trigger": d.dynamic_exit_trigger,
        "dynamic_exit_time": None
        if d.dynamic_exit_time is None
        else d.dynamic_exit_time.isoformat(),
        "dynamic_exit_price": _fmt(d.dynamic_exit_price),
        "dynamic_exit_r_multiple": d.dynamic_exit_r_multiple,
        "falling_ask_blocker_active": d.falling_ask_blocker_active,
        "falling_ask_blocker_expired": d.falling_ask_blocker_expired,
        "falling_ask_reclaim_reason": d.falling_ask_reclaim_reason,
    }


def attach_dynamic_exit(
    candidate: AcceptedCandidate,
    *,
    snapshots: Sequence[SnapshotRecord],
    near_views: Sequence[NearSnapshotView],
    sequences: Sequence[SequenceRecord],
    near_tx: Sequence[NearAskTransition],
    ladder_seqs: Sequence[Any],
    params: AuditParams,
) -> None:
    """Attach a forward-only research exit; never influences entry."""
    if candidate.decision.entry_setup_type != SUPPORT_RETEST_RECLAIM_LONG:
        return
    for i, snap in enumerate(snapshots):
        if _ensure_utc(snap.timestamp) <= _ensure_utc(candidate.entry_time):
            continue
        falling_bid, _, expired = aged_sequence(
            sequences,
            FALLING_BID_FLOOR,
            snap.timestamp,
            params.contradiction_state_max_age_seconds,
        )
        regime = build_regime_at(
            snapshots, near_views, near_tx, ladder_seqs, index=i
        )
        near_class = recent_near_ask_class(near_tx, as_of=snap.timestamp)
        trigger = None
        if falling_bid is not None and not expired:
            trigger = "BID_FLOOR_FALLS"
        elif regime.get("auction_direction") == "LOWER":
            trigger = "AUCTION_LOWER"
        elif near_class == NEAR_ASK_MOVING_LOWER:
            trigger = "NEAR_ASK_CONFIRMED_LOWER"
        if trigger is None:
            continue
        risk = candidate.stop_risk_per_unit
        candidate.decision.dynamic_exit_trigger = trigger
        candidate.decision.dynamic_exit_time = snap.timestamp
        candidate.decision.dynamic_exit_price = snap.mid_price
        candidate.decision.dynamic_exit_r_multiple = (
            float((snap.mid_price - candidate.entry_price) / risk)
            if risk
            else None
        )
        return


def run_audit_from_snapshots(
    *,
    snapshots: Sequence[SnapshotRecord],
    near_views: Sequence[NearSnapshotView],
    sequences: Sequence[SequenceRecord],
    transitions: Sequence[TransitionRecord],
    near_tx: Sequence[NearAskTransition],
    ladder_seqs: Sequence[Any],
    liquidations: Sequence[LiquidationEvent],
    price_path: Sequence[tuple[datetime, Decimal]],
    params: AuditParams,
    end: datetime,
    output_dir: Path,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_decisions: list[CandidateDecision] = []
    legacy_decisions: list[CandidateDecision] = []
    retest_tracker = RetestTracker(params)
    # need at least a few snapshots for structure
    start_i = max(2, params.movement.sequence_min_snapshots - 1)
    for i in range(1, len(snapshots)):
        retest_state = retest_tracker.process(snapshots, index=i)
        if i < start_i:
            continue
        legacy_decisions.append(
            _evaluate_candidate_at_legacy(
                index=i,
                snapshots=snapshots,
                near_views=near_views,
                sequences=sequences,
                transitions=transitions,
                near_tx=near_tx,
                ladder_seqs=ladder_seqs,
                liquidations=liquidations,
                params=params,
            )
        )
        raw_decisions.append(
            evaluate_candidate_at(
                index=i,
                snapshots=snapshots,
                near_views=near_views,
                sequences=sequences,
                transitions=transitions,
                near_tx=near_tx,
                ladder_seqs=ladder_seqs,
                liquidations=liquidations,
                params=params,
                retest_state=retest_state,
            )
        )

    # Validate entry/SL/TP before episode bookkeeping so CRV/SL rejects
    # do not open episodes and suppress later candidates.
    provisional: list[tuple[CandidateDecision, AcceptedCandidate]] = []
    validation_stream: list[CandidateDecision] = []
    for d in raw_decisions:
        if d.side not in {LONG, SHORT}:
            validation_stream.append(d)
            continue
        entry = resolve_entry_price(
            mode=params.entry_mode,
            signal_index=d.snapshot_index,
            snapshots=snapshots,
            trades=price_path,
        )
        if entry is None:
            validation_stream.append(
                replace(d, side=NO_TRADE, reason=NO_TRADE_NO_ENTRY_PRICE)
            )
            continue
        entry_time, entry_price = entry
        built = build_accepted_candidate(
            d,
            candidate_id="pending",
            episode_id="pending",
            entry_time=entry_time,
            entry_price=entry_price,
            params=params,
        )
        if isinstance(built, CandidateDecision):
            validation_stream.append(built)
            continue
        provisional.append((d, built))
        validation_stream.append(d)

    accepted_signals, cooldown_rejects, episodes = apply_cooldown_filter(
        validation_stream, cooldown_minutes=params.cooldown_minutes
    )
    accepted_ids = {id(d) for d, _ in accepted_signals}
    ep_by_id = {id(d): eid for d, eid in accepted_signals}

    trade_rows: list[dict[str, Any]] = []
    reject_rows: list[dict[str, Any]] = [
        decision_to_row(r) for r in cooldown_rejects
    ]
    forward_rows: list[dict[str, Any]] = []
    wall_rows: list[dict[str, Any]] = []
    score_rows: list[dict[str, Any]] = []
    accepted: list[AcceptedCandidate] = []

    cid = 0
    for decision, built0 in provisional:
        if id(decision) not in accepted_ids:
            continue
        cid += 1
        cand_id = f"C{cid:04d}"
        eid = ep_by_id[id(decision)]
        built = AcceptedCandidate(
            candidate_id=cand_id,
            episode_id=eid,
            decision=built0.decision,
            entry_time=built0.entry_time,
            entry_price=built0.entry_price,
            stop_loss=built0.stop_loss,
            take_profit_1=built0.take_profit_1,
            take_profit_2=built0.take_profit_2,
            sl_reference_type=built0.sl_reference_type,
            sl_reference_level=built0.sl_reference_level,
            sl_buffer_bps=built0.sl_buffer_bps,
            stop_distance_pct=built0.stop_distance_pct,
            stop_risk_per_unit=built0.stop_risk_per_unit,
            tp1_reference_type=built0.tp1_reference_type,
            tp1_reference_level=built0.tp1_reference_level,
            tp1_distance_pct=built0.tp1_distance_pct,
            tp1_crv=built0.tp1_crv,
            tp2_reference_type=built0.tp2_reference_type,
            tp2_reference_level=built0.tp2_reference_level,
            tp2_distance_pct=built0.tp2_distance_pct,
            tp2_crv=built0.tp2_crv,
        )
        entry_time = built.entry_time
        entry_price = built.entry_price
        attach_dynamic_exit(
            built,
            snapshots=snapshots,
            near_views=near_views,
            sequences=sequences,
            near_tx=near_tx,
            ladder_seqs=ladder_seqs,
            params=params,
        )
        accepted.append(built)
        trade_rows.append(accepted_to_row(built))
        for comp in decision.components:
            score_rows.append(comp.to_row(candidate_id=cand_id))
        wall_rows.append(
            {
                "candidate_id": cand_id,
                "side": decision.side,
                "nearest_bid": _fmt(decision.nearest_bid),
                "nearest_ask": _fmt(decision.nearest_ask),
                "dominant_bid": _fmt(decision.dominant_bid),
                "dominant_ask": _fmt(decision.dominant_ask),
                "sl_reference_type": built.sl_reference_type,
                "sl_reference_level": _fmt(built.sl_reference_level),
                "tp1_reference_type": built.tp1_reference_type,
                "tp1_reference_level": _fmt(built.tp1_reference_level),
                "tp2_reference_type": built.tp2_reference_type,
                "tp2_reference_level": _fmt(built.tp2_reference_level),
            }
        )
        # primary outcome to session end + horizon rows
        primary = simulate_trade_outcome(
            side=decision.side,
            entry_time=entry_time,
            entry_price=entry_price,
            stop_loss=built.stop_loss,
            take_profit_1=built.take_profit_1,
            take_profit_2=built.take_profit_2,
            price_path=price_path,
            end=end,
            horizon_seconds=None,
        )
        forward_rows.append(
            {
                "candidate_id": cand_id,
                "horizon_seconds": "session_end",
                "entry_time": entry_time.isoformat(),
                "entry_price": _fmt(entry_price),
                "side": decision.side,
                "stop_loss": _fmt(built.stop_loss),
                "take_profit_1": _fmt(built.take_profit_1),
                "take_profit_2": _fmt(built.take_profit_2),
                **primary,
            }
        )
        for h in HORIZONS_SEC:
            out_h = simulate_trade_outcome(
                side=decision.side,
                entry_time=entry_time,
                entry_price=entry_price,
                stop_loss=built.stop_loss,
                take_profit_1=built.take_profit_1,
                take_profit_2=built.take_profit_2,
                price_path=price_path,
                end=end,
                horizon_seconds=h,
            )
            forward_rows.append(
                {
                    "candidate_id": cand_id,
                    "horizon_seconds": h,
                    "entry_time": entry_time.isoformat(),
                    "entry_price": _fmt(entry_price),
                    "side": decision.side,
                    "stop_loss": _fmt(built.stop_loss),
                    "take_profit_1": _fmt(built.take_profit_1),
                    "take_profit_2": _fmt(built.take_profit_2),
                    **out_h,
                }
            )

    long_acc = [c for c in accepted if c.decision.side == LONG]
    short_acc = [c for c in accepted if c.decision.side == SHORT]
    primary_fwd = [r for r in forward_rows if r.get("horizon_seconds") == "session_end"]
    outcome_counts: dict[str, int] = {}
    for r in primary_fwd:
        outcome_counts[str(r["outcome"])] = outcome_counts.get(str(r["outcome"]), 0) + 1
    total_r = sum(float(r.get("realised_r_multiple") or 0) for r in primary_fwd)
    tp1_n = sum(1 for r in primary_fwd if "TP1" in str(r["outcome"]) or str(r["outcome"]) == TP1_HIT)
    tp2_n = sum(1 for r in primary_fwd if "TP2" in str(r["outcome"]))
    sl_n = sum(1 for r in primary_fwd if str(r["outcome"]) in {SL_HIT, TP1_THEN_SL, AMBIGUOUS_TP_SL_ORDER})

    earliest_long = min((c.decision.signal_time for c in long_acc), default=None)
    earliest_short = min((c.decision.signal_time for c in short_acc), default=None)
    best = max(primary_fwd, key=lambda r: float(r.get("realised_r_multiple") or -999), default=None)
    worst = min(primary_fwd, key=lambda r: float(r.get("realised_r_multiple") or 999), default=None)

    reject_reasons: dict[str, int] = {}
    for r in reject_rows:
        reject_reasons[str(r.get("reason"))] = reject_reasons.get(str(r.get("reason")), 0) + 1

    legacy_validation: list[CandidateDecision] = []
    for d in legacy_decisions:
        if d.side not in {LONG, SHORT}:
            legacy_validation.append(d)
            continue
        entry = resolve_entry_price(
            mode=params.entry_mode,
            signal_index=d.snapshot_index,
            snapshots=snapshots,
            trades=price_path,
        )
        if entry is None:
            legacy_validation.append(
                replace(d, side=NO_TRADE, reason=NO_TRADE_NO_ENTRY_PRICE)
            )
            continue
        legacy_built = build_accepted_candidate(
            d,
            candidate_id="legacy",
            episode_id="legacy",
            entry_time=entry[0],
            entry_price=entry[1],
            params=params,
        )
        legacy_validation.append(
            legacy_built if isinstance(legacy_built, CandidateDecision) else d
        )
    legacy_accepted_signals, _, _ = apply_cooldown_filter(
        legacy_validation,
        cooldown_minutes=params.cooldown_minutes,
    )
    legacy_accepted_long = sum(
        1 for d, _ in legacy_accepted_signals if d.side == LONG
    )
    legacy_accepted_short = sum(
        1 for d, _ in legacy_accepted_signals if d.side == SHORT
    )

    retest_acc = [
        c
        for c in accepted
        if c.decision.entry_setup_type == SUPPORT_RETEST_RECLAIM_LONG
    ]
    if retest_acc:
        decision_label = "RETEST_RECLAIM_ENTRY_PROMISING"
    elif snapshots:
        decision_label = "RETEST_RECLAIM_ENTRY_INCONCLUSIVE"
    else:
        decision_label = "RETEST_RECLAIM_ENTRY_FAILED"

    first_retest = min(
        retest_acc, key=lambda c: c.decision.signal_time, default=None
    )
    first_retest_row = (
        None if first_retest is None else accepted_to_row(first_retest)
    )
    first_retest_outcome = None
    if first_retest is not None:
        first_retest_outcome = next(
            (
                r
                for r in primary_fwd
                if r.get("candidate_id") == first_retest.candidate_id
            ),
            None,
        )
    target_1145 = datetime(2026, 7, 26, 11, 45, 29, tzinfo=timezone.utc)
    state_1145 = min(
        raw_decisions,
        key=lambda d: abs(
            (_ensure_utc(d.signal_time) - target_1145).total_seconds()
        ),
        default=None,
    )
    state_1145_row = (
        None if state_1145 is None else decision_to_row(state_1145)
    )
    legacy_long_raw = sum(1 for d in legacy_decisions if d.side == LONG)
    legacy_short_raw = sum(1 for d in legacy_decisions if d.side == SHORT)
    new_long_raw = sum(1 for d in raw_decisions if d.side == LONG)
    new_short_raw = sum(1 for d in raw_decisions if d.side == SHORT)
    false_retest_count = sum(
        1
        for c in retest_acc
        for r in primary_fwd
        if r.get("candidate_id") == c.candidate_id
        and r.get("outcome") in {SL_HIT, AMBIGUOUS_TP_SL_ORDER}
    )
    legacy_signal_keys = {
        (d.signal_time, d.side) for d, _ in legacy_accepted_signals
    }
    new_only_ids = {
        c.candidate_id
        for c in accepted
        if (c.decision.signal_time, c.decision.side) not in legacy_signal_keys
    }
    false_new_logic_count = sum(
        1
        for r in primary_fwd
        if r.get("candidate_id") in new_only_ids
        and r.get("outcome") in {SL_HIT, AMBIGUOUS_TP_SL_ORDER}
    )
    falling_bid_expiry = None
    falling_ask_expiry = None
    if state_1145 is not None:
        if state_1145.falling_bid_state_age_seconds is not None:
            sequence_end = state_1145.signal_time - timedelta(
                seconds=state_1145.falling_bid_state_age_seconds
            )
            falling_bid_expiry = sequence_end + timedelta(
                seconds=params.contradiction_state_max_age_seconds
                + params.sample_seconds
            )
        if state_1145.falling_ask_state_age_seconds is not None:
            sequence_end = state_1145.signal_time - timedelta(
                seconds=state_1145.falling_ask_state_age_seconds
            )
            falling_ask_expiry = sequence_end + timedelta(
                seconds=params.contradiction_state_max_age_seconds
                + params.sample_seconds
            )

    summary: dict[str, Any] = {
        "decision": decision_label,
        "long_candidate_count": len(long_acc),
        "short_candidate_count": len(short_acc),
        "retest_candidate_count": len(retest_acc),
        "first_retest_candidate": first_retest_row,
        "first_retest_outcome": first_retest_outcome,
        "state_at_11_45_29": state_1145_row,
        "legacy_raw_long_signal_count": legacy_long_raw,
        "legacy_raw_short_signal_count": legacy_short_raw,
        "new_raw_long_signal_count": new_long_raw,
        "new_raw_short_signal_count": new_short_raw,
        "additional_raw_long_signals": new_long_raw - legacy_long_raw,
        "additional_raw_short_signals": new_short_raw - legacy_short_raw,
        "false_retest_trade_count": false_retest_count,
        "legacy_accepted_long_count": legacy_accepted_long,
        "legacy_accepted_short_count": legacy_accepted_short,
        "additional_accepted_long_count": len(long_acc) - legacy_accepted_long,
        "additional_accepted_short_count": len(short_acc) - legacy_accepted_short,
        "false_new_logic_trade_count": false_new_logic_count,
        "historical_falling_bid_expired_at": None
        if falling_bid_expiry is None
        else falling_bid_expiry.isoformat(),
        "active_falling_ask_expired_at": None
        if falling_ask_expiry is None
        else falling_ask_expiry.isoformat(),
        "rejected_count": len(reject_rows),
        "reject_reasons": reject_reasons,
        "earliest_long": None if earliest_long is None else earliest_long.isoformat(),
        "earliest_short": None if earliest_short is None else earliest_short.isoformat(),
        "outcome_counts_session_end": outcome_counts,
        "tp1_related_count": tp1_n,
        "tp2_related_count": tp2_n,
        "sl_related_count": sl_n,
        "diagnostic_total_r_multiple": round(total_r, 6),
        "best_candidate": best,
        "worst_candidate": worst,
        "params": {
            "entry_mode": params.entry_mode,
            "minimum_entry_score": params.minimum_entry_score,
            "sl_buffer_bps": params.sl_buffer_bps,
            "min_sl_distance_pct": params.min_sl_distance_pct,
            "max_sl_distance_pct": params.max_sl_distance_pct,
            "tp_front_run_bps": params.tp_front_run_bps,
            "min_crv_tp1": params.min_crv_tp1,
            "min_crv_tp2": params.min_crv_tp2,
            "cooldown_minutes": params.cooldown_minutes,
            "structure_state_max_age_seconds": params.structure_state_max_age_seconds,
            "contradiction_state_max_age_seconds": params.contradiction_state_max_age_seconds,
            "reclaim_confirm_snapshots": params.reclaim_confirm_snapshots,
            "retest_distance_bps": params.retest_distance_bps,
            "retest_max_undershoot_bps": params.retest_max_undershoot_bps,
            "retest_sl_buffer_bps": params.retest_sl_buffer_bps,
            "retest_max_sl_distance_pct": params.retest_max_sl_distance_pct,
        },
        "limitations": [
            "Single-window diagnostic audit — not proof of a profitable strategy.",
            "Strict causality at signal time; forward data used only for outcomes.",
            "Previous miss root cause: evaluate_candidate_at used latest_sequence(FALLING_BID_FLOOR) without max age, so an ended sequence remained a permanent long contradiction; failed gates returned score=0 before feature scoring.",
            "Falling ask shifts were displayed by the old audit but were not the hard long_contradiction blocker; aged falling bid and pullback-negative delta were.",
            "Ambiguous same-timestamp TP/SL touches marked conservatively.",
            "Liquidation is confirmation-only and never sufficient for entry.",
            "No live orders; research module only.",
        ],
        "accepted": trade_rows,
    }

    write_csv(output_dir / "trade_candidates.csv", trade_rows)
    write_csv(output_dir / "rejected_candidates.csv", reject_rows)
    write_csv(output_dir / "candidate_forward_outcomes.csv", forward_rows)
    write_csv(output_dir / "candidate_wall_context.csv", wall_rows)
    write_csv(output_dir / "candidate_score_components.csv", score_rows)
    write_csv(output_dir / "signal_episodes.csv", episodes)
    retest_rows = [
        decision_to_row(d)
        for d in raw_decisions
        if d.retest_reference_level is not None
    ]
    aging_rows = [
        decision_to_row(d)
        for d in raw_decisions
    ]
    write_csv(output_dir / "retest_candidates.csv", retest_rows)
    write_csv(
        output_dir / "retest_state_transitions.csv",
        retest_tracker.transitions,
    )
    write_csv(
        output_dir / "retest_reclaim_context.csv",
        retest_tracker.context,
    )
    write_csv(output_dir / "structure_state_aging.csv", aging_rows)
    (output_dir / "strategy_summary.json").write_bytes(
        orjson.dumps(summary, option=orjson.OPT_INDENT_2)
    )
    (output_dir / "REPORT.md").write_text(render_audit_report(summary), encoding="utf-8")
    return summary


def render_audit_report(summary: dict[str, Any]) -> str:
    first = summary.get("first_retest_candidate") or {}
    outcome = summary.get("first_retest_outcome") or {}
    state_1145 = summary.get("state_at_11_45_29") or {}
    lines = [
        "# Orderbook Trade Candidate Audit",
        "",
        f"- Decision: **{summary.get('decision')}**",
        f"- LONG candidates: **{summary.get('long_candidate_count')}**",
        f"- SHORT candidates: **{summary.get('short_candidate_count')}**",
        f"- Rejected / NO_TRADE rows: **{summary.get('rejected_count')}**",
        f"- Earliest causal LONG: `{summary.get('earliest_long')}`",
        f"- Earliest causal SHORT: `{summary.get('earliest_short')}`",
        f"- Diagnostic total R: `{summary.get('diagnostic_total_r_multiple')}`",
        "",
        "## Retest-specific forensic answers",
        "",
        "1. Why was the prior long missed? `evaluate_candidate_at` called "
        "`latest_sequence(..., FALLING_BID_FLOOR)` without a max age; the aged "
        "sequence remained a permanent contradiction, while pullback-negative "
        "delta also failed `flow_ok`.",
        "2. Which old structure blocked it? The historical `FALLING_BID_FLOOR`; "
        "displayed `falling_ask_shifts` was not part of the old hard long contradiction.",
        f"3. When was the blocker expired under the new logic? Historical falling "
        f"bid: `{summary.get('historical_falling_bid_expired_at')}`; the new falling-ask "
        f"pullback state seen at 11:45 expired at "
        f"`{summary.get('active_falling_ask_expired_at')}`.",
        f"4. When did retest begin? `{first.get('retest_first_touch_time')}`.",
        f"5. When was reclaim confirmed? `{first.get('reclaim_confirm_time')}`.",
        f"6. What was local support? level=`{first.get('local_support_level')}`, "
        f"notional=`{first.get('local_support_notional')}`.",
        f"7. What local SL was selected? SL=`{first.get('stop_loss')}`, "
        f"reference=`{first.get('sl_reference_type')}`.",
        f"8. What were TP/CRV values? TP1=`{first.get('take_profit_1')}` "
        f"(CRV `{first.get('tp1_crv')}`), TP2=`{first.get('take_profit_2')}` "
        f"(CRV `{first.get('tp2_crv')}`).",
        f"9. What was the observed outcome? `{outcome.get('outcome')}`.",
        f"10. What were MFE/MAE? MFE=`{outcome.get('mfe_pct')}`%, "
        f"MAE=`{outcome.get('mae_pct')}`%.",
        f"11. Was the 11:45:29 window tradeable? state side=`{state_1145.get('side')}`, "
        f"reason=`{state_1145.get('reason')}`, score=`{state_1145.get('score')}`; "
        "a causal trade exists only if the row is accepted after SL/CRV gates.",
        "12. Profitability conclusion: none. This is a single-window forensic research "
        "audit and does not establish general profitability.",
        "",
        "## Old versus new raw signals",
        "",
        f"- Old LONG/SHORT: `{summary.get('legacy_raw_long_signal_count')}` / "
        f"`{summary.get('legacy_raw_short_signal_count')}`",
        f"- New LONG/SHORT: `{summary.get('new_raw_long_signal_count')}` / "
        f"`{summary.get('new_raw_short_signal_count')}`",
        f"- Additional LONG/SHORT: `{summary.get('additional_raw_long_signals')}` / "
        f"`{summary.get('additional_raw_short_signals')}`",
        f"- Accepted old LONG/SHORT: `{summary.get('legacy_accepted_long_count')}` / "
        f"`{summary.get('legacy_accepted_short_count')}`",
        f"- Additional accepted LONG/SHORT: "
        f"`{summary.get('additional_accepted_long_count')}` / "
        f"`{summary.get('additional_accepted_short_count')}`",
        f"- Retest candidates classified false by session-end SL: "
        f"`{summary.get('false_retest_trade_count')}`",
        f"- All new-logic-only trades classified false by session-end SL: "
        f"`{summary.get('false_new_logic_trade_count')}`",
        "",
        "## Reject reasons",
        "",
    ]
    for k, v in (summary.get("reject_reasons") or {}).items():
        lines.append(f"- `{k}`: {v}")
    lines += ["", "## Accepted candidates", ""]
    for row in summary.get("accepted") or []:
        lines.append(
            f"- `{row.get('candidate_id')}` {row.get('side')} signal=`{row.get('signal_time')}` "
            f"entry=`{row.get('entry_price')}` SL=`{row.get('stop_loss')}` "
            f"TP1=`{row.get('take_profit_1')}` TP2=`{row.get('take_profit_2')}` "
            f"CRV1=`{row.get('tp1_crv')}` CRV2=`{row.get('tp2_crv')}`"
        )
    lines += [
        "",
        "## Outcomes (session end)",
        "",
        f"- Counts: `{summary.get('outcome_counts_session_end')}`",
        f"- TP1-related: {summary.get('tp1_related_count')}",
        f"- TP2-related: {summary.get('tp2_related_count')}",
        f"- SL-related: {summary.get('sl_related_count')}",
        "",
        "## Best / worst (diagnostic)",
        "",
        f"- Best: `{summary.get('best_candidate')}`",
        f"- Worst: `{summary.get('worst_candidate')}`",
        "",
        "## Method notes",
        "",
        "- Was the large upside move tradeable? See earliest LONG + forward outcomes.",
        "- Was the later downside tradeable? See earliest SHORT + forward outcomes.",
        "- This report does **not** claim a profitable strategy from one history window.",
        "",
    ]
    for lim in summary.get("limitations") or []:
        lines.append(f"- {lim}")
    lines.append("")
    return "\n".join(lines)


def prepare_tracker_state(
    *,
    db: Any,
    symbol: str,
    start: datetime,
    end: datetime,
    params: AuditParams,
) -> dict[str, Any]:
    movement = params.movement
    movement.sample_seconds = params.sample_seconds
    movement.target_bps = params.target_bps
    movement.distance_max_pct = params.distance_max_pct
    movement.near_min_distance_pct = params.near_min_distance_pct
    movement.near_max_distance_pct = params.near_max_distance_pct
    movement.near_top_n = params.near_top_n
    movement.near_max_buckets = params.near_max_buckets
    movement.wall_params = WallDetectorParams(distance_max_pct=params.distance_max_pct)

    snap_ts, snap_u, snap_seq = find_bootstrap_snapshot(
        db, symbol=symbol, start=start, end=end
    )
    events = load_events(
        db,
        symbol=symbol,
        snapshot_ts=snap_ts,
        snapshot_u=snap_u,
        snapshot_seq=snap_seq,
        end=end,
    )
    sample_times: list[datetime] = []
    t = start
    while t <= end:
        sample_times.append(t)
        t += timedelta(seconds=params.sample_seconds)
    if not sample_times or sample_times[-1] < end:
        sample_times.append(end)

    final_book, timed_books = reconstruct_with_samples(
        events, sample_times=sample_times, end=end
    )
    if end not in timed_books:
        timed_books[end] = final_book

    prices: list[Decimal] = []
    for book in timed_books.values():
        prices.extend(book.bids)
        prices.extend(book.asks)
    tick = infer_tick_size(prices) if prices else Decimal("0.0001")
    mid_end = final_book.mid_price() or Decimal("1")
    bucket_size = choose_bucket_size(mid_end, tick, params.target_bps)

    trade_intervals: dict[datetime, tuple[Decimal, Decimal, Decimal, Decimal]] = {}
    oi_at: dict[datetime, Decimal | None] = {}
    prev_ts = start
    for ts in sample_times:
        if ts == sample_times[0]:
            trade_intervals[ts] = (Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"))
        else:
            trade_intervals[ts] = load_trades_between(
                db, symbol=symbol, start=prev_ts, end=ts
            )
        oi_at[ts] = load_oi_at(db, symbol=symbol, as_of=ts)
        prev_ts = ts

    snapshots = build_snapshots_from_books(
        timed_books,
        sample_times,
        bucket_size=bucket_size,
        params=movement,
        trade_intervals=trade_intervals,
        oi_at=oi_at,
    )
    transitions = build_transitions(snapshots, movement)
    sequences = build_sequences(snapshots, transitions, movement)

    near_params = NearParams(
        near_min_distance_pct=params.near_min_distance_pct,
        near_max_distance_pct=params.near_max_distance_pct,
        near_top_n=params.near_top_n,
        near_max_buckets=params.near_max_buckets,
        sample_seconds=params.sample_seconds,
    )
    near_views: list[NearSnapshotView] = []
    for snap in snapshots:
        near_views.append(
            NearSnapshotView(
                nearest_bid=snap.nearest_bid,
                nearest_ask=snap.nearest_ask,
                dominant_bid=snap.dominant_bid,
                dominant_ask=snap.dominant_ask,
                near_bids=snap.near_bids,
                near_asks=snap.near_asks,
                total_near_bid_notional=snap.total_near_bid_notional,
                total_near_ask_notional=snap.total_near_ask_notional,
                near_book_imbalance=snap.near_book_imbalance,
                nearest_bid_ask_gap=snap.nearest_bid_ask_gap,
                mid_position_between_near_walls=snap.mid_position_between_near_walls,
                near_bid_weighted_price=snap.near_bid_weighted_price,
                near_ask_weighted_price=snap.near_ask_weighted_price,
                weighted_liquidity_gap=snap.weighted_liquidity_gap,
                weighted_liquidity_midpoint=snap.weighted_liquidity_midpoint,
            )
        )
    near_tx = build_near_ask_transitions(snapshots, near_views, near_params)
    ladder = detect_ask_ladder_sequences(snapshots, near_views, near_params)
    liqs = load_liquidations(db, symbol=symbol, start=start, end=end)
    book_path = [(s.timestamp, s.mid_price) for s in snapshots]
    trade_path = load_trade_price_path(db, symbol=symbol, start=start, end=end)
    price_path = merge_price_paths(book_path, trade_path)
    return {
        "snapshots": snapshots,
        "near_views": near_views,
        "sequences": sequences,
        "transitions": transitions,
        "near_tx": near_tx,
        "ladder_seqs": ladder,
        "liquidations": liqs,
        "price_path": price_path,
        "tick": tick,
        "bucket_size": bucket_size,
    }


def run_audit(args: argparse.Namespace) -> dict[str, Any]:
    start = parse_utc(args.start)
    end = parse_utc(args.end)
    params = AuditParams(
        sample_seconds=int(args.sample_seconds),
        target_bps=float(args.target_bps),
        near_min_distance_pct=float(args.near_min_distance_pct),
        near_max_distance_pct=float(args.near_max_distance_pct),
        near_top_n=int(args.near_top_n),
        minimum_entry_score=int(args.minimum_entry_score),
        entry_mode=str(args.entry_mode),
        sl_buffer_bps=float(args.sl_buffer_bps),
        min_sl_distance_pct=float(args.min_sl_distance_pct),
        max_sl_distance_pct=float(args.max_sl_distance_pct),
        tp_front_run_bps=float(args.tp_front_run_bps),
        min_crv_tp1=float(args.min_crv_tp1),
        min_crv_tp2=float(args.min_crv_tp2),
        cooldown_minutes=int(args.cooldown_minutes),
        structure_state_max_age_seconds=int(args.structure_state_max_age_seconds),
        contradiction_state_max_age_seconds=int(args.contradiction_state_max_age_seconds),
        reclaim_confirm_snapshots=int(args.reclaim_confirm_snapshots),
        retest_distance_bps=float(args.retest_distance_bps),
        retest_max_undershoot_bps=float(args.retest_max_undershoot_bps),
        retest_sl_buffer_bps=float(args.retest_sl_buffer_bps),
        retest_max_sl_distance_pct=float(args.retest_max_sl_distance_pct),
    )
    out_dir = Path(args.output_dir) if args.output_dir else (
        PROJECT_ROOT
        / "results"
        / f"orderbook_trade_candidate_full_history_{utc_now().strftime('%Y%m%dT%H%M%SZ')}"
    )
    db = connect_readonly()
    try:
        state = prepare_tracker_state(
            db=db, symbol=args.symbol, start=start, end=end, params=params
        )
        summary = run_audit_from_snapshots(
            snapshots=state["snapshots"],
            near_views=state["near_views"],
            sequences=state["sequences"],
            transitions=state["transitions"],
            near_tx=state["near_tx"],
            ladder_seqs=state["ladder_seqs"],
            liquidations=state["liquidations"],
            price_path=state["price_path"],
            params=params,
            end=end,
            output_dir=out_dir,
        )
        summary["symbol"] = args.symbol
        summary["start"] = start.isoformat()
        summary["end"] = end.isoformat()
        summary["output_dir"] = str(out_dir)
        (out_dir / "strategy_summary.json").write_bytes(
            orjson.dumps(summary, option=orjson.OPT_INDENT_2)
        )
        (out_dir / "REPORT.md").write_text(render_audit_report(summary), encoding="utf-8")
        return summary
    finally:
        db.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Causal orderbook trade candidate audit")
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--sample-seconds", type=int, default=30)
    p.add_argument("--target-bps", type=float, default=10.0)
    p.add_argument("--near-min-distance-pct", type=float, default=0.10)
    p.add_argument("--near-max-distance-pct", type=float, default=1.50)
    p.add_argument("--near-top-n", type=int, default=3)
    p.add_argument("--minimum-entry-score", type=int, default=5)
    p.add_argument(
        "--entry-mode",
        default="next-snapshot-mid",
        choices=["mid", "next-trade", "next-snapshot-mid"],
    )
    p.add_argument("--sl-buffer-bps", type=float, default=8.0)
    p.add_argument("--min-sl-distance-pct", type=float, default=0.20)
    p.add_argument("--max-sl-distance-pct", type=float, default=1.50)
    p.add_argument("--tp-front-run-bps", type=float, default=5.0)
    p.add_argument("--min-crv-tp1", type=float, default=1.20)
    p.add_argument("--min-crv-tp2", type=float, default=1.80)
    p.add_argument("--cooldown-minutes", type=int, default=5)
    p.add_argument("--structure-state-max-age-seconds", type=int, default=180)
    p.add_argument("--contradiction-state-max-age-seconds", type=int, default=120)
    p.add_argument("--reclaim-confirm-snapshots", type=int, default=2)
    p.add_argument("--retest-distance-bps", type=float, default=12.0)
    p.add_argument("--retest-max-undershoot-bps", type=float, default=10.0)
    p.add_argument("--retest-sl-buffer-bps", type=float, default=5.0)
    p.add_argument("--retest-max-sl-distance-pct", type=float, default=0.60)
    p.add_argument("--output-dir", default=None)
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    summary = run_audit(args)
    sys.stdout.buffer.write(
        orjson.dumps(
            {
                "decision": summary.get("decision"),
                "long": summary.get("long_candidate_count"),
                "short": summary.get("short_candidate_count"),
                "output_dir": summary.get("output_dir"),
            },
            option=orjson.OPT_INDENT_2,
        )
    )
    sys.stdout.buffer.write(b"\n")
    return 0 if summary.get("decision") != "RETEST_RECLAIM_ENTRY_FAILED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
