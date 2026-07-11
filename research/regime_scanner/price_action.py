"""Phase-2 causal Price Action confirmation (no momentum / entry / TP).

Flow
----
SetupActivation → pullback / structure → Close break → PriceActionConfirmation

Reuses ``ConfirmedPivot`` / ``find_confirmed_pivots`` / ``filter_pivots_as_of``.
Does **not** import or call ``signal_tp_audit`` entry helpers.
Does **not** reuse exhaustion retest distance caps for PA lower highs.
"""

from __future__ import annotations

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
    max_setup_age_candles: int = 96
    price_epsilon_pct: float = 0.01
    breakout_tolerance_pct: float = 0.0
    source_timeframe: str = "5m"

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
    state["pattern_type"] = "lower_high"
    state["candidate_swing"] = deepcopy(candidate)
    state["intermediate_swing"] = deepcopy(intermediate)
    state["confirmation_level"] = float(intermediate["price"])
    state["invalidation_level"] = float(candidate["price"])
    state["structure_candidate_detected"] = True
    state["structure_confirmed"] = True
    state["pullback_detected"] = True
    state["state"] = "waiting_for_structure_break"
    _add_reason(state, "LOWER_HIGH_ARMED")
    _append_event(state, "structure_candidate", pattern_type="lower_high")
    _append_event(state, "structure_armed", pattern_type="lower_high")
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
    state["pattern_type"] = "higher_low"
    state["candidate_swing"] = deepcopy(candidate)
    state["intermediate_swing"] = deepcopy(intermediate)
    state["confirmation_level"] = float(intermediate["price"])
    state["invalidation_level"] = float(candidate["price"])
    state["structure_candidate_detected"] = True
    state["structure_confirmed"] = True
    state["pullback_detected"] = True
    state["state"] = "waiting_for_structure_break"
    _add_reason(state, "HIGHER_LOW_ARMED")
    _append_event(state, "structure_candidate", pattern_type="higher_low")
    _append_event(state, "structure_armed", pattern_type="higher_low")
    _append_event(
        state,
        "pullback_detected",
        candidate_swing=deepcopy(candidate),
    )


def _arm_failed_breakout(
    state: dict[str, Any],
    *,
    candle: dict[str, Any],
    level: float,
    extreme: float,
    intermediate: dict[str, Any] | None,
) -> None:
    ts = _candle_ts(candle)
    state["pattern_type"] = "failed_breakout"
    state["failed_break_level"] = float(level)
    state["failed_break_extreme"] = float(extreme)
    state["failed_break_timestamp"] = ts
    state["invalidation_level"] = float(extreme)
    if intermediate is not None:
        state["intermediate_swing"] = deepcopy(intermediate)
        state["confirmation_level"] = float(intermediate["price"])
        state["structure_candidate_detected"] = True
        state["structure_confirmed"] = True
        state["state"] = "waiting_for_structure_break"
    else:
        # Wait for a usable low before arming break.
        state["structure_candidate_detected"] = True
        state["structure_confirmed"] = False
        state["state"] = "tracking_structure_candidate"
    state["pullback_detected"] = True
    _add_reason(state, "FAILED_BREAKOUT_DETECTED")
    _append_event(
        state,
        "failed_breakout",
        level=level,
        extreme=extreme,
        confirmation_level=state.get("confirmation_level"),
    )


def _arm_failed_breakdown(
    state: dict[str, Any],
    *,
    candle: dict[str, Any],
    level: float,
    extreme: float,
    intermediate: dict[str, Any] | None,
) -> None:
    ts = _candle_ts(candle)
    state["pattern_type"] = "failed_breakdown"
    state["failed_break_level"] = float(level)
    state["failed_break_extreme"] = float(extreme)
    state["failed_break_timestamp"] = ts
    state["invalidation_level"] = float(extreme)
    if intermediate is not None:
        state["intermediate_swing"] = deepcopy(intermediate)
        state["confirmation_level"] = float(intermediate["price"])
        state["structure_candidate_detected"] = True
        state["structure_confirmed"] = True
        state["state"] = "waiting_for_structure_break"
    else:
        state["structure_candidate_detected"] = True
        state["structure_confirmed"] = False
        state["state"] = "tracking_structure_candidate"
    state["pullback_detected"] = True
    _add_reason(state, "FAILED_BREAKDOWN_DETECTED")
    _append_event(
        state,
        "failed_breakdown",
        level=level,
        extreme=extreme,
        confirmation_level=state.get("confirmation_level"),
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
            and state["state"] == "tracking_structure_candidate"
        ):
            if state["pattern_type"] == "failed_breakout" and swing["side"] == "low":
                state["intermediate_swing"] = deepcopy(swing)
                state["confirmation_level"] = float(swing["price"])
                state["structure_confirmed"] = True
                state["state"] = "waiting_for_structure_break"
                _append_event(state, "structure_armed", pattern_type="failed_breakout")
            elif state["pattern_type"] == "failed_breakdown" and swing["side"] == "high":
                state["intermediate_swing"] = deepcopy(swing)
                state["confirmation_level"] = float(swing["price"])
                state["structure_confirmed"] = True
                state["state"] = "waiting_for_structure_break"
                _append_event(state, "structure_armed", pattern_type="failed_breakdown")

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

    if state.get("reference_swing") is None:
        return

    cfg = PriceActionConfig(**state["config"])
    level = float(state["reference_swing"]["price"])
    tol = _tol(level, cfg.breakout_tolerance_pct)

    if state["setup_side"] == "short":
        # Failed breakout: high pierces above level, close back at/under level.
        if _candle_high(candle) > level + tol and _candle_close(candle) <= level + tol:
            # confirmation_level: last low usable at this candle time; else wait.
            intermediate = _last_swing(
                state["known_swings"],
                side="low",
                as_of=_candle_ts(candle),
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
            intermediate = _last_swing(
                state["known_swings"],
                side="high",
                as_of=_candle_ts(candle),
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
    confirmed = False
    if state["setup_side"] == "short" and close < float(level) - tol:
        confirmed = True
    if state["setup_side"] == "long" and close > float(level) + tol:
        confirmed = True
    if not confirmed:
        return False

    ts = _candle_ts(candle)
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
        "structure_break_timestamp": ts,
        "confirmation_level": float(level),
        "invalidation_level": state.get("invalidation_level"),
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
