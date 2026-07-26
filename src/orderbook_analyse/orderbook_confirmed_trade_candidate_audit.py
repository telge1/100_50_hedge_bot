"""Confirmed orderbook trade-candidate audit (research only).

Two-phase causal pipeline:

1. Open a setup-typed Watch at snapshot ``t0`` (watch snapshot is not a confirmation).
2. Require ``confirmation_snapshots`` consecutive later snapshots to confirm.
3. Fill at the next snapshot mid after ``confirm_time``.

Strict ordering: ``watch_open_time < confirm_time < entry_time``.
``signal_time`` equals ``confirm_time``. Forward outcomes start strictly after entry.

Does not place live orders. Reuses helpers from
``orderbook_trade_candidate_audit`` without modifying that module.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

import orjson

from orderbook_analyse.dynamic_wall_detector import (
    PROJECT_ROOT,
    connect_readonly,
    parse_utc,
    utc_now,
    write_csv,
)
from orderbook_analyse.liquidation_analysis import LiquidationEvent
from orderbook_analyse.near_liquidity import (
    BEARISH_LIQUIDITY_SHIFT,
    BULLISH_LIQUIDITY_SHIFT,
    NEAR_ASK_BUILDING,
    NEAR_ASK_MOVING_HIGHER,
    NEAR_ASK_MOVING_LOWER,
    NEAR_ASK_STABLE,
    NearAskTransition,
    NearSnapshotView,
)
from orderbook_analyse.orderbook_trade_candidate_audit import (
    HORIZONS_SEC,
    LONG,
    NO_TRADE,
    NO_TRADE_INSUFFICIENT_CRV,
    NO_TRADE_NO_ENTRY_PRICE,
    SHORT,
    SUPPORT_RETEST_RECLAIM_LONG,
    AcceptedCandidate,
    AuditParams,
    CandidateDecision,
    RetestState,
    RetestTracker,
    ScoreComponent,
    _ensure_utc,
    _fmt,
    build_accepted_candidate,
    build_regime_at,
    compute_retest_stop_loss,
    compute_take_profits,
    cumulative_delta,
    cumulative_oi_change,
    evaluate_candidate_at,
    prepare_tracker_state,
    price_trend,
    recent_near_ask_class,
    resolve_entry_price,
    simulate_trade_outcome,
)
from orderbook_analyse.wall_movement_tracker import (
    SequenceRecord,
    SnapshotRecord,
    TransitionRecord,
)

logger = logging.getLogger(__name__)

# Confirmed setup types (only these may become confirmed entries).
BREAKOUT_RECLAIM_LONG = "BREAKOUT_RECLAIM_LONG"
RESISTANCE_REJECTION_SHORT = "RESISTANCE_REJECTION_SHORT"
FAILED_BREAKOUT_SHORT = "FAILED_BREAKOUT_SHORT"
SHORT_CONTINUATION = "SHORT_CONTINUATION"

CONFIRMED_SETUP_TYPES = frozenset(
    {
        SUPPORT_RETEST_RECLAIM_LONG,
        BREAKOUT_RECLAIM_LONG,
        RESISTANCE_REJECTION_SHORT,
        FAILED_BREAKOUT_SHORT,
        SHORT_CONTINUATION,
    }
)

LONG_SETUPS = frozenset({SUPPORT_RETEST_RECLAIM_LONG, BREAKOUT_RECLAIM_LONG})
SHORT_SETUPS = frozenset(
    {RESISTANCE_REJECTION_SHORT, FAILED_BREAKOUT_SHORT, SHORT_CONTINUATION}
)

# Watch / confirmation reject reasons
WATCH_OPENED = "WATCH_OPENED"
WATCH_CONTEXT_ONLY = "WATCH_CONTEXT_ONLY"
NO_CONFIRM_TIMEOUT = "NO_CONFIRM_TIMEOUT"
NO_CONFIRM_INVALIDATED = "NO_CONFIRM_INVALIDATED"
NO_CONFIRM_COUNT_RESET = "NO_CONFIRM_COUNT_RESET"
NO_CONFIRM_OPPOSITE_RECLAIM = "NO_CONFIRM_OPPOSITE_RECLAIM"
NO_CONFIRM_STRUCTURE_FAILED = "NO_CONFIRM_STRUCTURE_FAILED"
NO_CONFIRM_FEATURE_COUNT = "NO_CONFIRM_FEATURE_COUNT"
NO_CONFIRM_DELTA_HARD_GATE = "NO_CONFIRM_DELTA_HARD_GATE"
NO_CONFIRM_OI_HARD_GATE = "NO_CONFIRM_OI_HARD_GATE"
NO_CONFIRM_GENERIC_NOT_ELIGIBLE = "NO_CONFIRM_GENERIC_NOT_ELIGIBLE"
CONFIRMED = "CONFIRMED"


@dataclass
class ConfirmedAuditParams:
    """Confirmed-entry params; embeds base AuditParams for reuse."""

    base: AuditParams = field(default_factory=AuditParams)
    confirmation_snapshots: int = 2
    confirmation_max_seconds: int = 180
    confirmation_min_feature_count: int = 3
    confirmation_require_directional_delta: bool = False
    confirmation_require_supportive_oi: bool = False
    confirmation_price_bps: float = 3.0
    breakout_reclaim_bps: float = 8.0
    rejection_touch_bps: float = 12.0


@dataclass
class LevelMemory:
    """Causal memory of a broken near-ask / resistance level."""

    level: Decimal
    broken_at: datetime
    broken_index: int
    side_hint: str  # LONG breakout above ask, or SHORT after fail


@dataclass
class WatchState:
    setup_type: str
    side: str
    watch_index: int
    watch_open_time: datetime
    watch_mid: Decimal
    reference_level: Decimal | None
    invalidation_level: Decimal | None
    context_decision: CandidateDecision
    confirm_count: int = 0
    last_confirm_index: int | None = None
    confirm_time: datetime | None = None
    confirm_index: int | None = None
    confirm_features: list[str] = field(default_factory=list)
    closed: bool = False
    close_reason: str | None = None
    transitions: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ConfirmedCandidate:
    candidate_id: str
    episode_id: str
    setup_type: str
    side: str
    watch_open_time: datetime
    confirm_time: datetime
    signal_time: datetime
    entry_time: datetime
    entry_price: Decimal
    watch_mid: Decimal
    confirm_mid: Decimal
    confirm_feature_count: int
    confirm_features: list[str]
    confirm_snapshot_count: int
    reference_level: Decimal | None
    invalidation_level: Decimal | None
    accepted: AcceptedCandidate
    score: int
    components: list[ScoreComponent]


def setup_side(setup_type: str) -> str:
    if setup_type in LONG_SETUPS:
        return LONG
    if setup_type in SHORT_SETUPS:
        return SHORT
    raise ValueError(f"unknown setup_type={setup_type!r}")


def _bps_distance(a: Decimal, b: Decimal) -> float:
    if b == 0:
        return float("inf")
    return float(abs(a - b) / b * Decimal("10000"))


class BreakoutMemoryTracker:
    """Track causal breakouts above near-ask for reclaim / failed-breakout setups."""

    def __init__(self) -> None:
        self.memory: LevelMemory | None = None

    def process(self, snapshots: Sequence[SnapshotRecord], *, index: int) -> LevelMemory | None:
        if index <= 0:
            return self.memory
        prev = snapshots[index - 1]
        snap = snapshots[index]
        if prev.nearest_ask is not None:
            level = prev.nearest_ask.price
            if prev.mid_price <= level < snap.mid_price:
                self.memory = LevelMemory(
                    level=level,
                    broken_at=snap.timestamp,
                    broken_index=index,
                    side_hint="BREAKOUT",
                )
        if self.memory is not None:
            # Age out very old memories via caller params later; keep until replaced.
            if snap.mid_price < self.memory.level:
                # still remember level for failed-breakout classification
                pass
        return self.memory


def classify_setup_type(
    *,
    index: int,
    snapshots: Sequence[SnapshotRecord],
    decision: CandidateDecision,
    retest_state: RetestState | None,
    breakout_memory: LevelMemory | None,
    regime: MappingLike,
    params: ConfirmedAuditParams,
) -> str | None:
    """Map causal structure into one confirmed setup type, or None."""
    snap = snapshots[index]
    auction = str(regime.get("auction_direction") or "INCONCLUSIVE")
    bias = str(regime.get("short_term_bias") or "INCONCLUSIVE")
    near_ask_dir = str(regime.get("near_ask_direction") or "INCONCLUSIVE")
    near_bid_dir = str(regime.get("near_bid_direction") or "INCONCLUSIVE")
    near_class = decision.near_ask_class

    # 1) Support retest reclaim long
    if (
        decision.entry_setup_type == SUPPORT_RETEST_RECLAIM_LONG
        or (
            retest_state is not None
            and retest_state.reclaim_confirm_time is not None
            and not retest_state.broken
            and retest_state.reclaim_snapshot_count
            >= params.base.reclaim_confirm_snapshots
        )
    ):
        return SUPPORT_RETEST_RECLAIM_LONG

    # 2) Breakout reclaim long: prior breakout level, mid back above it
    if breakout_memory is not None:
        level = breakout_memory.level
        above = snap.mid_price >= level
        age_ok = (
            _ensure_utc(snap.timestamp) - _ensure_utc(breakout_memory.broken_at)
        ).total_seconds() <= params.confirmation_max_seconds * 2
        pulled_back = False
        for j in range(breakout_memory.broken_index + 1, index):
            if snapshots[j].mid_price < level:
                pulled_back = True
                break
        rising = decision.active_rising_bid_shifts >= 1 or near_bid_dir in {
            "HIGHER",
            "STABLE",
        }
        if (
            age_ok
            and above
            and pulled_back
            and rising
            and (
                auction == "HIGHER"
                or bias == BULLISH_LIQUIDITY_SHIFT
                or near_class == NEAR_ASK_MOVING_HIGHER
            )
            and decision.active_falling_ask_shifts == 0
        ):
            return BREAKOUT_RECLAIM_LONG

        # 3) Failed breakout short: traded above level, now clearly back below
        if age_ok and snap.mid_price < level:
            undershoot = _bps_distance(snap.mid_price, level)
            if undershoot >= params.confirmation_price_bps and (
                auction == "LOWER"
                or bias == BEARISH_LIQUIDITY_SHIFT
                or near_class == NEAR_ASK_MOVING_LOWER
                or decision.active_falling_ask_shifts > 0
                or near_bid_dir == "LOWER"
            ):
                return FAILED_BREAKOUT_SHORT

    # 4) Resistance rejection short: touch near ask then reject without sustained break
    if snap.nearest_ask is not None:
        ask = snap.nearest_ask.price
        touch = _bps_distance(snap.mid_price, ask) <= params.rejection_touch_bps
        below = snap.mid_price < ask
        rejected = below and touch
        if index > 0:
            prev = snapshots[index - 1]
            if prev.nearest_ask is not None:
                # approached then turned down
                if prev.mid_price >= snap.mid_price and prev.mid_price <= ask:
                    rejected = True
        if rejected and below and (
            near_class in {NEAR_ASK_STABLE, NEAR_ASK_BUILDING, NEAR_ASK_MOVING_LOWER}
            or near_ask_dir in {"STABLE", "LOWER"}
        ) and (
            auction == "LOWER"
            or bias == BEARISH_LIQUIDITY_SHIFT
            or decision.trade_delta < 0
            or near_bid_dir == "LOWER"
        ):
            # Prefer failed-breakout when memory says we were above
            if not (
                breakout_memory is not None
                and any(
                    snapshots[j].mid_price > breakout_memory.level
                    for j in range(max(0, breakout_memory.broken_index), index + 1)
                )
                and snap.mid_price < breakout_memory.level
            ):
                return RESISTANCE_REJECTION_SHORT

    # 5) Short continuation: active bearish structure without needing breakout memory
    if (
        decision.active_falling_ask_shifts >= 2
        or (
            decision.active_falling_ask_shifts >= 1
            and (auction == "LOWER" or bias == BEARISH_LIQUIDITY_SHIFT)
        )
    ) and decision.active_rising_bid_shifts < 2 and bias != BULLISH_LIQUIDITY_SHIFT:
        if near_bid_dir == "LOWER" or auction == "LOWER" or decision.trade_delta < 0:
            return SHORT_CONTINUATION

    return None


# Typing alias without importing Mapping from typing twice awkwardly
MappingLike = dict[str, Any]


def default_invalidation_level(
    *,
    setup_type: str,
    snap: SnapshotRecord,
    retest_state: RetestState | None,
    breakout_memory: LevelMemory | None,
) -> Decimal | None:
    if setup_type == SUPPORT_RETEST_RECLAIM_LONG:
        if retest_state is not None and retest_state.retest_low is not None:
            return retest_state.retest_low
        if retest_state is not None:
            return retest_state.reference_level
        return snap.nearest_bid.price if snap.nearest_bid else None
    if setup_type == BREAKOUT_RECLAIM_LONG:
        if breakout_memory is not None:
            return breakout_memory.level
        return snap.nearest_bid.price if snap.nearest_bid else None
    if setup_type == FAILED_BREAKOUT_SHORT:
        if breakout_memory is not None:
            return breakout_memory.level
        return snap.nearest_ask.price if snap.nearest_ask else None
    if setup_type == RESISTANCE_REJECTION_SHORT:
        return snap.nearest_ask.price if snap.nearest_ask else None
    if setup_type == SHORT_CONTINUATION:
        return snap.nearest_ask.price if snap.nearest_ask else snap.mid_price
    return None


def default_reference_level(
    *,
    setup_type: str,
    snap: SnapshotRecord,
    retest_state: RetestState | None,
    breakout_memory: LevelMemory | None,
) -> Decimal | None:
    if setup_type == SUPPORT_RETEST_RECLAIM_LONG and retest_state is not None:
        return retest_state.reference_level
    if setup_type in {BREAKOUT_RECLAIM_LONG, FAILED_BREAKOUT_SHORT} and breakout_memory:
        return breakout_memory.level
    if setup_type == RESISTANCE_REJECTION_SHORT and snap.nearest_ask is not None:
        return snap.nearest_ask.price
    if setup_type == SHORT_CONTINUATION and snap.nearest_ask is not None:
        return snap.nearest_ask.price
    return snap.mid_price


def structure_holds(
    *,
    setup_type: str,
    snap: SnapshotRecord,
    reference_level: Decimal | None,
    params: ConfirmedAuditParams,
) -> bool:
    mid = snap.mid_price
    if reference_level is None:
        return False
    tol = Decimal(str(params.confirmation_price_bps)) / Decimal("10000")
    if setup_type in LONG_SETUPS:
        return mid >= reference_level * (Decimal("1") - tol)
    # shorts: remain below resistance / failed breakout level
    return mid <= reference_level * (Decimal("1") + tol)


def invalidation_hit(
    *,
    setup_type: str,
    snap: SnapshotRecord,
    invalidation_level: Decimal | None,
) -> bool:
    if invalidation_level is None:
        return False
    mid = snap.mid_price
    if setup_type in LONG_SETUPS:
        return mid < invalidation_level
    return mid > invalidation_level


def opposite_reclaim_active(
    *,
    setup_type: str,
    decision_like: CandidateDecision,
    regime: MappingLike,
    near_class: str | None,
) -> bool:
    auction = str(regime.get("auction_direction") or "INCONCLUSIVE")
    bias = str(regime.get("short_term_bias") or "INCONCLUSIVE")
    if setup_type in LONG_SETUPS:
        return (
            decision_like.active_falling_ask_shifts > 0
            and near_class == NEAR_ASK_MOVING_LOWER
        ) or bias == BEARISH_LIQUIDITY_SHIFT and auction == "LOWER"
    return (
        decision_like.active_rising_bid_shifts >= 2
        and near_class == NEAR_ASK_MOVING_HIGHER
    ) or (bias == BULLISH_LIQUIDITY_SHIFT and auction == "HIGHER")


def collect_confirm_features(
    *,
    side: str,
    snap: SnapshotRecord,
    previous: SnapshotRecord | None,
    regime: MappingLike,
    near_class: str | None,
    delta: Decimal,
    oi_chg: Decimal | None,
    trend: str,
) -> list[str]:
    features: list[str] = []
    auction = str(regime.get("auction_direction") or "INCONCLUSIVE")
    bias = str(regime.get("short_term_bias") or "INCONCLUSIVE")
    near_bid_dir = str(regime.get("near_bid_direction") or "INCONCLUSIVE")
    near_ask_dir = str(regime.get("near_ask_direction") or "INCONCLUSIVE")

    if side == LONG:
        if delta > 0:
            features.append("directional_delta")
        if oi_chg is not None and oi_chg >= 0:
            features.append("supportive_oi")
        if auction == "HIGHER":
            features.append("auction")
        if bias == BULLISH_LIQUIDITY_SHIFT:
            features.append("liquidity_shift")
        if near_bid_dir in {"HIGHER", "STABLE"} or (
            snap.nearest_bid is not None and snap.nearest_bid.price < snap.mid_price
        ):
            features.append("near_book")
        if near_class in {NEAR_ASK_MOVING_HIGHER, NEAR_ASK_STABLE} or near_ask_dir in {
            "HIGHER",
            "STABLE",
        }:
            features.append("near_ask")
        if trend == "UP" or (
            previous is not None and snap.mid_price >= previous.mid_price
        ):
            features.append("price_direction")
    else:
        if delta < 0:
            features.append("directional_delta")
        # Short OI: falling OI (delever) OR rising OI with sell delta both count
        if oi_chg is not None and (oi_chg <= 0 or delta < 0):
            features.append("supportive_oi")
        if auction == "LOWER":
            features.append("auction")
        if bias == BEARISH_LIQUIDITY_SHIFT:
            features.append("liquidity_shift")
        if near_ask_dir in {"LOWER", "STABLE"} or near_class in {
            NEAR_ASK_MOVING_LOWER,
            NEAR_ASK_STABLE,
            NEAR_ASK_BUILDING,
        }:
            features.append("near_ask")
        if near_bid_dir == "LOWER" or (
            snap.nearest_bid is not None and snap.nearest_bid.price < snap.mid_price
        ):
            features.append("near_book")
        if trend == "DOWN" or (
            previous is not None and snap.mid_price <= previous.mid_price
        ):
            features.append("price_direction")
    # unique preserve order
    seen: set[str] = set()
    out: list[str] = []
    for f in features:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def evaluate_confirmation_snapshot(
    *,
    watch: WatchState,
    index: int,
    snapshots: Sequence[SnapshotRecord],
    near_views: Sequence[NearSnapshotView],
    sequences: Sequence[SequenceRecord],
    near_tx: Sequence[NearAskTransition],
    ladder_seqs: Sequence[Any],
    liquidations: Sequence[LiquidationEvent],
    params: ConfirmedAuditParams,
) -> dict[str, Any]:
    """Advance one watch at snapshot index (> watch_index). Never treats t0 as confirm."""
    assert index > watch.watch_index
    snap = snapshots[index]
    as_of = snap.timestamp
    age = (_ensure_utc(as_of) - _ensure_utc(watch.watch_open_time)).total_seconds()
    if age > params.confirmation_max_seconds:
        watch.closed = True
        watch.close_reason = NO_CONFIRM_TIMEOUT
        return {"status": NO_CONFIRM_TIMEOUT, "watch": watch}

    if invalidation_hit(
        setup_type=watch.setup_type,
        snap=snap,
        invalidation_level=watch.invalidation_level,
    ):
        watch.closed = True
        watch.close_reason = NO_CONFIRM_INVALIDATED
        return {"status": NO_CONFIRM_INVALIDATED, "watch": watch}

    # Causal structure snapshot via evaluate_candidate_at (as_of = this index)
    decision = evaluate_candidate_at(
        index=index,
        snapshots=snapshots,
        near_views=near_views,
        sequences=sequences,
        transitions=[],
        near_tx=near_tx,
        ladder_seqs=ladder_seqs,
        liquidations=liquidations,
        params=params.base,
        retest_state=None,
    )
    regime = build_regime_at(
        snapshots, near_views, near_tx, ladder_seqs, index=index
    )
    near_class = recent_near_ask_class(near_tx, as_of=as_of)
    opposite = opposite_reclaim_active(
        setup_type=watch.setup_type,
        decision_like=decision,
        regime=regime,
        near_class=near_class,
    )
    if opposite:
        watch.closed = True
        watch.close_reason = NO_CONFIRM_OPPOSITE_RECLAIM
        return {"status": NO_CONFIRM_OPPOSITE_RECLAIM, "watch": watch}

    holds = structure_holds(
        setup_type=watch.setup_type,
        snap=snap,
        reference_level=watch.reference_level,
        params=params,
    )
    if not holds:
        if watch.confirm_count > 0:
            watch.confirm_count = 0
            watch.last_confirm_index = None
            watch.transitions.append(
                {
                    "timestamp": as_of.isoformat(),
                    "transition": NO_CONFIRM_COUNT_RESET,
                    "setup_type": watch.setup_type,
                    "mid": _fmt(snap.mid_price),
                }
            )
            return {"status": NO_CONFIRM_COUNT_RESET, "watch": watch}
        return {"status": NO_CONFIRM_STRUCTURE_FAILED, "watch": watch}

    # Consecutive confirmation snapshots only
    if watch.last_confirm_index is not None and index != watch.last_confirm_index + 1:
        watch.confirm_count = 0
        watch.last_confirm_index = None
        watch.transitions.append(
            {
                "timestamp": as_of.isoformat(),
                "transition": NO_CONFIRM_COUNT_RESET,
                "setup_type": watch.setup_type,
                "detail": "non_consecutive",
            }
        )

    delta = cumulative_delta(
        snapshots, end_index=index, lookback=params.base.flow_lookback_snapshots
    )
    oi_chg = cumulative_oi_change(
        snapshots, end_index=index, lookback=params.base.flow_lookback_snapshots
    )
    trend = price_trend(
        snapshots, end_index=index, lookback=params.base.flow_lookback_snapshots
    )
    previous = snapshots[index - 1] if index > 0 else None
    features = collect_confirm_features(
        side=watch.side,
        snap=snap,
        previous=previous,
        regime=regime,
        near_class=near_class,
        delta=delta,
        oi_chg=oi_chg,
        trend=trend,
    )

    if params.confirmation_require_directional_delta:
        if "directional_delta" not in features:
            return {"status": NO_CONFIRM_DELTA_HARD_GATE, "watch": watch, "features": features}
    if params.confirmation_require_supportive_oi:
        if "supportive_oi" not in features:
            return {"status": NO_CONFIRM_OI_HARD_GATE, "watch": watch, "features": features}

    watch.confirm_count += 1
    watch.last_confirm_index = index
    watch.confirm_features = features
    watch.transitions.append(
        {
            "timestamp": as_of.isoformat(),
            "transition": "CONFIRM_SNAPSHOT",
            "setup_type": watch.setup_type,
            "confirm_count": watch.confirm_count,
            "features": ",".join(features),
            "mid": _fmt(snap.mid_price),
        }
    )

    if watch.confirm_count < params.confirmation_snapshots:
        return {
            "status": "CONFIRM_PROGRESS",
            "watch": watch,
            "features": features,
            "confirm_count": watch.confirm_count,
        }

    if len(features) < params.confirmation_min_feature_count:
        # Keep count but do not confirm yet; allow next snapshot to add features
        # Actually user said confirmation_min_feature_count is mandatory at confirm.
        # Hold confirm_time unset until features satisfied on a confirm snapshot.
        watch.confirm_count = params.confirmation_snapshots  # stay at threshold
        return {
            "status": NO_CONFIRM_FEATURE_COUNT,
            "watch": watch,
            "features": features,
            "feature_count": len(features),
        }

    watch.confirm_time = as_of
    watch.confirm_index = index
    watch.closed = True
    watch.close_reason = CONFIRMED
    watch.transitions.append(
        {
            "timestamp": as_of.isoformat(),
            "transition": CONFIRMED,
            "setup_type": watch.setup_type,
            "confirm_count": watch.confirm_count,
            "features": ",".join(features),
        }
    )
    return {
        "status": CONFIRMED,
        "watch": watch,
        "features": features,
        "confirm_count": watch.confirm_count,
        "decision": decision,
        "regime": regime,
    }


def build_confirmed_accepted(
    *,
    watch: WatchState,
    confirm_decision: CandidateDecision,
    confirm_index: int,
    snapshots: Sequence[SnapshotRecord],
    price_path: Sequence[tuple[datetime, Decimal]],
    params: ConfirmedAuditParams,
    candidate_id: str,
    episode_id: str,
) -> ConfirmedCandidate | CandidateDecision:
    """Build accepted candidate with entry after confirm; SL/TP from confirm context."""
    if watch.confirm_time is None or watch.confirm_index is None:
        return replace(
            confirm_decision,
            side=NO_TRADE,
            reason=NO_CONFIRM_STRUCTURE_FAILED,
        )
    entry = resolve_entry_price(
        mode="next-snapshot-mid",
        signal_index=confirm_index,
        snapshots=snapshots,
        trades=price_path,
    )
    if entry is None:
        return replace(confirm_decision, side=NO_TRADE, reason=NO_TRADE_NO_ENTRY_PRICE)
    entry_time, entry_price = entry
    if not (
        _ensure_utc(watch.watch_open_time)
        < _ensure_utc(watch.confirm_time)
        < _ensure_utc(entry_time)
    ):
        return replace(confirm_decision, side=NO_TRADE, reason=NO_TRADE_NO_ENTRY_PRICE)

    # Force signal_time = confirm_time on decision used for outputs
    decision = replace(
        confirm_decision,
        signal_time=watch.confirm_time,
        side=watch.side,
        reason=watch.setup_type,
        entry_setup_type=watch.setup_type,
        nearest_bid=confirm_decision.nearest_bid,
        nearest_ask=confirm_decision.nearest_ask,
        dominant_bid=confirm_decision.dominant_bid,
        dominant_ask=confirm_decision.dominant_ask,
        mid=snapshots[confirm_index].mid_price,
        retest_reference_level=watch.reference_level,
        retest_low=(
            watch.invalidation_level
            if watch.setup_type == SUPPORT_RETEST_RECLAIM_LONG
            else confirm_decision.retest_low
        ),
        local_support_level=confirm_decision.nearest_bid,
    )

    if watch.setup_type == SUPPORT_RETEST_RECLAIM_LONG:
        sl = compute_retest_stop_loss(
            entry=entry_price,
            retest_low=watch.invalidation_level,
            local_near_bid=decision.nearest_bid,
            reclaim_level=watch.reference_level,
            deeper_bid_floor=decision.dominant_bid,
            params=params.base,
        )
        if isinstance(sl, str):
            return replace(decision, side=NO_TRADE, reason=sl)
        ref_type, ref_level, stop, dist_pct, risk = sl
        tp_fields, tp_reject = compute_take_profits(
            side=LONG,
            entry=entry_price,
            stop_loss=stop,
            nearest_bid=decision.nearest_bid,
            nearest_ask=decision.nearest_ask,
            dominant_bid=decision.dominant_bid,
            dominant_ask=decision.dominant_ask,
            params=params.base,
        )
        if tp_reject or tp_fields is None:
            return replace(
                decision,
                side=NO_TRADE,
                reason=tp_reject or NO_TRADE_INSUFFICIENT_CRV,
            )
        accepted = AcceptedCandidate(
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
            sl_buffer_bps=params.base.retest_sl_buffer_bps,
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
    else:
        built = build_accepted_candidate(
            decision,
            candidate_id=candidate_id,
            episode_id=episode_id,
            entry_time=entry_time,
            entry_price=entry_price,
            params=params.base,
        )
        if isinstance(built, CandidateDecision):
            return built
        accepted = built

    comps = list(decision.components)
    comps.append(
        ScoreComponent(
            "confirmation_features",
            len(watch.confirm_features),
            detail=",".join(watch.confirm_features),
        )
    )
    return ConfirmedCandidate(
        candidate_id=candidate_id,
        episode_id=episode_id,
        setup_type=watch.setup_type,
        side=watch.side,
        watch_open_time=watch.watch_open_time,
        confirm_time=watch.confirm_time,
        signal_time=watch.confirm_time,
        entry_time=entry_time,
        entry_price=entry_price,
        watch_mid=watch.watch_mid,
        confirm_mid=snapshots[confirm_index].mid_price,
        confirm_feature_count=len(watch.confirm_features),
        confirm_features=list(watch.confirm_features),
        confirm_snapshot_count=watch.confirm_count,
        reference_level=watch.reference_level,
        invalidation_level=watch.invalidation_level,
        accepted=accepted,
        score=decision.score + len(watch.confirm_features),
        components=comps,
    )


def confirmed_to_row(c: ConfirmedCandidate) -> dict[str, Any]:
    a = c.accepted
    d = a.decision
    return {
        "candidate_id": c.candidate_id,
        "episode_id": c.episode_id,
        "setup_type": c.setup_type,
        "side": c.side,
        "watch_open_time": c.watch_open_time.isoformat(),
        "confirm_time": c.confirm_time.isoformat(),
        "signal_time": c.signal_time.isoformat(),
        "entry_time": c.entry_time.isoformat(),
        "entry_price": _fmt(c.entry_price),
        "watch_mid": _fmt(c.watch_mid),
        "confirm_mid": _fmt(c.confirm_mid),
        "confirm_feature_count": c.confirm_feature_count,
        "confirm_features": ",".join(c.confirm_features),
        "confirm_snapshot_count": c.confirm_snapshot_count,
        "reference_level": _fmt(c.reference_level),
        "invalidation_level": _fmt(c.invalidation_level),
        "score": c.score,
        "stop_loss": _fmt(a.stop_loss),
        "take_profit_1": _fmt(a.take_profit_1),
        "take_profit_2": _fmt(a.take_profit_2),
        "sl_reference_type": a.sl_reference_type,
        "sl_reference_level": _fmt(a.sl_reference_level),
        "stop_distance_pct": round(a.stop_distance_pct, 6),
        "tp1_crv": None if a.tp1_crv is None else round(a.tp1_crv, 6),
        "tp2_crv": None if a.tp2_crv is None else round(a.tp2_crv, 6),
        "auction_direction": d.auction_direction,
        "short_term_bias": d.short_term_bias,
        "near_ask_class": d.near_ask_class,
        "trade_delta": _fmt(d.trade_delta),
        "oi_change": _fmt(d.oi_change),
    }


def run_confirmed_audit_from_snapshots(
    *,
    snapshots: Sequence[SnapshotRecord],
    near_views: Sequence[NearSnapshotView],
    sequences: Sequence[SequenceRecord],
    transitions: Sequence[TransitionRecord],
    near_tx: Sequence[NearAskTransition],
    ladder_seqs: Sequence[Any],
    liquidations: Sequence[LiquidationEvent],
    price_path: Sequence[tuple[datetime, Decimal]],
    params: ConfirmedAuditParams,
    end: datetime,
    output_dir: Path,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    retest_tracker = RetestTracker(params.base)
    breakout_tracker = BreakoutMemoryTracker()

    watch_rows: list[dict[str, Any]] = []
    transition_rows: list[dict[str, Any]] = []
    reject_rows: list[dict[str, Any]] = []
    confirmed: list[ConfirmedCandidate] = []
    context_only_rows: list[dict[str, Any]] = []

    active_watch: WatchState | None = None
    start_i = max(2, params.base.movement.sequence_min_snapshots - 1)
    cooldown = timedelta(minutes=params.base.cooldown_minutes)
    last_entry_time: datetime | None = None
    last_entry_side: str | None = None
    episode_id = 0
    cid = 0

    for i in range(1, len(snapshots)):
        retest_state = retest_tracker.process(snapshots, index=i)
        breakout_memory = breakout_tracker.process(snapshots, index=i)
        if i < start_i:
            continue

        # Advance active watch first (confirmation uses snapshots after t0)
        if active_watch is not None and not active_watch.closed:
            if i <= active_watch.watch_index:
                continue
            result = evaluate_confirmation_snapshot(
                watch=active_watch,
                index=i,
                snapshots=snapshots,
                near_views=near_views,
                sequences=sequences,
                near_tx=near_tx,
                ladder_seqs=ladder_seqs,
                liquidations=liquidations,
                params=params,
            )
            for tr in active_watch.transitions[-1:]:
                transition_rows.append(
                    {
                        "watch_open_time": active_watch.watch_open_time.isoformat(),
                        "setup_type": active_watch.setup_type,
                        **tr,
                    }
                )
            status = result["status"]
            if status == CONFIRMED:
                confirm_decision = result["decision"]
                # Cooldown check
                if (
                    last_entry_time is not None
                    and last_entry_side == active_watch.side
                    and _ensure_utc(active_watch.confirm_time)  # type: ignore[arg-type]
                    < _ensure_utc(last_entry_time) + cooldown
                ):
                    reject_rows.append(
                        {
                            "setup_type": active_watch.setup_type,
                            "side": active_watch.side,
                            "watch_open_time": active_watch.watch_open_time.isoformat(),
                            "confirm_time": active_watch.confirm_time.isoformat()
                            if active_watch.confirm_time
                            else None,
                            "reason": "NO_TRADE_COOLDOWN",
                        }
                    )
                    active_watch = None
                    continue

                episode_id += 1
                cid += 1
                built = build_confirmed_accepted(
                    watch=active_watch,
                    confirm_decision=confirm_decision,
                    confirm_index=active_watch.confirm_index or i,
                    snapshots=snapshots,
                    price_path=price_path,
                    params=params,
                    candidate_id=f"CC{cid:04d}",
                    episode_id=f"CE{episode_id:04d}",
                )
                if isinstance(built, CandidateDecision):
                    reject_rows.append(
                        {
                            "setup_type": active_watch.setup_type,
                            "side": active_watch.side,
                            "watch_open_time": active_watch.watch_open_time.isoformat(),
                            "confirm_time": active_watch.confirm_time.isoformat()
                            if active_watch.confirm_time
                            else None,
                            "reason": built.reason,
                            "score": built.score,
                        }
                    )
                else:
                    confirmed.append(built)
                    last_entry_time = built.entry_time
                    last_entry_side = built.side
                active_watch = None
                continue
            if active_watch.closed:
                reject_rows.append(
                    {
                        "setup_type": active_watch.setup_type,
                        "side": active_watch.side,
                        "watch_open_time": active_watch.watch_open_time.isoformat(),
                        "confirm_time": None,
                        "reason": active_watch.close_reason,
                        "confirm_count": active_watch.confirm_count,
                    }
                )
                active_watch = None
            # else still progressing — do not open a second watch
            if active_watch is not None:
                continue

        # No active watch: maybe open one
        decision = evaluate_candidate_at(
            index=i,
            snapshots=snapshots,
            near_views=near_views,
            sequences=sequences,
            transitions=transitions,
            near_tx=near_tx,
            ladder_seqs=ladder_seqs,
            liquidations=liquidations,
            params=params.base,
            retest_state=retest_state,
        )
        regime = build_regime_at(
            snapshots, near_views, near_tx, ladder_seqs, index=i
        )
        setup = classify_setup_type(
            index=i,
            snapshots=snapshots,
            decision=decision,
            retest_state=retest_state,
            breakout_memory=breakout_memory,
            regime=regime,
            params=params,
        )

        # Generic LONG/SHORT provides context only
        if setup is None and decision.side in {LONG, SHORT}:
            context_only_rows.append(
                {
                    "timestamp": decision.signal_time.isoformat(),
                    "generic_side": decision.side,
                    "generic_reason": decision.reason,
                    "score": decision.score,
                    "note": NO_CONFIRM_GENERIC_NOT_ELIGIBLE,
                }
            )
            watch_rows.append(
                {
                    "timestamp": decision.signal_time.isoformat(),
                    "event": WATCH_CONTEXT_ONLY,
                    "setup_type": None,
                    "generic_side": decision.side,
                    "generic_reason": decision.reason,
                    "score": decision.score,
                }
            )
            continue

        if setup is None:
            continue

        # Open watch — t0 does not count as confirmation
        inv = default_invalidation_level(
            setup_type=setup,
            snap=snapshots[i],
            retest_state=retest_state,
            breakout_memory=breakout_memory,
        )
        ref = default_reference_level(
            setup_type=setup,
            snap=snapshots[i],
            retest_state=retest_state,
            breakout_memory=breakout_memory,
        )
        active_watch = WatchState(
            setup_type=setup,
            side=setup_side(setup),
            watch_index=i,
            watch_open_time=snapshots[i].timestamp,
            watch_mid=snapshots[i].mid_price,
            reference_level=ref,
            invalidation_level=inv,
            context_decision=decision,
        )
        active_watch.transitions.append(
            {
                "timestamp": snapshots[i].timestamp.isoformat(),
                "transition": WATCH_OPENED,
                "setup_type": setup,
                "reference_level": _fmt(ref),
                "invalidation_level": _fmt(inv),
                "mid": _fmt(snapshots[i].mid_price),
            }
        )
        watch_rows.append(
            {
                "timestamp": snapshots[i].timestamp.isoformat(),
                "event": WATCH_OPENED,
                "setup_type": setup,
                "side": setup_side(setup),
                "watch_index": i,
                "reference_level": _fmt(ref),
                "invalidation_level": _fmt(inv),
                "watch_mid": _fmt(snapshots[i].mid_price),
                "context_reason": decision.reason,
                "context_score": decision.score,
            }
        )
        transition_rows.append(
            {
                "watch_open_time": active_watch.watch_open_time.isoformat(),
                "setup_type": setup,
                **active_watch.transitions[-1],
            }
        )

    # Close dangling watch at end
    if active_watch is not None and not active_watch.closed:
        reject_rows.append(
            {
                "setup_type": active_watch.setup_type,
                "side": active_watch.side,
                "watch_open_time": active_watch.watch_open_time.isoformat(),
                "confirm_time": None,
                "reason": NO_CONFIRM_TIMEOUT,
                "confirm_count": active_watch.confirm_count,
            }
        )

    # Outcomes + outputs
    trade_rows = [confirmed_to_row(c) for c in confirmed]
    forward_rows: list[dict[str, Any]] = []
    score_rows: list[dict[str, Any]] = []
    by_setup: dict[str, list[ConfirmedCandidate]] = {s: [] for s in sorted(CONFIRMED_SETUP_TYPES)}
    for c in confirmed:
        by_setup.setdefault(c.setup_type, []).append(c)
        for comp in c.components:
            score_rows.append(comp.to_row(candidate_id=c.candidate_id))
        for h in (None, *HORIZONS_SEC):
            out = simulate_trade_outcome(
                side=c.side,
                entry_time=c.entry_time,
                entry_price=c.entry_price,
                stop_loss=c.accepted.stop_loss,
                take_profit_1=c.accepted.take_profit_1,
                take_profit_2=c.accepted.take_profit_2,
                price_path=price_path,
                end=end,
                horizon_seconds=h,
            )
            forward_rows.append(
                {
                    "candidate_id": c.candidate_id,
                    "setup_type": c.setup_type,
                    "horizon_seconds": "session_end" if h is None else h,
                    "entry_time": c.entry_time.isoformat(),
                    "entry_price": _fmt(c.entry_price),
                    "side": c.side,
                    "stop_loss": _fmt(c.accepted.stop_loss),
                    "take_profit_1": _fmt(c.accepted.take_profit_1),
                    "take_profit_2": _fmt(c.accepted.take_profit_2),
                    **out,
                }
            )

    # Per-setup CSVs
    for setup_type, items in by_setup.items():
        write_csv(
            output_dir / f"confirmed_candidates_{setup_type.lower()}.csv",
            [confirmed_to_row(c) for c in items],
        )

    write_csv(output_dir / "watched_candidates.csv", watch_rows)
    write_csv(output_dir / "confirmation_state_transitions.csv", transition_rows)
    write_csv(output_dir / "confirmation_rejects.csv", reject_rows)
    write_csv(output_dir / "generic_context_only.csv", context_only_rows)
    write_csv(output_dir / "confirmed_candidates.csv", trade_rows)
    write_csv(output_dir / "candidate_forward_outcomes.csv", forward_rows)
    write_csv(output_dir / "candidate_score_components.csv", score_rows)

    counts = {s: len(by_setup.get(s, [])) for s in sorted(CONFIRMED_SETUP_TYPES)}
    session_outcomes: dict[str, int] = {}
    for row in forward_rows:
        if row["horizon_seconds"] != "session_end":
            continue
        session_outcomes[row["outcome"]] = session_outcomes.get(row["outcome"], 0) + 1

    summary: dict[str, Any] = {
        "decision": (
            "CONFIRMED_ENTRY_AUDIT_PROMISING"
            if confirmed
            else "CONFIRMED_ENTRY_AUDIT_INCONCLUSIVE"
        ),
        "confirmed_count": len(confirmed),
        "confirmed_by_setup": counts,
        "watch_open_count": sum(1 for r in watch_rows if r.get("event") == WATCH_OPENED),
        "context_only_count": len(context_only_rows),
        "reject_count": len(reject_rows),
        "reject_reasons": _count_by(reject_rows, "reason"),
        "outcome_counts_session_end": session_outcomes,
        "params": {
            "confirmation_snapshots": params.confirmation_snapshots,
            "confirmation_max_seconds": params.confirmation_max_seconds,
            "confirmation_min_feature_count": params.confirmation_min_feature_count,
            "confirmation_require_directional_delta": (
                params.confirmation_require_directional_delta
            ),
            "confirmation_require_supportive_oi": (
                params.confirmation_require_supportive_oi
            ),
            "base": asdict(params.base)
            if hasattr(params.base, "__dataclass_fields__")
            else str(params.base),
        },
        "limitations": [
            "Research-only; no live orders.",
            "Generic LONG/SHORT structure signals are context-only and never confirmed unchanged.",
            "SL/TP use confirm-time wall context; entry is next snapshot mid after confirm.",
            "Forward outcomes evaluated strictly after entry_time.",
        ],
    }
    # Avoid dumping nested MovementParams issues
    try:
        base_dict = {
            k: getattr(params.base, k)
            for k in (
                "sample_seconds",
                "minimum_entry_score",
                "sl_buffer_bps",
                "min_crv_tp1",
                "min_crv_tp2",
                "structure_state_max_age_seconds",
                "contradiction_state_max_age_seconds",
                "reclaim_confirm_snapshots",
            )
        }
        summary["params"]["base"] = base_dict
    except Exception:
        summary["params"]["base"] = {}

    (output_dir / "strategy_summary.json").write_bytes(
        orjson.dumps(summary, option=orjson.OPT_INDENT_2)
    )
    (output_dir / "REPORT.md").write_text(
        render_confirmed_report(summary), encoding="utf-8"
    )
    return summary


def _count_by(rows: Sequence[dict[str, Any]], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        k = str(r.get(key) or "UNKNOWN")
        out[k] = out.get(k, 0) + 1
    return out


def render_confirmed_report(summary: dict[str, Any]) -> str:
    lines = [
        "# Confirmed Orderbook Trade Candidate Audit",
        "",
        f"Decision: **{summary.get('decision')}**",
        f"Confirmed entries: {summary.get('confirmed_count')}",
        "",
        "## By setup_type",
        "",
    ]
    for setup, n in (summary.get("confirmed_by_setup") or {}).items():
        lines.append(f"- {setup}: {n}")
    lines.extend(
        [
            "",
            "## Rejects",
            "",
            f"Total rejects: {summary.get('reject_count')}",
        ]
    )
    for reason, n in (summary.get("reject_reasons") or {}).items():
        lines.append(f"- {reason}: {n}")
    lines.extend(
        [
            "",
            "## Outcomes (session end)",
            "",
        ]
    )
    for outcome, n in (summary.get("outcome_counts_session_end") or {}).items():
        lines.append(f"- {outcome}: {n}")
    lines.extend(
        [
            "",
            "## Limitations",
            "",
        ]
    )
    for lim in summary.get("limitations") or []:
        lines.append(f"- {lim}")
    lines.append("")
    return "\n".join(lines)


def params_from_args(args: argparse.Namespace) -> ConfirmedAuditParams:
    base = AuditParams(
        sample_seconds=int(args.sample_seconds),
        target_bps=float(args.target_bps),
        near_min_distance_pct=float(args.near_min_distance_pct),
        near_max_distance_pct=float(args.near_max_distance_pct),
        near_top_n=int(args.near_top_n),
        minimum_entry_score=int(args.minimum_entry_score),
        entry_mode="next-snapshot-mid",
        sl_buffer_bps=float(args.sl_buffer_bps),
        min_sl_distance_pct=float(args.min_sl_distance_pct),
        max_sl_distance_pct=float(args.max_sl_distance_pct),
        tp_front_run_bps=float(args.tp_front_run_bps),
        min_crv_tp1=float(args.min_crv_tp1),
        min_crv_tp2=float(args.min_crv_tp2),
        cooldown_minutes=int(args.cooldown_minutes),
        structure_state_max_age_seconds=int(args.structure_state_max_age_seconds),
        contradiction_state_max_age_seconds=int(
            args.contradiction_state_max_age_seconds
        ),
        reclaim_confirm_snapshots=int(args.reclaim_confirm_snapshots),
        retest_distance_bps=float(args.retest_distance_bps),
        retest_max_undershoot_bps=float(args.retest_max_undershoot_bps),
        retest_sl_buffer_bps=float(args.retest_sl_buffer_bps),
        retest_max_sl_distance_pct=float(args.retest_max_sl_distance_pct),
    )
    return ConfirmedAuditParams(
        base=base,
        confirmation_snapshots=int(args.confirmation_snapshots),
        confirmation_max_seconds=int(args.confirmation_max_seconds),
        confirmation_min_feature_count=int(args.confirmation_min_feature_count),
        confirmation_require_directional_delta=bool(
            args.confirmation_require_directional_delta
        ),
        confirmation_require_supportive_oi=bool(
            args.confirmation_require_supportive_oi
        ),
        confirmation_price_bps=float(args.confirmation_price_bps),
    )


def run_confirmed_audit(args: argparse.Namespace) -> dict[str, Any]:
    start = parse_utc(args.start)
    end = parse_utc(args.end)
    params = params_from_args(args)
    out_dir = Path(args.output_dir) if args.output_dir else (
        PROJECT_ROOT
        / "results"
        / f"orderbook_confirmed_trade_candidate_{utc_now().strftime('%Y%m%dT%H%M%SZ')}"
    )
    db = connect_readonly()
    try:
        state = prepare_tracker_state(
            db=db, symbol=args.symbol, start=start, end=end, params=params.base
        )
        summary = run_confirmed_audit_from_snapshots(
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
        return summary
    finally:
        db.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Confirmed causal orderbook trade candidate audit"
    )
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--sample-seconds", type=int, default=30)
    p.add_argument("--target-bps", type=float, default=10.0)
    p.add_argument("--near-min-distance-pct", type=float, default=0.10)
    p.add_argument("--near-max-distance-pct", type=float, default=1.50)
    p.add_argument("--near-top-n", type=int, default=3)
    p.add_argument("--minimum-entry-score", type=int, default=5)
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
    p.add_argument("--confirmation-snapshots", type=int, default=2)
    p.add_argument("--confirmation-max-seconds", type=int, default=180)
    p.add_argument("--confirmation-min-feature-count", type=int, default=3)
    p.add_argument(
        "--confirmation-require-directional-delta",
        action="store_true",
        default=False,
    )
    p.add_argument(
        "--confirmation-require-supportive-oi",
        action="store_true",
        default=False,
    )
    p.add_argument("--confirmation-price-bps", type=float, default=3.0)
    p.add_argument("--output-dir", default=None)
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    summary = run_confirmed_audit(args)
    sys.stdout.buffer.write(
        orjson.dumps(
            {
                "decision": summary.get("decision"),
                "confirmed": summary.get("confirmed_count"),
                "by_setup": summary.get("confirmed_by_setup"),
                "output_dir": summary.get("output_dir"),
            },
            option=orjson.OPT_INDENT_2,
        )
    )
    sys.stdout.buffer.write(b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
