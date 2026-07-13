"""Phase-3 causal Momentum confirmation (no entry / TP / live trading).

Flow
----
PriceActionConfirmation → MomentumConfirmation (5m closed candles only)

Window semantics (v1)
---------------------
``confirmation_window_candles = 3`` with ``allow_confirmation_on_break_candle``:

* Break candle = offset / age **0** (may confirm if allowed)
* Then ages **1**, **2**, **3**
* After unsuccessful evaluation of age **3** → ``expired``

15m / 30m are never used for momentum signals (regime/setup context only).
"""

from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any, Literal

import pandas as pd

SetupSide = Literal["long", "short"]
MomentumStateName = Literal[
    "waiting_for_momentum",
    "momentum_confirmed",
    "invalidated",
    "rejected",
    "expired",
]
Confidence = Literal["low", "medium", "high"]

_FORBIDDEN_KEYS = frozenset(
    {
        "entry_price",
        "tp_price",
        "tp_pct",
        "stop_loss",
        "position_size",
        "mae_pct",
        "mfe_pct",
        "max_adverse_excursion_pct",
        "max_favorable_excursion_pct",
    }
)

_TERMINAL = frozenset({"momentum_confirmed", "invalidated", "rejected", "expired"})


@dataclass(frozen=True)
class MomentumConfig:
    """Defaults are conservative starters — not fitted to the March week."""

    confirmation_window_candles: int = 3
    allow_confirmation_on_break_candle: bool = True
    min_body_to_range_ratio: float = 0.50
    min_close_location_ratio: float = 0.60
    min_range_atr_ratio: float = 0.30
    max_range_atr_ratio: float = 3.00
    require_directional_body: bool = True
    require_structure_level_hold: bool = True
    max_counter_move_pct: float = 0.50
    volume_filter_enabled: bool = False
    min_volume_to_median_ratio: float = 1.00
    # High-confidence overlays (still not optimised).
    high_min_body_to_range_ratio: float = 0.65
    high_min_close_location_ratio: float = 0.75
    high_min_range_atr_ratio: float = 0.50
    high_max_range_atr_ratio: float = 2.50

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def default_momentum_config() -> MomentumConfig:
    return MomentumConfig()


def _ts(value: object) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def _candle_ts(candle: dict[str, Any]) -> str:
    return str(_ts(candle["timestamp"]).isoformat())


def _finite(value: object) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _assert_no_forbidden(payload: dict[str, Any]) -> None:
    stack: list[Any] = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            bad = _FORBIDDEN_KEYS.intersection(item)
            if bad:
                raise ValueError(f"forbidden Phase-3 fields present: {sorted(bad)}")
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)


def _add_reason(state: dict[str, Any], code: str) -> None:
    codes = state.setdefault("reason_codes", [])
    if code not in codes:
        codes.append(code)


def _append_event(state: dict[str, Any], event_type: str, **payload: Any) -> None:
    state.setdefault("event_log", []).append(
        {
            "event": event_type,
            "timestamp": state.get("last_updated_timestamp"),
            "state": state.get("state"),
            **payload,
        }
    )


def _mark_terminal(
    state: dict[str, Any],
    *,
    name: MomentumStateName,
    reason: str,
    event: str,
) -> dict[str, Any]:
    state["state"] = name
    state["invalidation_reason"] = reason
    _append_event(state, event, reason=reason)
    return state


# ---------------------------------------------------------------------------
# Pure candle metrics (side-symmetric)
# ---------------------------------------------------------------------------


def candle_range(candle: dict[str, Any]) -> float | None:
    high = _finite(candle.get("high"))
    low = _finite(candle.get("low"))
    if high is None or low is None:
        return None
    if high < low:
        return None
    return high - low


def candle_body(candle: dict[str, Any]) -> float | None:
    open_ = _finite(candle.get("open"))
    close = _finite(candle.get("close"))
    if open_ is None or close is None:
        return None
    return abs(close - open_)


def body_to_range_ratio(candle: dict[str, Any]) -> float | None:
    rng = candle_range(candle)
    body = candle_body(candle)
    if rng is None or body is None:
        return None
    if rng == 0.0:
        return 0.0
    return body / rng


def close_location_ratio(candle: dict[str, Any], *, side: str) -> float | None:
    """Long: (close-low)/range; Short: (high-close)/range."""
    rng = candle_range(candle)
    high = _finite(candle.get("high"))
    low = _finite(candle.get("low"))
    close = _finite(candle.get("close"))
    if rng is None or high is None or low is None or close is None:
        return None
    if rng == 0.0:
        return 0.0
    if side == "long":
        return (close - low) / rng
    if side == "short":
        return (high - close) / rng
    return None


def range_atr_ratio(candle: dict[str, Any], atr: float | None) -> float | None:
    rng = candle_range(candle)
    atr_v = _finite(atr)
    if rng is None or atr_v is None or atr_v <= 0.0:
        return None
    return rng / atr_v


def directional_body(candle: dict[str, Any], *, side: str) -> bool | None:
    open_ = _finite(candle.get("open"))
    close = _finite(candle.get("close"))
    if open_ is None or close is None:
        return None
    if side == "long":
        return close > open_
    if side == "short":
        return close < open_
    return None


def volume_ratio(
    candle: dict[str, Any],
    rolling_median_volume: float | None,
) -> float | None:
    vol = _finite(candle.get("volume"))
    med = _finite(rolling_median_volume)
    if vol is None or med is None or med <= 0.0:
        return None
    return vol / med


def ohlc_is_valid(candle: dict[str, Any]) -> bool:
    for key in ("open", "high", "low", "close"):
        if _finite(candle.get(key)) is None:
            return False
    high = float(candle["high"])
    low = float(candle["low"])
    return high >= low


def compute_candle_metrics(
    candle: dict[str, Any],
    *,
    side: str,
    atr: float | None = None,
    rolling_median_volume: float | None = None,
) -> dict[str, Any]:
    """Diagnostic metrics for one closed candle."""
    valid = ohlc_is_valid(candle)
    return {
        "ohlc_valid": valid,
        "open": _finite(candle.get("open")),
        "high": _finite(candle.get("high")),
        "low": _finite(candle.get("low")),
        "close": _finite(candle.get("close")),
        "volume": _finite(candle.get("volume")),
        "atr": _finite(atr),
        "candle_range": candle_range(candle) if valid else None,
        "candle_body": candle_body(candle) if valid else None,
        "body_to_range_ratio": body_to_range_ratio(candle) if valid else None,
        "close_location_ratio": close_location_ratio(candle, side=side) if valid else None,
        "range_atr_ratio": range_atr_ratio(candle, atr) if valid else None,
        "directional_body": directional_body(candle, side=side) if valid else None,
        "volume_ratio": volume_ratio(candle, rolling_median_volume),
    }


# ---------------------------------------------------------------------------
# Structure hold / counter-move
# ---------------------------------------------------------------------------


def structure_level_held(
    candle: dict[str, Any],
    *,
    side: str,
    confirmation_level: float,
) -> bool | None:
    close = _finite(candle.get("close"))
    if close is None:
        return None
    if side == "long":
        return close > float(confirmation_level)
    if side == "short":
        return close < float(confirmation_level)
    return None


def counter_move_pct(
    candle: dict[str, Any],
    *,
    side: str,
    reference_close: float,
) -> float | None:
    """Adverse percent move from the PA break close (wick ignored; close-based)."""
    close = _finite(candle.get("close"))
    ref = _finite(reference_close)
    if close is None or ref is None or ref == 0.0:
        return None
    if side == "long":
        if close >= ref:
            return 0.0
        return (ref - close) / abs(ref) * 100.0
    if side == "short":
        if close <= ref:
            return 0.0
        return (close - ref) / abs(ref) * 100.0
    return None


# ---------------------------------------------------------------------------
# Condition evaluation
# ---------------------------------------------------------------------------


def evaluate_momentum_conditions(
    metrics: dict[str, Any],
    *,
    side: str,
    config: MomentumConfig,
    confirmation_level: float,
    candle: dict[str, Any],
    allow_confirm: bool,
) -> dict[str, Any]:
    """Return passed/failed condition lists and whether momentum confirms."""
    cfg = config
    passed: list[str] = []
    failed: list[str] = []

    if not metrics.get("ohlc_valid"):
        failed.append("OHLC_VALID")
        return {
            "passed": passed,
            "failed": failed,
            "confirms": False,
            "structure_level_held": None,
            "confidence": "low",
            "blocker": "INVALID_OHLC",
        }

    held = structure_level_held(
        candle, side=side, confirmation_level=confirmation_level
    )
    if cfg.require_structure_level_hold:
        if held is True:
            passed.append("STRUCTURE_LEVEL_HOLD")
        else:
            failed.append("STRUCTURE_LEVEL_HOLD")

    if cfg.require_directional_body:
        if metrics.get("directional_body") is True:
            passed.append("DIRECTIONAL_BODY")
        else:
            failed.append("DIRECTIONAL_BODY")

    body_r = metrics.get("body_to_range_ratio")
    if body_r is not None and body_r >= cfg.min_body_to_range_ratio:
        passed.append("BODY_TO_RANGE")
    else:
        failed.append("BODY_TO_RANGE")

    close_r = metrics.get("close_location_ratio")
    if close_r is not None and close_r >= cfg.min_close_location_ratio:
        passed.append("CLOSE_LOCATION")
    else:
        failed.append("CLOSE_LOCATION")

    range_r = metrics.get("range_atr_ratio")
    if range_r is None:
        failed.append("RANGE_ATR")
    elif range_r < cfg.min_range_atr_ratio:
        failed.append("RANGE_ATR_TOO_SMALL")
    elif range_r > cfg.max_range_atr_ratio:
        failed.append("RANGE_ATR_TOO_LARGE")
    else:
        passed.append("RANGE_ATR")

    if cfg.volume_filter_enabled:
        vol_r = metrics.get("volume_ratio")
        if vol_r is not None and vol_r >= cfg.min_volume_to_median_ratio:
            passed.append("VOLUME")
        else:
            failed.append("VOLUME")

    confirms = allow_confirm and not failed
    confidence: Confidence = "low"
    if confirms:
        confidence = "medium"
        high_ok = (
            body_r is not None
            and body_r >= cfg.high_min_body_to_range_ratio
            and close_r is not None
            and close_r >= cfg.high_min_close_location_ratio
            and range_r is not None
            and cfg.high_min_range_atr_ratio <= range_r <= cfg.high_max_range_atr_ratio
        )
        if cfg.volume_filter_enabled:
            vol_r = metrics.get("volume_ratio")
            high_ok = high_ok and vol_r is not None and vol_r >= cfg.min_volume_to_median_ratio
        if high_ok:
            confidence = "high"

    return {
        "passed": passed,
        "failed": failed,
        "confirms": confirms,
        "structure_level_held": held,
        "confidence": confidence,
        "blocker": None,
    }


# ---------------------------------------------------------------------------
# State machine API
# ---------------------------------------------------------------------------


def initialize_momentum_state(
    price_action_confirmation: dict[str, Any],
    config: MomentumConfig | None = None,
) -> dict[str, Any]:
    """Create MomentumState from a PriceActionConfirmation dict."""
    cfg = config or default_momentum_config()
    pa = deepcopy(price_action_confirmation)
    side = pa.get("side")
    if side not in {"long", "short"}:
        state = {
            "side": side,
            "state": "rejected",
            "price_action_confirmation_timestamp": pa.get("structure_break_timestamp")
            or pa.get("setup_activation_timestamp"),
            "structure_break_timestamp": pa.get("structure_break_timestamp"),
            "confirmation_level": pa.get("confirmation_level"),
            "invalidation_level": pa.get("invalidation_level")
            or pa.get("final_invalidation_level"),
            "window_start_timestamp": pa.get("structure_break_timestamp"),
            "last_updated_timestamp": None,
            "age_candles": None,
            "evaluated_candles": 0,
            "latest_metrics": None,
            "latest_condition_result": None,
            "candle_diagnostics": [],
            "reason_codes": ["INVALID_SIDE"],
            "warnings": [],
            "blockers": ["INVALID_SIDE"],
            "invalidation_reason": "INVALID_SIDE",
            "source_price_action_confirmation": pa,
            "config": cfg.to_dict(),
            "confirmation": None,
            "event_log": [],
            "break_close": None,
            "pattern_type": pa.get("pattern_type"),
            "setup_id": pa.get("setup_id"),
        }
        _append_event(state, "rejected", reason="INVALID_SIDE")
        _assert_no_forbidden(state)
        return state

    break_ts = pa.get("structure_break_timestamp")
    state: dict[str, Any] = {
        "side": side,
        "state": "waiting_for_momentum",
        "price_action_confirmation_timestamp": break_ts,
        "structure_break_timestamp": break_ts,
        "confirmation_level": float(pa["confirmation_level"])
        if pa.get("confirmation_level") is not None
        else None,
        "invalidation_level": (
            float(pa["invalidation_level"])
            if pa.get("invalidation_level") is not None
            else (
                float(pa["final_invalidation_level"])
                if pa.get("final_invalidation_level") is not None
                else None
            )
        ),
        "window_start_timestamp": break_ts,
        "last_updated_timestamp": None,
        "age_candles": None,
        "evaluated_candles": 0,
        "latest_metrics": None,
        "latest_condition_result": None,
        "candle_diagnostics": [],
        "reason_codes": [],
        "warnings": list(pa.get("warnings") or []),
        "blockers": list(pa.get("blockers") or []),
        "invalidation_reason": None,
        "source_price_action_confirmation": pa,
        "config": cfg.to_dict(),
        "confirmation": None,
        "event_log": [],
        "break_close": None,
        "pattern_type": pa.get("pattern_type"),
        "setup_id": pa.get("setup_id"),
    }
    _append_event(state, "momentum_initialized")
    _assert_no_forbidden(state)
    return state


def evaluate_momentum_confirmation(state: dict[str, Any]) -> dict[str, Any] | None:
    if state.get("state") != "momentum_confirmed":
        return None
    confirmation = state.get("confirmation")
    if confirmation is None:
        return None
    _assert_no_forbidden(confirmation)
    return deepcopy(confirmation)


def update_momentum_state(
    state: dict[str, Any],
    closed_candle: dict[str, Any],
    *,
    atr: float | None = None,
    rolling_median_volume: float | None = None,
    opposing_setup: dict[str, Any] | None = None,
    price_action_invalidated: bool = False,
) -> dict[str, Any]:
    """Advance momentum by exactly one closed 5m candle."""
    out = deepcopy(state)
    if out["state"] in _TERMINAL:
        return out

    cfg = MomentumConfig(**out["config"])
    ts = _candle_ts(closed_candle)
    out["last_updated_timestamp"] = ts

    # 1) external invalidations before age/metrics
    if price_action_invalidated:
        return _mark_terminal(
            out,
            name="invalidated",
            reason="PRICE_ACTION_INVALIDATED",
            event="invalidated",
        )
    if opposing_setup is not None:
        opp_side = opposing_setup.get("setup_side")
        if (
            opposing_setup.get("setup_activated")
            and opp_side in {"long", "short"}
            and opp_side != out.get("side")
        ):
            return _mark_terminal(
                out,
                name="invalidated",
                reason="NEW_OPPOSING_SETUP",
                event="invalidated",
            )

    # 2) age: break candle = 0, then 1..window
    prev_age = out.get("age_candles")
    age = 0 if prev_age is None else int(prev_age) + 1
    out["age_candles"] = age
    out["evaluated_candles"] = int(out.get("evaluated_candles") or 0) + 1

    if age > int(cfg.confirmation_window_candles):
        return _mark_terminal(
            out,
            name="expired",
            reason="MOMENTUM_WINDOW_EXPIRED",
            event="expired",
        )

    # Capture break close on age 0
    if age == 0:
        out["break_close"] = _finite(closed_candle.get("close"))

    side = str(out["side"])
    conf_level = out.get("confirmation_level")
    if conf_level is None:
        out["blockers"] = list(out.get("blockers") or []) + ["MISSING_CONFIRMATION_LEVEL"]
        return _mark_terminal(
            out,
            name="rejected",
            reason="MISSING_CONFIRMATION_LEVEL",
            event="rejected",
        )

    # 3) structure hold / invalidation (close-only)
    if not ohlc_is_valid(closed_candle):
        out["blockers"] = list(dict.fromkeys([*(out.get("blockers") or []), "INVALID_OHLC"]))
        _add_reason(out, "INVALID_OHLC")
        metrics = compute_candle_metrics(
            closed_candle,
            side=side,
            atr=atr,
            rolling_median_volume=rolling_median_volume,
        )
        out["latest_metrics"] = metrics
        diag = _build_diagnostic(
            out,
            closed_candle,
            metrics=metrics,
            condition_result={
                "passed": [],
                "failed": ["OHLC_VALID"],
                "confirms": False,
                "structure_level_held": None,
                "confidence": "low",
                "blocker": "INVALID_OHLC",
            },
            candle_offset=age,
        )
        out["candle_diagnostics"].append(diag)
        _append_event(out, "candle_evaluated", **{k: diag[k] for k in (
            "candle_timestamp",
            "candle_offset",
            "failed_conditions",
            "passed_conditions",
        )})
        # Invalid OHLC does not confirm; continue window unless last candle
        if age >= int(cfg.confirmation_window_candles):
            return _mark_terminal(
                out,
                name="expired",
                reason="MOMENTUM_WINDOW_EXPIRED",
                event="expired",
            )
        return out

    held = structure_level_held(
        closed_candle, side=side, confirmation_level=float(conf_level)
    )
    if cfg.require_structure_level_hold and held is False:
        metrics = compute_candle_metrics(
            closed_candle,
            side=side,
            atr=atr,
            rolling_median_volume=rolling_median_volume,
        )
        out["latest_metrics"] = metrics
        cond = {
            "passed": [],
            "failed": ["STRUCTURE_LEVEL_HOLD"],
            "confirms": False,
            "structure_level_held": False,
            "confidence": "low",
            "blocker": None,
        }
        out["latest_condition_result"] = cond
        diag = _build_diagnostic(
            out, closed_candle, metrics=metrics, condition_result=cond, candle_offset=age
        )
        out["candle_diagnostics"].append(diag)
        return _mark_terminal(
            out,
            name="invalidated",
            reason="CLOSE_BEYOND_STRUCTURE_LEVEL",
            event="invalidated",
        )

    # Counter-move vs break close
    break_close = out.get("break_close")
    if break_close is not None:
        cm = counter_move_pct(
            closed_candle, side=side, reference_close=float(break_close)
        )
        if cm is not None and cm >= float(cfg.max_counter_move_pct):
            metrics = compute_candle_metrics(
                closed_candle,
                side=side,
                atr=atr,
                rolling_median_volume=rolling_median_volume,
            )
            out["latest_metrics"] = metrics
            return _mark_terminal(
                out,
                name="invalidated",
                reason="MAX_COUNTER_MOVE",
                event="invalidated",
            )

    # 4) metrics
    metrics = compute_candle_metrics(
        closed_candle,
        side=side,
        atr=atr,
        rolling_median_volume=rolling_median_volume,
    )
    out["latest_metrics"] = metrics

    # 5–6) confirm conditions
    allow_confirm = True
    if age == 0 and not cfg.allow_confirmation_on_break_candle:
        allow_confirm = False
    cond = evaluate_momentum_conditions(
        metrics,
        side=side,
        config=cfg,
        confirmation_level=float(conf_level),
        candle=closed_candle,
        allow_confirm=allow_confirm,
    )
    if age == 0 and not cfg.allow_confirmation_on_break_candle:
        if "BREAK_CANDLE_DISALLOWED" not in cond["failed"]:
            cond["failed"] = list(cond["failed"]) + ["BREAK_CANDLE_DISALLOWED"]
        cond["confirms"] = False
    out["latest_condition_result"] = cond
    diag = _build_diagnostic(
        out, closed_candle, metrics=metrics, condition_result=cond, candle_offset=age
    )
    out["candle_diagnostics"].append(diag)
    _append_event(
        out,
        "candle_evaluated",
        candle_timestamp=ts,
        candle_offset=age,
        passed_conditions=list(cond["passed"]),
        failed_conditions=list(cond["failed"]),
        confirms=bool(cond["confirms"]),
    )

    if cond["confirms"]:
        confirmation_type = "break_candle" if age == 0 else f"candle_{age}"
        confirmation = {
            "side": side,
            "pattern_type": out.get("pattern_type"),
            "setup_id": out.get("setup_id"),
            "confirmation_timestamp": ts,
            "confirming_candle_timestamp": ts,
            "candles_after_price_action_confirmation": int(age),
            "confirmation_type": confirmation_type,
            "body_to_range_ratio": metrics.get("body_to_range_ratio"),
            "close_location_ratio": metrics.get("close_location_ratio"),
            "range_atr_ratio": metrics.get("range_atr_ratio"),
            "volume_ratio": metrics.get("volume_ratio"),
            "directional_body": metrics.get("directional_body"),
            "structure_level_held": cond.get("structure_level_held"),
            "confidence": cond.get("confidence"),
            "reason_codes": list(cond.get("passed") or []),
            "source_price_action_confirmation": deepcopy(
                out.get("source_price_action_confirmation")
            ),
        }
        _assert_no_forbidden(confirmation)
        out["confirmation"] = confirmation
        out["state"] = "momentum_confirmed"
        _add_reason(out, "MOMENTUM_CONFIRMED")
        _append_event(
            out,
            "momentum_confirmed",
            confirmation_type=confirmation_type,
            confidence=confirmation["confidence"],
        )
        return out

    # 7) window expiry after evaluating last allowed candle
    if age >= int(cfg.confirmation_window_candles):
        return _mark_terminal(
            out,
            name="expired",
            reason="MOMENTUM_WINDOW_EXPIRED",
            event="expired",
        )
    return out


def _build_diagnostic(
    state: dict[str, Any],
    candle: dict[str, Any],
    *,
    metrics: dict[str, Any],
    condition_result: dict[str, Any],
    candle_offset: int,
) -> dict[str, Any]:
    return {
        "setup_id": state.get("setup_id"),
        "pattern_type": state.get("pattern_type"),
        "side": state.get("side"),
        "candle_timestamp": _candle_ts(candle),
        "candle_offset": int(candle_offset),
        "open": metrics.get("open"),
        "high": metrics.get("high"),
        "low": metrics.get("low"),
        "close": metrics.get("close"),
        "atr": metrics.get("atr"),
        "body_to_range_ratio": metrics.get("body_to_range_ratio"),
        "close_location_ratio": metrics.get("close_location_ratio"),
        "range_atr_ratio": metrics.get("range_atr_ratio"),
        "directional_body": metrics.get("directional_body"),
        "structure_level_held": condition_result.get("structure_level_held"),
        "volume_ratio": metrics.get("volume_ratio"),
        "passed_conditions": list(condition_result.get("passed") or []),
        "failed_conditions": list(condition_result.get("failed") or []),
        "momentum_state_after": state.get("state"),
        "confidence": condition_result.get("confidence"),
    }
