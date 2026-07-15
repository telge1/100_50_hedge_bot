"""Deterministic normalization of scanner outputs before storage and hashing."""

from __future__ import annotations

import math
from typing import Any

from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.research_runs.hashing import json_hash
from research.regime_scanner.timeframes import ensure_utc_timestamp

# Documented normalization rules:
# - Timestamps: UTC ISO-8601 via ensure_utc_timestamp().isoformat()
# - Floats: None for NaN/Inf, else round-trip via "%.17g"
# - Sorting: trend states by timestamp; structure by (timestamp, event_type, event_key);
#   signals by (timestamp, direction, signal_key)
# - Event keys documented per entity below.


def _iso(value: object | None) -> str | None:
    if value is None:
        return None
    return ensure_utc_timestamp(value).isoformat()


def _float(value: object | None) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return float(f"{number:.17g}")


def _clean(value: Any) -> Any:
    return json_safe(value)


def trend_state_event_key(*, timestamp: str, state: str, transition_reason: str | None) -> str:
    return f"{timestamp}|{state}|{transition_reason or ''}"


def structure_event_key(
    *,
    timestamp: str,
    event_type: str,
    direction: str | None,
    reference_pivot_time: str | None,
    price: float | None,
    timeframe: str | None = None,
) -> str:
    ref = reference_pivot_time or ""
    price_s = "" if price is None else f"{price:.17g}"
    direction_s = direction or ""
    tf = timeframe or ""
    return f"{timestamp}|{event_type}|{direction_s}|{ref}|{price_s}|{tf}"


def signal_event_key(
    *,
    timestamp: str,
    direction: str | None,
    signal_type: str,
    setup_id: str | None,
) -> str:
    return f"{timestamp}|{direction or ''}|{signal_type}|{setup_id or ''}"


def normalize_trend_states(snapshots: list[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for snap in snapshots:
        raw = snap.to_dict() if hasattr(snap, "to_dict") else dict(snap)
        ts = _iso(raw.get("decision_time"))
        if ts is None:
            continue
        state = str(raw.get("current_state") or "")
        prev = raw.get("previous_state")
        prev_s = None if prev is None else str(prev)
        reasons = list(raw.get("active_reasons") or [])
        transition = None
        if prev_s and prev_s != state:
            transition = "|".join(str(r) for r in reasons) or None
        structure_5m = raw.get("structure_5m") or {}
        direction = structure_5m.get("bias") if isinstance(structure_5m, dict) else None
        row = {
            "event_key": trend_state_event_key(
                timestamp=ts, state=state, transition_reason=transition
            ),
            "timestamp": ts,
            "state": state,
            "previous_state": prev_s,
            "direction": None if direction is None else str(direction),
            "strength": _float(raw.get("state_confidence")),
            "transition_reason": transition,
            "confirmation_count": len(reasons) if reasons else None,
            "protective_high": None,
            "protective_low": None,
            "metadata_json": _clean(
                {
                    "age_5m_bars": raw.get("age_5m_bars"),
                    "min_hold_remaining": raw.get("min_hold_remaining"),
                    "bearish_score": _float(raw.get("bearish_score")),
                    "bullish_score": _float(raw.get("bullish_score")),
                    "weakening_score": _float(raw.get("weakening_score")),
                    "bottoming_score": _float(raw.get("bottoming_score")),
                    "allow_long": raw.get("allow_long"),
                    "allow_short": raw.get("allow_short"),
                    "structure_5m": structure_5m,
                    "structure_15m": raw.get("structure_15m"),
                    "context_30m": raw.get("context_30m"),
                    "active_reasons": reasons,
                    "policy": raw.get("policy"),
                }
            ),
        }
        rows.append(row)
    seen: dict[str, int] = {}
    for row in rows:
        base = row["event_key"]
        n = seen.get(base, 0)
        seen[base] = n + 1
        if n:
            row["event_key"] = f"{base}|#{n}"
    rows.sort(key=lambda r: (r["timestamp"], r["state"], r["event_key"]))
    return rows


def normalize_structure_events(
    events: list[Any],
    *,
    start_time: object | None = None,
    end_time: object | None = None,
) -> list[dict[str, Any]]:
    start_ts = None if start_time is None else ensure_utc_timestamp(start_time)
    end_ts = None if end_time is None else ensure_utc_timestamp(end_time)
    rows: list[dict[str, Any]] = []
    for event in events:
        if hasattr(event, "to_dict"):
            raw = event.to_dict()
        elif hasattr(event, "__dict__"):
            raw = dict(vars(event))
        else:
            raw = dict(event)
        ts = _iso(raw.get("event_time") or raw.get("timestamp"))
        if ts is None:
            continue
        ts_obj = ensure_utc_timestamp(ts)
        if start_ts is not None and ts_obj < start_ts:
            continue
        if end_ts is not None and ts_obj >= end_ts:
            continue
        event_type = str(raw.get("event_type") or "")
        direction = raw.get("direction")
        price = _float(raw.get("level") or raw.get("price"))
        ref_time = _iso(raw.get("reference_pivot_time"))
        swing_type = None
        if event_type in {"higher_high", "lower_high", "equal_high"}:
            swing_type = "high"
        elif event_type in {"higher_low", "lower_low", "equal_low"}:
            swing_type = "low"
        timeframe = None if raw.get("timeframe") is None else str(raw.get("timeframe"))
        row = {
            "event_key": structure_event_key(
                timestamp=ts,
                event_type=event_type,
                direction=None if direction is None else str(direction),
                reference_pivot_time=ref_time,
                price=price,
                timeframe=timeframe,
            ),
            "timestamp": ts,
            "event_type": event_type,
            "direction": None if direction is None else str(direction),
            "price": price,
            "swing_type": swing_type,
            "protective_level": price,
            "structure_state": timeframe,
            "metadata_json": _clean(
                {
                    "timeframe": timeframe,
                    "reference_pivot_price": _float(raw.get("reference_pivot_price")),
                    "reason_codes": list(raw.get("reason_codes") or []),
                }
            ),
        }
        rows.append(row)
    # Disambiguate rare true duplicates with a stable ordinal suffix.
    seen: dict[str, int] = {}
    for row in rows:
        base = row["event_key"]
        n = seen.get(base, 0)
        seen[base] = n + 1
        if n:
            row["event_key"] = f"{base}|#{n}"
    rows.sort(key=lambda r: (r["timestamp"], r["event_type"], r["event_key"]))
    return rows


def normalize_price_action_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in events:
        ts = _iso(raw.get("timestamp") or raw.get("event_timestamp"))
        if ts is None:
            continue
        event_type = str(raw.get("event") or raw.get("event_type") or "unknown")
        setup_id = raw.get("setup_id")
        key = f"{ts}|{event_type}|{setup_id or ''}"
        rows.append(
            {
                "event_key": key,
                "timestamp": ts,
                "event_type": event_type,
                "metadata_json": _clean(raw),
            }
        )
    rows.sort(key=lambda r: (r["timestamp"], r["event_type"], r["event_key"]))
    return rows


def normalize_momentum_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in events:
        ts = _iso(raw.get("timestamp"))
        if ts is None:
            continue
        event_type = str(raw.get("event") or "unknown")
        setup_id = raw.get("setup_id")
        key = f"{ts}|{event_type}|{setup_id or ''}"
        rows.append(
            {
                "event_key": key,
                "timestamp": ts,
                "event_type": event_type,
                "metadata_json": _clean(raw),
            }
        )
    rows.sort(key=lambda r: (r["timestamp"], r["event_type"], r["event_key"]))
    return rows


def normalize_signals_from_momentum(confirmations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in confirmations:
        ts = _iso(
            raw.get("momentum_confirmation_timestamp")
            or raw.get("confirmation_timestamp")
            or raw.get("timestamp")
        )
        if ts is None:
            continue
        direction = raw.get("side") or raw.get("direction")
        setup_id = raw.get("setup_id")
        signal_type = "momentum_confirmed"
        row = {
            "signal_key": signal_event_key(
                timestamp=ts,
                direction=None if direction is None else str(direction),
                signal_type=signal_type,
                setup_id=None if setup_id is None else str(setup_id),
            ),
            "timestamp": ts,
            "direction": None if direction is None else str(direction),
            "signal_type": signal_type,
            "setup_id": None if setup_id is None else str(setup_id),
            "status": str(raw.get("final_state") or raw.get("status") or "confirmed"),
            "entry_time": _iso(raw.get("momentum_confirmation_timestamp") or raw.get("timestamp")),
            "entry_price": _float(raw.get("confirmation_close") or raw.get("entry_price")),
            "invalidation_time": None,
            "invalidation_price": None,
            "reason": raw.get("confirmation_type"),
            "metadata_json": _clean(raw),
        }
        rows.append(row)
    seen: dict[str, int] = {}
    for row in rows:
        base = row["signal_key"]
        n = seen.get(base, 0)
        seen[base] = n + 1
        if n:
            row["signal_key"] = f"{base}|#{n}"
    rows.sort(key=lambda r: (r["timestamp"], r.get("direction") or "", r["signal_key"]))
    return rows


def hash_normalized_rows(rows: list[dict[str, Any]], *, key_field: str = "event_key") -> str:
    payload = [
        {k: row[k] for k in sorted(row) if k != "metadata_json"}
        | {"metadata_json": row.get("metadata_json")}
        for row in rows
    ]
    return json_hash(payload)


def compute_run_metrics(
    *,
    trend_states: list[dict[str, Any]],
    structure_events: list[dict[str, Any]],
    signals: list[dict[str, Any]],
    runtime_seconds: float,
) -> list[dict[str, Any]]:
    state_counts: dict[str, int] = {}
    changes = 0
    prev = None
    for row in trend_states:
        st = str(row.get("state") or "")
        state_counts[st] = state_counts.get(st, 0) + 1
        if prev is not None and prev != st:
            changes += 1
        prev = st

    def _bars_for(*states: str) -> int:
        return sum(state_counts.get(s, 0) for s in states)

    metrics = [
        ("trend_state_count", float(len(trend_states)), None),
        ("state_change_count", float(changes), None),
        ("structure_event_count", float(len(structure_events)), None),
        ("signal_count", float(len(signals)), None),
        ("uptrend_bars", float(_bars_for("uptrend", "early_uptrend")), None),
        ("downtrend_bars", float(_bars_for("downtrend", "early_downtrend")), None),
        ("range_bars", float(_bars_for("range", "neutral")), None),
        (
            "transition_bars",
            float(
                _bars_for(
                    "weakening_uptrend",
                    "weakening_downtrend",
                    "topping",
                    "bottoming",
                    "early_uptrend",
                    "early_downtrend",
                )
            ),
            None,
        ),
        ("runtime_seconds", float(runtime_seconds), None),
    ]
    return [
        {"metric_name": name, "metric_value": value, "metric_text": text}
        for name, value, text in metrics
    ]
