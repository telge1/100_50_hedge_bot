from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .frozen_gate import FrozenGateLabel, classify_long_frozen, classify_short_frozen
from .persistence import ImpulseMetrics
from .states import ImpulseState, Side
from .thresholds import FrozenGateThresholds, ResearchExploreParams, FROZEN_DEFAULT, DEFAULT_RESEARCH
from .whipsaw import WhipsawDecision, evaluate_whipsaw


@dataclass
class DataAvailability:
    trades: str = "MISSING"  # VALID / PARTIAL / MISSING
    orderbook: str = "MISSING"
    oi: str = "MISSING"
    candles: str = "MISSING"
    liquidations: str = "MISSING"
    stale_flags: dict[str, bool] = field(default_factory=dict)

    @property
    def blocks_confirmed(self) -> bool:
        return self.trades != "VALID" or self.orderbook != "VALID" or self.candles != "VALID"


@dataclass
class DecisionSnapshot:
    side: Side
    frozen_label: FrozenGateLabel
    state: ImpulseState
    reason: str
    whipsaw: WhipsawDecision | None = None
    metrics: ImpulseMetrics | None = None
    data: DataAvailability | None = None
    research_only: bool = True
    live_entry_allowed: bool = False


def _map_frozen_to_early_confirming(label: FrozenGateLabel, side: Side) -> ImpulseState | None:
    if side == Side.LONG:
        if label == FrozenGateLabel.EARLY_PRESSURE:
            return ImpulseState.EARLY_PRESSURE
        if label == FrozenGateLabel.PUMP_CONFIRMING:
            return ImpulseState.CONFIRMING
        if label == FrozenGateLabel.PUMP_CONFIRMED:
            return ImpulseState.CONFIRMING  # never promote frozen CONFIRMED alone
        if label == FrozenGateLabel.MIXED:
            return ImpulseState.MIXED
        if label == FrozenGateLabel.NO_EVIDENCE:
            return ImpulseState.NO_EVIDENCE
    else:
        if label == FrozenGateLabel.EARLY_SELL_PRESSURE:
            return ImpulseState.EARLY_PRESSURE
        if label == FrozenGateLabel.DUMP_CONFIRMING:
            return ImpulseState.CONFIRMING
        if label == FrozenGateLabel.DUMP_CONFIRMED:
            return ImpulseState.CONFIRMING
        if label == FrozenGateLabel.MIXED:
            return ImpulseState.MIXED
        if label == FrozenGateLabel.NO_EVIDENCE:
            return ImpulseState.NO_EVIDENCE
    return None


def decide_state(
    side: Side,
    features: Mapping[str, Any],
    metrics: ImpulseMetrics | None,
    data: DataAvailability,
    last_opposite_impulse_age_s: float | None = None,
    opposite_was_active: bool = False,
    thr: FrozenGateThresholds = FROZEN_DEFAULT,
    params: ResearchExploreParams = DEFAULT_RESEARCH,
) -> DecisionSnapshot:
    """Causal research decision. Single spike cannot yield CONFIRMED."""
    frozen = (
        classify_long_frozen(features, thr) if side == Side.LONG else classify_short_frozen(features, thr)
    )

    # Whipsaw first if proposing a directional entry state
    base = _map_frozen_to_early_confirming(frozen, side)
    independent_confirm = False
    if base == ImpulseState.CONFIRMING and metrics is not None:
        gb_i = metrics.giveback_ratio.get(params.confirm_persist_s)
        independent_confirm = (
            metrics.persistence_ok.get(params.confirm_persist_s, False)
            and gb_i is not None
            and gb_i < params.giveback_mark_low
        )
    whip = evaluate_whipsaw(
        side,
        last_opposite_impulse_age_s,
        opposite_was_active,
        new_direction_independently_confirmed=independent_confirm,
        params=params,
    )
    if whip.blocked and base in (ImpulseState.EARLY_PRESSURE, ImpulseState.CONFIRMING):
        return DecisionSnapshot(
            side=side,
            frozen_label=frozen,
            state=ImpulseState.WHIPSAW_BLOCKED,
            reason=whip.reason,
            whipsaw=whip,
            metrics=metrics,
            data=data,
            live_entry_allowed=False,
        )

    if data.blocks_confirmed and base == ImpulseState.CONFIRMING:
        return DecisionSnapshot(
            side=side,
            frozen_label=frozen,
            state=ImpulseState.INCONCLUSIVE_DATA,
            reason="confirming_legs_present_but_required_sources_invalid",
            whipsaw=whip,
            metrics=metrics,
            data=data,
            live_entry_allowed=False,
        )

    if base in (ImpulseState.NO_EVIDENCE, ImpulseState.MIXED, None):
        st = base or ImpulseState.NO_EVIDENCE
        return DecisionSnapshot(
            side=side,
            frozen_label=frozen,
            state=st,
            reason=f"frozen={frozen.value}",
            whipsaw=whip,
            metrics=metrics,
            data=data,
            live_entry_allowed=False,
        )

    # FAILED_IMPULSE: early/confirming but fast giveback or no persistence
    if metrics is not None:
        gb60 = metrics.giveback_ratio.get(60)
        persist60 = metrics.persistence_ok.get(params.confirm_persist_s, False)
        if gb60 is not None and gb60 >= params.giveback_mark_low:
            return DecisionSnapshot(
                side=side,
                frozen_label=frozen,
                state=ImpulseState.FAILED_IMPULSE,
                reason=f"giveback_60s={gb60:.3f}>={params.giveback_mark_low}",
                whipsaw=whip,
                metrics=metrics,
                data=data,
                live_entry_allowed=False,
            )
        if base == ImpulseState.EARLY_PRESSURE and not persist60:
            # still early; not failed yet unless giveback high already handled
            return DecisionSnapshot(
                side=side,
                frozen_label=frozen,
                state=ImpulseState.EARLY_PRESSURE,
                reason="early_without_persistence",
                whipsaw=whip,
                metrics=metrics,
                data=data,
                live_entry_allowed=False,
            )

    gb_confirm = metrics.giveback_ratio.get(params.confirm_persist_s) if metrics else None
    gb_ok = gb_confirm is not None and gb_confirm < params.giveback_mark_low
    # CONFIRMED research: need confirming + persistence + low giveback + data valid
    if (
        base == ImpulseState.CONFIRMING
        and metrics is not None
        and metrics.persistence_ok.get(params.confirm_persist_s, False)
        and gb_ok
        and not data.blocks_confirmed
        and frozen
        in (
            FrozenGateLabel.PUMP_CONFIRMING,
            FrozenGateLabel.PUMP_CONFIRMED,
            FrozenGateLabel.DUMP_CONFIRMING,
            FrozenGateLabel.DUMP_CONFIRMED,
        )
    ):
        return DecisionSnapshot(
            side=side,
            frozen_label=frozen,
            state=ImpulseState.CONFIRMED,
            reason="frozen_confirming_plus_persistence_and_low_giveback",
            whipsaw=whip,
            metrics=metrics,
            data=data,
            live_entry_allowed=False,  # research never arms live
        )

    if base == ImpulseState.CONFIRMING:
        return DecisionSnapshot(
            side=side,
            frozen_label=frozen,
            state=ImpulseState.CONFIRMING,
            reason="frozen_confirming_awaiting_persistence",
            whipsaw=whip,
            metrics=metrics,
            data=data,
            live_entry_allowed=False,
        )

    return DecisionSnapshot(
        side=side,
        frozen_label=frozen,
        state=ImpulseState.EARLY_PRESSURE,
        reason="early_pressure_only",
        whipsaw=whip,
        metrics=metrics,
        data=data,
        live_entry_allowed=False,
    )
