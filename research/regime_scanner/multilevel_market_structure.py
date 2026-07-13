"""Multi-level market structure (Phase A) — isolated research module.

Internal and Swing structure run on fully separate state machines.
No policy / state-machine / live-bot coupling.

Pivot detection reuses LuxAlgo-style leg semantics from
``luxalgo_structure_reference`` (CC BY-NC-SA 4.0 attribution for that subset):
``high[size] > ta.highest(size)`` / ``low[size] < ta.lowest(size)``.

Adds:
- three pivot timestamps (extreme / confirmation / available_from)
- active levels with wick vs close crosses
- BOS/CHoCH only on close breaks (one event per level)
- combined internal+swing context labels
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import numpy as np
import pandas as pd

from research.regime_scanner.luxalgo_structure_reference import (
    BEARISH,
    BEARISH_LEG,
    BULLISH,
    BULLISH_LEG,
    new_leg_high,
    new_leg_low,
)

StructureLevelName = Literal["internal", "swing"]
PivotSide = Literal["high", "low"]
PointType = Literal["HH", "HL", "LH", "LL", ""]
EventType = Literal["bos", "choch"]
DirectionName = Literal["bullish", "bearish"]


def _ts(v: object) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _iso(v: object | None) -> str | None:
    if v is None:
        return None
    return _ts(v).isoformat()


def _new_id(prefix: str, counter: int) -> str:
    return f"{prefix}_{counter:06d}"


@dataclass
class ConfirmedPivot:
    pivot_id: str
    structure_level: StructureLevelName
    side: PivotSide
    price: float
    point_type: PointType
    extreme_timestamp_utc: str
    confirmation_timestamp_utc: str
    available_from_timestamp_utc: str
    extreme_bar_index: int
    confirmation_bar_index: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ActiveStructureLevel:
    level_id: str
    structure_level: StructureLevelName
    side: PivotSide
    price: float
    pivot_type: PointType
    pivot_extreme_timestamp: str
    confirmation_timestamp: str
    available_from_timestamp: str
    activated_timestamp: str
    first_wick_cross_timestamp: str | None = None
    first_close_cross_timestamp: str | None = None
    crossed_by_wick: bool = False
    crossed_by_close: bool = False
    invalidated_timestamp: str | None = None
    invalidation_reason: str | None = None
    event_emitted: bool = False
    active: bool = True
    source_pivot_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StructureBreakEvent:
    event_id: str
    timestamp_utc: str
    structure_level: StructureLevelName
    direction: DirectionName
    event_type: EventType
    prior_bias: int
    new_bias: int
    broken_level_id: str
    broken_price: float
    close_price: float
    close_distance_pct: float
    wick_cross_before_close: bool
    source_pivot_extreme_timestamp: str | None
    source_pivot_available_from: str | None
    event_decision_timestamp: str
    failed_break_later: bool | None = None
    retest_held_later: bool | None = None
    reentered_old_structure: bool | None = None
    max_followthrough_4h: float | None = None
    max_adverse_4h: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CombinedContext:
    aligned_bullish: bool = False
    aligned_bearish: bool = False
    bullish_pullback_inside_bullish_swing: bool = False
    bearish_pullback_inside_bearish_swing: bool = False
    bearish_pullback_inside_bullish_swing: bool = False
    bullish_recovery_inside_bearish_swing: bool = False
    bearish_recovery_inside_bullish_swing: bool = False
    possible_bullish_swing_reversal: bool = False
    possible_bearish_swing_reversal: bool = False
    confirmed_bullish_swing_reversal: bool = False
    confirmed_bearish_swing_reversal: bool = False
    mixed_or_range: bool = False
    insufficient_structure: bool = False
    primary_label: str = "insufficient_structure"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify_combined_context(
    *,
    internal_bias: int,
    swing_bias: int,
    swing_close_broken_bull: bool,
    swing_close_broken_bear: bool,
    internal_bull_bos_after_choch: bool,
    internal_bear_bos_after_choch: bool,
    swing_bull_choch_pending: bool,
    swing_bear_choch_pending: bool,
    swing_bull_confirmed: bool,
    swing_bear_confirmed: bool,
) -> CombinedContext:
    ctx = CombinedContext()
    if internal_bias == 0 and swing_bias == 0:
        ctx.insufficient_structure = True
        ctx.primary_label = "insufficient_structure"
        return ctx

    if swing_bull_confirmed:
        ctx.confirmed_bullish_swing_reversal = True
    if swing_bear_confirmed:
        ctx.confirmed_bearish_swing_reversal = True
    if swing_bull_choch_pending and not swing_bull_confirmed:
        ctx.possible_bullish_swing_reversal = True
    if swing_bear_choch_pending and not swing_bear_confirmed:
        ctx.possible_bearish_swing_reversal = True
    if internal_bull_bos_after_choch and swing_bias == BEARISH:
        ctx.possible_bullish_swing_reversal = True
    if internal_bear_bos_after_choch and swing_bias == BULLISH:
        ctx.possible_bearish_swing_reversal = True

    if internal_bias == BULLISH and swing_bias == BULLISH:
        ctx.aligned_bullish = True
    if internal_bias == BEARISH and swing_bias == BEARISH:
        ctx.aligned_bearish = True

    if internal_bias == BULLISH and swing_bias == BEARISH and not swing_close_broken_bull:
        ctx.bullish_recovery_inside_bearish_swing = True
    if internal_bias == BEARISH and swing_bias == BULLISH and not swing_close_broken_bear:
        ctx.bearish_pullback_inside_bullish_swing = True
        ctx.bearish_recovery_inside_bullish_swing = True
    if internal_bias == BEARISH and swing_bias == BEARISH:
        ctx.bearish_pullback_inside_bearish_swing = False
    if internal_bias == BULLISH and swing_bias == BULLISH and not swing_close_broken_bear:
        ctx.bullish_pullback_inside_bullish_swing = False

    if (
        internal_bias != 0
        and swing_bias != 0
        and internal_bias != swing_bias
        and not (
            ctx.bullish_recovery_inside_bearish_swing
            or ctx.bearish_pullback_inside_bullish_swing
            or ctx.possible_bullish_swing_reversal
            or ctx.possible_bearish_swing_reversal
            or ctx.confirmed_bullish_swing_reversal
            or ctx.confirmed_bearish_swing_reversal
        )
    ):
        ctx.mixed_or_range = True

    # Live conflict (recovery/pullback) outranks a still-active confirmed swing episode.
    if ctx.bullish_recovery_inside_bearish_swing:
        ctx.primary_label = "bullish_recovery_inside_bearish_swing"
    elif ctx.bearish_pullback_inside_bullish_swing:
        ctx.primary_label = "bearish_pullback_inside_bullish_swing"
    elif ctx.confirmed_bullish_swing_reversal and swing_bias == BULLISH:
        ctx.primary_label = "confirmed_bullish_swing_reversal"
    elif ctx.confirmed_bearish_swing_reversal and swing_bias == BEARISH:
        ctx.primary_label = "confirmed_bearish_swing_reversal"
    elif ctx.possible_bullish_swing_reversal:
        ctx.primary_label = "possible_bullish_swing_reversal"
    elif ctx.possible_bearish_swing_reversal:
        ctx.primary_label = "possible_bearish_swing_reversal"
    elif ctx.aligned_bullish:
        ctx.primary_label = "aligned_bullish"
    elif ctx.aligned_bearish:
        ctx.primary_label = "aligned_bearish"
    elif ctx.mixed_or_range:
        ctx.primary_label = "mixed_or_range"
    else:
        ctx.insufficient_structure = True
        ctx.primary_label = "insufficient_structure"
    return ctx


@dataclass
class _LayerState:
    name: StructureLevelName
    size: int
    leg: int = BEARISH_LEG
    bias: int = 0
    last_high_price: float | None = None
    last_low_price: float | None = None
    active_high: ActiveStructureLevel | None = None
    active_low: ActiveStructureLevel | None = None
    history_levels: list[ActiveStructureLevel] = field(default_factory=list)
    pivots: list[ConfirmedPivot] = field(default_factory=list)
    events: list[StructureBreakEvent] = field(default_factory=list)
    pending_bull_choch: bool = False
    pending_bear_choch: bool = False
    bull_bos_after_choch: bool = False
    bear_bos_after_choch: bool = False
    _pivot_seq: int = 0
    _level_seq: int = 0
    _event_seq: int = 0


class MultiLevelStructureEngine:
    """Causal dual-layer structure runner on closed OHLCV bars.

    Frame requires: timestamp (open), decision_time (close), open, high, low, close.
    """

    def __init__(self, *, internal_size: int = 5, swing_size: int = 50, timeframe: str = "30m"):
        self.timeframe = timeframe
        self.internal = _LayerState(name="internal", size=int(internal_size))
        self.swing = _LayerState(name="swing", size=int(swing_size))
        self.bar_rows: list[dict[str, Any]] = []
        self.all_pivots: list[ConfirmedPivot] = []
        self.all_levels: list[ActiveStructureLevel] = []
        self.all_events: list[StructureBreakEvent] = []

    def _step_leg(self, layer: _LayerState, highs: np.ndarray, lows: np.ndarray, i: int) -> tuple[bool, bool]:
        if i == 0:
            if new_leg_high(highs, i, layer.size):
                layer.leg = BEARISH_LEG
            elif new_leg_low(lows, i, layer.size):
                layer.leg = BULLISH_LEG
            return False, False
        old = layer.leg
        new = old
        if new_leg_high(highs, i, layer.size):
            new = BEARISH_LEG
        elif new_leg_low(lows, i, layer.size):
            new = BULLISH_LEG
        layer.leg = new
        changed = new - old
        return changed == -1, changed == +1

    def _activate_pivot(
        self,
        layer: _LayerState,
        *,
        side: PivotSide,
        price: float,
        extreme_i: int,
        confirm_i: int,
        times: list[pd.Timestamp],
        decisions: list[pd.Timestamp],
    ) -> ConfirmedPivot:
        point: PointType
        if side == "high":
            if layer.last_high_price is None:
                point = "HH"
            else:
                point = "HH" if price > layer.last_high_price else "LH"
            layer.last_high_price = price
        else:
            if layer.last_low_price is None:
                point = "LL"
            else:
                point = "LL" if price < layer.last_low_price else "HL"
            layer.last_low_price = price

        layer._pivot_seq += 1
        pivot = ConfirmedPivot(
            pivot_id=_new_id(f"{layer.name}_pivot", layer._pivot_seq),
            structure_level=layer.name,
            side=side,
            price=float(price),
            point_type=point,
            extreme_timestamp_utc=_iso(times[extreme_i]) or "",
            confirmation_timestamp_utc=_iso(times[confirm_i]) or "",
            available_from_timestamp_utc=_iso(decisions[confirm_i]) or "",
            extreme_bar_index=extreme_i,
            confirmation_bar_index=confirm_i,
        )
        layer.pivots.append(pivot)
        self.all_pivots.append(pivot)

        layer._level_seq += 1
        level = ActiveStructureLevel(
            level_id=_new_id(f"{layer.name}_lvl", layer._level_seq),
            structure_level=layer.name,
            side=side,
            price=float(price),
            pivot_type=point,
            pivot_extreme_timestamp=pivot.extreme_timestamp_utc,
            confirmation_timestamp=pivot.confirmation_timestamp_utc,
            available_from_timestamp=pivot.available_from_timestamp_utc,
            activated_timestamp=pivot.available_from_timestamp_utc,
            source_pivot_id=pivot.pivot_id,
        )
        if side == "high":
            if layer.active_high is not None and layer.active_high.active:
                layer.active_high.active = False
                if layer.active_high.invalidated_timestamp is None:
                    layer.active_high.invalidated_timestamp = pivot.available_from_timestamp_utc
                    layer.active_high.invalidation_reason = "replaced_by_new_confirmed_pivot"
            layer.active_high = level
        else:
            if layer.active_low is not None and layer.active_low.active:
                layer.active_low.active = False
                if layer.active_low.invalidated_timestamp is None:
                    layer.active_low.invalidated_timestamp = pivot.available_from_timestamp_utc
                    layer.active_low.invalidation_reason = "replaced_by_new_confirmed_pivot"
            layer.active_low = level
        layer.history_levels.append(level)
        self.all_levels.append(level)
        return pivot

    def _emit_break(
        self,
        layer: _LayerState,
        *,
        level: ActiveStructureLevel,
        direction: DirectionName,
        close: float,
        decision_ts: pd.Timestamp,
        prior_bias: int,
    ) -> tuple[EventType, StructureBreakEvent]:
        is_choch = (direction == "bullish" and prior_bias == BEARISH) or (
            direction == "bearish" and prior_bias == BULLISH
        )
        event_type: EventType = "choch" if is_choch else "bos"
        new_bias = BULLISH if direction == "bullish" else BEARISH
        layer.bias = new_bias
        if direction == "bullish":
            # clear opposite episode
            layer.pending_bear_choch = False
            layer.bear_bos_after_choch = False
            if is_choch:
                layer.pending_bull_choch = True
                layer.bull_bos_after_choch = False
            elif layer.pending_bull_choch:
                layer.bull_bos_after_choch = True
        else:
            layer.pending_bull_choch = False
            layer.bull_bos_after_choch = False
            if is_choch:
                layer.pending_bear_choch = True
                layer.bear_bos_after_choch = False
            elif layer.pending_bear_choch:
                layer.bear_bos_after_choch = True

        dist = (
            (close - level.price) / level.price * 100.0
            if direction == "bullish"
            else (level.price - close) / level.price * 100.0
        )
        layer._event_seq += 1
        ev = StructureBreakEvent(
            event_id=_new_id(f"{layer.name}_evt", layer._event_seq),
            timestamp_utc=_iso(decision_ts) or "",
            structure_level=layer.name,
            direction=direction,
            event_type=event_type,
            prior_bias=prior_bias,
            new_bias=new_bias,
            broken_level_id=level.level_id,
            broken_price=float(level.price),
            close_price=float(close),
            close_distance_pct=float(dist),
            wick_cross_before_close=bool(level.crossed_by_wick),
            source_pivot_extreme_timestamp=level.pivot_extreme_timestamp,
            source_pivot_available_from=level.available_from_timestamp,
            event_decision_timestamp=_iso(decision_ts) or "",
        )
        level.event_emitted = True
        level.crossed_by_close = True
        level.first_close_cross_timestamp = _iso(decision_ts)
        level.active = False
        level.invalidated_timestamp = _iso(decision_ts)
        level.invalidation_reason = f"close_{event_type}"
        layer.events.append(ev)
        self.all_events.append(ev)
        return event_type, ev

    def _process_crosses(
        self,
        layer: _LayerState,
        *,
        high: float,
        low: float,
        close: float,
        prior_close: float | None,
        decision_ts: pd.Timestamp,
    ) -> dict[str, bool]:
        flags = {
            "wick_cross_high": False,
            "wick_cross_low": False,
            "close_cross_high": False,
            "close_cross_low": False,
            "bullish_bos": False,
            "bearish_bos": False,
            "bullish_choch": False,
            "bearish_choch": False,
        }
        ah = layer.active_high
        al = layer.active_low
        if ah is not None and ah.active and _ts(ah.available_from_timestamp) <= decision_ts:
            if high > ah.price:
                flags["wick_cross_high"] = True
                if not ah.crossed_by_wick:
                    ah.crossed_by_wick = True
                    ah.first_wick_cross_timestamp = _iso(decision_ts)
            if (
                prior_close is not None
                and prior_close <= ah.price
                and close > ah.price
                and not ah.event_emitted
            ):
                flags["close_cross_high"] = True
                etype, _ = self._emit_break(
                    layer,
                    level=ah,
                    direction="bullish",
                    close=close,
                    decision_ts=decision_ts,
                    prior_bias=layer.bias,
                )
                if etype == "choch":
                    flags["bullish_choch"] = True
                else:
                    flags["bullish_bos"] = True

        if al is not None and al.active and _ts(al.available_from_timestamp) <= decision_ts:
            if low < al.price:
                flags["wick_cross_low"] = True
                if not al.crossed_by_wick:
                    al.crossed_by_wick = True
                    al.first_wick_cross_timestamp = _iso(decision_ts)
            if (
                prior_close is not None
                and prior_close >= al.price
                and close < al.price
                and not al.event_emitted
            ):
                flags["close_cross_low"] = True
                etype, _ = self._emit_break(
                    layer,
                    level=al,
                    direction="bearish",
                    close=close,
                    decision_ts=decision_ts,
                    prior_bias=layer.bias,
                )
                if etype == "choch":
                    flags["bearish_choch"] = True
                else:
                    flags["bearish_bos"] = True
        return flags

    def run(self, frame: pd.DataFrame) -> list[dict[str, Any]]:
        df = frame.copy()
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        if "decision_time" not in df.columns:
            raise ValueError("frame requires decision_time")
        df["decision_time"] = pd.to_datetime(df["decision_time"], utc=True)
        highs = df["high"].astype(float).to_numpy()
        lows = df["low"].astype(float).to_numpy()
        closes = df["close"].astype(float).to_numpy()
        opens = df["open"].astype(float).to_numpy()
        times = [_ts(t) for t in df["timestamp"]]
        decisions = [_ts(t) for t in df["decision_time"]]
        out: list[dict[str, Any]] = []

        for i in range(len(df)):
            last_int = None
            last_sw = None
            inh, inl = self._step_leg(self.internal, highs, lows, i)
            snh, snl = self._step_leg(self.swing, highs, lows, i)

            if inl:
                last_int = self._activate_pivot(
                    self.internal,
                    side="low",
                    price=float(lows[i - self.internal.size]),
                    extreme_i=i - self.internal.size,
                    confirm_i=i,
                    times=times,
                    decisions=decisions,
                )
            elif inh:
                last_int = self._activate_pivot(
                    self.internal,
                    side="high",
                    price=float(highs[i - self.internal.size]),
                    extreme_i=i - self.internal.size,
                    confirm_i=i,
                    times=times,
                    decisions=decisions,
                )
            if snl:
                last_sw = self._activate_pivot(
                    self.swing,
                    side="low",
                    price=float(lows[i - self.swing.size]),
                    extreme_i=i - self.swing.size,
                    confirm_i=i,
                    times=times,
                    decisions=decisions,
                )
            elif snh:
                last_sw = self._activate_pivot(
                    self.swing,
                    side="high",
                    price=float(highs[i - self.swing.size]),
                    extreme_i=i - self.swing.size,
                    confirm_i=i,
                    times=times,
                    decisions=decisions,
                )

            prior_close = float(closes[i - 1]) if i > 0 else None
            decision_ts = decisions[i]
            iflags = self._process_crosses(
                self.internal,
                high=float(highs[i]),
                low=float(lows[i]),
                close=float(closes[i]),
                prior_close=prior_close,
                decision_ts=decision_ts,
            )
            sflags = self._process_crosses(
                self.swing,
                high=float(highs[i]),
                low=float(lows[i]),
                close=float(closes[i]),
                prior_close=prior_close,
                decision_ts=decision_ts,
            )

            ctx = classify_combined_context(
                internal_bias=self.internal.bias,
                swing_bias=self.swing.bias,
                swing_close_broken_bull=sflags["close_cross_high"],
                swing_close_broken_bear=sflags["close_cross_low"],
                internal_bull_bos_after_choch=self.internal.bull_bos_after_choch
                and self.internal.bias == BULLISH,
                internal_bear_bos_after_choch=self.internal.bear_bos_after_choch
                and self.internal.bias == BEARISH,
                swing_bull_choch_pending=self.swing.pending_bull_choch
                and not self.swing.bull_bos_after_choch
                and self.swing.bias == BULLISH,
                swing_bear_choch_pending=self.swing.pending_bear_choch
                and not self.swing.bear_bos_after_choch
                and self.swing.bias == BEARISH,
                swing_bull_confirmed=self.swing.bull_bos_after_choch and self.swing.bias == BULLISH,
                swing_bear_confirmed=self.swing.bear_bos_after_choch and self.swing.bias == BEARISH,
            )

            def _active(level: ActiveStructureLevel | None) -> ActiveStructureLevel | None:
                return level if level is not None and level.active else None

            iah = _active(self.internal.active_high)
            ial = _active(self.internal.active_low)
            sah = _active(self.swing.active_high)
            sal = _active(self.swing.active_low)

            row = {
                "timestamp_utc": _iso(times[i]),
                "decision_timestamp_utc": _iso(decision_ts),
                "timeframe": self.timeframe,
                "open": float(opens[i]),
                "high": float(highs[i]),
                "low": float(lows[i]),
                "close": float(closes[i]),
                "internal_bias": self.internal.bias,
                "swing_bias": self.swing.bias,
                "internal_leg": self.internal.leg,
                "swing_leg": self.swing.leg,
                "internal_point_type": "" if last_int is None else last_int.point_type,
                "swing_point_type": "" if last_sw is None else last_sw.point_type,
                "internal_pivot_high": inh,
                "internal_pivot_low": inl,
                "swing_pivot_high": snh,
                "swing_pivot_low": snl,
                "internal_pivot_extreme_timestamp": None if last_int is None else last_int.extreme_timestamp_utc,
                "internal_pivot_confirmation_timestamp": (
                    None if last_int is None else last_int.confirmation_timestamp_utc
                ),
                "internal_pivot_available_from": (
                    None if last_int is None else last_int.available_from_timestamp_utc
                ),
                "swing_pivot_extreme_timestamp": None if last_sw is None else last_sw.extreme_timestamp_utc,
                "swing_pivot_confirmation_timestamp": (
                    None if last_sw is None else last_sw.confirmation_timestamp_utc
                ),
                "swing_pivot_available_from": None if last_sw is None else last_sw.available_from_timestamp_utc,
                "internal_active_high": None if iah is None else iah.price,
                "internal_active_low": None if ial is None else ial.price,
                "swing_active_high": None if sah is None else sah.price,
                "swing_active_low": None if sal is None else sal.price,
                "internal_active_high_id": None if iah is None else iah.level_id,
                "internal_active_low_id": None if ial is None else ial.level_id,
                "swing_active_high_id": None if sah is None else sah.level_id,
                "swing_active_low_id": None if sal is None else sal.level_id,
                "internal_bullish_bos": iflags["bullish_bos"],
                "internal_bearish_bos": iflags["bearish_bos"],
                "internal_bullish_choch": iflags["bullish_choch"],
                "internal_bearish_choch": iflags["bearish_choch"],
                "swing_bullish_bos": sflags["bullish_bos"],
                "swing_bearish_bos": sflags["bearish_bos"],
                "swing_bullish_choch": sflags["bullish_choch"],
                "swing_bearish_choch": sflags["bearish_choch"],
                "internal_wick_cross_high": iflags["wick_cross_high"],
                "internal_wick_cross_low": iflags["wick_cross_low"],
                "swing_wick_cross_high": sflags["wick_cross_high"],
                "swing_wick_cross_low": sflags["wick_cross_low"],
                "internal_close_cross_high": iflags["close_cross_high"],
                "internal_close_cross_low": iflags["close_cross_low"],
                "swing_close_cross_high": sflags["close_cross_high"],
                "swing_close_cross_low": sflags["close_cross_low"],
                **{f"ctx_{k}": v for k, v in ctx.to_dict().items()},
                "combined_primary_label": ctx.primary_label,
            }
            out.append(row)
            self.bar_rows.append(row)
        return out


def run_multilevel_structure(
    frame: pd.DataFrame,
    *,
    internal_size: int = 5,
    swing_size: int = 50,
    timeframe: str = "30m",
) -> tuple[list[dict[str, Any]], MultiLevelStructureEngine]:
    eng = MultiLevelStructureEngine(
        internal_size=internal_size,
        swing_size=swing_size,
        timeframe=timeframe,
    )
    return eng.run(frame), eng


def annotate_event_outcomes(
    events: list[StructureBreakEvent],
    bars: pd.DataFrame,
    *,
    horizon_hours: float = 4.0,
) -> None:
    """Post-hoc only: fill followthrough / failed-break fields without mutating decisions."""
    if not events or bars.empty:
        return
    df = bars.copy()
    df["decision_timestamp_utc"] = pd.to_datetime(df["decision_timestamp_utc"], utc=True)
    for ev in events:
        t0 = _ts(ev.event_decision_timestamp)
        t1 = t0 + pd.Timedelta(hours=horizon_hours)
        fut = df[(df["decision_timestamp_utc"] > t0) & (df["decision_timestamp_utc"] <= t1)]
        if fut.empty:
            continue
        px = float(ev.close_price)
        broken = float(ev.broken_price)
        if ev.direction == "bullish":
            mfe = float((fut["high"].max() - px) / px * 100.0)
            mae = float((px - fut["low"].min()) / px * 100.0)
            reentered = bool((fut["close"] < broken).any())
            failed = bool((fut["close"] < broken).any()) and mfe < 0.25
            retest_held = bool((fut["low"] <= broken * 1.002).any()) and not failed
        else:
            mfe = float((px - fut["low"].min()) / px * 100.0)
            mae = float((fut["high"].max() - px) / px * 100.0)
            reentered = bool((fut["close"] > broken).any())
            failed = bool((fut["close"] > broken).any()) and mfe < 0.25
            retest_held = bool((fut["high"] >= broken * 0.998).any()) and not failed
        ev.max_followthrough_4h = mfe
        ev.max_adverse_4h = mae
        ev.reentered_old_structure = reentered
        ev.failed_break_later = failed
        ev.retest_held_later = retest_held
