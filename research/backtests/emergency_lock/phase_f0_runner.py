"""Phase F0 runner: 2%%-leg speed audit on Phase-C emergency-lock events."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from research.backtests.candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol

from .config import EmergencyLockRecoveryConfig
from .event_finder import CrashEvent
from .phase_c_runner import phase_b_baseline_config
from .phase_d_runner import (
    load_oracle_flags,
    load_phase_c_events,
    run_signal_on_window,
)
from .phase_f0_outcomes import (
    build_recovery_attempts_for_crossing,
    first_touch_race,
    forward_outcomes_from_bar,
)
from .phase_f0_speed import (
    PhaseF0Config,
    build_leg_metrics,
    find_level_crossings,
    _ts_iso as _ts_iso_safe,
)

DEFAULT_PHASE_F0_OUTPUT_DIR = "research/backtests/results/emergency_lock/phase_f0"


def phase_f0_base_config(*, symbol: str = "APTUSDT") -> EmergencyLockRecoveryConfig:
    cfg = phase_b_baseline_config(symbol=symbol)
    cfg.output_dir = DEFAULT_PHASE_F0_OUTPUT_DIR
    return cfg


def _lock_context(
    window: list[dict[str, Any]], cfg: EmergencyLockRecoveryConfig
) -> dict[str, Any]:
    """Reuse Phase-D full-lock path to obtain short_avg_after_lock + lock offset."""
    result = run_signal_on_window(window, cfg, signal_name="full_lock_control")
    s = result["summary"]
    return {
        "lock_triggered": bool(s.get("lock_triggered")),
        "lock_offset": s.get("lock_offset"),
        "lock_timestamp": s.get("lock_timestamp"),
        "short_avg_after_lock": s.get("short_avg_after_lock"),
        "basket_pnl_at_lock": s.get("basket_pnl_at_lock"),
        "full_lock_short_qty": float(result["ledger"].short_qty)
        if s.get("lock_triggered")
        else None,
        "total_fees_at_lock": float(result["ledger"].total_fees)
        if s.get("lock_triggered")
        else None,
    }


def audit_event_window(
    window: list[dict[str, Any]],
    event: CrashEvent,
    cfg: EmergencyLockRecoveryConfig,
    f0: PhaseF0Config,
    *,
    oracle_possible: bool | None = None,
) -> dict[str, Any]:
    lock = _lock_context(window, cfg)
    if not lock["lock_triggered"] or lock["lock_offset"] is None:
        return {
            "event_id": event.event_id,
            "lock_triggered": False,
            "crossings": [],
            "crossings_close": [],
            "legs": [],
            "forward_outcomes": [],
            "first_touch": [],
            "recovery_attempts": [],
            "per_event": {
                "event_id": event.event_id,
                "lock_triggered": False,
                "final_status": "NO_EMERGENCY_TRIGGER",
            },
            "lock": lock,
        }

    lock_i = int(lock["lock_offset"])
    ref = float(lock["short_avg_after_lock"])
    end_i = len(window) - 1
    unlock_qty = float(lock["full_lock_short_qty"]) * float(f0.test_unlock_fraction)

    # 0% is the sequence origin at lock — not discovered by a later touch.
    down_only = tuple(p for p in f0.down_levels_pct if abs(float(p)) > 1e-15)
    origin = {
        "event_id": event.event_id,
        "touch_mode": "first_low_touch",
        "level_index": 0,
        "level_pct": 0.0,
        "level_price": ref,
        "start_timestamp": None,
        "end_timestamp": _ts_iso_safe(window[lock_i]["timestamp"]),
        "start_bar": lock_i,
        "end_bar": lock_i,
        "bars_needed": 0,
        "minutes_needed": 0.0,
        "hours_needed": 0.0,
        "sequence_start_timestamp": _ts_iso_safe(window[lock_i]["timestamp"]),
        "sequence_minutes_from_ref": 0.0,
        "actual_start_price": ref,
        "actual_end_price": ref,
        "candle_low": float(window[lock_i]["low"]),
        "candle_close": float(window[lock_i]["close"]),
        "previous_level_complete": True,
        "window_truncated_at_data_end": bool(event.window_truncated_at_data_end),
        "reference_price": ref,
    }
    crossings_down = find_level_crossings(
        window,
        reference_price=ref,
        levels_pct=down_only,
        start_index=lock_i,
        end_index=end_i,
        touch_mode="first_low_touch",
        event_id=event.event_id,
        window_truncated=bool(event.window_truncated_at_data_end),
    )
    crossings = [origin] + crossings_down
    crossings_close_down = find_level_crossings(
        window,
        reference_price=ref,
        levels_pct=down_only,
        start_index=lock_i,
        end_index=end_i,
        touch_mode="first_close_below",
        event_id=event.event_id,
        window_truncated=bool(event.window_truncated_at_data_end),
    )
    origin_close = dict(origin)
    origin_close["touch_mode"] = "first_close_below"
    crossings_close = [origin_close] + crossings_close_down
    # Annotate drop / oracle
    for row in crossings + crossings_close:
        row["max_drop_pct"] = event.max_drop_pct
        row["oracle_break_even_possible"] = oracle_possible
        row["drop_bucket"] = (
            ">=15%"
            if event.max_drop_pct >= 0.15
            else "12.5–15%"
            if event.max_drop_pct >= 0.125
            else "10–12.5%"
            if event.max_drop_pct >= 0.10
            else "<10%"
        )

    legs = build_leg_metrics(
        crossings, window, f0, event_id=event.event_id
    )
    for leg in legs:
        leg["max_drop_pct"] = event.max_drop_pct
        leg["oracle_break_even_possible"] = oracle_possible
        leg["drop_bucket"] = crossings[0]["drop_bucket"] if crossings else None

    # Map to_level_pct -> leg ending there
    leg_by_to = {float(lg["to_level_pct"]): lg for lg in legs}

    forward_rows: list[dict[str, Any]] = []
    race_rows: list[dict[str, Any]] = []
    recovery_rows: list[dict[str, Any]] = []

    ordered = sorted(crossings, key=lambda r: -float(r["level_pct"]))
    prev_price: float | None = None
    for cx in ordered:
        # Skip pure 0% reference crossing for recovery (no down move yet)
        if abs(float(cx["level_pct"])) < 1e-15:
            prev_price = float(cx["level_price"])
            continue
        entry_bar = int(cx["end_bar"])
        entry_price = float(cx["level_price"])
        fo = forward_outcomes_from_bar(
            window,
            entry_bar=entry_bar,
            entry_price=entry_price,
            horizons=f0.forward_horizons_bars,
            event_id=event.event_id,
            level_pct=float(cx["level_pct"]),
        )
        for r in fo:
            r["touch_mode"] = "first_low_touch"
            r["oracle_break_even_possible"] = oracle_possible
            r["drop_bucket"] = cx.get("drop_bucket")
        forward_rows.extend(fo)

        for tp, stop in f0.first_touch_races:
            race = first_touch_race(
                window,
                entry_bar=entry_bar,
                entry_price=entry_price,
                tp_pct=tp,
                stop_pct=stop,
                same_bar_policy=f0.same_bar_collision_policy,
                event_id=event.event_id,
                level_pct=float(cx["level_pct"]),
            )
            race["touch_mode"] = "first_low_touch"
            race["drop_bucket"] = cx.get("drop_bucket")
            race_rows.append(race)

        leg = leg_by_to.get(float(cx["level_pct"]))
        recovery_rows.extend(
            build_recovery_attempts_for_crossing(
                window,
                cx,
                leg,
                prev_price,
                f0,
                unlock_qty=unlock_qty,
            )
        )
        prev_price = float(cx["level_price"])

    per_event = {
        "event_id": event.event_id,
        "lock_triggered": True,
        "lock_timestamp": lock["lock_timestamp"],
        "lock_offset": lock_i,
        "short_avg_after_lock": ref,
        "basket_pnl_at_lock": lock["basket_pnl_at_lock"],
        "full_lock_short_qty": lock["full_lock_short_qty"],
        "max_drop_pct": event.max_drop_pct,
        "oracle_break_even_possible": oracle_possible,
        "levels_reached_count": len(
            [c for c in crossings if abs(float(c["level_pct"])) > 1e-15]
        ),
        "legs_count": len(legs),
        "window_truncated_at_data_end": event.window_truncated_at_data_end,
        "median_leg_minutes": None,
        "fastest_leg_minutes": None,
        "slowest_leg_minutes": None,
    }
    if legs:
        mins = [float(lg["minutes_for_leg"]) for lg in legs]
        mins_sorted = sorted(mins)
        mid = len(mins_sorted) // 2
        per_event["median_leg_minutes"] = (
            mins_sorted[mid]
            if len(mins_sorted) % 2
            else 0.5 * (mins_sorted[mid - 1] + mins_sorted[mid])
        )
        per_event["fastest_leg_minutes"] = min(mins)
        per_event["slowest_leg_minutes"] = max(mins)

    return {
        "event_id": event.event_id,
        "lock_triggered": True,
        "crossings": crossings,
        "crossings_close": crossings_close,
        "legs": legs,
        "forward_outcomes": forward_rows,
        "first_touch": race_rows,
        "recovery_attempts": recovery_rows,
        "per_event": per_event,
        "lock": lock,
        "window": window,
    }


def run_phase_f0(
    *,
    candles: Sequence[dict[str, Any]] | None = None,
    events: Sequence[CrashEvent] | None = None,
    cfg: EmergencyLockRecoveryConfig | None = None,
    f0_cfg: PhaseF0Config | None = None,
) -> dict[str, Any]:
    base = cfg or phase_f0_base_config()
    f0 = f0_cfg or PhaseF0Config(
        fee_rate=float(base.fee_rate),
        slippage_bps=float(base.slippage_bps),
        long_notional_usdt=float(base.initial_long_notional_usdt),
        # Recovery notional uses full-lock short qty from simulation.
        short_notional_usdt=float(base.initial_long_notional_usdt),
    )
    rows = (
        list(candles)
        if candles is not None
        else load_candles_for_symbol(
            symbol=base.symbol,
            timeframe=base.timeframe,
            data_dir=DEFAULT_DATA_DIR,
            limit=None,
        )
    )
    evs = list(events) if events is not None else load_phase_c_events()
    oracle = load_oracle_flags()

    crossings: list[dict[str, Any]] = []
    crossings_close: list[dict[str, Any]] = []
    legs: list[dict[str, Any]] = []
    forwards: list[dict[str, Any]] = []
    races: list[dict[str, Any]] = []
    recoveries: list[dict[str, Any]] = []
    per_event: list[dict[str, Any]] = []
    event_results: dict[str, dict[str, Any]] = {}

    for event in evs:
        window = list(
            rows[event.simulation_start_index : event.simulation_end_index + 1]
        )
        out = audit_event_window(
            window,
            event,
            base,
            f0,
            oracle_possible=oracle.get(event.event_id),
        )
        event_results[event.event_id] = out
        crossings.extend(out["crossings"])
        crossings_close.extend(out["crossings_close"])
        legs.extend(out["legs"])
        forwards.extend(out["forward_outcomes"])
        races.extend(out["first_touch"])
        recoveries.extend(out["recovery_attempts"])
        per_event.append(out["per_event"])

    return {
        "candles": rows,
        "events": evs,
        "config": base,
        "f0_config": f0,
        "crossings": crossings,
        "crossings_close": crossings_close,
        "legs": legs,
        "forward_outcomes": forwards,
        "first_touch": races,
        "recovery_attempts": recoveries,
        "per_event_rows": per_event,
        "event_results": event_results,
        "all_history_implemented": False,
        "all_history_skip_reason": (
            "Deferred: primary 14-event audit is complete; a clean "
            "non-overlapping all-history sequence detector would require a "
            "separate event definition and was not needed for F0 decision."
        ),
    }
