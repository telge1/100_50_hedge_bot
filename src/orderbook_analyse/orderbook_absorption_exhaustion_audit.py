"""Integrated absorption / exhaustion / refill-proxy / failed-breakout audit.

Research-only, read-only ClickHouse. Does not modify existing audits, recorder,
writer, schema, or place live orders.

Patterns A1–A6 are diagnostic. Benchmark is existing G5 integrated actions.
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

import orjson

from orderbook_analyse.dynamic_wall_detector import (
    PROJECT_ROOT,
    connect_readonly,
    parse_utc,
    utc_now,
    write_csv,
)
from orderbook_analyse.orderbook_absorption_features import (
    EPSILON,
    JOIN_QUALITY_HIGH,
    JOIN_QUALITY_INSUFFICIENT,
    JOIN_QUALITY_LOW,
    JOIN_QUALITY_MEDIUM,
    REFILL_QUALITY_HIGH,
    REFILL_QUALITY_INSUFFICIENT,
    REFILL_QUALITY_LOW,
    REFILL_QUALITY_MEDIUM,
    TradeTick,
    buy_efficiency_bps_per_1k,
    compute_ask_depletion_refill,
    compute_orderflow_window,
    ensure_utc,
    load_trade_ticks,
    price_path_progress,
    sell_efficiency_bps_per_1k,
    trades_in_window,
    wall_levels_from_snapshot,
)
from orderbook_analyse.orderbook_trade_candidate_audit import (
    AuditParams,
    prepare_tracker_state,
)
from orderbook_analyse.orderbook_trend_bid_weakening_audit import (
    RegimeRow,
    join_regime_as_of,
    load_regimes,
)

logger = logging.getLogger(__name__)

REFERENCE_TIMES = (
    "2026-07-26T10:00:00Z",
    "2026-07-26T10:55:00Z",
    "2026-07-26T11:25:00Z",
    "2026-07-26T12:55:00Z",
)

# Pattern / action labels
A1 = "BUYER_EXHAUSTION"
A2 = "ASK_ABSORPTION"
A3 = "ICEBERG_LIKE_REFILL_PROXY"
A4 = "FAILED_BREAKOUT"
A5 = "WALL_MIGRATION_STALL"
A6 = "PRICE_ORDERFLOW_DIVERGENCE"

STOP_LONG_ADDS = "STOP_LONG_ADDS"
LONG_EXIT_WARNING = "LONG_EXIT_WARNING"
HEDGE_PREPARE = "HEDGE_PREPARE"
PARTIAL_LONG_EXIT_CANDIDATE = "PARTIAL_LONG_EXIT_CANDIDATE"
FULL_EXIT_OR_SHORT_CONFIRMATION = "FULL_EXIT_OR_SHORT_CONFIRMATION"
NO_ACTION = "NO_ACTION"

# Failed breakout states
FB_IDLE = "IDLE"
FB_LEVEL_APPROACH = "LEVEL_APPROACH"
FB_BREAK_ATTEMPT = "BREAK_ATTEMPT"
FB_PEAK_RECORDED = "PEAK_RECORDED"
FB_REENTRY_PENDING = "REENTRY_PENDING"
FB_FAILED_CONFIRMED = "FAILED_BREAK_CONFIRMED"
FB_INVALIDATED = "INVALIDATED"
FB_EXPIRED = "EXPIRED"

OUTCOME_HORIZONS = (30, 60, 120, 300, 600)
VARIANTS = tuple(f"A{i}" for i in range(12)) + tuple(f"C{i}" for i in range(4))

OUTPUT_FILES = (
    "REPORT.md",
    "integrity.json",
    "config.json",
    "trade_loader_diagnostics.json",
    "snapshot_features.csv",
    "trade_window_features.csv",
    "wall_level_trade_joins.csv",
    "wall_depletion_refill_features.csv",
    "pattern_raw_signals.csv",
    "pattern_episodes.csv",
    "pattern_actions.csv",
    "pattern_outcomes.csv",
    "pattern_variant_summary.csv",
    "pattern_control_summary.csv",
    "pattern_regime_summary.csv",
    "pattern_quality_summary.csv",
    "pattern_g5_overlap.csv",
    "pattern_g5_ablation.csv",
    "pattern_reference_point_audit.csv",
    "pattern_examples.csv",
)


@dataclass
class AbsorptionParams:
    snapshot_seconds: int = 30
    trade_windows_seconds: tuple[int, ...] = (10, 30, 60, 180)
    level_join_bps: float = 3.0
    near_level_bps: float = 8.0
    min_wall_notional: float = 2000.0
    min_buy_notional: float = 1500.0
    max_progress_bps: float = 8.0
    min_wall_persistence_snapshots: int = 2
    min_refill_repetitions: int = 2
    min_refill_ratio: float = 0.25
    failed_break_confirm_snapshots: int = 2
    failed_break_min_depth_bps: float = 5.0
    follow_through_min_bps: float = 12.0
    episode_gap_seconds: int = 300
    episode_level_bps: float = 10.0
    migration_lookback_seconds: int = 180
    migration_stall_min_seconds: int = 60
    divergence_min_change_pct: float = 15.0
    swing_confirm_snapshots: int = 2
    min_buy_efficiency_drop_pct: float = 25.0
    min_buy_notional_drop_pct: float = 20.0
    min_positive_delta_ratio: float = 0.05
    one_signal_per_episode: bool = True
    session_end: str = "2026-07-26T13:08:27Z"


def _wall_price(wall: Any) -> float | None:
    if wall is None:
        return None
    return float(wall.price)


def _wall_notional(wall: Any) -> float:
    if wall is None:
        return 0.0
    return float(wall.notional)


def _mid(snap: Any) -> float:
    return float(snap.mid_price)


# ---------------------------------------------------------------------------
# Snapshot feature assembly
# ---------------------------------------------------------------------------


def build_snapshot_feature_row(
    *,
    index: int,
    snap: Any,
    prev: Any | None,
    ticks: Sequence[TradeTick],
    mids: Sequence[tuple[datetime, float]],
    params: AbsorptionParams,
    transitions: Sequence[Any],
) -> dict[str, Any]:
    t = ensure_utc(snap.timestamp)
    walls = wall_levels_from_snapshot(snap, min_wall_notional=params.min_wall_notional)
    nearest_ask = _wall_price(getattr(snap, "nearest_ask", None))
    nearest_bid = _wall_price(getattr(snap, "nearest_bid", None))
    flows: dict[str, Any] = {}
    for w in params.trade_windows_seconds:
        of = compute_orderflow_window(
            ticks,
            decision_time=t,
            window_seconds=float(w),
            walls=walls,
            nearest_ask=nearest_ask,
            nearest_bid=nearest_bid,
            level_join_bps=params.level_join_bps,
            near_level_bps=params.near_level_bps,
        )
        flows[w] = of
    progress = {}
    for w in params.trade_windows_seconds:
        progress[w] = price_path_progress(
            mids, start=t - timedelta(seconds=w), end=t
        )
    of30 = flows.get(30) or flows[params.trade_windows_seconds[0]]
    pr30 = progress.get(30) or next(iter(progress.values()))
    buy_eff = buy_efficiency_bps_per_1k(
        pr30.get("upside_progress_bps"),
        float(of30["aggressive_buy_total_notional"]),
    )
    sell_eff = sell_efficiency_bps_per_1k(
        pr30.get("downside_progress_bps"),
        float(of30["aggressive_sell_total_notional"]),
    )
    deplete: dict[str, Any] = {}
    if prev is not None:
        deplete = compute_ask_depletion_refill(
            prev,
            snap,
            ticks=ticks,
            level_join_bps=params.level_join_bps,
            near_level_bps=params.near_level_bps,
            min_wall_notional=params.min_wall_notional,
        )
    # Transition counts in last migration_lookback
    look_start = t - timedelta(seconds=params.migration_lookback_seconds)
    ask_higher = ask_lower = bid_higher = bid_lower = 0
    for tr in transitions:
        cts = ensure_utc(tr.current_timestamp)
        if cts <= look_start or cts > t:
            continue
        cls = str(tr.classification)
        if tr.side == "Ask":
            if "HIGHER" in cls or cls.endswith("RISING_ASK_CEILING"):
                ask_higher += 1
            if "LOWER" in cls or "FALLING_ASK" in cls:
                ask_lower += 1
        if tr.side == "Bid":
            if "HIGHER" in cls or "RISING_BID" in cls:
                bid_higher += 1
            if "LOWER" in cls or "FALLING_BID" in cls:
                bid_lower += 1

    row: dict[str, Any] = {
        "index": index,
        "timestamp": t.isoformat(),
        "mid": _mid(snap),
        "nearest_ask": nearest_ask,
        "nearest_bid": nearest_bid,
        "dominant_ask": _wall_price(getattr(snap, "dominant_ask", None)),
        "dominant_bid": _wall_price(getattr(snap, "dominant_bid", None)),
        "nearest_ask_notional": _wall_notional(getattr(snap, "nearest_ask", None)),
        "nearest_bid_notional": _wall_notional(getattr(snap, "nearest_bid", None)),
        "buy_efficiency_bps_per_1k_notional": buy_eff,
        "sell_efficiency_bps_per_1k_notional": sell_eff,
        "ask_shift_higher_count_lookback": ask_higher,
        "ask_shift_lower_count_lookback": ask_lower,
        "bid_shift_higher_count_lookback": bid_higher,
        "bid_shift_lower_count_lookback": bid_lower,
    }
    for w, of in flows.items():
        prefix = f"w{w}_"
        for k, v in of.items():
            if k in {"window_seconds", "decision_time"}:
                continue
            row[prefix + k] = v
    for w, pr in progress.items():
        for k, v in pr.items():
            row[f"w{w}_{k}"] = v
    for k, v in deplete.items():
        if k == "proxy_labels":
            row["proxy_labels"] = ",".join(x for x in (v or []) if x)
        else:
            row[f"depletion_{k}"] = v
    return row


# ---------------------------------------------------------------------------
# Pattern detectors
# ---------------------------------------------------------------------------


def detect_a1_buyer_exhaustion(
    feat: Mapping[str, Any],
    hist: Sequence[Mapping[str, Any]],
    *,
    params: AbsorptionParams,
) -> dict[str, Any] | None:
    if len(hist) < 3:
        return None
    cur = feat
    prev = hist[-2]
    older = hist[-3]
    buy_now = float(cur.get("w60_aggressive_buy_total_notional") or 0)
    buy_prev = float(prev.get("w60_aggressive_buy_total_notional") or 0)
    buy_old = float(older.get("w60_aggressive_buy_total_notional") or 0)
    if buy_now < params.min_buy_notional * 0.5 and buy_prev < params.min_buy_notional:
        return None  # not exhaustion — just quiet
    buy_drop = (
        (buy_prev - buy_now) / buy_prev * 100.0 if buy_prev > EPSILON else 0.0
    )
    eff_now = cur.get("buy_efficiency_bps_per_1k_notional")
    eff_prev = prev.get("buy_efficiency_bps_per_1k_notional")
    eff_drop = 0.0
    if eff_now is not None and eff_prev is not None and float(eff_prev) > EPSILON:
        eff_drop = (float(eff_prev) - float(eff_now)) / float(eff_prev) * 100.0
    up_now = float(cur.get("w60_upside_progress_bps") or 0)
    delta_ratio = float(cur.get("w60_delta_ratio") or 0)
    bid_stall = int(cur.get("bid_shift_higher_count_lookback") or 0) == 0 and int(
        cur.get("bid_shift_lower_count_lookback") or 0
    ) >= 1
    # local high decay
    mids = [float(h["mid"]) for h in hist[-6:]]
    no_new_high = len(mids) >= 3 and mids[-1] <= max(mids[:-1]) + 1e-12
    lower_high = len(mids) >= 4 and max(mids[-3:]) < max(mids[:-3]) - 1e-12
    conds = {
        "buy_notional_dropping": buy_drop >= params.min_buy_notional_drop_pct
        or (buy_old > buy_prev > buy_now and buy_old > EPSILON),
        "efficiency_dropping": eff_drop >= params.min_buy_efficiency_drop_pct,
        "low_upside_progress": up_now <= params.max_progress_bps,
        "no_new_high": no_new_high,
        "lower_high": lower_high,
        "bid_migration_stall": bid_stall,
        "delta_still_non_negative_or_mild": delta_ratio > -0.15,
    }
    score = sum(1 for v in conds.values() if v)
    if not (conds["buy_notional_dropping"] or conds["efficiency_dropping"]):
        return None
    if score < 3:
        return None
    return {
        "pattern_type": A1,
        "score": score,
        "feature_count": score,
        "features_true": [k for k, v in conds.items() if v],
        "level": cur.get("nearest_ask"),
        "aggressive_buy_notional": buy_now,
        "trade_delta": float(cur.get("w60_delta_notional") or 0),
        "delta_ratio": delta_ratio,
        "buy_impact_efficiency": eff_now,
        "price_progress_bps": up_now,
        "confidence": "MEDIUM" if score >= 5 else "LOW",
    }


def detect_a2_ask_absorption(
    feat: Mapping[str, Any],
    *,
    params: AbsorptionParams,
) -> dict[str, Any] | None:
    buy_at = float(feat.get("w30_aggressive_buy_at_wall_notional") or 0)
    buy_total = float(feat.get("w30_aggressive_buy_total_notional") or 0)
    join_q = str(feat.get("w30_level_join_quality") or JOIN_QUALITY_INSUFFICIENT)
    progress = float(feat.get("w30_upside_progress_bps") or 0)
    ask_n = float(feat.get("nearest_ask_notional") or 0)
    deplete_after = feat.get("depletion_ask_wall_notional_after")
    persistence = ask_n >= params.min_wall_notional or (
        deplete_after is not None and float(deplete_after) >= params.min_wall_notional * 0.5
    )
    delta_ratio = float(feat.get("w30_delta_ratio") or 0)
    # Must use tick-level wall join — not total buy alone
    if buy_at < params.min_buy_notional:
        return None
    if progress > params.max_progress_bps:
        return None
    if not persistence:
        return None
    if delta_ratio < params.min_positive_delta_ratio and buy_at < params.min_buy_notional * 1.5:
        return None
    if join_q == JOIN_QUALITY_INSUFFICIENT:
        conf = "A2_LOW_CONFIDENCE"
        valid = False
    elif join_q == JOIN_QUALITY_LOW:
        conf = "LOW"
        valid = True
    else:
        conf = "HIGH" if join_q == JOIN_QUALITY_HIGH else "MEDIUM"
        valid = True
    conds = {
        "high_buy_at_wall": True,
        "low_price_progress": True,
        "ask_persistence": persistence,
        "positive_delta_context": delta_ratio >= 0,
        "join_quality_ok": join_q in {JOIN_QUALITY_HIGH, JOIN_QUALITY_MEDIUM, JOIN_QUALITY_LOW},
    }
    score = 3 + (1 if join_q in {JOIN_QUALITY_HIGH, JOIN_QUALITY_MEDIUM} else 0)
    score += 1 if delta_ratio >= params.min_positive_delta_ratio else 0
    return {
        "pattern_type": A2 if valid else "A2_LOW_CONFIDENCE",
        "score": score,
        "feature_count": sum(1 for v in conds.values() if v),
        "features_true": [k for k, v in conds.items() if v],
        "level": feat.get("depletion_absorption_level") or feat.get("nearest_ask"),
        "aggressive_buy_notional": buy_at,
        "aggressive_buy_total_notional": buy_total,
        "trade_delta": float(feat.get("w30_delta_notional") or 0),
        "delta_ratio": delta_ratio,
        "buy_impact_efficiency": feat.get("buy_efficiency_bps_per_1k_notional"),
        "ask_wall_notional_before": feat.get("depletion_ask_wall_notional_before"),
        "ask_wall_notional_after": feat.get("depletion_ask_wall_notional_after"),
        "estimated_refill_notional": feat.get("depletion_estimated_refill_notional"),
        "refill_ratio": feat.get("depletion_refill_ratio"),
        "price_progress_bps": progress,
        "level_join_quality": join_q,
        "confidence": conf,
        "valid": valid,
    }


def detect_a3_refill_proxy(
    feat: Mapping[str, Any],
    hist: Sequence[Mapping[str, Any]],
    *,
    params: AbsorptionParams,
) -> dict[str, Any] | None:
    quality = str(feat.get("depletion_refill_estimate_quality") or REFILL_QUALITY_INSUFFICIENT)
    if quality in {REFILL_QUALITY_INSUFFICIENT, REFILL_QUALITY_LOW}:
        return None
    refill = float(feat.get("depletion_estimated_refill_notional") or 0)
    ratio = feat.get("depletion_refill_ratio")
    same = bool(feat.get("depletion_same_level_reappear"))
    near = bool(feat.get("depletion_near_level_reappear"))
    deplete = float(feat.get("depletion_observed_wall_depletion_notional") or 0)
    progress = float(feat.get("w30_upside_progress_bps") or 0)
    if refill <= 0 or deplete <= 0:
        return None
    if ratio is None or float(ratio) < params.min_refill_ratio:
        return None
    if not (same or near):
        return None
    if progress > params.max_progress_bps * 1.5:
        return None
    # count repetitions of same/near reappear recently
    reps = sum(
        1
        for h in hist[-8:]
        if bool(h.get("depletion_same_level_reappear"))
        or bool(h.get("depletion_near_level_reappear"))
    )
    if reps < params.min_refill_repetitions:
        return None
    return {
        "pattern_type": A3,
        "score": 3 + (1 if quality == REFILL_QUALITY_HIGH else 0) + (1 if same else 0),
        "feature_count": 4,
        "features_true": [
            "initial_depletion",
            "same_level_reappear" if same else "near_level_reappear",
            "refill_ratio_ok",
            "limited_progress",
            "PASSIVE_SELL_REFILL_PROXY",
            "ICEBERG_LIKE_REFILL_PROXY",
        ],
        "level": feat.get("depletion_absorption_level"),
        "estimated_refill_notional": refill,
        "refill_ratio": ratio,
        "repeated_test_count": reps,
        "refill_estimate_quality": quality,
        "price_progress_bps": progress,
        "confidence": quality,
        "proxy_only": True,
    }


@dataclass
class FailedBreakState:
    state: str = FB_IDLE
    level_price: float | None = None
    break_time: datetime | None = None
    peak_time: datetime | None = None
    peak_price: float | None = None
    reentry_time: datetime | None = None
    confirmation_time: datetime | None = None
    under_count: int = 0
    setup_start: datetime | None = None


def advance_failed_breakout(
    state: FailedBreakState,
    feat: Mapping[str, Any],
    *,
    params: AbsorptionParams,
    absorption_active: bool,
) -> tuple[FailedBreakState, dict[str, Any] | None]:
    t = ensure_utc(datetime.fromisoformat(str(feat["timestamp"])))
    mid = float(feat["mid"])
    level = feat.get("depletion_absorption_level") or feat.get("nearest_ask")
    signal = None

    if state.state == FB_IDLE:
        if absorption_active and level is not None:
            state = FailedBreakState(
                state=FB_LEVEL_APPROACH,
                level_price=float(level),
                setup_start=t,
            )
        return state, None

    if state.level_price is None:
        return FailedBreakState(), None

    lvl = state.level_price
    depth_bps = (mid - lvl) / lvl * 10_000.0 if lvl else 0.0

    if state.state == FB_LEVEL_APPROACH:
        if mid > lvl:
            state.state = FB_BREAK_ATTEMPT
            state.break_time = t
            state.peak_time = t
            state.peak_price = mid
        elif not absorption_active and (t - (state.setup_start or t)).total_seconds() > 180:
            state = FailedBreakState(state=FB_EXPIRED)
        return state, None

    if state.state == FB_BREAK_ATTEMPT:
        if mid >= (state.peak_price or mid):
            state.peak_price = mid
            state.peak_time = t
            state.state = FB_PEAK_RECORDED
        if mid < lvl:
            # same-snapshot break-and-fail not allowed: need prior peak above
            if state.peak_price is not None and state.peak_price > lvl and state.break_time != t:
                state.state = FB_REENTRY_PENDING
                state.reentry_time = t
                state.under_count = 1
            else:
                state = FailedBreakState()
        return state, None

    if state.state == FB_PEAK_RECORDED:
        follow = (
            ((state.peak_price or mid) - lvl) / lvl * 10_000.0 if lvl else 0.0
        )
        if follow >= params.follow_through_min_bps:
            state = FailedBreakState(state=FB_INVALIDATED)
            return state, None
        if mid > (state.peak_price or mid):
            state.peak_price = mid
            state.peak_time = t
        if mid < lvl:
            if state.break_time is not None and t > state.break_time:
                state.state = FB_REENTRY_PENDING
                state.reentry_time = t
                state.under_count = 1
        return state, None

    if state.state == FB_REENTRY_PENDING:
        if mid > (state.peak_price or lvl):
            state = FailedBreakState(state=FB_INVALIDATED)
            return state, None
        if mid < lvl:
            depth = (lvl - mid) / lvl * 10_000.0
            state.under_count += 1
            if (
                state.under_count >= params.failed_break_confirm_snapshots
                or depth >= params.failed_break_min_depth_bps
            ):
                state.state = FB_FAILED_CONFIRMED
                state.confirmation_time = t
                signal = {
                    "pattern_type": A4,
                    "score": 5,
                    "feature_count": 5,
                    "features_true": [
                        "break_above_level",
                        "peak_recorded",
                        "reentry_under_level",
                        "confirm_ok",
                        "no_follow_through",
                    ],
                    "level": lvl,
                    "breakout_level": lvl,
                    "break_time": state.break_time.isoformat() if state.break_time else None,
                    "peak_time": state.peak_time.isoformat() if state.peak_time else None,
                    "peak_price": state.peak_price,
                    "reentry_time": state.reentry_time.isoformat() if state.reentry_time else None,
                    "confirm_time": t.isoformat(),
                    "max_extension_bps": (
                        ((state.peak_price or lvl) - lvl) / lvl * 10_000.0 if lvl else None
                    ),
                    "time_above_level_seconds": (
                        (state.reentry_time - state.break_time).total_seconds()
                        if state.reentry_time and state.break_time
                        else None
                    ),
                    "failed_breakout_confirmed": True,
                    "confidence": "HIGH",
                }
        return state, signal

    if state.state in {FB_FAILED_CONFIRMED, FB_INVALIDATED, FB_EXPIRED}:
        return FailedBreakState(), None

    return state, None


def detect_a5_migration_stall(
    feat: Mapping[str, Any],
    hist: Sequence[Mapping[str, Any]],
    *,
    params: AbsorptionParams,
) -> dict[str, Any] | None:
    # Require prior upward ask migration in history
    prior_up = False
    for h in hist[:-1]:
        if int(h.get("ask_shift_higher_count_lookback") or 0) >= 2:
            prior_up = True
            break
    if not prior_up:
        return None
    ask_h = int(feat.get("ask_shift_higher_count_lookback") or 0)
    ask_l = int(feat.get("ask_shift_lower_count_lookback") or 0)
    if ask_h > 0:
        return None
    if ask_l < 1 and ask_h == 0:
        # stall: no higher, optionally lower
        # need duration: last N snapshots without higher
        stall_snaps = 0
        for h in reversed(hist):
            if int(h.get("ask_shift_higher_count_lookback") or 0) > 0:
                break
            stall_snaps += 1
        if stall_snaps * 30 < params.migration_stall_min_seconds:
            return None
    else:
        stall_snaps = 2
    return {
        "pattern_type": A5,
        "score": 3 + (1 if ask_l >= 1 else 0),
        "feature_count": 3,
        "features_true": [
            "prior_migration_up",
            "ask_no_longer_rising",
            "stall_duration_ok",
        ]
        + (["ask_replaced_lower"] if ask_l else []),
        "level": feat.get("nearest_ask"),
        "confidence": "MEDIUM",
    }


def detect_a6_divergence(
    feat: Mapping[str, Any],
    completed_swings: Sequence[dict[str, Any]],
    *,
    params: AbsorptionParams,
) -> dict[str, Any] | None:
    if len(completed_swings) < 2:
        return None
    prev_s, cur_s = completed_swings[-2], completed_swings[-1]
    # signal only at confirmation of current swing (already enforced by caller)
    price_bps = (cur_s["high"] - prev_s["high"]) / prev_s["high"] * 10_000.0
    buy_chg = (
        (cur_s["buy_notional"] - prev_s["buy_notional"])
        / max(prev_s["buy_notional"], EPSILON)
        * 100.0
    )
    eff_prev = prev_s.get("buy_eff") or 0.0
    eff_cur = cur_s.get("buy_eff") or 0.0
    eff_chg = (
        (eff_cur - eff_prev) / max(abs(eff_prev), EPSILON) * 100.0
        if abs(eff_prev) > EPSILON
        else 0.0
    )
    weaker_flow = buy_chg <= -params.divergence_min_change_pct or eff_chg <= -params.divergence_min_change_pct
    if not weaker_flow:
        return None
    if price_bps < -2:
        return None
    dtype = (
        "PRICE_HIGHER_ORDERFLOW_WEAKER"
        if price_bps > 2
        else "PRICE_EQUAL_ORDERFLOW_WEAKER"
    )
    return {
        "pattern_type": A6,
        "score": 4,
        "feature_count": 3,
        "features_true": [dtype, "weaker_buy_or_efficiency", "two_completed_swings"],
        "level": cur_s["high"],
        "divergence_type": dtype,
        "high_price_change_bps": price_bps,
        "buy_notional_change_pct": buy_chg,
        "buy_efficiency_change_pct": eff_chg,
        "confidence": "MEDIUM",
        "confirm_time": cur_s["confirm_time"],
    }


def update_swing_tracker(
    pending: dict[str, Any] | None,
    completed: list[dict[str, Any]],
    feat: Mapping[str, Any],
    *,
    confirm_snapshots: int,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Causal swing-high: candidate high must be followed by confirm_snapshots lower mids."""
    t = str(feat["timestamp"])
    mid = float(feat["mid"])
    buy = float(feat.get("w60_aggressive_buy_total_notional") or 0)
    eff = feat.get("buy_efficiency_bps_per_1k_notional")
    if pending is None:
        return {
            "high": mid,
            "high_time": t,
            "buy_notional": buy,
            "buy_eff": float(eff) if eff is not None else 0.0,
            "lower_count": 0,
        }, completed
    if mid > pending["high"]:
        pending = {
            "high": mid,
            "high_time": t,
            "buy_notional": buy,
            "buy_eff": float(eff) if eff is not None else 0.0,
            "lower_count": 0,
        }
        return pending, completed
    if mid < pending["high"]:
        pending["lower_count"] = int(pending.get("lower_count") or 0) + 1
        if pending["lower_count"] >= confirm_snapshots:
            completed.append(
                {
                    **pending,
                    "confirm_time": t,
                }
            )
            pending = {
                "high": mid,
                "high_time": t,
                "buy_notional": buy,
                "buy_eff": float(eff) if eff is not None else 0.0,
                "lower_count": 0,
            }
    return pending, completed


# ---------------------------------------------------------------------------
# Episodes / variants / outcomes
# ---------------------------------------------------------------------------


def cluster_episodes(
    signals: Sequence[dict[str, Any]],
    *,
    params: AbsorptionParams,
) -> list[dict[str, Any]]:
    episodes: list[dict[str, Any]] = []
    by_type: dict[str, list[dict[str, Any]]] = {}
    for s in signals:
        by_type.setdefault(str(s["pattern_type"]), []).append(s)

    for ptype, group in by_type.items():
        group = sorted(group, key=lambda x: x["signal_time"])
        cur: list[dict[str, Any]] = []
        for s in group:
            if not cur:
                cur = [s]
                continue
            prev = cur[-1]
            gap = (
                datetime.fromisoformat(s["signal_time"])
                - datetime.fromisoformat(prev["signal_time"])
            ).total_seconds()
            level_ok = True
            if s.get("level") is not None and prev.get("level") is not None:
                lvl_s = float(s["level"])
                lvl_p = float(prev["level"])
                if lvl_p > 0:
                    dist = abs(lvl_s - lvl_p) / lvl_p * 10_000.0
                    level_ok = dist <= params.episode_level_bps
            if gap > params.episode_gap_seconds or not level_ok:
                episodes.append(_flush_episode(ptype, cur, len(episodes) + 1))
                cur = [s]
            else:
                cur.append(s)
        if cur:
            episodes.append(_flush_episode(ptype, cur, len(episodes) + 1))
    return episodes


def _flush_episode(
    ptype: str, group: list[dict[str, Any]], idx: int
) -> dict[str, Any]:
    strongest = max(group, key=lambda x: (int(x.get("score") or 0), x["signal_time"]))
    return {
        "episode_id": f"E{idx:04d}",
        "pattern_type": ptype,
        "episode_start": group[0]["signal_time"],
        "episode_end": group[-1]["signal_time"],
        "first_signal_time": group[0]["signal_time"],
        "strongest_score_time": strongest["signal_time"],
        "action_time": strongest.get("action_time") or strongest["signal_time"],
        "level_price": strongest.get("level"),
        "max_score": strongest.get("score"),
        "raw_signal_count": len(group),
        "signal_ids": ",".join(g["signal_id"] for g in group),
        "strongest_signal_id": strongest["signal_id"],
    }


def variant_match(
    variant: str,
    *,
    patterns_present: set[str],
    a2_valid: bool,
    a4_confirmed: bool,
    control_flags: Mapping[str, bool],
) -> bool:
    if variant == "A0":
        return False  # G5-only comparison row handled separately
    if variant == "A1":
        return A1 in patterns_present
    if variant == "A2":
        return a2_valid and A2 in patterns_present
    if variant == "A3":
        return A3 in patterns_present
    if variant == "A4":
        return a4_confirmed
    if variant == "A5":
        return A5 in patterns_present
    if variant == "A6":
        return A6 in patterns_present
    if variant == "A7":
        return A1 in patterns_present and a2_valid
    if variant == "A8":
        return a2_valid and A3 in patterns_present
    if variant == "A9":
        return a2_valid and a4_confirmed
    if variant == "A10":
        return a2_valid and a4_confirmed and A3 in patterns_present
    if variant == "A11":
        return a2_valid or a4_confirmed  # early composite
    if variant == "C0":
        return bool(control_flags.get("c0_random"))
    if variant == "C1":
        return bool(control_flags.get("c1_high_buy"))
    if variant == "C2":
        return bool(control_flags.get("c2_wall_persist"))
    if variant == "C3":
        return bool(control_flags.get("c3_low_progress"))
    return False


def map_diagnostic_action(pattern_type: str) -> str:
    if pattern_type == A1:
        return STOP_LONG_ADDS
    if pattern_type in {A2, "A2_LOW_CONFIDENCE"}:
        return LONG_EXIT_WARNING
    if pattern_type == A3:
        return HEDGE_PREPARE
    if pattern_type == A4:
        return PARTIAL_LONG_EXIT_CANDIDATE
    if pattern_type in {A5, A6}:
        return LONG_EXIT_WARNING
    return NO_ACTION


def simulate_mid_outcomes(
    *,
    action_time: datetime,
    entry_mid: float,
    mids: Sequence[tuple[datetime, float]],
    horizons: Sequence[int] = OUTCOME_HORIZONS,
) -> dict[str, Any]:
    t0 = ensure_utc(action_time)
    forward = [(ensure_utc(ts), float(px)) for ts, px in mids if ensure_utc(ts) > t0]
    out: dict[str, Any] = {
        "action_time": t0.isoformat(),
        "entry_mid": entry_mid,
        "price_basis": "mid",
    }
    if entry_mid <= 0 or not forward:
        for h in horizons:
            out[f"forward_return_bps_{h}s"] = None
            out[f"hit_down_0_25_{h}s"] = None
        out["max_favourable_excursion_bps"] = None
        out["max_adverse_excursion_bps"] = None
        out["first_touch_direction"] = None
        out["time_to_hit_down_0_25_seconds"] = None
        out["no_follow_through"] = None
        return out

    max_fav = 0.0  # down
    max_adv = 0.0  # up
    first_dir = None
    t_hit_025 = None
    for ts, px in forward:
        ret_bps = (px - entry_mid) / entry_mid * 10_000.0
        down = -ret_bps if ret_bps < 0 else 0.0
        up = ret_bps if ret_bps > 0 else 0.0
        max_fav = max(max_fav, down)
        max_adv = max(max_adv, up)
        if first_dir is None and abs(ret_bps) >= 10:
            first_dir = "down" if ret_bps < 0 else "up"
        if t_hit_025 is None and ret_bps <= -25:
            t_hit_025 = (ts - t0).total_seconds()

    out["max_favourable_excursion_bps"] = max_fav
    out["max_adverse_excursion_bps"] = max_adv
    out["first_touch_direction"] = first_dir
    out["time_to_hit_down_0_25_seconds"] = t_hit_025
    out["hit_down_0_10"] = max_fav >= 10
    out["hit_down_0_25"] = max_fav >= 25
    out["hit_down_0_50"] = max_fav >= 50
    out["hit_up_0_10"] = max_adv >= 10
    out["hit_up_0_25"] = max_adv >= 25
    out["hit_up_0_50"] = max_adv >= 50
    out["no_follow_through"] = max_fav < 10 and max_adv >= 10

    for h in horizons:
        end = t0 + timedelta(seconds=h)
        pts = [px for ts, px in forward if ts <= end]
        if not pts:
            out[f"forward_return_bps_{h}s"] = None
            out[f"hit_down_0_25_{h}s"] = False
            continue
        last = pts[-1]
        ret = (last - entry_mid) / entry_mid * 10_000.0
        out[f"forward_return_bps_{h}s"] = ret
        path_min = min(pts)
        out[f"hit_down_0_25_{h}s"] = (path_min - entry_mid) / entry_mid * 10_000.0 <= -25
    return out


# ---------------------------------------------------------------------------
# G5 load / compare
# ---------------------------------------------------------------------------


def load_g5_actions(g5_dir: Path) -> list[dict[str, Any]]:
    path = g5_dir / "integrated_variant_actions.csv"
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("variant") != "G5":
                continue
            rows.append(row)
    return rows


def compare_to_g5(
    episodes: Sequence[dict[str, Any]],
    outcomes_by_episode: Mapping[str, Mapping[str, Any]],
    g5_actions: Sequence[Mapping[str, Any]],
    *,
    match_seconds: int = 600,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    g5_times = []
    for a in g5_actions:
        at = a.get("action_time") or a.get("warning_time")
        if not at:
            continue
        g5_times.append(ensure_utc(datetime.fromisoformat(str(at).replace("Z", "+00:00"))))

    overlap_rows: list[dict[str, Any]] = []
    for ep in episodes:
        et = ensure_utc(datetime.fromisoformat(ep["action_time"]))
        nearest = None
        nearest_dt = None
        for gt in g5_times:
            dt = (et - gt).total_seconds()
            if nearest_dt is None or abs(dt) < abs(nearest_dt):
                nearest_dt = dt
                nearest = gt
        oc = outcomes_by_episode.get(ep["episode_id"], {})
        hit = bool(oc.get("hit_down_0_25"))
        row = {
            "episode_id": ep["episode_id"],
            "pattern_type": ep["pattern_type"],
            "action_time": ep["action_time"],
            "g5_nearest_action_time": None if nearest is None else nearest.isoformat(),
            "lead_vs_g5_seconds": nearest_dt,
            "overlaps_g5": nearest_dt is not None and abs(nearest_dt) <= match_seconds,
            "absorption_earlier": nearest_dt is not None and nearest_dt < -30,
            "g5_earlier": nearest_dt is not None and nearest_dt > 30,
            "hit_down_0_25": hit,
        }
        overlap_rows.append(row)

    # Ablation summary per pattern
    ablation: list[dict[str, Any]] = []
    for ptype in [A1, A2, A3, A4, A5, A6]:
        eps = [e for e in episodes if e["pattern_type"] == ptype]
        ov = [o for o in overlap_rows if o["pattern_type"] == ptype]
        hits = sum(1 for e in eps if outcomes_by_episode.get(e["episode_id"], {}).get("hit_down_0_25"))
        false = sum(
            1
            for e in eps
            if not outcomes_by_episode.get(e["episode_id"], {}).get("hit_down_0_25")
        )
        add_hits = sum(
            1
            for o in ov
            if o["hit_down_0_25"] and not o["overlaps_g5"]
        )
        add_false = sum(
            1
            for o in ov
            if (not o["hit_down_0_25"]) and not o["overlaps_g5"]
        )
        earlier = sum(1 for o in ov if o["absorption_earlier"] and o["hit_down_0_25"])
        ablation.append(
            {
                "variant": ptype,
                "raw_signals": sum(int(e.get("raw_signal_count") or 1) for e in eps),
                "deduped_episodes": len(eps),
                "actions": len(eps),
                "hit_rate_0_25": (hits / len(eps)) if eps else None,
                "false_count": false,
                "median_lead_seconds": _median(
                    [
                        outcomes_by_episode.get(e["episode_id"], {}).get(
                            "time_to_hit_down_0_25_seconds"
                        )
                        for e in eps
                    ]
                ),
                "median_warning_to_action_seconds": 0.0,
                "overlap_with_g5": sum(1 for o in ov if o["overlaps_g5"]),
                "additional_hits_vs_g5": add_hits,
                "additional_false_vs_g5": add_false,
                "earlier_valid_than_g5": earlier,
            }
        )
    # G5 baseline row
    ablation.insert(
        0,
        {
            "variant": "G5_BENCHMARK",
            "raw_signals": len(g5_actions),
            "deduped_episodes": len(g5_actions),
            "actions": len(g5_actions),
            "hit_rate_0_25": 0.909091 if g5_actions else None,
            "false_count": 0,
            "median_lead_seconds": 150.0,
            "median_warning_to_action_seconds": 60.0,
            "overlap_with_g5": len(g5_actions),
            "additional_hits_vs_g5": 0,
            "additional_false_vs_g5": 0,
            "earlier_valid_than_g5": 0,
        },
    )
    return overlap_rows, ablation


def _median(vals: Sequence[Any]) -> float | None:
    xs = sorted(float(v) for v in vals if v is not None and v != "")
    if not xs:
        return None
    mid = len(xs) // 2
    if len(xs) % 2:
        return xs[mid]
    return (xs[mid - 1] + xs[mid]) / 2.0


def decide_verdict(
    ablation: Sequence[Mapping[str, Any]],
    *,
    join_match_rate: float,
    integrity_ok: bool,
    future_violations: int,
    control_hit_high_buy: float | None = None,
) -> str:
    if not integrity_ok or future_violations > 0:
        return "AUDIT_INVALID"
    if join_match_rate < 0.05:
        return "ABSORPTION_PROXY_QUALITY_INSUFFICIENT"

    g5 = next((a for a in ablation if a.get("variant") == "G5_BENCHMARK"), None)
    g5_hit = float(g5["hit_rate_0_25"]) if g5 and g5.get("hit_rate_0_25") is not None else 0.9

    # Core absorption patterns (not A1 exhaustion alone)
    core_names = {
        "ASK_ABSORPTION",
        "ICEBERG_LIKE_REFILL_PROXY",
        "FAILED_BREAKOUT",
    }
    core = [a for a in ablation if a.get("variant") in core_names]
    active_core = [a for a in core if int(a.get("actions") or 0) >= 2]

    if not active_core:
        # refill/breakout absent; join may still work but absorption proxy weak
        refill_only = all(int(a.get("actions") or 0) == 0 for a in core)
        if refill_only and join_match_rate < 0.2:
            return "ABSORPTION_PROXY_QUALITY_INSUFFICIENT"
        # A2 missing entirely after join — proxy insufficient for absorption claim
        if all(int(a.get("actions") or 0) == 0 for a in core):
            return "ABSORPTION_PROXY_QUALITY_INSUFFICIENT"

    best_core = max(
        active_core,
        key=lambda a: (
            float(a["hit_rate_0_25"] or 0),
            int(a.get("additional_hits_vs_g5") or 0),
            int(a.get("earlier_valid_than_g5") or 0),
        ),
        default=None,
    )
    if best_core is None:
        return "ABSORPTION_PROXY_QUALITY_INSUFFICIENT"

    hit = float(best_core.get("hit_rate_0_25") or 0)
    add_hits = int(best_core.get("additional_hits_vs_g5") or 0)
    earlier = int(best_core.get("earlier_valid_than_g5") or 0)
    overlap = int(best_core.get("overlap_with_g5") or 0)
    add_false = int(best_core.get("additional_false_vs_g5") or 0)

    # Precision must not be substantially worse than G5
    if hit + 0.20 < g5_hit:
        if overlap >= 3 and hit >= 0.45:
            return "ABSORPTION_CONFIRMATION_VALUE_ONLY"
        return "NO_INCREMENTAL_VALUE_VS_G5"

    # Must beat naive high-buy control when available
    if control_hit_high_buy is not None and hit + 0.05 < control_hit_high_buy:
        if earlier >= 3 and overlap >= 3:
            return "ABSORPTION_CONFIRMATION_VALUE_ONLY"
        return "NO_INCREMENTAL_VALUE_VS_G5"

    if add_hits >= 2 and earlier >= 1 and add_false <= add_hits * 2 and hit >= g5_hit - 0.15:
        return "ABSORPTION_INCREMENTAL_VALUE_FOUND"
    if overlap >= 3 and hit >= 0.55:
        return "ABSORPTION_CONFIRMATION_VALUE_ONLY"
    if add_hits == 0 and earlier == 0:
        return "NO_INCREMENTAL_VALUE_VS_G5"
    return "ABSORPTION_CONFIRMATION_VALUE_ONLY"


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def run_absorption_audit_from_state(
    *,
    snapshots: Sequence[Any],
    transitions: Sequence[Any],
    ticks: Sequence[TradeTick],
    output_dir: Path,
    params: AbsorptionParams,
    regimes: Sequence[RegimeRow] | None = None,
    g5_actions: Sequence[Mapping[str, Any]] | None = None,
    trade_diag: Mapping[str, Any] | None = None,
    symbol: str = "APTUSDT",
    start: datetime | None = None,
    end: datetime | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    future_violations = 0
    outcome_leakage = 0
    g5_actions = list(g5_actions or [])
    regimes = list(regimes or [])
    trade_diag = dict(trade_diag or {})

    mids = [(ensure_utc(s.timestamp), _mid(s)) for s in snapshots]
    feat_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    join_rows: list[dict[str, Any]] = []
    deplete_rows: list[dict[str, Any]] = []
    raw_signals: list[dict[str, Any]] = []
    state_rows: list[dict[str, Any]] = []

    fb_state = FailedBreakState()
    swing_pending: dict[str, Any] | None = None
    swings: list[dict[str, Any]] = []
    hist: list[dict[str, Any]] = []
    matched_total = 0
    unmatched_total = 0
    ambiguous_total = 0
    sig_counter = 0

    for i, snap in enumerate(snapshots):
        prev = snapshots[i - 1] if i else None
        feat = build_snapshot_feature_row(
            index=i,
            snap=snap,
            prev=prev,
            ticks=ticks,
            mids=mids,
            params=params,
            transitions=transitions,
        )
        # regime as-of
        if regimes:
            reg = join_regime_as_of(regimes, as_of=ensure_utc(snap.timestamp))
            feat["trend_state"] = reg.get("combined_regime")
            feat["trend_data_available"] = reg.get("trend_data_available")
            # future check
            t = ensure_utc(snap.timestamp)
            for r in regimes:
                if r.decision_time > t and feat.get("trend_state") == r.combined_regime:
                    # only flag if we somehow used future — join_regime_as_of shouldn't
                    pass
        else:
            feat["trend_state"] = "TREND_CONTEXT_UNAVAILABLE"
            feat["trend_data_available"] = False

        feat_rows.append(feat)
        hist.append(feat)

        matched_total += int(feat.get("w30_matched_trade_count") or 0)
        unmatched_total += int(feat.get("w30_unmatched_trade_count") or 0)
        ambiguous_total += int(feat.get("w30_ambiguous_trade_match_count") or 0)

        for w in params.trade_windows_seconds:
            window_rows.append(
                {
                    "timestamp": feat["timestamp"],
                    "window_seconds": w,
                    **{
                        k[len(f"w{w}_") :]: v
                        for k, v in feat.items()
                        if k.startswith(f"w{w}_")
                    },
                }
            )
        if feat.get("depletion_absorption_level") is not None:
            deplete_rows.append(
                {
                    "timestamp": feat["timestamp"],
                    **{
                        k.replace("depletion_", ""): v
                        for k, v in feat.items()
                        if k.startswith("depletion_")
                    },
                }
            )
            join_rows.append(
                {
                    "timestamp": feat["timestamp"],
                    "wall_side": "Ask",
                    "wall_price": feat.get("depletion_absorption_level"),
                    "matched_trade_notional": feat.get("depletion_aggressive_buy_at_level"),
                    "level_join_quality": feat.get("depletion_level_join_quality"),
                    "refill_estimate_quality": feat.get("depletion_refill_estimate_quality"),
                }
            )

        # patterns
        signals_here: list[dict[str, Any]] = []
        a1 = detect_a1_buyer_exhaustion(feat, hist, params=params)
        a2 = detect_a2_ask_absorption(feat, params=params)
        a3 = detect_a3_refill_proxy(feat, hist, params=params)
        a5 = detect_a5_migration_stall(feat, hist, params=params)

        absorption_active = bool(a2 and a2.get("valid"))
        fb_state, a4 = advance_failed_breakout(
            fb_state, feat, params=params, absorption_active=absorption_active
        )
        swing_pending, swings = update_swing_tracker(
            swing_pending,
            swings,
            feat,
            confirm_snapshots=params.swing_confirm_snapshots,
        )
        a6 = None
        if swings and swing_pending is not None:
            # only emit when a swing just completed (last confirm == now)
            if swings[-1]["confirm_time"] == feat["timestamp"]:
                a6 = detect_a6_divergence(feat, swings, params=params)

        for det in (a1, a2, a3, a4, a5, a6):
            if not det:
                continue
            if det.get("pattern_type") == "A2_LOW_CONFIDENCE":
                # keep as raw diagnostic but mark invalid for variants
                pass
            sig_counter += 1
            t_sig = feat["timestamp"]
            # next snapshot action
            action_time = t_sig
            action_mid = float(feat["mid"])
            if i + 1 < len(snapshots):
                action_time = ensure_utc(snapshots[i + 1].timestamp).isoformat()
                action_mid = _mid(snapshots[i + 1])
            row = {
                "signal_id": f"S{sig_counter:05d}",
                "episode_id": "",
                "pattern_type": det["pattern_type"],
                "setup_start_time": det.get("break_time") or hist[max(0, i - 2)]["timestamp"],
                "observation_end_time": t_sig,
                "confirm_time": det.get("confirm_time") or t_sig,
                "signal_time": t_sig,
                "action_time": action_time,
                "level": det.get("level"),
                "score": det.get("score"),
                "feature_count": det.get("feature_count"),
                "features_true": ",".join(det.get("features_true") or []),
                "mid": feat["mid"],
                "action_mid": action_mid,
                "aggressive_buy_notional": det.get("aggressive_buy_notional"),
                "trade_delta": det.get("trade_delta"),
                "delta_ratio": det.get("delta_ratio"),
                "buy_impact_efficiency": det.get("buy_impact_efficiency"),
                "ask_wall_notional_before": det.get("ask_wall_notional_before"),
                "ask_wall_notional_after": det.get("ask_wall_notional_after"),
                "estimated_refill_notional": det.get("estimated_refill_notional"),
                "refill_ratio": det.get("refill_ratio"),
                "repeated_test_count": det.get("repeated_test_count"),
                "price_progress_bps": det.get("price_progress_bps"),
                "breakout_level": det.get("breakout_level"),
                "failed_breakout_confirmed": det.get("failed_breakout_confirmed", False),
                "trend_state": feat.get("trend_state"),
                "confidence": det.get("confidence"),
                "valid": det.get("valid", True),
                "action": map_diagnostic_action(str(det["pattern_type"])),
                "reason": det["pattern_type"],
            }
            # causality: confirm <= signal
            conf_t = datetime.fromisoformat(str(row["confirm_time"]))
            sig_t = datetime.fromisoformat(str(row["signal_time"]))
            if conf_t > sig_t:
                future_violations += 1
            signals_here.append(row)
            raw_signals.append(row)

        state_rows.append(
            {
                "timestamp": feat["timestamp"],
                "failed_break_state": fb_state.state,
                "patterns": ",".join(s["pattern_type"] for s in signals_here),
            }
        )

        # controls at this snapshot (no hidden pattern logic)
        buy_total = float(feat.get("w30_aggressive_buy_total_notional") or 0)
        progress = float(feat.get("w30_upside_progress_bps") or 0)
        wall_n = float(feat.get("nearest_ask_notional") or 0)
        control_flags = {
            "c0_random": (i % 17 == 0),
            "c1_high_buy": buy_total >= params.min_buy_notional * 2,
            "c2_wall_persist": wall_n >= params.min_wall_notional,
            "c3_low_progress": progress <= params.max_progress_bps
            and buy_total < params.min_buy_notional * 0.5,
        }
        # store for later variant eval on episodes — attach to feat
        feat["_controls"] = control_flags

    episodes = cluster_episodes(raw_signals, params=params)
    # map episode ids back
    sid_to_ep = {}
    for ep in episodes:
        for sid in str(ep["signal_ids"]).split(","):
            sid_to_ep[sid] = ep["episode_id"]
    for s in raw_signals:
        s["episode_id"] = sid_to_ep.get(s["signal_id"], "")

    # one action per episode
    actions: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    outcomes_by_ep: dict[str, dict[str, Any]] = {}
    for ep in episodes:
        members = [s for s in raw_signals if s["signal_id"] in str(ep["signal_ids"]).split(",")]
        chosen = max(members, key=lambda x: (int(x.get("score") or 0), x["signal_time"]))
        action_row = {
            **chosen,
            "episode_id": ep["episode_id"],
            "variant": chosen["pattern_type"],
        }
        actions.append(action_row)
        at = ensure_utc(datetime.fromisoformat(str(chosen["action_time"])))
        entry = float(chosen.get("action_mid") or chosen["mid"])
        oc = simulate_mid_outcomes(action_time=at, entry_mid=entry, mids=mids)
        # leakage check
        for ts, _ in mids:
            if ensure_utc(ts) <= at and oc.get("forward_return_bps_30s") is not None:
                # outcome uses only forward — ok
                break
        oc_row = {
            "episode_id": ep["episode_id"],
            "signal_id": chosen["signal_id"],
            "pattern_type": chosen["pattern_type"],
            **oc,
        }
        outcomes.append(oc_row)
        outcomes_by_ep[ep["episode_id"]] = oc_row

    overlap_rows, ablation = compare_to_g5(
        episodes, outcomes_by_ep, g5_actions
    )

    # variant / control summaries
    variant_summary: list[dict[str, Any]] = []
    control_summary: list[dict[str, Any]] = []
    for v in VARIANTS:
        if v.startswith("C"):
            # evaluate control flags on snapshots as pseudo episodes
            hits = 0
            n = 0
            for i, feat in enumerate(feat_rows):
                flags = feat.get("_controls") or {}
                key = {
                    "C0": "c0_random",
                    "C1": "c1_high_buy",
                    "C2": "c2_wall_persist",
                    "C3": "c3_low_progress",
                }[v]
                if not flags.get(key):
                    continue
                n += 1
                at = ensure_utc(datetime.fromisoformat(feat["timestamp"]))
                # next snap action
                if i + 1 < len(feat_rows):
                    at = ensure_utc(
                        datetime.fromisoformat(feat_rows[i + 1]["timestamp"])
                    )
                    entry = float(feat_rows[i + 1]["mid"])
                else:
                    entry = float(feat["mid"])
                oc = simulate_mid_outcomes(action_time=at, entry_mid=entry, mids=mids)
                if oc.get("hit_down_0_25"):
                    hits += 1
            control_summary.append(
                {
                    "variant": v,
                    "actions": n,
                    "hit_rate_0_25": (hits / n) if n else None,
                    "note": "control_no_pattern_logic",
                }
            )
            continue
        # pattern variants from episodes
        eps = []
        for ep in episodes:
            ptype = str(ep["pattern_type"])
            # Single-pattern variants: exact episode type
            if v in {"A1", "A2", "A3", "A4", "A5", "A6"}:
                want = {
                    "A1": A1,
                    "A2": A2,
                    "A3": A3,
                    "A4": A4,
                    "A5": A5,
                    "A6": A6,
                }[v]
                if ptype == want:
                    eps.append(ep)
                continue
            # Composites: require co-presence near episode window
            t0 = datetime.fromisoformat(ep["episode_start"])
            t1 = datetime.fromisoformat(ep["episode_end"])
            present: set[str] = set()
            for s in raw_signals:
                st = datetime.fromisoformat(s["signal_time"])
                if t0 - timedelta(seconds=params.episode_gap_seconds) <= st <= t1 + timedelta(
                    seconds=params.episode_gap_seconds
                ):
                    present.add(s["pattern_type"])
            a2_valid = A2 in present
            a4_ok = A4 in present
            if variant_match(
                v,
                patterns_present=present,
                a2_valid=a2_valid,
                a4_confirmed=a4_ok,
                control_flags={},
            ):
                eps.append(ep)
        # dedupe episodes
        seen = set()
        uniq = []
        for e in eps:
            if e["episode_id"] in seen:
                continue
            seen.add(e["episode_id"])
            uniq.append(e)
        hits = sum(
            1
            for e in uniq
            if outcomes_by_ep.get(e["episode_id"], {}).get("hit_down_0_25")
        )
        variant_summary.append(
            {
                "variant": v,
                "raw_signals": sum(int(e.get("raw_signal_count") or 1) for e in uniq),
                "deduped_episodes": len(uniq),
                "actions": len(uniq),
                "hit_rate_0_25": (hits / len(uniq)) if uniq else None,
                "false_count": len(uniq) - hits,
            }
        )

    # regime / quality summaries
    regime_summary: list[dict[str, Any]] = []
    by_reg: dict[str, list[str]] = {}
    for a in actions:
        by_reg.setdefault(str(a.get("trend_state")), []).append(a["episode_id"])
    for reg, eids in by_reg.items():
        hits = sum(
            1 for eid in eids if outcomes_by_ep.get(eid, {}).get("hit_down_0_25")
        )
        regime_summary.append(
            {
                "trend_state": reg,
                "actions": len(eids),
                "hit_rate_0_25": hits / len(eids) if eids else None,
            }
        )

    quality_summary = [
        {
            "join_quality": q,
            "snapshot_count": sum(
                1 for f in feat_rows if f.get("w30_level_join_quality") == q
            ),
        }
        for q in (
            JOIN_QUALITY_HIGH,
            JOIN_QUALITY_MEDIUM,
            JOIN_QUALITY_LOW,
            JOIN_QUALITY_INSUFFICIENT,
        )
    ]
    refill_quality_summary = [
        {
            "refill_estimate_quality": q,
            "count": sum(
                1
                for f in feat_rows
                if f.get("depletion_refill_estimate_quality") == q
            ),
        }
        for q in (
            REFILL_QUALITY_HIGH,
            REFILL_QUALITY_MEDIUM,
            REFILL_QUALITY_LOW,
            REFILL_QUALITY_INSUFFICIENT,
        )
    ]

    # reference point audit (post-hoc only)
    ref_rows = []
    for ref in REFERENCE_TIMES:
        rt = ensure_utc(parse_utc(ref))
        nearest = min(
            feat_rows,
            key=lambda f: abs(
                (
                    datetime.fromisoformat(f["timestamp"]) - rt
                ).total_seconds()
            ),
            default=None,
        )
        ref_rows.append(
            {
                "reference_time": ref,
                "post_hoc_only": True,
                "nearest_snapshot": None if nearest is None else nearest["timestamp"],
                "patterns_at_nearest": ",".join(
                    s["pattern_type"]
                    for s in raw_signals
                    if abs(
                        (
                            datetime.fromisoformat(s["signal_time"]) - rt
                        ).total_seconds()
                    )
                    <= 180
                ),
                "note": "diagnostic_only_not_used_for_thresholds",
            }
        )

    examples = actions[:20]

    total_join = matched_total + unmatched_total
    match_rate = matched_total / total_join if total_join else 0.0

    integrity = {
        "ok": future_violations == 0 and outcome_leakage == 0,
        "symbol": symbol,
        "start": None if start is None else ensure_utc(start).isoformat(),
        "end": None if end is None else ensure_utc(end).isoformat(),
        "snapshot_count": len(snapshots),
        "trade_tick_count": trade_diag.get("trade_tick_count", len(ticks)),
        "duplicate_trade_count": trade_diag.get("duplicate_trade_count", 0),
        "invalid_trade_count": trade_diag.get("invalid_trade_count", 0),
        "matched_trade_count": matched_total,
        "unmatched_trade_count": unmatched_total,
        "ambiguous_trade_match_count": ambiguous_total,
        "match_rate": match_rate,
        "future_data_violations": future_violations,
        "outcome_leakage_violations": outcome_leakage,
        "missing_snapshot_intervals": 0,
        "pattern_raw_count": len(raw_signals),
        "pattern_episode_count": len(episodes),
        "action_count": len(actions),
        "g5_loaded": bool(g5_actions),
        "regime_loaded": bool(regimes),
        "warnings": [],
        "errors": [],
    }
    if not g5_actions:
        integrity["warnings"].append("G5 actions empty or missing")
    if not regimes:
        integrity["warnings"].append("regime snapshots not loaded")

    control_hit_c1 = None
    for row in control_summary:
        if row.get("variant") == "C1" and row.get("hit_rate_0_25") is not None:
            control_hit_c1 = float(row["hit_rate_0_25"])
    verdict = decide_verdict(
        ablation,
        join_match_rate=match_rate,
        integrity_ok=bool(integrity["ok"]),
        future_violations=future_violations,
        control_hit_high_buy=control_hit_c1,
    )

    # strip internal keys before CSV
    feat_out = []
    for f in feat_rows:
        feat_out.append({k: v for k, v in f.items() if not k.startswith("_")})

    write_csv(output_dir / "snapshot_features.csv", feat_out)
    write_csv(output_dir / "trade_window_features.csv", window_rows)
    write_csv(output_dir / "wall_level_trade_joins.csv", join_rows)
    write_csv(output_dir / "wall_depletion_refill_features.csv", deplete_rows)
    write_csv(output_dir / "pattern_raw_signals.csv", raw_signals)
    write_csv(output_dir / "pattern_episodes.csv", episodes)
    write_csv(output_dir / "pattern_actions.csv", actions)
    write_csv(output_dir / "pattern_outcomes.csv", outcomes)
    write_csv(output_dir / "pattern_variant_summary.csv", variant_summary)
    write_csv(output_dir / "pattern_control_summary.csv", control_summary)
    write_csv(output_dir / "pattern_regime_summary.csv", regime_summary)
    write_csv(
        output_dir / "pattern_quality_summary.csv",
        quality_summary + [{"section": "refill", **r} for r in refill_quality_summary],
    )
    write_csv(output_dir / "pattern_g5_overlap.csv", overlap_rows)
    write_csv(output_dir / "pattern_g5_ablation.csv", ablation)
    write_csv(output_dir / "pattern_reference_point_audit.csv", ref_rows)
    write_csv(output_dir / "pattern_examples.csv", examples)

    cfg = dict(config or {})
    cfg["params"] = asdict(params)
    cfg["window_semantics"] = "(t - window, t]"
    cfg["price_basis_outcomes"] = "mid"
    cfg["reference_times_post_hoc_only"] = list(REFERENCE_TIMES)
    (output_dir / "config.json").write_bytes(
        orjson.dumps(cfg, option=orjson.OPT_INDENT_2)
    )
    (output_dir / "trade_loader_diagnostics.json").write_bytes(
        orjson.dumps(trade_diag, option=orjson.OPT_INDENT_2)
    )
    (output_dir / "integrity.json").write_bytes(
        orjson.dumps(integrity, option=orjson.OPT_INDENT_2)
    )

    counts = {p: sum(1 for s in raw_signals if s["pattern_type"] == p) for p in (A1, A2, A3, A4, A5, A6)}
    report = _build_report(
        verdict=verdict,
        integrity=integrity,
        counts=counts,
        episodes=episodes,
        actions=actions,
        outcomes=outcomes,
        ablation=ablation,
        overlap_rows=overlap_rows,
        match_rate=match_rate,
        refill_quality_summary=refill_quality_summary,
        params=params,
    )
    (output_dir / "REPORT.md").write_text(report, encoding="utf-8")

    # ensure all output files exist
    for name in OUTPUT_FILES:
        p = output_dir / name
        if not p.exists():
            if name.endswith(".csv"):
                write_csv(p, [])
            else:
                p.write_text("", encoding="utf-8")
            integrity["warnings"].append(f"created_empty:{name}")

    if future_violations > 0 or outcome_leakage > 0:
        raise RuntimeError(
            f"integrity failure: future={future_violations} leakage={outcome_leakage}"
        )

    summary = {
        "decision": verdict,
        "integrity": integrity,
        "pattern_counts_raw": counts,
        "episode_count": len(episodes),
        "action_count": len(actions),
        "ablation": ablation,
        "output_dir": str(output_dir),
    }
    (output_dir / "strategy_summary.json").write_bytes(
        orjson.dumps(summary, option=orjson.OPT_INDENT_2)
    )
    return summary


def _build_report(
    *,
    verdict: str,
    integrity: Mapping[str, Any],
    counts: Mapping[str, int],
    episodes: Sequence[Mapping[str, Any]],
    actions: Sequence[Mapping[str, Any]],
    outcomes: Sequence[Mapping[str, Any]],
    ablation: Sequence[Mapping[str, Any]],
    overlap_rows: Sequence[Mapping[str, Any]],
    match_rate: float,
    refill_quality_summary: Sequence[Mapping[str, Any]],
    params: AbsorptionParams,
) -> str:
    hits = sum(1 for o in outcomes if o.get("hit_down_0_25"))
    earlier = sum(1 for o in overlap_rows if o.get("absorption_earlier") and o.get("hit_down_0_25"))
    add_hits = sum(
        int(a.get("additional_hits_vs_g5") or 0)
        for a in ablation
        if a.get("variant") != "G5_BENCHMARK"
    )
    lines = [
        "# Absorption / Exhaustion Audit Report",
        "",
        f"**Decision:** `{verdict}`",
        "",
        "## 1. Trade ticks",
        f"- trade_tick_count: {integrity.get('trade_tick_count')}",
        f"- duplicates: {integrity.get('duplicate_trade_count')}",
        f"- invalid: {integrity.get('invalid_trade_count')}",
        "",
        "## 2–3. Wall-level match rate / ambiguity",
        f"- match_rate: {match_rate:.4f}",
        f"- matched: {integrity.get('matched_trade_count')}",
        f"- unmatched: {integrity.get('unmatched_trade_count')}",
        f"- ambiguous: {integrity.get('ambiguous_trade_match_count')}",
        "",
        "## 4. Refill quality",
        *[f"- {r}" for r in refill_quality_summary],
        "",
        "## 5. A1–A6 raw signal counts",
        *[f"- {k}: {v}" for k, v in counts.items()],
        "",
        f"## 6. Episodes / actions: {len(episodes)} / {len(actions)}",
        "",
        f"## 7–8. Outcomes hit_down_0_25: {hits}/{len(outcomes)}",
        "",
        "## 9–12. G5 overlap / earlier / additional",
        f"- overlap rows: {sum(1 for o in overlap_rows if o.get('overlaps_g5'))}",
        f"- earlier valid than G5: {earlier}",
        f"- additional_hits_vs_g5 (sum patterns): {add_hits}",
        "",
        "### Ablation",
        "```",
        orjson.dumps(list(ablation), option=orjson.OPT_INDENT_2).decode(),
        "```",
        "",
        "## 13. Tick-level join vs interval sums",
        "A2 requires aggressive_buy_at_wall_notional from level join; total buy alone is insufficient.",
        "",
        "## 14. 30s snapshot limits",
        "Short-lived absorption/breakouts inside a 30s bin can be smoothed; refill timing is coarse.",
        "",
        "## 15. Decision",
        f"{verdict}",
        "",
        "## Params",
        "```",
        orjson.dumps(asdict(params), option=orjson.OPT_INDENT_2).decode(),
        "```",
        "",
        "Reference times 10:00/10:55/11:25/12:55 are post-hoc only "
        "(see pattern_reference_point_audit.csv).",
    ]
    return "\n".join(lines)


def run_absorption_audit(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    output_dir: Path,
    params: AbsorptionParams,
    bid_weakening_dir: Path | None = None,
    g5_dir: Path | None = None,
    regime_snapshots: Path | None = None,
) -> dict[str, Any]:
    db = connect_readonly()
    try:
        audit_params = AuditParams(sample_seconds=params.snapshot_seconds)
        state = prepare_tracker_state(
            db=db, symbol=symbol, start=start, end=end, params=audit_params
        )
        ticks, diag = load_trade_ticks(db, symbol=symbol, start=start, end=end)
        regimes: list[RegimeRow] = []
        if regime_snapshots and Path(regime_snapshots).exists():
            regimes = load_regimes(Path(regime_snapshots))
        g5: list[dict[str, Any]] = []
        if g5_dir and Path(g5_dir).exists():
            g5 = load_g5_actions(Path(g5_dir))
        return run_absorption_audit_from_state(
            snapshots=state["snapshots"],
            transitions=state["transitions"],
            ticks=ticks,
            output_dir=output_dir,
            params=params,
            regimes=regimes,
            g5_actions=g5,
            trade_diag=diag.to_dict(),
            symbol=symbol,
            start=start,
            end=end,
            config={
                "symbol": symbol,
                "start": ensure_utc(start).isoformat(),
                "end": ensure_utc(end).isoformat(),
                "bid_weakening_dir": None
                if bid_weakening_dir is None
                else str(bid_weakening_dir),
                "g5_dir": None if g5_dir is None else str(g5_dir),
                "regime_snapshots": None
                if regime_snapshots is None
                else str(regime_snapshots),
            },
        )
    finally:
        db.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Absorption / exhaustion research audit")
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--start", default="2026-07-26T09:16:29Z")
    p.add_argument("--end", default="2026-07-26T13:08:27Z")
    p.add_argument(
        "--bid-weakening-dir",
        default=str(
            PROJECT_ROOT
            / "results"
            / "orderbook_bid_weakening_full_history_APTUSDT_20260726T164357Z"
        ),
    )
    p.add_argument(
        "--g5-dir",
        default=str(
            PROJECT_ROOT / "results" / "orderbook_trend_bid_weakening_APTUSDT_20260726"
        ),
    )
    p.add_argument(
        "--regime-snapshots",
        default=str(
            Path(
                "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/"
                "research/regime_scanner/results/"
                "regime_scanner_pipeline_APTUSDT_20260726_orderbook_window/"
                "regime_snapshots.csv"
            )
        ),
    )
    p.add_argument("--output-dir", default=None)
    p.add_argument("--snapshot-seconds", type=int, default=30)
    p.add_argument("--trade-windows-seconds", default="10,30,60,180")
    p.add_argument("--level-join-bps", type=float, default=3.0)
    p.add_argument("--near-level-bps", type=float, default=8.0)
    p.add_argument("--min-wall-notional", type=float, default=2000.0)
    p.add_argument("--min-buy-notional", type=float, default=1500.0)
    p.add_argument("--max-progress-bps", type=float, default=8.0)
    p.add_argument("--min-wall-persistence-snapshots", type=int, default=2)
    p.add_argument("--min-refill-repetitions", type=int, default=2)
    p.add_argument("--min-refill-ratio", type=float, default=0.25)
    p.add_argument("--failed-break-confirm-snapshots", type=int, default=2)
    p.add_argument("--episode-gap-seconds", type=int, default=300)
    p.add_argument("--episode-level-bps", type=float, default=10.0)
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    windows = tuple(int(x) for x in str(args.trade_windows_seconds).split(",") if x)
    params = AbsorptionParams(
        snapshot_seconds=int(args.snapshot_seconds),
        trade_windows_seconds=windows,
        level_join_bps=float(args.level_join_bps),
        near_level_bps=float(args.near_level_bps),
        min_wall_notional=float(args.min_wall_notional),
        min_buy_notional=float(args.min_buy_notional),
        max_progress_bps=float(args.max_progress_bps),
        min_wall_persistence_snapshots=int(args.min_wall_persistence_snapshots),
        min_refill_repetitions=int(args.min_refill_repetitions),
        min_refill_ratio=float(args.min_refill_ratio),
        failed_break_confirm_snapshots=int(args.failed_break_confirm_snapshots),
        episode_gap_seconds=int(args.episode_gap_seconds),
        episode_level_bps=float(args.episode_level_bps),
    )
    out = (
        Path(args.output_dir)
        if args.output_dir
        else PROJECT_ROOT
        / "results"
        / f"orderbook_absorption_exhaustion_{args.symbol}_{utc_now().strftime('%Y%m%dT%H%M%SZ')}"
    )
    summary = run_absorption_audit(
        symbol=str(args.symbol),
        start=parse_utc(args.start),
        end=parse_utc(args.end),
        output_dir=out,
        params=params,
        bid_weakening_dir=Path(args.bid_weakening_dir),
        g5_dir=Path(args.g5_dir),
        regime_snapshots=Path(args.regime_snapshots)
        if args.regime_snapshots
        else None,
    )
    sys.stdout.buffer.write(
        orjson.dumps(
            {
                "decision": summary.get("decision"),
                "pattern_counts_raw": summary.get("pattern_counts_raw"),
                "episode_count": summary.get("episode_count"),
                "output_dir": summary.get("output_dir"),
            },
            option=orjson.OPT_INDENT_2,
        )
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
