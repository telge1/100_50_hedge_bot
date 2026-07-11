"""Phase-2 causal Price Action confirmation (no momentum / entry / TP).

Flow
----
SetupActivation → pullback / structure → Close break → PriceActionConfirmation

Reuses ``ConfirmedPivot`` / ``find_confirmed_pivots`` / ``filter_pivots_as_of``.
Does **not** import or call ``signal_tp_audit`` entry helpers.
Does **not** reuse exhaustion retest distance caps for PA lower highs.

Timestamp semantics (pipeline / audits)
--------------------------------------
* ``decision_time``: moment *after* the closed candle under review (signal time).
* ``candle_timestamp`` / PA ``closed_candle["timestamp"]``: typically the candle
  *open* time of that closed bar. Event comparisons must use one consistent
  clock — PA events and ``structure_armed_timestamp`` use the closed-candle
  timestamp, not ``decision_time``.

Age policy
----------
``max_setup_age_candles = 96`` means ages ``0..96`` are allowed. Each
``update_price_action_state`` increments ``age_candles`` then expires when
``age_candles > max_setup_age_candles`` (first fail at age **97**).
"""

from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import pandas as pd

from .config import DEFAULT_PIVOT_LEFT_BY_TIMEFRAME, DEFAULT_PIVOT_RIGHT_BY_TIMEFRAME
from .structure import classify_swing_structure
from .swings import ConfirmedPivot

SetupSide = Literal["long", "short"]
PatternType = Literal["lower_high", "higher_low", "failed_breakout", "failed_breakdown"]
PAStateName = Literal[
    "waiting_for_pullback",
    "tracking_structure_candidate",
    "waiting_for_confirmation_level",
    "structure_armed",
    "waiting_for_structure_break",
    "price_action_confirmed",
    "invalidated",
    "expired",
]

_FORBIDDEN_KEYS = frozenset(
    {
        "entry_price",
        "tp_price",
        "tp_pct",
        "stop_loss",
        "mae_pct",
        "mfe_pct",
        "max_adverse_excursion_pct",
        "max_favorable_excursion_pct",
        "momentum",
        "momentum_result",
    }
)

_TERMINAL = frozenset({"price_action_confirmed", "invalidated", "expired"})


@dataclass(frozen=True)
class PriceActionConfig:
    pivot_left: int = field(
        default_factory=lambda: int(DEFAULT_PIVOT_LEFT_BY_TIMEFRAME.get("5m", 3))
    )
    pivot_right: int = field(
        default_factory=lambda: int(DEFAULT_PIVOT_RIGHT_BY_TIMEFRAME.get("5m", 3))
    )
    minimum_swing_separation_candles: int = 5
    # Ages 0..max inclusive allowed; expire on first update with age > max (97 when max=96).
    max_setup_age_candles: int = 96
    price_epsilon_pct: float = 0.01
    breakout_tolerance_pct: float = 0.0
    source_timeframe: str = "5m"
    # Failed BO/BD invalidation cushion in ATR units. Starter default only — not optimised.
    # Long FBD: inv = extreme - atr * buffer; short FBO: inv = extreme + atr * buffer.
    # If ATR missing/non-finite: distance = |extreme| * (breakout_tolerance_pct / 100)
    # (explicit 0 when tolerance is 0 — never silent NaN).
    failed_break_invalidation_atr_buffer: float = 0.1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def default_price_action_config() -> PriceActionConfig:
    return PriceActionConfig()


def confirmed_pivot_to_swing(
    pivot: ConfirmedPivot | dict[str, Any],
    *,
    source_timeframe: str = "5m",
    reason_codes: list[Any] | None = None,
) -> dict[str, Any]:
    """Thin ConfirmedSwing adapter over ConfirmedPivot (serialisable dict)."""
    if isinstance(pivot, ConfirmedPivot):
        payload = pivot.to_dict()
    else:
        payload = dict(pivot)
    return {
        "side": str(payload.get("pivot_type") or payload.get("side")),
        "price": float(payload["price"]),
        "pivot_index": int(payload["pivot_index"]),
        "pivot_timestamp": str(payload["pivot_timestamp"]),
        "confirmation_index": int(payload["confirmation_index"]),
        "confirmation_timestamp": str(payload["confirmation_timestamp"]),
        "source_timeframe": str(
            payload.get("source_timeframe") or source_timeframe
        ),
        "reason_codes": list(reason_codes or payload.get("reason_codes") or []),
    }


def swing_key(swing: dict[str, Any]) -> tuple[int, int, str]:
    return (
        int(swing["confirmation_index"]),
        int(swing["pivot_index"]),
        str(swing["side"]),
    )


def _ts(value: object) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def swing_usable_as_of(swing: dict[str, Any], as_of: object) -> bool:
    """True when confirmation_timestamp <= as_of (closed-candle semantics)."""
    return _ts(swing["confirmation_timestamp"]) <= _ts(as_of)


def filter_swings_as_of(
    swings: list[dict[str, Any]],
    as_of: object,
) -> list[dict[str, Any]]:
    return [s for s in swings if swing_usable_as_of(s, as_of)]


def sort_swings(swings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(swings, key=swing_key)


def _assert_no_forbidden(payload: dict[str, Any]) -> None:
    stack: list[Any] = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            bad = _FORBIDDEN_KEYS.intersection(item)
            if bad:
                raise ValueError(f"forbidden Phase-2 fields present: {sorted(bad)}")
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)


def _tol(level: float, tolerance_pct: float) -> float:
    return abs(float(level)) * float(tolerance_pct) / 100.0


def _candle_close(candle: dict[str, Any]) -> float:
    return float(candle["close"])


def _candle_high(candle: dict[str, Any]) -> float:
    return float(candle["high"])


def _candle_low(candle: dict[str, Any]) -> float:
    return float(candle["low"])


def _candle_ts(candle: dict[str, Any]) -> str:
    return str(_ts(candle["timestamp"]).isoformat())


def _candle_atr(candle: dict[str, Any]) -> float | None:
    raw = candle.get("atr")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0.0:
        return None
    return value


def _candle_index(candle: dict[str, Any]) -> int | None:
    raw = candle.get("candle_index")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _is_finite_positive(value: object) -> bool:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0.0


def validate_structure_geometry(
    *,
    side: str,
    confirmation_level: object,
    invalidation_level: object,
) -> bool:
    """SHORT: inv > conf; LONG: inv < conf; levels finite, positive, unequal."""
    if not _is_finite_positive(confirmation_level):
        return False
    if not _is_finite_positive(invalidation_level):
        return False
    conf = float(confirmation_level)  # type: ignore[arg-type]
    inv = float(invalidation_level)  # type: ignore[arg-type]
    if conf == inv:
        return False
    if side == "short":
        return inv > conf
    if side == "long":
        return inv < conf
    return False


def _failed_break_buffer_distance(
    *,
    extreme: float,
    atr: float | None,
    cfg: PriceActionConfig,
) -> tuple[float, str]:
    """Return (distance, mode). Never NaN."""
    buf = float(cfg.failed_break_invalidation_atr_buffer)
    if atr is not None:
        return float(atr) * buf, "atr"
    # Explicit fallback via breakout_tolerance_pct (percent of price).
    dist = abs(float(extreme)) * (float(cfg.breakout_tolerance_pct) / 100.0)
    return dist, "breakout_tolerance_pct_fallback"


def compute_failed_break_invalidation_level(
    *,
    side: str,
    extreme: float,
    atr: float | None,
    cfg: PriceActionConfig,
) -> dict[str, Any]:
    """Buffered invalidation for failed breakout/breakdown."""
    unbuffered = float(extreme)
    distance, mode = _failed_break_buffer_distance(extreme=unbuffered, atr=atr, cfg=cfg)
    if side == "long":
        level = unbuffered - distance
    else:
        level = unbuffered + distance
    return {
        "unbuffered_failed_break_extreme": unbuffered,
        "failed_break_invalidation_buffer": float(distance),
        "failed_break_invalidation_buffer_mode": mode,
        "final_invalidation_level": float(level),
    }


def _select_failed_breakdown_confirmation_high(
    swings: list[dict[str, Any]],
    *,
    reference_low: dict[str, Any],
    failed_break_ts: object,
    as_of: object,
    setup_activation_ts: object | None,
) -> dict[str, Any] | None:
    """Local swing high belonging to the FBD attempt (not an ancient distant high)."""
    ref_i = int(reference_low["pivot_index"])
    fb_ts = _ts(failed_break_ts)
    act_ts = _ts(setup_activation_ts) if setup_activation_ts is not None else None
    pool = [
        s
        for s in swings
        if s.get("side") == "high"
        and int(s["pivot_index"]) > ref_i
        and swing_usable_as_of(s, as_of)
    ]
    if not pool:
        return None
    after_attempt = [
        s
        for s in pool
        if _ts(s["pivot_timestamp"]) >= fb_ts or _ts(s["confirmation_timestamp"]) >= fb_ts
    ]
    if after_attempt:
        return sort_swings(after_attempt)[0]
    # Most recent high after reference, confirmed in the setup→attempt window.
    before = [
        s
        for s in pool
        if _ts(s["confirmation_timestamp"]) <= fb_ts
        and (
            act_ts is None
            or _ts(s["confirmation_timestamp"]) > act_ts
            or _ts(s["pivot_timestamp"]) >= act_ts
        )
    ]
    if not before:
        return None
    return sort_swings(before)[-1]


def _select_failed_breakout_confirmation_low(
    swings: list[dict[str, Any]],
    *,
    reference_high: dict[str, Any],
    failed_break_ts: object,
    as_of: object,
    setup_activation_ts: object | None,
) -> dict[str, Any] | None:
    """Mirror of FBD confirmation selection for short failed breakout."""
    ref_i = int(reference_high["pivot_index"])
    fb_ts = _ts(failed_break_ts)
    act_ts = _ts(setup_activation_ts) if setup_activation_ts is not None else None
    pool = [
        s
        for s in swings
        if s.get("side") == "low"
        and int(s["pivot_index"]) > ref_i
        and swing_usable_as_of(s, as_of)
    ]
    if not pool:
        return None
    after_attempt = [
        s
        for s in pool
        if _ts(s["pivot_timestamp"]) >= fb_ts or _ts(s["confirmation_timestamp"]) >= fb_ts
    ]
    if after_attempt:
        return sort_swings(after_attempt)[0]
    before = [
        s
        for s in pool
        if _ts(s["confirmation_timestamp"]) <= fb_ts
        and (
            act_ts is None
            or _ts(s["confirmation_timestamp"]) > act_ts
            or _ts(s["pivot_timestamp"]) >= act_ts
        )
    ]
    if not before:
        return None
    return sort_swings(before)[-1]


def _last_swing(
    swings: list[dict[str, Any]],
    *,
    side: str,
    as_of: object | None = None,
) -> dict[str, Any] | None:
    filtered = [s for s in swings if s.get("side") == side]
    if as_of is not None:
        filtered = filter_swings_as_of(filtered, as_of)
    if not filtered:
        return None
    return sort_swings(filtered)[-1]


def _select_intermediate_low(
    swings: list[dict[str, Any]],
    *,
    reference_high: dict[str, Any],
    candidate_high: dict[str, Any],
) -> dict[str, Any] | None:
    """confirmation_level swing for SHORT lower_high (v1).

    Prefer last confirmed low with:
      H1.pivot_index < L.pivot_index < H2.pivot_index
      and L.confirmation_timestamp <= H2.confirmation_timestamp
    Fallback: last confirmed low before H2 (pivot_index < H2.pivot_index,
    confirmation <= H2.confirmation).
    """
    h1_i = int(reference_high["pivot_index"])
    h2_i = int(candidate_high["pivot_index"])
    h2_conf = candidate_high["confirmation_timestamp"]
    lows = [
        s
        for s in swings
        if s.get("side") == "low"
        and int(s["pivot_index"]) < h2_i
        and swing_usable_as_of(s, h2_conf)
    ]
    between = [s for s in lows if h1_i < int(s["pivot_index"]) < h2_i]
    pool = between if between else lows
    if not pool:
        return None
    return sort_swings(pool)[-1]


def _select_intermediate_high(
    swings: list[dict[str, Any]],
    *,
    reference_low: dict[str, Any],
    candidate_low: dict[str, Any],
) -> dict[str, Any] | None:
    """confirmation_level swing for LONG higher_low (mirror of low rule)."""
    l1_i = int(reference_low["pivot_index"])
    l2_i = int(candidate_low["pivot_index"])
    l2_conf = candidate_low["confirmation_timestamp"]
    highs = [
        s
        for s in swings
        if s.get("side") == "high"
        and int(s["pivot_index"]) < l2_i
        and swing_usable_as_of(s, l2_conf)
    ]
    between = [s for s in highs if l1_i < int(s["pivot_index"]) < l2_i]
    pool = between if between else highs
    if not pool:
        return None
    return sort_swings(pool)[-1]


def _empty_state_skeleton(
    *,
    setup: dict[str, Any],
    config: PriceActionConfig,
    state_name: PAStateName,
) -> dict[str, Any]:
    activation_ts = setup.get("setup_activation_timestamp")
    return {
        "setup_side": setup.get("setup_side"),
        "setup_type": setup.get("setup_type"),
        "setup_activation_timestamp": activation_ts,
        "state": state_name,
        "observed_from_timestamp": activation_ts,
        "last_updated_timestamp": activation_ts,
        "age_candles": 0,
        "reference_swing": None,
        "candidate_swing": None,
        "intermediate_swing": None,
        "pattern_type": None,
        "confirmation_level": None,
        "invalidation_level": None,
        "failed_break_level": None,
        "failed_break_extreme": None,
        "failed_break_timestamp": None,
        "unbuffered_failed_break_extreme": None,
        "failed_break_invalidation_buffer": None,
        "failed_break_invalidation_buffer_mode": None,
        "final_invalidation_level": None,
        "structure_armed_timestamp": None,
        "structure_armed_candle_index": None,
        "same_bar_confirmation_blocked": False,
        "same_bar_confirmation_blocked_count": 0,
        "invalid_structure_geometry": False,
        "waiting_for_confirmation_level": False,
        "pullback_detected": False,
        "structure_candidate_detected": False,
        "structure_confirmed": False,
        "structure_break_detected": False,
        "reason_codes": [],
        "warnings": list(setup.get("warnings") or []),
        "blockers": list(setup.get("blockers") or []),
        "invalidation_reason": None,
        "source_setup_activation": deepcopy(setup),
        "config": config.to_dict(),
        "processed_swing_keys": [],
        "known_swings": [],
        "confirmation": None,
        "event_log": [],
        "_current_candle_index": None,
    }


def _append_event(state: dict[str, Any], event_type: str, **payload: Any) -> None:
    state.setdefault("event_log", []).append(
        {
            "event": event_type,
            "timestamp": state.get("last_updated_timestamp"),
            "state": state.get("state"),
            **payload,
        }
    )


def _add_reason(state: dict[str, Any], code: str) -> None:
    codes = state.setdefault("reason_codes", [])
    if code not in codes:
        codes.append(code)


def _reject_structure_geometry(
    state: dict[str, Any],
    *,
    confirmation_level: float,
    invalidation_level: float,
    pattern_type: str | None,
) -> dict[str, Any]:
    state["invalid_structure_geometry"] = True
    state["confirmation_level"] = float(confirmation_level)
    state["invalidation_level"] = float(invalidation_level)
    state["final_invalidation_level"] = float(invalidation_level)
    state["structure_confirmed"] = False
    state["waiting_for_confirmation_level"] = False
    _add_reason(state, "INVALID_STRUCTURE_GEOMETRY")
    _append_event(
        state,
        "structure_geometry_invalid",
        pattern_type=pattern_type,
        confirmation_level=float(confirmation_level),
        invalidation_level=float(invalidation_level),
        reason="INVALID_STRUCTURE_GEOMETRY",
    )
    return _mark_terminal(
        state,
        name="invalidated",
        reason="INVALID_STRUCTURE_GEOMETRY",
        event="invalidated",
    )


def _mark_structure_armed(state: dict[str, Any], *, pattern_type: str) -> None:
    state["structure_armed_timestamp"] = state.get("last_updated_timestamp")
    state["structure_armed_candle_index"] = state.get("_current_candle_index")
    state["structure_candidate_detected"] = True
    state["structure_confirmed"] = True
    state["waiting_for_confirmation_level"] = False
    state["pullback_detected"] = True
    state["state"] = "waiting_for_structure_break"
    state["final_invalidation_level"] = state.get("invalidation_level")
    _append_event(state, "structure_armed", pattern_type=pattern_type)


def _try_arm_with_geometry(
    state: dict[str, Any],
    *,
    pattern_type: str,
    confirmation_level: float,
    invalidation_level: float,
) -> bool:
    """Arm if geometry valid; otherwise invalidate. Returns True if armed."""
    side = str(state.get("setup_side") or "")
    if not validate_structure_geometry(
        side=side,
        confirmation_level=confirmation_level,
        invalidation_level=invalidation_level,
    ):
        _reject_structure_geometry(
            state,
            confirmation_level=float(confirmation_level),
            invalidation_level=float(invalidation_level),
            pattern_type=pattern_type,
        )
        return False
    state["pattern_type"] = pattern_type
    state["confirmation_level"] = float(confirmation_level)
    state["invalidation_level"] = float(invalidation_level)
    _mark_structure_armed(state, pattern_type=pattern_type)
    return True


def initialize_price_action_state(
    setup_activation: dict[str, Any],
    config: PriceActionConfig | None = None,
    confirmed_swings_as_of_setup: list[dict[str, Any]] | list[ConfirmedPivot] | None = None,
) -> dict[str, Any]:
    """Create PA state from an activated SetupActivation + swings known at setup time."""
    cfg = config or default_price_action_config()
    setup = deepcopy(setup_activation)
    blockers = list(setup.get("blockers") or [])

    if "HTF_OPPOSING_TREND" in blockers or not setup.get("setup_activated"):
        state = _empty_state_skeleton(
            setup=setup,
            config=cfg,
            state_name="invalidated",
        )
        reason = (
            "HTF_OPPOSING_TREND"
            if "HTF_OPPOSING_TREND" in blockers
            else "SETUP_NOT_ACTIVATED"
        )
        state["invalidation_reason"] = reason
        if "HTF_OPPOSING_TREND" in blockers and "HTF_OPPOSING_TREND" not in state["blockers"]:
            state["blockers"].append("HTF_OPPOSING_TREND")
        _append_event(state, "invalidated", reason=reason)
        _assert_no_forbidden(state)
        return state

    side = setup.get("setup_side")
    if side not in {"long", "short"}:
        state = _empty_state_skeleton(setup=setup, config=cfg, state_name="invalidated")
        state["invalidation_reason"] = "INVALID_SETUP_SIDE"
        _append_event(state, "invalidated", reason="INVALID_SETUP_SIDE")
        _assert_no_forbidden(state)
        return state

    swings_raw = confirmed_swings_as_of_setup or []
    swings: list[dict[str, Any]] = []
    for item in swings_raw:
        swing = (
            confirmed_pivot_to_swing(item, source_timeframe=cfg.source_timeframe)
            if not (isinstance(item, dict) and "side" in item and "confirmation_timestamp" in item)
            else dict(item)
        )
        if "pivot_type" in swing and "side" not in swing:
            swing = confirmed_pivot_to_swing(swing, source_timeframe=cfg.source_timeframe)
        activation_ts = setup.get("setup_activation_timestamp")
        if activation_ts is not None and not swing_usable_as_of(swing, activation_ts):
            continue
        swings.append(swing)
    swings = sort_swings(swings)

    state = _empty_state_skeleton(
        setup=setup,
        config=cfg,
        state_name="waiting_for_pullback",
    )
    state["known_swings"] = deepcopy(swings)
    state["processed_swing_keys"] = [list(swing_key(s)) for s in swings]

    ref_side = "high" if side == "short" else "low"
    reference = _last_swing(
        swings,
        side=ref_side,
        as_of=setup.get("setup_activation_timestamp"),
    )
    if reference is None:
        state["warnings"].append("REFERENCE_SWING_MISSING")
        _add_reason(state, "REFERENCE_SWING_MISSING")
        _append_event(state, "setup_initialized", reference_swing=None)
    else:
        state["reference_swing"] = deepcopy(reference)
        _add_reason(state, "REFERENCE_SWING_SELECTED")
        _append_event(
            state,
            "setup_initialized",
            reference_swing=deepcopy(reference),
        )
        _append_event(state, "reference_swing_selected", swing=deepcopy(reference))

    _assert_no_forbidden(state)
    return state


def _mark_terminal(
    state: dict[str, Any],
    *,
    name: PAStateName,
    reason: str,
    event: str,
) -> dict[str, Any]:
    state["state"] = name
    state["invalidation_reason"] = reason
    _append_event(state, event, reason=reason)
    return state


def _arm_lower_high(
    state: dict[str, Any],
    *,
    reference: dict[str, Any],
    candidate: dict[str, Any],
    intermediate: dict[str, Any],
) -> None:
    confirmation_level = float(intermediate["price"])
    invalidation_level = float(candidate["price"])
    state["pattern_type"] = "lower_high"
    state["candidate_swing"] = deepcopy(candidate)
    state["intermediate_swing"] = deepcopy(intermediate)
    _add_reason(state, "LOWER_HIGH_ARMED")
    _append_event(state, "structure_candidate", pattern_type="lower_high")
    if not _try_arm_with_geometry(
        state,
        pattern_type="lower_high",
        confirmation_level=confirmation_level,
        invalidation_level=invalidation_level,
    ):
        return
    _append_event(
        state,
        "pullback_detected",
        candidate_swing=deepcopy(candidate),
    )


def _arm_higher_low(
    state: dict[str, Any],
    *,
    reference: dict[str, Any],
    candidate: dict[str, Any],
    intermediate: dict[str, Any],
) -> None:
    confirmation_level = float(intermediate["price"])
    invalidation_level = float(candidate["price"])
    state["pattern_type"] = "higher_low"
    state["candidate_swing"] = deepcopy(candidate)
    state["intermediate_swing"] = deepcopy(intermediate)
    _add_reason(state, "HIGHER_LOW_ARMED")
    _append_event(state, "structure_candidate", pattern_type="higher_low")
    if not _try_arm_with_geometry(
        state,
        pattern_type="higher_low",
        confirmation_level=confirmation_level,
        invalidation_level=invalidation_level,
    ):
        return
    _append_event(
        state,
        "pullback_detected",
        candidate_swing=deepcopy(candidate),
    )


def _apply_failed_break_levels(
    state: dict[str, Any],
    *,
    candle: dict[str, Any],
    level: float,
    extreme: float,
    pattern_type: str,
) -> None:
    cfg = PriceActionConfig(**state["config"])
    pack = compute_failed_break_invalidation_level(
        side=str(state["setup_side"]),
        extreme=float(extreme),
        atr=_candle_atr(candle),
        cfg=cfg,
    )
    state["pattern_type"] = pattern_type
    state["failed_break_level"] = float(level)
    state["failed_break_extreme"] = float(extreme)
    state["failed_break_timestamp"] = _candle_ts(candle)
    state["unbuffered_failed_break_extreme"] = pack["unbuffered_failed_break_extreme"]
    state["failed_break_invalidation_buffer"] = pack["failed_break_invalidation_buffer"]
    state["failed_break_invalidation_buffer_mode"] = pack[
        "failed_break_invalidation_buffer_mode"
    ]
    state["invalidation_level"] = pack["final_invalidation_level"]
    state["final_invalidation_level"] = pack["final_invalidation_level"]
    state["structure_candidate_detected"] = True
    state["pullback_detected"] = True


def _enter_waiting_for_confirmation_level(state: dict[str, Any], *, pattern_type: str) -> None:
    state["pattern_type"] = pattern_type
    state["confirmation_level"] = None
    state["structure_confirmed"] = False
    state["waiting_for_confirmation_level"] = True
    state["state"] = "waiting_for_confirmation_level"
    _append_event(state, "waiting_for_confirmation_level", pattern_type=pattern_type)


def _arm_failed_breakout(
    state: dict[str, Any],
    *,
    candle: dict[str, Any],
    level: float,
    extreme: float,
    intermediate: dict[str, Any] | None,
) -> None:
    _apply_failed_break_levels(
        state,
        candle=candle,
        level=level,
        extreme=extreme,
        pattern_type="failed_breakout",
    )
    _add_reason(state, "FAILED_BREAKOUT_DETECTED")
    _append_event(
        state,
        "failed_breakout",
        level=level,
        extreme=extreme,
        unbuffered_failed_break_extreme=state.get("unbuffered_failed_break_extreme"),
        failed_break_invalidation_buffer=state.get("failed_break_invalidation_buffer"),
        final_invalidation_level=state.get("final_invalidation_level"),
    )
    if intermediate is None:
        _enter_waiting_for_confirmation_level(state, pattern_type="failed_breakout")
        return
    state["intermediate_swing"] = deepcopy(intermediate)
    _try_arm_with_geometry(
        state,
        pattern_type="failed_breakout",
        confirmation_level=float(intermediate["price"]),
        invalidation_level=float(state["invalidation_level"]),
    )


def _arm_failed_breakdown(
    state: dict[str, Any],
    *,
    candle: dict[str, Any],
    level: float,
    extreme: float,
    intermediate: dict[str, Any] | None,
) -> None:
    _apply_failed_break_levels(
        state,
        candle=candle,
        level=level,
        extreme=extreme,
        pattern_type="failed_breakdown",
    )
    _add_reason(state, "FAILED_BREAKDOWN_DETECTED")
    _append_event(
        state,
        "failed_breakdown",
        level=level,
        extreme=extreme,
        unbuffered_failed_break_extreme=state.get("unbuffered_failed_break_extreme"),
        failed_break_invalidation_buffer=state.get("failed_break_invalidation_buffer"),
        final_invalidation_level=state.get("final_invalidation_level"),
    )
    if intermediate is None:
        _enter_waiting_for_confirmation_level(state, pattern_type="failed_breakdown")
        return
    state["intermediate_swing"] = deepcopy(intermediate)
    _try_arm_with_geometry(
        state,
        pattern_type="failed_breakdown",
        confirmation_level=float(intermediate["price"]),
        invalidation_level=float(state["invalidation_level"]),
    )


def _process_new_swings(state: dict[str, Any], newly: list[dict[str, Any]]) -> None:
    if state["state"] in _TERMINAL:
        return
    if state["state"] == "waiting_for_structure_break" and state.get("confirmation_level") is not None:
        # Still accept swings for failed-break confirmation_level fill / bookkeeping.
        pass

    cfg = PriceActionConfig(**state["config"])
    activation_ts = state["setup_activation_timestamp"]
    known = {tuple(k) if not isinstance(k, tuple) else k for k in state["processed_swing_keys"]}
    # Normalize processed keys to tuples
    known = {tuple(k) for k in state["processed_swing_keys"]}

    incoming: list[dict[str, Any]] = []
    for item in newly:
        swing = (
            confirmed_pivot_to_swing(item, source_timeframe=cfg.source_timeframe)
            if isinstance(item, ConfirmedPivot)
            or (isinstance(item, dict) and "pivot_type" in item and "side" not in item)
            else dict(item)
        )
        key = swing_key(swing)
        if key in known:
            continue
        # Developing / unconfirmed relative to candle time is rejected by caller;
        # also require confirmation after setup for structure candidates.
        incoming.append(swing)

    incoming = sort_swings(incoming)
    for swing in incoming:
        key = swing_key(swing)
        if key in known:
            continue
        known.add(key)
        state["processed_swing_keys"].append(list(key))
        state["known_swings"].append(deepcopy(swing))

        # Fill confirmation_level for failed patterns waiting on opposite swing.
        if (
            state.get("pattern_type") in {"failed_breakout", "failed_breakdown"}
            and state.get("confirmation_level") is None
            and state["state"] == "waiting_for_confirmation_level"
            and state.get("failed_break_timestamp") is not None
            and state.get("reference_swing") is not None
        ):
            if state["pattern_type"] == "failed_breakout" and swing["side"] == "low":
                chosen = _select_failed_breakout_confirmation_low(
                    state["known_swings"],
                    reference_high=state["reference_swing"],
                    failed_break_ts=state["failed_break_timestamp"],
                    as_of=state.get("last_updated_timestamp") or swing["confirmation_timestamp"],
                    setup_activation_ts=activation_ts,
                )
                if chosen is not None:
                    state["intermediate_swing"] = deepcopy(chosen)
                    _try_arm_with_geometry(
                        state,
                        pattern_type="failed_breakout",
                        confirmation_level=float(chosen["price"]),
                        invalidation_level=float(state["invalidation_level"]),
                    )
            elif state["pattern_type"] == "failed_breakdown" and swing["side"] == "high":
                chosen = _select_failed_breakdown_confirmation_high(
                    state["known_swings"],
                    reference_low=state["reference_swing"],
                    failed_break_ts=state["failed_break_timestamp"],
                    as_of=state.get("last_updated_timestamp") or swing["confirmation_timestamp"],
                    setup_activation_ts=activation_ts,
                )
                if chosen is not None:
                    state["intermediate_swing"] = deepcopy(chosen)
                    _try_arm_with_geometry(
                        state,
                        pattern_type="failed_breakdown",
                        confirmation_level=float(chosen["price"]),
                        invalidation_level=float(state["invalidation_level"]),
                    )

        # Do not re-arm once a structure path is confirmed or state is terminal.
        if state.get("structure_confirmed"):
            continue
        if state["state"] in _TERMINAL:
            continue

        reference = state.get("reference_swing")
        if reference is None:
            continue
        if not (_ts(swing["confirmation_timestamp"]) > _ts(activation_ts)):
            continue

        sep = abs(int(swing["pivot_index"]) - int(reference["pivot_index"]))
        if sep < int(cfg.minimum_swing_separation_candles):
            continue

        if state["setup_side"] == "short" and swing["side"] == "high":
            label = classify_swing_structure(
                reference,
                swing,
                side="high",
                epsilon_pct=cfg.price_epsilon_pct,
            )["structure_type"]
            if label != "lower_high":
                continue
            intermediate = _select_intermediate_low(
                state["known_swings"],
                reference_high=reference,
                candidate_high=swing,
            )
            if intermediate is None:
                state["warnings"].append("INTERMEDIATE_SWING_MISSING")
                state["state"] = "tracking_structure_candidate"
                state["candidate_swing"] = deepcopy(swing)
                state["structure_candidate_detected"] = True
                state["pattern_type"] = "lower_high"
                _append_event(state, "structure_candidate", pattern_type="lower_high")
                continue
            _arm_lower_high(
                state,
                reference=reference,
                candidate=swing,
                intermediate=intermediate,
            )
        elif state["setup_side"] == "long" and swing["side"] == "low":
            label = classify_swing_structure(
                reference,
                swing,
                side="low",
                epsilon_pct=cfg.price_epsilon_pct,
            )["structure_type"]
            if label != "higher_low":
                continue
            intermediate = _select_intermediate_high(
                state["known_swings"],
                reference_low=reference,
                candidate_low=swing,
            )
            if intermediate is None:
                state["warnings"].append("INTERMEDIATE_SWING_MISSING")
                state["state"] = "tracking_structure_candidate"
                state["candidate_swing"] = deepcopy(swing)
                state["structure_candidate_detected"] = True
                state["pattern_type"] = "higher_low"
                _append_event(state, "structure_candidate", pattern_type="higher_low")
                continue
            _arm_higher_low(
                state,
                reference=reference,
                candidate=swing,
                intermediate=intermediate,
            )


def _detect_failed_break(state: dict[str, Any], candle: dict[str, Any]) -> None:
    if state["state"] in _TERMINAL:
        return
    if state.get("structure_confirmed") and state.get("pattern_type") in {
        "lower_high",
        "higher_low",
        "failed_breakout",
        "failed_breakdown",
    }:
        # Already armed / tracking a pattern — do not overwrite LH/HL.
        if state.get("pattern_type") in {"lower_high", "higher_low"}:
            return
        if state.get("pattern_type") in {"failed_breakout", "failed_breakdown"}:
            return
    if state.get("waiting_for_confirmation_level"):
        return

    if state.get("reference_swing") is None:
        return

    cfg = PriceActionConfig(**state["config"])
    level = float(state["reference_swing"]["price"])
    tol = _tol(level, cfg.breakout_tolerance_pct)
    as_of = _candle_ts(candle)
    activation_ts = state.get("setup_activation_timestamp")

    if state["setup_side"] == "short":
        # Failed breakout: high pierces above level, close back at/under level.
        if _candle_high(candle) > level + tol and _candle_close(candle) <= level + tol:
            intermediate = _select_failed_breakout_confirmation_low(
                state["known_swings"],
                reference_high=state["reference_swing"],
                failed_break_ts=as_of,
                as_of=as_of,
                setup_activation_ts=activation_ts,
            )
            _arm_failed_breakout(
                state,
                candle=candle,
                level=level,
                extreme=_candle_high(candle),
                intermediate=intermediate,
            )
    elif state["setup_side"] == "long":
        if _candle_low(candle) < level - tol and _candle_close(candle) >= level - tol:
            intermediate = _select_failed_breakdown_confirmation_high(
                state["known_swings"],
                reference_low=state["reference_swing"],
                failed_break_ts=as_of,
                as_of=as_of,
                setup_activation_ts=activation_ts,
            )
            _arm_failed_breakdown(
                state,
                candle=candle,
                level=level,
                extreme=_candle_low(candle),
                intermediate=intermediate,
            )


def _check_invalidation(state: dict[str, Any], candle: dict[str, Any]) -> bool:
    """Return True if state became terminal via invalidation."""
    if state["state"] in _TERMINAL:
        return True
    level = state.get("invalidation_level")
    if level is None:
        return False
    cfg = PriceActionConfig(**state["config"])
    tol = _tol(float(level), cfg.breakout_tolerance_pct)
    close = _candle_close(candle)
    side = state["setup_side"]
    pattern = state.get("pattern_type")

    invalidated = False
    if side == "short":
        # Close above invalidation (LH price or failed extreme).
        if close > float(level) + tol:
            invalidated = True
    elif side == "long":
        if close < float(level) - tol:
            invalidated = True

    if invalidated:
        reason = f"CLOSE_BEYOND_INVALIDATION:{pattern or 'unknown'}"
        _mark_terminal(state, name="invalidated", reason=reason, event="invalidated")
        return True
    return False


def _check_structure_break(state: dict[str, Any], candle: dict[str, Any]) -> bool:
    """Return True if PriceActionConfirmation was produced."""
    if state["state"] != "waiting_for_structure_break":
        return False
    level = state.get("confirmation_level")
    if level is None:
        return False

    cfg = PriceActionConfig(**state["config"])
    tol = _tol(float(level), cfg.breakout_tolerance_pct)
    close = _candle_close(candle)
    would_confirm = False
    if state["setup_side"] == "short" and close < float(level) - tol:
        would_confirm = True
    if state["setup_side"] == "long" and close > float(level) + tol:
        would_confirm = True
    if not would_confirm:
        return False

    ts = _candle_ts(candle)
    armed_ts = state.get("structure_armed_timestamp")
    armed_idx = state.get("structure_armed_candle_index")
    cur_idx = _candle_index(candle)
    # Conservative same-bar policy: never confirm on the arming candle.
    same_bar = False
    if armed_ts is not None and not (_ts(ts) > _ts(armed_ts)):
        same_bar = True
    if (
        armed_idx is not None
        and cur_idx is not None
        and not (int(cur_idx) > int(armed_idx))
    ):
        same_bar = True
    if same_bar:
        state["same_bar_confirmation_blocked"] = True
        state["same_bar_confirmation_blocked_count"] = int(
            state.get("same_bar_confirmation_blocked_count") or 0
        ) + 1
        _append_event(
            state,
            "same_bar_confirmation_blocked",
            structure_armed_timestamp=armed_ts,
            structure_armed_candle_index=armed_idx,
            candle_timestamp=ts,
            candle_index=cur_idx,
            confirmation_level=level,
        )
        return False

    state["structure_break_detected"] = True
    state["state"] = "price_action_confirmed"
    confirmation = {
        "side": state["setup_side"],
        "pattern_type": state.get("pattern_type"),
        "setup_activation_timestamp": state.get("setup_activation_timestamp"),
        "structure_detection_timestamp": (
            (state.get("candidate_swing") or {}).get("confirmation_timestamp")
            or state.get("failed_break_timestamp")
        ),
        "structure_armed_timestamp": state.get("structure_armed_timestamp"),
        "structure_armed_candle_index": state.get("structure_armed_candle_index"),
        "structure_break_timestamp": ts,
        "confirmation_level": float(level),
        "invalidation_level": state.get("invalidation_level"),
        "final_invalidation_level": state.get("final_invalidation_level"),
        "unbuffered_failed_break_extreme": state.get("unbuffered_failed_break_extreme"),
        "failed_break_invalidation_buffer": state.get("failed_break_invalidation_buffer"),
        "same_bar_confirmation_blocked": bool(state.get("same_bar_confirmation_blocked")),
        "reference_swing": deepcopy(state.get("reference_swing")),
        "candidate_swing": deepcopy(state.get("candidate_swing")),
        "intermediate_swing": deepcopy(state.get("intermediate_swing")),
        "reason_codes": list(state.get("reason_codes") or []),
        "source_setup_activation": deepcopy(state.get("source_setup_activation")),
    }
    _assert_no_forbidden(confirmation)
    state["confirmation"] = confirmation
    _add_reason(state, "STRUCTURE_BREAK_CLOSE")
    _append_event(state, "structure_break", confirmation_level=level)
    _append_event(state, "price_action_confirmed", confirmation=deepcopy(confirmation))
    return True


def _try_arm_failed_break_from_known(
    state: dict[str, Any],
    candle: dict[str, Any],
) -> None:
    """While waiting_for_confirmation_level, arm once a local opposite swing exists."""
    if state["state"] != "waiting_for_confirmation_level":
        return
    if state.get("reference_swing") is None or state.get("failed_break_timestamp") is None:
        return
    if state.get("invalidation_level") is None:
        return
    as_of = _candle_ts(candle)
    activation_ts = state.get("setup_activation_timestamp")
    if state.get("pattern_type") == "failed_breakout":
        chosen = _select_failed_breakout_confirmation_low(
            state["known_swings"],
            reference_high=state["reference_swing"],
            failed_break_ts=state["failed_break_timestamp"],
            as_of=as_of,
            setup_activation_ts=activation_ts,
        )
        if chosen is None:
            return
        state["intermediate_swing"] = deepcopy(chosen)
        _try_arm_with_geometry(
            state,
            pattern_type="failed_breakout",
            confirmation_level=float(chosen["price"]),
            invalidation_level=float(state["invalidation_level"]),
        )
    elif state.get("pattern_type") == "failed_breakdown":
        chosen = _select_failed_breakdown_confirmation_high(
            state["known_swings"],
            reference_low=state["reference_swing"],
            failed_break_ts=state["failed_break_timestamp"],
            as_of=as_of,
            setup_activation_ts=activation_ts,
        )
        if chosen is None:
            return
        state["intermediate_swing"] = deepcopy(chosen)
        _try_arm_with_geometry(
            state,
            pattern_type="failed_breakdown",
            confirmation_level=float(chosen["price"]),
            invalidation_level=float(state["invalidation_level"]),
        )


def update_price_action_state(
    state: dict[str, Any],
    closed_candle: dict[str, Any],
    newly_confirmed_swings: list[dict[str, Any]] | list[ConfirmedPivot] | None = None,
    *,
    opposing_setup: dict[str, Any] | None = None,
    regime_invalidation: str | None = None,
) -> dict[str, Any]:
    """Advance PA state by exactly one closed candle + optional new swings.

    Update order (v1)
    -----------------
    1. age += 1
    2. opposing setup / explicit regime invalidation / HTF blockers
    3. max age → expired
    4. invalidation via Close vs invalidation_level
    5. ingest newly confirmed swings (deterministic order)
    6. arm LH/HL / fill failed-break levels
    7. detect failed BO/BD on this candle (if not yet armed)
    8. structure break via Close vs confirmation_level
    """
    out = deepcopy(state)
    if out["state"] in _TERMINAL:
        return out

    cfg = PriceActionConfig(**out["config"])
    out["last_updated_timestamp"] = _candle_ts(closed_candle)
    out["_current_candle_index"] = _candle_index(closed_candle)
    out["age_candles"] = int(out.get("age_candles") or 0) + 1

    # 2) external invalidation
    if opposing_setup is not None:
        opp_side = opposing_setup.get("setup_side")
        if (
            opposing_setup.get("setup_activated")
            and opp_side in {"long", "short"}
            and opp_side != out.get("setup_side")
        ):
            return _mark_terminal(
                out,
                name="invalidated",
                reason="NEW_OPPOSING_SETUP",
                event="invalidated",
            )

    if regime_invalidation:
        return _mark_terminal(
            out,
            name="invalidated",
            reason=str(regime_invalidation),
            event="invalidated",
        )

    blockers = out.get("blockers") or []
    if "HTF_OPPOSING_TREND" in blockers:
        return _mark_terminal(
            out,
            name="invalidated",
            reason="HTF_OPPOSING_TREND",
            event="invalidated",
        )

    # 3) age
    if out["age_candles"] > int(cfg.max_setup_age_candles):
        return _mark_terminal(
            out,
            name="expired",
            reason="MAX_SETUP_AGE",
            event="expired",
        )

    # 4) invalidation (only when a level exists)
    if _check_invalidation(out, closed_candle):
        return out

    # 5–6) swings
    _process_new_swings(out, list(newly_confirmed_swings or []))

    # If LH/HL armed without intermediate earlier, try fill from known swings.
    if (
        out["state"] == "tracking_structure_candidate"
        and out.get("pattern_type") == "lower_high"
        and out.get("candidate_swing") is not None
        and out.get("reference_swing") is not None
        and out.get("confirmation_level") is None
    ):
        intermediate = _select_intermediate_low(
            out["known_swings"],
            reference_high=out["reference_swing"],
            candidate_high=out["candidate_swing"],
        )
        if intermediate is not None:
            _arm_lower_high(
                out,
                reference=out["reference_swing"],
                candidate=out["candidate_swing"],
                intermediate=intermediate,
            )
    if (
        out["state"] == "tracking_structure_candidate"
        and out.get("pattern_type") == "higher_low"
        and out.get("candidate_swing") is not None
        and out.get("reference_swing") is not None
        and out.get("confirmation_level") is None
    ):
        intermediate = _select_intermediate_high(
            out["known_swings"],
            reference_low=out["reference_swing"],
            candidate_low=out["candidate_swing"],
        )
        if intermediate is not None:
            _arm_higher_low(
                out,
                reference=out["reference_swing"],
                candidate=out["candidate_swing"],
                intermediate=intermediate,
            )

    # 7) failed patterns only while still waiting for pullback (no LH/HL candidate yet)
    if out["state"] == "waiting_for_pullback" and not out.get("structure_candidate_detected"):
        _detect_failed_break(out, closed_candle)

    # 7b) failed BO/BD waiting for a local confirmation swing
    if out["state"] == "waiting_for_confirmation_level":
        _try_arm_failed_break_from_known(out, closed_candle)

    # 8) close break
    _check_structure_break(out, closed_candle)

    _assert_no_forbidden(out)
    return out


def evaluate_price_action_confirmation(
    state: dict[str, Any],
) -> dict[str, Any] | None:
    """Return PriceActionConfirmation dict if state is confirmed, else None."""
    if state.get("state") != "price_action_confirmed":
        return None
    confirmation = state.get("confirmation")
    if confirmation is None:
        return None
    _assert_no_forbidden(confirmation)
    return deepcopy(confirmation)
