"""Historical pipeline audit: RegimeSnapshot → Setup → PriceAction → Momentum.

Research-only candle walk. No entry or TP.
Price Action / Momentum use **5m** only; 15m/30m enter solely via RegimeSnapshot /
SetupActivation context.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import ProcessPoolExecutor
from copy import deepcopy
from pathlib import Path
from typing import Any

import pandas as pd

from .config import RegimeScannerConfig, default_regime_scanner_config
from .data_loader import load_symbol_candles
from .indicators import atr_wilder
from .momentum import MomentumConfig, default_momentum_config
from .momentum_audit import run_momentum_audit, write_momentum_audit_outputs
from .point_audit import build_point_audit, json_safe
from .price_action import (
    PriceActionConfig,
    confirmed_pivot_to_swing,
    default_price_action_config,
    evaluate_price_action_confirmation,
    filter_swings_as_of,
    initialize_price_action_state,
    sort_swings,
    swing_key,
    update_price_action_state,
)
from .regime_snapshot import (
    build_regime_snapshot_from_point_audit,
    evaluate_setup_activation,
)
from .signal_tp_audit import decision_time_for_index, prepare_candle_window
from .swings import find_confirmed_pivots
from .timeframes import parse_timeframes, required_5m_history_candles

_WORKER_CANDLES: pd.DataFrame | None = None
_WORKER_CFG: dict[str, Any] | None = None


def _init_worker(candles_payload: dict[str, Any], worker_cfg: dict[str, Any]) -> None:
    global _WORKER_CANDLES, _WORKER_CFG
    frame = pd.DataFrame(candles_payload)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    _WORKER_CANDLES = frame
    _WORKER_CFG = worker_cfg


def _worker_snapshot_at_index(index: int) -> tuple[int, dict[str, Any]]:
    assert _WORKER_CANDLES is not None and _WORKER_CFG is not None
    candles = _WORKER_CANDLES
    cfg = default_regime_scanner_config()
    requested = parse_timeframes(_WORKER_CFG["timeframes"])
    need = required_5m_history_candles(_WORKER_CFG["history_candles"], requested)
    decision = decision_time_for_index(candles, index)
    start = max(0, int(index) + 1 - int(need))
    window = candles.iloc[start : int(index) + 1].reset_index(drop=True)
    audit = build_point_audit(
        symbol=_WORKER_CFG["symbol"],
        decision_time=decision,
        candles=window,
        history_candles=_WORKER_CFG["history_candles"],
        timeframes=requested,
        config=cfg,
        include_setup_activation=False,
    )
    snapshot = build_regime_snapshot_from_point_audit(audit)
    return int(index), snapshot


def _ts(value: object) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    return float(pd.Series(values).median())


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return float(pd.Series(values).mean())


def _candles_between(start_ts: object, end_ts: object) -> int | None:
    if start_ts is None or end_ts is None:
        return None
    delta = _ts(end_ts) - _ts(start_ts)
    minutes = delta.total_seconds() / 60.0
    if minutes < 0:
        return None
    return int(round(minutes / 5.0))


def _flatten_swing(prefix: str, swing: dict[str, Any] | None) -> dict[str, Any]:
    if not swing:
        return {
            f"{prefix}_side": None,
            f"{prefix}_price": None,
            f"{prefix}_pivot_timestamp": None,
            f"{prefix}_confirmation_timestamp": None,
            f"{prefix}_pivot_index": None,
        }
    return {
        f"{prefix}_side": swing.get("side"),
        f"{prefix}_price": swing.get("price"),
        f"{prefix}_pivot_timestamp": swing.get("pivot_timestamp"),
        f"{prefix}_confirmation_timestamp": swing.get("confirmation_timestamp"),
        f"{prefix}_pivot_index": swing.get("pivot_index"),
    }


def precompute_5m_swings(
    candles: pd.DataFrame,
    *,
    pa_config: PriceActionConfig,
) -> list[dict[str, Any]]:
    """All confirmed 5m swings on the closed frame (feed only as-of confirmation)."""
    pivots = find_confirmed_pivots(
        candles,
        pivot_left=pa_config.pivot_left,
        pivot_right=pa_config.pivot_right,
    )
    return sort_swings(
        [
            confirmed_pivot_to_swing(p, source_timeframe="5m")
            for p in pivots
        ]
    )


def run_pipeline_audit(
    *,
    symbol: str = "APTUSDT",
    start: str = "2026-03-01",
    end: str = "2026-03-08",
    timeframes: str = "5m,15m,30m",
    history_candles: int = 144,
    workers: int = 4,
    progress_every: int = 200,
    prefetch_batch_size: int = 32,
    pa_config: PriceActionConfig | None = None,
    momentum_config: MomentumConfig | None = None,
    enable_momentum: bool = True,
    data_dir: str | Path | None = None,
    scanner_config: RegimeScannerConfig | None = None,
) -> dict[str, Any]:
    """Causal week walk: snapshot → setup → PA confirmation → optional Momentum."""
    t0 = time.perf_counter()
    cfg = scanner_config or default_regime_scanner_config()
    pa_cfg = pa_config or default_price_action_config()
    candles_raw = load_symbol_candles(symbol, data_dir=data_dir)
    prepared = prepare_candle_window(
        candles_raw,
        start=start,
        end=end,
        history_candles=history_candles,
        timeframes=timeframes,
    )
    frame: pd.DataFrame = prepared["candles"]
    start_index = int(prepared["signal_start_index"])
    n = len(frame)
    all_swings = precompute_5m_swings(frame, pa_config=pa_cfg)
    atr_series = atr_wilder(
        pd.to_numeric(frame["high"], errors="coerce"),
        pd.to_numeric(frame["low"], errors="coerce"),
        pd.to_numeric(frame["close"], errors="coerce"),
        int(getattr(cfg, "atr_period", 14) or 14),
    )

    # Prefetch regime snapshots for the signal window.
    snapshot_cache: dict[int, dict[str, Any]] = {}
    use_pool = int(workers) > 1
    executor: ProcessPoolExecutor | None = None
    if use_pool:
        payload = {
            col: frame[col].to_numpy()
            for col in ("timestamp", "open", "high", "low", "close", "volume")
        }
        payload["timestamp"] = frame["timestamp"].astype(str).to_numpy()
        worker_cfg = {
            "symbol": str(symbol).upper(),
            "timeframes": timeframes,
            "history_candles": int(history_candles),
        }
        executor = ProcessPoolExecutor(
            max_workers=int(workers),
            initializer=_init_worker,
            initargs=(payload, worker_cfg),
        )

    def fetch_missing(indices: list[int]) -> None:
        missing = [i for i in indices if i not in snapshot_cache]
        if not missing:
            return
        if executor is None:
            requested = parse_timeframes(timeframes)
            need = required_5m_history_candles(history_candles, requested)
            for idx in missing:
                decision = decision_time_for_index(frame, idx)
                start_i = max(0, int(idx) + 1 - int(need))
                window = frame.iloc[start_i : int(idx) + 1].reset_index(drop=True)
                audit = build_point_audit(
                    symbol=symbol,
                    decision_time=decision,
                    candles=window,
                    history_candles=history_candles,
                    timeframes=requested,
                    config=cfg,
                    include_setup_activation=False,
                )
                snapshot_cache[idx] = build_regime_snapshot_from_point_audit(audit)
            return
        for idx, snap in executor.map(_worker_snapshot_at_index, missing, chunksize=4):
            snapshot_cache[idx] = snap

    snapshot_rows: list[dict[str, Any]] = []
    setup_rows: list[dict[str, Any]] = []
    event_rows: list[dict[str, Any]] = []
    confirmation_rows: list[dict[str, Any]] = []
    detail_cases: dict[str, Any] = {
        "confirmed_structure": None,
        "invalidated_setup": None,
        "expired_or_unconfirmed_setup": None,
    }

    # Active PA states: at most one per side.
    active: dict[str, dict[str, Any]] = {}
    previous_combined: str | None = None
    max_concurrent = 0
    duplicate_confirmations = 0
    duplicate_swing_feeds = 0
    seen_confirmation_ids: set[str] = set()
    setup_seq = 0

    # Tracking for timing metrics.
    armed_latencies: list[float] = []
    confirm_latencies: list[float] = []
    setup_outcomes: dict[str, dict[str, Any]] = {}

    try:
        index = start_index
        batch = max(1, int(prefetch_batch_size))
        while index < n:
            batch_end = min(n, index + batch)
            fetch_missing(list(range(index, batch_end)))
            if progress_every and index % int(progress_every) < batch:
                print(
                    f"pipeline progress index={index}/{n} "
                    f"setups={len(setup_rows)} confirmations={len(confirmation_rows)} "
                    f"active={list(active.keys())}",
                    flush=True,
                )

            for cursor in range(index, batch_end):
                base_snap = snapshot_cache[cursor]
                # Attach previous for regime_change detection.
                snapshot = deepcopy(base_snap)
                snapshot["previous_combined_regime"] = previous_combined
                snapshot["regime_change"] = bool(
                    previous_combined is not None
                    and previous_combined != snapshot.get("combined_regime")
                )
                setup = evaluate_setup_activation(snapshot)
                decision_ts = snapshot.get("decision_time")
                candle_open = frame.iloc[cursor]
                # Closed candle for PA = the bar that just closed at decision_time.
                # candle timestamp is typically the open time of that closed bar.
                atr_raw = atr_series.iloc[cursor]
                atr_val = None
                try:
                    if atr_raw == atr_raw:  # not NaN
                        atr_f = float(atr_raw)
                        if atr_f > 0:
                            atr_val = atr_f
                except (TypeError, ValueError):
                    atr_val = None
                closed_candle = {
                    "timestamp": candle_open["timestamp"],
                    "open": float(candle_open["open"]),
                    "high": float(candle_open["high"]),
                    "low": float(candle_open["low"]),
                    "close": float(candle_open["close"]),
                    "volume": float(candle_open.get("volume") or 0.0),
                    "candle_index": int(cursor),
                    "atr": atr_val,
                }
                # Swings confirmed by the close of this bar:
                # confirmation_timestamp == candle timestamp (pivot confirm bar).
                candle_ts = _ts(closed_candle["timestamp"])
                usable = filter_swings_as_of(all_swings, candle_ts)

                snapshot_rows.append(
                    {
                        "index": cursor,
                        "decision_time": decision_ts,
                        "candle_timestamp": str(candle_ts.isoformat()),
                        "regime_5m": snapshot.get("regime_5m"),
                        "regime_15m": snapshot.get("regime_15m"),
                        "regime_30m": snapshot.get("regime_30m"),
                        "combined_regime": snapshot.get("combined_regime"),
                        "previous_combined_regime": previous_combined,
                        "regime_change": snapshot.get("regime_change"),
                        "trend_direction": snapshot.get("trend_direction"),
                        "trend_strength": snapshot.get("trend_strength"),
                        "trend_weakness": snapshot.get("trend_weakness"),
                        "transition_detected": snapshot.get("transition_detected"),
                        "setup_activated": bool(setup.get("setup_activated")),
                        "setup_side": setup.get("setup_side"),
                        "setup_type": setup.get("setup_type"),
                    }
                )

                # --- Update existing PA states with this closed candle ---
                opposing_for: dict[str, dict[str, Any]] = {}
                if setup.get("setup_activated") and setup.get("setup_side") in {"long", "short"}:
                    side = str(setup["setup_side"])
                    other = "long" if side == "short" else "short"
                    opposing_for[other] = setup

                finished_sides: list[str] = []
                for side, state in list(active.items()):
                    before_keys = {
                        tuple(k) for k in (state.get("processed_swing_keys") or [])
                    }
                    newly = []
                    for s in usable:
                        key = swing_key(s)
                        if key not in before_keys:
                            newly.append(s)
                    # Detect duplicate feed attempts of already processed keys
                    for s in newly:
                        pass
                    prev_event_n = len(state.get("event_log") or [])
                    updated = update_price_action_state(
                        state,
                        closed_candle,
                        newly,
                        opposing_setup=opposing_for.get(side),
                    )
                    # Count duplicate swing keys already processed
                    after_keys = {
                        tuple(k) for k in (updated.get("processed_swing_keys") or [])
                    }
                    if len(newly) != len({swing_key(s) for s in newly}):
                        duplicate_swing_feeds += 1
                    fed_again = [s for s in newly if swing_key(s) in before_keys]
                    if fed_again:
                        duplicate_swing_feeds += len(fed_again)

                    for ev in (updated.get("event_log") or [])[prev_event_n:]:
                        event_rows.append(
                            {
                                "setup_id": updated.get("setup_id"),
                                "setup_side": updated.get("setup_side"),
                                "setup_type": updated.get("setup_type"),
                                "event": ev.get("event"),
                                "timestamp": ev.get("timestamp") or decision_ts,
                                "state": ev.get("state") or updated.get("state"),
                                "pattern_type": ev.get("pattern_type")
                                or updated.get("pattern_type"),
                                "reason": ev.get("reason"),
                                "confirmation_level": ev.get("confirmation_level")
                                or updated.get("confirmation_level"),
                                "invalidation_level": updated.get("invalidation_level"),
                                "structure_armed_timestamp": updated.get(
                                    "structure_armed_timestamp"
                                ),
                                "structure_armed_candle_index": updated.get(
                                    "structure_armed_candle_index"
                                ),
                                "same_bar_confirmation_blocked": updated.get(
                                    "same_bar_confirmation_blocked"
                                ),
                                "invalid_structure_geometry": updated.get(
                                    "invalid_structure_geometry"
                                ),
                                "waiting_for_confirmation_level": updated.get(
                                    "waiting_for_confirmation_level"
                                ),
                                "failed_break_invalidation_buffer": updated.get(
                                    "failed_break_invalidation_buffer"
                                ),
                                "unbuffered_failed_break_extreme": updated.get(
                                    "unbuffered_failed_break_extreme"
                                ),
                                "final_invalidation_level": updated.get(
                                    "final_invalidation_level"
                                ),
                            }
                        )
                        sid = str(updated.get("setup_id"))
                        meta = setup_outcomes.setdefault(sid, {})
                        if ev.get("event") == "structure_armed":
                            if meta.get("armed_ts") is None:
                                meta["armed_ts"] = updated.get("last_updated_timestamp")
                                lat = _candles_between(
                                    updated.get("setup_activation_timestamp"),
                                    meta["armed_ts"],
                                )
                                if lat is not None:
                                    armed_latencies.append(float(lat))
                                    meta["candles_to_armed"] = lat
                        if ev.get("event") == "same_bar_confirmation_blocked":
                            meta["same_bar_blocked"] = True
                        if ev.get("event") == "structure_geometry_invalid":
                            meta["invalid_structure_geometry"] = True
                        if ev.get("event") == "waiting_for_confirmation_level":
                            meta["waiting_for_confirmation_level"] = True

                    conf = evaluate_price_action_confirmation(updated)
                    if conf is not None:
                        conf_id = (
                            f"{conf.get('setup_activation_timestamp')}|"
                            f"{conf.get('structure_break_timestamp')}|"
                            f"{conf.get('side')}|"
                            f"{conf.get('pattern_type')}"
                        )
                        if conf_id in seen_confirmation_ids:
                            duplicate_confirmations += 1
                        else:
                            seen_confirmation_ids.add(conf_id)
                            lat = _candles_between(
                                conf.get("setup_activation_timestamp"),
                                conf.get("structure_break_timestamp"),
                            )
                            if lat is not None:
                                confirm_latencies.append(float(lat))
                            sid = str(updated.get("setup_id"))
                            meta = setup_outcomes.setdefault(sid, {})
                            row = {
                                "setup_id": updated.get("setup_id"),
                                "side": conf.get("side"),
                                "setup_type": updated.get("setup_type"),
                                "setup_activation_timestamp": conf.get(
                                    "setup_activation_timestamp"
                                ),
                                "pattern_type": conf.get("pattern_type"),
                                "confirmation_level": conf.get("confirmation_level"),
                                "invalidation_level": conf.get("invalidation_level"),
                                "final_invalidation_level": conf.get(
                                    "final_invalidation_level"
                                )
                                or updated.get("final_invalidation_level"),
                                "structure_detection_timestamp": conf.get(
                                    "structure_detection_timestamp"
                                ),
                                "structure_armed_timestamp": conf.get(
                                    "structure_armed_timestamp"
                                )
                                or updated.get("structure_armed_timestamp"),
                                "structure_armed_candle_index": conf.get(
                                    "structure_armed_candle_index"
                                )
                                or updated.get("structure_armed_candle_index"),
                                "structure_break_timestamp": conf.get(
                                    "structure_break_timestamp"
                                ),
                                "same_bar_confirmation_blocked": bool(
                                    conf.get("same_bar_confirmation_blocked")
                                    or meta.get("same_bar_blocked")
                                ),
                                "failed_break_invalidation_buffer": conf.get(
                                    "failed_break_invalidation_buffer"
                                )
                                or updated.get("failed_break_invalidation_buffer"),
                                "unbuffered_failed_break_extreme": conf.get(
                                    "unbuffered_failed_break_extreme"
                                )
                                or updated.get("unbuffered_failed_break_extreme"),
                                "candles_from_setup_to_confirmation": lat,
                                "regime_5m": (updated.get("source_setup_activation") or {})
                                .get("source_snapshot", {})
                                .get("regime_5m"),
                                "regime_15m": (updated.get("source_setup_activation") or {})
                                .get("source_snapshot", {})
                                .get("regime_15m"),
                                "regime_30m": (updated.get("source_setup_activation") or {})
                                .get("source_snapshot", {})
                                .get("regime_30m"),
                                "combined_regime": (updated.get("source_setup_activation") or {})
                                .get("source_snapshot", {})
                                .get("combined_regime"),
                                "warnings": list(
                                    (updated.get("source_setup_activation") or {}).get(
                                        "warnings"
                                    )
                                    or []
                                ),
                                "blockers": list(
                                    (updated.get("source_setup_activation") or {}).get(
                                        "blockers"
                                    )
                                    or []
                                ),
                                **_flatten_swing("reference", conf.get("reference_swing")),
                                **_flatten_swing("candidate", conf.get("candidate_swing")),
                                **_flatten_swing(
                                    "intermediate", conf.get("intermediate_swing")
                                ),
                            }
                            confirmation_rows.append(row)
                            if detail_cases["confirmed_structure"] is None:
                                detail_cases["confirmed_structure"] = {
                                    "confirmation": row,
                                    "final_state": updated.get("state"),
                                    "pattern_type": conf.get("pattern_type"),
                                }
                            sid = str(updated.get("setup_id"))
                            setup_outcomes.setdefault(sid, {})["confirmed"] = True

                    active[side] = updated
                    if updated.get("state") in {
                        "invalidated",
                        "expired",
                        "price_action_confirmed",
                    }:
                        finished_sides.append(side)
                        sid = str(updated.get("setup_id"))
                        meta = setup_outcomes.setdefault(sid, {})
                        meta["terminal_state"] = updated.get("state")
                        meta["invalidation_reason"] = updated.get("invalidation_reason")
                        if (
                            updated.get("state") == "invalidated"
                            and detail_cases["invalidated_setup"] is None
                        ):
                            detail_cases["invalidated_setup"] = {
                                "setup_id": sid,
                                "side": side,
                                "reason": updated.get("invalidation_reason"),
                                "setup_activation_timestamp": updated.get(
                                    "setup_activation_timestamp"
                                ),
                                "pattern_type": updated.get("pattern_type"),
                                "age_candles": updated.get("age_candles"),
                            }
                        if (
                            updated.get("state") == "expired"
                            and detail_cases["expired_or_unconfirmed_setup"] is None
                        ):
                            detail_cases["expired_or_unconfirmed_setup"] = {
                                "setup_id": sid,
                                "side": side,
                                "reason": updated.get("invalidation_reason"),
                                "setup_activation_timestamp": updated.get(
                                    "setup_activation_timestamp"
                                ),
                                "last_state_before": "expired",
                                "age_candles": updated.get("age_candles"),
                                "structure_confirmed": updated.get(
                                    "structure_confirmed"
                                ),
                            }

                for side in finished_sides:
                    active.pop(side, None)

                # --- New setup activation handling ---
                # Policy (research v1):
                # * At most one active PA state per side.
                # * continuation_weakness is level-triggered in Phase-1; here we only
                #   START a new PA when that side has no non-terminal active state
                #   (ignore repeated same-side activations while PA is in flight).
                # * Opposing setup invalidates the other side via update() above.
                # * Same-side replace only when setup_type changes while active.
                if setup.get("setup_activated") and setup.get("setup_side") in {
                    "long",
                    "short",
                }:
                    side = str(setup["setup_side"])
                    setup_type = str(setup.get("setup_type") or "")
                    existing = active.get(side)
                    should_start = False
                    replace_reason = None
                    if existing is None:
                        should_start = True
                    elif existing.get("state") in {
                        "invalidated",
                        "expired",
                        "price_action_confirmed",
                    }:
                        should_start = True
                        active.pop(side, None)
                    elif str(existing.get("setup_type") or "") != setup_type:
                        should_start = True
                        replace_reason = "SAME_SIDE_TYPE_CHANGED"
                    # else: ignore repeated same-side activation while PA active

                    if should_start:
                        setup_seq += 1
                        setup_id = f"setup_{setup_seq:05d}"
                        if replace_reason and existing is not None:
                            old = update_price_action_state(
                                existing,
                                closed_candle,
                                [],
                                regime_invalidation=replace_reason,
                            )
                            event_rows.append(
                                {
                                    "setup_id": old.get("setup_id"),
                                    "setup_side": old.get("setup_side"),
                                    "setup_type": old.get("setup_type"),
                                    "event": "invalidated",
                                    "timestamp": decision_ts,
                                    "state": old.get("state"),
                                    "pattern_type": old.get("pattern_type"),
                                    "reason": replace_reason,
                                    "confirmation_level": old.get("confirmation_level"),
                                }
                            )
                            if detail_cases["invalidated_setup"] is None:
                                detail_cases["invalidated_setup"] = {
                                    "setup_id": old.get("setup_id"),
                                    "side": side,
                                    "reason": replace_reason,
                                    "setup_activation_timestamp": old.get(
                                        "setup_activation_timestamp"
                                    ),
                                }
                            active.pop(side, None)

                        initial_swings = filter_swings_as_of(all_swings, decision_ts)
                        setup_payload = deepcopy(setup)
                        setup_payload["setup_activation_timestamp"] = decision_ts
                        setup_payload["setup_id"] = setup_id
                        state = initialize_price_action_state(
                            setup_payload,
                            pa_cfg,
                            confirmed_swings_as_of_setup=initial_swings,
                        )
                        state["setup_id"] = setup_id
                        setup_rows.append(
                            {
                                "setup_id": setup_id,
                                "setup_activated": True,
                                "setup_side": setup.get("setup_side"),
                                "setup_type": setup.get("setup_type"),
                                "setup_activation_timestamp": decision_ts,
                                "activating_regime": setup.get("activating_regime"),
                                "previous_regime": setup.get("previous_regime"),
                                "confidence": setup.get("confidence"),
                                "warnings": list(setup.get("warnings") or []),
                                "blockers": list(setup.get("blockers") or []),
                                "regime_5m": snapshot.get("regime_5m"),
                                "regime_15m": snapshot.get("regime_15m"),
                                "regime_30m": snapshot.get("regime_30m"),
                                "combined_regime": snapshot.get("combined_regime"),
                                "reference_swing_missing": "REFERENCE_SWING_MISSING"
                                in (state.get("warnings") or []),
                                "initial_state": state.get("state"),
                                "intake_policy": replace_reason or "start_idle_side",
                            }
                        )
                        setup_outcomes[setup_id] = {
                            "side": side,
                            "setup_type": setup.get("setup_type"),
                            "activation_ts": decision_ts,
                            "confirmed": False,
                        }
                        for ev in state.get("event_log") or []:
                            event_rows.append(
                                {
                                    "setup_id": setup_id,
                                    "setup_side": side,
                                    "setup_type": setup.get("setup_type"),
                                    "event": ev.get("event"),
                                    "timestamp": ev.get("timestamp") or decision_ts,
                                    "state": ev.get("state") or state.get("state"),
                                    "pattern_type": ev.get("pattern_type"),
                                    "reason": ev.get("reason"),
                                    "confirmation_level": None,
                                }
                            )
                        if state.get("state") not in {"invalidated", "expired"}:
                            active[side] = state
                        elif detail_cases["invalidated_setup"] is None:
                            detail_cases["invalidated_setup"] = {
                                "setup_id": setup_id,
                                "side": side,
                                "reason": state.get("invalidation_reason"),
                                "setup_activation_timestamp": decision_ts,
                            }

                max_concurrent = max(max_concurrent, len(active))
                previous_combined = str(snapshot.get("combined_regime") or "") or None

            index = batch_end
    finally:
        if executor is not None:
            executor.shutdown(wait=True)

    # Capture an unconfirmed still-active or never-confirmed setup for details.
    if detail_cases["expired_or_unconfirmed_setup"] is None:
        for sid, meta in setup_outcomes.items():
            if not meta.get("confirmed") and meta.get("terminal_state") != "price_action_confirmed":
                detail_cases["expired_or_unconfirmed_setup"] = {
                    "setup_id": sid,
                    "side": meta.get("side"),
                    "setup_type": meta.get("setup_type"),
                    "setup_activation_timestamp": meta.get("activation_ts"),
                    "terminal_state": meta.get("terminal_state") or "still_active_or_replaced",
                    "armed_ts": meta.get("armed_ts"),
                    "candles_to_armed": meta.get("candles_to_armed"),
                }
                break
        if detail_cases["expired_or_unconfirmed_setup"] is None and active:
            side, state = next(iter(active.items()))
            detail_cases["expired_or_unconfirmed_setup"] = {
                "setup_id": state.get("setup_id"),
                "side": side,
                "setup_activation_timestamp": state.get("setup_activation_timestamp"),
                "terminal_state": state.get("state"),
                "age_candles": state.get("age_candles"),
                "structure_confirmed": state.get("structure_confirmed"),
                "pattern_type": state.get("pattern_type"),
                "note": "still active at end of window",
            }

    summary = build_pipeline_summary(
        snapshot_rows=snapshot_rows,
        setup_rows=setup_rows,
        event_rows=event_rows,
        confirmation_rows=confirmation_rows,
        armed_latencies=armed_latencies,
        confirm_latencies=confirm_latencies,
        max_concurrent=max_concurrent,
        duplicate_confirmations=duplicate_confirmations,
        duplicate_swing_feeds=duplicate_swing_feeds,
        detail_cases=detail_cases,
        symbol=symbol,
        start=prepared.get("start"),
        end=prepared.get("end"),
        elapsed_seconds=time.perf_counter() - t0,
        pa_config=pa_cfg,
        timeframes=timeframes,
    )
    result: dict[str, Any] = {
        "summary": summary,
        "regime_snapshots": snapshot_rows,
        "setup_activations": setup_rows,
        "price_action_events": event_rows,
        "price_action_confirmations": confirmation_rows,
        "detail_cases": detail_cases,
        "candles": frame,
    }

    if enable_momentum:
        mom_cfg = momentum_config or default_momentum_config()
        momentum_payload = run_momentum_audit(
            price_action_confirmations=confirmation_rows,
            candles=frame,
            momentum_config=mom_cfg,
            setup_activations=setup_rows,
            atr_period=int(getattr(cfg, "atr_period", 14) or 14),
        )
        result["momentum"] = momentum_payload
        summary["momentum"] = momentum_payload.get("summary")
    return result


def build_pipeline_summary(
    *,
    snapshot_rows: list[dict[str, Any]],
    setup_rows: list[dict[str, Any]],
    event_rows: list[dict[str, Any]],
    confirmation_rows: list[dict[str, Any]],
    armed_latencies: list[float],
    confirm_latencies: list[float],
    max_concurrent: int,
    duplicate_confirmations: int,
    duplicate_swing_feeds: int,
    detail_cases: dict[str, Any],
    symbol: str,
    start: object,
    end: object,
    elapsed_seconds: float,
    pa_config: PriceActionConfig,
    timeframes: str,
) -> dict[str, Any]:
    setups_n = len(setup_rows)
    conf_n = len(confirmation_rows)
    by_side: dict[str, int] = {"long": 0, "short": 0}
    by_type: dict[str, int] = {}
    for row in setup_rows:
        side = str(row.get("setup_side") or "")
        if side in by_side:
            by_side[side] += 1
        st = str(row.get("setup_type") or "unknown")
        by_type[st] = by_type.get(st, 0) + 1

    conf_by_side = {"long": 0, "short": 0}
    pattern_counts = {
        "lower_high": 0,
        "higher_low": 0,
        "failed_breakout": 0,
        "failed_breakdown": 0,
    }
    for row in confirmation_rows:
        side = str(row.get("side") or "")
        if side in conf_by_side:
            conf_by_side[side] += 1
        pt = str(row.get("pattern_type") or "")
        if pt in pattern_counts:
            pattern_counts[pt] += 1

    def _quote(side: str) -> float | None:
        denom = by_side.get(side) or 0
        if denom <= 0:
            return None
        return float(conf_by_side.get(side, 0) / denom)

    invalidated = sum(1 for e in event_rows if e.get("event") == "invalidated")
    expired = sum(1 for e in event_rows if e.get("event") == "expired")
    ref_missing = sum(1 for r in setup_rows if r.get("reference_swing_missing"))
    htf_transition = sum(
        1
        for r in setup_rows
        if "HTF_TRANSITION" in (r.get("warnings") or [])
    )
    htf_opposing = sum(
        1
        for r in setup_rows
        if "HTF_OPPOSING_TREND" in (r.get("blockers") or [])
    )

    # Confirmations that never logged structure_armed for their setup_id
    armed_setups = {
        str(e.get("setup_id"))
        for e in event_rows
        if e.get("event") == "structure_armed" and e.get("setup_id") is not None
    }
    confirmations_without_arming = sum(
        1
        for row in confirmation_rows
        if str(row.get("setup_id")) not in armed_setups
    )

    same_bar_blocked_events = sum(
        1 for e in event_rows if e.get("event") == "same_bar_confirmation_blocked"
    )
    same_bar_blocked_setups = {
        str(e.get("setup_id"))
        for e in event_rows
        if e.get("event") == "same_bar_confirmation_blocked" and e.get("setup_id") is not None
    }
    later_confirmed_after_same_bar_block = sum(
        1
        for row in confirmation_rows
        if str(row.get("setup_id")) in same_bar_blocked_setups
        or row.get("same_bar_confirmation_blocked")
    )
    invalid_structure_geometry_count = sum(
        1 for e in event_rows if e.get("event") == "structure_geometry_invalid"
    )
    waiting_for_confirmation_level_count = sum(
        1 for e in event_rows if e.get("event") == "waiting_for_confirmation_level"
    )

    def _pattern_lifecycle(pattern: str) -> dict[str, int]:
        armed = sum(
            1
            for e in event_rows
            if e.get("event") == "structure_armed" and e.get("pattern_type") == pattern
        )
        confirmed = sum(
            1 for row in confirmation_rows if row.get("pattern_type") == pattern
        )
        invalidated = sum(
            1
            for e in event_rows
            if e.get("event") == "invalidated" and e.get("pattern_type") == pattern
        )
        return {
            "armed": int(armed),
            "confirmed": int(confirmed),
            "invalidated": int(invalidated),
        }

    return {
        "symbol": str(symbol).upper(),
        "start": start,
        "end": end,
        "timeframes_regime": timeframes,
        "price_action_timeframe": "5m",
        "elapsed_seconds": float(elapsed_seconds),
        "regime_snapshots": len(snapshot_rows),
        "setup_activations": setups_n,
        "setups_by_side": by_side,
        "setups_by_type": by_type,
        "price_action_confirmations": conf_n,
        "confirmation_rate_overall": (float(conf_n / setups_n) if setups_n else None),
        "confirmation_rate_by_side": {
            "long": _quote("long"),
            "short": _quote("short"),
        },
        "pattern_counts": pattern_counts,
        "failed_breakout_lifecycle": _pattern_lifecycle("failed_breakout"),
        "failed_breakdown_lifecycle": _pattern_lifecycle("failed_breakdown"),
        "lower_high_lifecycle": _pattern_lifecycle("lower_high"),
        "higher_low_lifecycle": _pattern_lifecycle("higher_low"),
        "invalidated_events": invalidated,
        "expired_events": expired,
        "reference_swing_missing": ref_missing,
        "htf_transition_warnings": htf_transition,
        "htf_opposing_trend_blockers": htf_opposing,
        "median_candles_setup_to_structure_armed": _median(armed_latencies),
        "mean_candles_setup_to_structure_armed": _mean(armed_latencies),
        "median_candles_setup_to_confirmation": _median(confirm_latencies),
        "mean_candles_setup_to_confirmation": _mean(confirm_latencies),
        "max_concurrent_active_states": int(max_concurrent),
        "duplicate_confirmations": int(duplicate_confirmations),
        "duplicate_swing_feeds": int(duplicate_swing_feeds),
        "confirmations_without_structure_armed": int(confirmations_without_arming),
        "same_bar_confirmations_blocked": int(len(same_bar_blocked_setups)),
        "same_bar_confirmation_blocked_events": int(same_bar_blocked_events),
        "later_confirmed_after_same_bar_block": int(later_confirmed_after_same_bar_block),
        "invalid_structure_geometry_count": int(invalid_structure_geometry_count),
        "waiting_for_confirmation_level_count": int(waiting_for_confirmation_level_count),
        "pa_config": pa_config.to_dict(),
        "same_side_policy": (
            "ignore_repeated_activation_while_pa_active;"
            "replace_only_on_setup_type_change"
        ),
        "opposing_side_policy": "invalidate_via_NEW_OPPOSING_SETUP",
        "same_bar_confirmation_policy": (
            "confirm only when current_candle_timestamp > structure_armed_timestamp "
            "(no same-bar confirmation)"
        ),
        "age_policy_note": (
            "max_setup_age_candles allows ages 0..max inclusive; "
            "expire when age_candles > max (age 97 when max=96)"
        ),
        "phase1_note": (
            "evaluate_setup_activation is level-triggered for continuation_weakness; "
            "pipeline intake is edge/idle-side gated so PA is not restarted every bar"
        ),
        "detail_cases": detail_cases,
        "causal_notes": {
            "pa_pivots": "5m ConfirmedPivot only; fed when confirmation_timestamp <= candle ts",
            "htf_role": "15m/30m used only inside RegimeSnapshot/SetupActivation",
            "precomputed_swings": (
                "Swings precomputed on full closed window but gated by confirmation_timestamp"
            ),
            "decision_time": (
                "decision_time is the signal time after the closed candle under review"
            ),
            "candle_timestamp": (
                "PA closed_candle timestamp is typically the candle open time; "
                "structure_armed / break events use that same clock"
            ),
        },
    }


def format_pipeline_summary_md(summary: dict[str, Any]) -> str:
    lines = [
        "# Pipeline Audit: Regime → Setup → PriceAction",
        "",
        f"- Symbol: `{summary.get('symbol')}`",
        f"- Window: `{summary.get('start')}` → `{summary.get('end')}`",
        f"- Regime TFs: `{summary.get('timeframes_regime')}`",
        f"- PA TF: `{summary.get('price_action_timeframe')}`",
        f"- Runtime: **{summary.get('elapsed_seconds'):.1f}s**",
        "",
        "## Counts",
        "",
        f"- RegimeSnapshots: **{summary.get('regime_snapshots')}**",
        f"- SetupActivations: **{summary.get('setup_activations')}**",
        f"- By side: `{summary.get('setups_by_side')}`",
        f"- By type: `{summary.get('setups_by_type')}`",
        f"- PriceActionConfirmations: **{summary.get('price_action_confirmations')}**",
        f"- Confirmation rate overall: **{summary.get('confirmation_rate_overall')}**",
        f"- Confirmation rate by side: `{summary.get('confirmation_rate_by_side')}`",
        "",
        "## Patterns",
        "",
    ]
    for k, v in (summary.get("pattern_counts") or {}).items():
        lines.append(f"- {k}: **{v}**")
    lines.extend(
        [
            "",
            "## Lifecycle",
            "",
            f"- invalidated events: **{summary.get('invalidated_events')}**",
            f"- expired events: **{summary.get('expired_events')}**",
            f"- reference_swing_missing: **{summary.get('reference_swing_missing')}**",
            f"- HTF_TRANSITION warnings: **{summary.get('htf_transition_warnings')}**",
            f"- HTF_OPPOSING_TREND blockers: **{summary.get('htf_opposing_trend_blockers')}**",
            f"- max concurrent active states: **{summary.get('max_concurrent_active_states')}**",
            f"- duplicate confirmations: **{summary.get('duplicate_confirmations')}**",
            f"- duplicate swing feeds: **{summary.get('duplicate_swing_feeds')}**",
            f"- same-bar confirmations blocked: **{summary.get('same_bar_confirmations_blocked')}**",
            f"- later confirmed after same-bar block: **{summary.get('later_confirmed_after_same_bar_block')}**",
            f"- invalid structure geometry: **{summary.get('invalid_structure_geometry_count')}**",
            f"- waiting_for_confirmation_level events: **{summary.get('waiting_for_confirmation_level_count')}**",
            f"- failed_breakdown lifecycle: `{summary.get('failed_breakdown_lifecycle')}`",
            f"- failed_breakout lifecycle: `{summary.get('failed_breakout_lifecycle')}`",
            "",
            "## Timing (candles)",
            "",
            f"- median setup→armed: **{summary.get('median_candles_setup_to_structure_armed')}**",
            f"- mean setup→armed: **{summary.get('mean_candles_setup_to_structure_armed')}**",
            f"- median setup→confirmed: **{summary.get('median_candles_setup_to_confirmation')}**",
            f"- mean setup→confirmed: **{summary.get('mean_candles_setup_to_confirmation')}**",
            "",
            "## Policies",
            "",
            f"- same-side: `{summary.get('same_side_policy')}`",
            f"- opposing: `{summary.get('opposing_side_policy')}`",
            f"- same-bar confirmation: `{summary.get('same_bar_confirmation_policy')}`",
            f"- age: `{summary.get('age_policy_note')}`",
            "",
            "## Momentum (Phase 3)",
            "",
            "```json",
            json.dumps(json_safe(summary.get("momentum")), indent=2),
            "```",
            "",
            "## Detail cases",
            "",
            "```json",
            json.dumps(json_safe(summary.get("detail_cases")), indent=2),
            "```",
            "",
            "## Causality",
            "",
            "```json",
            json.dumps(json_safe(summary.get("causal_notes")), indent=2),
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def write_pipeline_audit_outputs(
    payload: dict[str, Any],
    output_dir: str | Path,
) -> dict[str, Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = {
        "summary_json": out / "summary.json",
        "summary_md": out / "summary.md",
        "snapshots_csv": out / "regime_snapshots.csv",
        "snapshots_json": out / "regime_snapshots.json",
        "setups_csv": out / "setup_activations.csv",
        "setups_json": out / "setup_activations.json",
        "events_csv": out / "price_action_events.csv",
        "events_json": out / "price_action_events.json",
        "confirmations_csv": out / "price_action_confirmations.csv",
        "confirmations_json": out / "price_action_confirmations.json",
    }
    summary = payload.get("summary") or {}
    paths["summary_json"].write_text(
        json.dumps(json_safe(summary), indent=2, allow_nan=False),
        encoding="utf-8",
    )
    paths["summary_md"].write_text(format_pipeline_summary_md(summary), encoding="utf-8")

    def _write_table(key_csv: str, key_json: str, rows: list[dict[str, Any]]) -> None:
        safe = json_safe(rows)
        paths[key_json].write_text(
            json.dumps(safe, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        flat = []
        for row in rows:
            item = dict(row)
            for field in ("warnings", "blockers"):
                if isinstance(item.get(field), list):
                    item[field] = json.dumps(item[field], ensure_ascii=True)
            flat.append(item)
        pd.DataFrame(flat).to_csv(paths[key_csv], index=False)

    _write_table("snapshots_csv", "snapshots_json", payload.get("regime_snapshots") or [])
    _write_table("setups_csv", "setups_json", payload.get("setup_activations") or [])
    _write_table("events_csv", "events_json", payload.get("price_action_events") or [])
    _write_table(
        "confirmations_csv",
        "confirmations_json",
        payload.get("price_action_confirmations") or [],
    )
    if payload.get("momentum"):
        mom_paths = write_momentum_audit_outputs(payload["momentum"], out)
        paths.update({f"momentum_{k}": v for k, v in mom_paths.items()})
    return paths


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Historical Regime→Setup→PriceAction pipeline audit (research-only)."
        )
    )
    parser.add_argument("--symbol", default="APTUSDT")
    parser.add_argument("--start", default="2026-03-01")
    parser.add_argument("--end", default="2026-03-08")
    parser.add_argument("--timeframes", default="5m,15m,30m")
    parser.add_argument("--history-candles", type=int, default=144)
    parser.add_argument("--workers", type=int, default=max(1, min(4, (os.cpu_count() or 2))))
    parser.add_argument("--progress-every", type=int, default=200)
    parser.add_argument("--prefetch-batch-size", type=int, default=32)
    parser.add_argument("--max-setup-age-candles", type=int, default=96)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument(
        "--output-dir",
        default="research/backtests/results/regime_scanner_pipeline_audit_march_week1",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    pa_cfg = PriceActionConfig(max_setup_age_candles=args.max_setup_age_candles)
    payload = run_pipeline_audit(
        symbol=args.symbol,
        start=args.start,
        end=args.end,
        timeframes=args.timeframes,
        history_candles=args.history_candles,
        workers=args.workers,
        progress_every=args.progress_every,
        prefetch_batch_size=args.prefetch_batch_size,
        pa_config=pa_cfg,
        data_dir=args.data_dir,
    )
    paths = write_pipeline_audit_outputs(payload, args.output_dir)
    summary = payload.get("summary") or {}
    mom = summary.get("momentum") or {}
    print(
        f"Pipeline audit complete: setups={summary.get('setup_activations')} "
        f"pa_confirmations={summary.get('price_action_confirmations')} "
        f"momentum={mom.get('momentum_confirmations')} "
        f"elapsed={summary.get('elapsed_seconds'):.1f}s"
    )
    for path in paths.values():
        print(f"Wrote: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
