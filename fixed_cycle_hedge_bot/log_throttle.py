from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

DEFAULT_THROTTLE_INTERVAL_SEC = 60
MAX_THROTTLE_KEYS = 500

NEVER_SUPPRESS_EVENTS = frozenset(
    {
        "order_submitted",
        "intent_submit_started",
        "intent_submitted",
        "fill_received",
        "order_finalized",
        "confirmed_order_pnl_written",
        "fixed_cycle_recovery_reload_required",
        "fixed_cycle_recovery_pre_reload_cancel_completed",
        "fixed_cycle_recovery_reload_entry_submitted",
        "fixed_cycle_post_refill_structure_rebuild_completed",
        "fixed_cycle_normal_cycle_second_leg_split_created",
    }
)

NEVER_SUPPRESS_PREFIXES = (
    "fixed_cycle_recovery_wallet_transfer_",
)

DEBUG_ONLY_EVENTS = frozenset(
    {
        "fixed_cycle_downside_cycle_intent_build_attempt",
        "fixed_cycle_downside_cycle_intent_build_result",
        "fixed_cycle_time_distance_refill_cycle_resolved",
        "fixed_cycle_final_exit_purpose_excluded_from_cycle_normalization",
        "fixed_cycle_short_followup_entered_after_long_reduce_fill",
    }
)

THROTTLED_INFO_EVENTS = frozenset(
    {
        "fixed_cycle_time_distance_refill_already_triggered_skipped",
        "fixed_cycle_second_leg_pending_skip_rebuild",
        "fixed_cycle_exit_deferred_pending_second_pair_short_reduce",
        "fixed_cycle_short_tp_follow_up_skip",
    }
)

_SIGNATURE_FIELDS = (
    "symbol",
    "trade_block_id",
    "cycle_index",
    "reason",
    "next_required_purpose",
    "purpose",
    "active_cycle_index",
)

_MODULE_THROTTLE_BUCKETS: dict[str, dict[str, Any]] = {}


@dataclass(frozen=True)
class ThrottleDecision:
    should_log: bool
    suppressed_count: int
    throttle_interval_sec: int


def is_never_suppress_event(event_name: str) -> bool:
    if event_name in NEVER_SUPPRESS_EVENTS:
        return True
    return any(event_name.startswith(prefix) for prefix in NEVER_SUPPRESS_PREFIXES)


def is_debug_only_event(event_name: str) -> bool:
    return event_name in DEBUG_ONLY_EVENTS


def is_throttled_info_event(event_name: str) -> bool:
    return event_name in THROTTLED_INFO_EVENTS


def build_log_signature(event_name: str, payload: dict[str, Any]) -> str:
    parts = [str(event_name or "")]
    for field in _SIGNATURE_FIELDS:
        value = payload.get(field)
        if value is None and field == "active_cycle_index":
            before = payload.get("before")
            after = payload.get("after")
            if isinstance(before, dict) and before.get("active_cycle_index") is not None:
                value = before.get("active_cycle_index")
            elif isinstance(after, dict) and after.get("active_cycle_index") is not None:
                value = after.get("active_cycle_index")
        if value is not None and str(value) != "":
            parts.append(f"{field}={value}")
    return "|".join(parts)


def _prune_throttle_state(throttle_state: dict[str, Any]) -> None:
    if len(throttle_state) <= MAX_THROTTLE_KEYS:
        return
    sorted_keys = sorted(
        throttle_state.keys(),
        key=lambda key: float((throttle_state.get(key) or {}).get("last_seen_at") or 0.0),
    )
    for key in sorted_keys[: max(1, len(throttle_state) - MAX_THROTTLE_KEYS)]:
        throttle_state.pop(key, None)


def resolve_throttle_state(
    payload: dict[str, Any],
    strategy_state: dict[str, Any] | None,
) -> dict[str, Any]:
    if isinstance(strategy_state, dict):
        bucket = strategy_state.setdefault("_log_throttle_state", {})
        if isinstance(bucket, dict):
            return bucket
    bucket_key = str(
        payload.get("trade_block_id")
        or payload.get("bot_name")
        or payload.get("symbol")
        or "_global"
    )
    return _MODULE_THROTTLE_BUCKETS.setdefault(bucket_key, {})


def should_log_throttled_event(
    event_name: str,
    payload: dict[str, Any],
    throttle_state: dict[str, Any],
    now: float | None = None,
    interval_sec: int = DEFAULT_THROTTLE_INTERVAL_SEC,
) -> ThrottleDecision:
    if is_never_suppress_event(event_name):
        return ThrottleDecision(
            should_log=True,
            suppressed_count=0,
            throttle_interval_sec=interval_sec,
        )

    current_time = float(time.time() if now is None else now)
    signature = build_log_signature(event_name, payload)
    entry = throttle_state.get(event_name)
    if not isinstance(entry, dict) or entry.get("signature") != signature:
        throttle_state[event_name] = {
            "signature": signature,
            "last_logged_at": current_time,
            "last_seen_at": current_time,
            "suppressed_count": 0,
        }
        _prune_throttle_state(throttle_state)
        return ThrottleDecision(
            should_log=True,
            suppressed_count=0,
            throttle_interval_sec=interval_sec,
        )

    entry["last_seen_at"] = current_time
    elapsed = current_time - float(entry.get("last_logged_at") or 0.0)
    if elapsed < float(interval_sec):
        entry["suppressed_count"] = int(entry.get("suppressed_count") or 0) + 1
        _prune_throttle_state(throttle_state)
        return ThrottleDecision(
            should_log=False,
            suppressed_count=int(entry.get("suppressed_count") or 0),
            throttle_interval_sec=interval_sec,
        )

    suppressed_count = int(entry.get("suppressed_count") or 0)
    entry["last_logged_at"] = current_time
    entry["suppressed_count"] = 0
    _prune_throttle_state(throttle_state)
    return ThrottleDecision(
        should_log=True,
        suppressed_count=suppressed_count,
        throttle_interval_sec=interval_sec,
    )
