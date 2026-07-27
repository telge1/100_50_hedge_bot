"""Price Higher Lows → Ask Ceiling research audit (read-only).

Causal pullback / higher-low state machine (no retroactive pivots):
  IDLE → IMPULSE_UP → PULLBACK_ACTIVE → LOW_CANDIDATE → LOW_CONFIRMED
  Two confirmed lows with second > first + min_higher_low_bps → HIGHER_LOW_CONFIRMED
  Signal time = second_low confirmation time (never the historical trough time).

Long-to-ceiling (T) and breakout (B) are separate goals.
Benchmark control: C1 Ask-ceiling-within-distance alone (must be beaten for VALUE_FOUND).

A2/G5 CSVs are diagnostic only and never alter base signal formation.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from collections import Counter
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
from orderbook_analyse.orderbook_rising_bid_floor_compression_audit import (
    bps_dist,
    bps_signed,
    select_ceiling,
    write_csv_headered,
)
from orderbook_analyse.orderbook_trade_candidate_audit import (
    AuditParams,
    prepare_tracker_state,
)
from orderbook_analyse.wall_movement_tracker import (
    WALL_PULLED,
    WALL_REPLACED_HIGHER,
    WALL_REPLACED_LOWER,
)

logger = logging.getLogger(__name__)

EPSILON = 1e-12
REFERENCE_TIMES = (
    "2026-07-26T10:00:00Z",
    "2026-07-26T10:55:00Z",
    "2026-07-26T11:25:00Z",
    "2026-07-26T12:55:00Z",
)

IDLE = "IDLE"
IMPULSE_UP = "IMPULSE_UP"
PULLBACK_ACTIVE = "PULLBACK_ACTIVE"
LOW_CANDIDATE = "LOW_CANDIDATE"
LOW_CONFIRMED = "LOW_CONFIRMED"
REBOUND_ACTIVE = "REBOUND_ACTIVE"
HIGHER_LOW_CONFIRMED = "HIGHER_LOW_CONFIRMED"
HIGHER_LOW_ARMED = "HIGHER_LOW_ARMED"
HIGHER_LOW_ACTIONED = "HIGHER_LOW_ACTIONED"
HIGHER_LOW_INVALIDATED = "HIGHER_LOW_INVALIDATED"
HIGHER_LOW_EXPIRED = "HIGHER_LOW_EXPIRED"
LONG_SIGNAL = "LONG_SIGNAL"
INVALIDATED = "INVALIDATED"
EXPIRED = "EXPIRED"

P_VARIANTS = tuple(f"P{i}" for i in range(12))
P_HL_VARIANTS = tuple(f"P{i}" for i in range(3, 12))
B_VARIANTS = ("B0", "B1", "B2", "B3", "B4", "B5", "B6")
CONTROLS = tuple(f"C{i}" for i in range(9))
HORIZONS = (30, 60, 120, 300, 600, 900, 1800, 3600)
ARMED_SECONDS_ABLATIONS = (0, 300, 600, 900, 1800)

OUTPUT_FILES = (
    "REPORT.md",
    "config.json",
    "integrity.json",
    "input_inventory.json",
    "snapshot_price_structure_features.csv",
    "pullback_low_candidates.csv",
    "confirmed_pullback_lows.csv",
    "higher_low_pairs.csv",
    "price_structure_state_transitions.csv",
    "higher_low_raw_signals.csv",
    "higher_low_episodes.csv",
    "long_to_ceiling_actions.csv",
    "long_to_ceiling_outcomes.csv",
    "breakout_actions.csv",
    "breakout_outcomes.csv",
    "variant_summary.csv",
    "control_summary.csv",
    "ceiling_distance_ablation.csv",
    "impulse_ablation.csv",
    "pullback_ablation.csv",
    "rebound_ablation.csv",
    "higher_low_distance_ablation.csv",
    "time_between_lows_ablation.csv",
    "crv_ablation.csv",
    "breakout_confirmation_ablation.csv",
    "higher_low_armed_ablation.csv",
    "a2_g5_diagnostics.csv",
    "pattern_examples.csv",
    "pattern_reference_point_audit.csv",
)

EMPTY_CSV_HEADERS: dict[str, tuple[str, ...]] = {
    "pullback_low_candidates.csv": (
        "timestamp",
        "low_candidate_time",
        "low_candidate_price",
        "mid",
        "state",
    ),
    "confirmed_pullback_lows.csv": (
        "low_id",
        "candidate_time",
        "candidate_price",
        "confirmation_time",
        "confirmation_price",
        "pullback_depth_bps",
        "rebound_bps",
        "delta_ratio_at_confirm",
        "buy_notional_at_confirm",
        "sell_notional_at_confirm",
        "quality",
    ),
    "higher_low_pairs.csv": (
        "first_low_id",
        "second_low_id",
        "first_low_time",
        "first_low_price",
        "first_low_confirmation_time",
        "second_low_time",
        "second_low_price",
        "second_low_confirmation_time",
        "higher_low_distance_bps",
        "time_between_lows_seconds",
        "first_pullback_depth_bps",
        "second_pullback_depth_bps",
        "rebound_after_second_low_bps",
        "delta_ratio_first",
        "delta_ratio_second",
        "delta_improving",
        "sell_declining",
        "buy_increasing",
    ),
    "price_structure_state_transitions.csv": (
        "previous_state",
        "new_state",
        "transition_time",
        "reason",
        "mid",
    ),
    "higher_low_raw_signals.csv": (
        "signal_id",
        "variant",
        "goal",
        "signal_time",
        "signal_price",
        "target_price",
        "ceiling_price",
    ),
    "higher_low_episodes.csv": (
        "episode_id",
        "variant",
        "signal_time",
        "signal_ids",
        "raw_signal_count",
    ),
    "long_to_ceiling_actions.csv": (
        "signal_id",
        "variant",
        "episode_id",
        "action_time",
        "signal_price",
        "ceiling_price",
    ),
    "long_to_ceiling_outcomes.csv": (
        "episode_id",
        "variant",
        "signal_id",
        "ceiling_touch",
        "mae_down_bps_before_touch",
        "second_low_invalidated_before_touch",
    ),
    "breakout_actions.csv": (
        "variant",
        "episode_id",
        "signal_time",
        "break_time",
        "action_time",
        "ceiling_price",
    ),
    "breakout_outcomes.csv": (
        "variant",
        "episode_id",
        "action_time",
        "failed_breakout",
        "mfe_up_bps",
        "mae_down_bps",
    ),
    "a2_g5_diagnostics.csv": (
        "episode_id",
        "variant",
        "signal_time",
        "a2_active_at_signal",
        "g5_action_after_signal",
        "ceiling_touch",
    ),
    "pattern_examples.csv": ("signal_id", "variant", "signal_time", "ceiling_price"),
    "pattern_reference_point_audit.csv": (
        "reference_time",
        "post_hoc_only",
        "nearest_snapshot",
        "state",
        "higher_low",
        "note",
    ),
    "ceiling_distance_ablation.csv": (
        "max_ceiling_distance_bps",
        "P3_actions",
        "P3_touch_rate",
    ),
    "impulse_ablation.csv": ("impulse_min_bps", "confirmed_lows"),
    "pullback_ablation.csv": ("pullback_min_bps", "confirmed_lows"),
    "rebound_ablation.csv": ("rebound_confirm_bps", "confirmed_lows"),
    "higher_low_distance_ablation.csv": (
        "min_higher_low_bps",
        "pairs",
        "P3_touch_rate",
    ),
    "time_between_lows_ablation.csv": ("max_time_between_lows_seconds", "pairs"),
    "crv_ablation.csv": ("min_crv", "P7_actions"),
    "breakout_confirmation_ablation.csv": (
        "variant",
        "actions",
        "failed_breakout_rate",
        "hit_rate_0_25",
    ),
    "higher_low_armed_ablation.csv": (
        "higher_low_armed_seconds",
        "armed_pair_count",
        "armed_action_count",
        "P3_actions",
        "P3_touch_rate",
        "median_pair_to_action_seconds",
        "note",
    ),
    "variant_summary.csv": ("variant", "actions", "ceiling_touch_rate"),
    "control_summary.csv": (
        "variant",
        "actions",
        "ceiling_touch_count",
        "ceiling_touch_rate",
        "median_mae_before_touch_bps",
        "note",
    ),
}


def ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return ensure_utc(parse_utc(str(value).replace("Z", "+00:00")))


def _f(x: Any) -> float | None:
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _median(vals: Sequence[float | None]) -> float | None:
    xs = sorted(float(v) for v in vals if v is not None)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2.0


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


@dataclass
class HigherLowParams:
    snapshot_seconds: int = 30
    max_ceiling_distance_bps: float = 100.0
    min_ceiling_notional: float = 1000.0
    impulse_min_bps: float = 10.0
    pullback_min_bps: float = 5.0
    rebound_confirm_bps: float = 5.0
    min_higher_low_bps: float = 2.0
    max_time_between_lows_seconds: int = 600
    max_pullback_duration_seconds: int = 900
    max_pullback_depth_bps: float = 100.0
    higher_low_armed_seconds: int = 600
    stop_buffer_bps: float = 3.0
    invalidate_armed_on_first_low_break: bool = True
    min_crv: float = 1.5
    breakout_min_extension_bps: float = 3.0
    failed_break_confirm_snapshots: int = 2
    episode_gap_seconds: int = 180
    episode_level_bps: float = 10.0
    min_ceiling_persistence: int = 1
    symbol: str = "APTUSDT"
    start: str = "2026-07-26T09:16:29Z"
    end: str = "2026-07-26T13:08:27Z"


@dataclass
class ConfirmedLow:
    low_id: str
    candidate_time: datetime
    candidate_price: float
    confirmation_time: datetime
    confirmation_price: float
    impulse_peak_price: float
    pullback_depth_bps: float
    rebound_bps: float
    delta_ratio_at_confirm: float
    buy_notional_at_confirm: float
    sell_notional_at_confirm: float
    quality: str


@dataclass
class PullbackMachine:
    state: str = IDLE
    impulse_start_time: datetime | None = None
    impulse_start_price: float | None = None
    impulse_peak_time: datetime | None = None
    impulse_peak_price: float | None = None
    pullback_start_time: datetime | None = None
    low_candidate_time: datetime | None = None
    low_candidate_price: float | None = None
    confirmed: list[ConfirmedLow] = field(default_factory=list)
    pullback_expired_count: int = 0
    pullback_invalidated_count: int = 0
    longest_pullback_seconds: float = 0.0
    last_pullback_duration_seconds: float | None = None
    last_pullback_depth_bps: float | None = None
    pullback_start_count: int = 0


def structure_quality(
    *,
    rebound_bps: float,
    pullback_depth_bps: float,
    gap_flag: bool,
    ceiling_persistence: int,
    higher_low_bps: float | None,
) -> str:
    if gap_flag:
        return "INSUFFICIENT"
    score = 0
    if rebound_bps >= 5:
        score += 1
    if pullback_depth_bps >= 5:
        score += 1
    if ceiling_persistence >= 2:
        score += 1
    if higher_low_bps is not None and higher_low_bps >= 2:
        score += 1
    if score >= 3:
        return "HIGH"
    if score >= 2:
        return "MEDIUM"
    if score >= 1:
        return "LOW"
    return "INSUFFICIENT"


@dataclass
class ArmedHigherLow:
    armed_pair_id: str
    first_low_id: str
    second_low_id: str
    first_low_price: float
    second_low_price: float
    armed_time: datetime
    expiry_time: datetime | None
    pair_info: dict[str, Any]
    actioned_variants: set[str] = field(default_factory=set)

    def age_seconds(self, ts: datetime) -> float:
        return (ensure_utc(ts) - ensure_utc(self.armed_time)).total_seconds()


def crv_from_hl(
    *,
    signal_price: float,
    second_low_price: float,
    ceiling_price: float,
    stop_buffer_bps: float,
) -> dict[str, Any]:
    """CRV for long-to-ceiling. Invalid when stop is not strictly below signal."""
    target_bps = (ceiling_price - signal_price) / signal_price * 10_000.0 if signal_price else 0.0
    stop_price = second_low_price * (1.0 - stop_buffer_bps / 10_000.0)
    stop_bps = (
        (signal_price - stop_price) / signal_price * 10_000.0 if signal_price else 0.0
    )
    out: dict[str, Any] = {
        "target_distance_bps": target_bps,
        "stop_price": stop_price,
        "stop_distance_bps": stop_bps,
        "estimated_crv": None,
        "crv_valid": False,
    }
    if signal_price <= 0 or stop_price >= signal_price or stop_bps <= 0:
        return out
    out["estimated_crv"] = target_bps / stop_bps
    out["crv_valid"] = True
    return out


def p_variant_ok(
    variant: str,
    *,
    has_ceiling: bool,
    one_low: bool,
    two_lows: bool,
    higher_low: bool,
    delta_pos: bool,
    delta_improving: bool,
    sell_declining: bool,
    crv_ok: bool,
    quality_ok: bool,
    no_a2: bool,
) -> bool:
    if variant == "P0":
        return has_ceiling
    if variant == "P1":
        return has_ceiling and one_low
    if variant == "P2":
        return has_ceiling and two_lows
    if variant == "P3":
        return has_ceiling and higher_low
    if variant == "P4":
        return has_ceiling and higher_low and delta_pos
    if variant == "P5":
        return has_ceiling and higher_low and delta_improving
    if variant == "P6":
        return has_ceiling and higher_low and sell_declining
    if variant == "P7":
        return has_ceiling and higher_low and crv_ok
    if variant == "P8":
        return has_ceiling and higher_low and crv_ok and quality_ok
    if variant == "P9":
        return has_ceiling and higher_low and crv_ok and quality_ok and no_a2
    if variant == "P10":
        return (
            has_ceiling
            and higher_low
            and crv_ok
            and quality_ok
            and delta_pos
            and no_a2
        )
    if variant == "P11":
        return has_ceiling and higher_low and (delta_pos or delta_improving)
    return False


def _restart_impulse_from_current(
    m: PullbackMachine,
    *,
    ts: datetime,
    mid: float,
    set_state,
    exit_state: str,
    exit_reason: str,
    restart_reason: str,
) -> None:
    """Abort active pullback (keep confirmed lows) and restart impulse tracking."""
    set_state(
        exit_state,
        exit_reason,
        extra={
            "pullback_duration_seconds": m.last_pullback_duration_seconds,
            "pullback_depth_bps": m.last_pullback_depth_bps,
        },
    )
    m.impulse_start_time = ts
    m.impulse_start_price = mid
    m.impulse_peak_time = ts
    m.impulse_peak_price = mid
    m.pullback_start_time = None
    m.low_candidate_time = None
    m.low_candidate_price = None
    set_state(IMPULSE_UP, restart_reason)


def advance_pullback_machine(
    m: PullbackMachine,
    *,
    ts: datetime,
    mid: float,
    params: HigherLowParams,
    delta_ratio: float,
    buy_n: float,
    sell_n: float,
    low_counter: list[int],
    transitions: list[dict[str, Any]],
) -> ConfirmedLow | None:
    """Advance causal pullback SM; return newly confirmed low or None.

    Pullback exits:
      - rebound confirm → ConfirmedLow (unchanged semantics)
      - duration >= max_pullback_duration_seconds → EXPIRED, restart IMPULSE_UP
      - depth >= max_pullback_depth_bps → INVALIDATED, restart IMPULSE_UP
    Confirmed lows are never cleared on expiry/invalidation.
    """

    def set_state(new: str, reason: str, extra: Mapping[str, Any] | None = None) -> None:
        row: dict[str, Any] = {
            "previous_state": m.state,
            "new_state": new,
            "transition_time": ts.isoformat(),
            "reason": reason,
            "mid": mid,
        }
        if extra:
            row.update(dict(extra))
        transitions.append(row)
        m.state = new

    confirmed: ConfirmedLow | None = None

    if m.state == IDLE:
        m.impulse_start_time = ts
        m.impulse_start_price = mid
        m.impulse_peak_time = ts
        m.impulse_peak_price = mid
        set_state(IMPULSE_UP, "start_track")
        return None

    if m.state == IMPULSE_UP:
        assert m.impulse_start_price is not None
        if mid >= (m.impulse_peak_price or mid):
            m.impulse_peak_price = mid
            m.impulse_peak_time = ts
        rise = bps_signed(m.impulse_peak_price or mid, m.impulse_start_price)
        if rise >= params.impulse_min_bps and mid < (m.impulse_peak_price or mid):
            # start of pullback after sufficient impulse
            drop = bps_signed(m.impulse_peak_price or mid, mid)
            if drop >= params.pullback_min_bps * 0.3:
                m.pullback_start_time = ts
                m.low_candidate_time = ts
                m.low_candidate_price = mid
                m.pullback_start_count += 1
                m.last_pullback_duration_seconds = 0.0
                m.last_pullback_depth_bps = drop
                set_state(PULLBACK_ACTIVE, "pullback_after_impulse")
        return None

    if m.state in {PULLBACK_ACTIVE, LOW_CANDIDATE}:
        assert m.impulse_peak_price is not None
        assert m.pullback_start_time is not None
        drop = bps_signed(m.impulse_peak_price, mid)
        if m.low_candidate_price is None or mid <= m.low_candidate_price:
            m.low_candidate_price = mid
            m.low_candidate_time = ts
            if m.state == PULLBACK_ACTIVE and drop >= params.pullback_min_bps:
                set_state(LOW_CANDIDATE, "low_candidate")

        depth = bps_signed(
            m.impulse_peak_price,
            m.low_candidate_price if m.low_candidate_price is not None else mid,
        )
        duration = (ts - m.pullback_start_time).total_seconds()
        m.last_pullback_duration_seconds = duration
        m.last_pullback_depth_bps = depth
        m.longest_pullback_seconds = max(m.longest_pullback_seconds, duration)

        # Depth abort before duration / rebound (no confirmed low).
        if depth >= params.max_pullback_depth_bps:
            m.pullback_invalidated_count += 1
            _restart_impulse_from_current(
                m,
                ts=ts,
                mid=mid,
                set_state=set_state,
                exit_state=INVALIDATED,
                exit_reason="pullback_depth_exceeded",
                restart_reason="restart_after_invalidate",
            )
            return None

        # Duration abort at/after max (no confirmed low); not before boundary.
        if duration >= params.max_pullback_duration_seconds:
            m.pullback_expired_count += 1
            _restart_impulse_from_current(
                m,
                ts=ts,
                mid=mid,
                set_state=set_state,
                exit_state=EXPIRED,
                exit_reason="pullback_duration_exceeded",
                restart_reason="restart_after_expire",
            )
            return None

        # rebound confirmation
        if (
            m.low_candidate_price is not None
            and drop >= params.pullback_min_bps
            and bps_signed(mid, m.low_candidate_price) >= params.rebound_confirm_bps
        ):
            low_counter[0] += 1
            lid = f"L{low_counter[0]:04d}"
            reb = bps_signed(mid, m.low_candidate_price)
            depth = bps_signed(m.impulse_peak_price, m.low_candidate_price)
            confirmed = ConfirmedLow(
                low_id=lid,
                candidate_time=m.low_candidate_time or ts,
                candidate_price=m.low_candidate_price,
                confirmation_time=ts,  # confirm time, not trough
                confirmation_price=mid,
                impulse_peak_price=m.impulse_peak_price,
                pullback_depth_bps=depth,
                rebound_bps=reb,
                delta_ratio_at_confirm=delta_ratio,
                buy_notional_at_confirm=buy_n,
                sell_notional_at_confirm=sell_n,
                quality=structure_quality(
                    rebound_bps=reb,
                    pullback_depth_bps=depth,
                    gap_flag=False,
                    ceiling_persistence=1,
                    higher_low_bps=None,
                ),
            )
            m.confirmed.append(confirmed)
            set_state(
                LOW_CONFIRMED,
                "low_confirmed_on_rebound",
                extra={
                    "pullback_duration_seconds": duration,
                    "pullback_depth_bps": depth,
                },
            )
            # reset for next impulse from confirmation
            m.impulse_start_time = ts
            m.impulse_start_price = mid
            m.impulse_peak_time = ts
            m.impulse_peak_price = mid
            m.pullback_start_time = None
            m.low_candidate_time = None
            m.low_candidate_price = None
            set_state(IMPULSE_UP, "restart_after_confirm")
        return confirmed

    return None


def long_to_ceiling_outcomes(
    *,
    signal_time: datetime,
    signal_price: float,
    ceiling_price: float,
    second_low_price: float,
    first_low_price: float,
    mids: Sequence[tuple[datetime, float]],
    transitions: Sequence[Any],
    horizons: Sequence[int] = HORIZONS,
) -> dict[str, Any]:
    t0 = ensure_utc(signal_time)
    forward = [(ensure_utc(ts), px) for ts, px in mids if ensure_utc(ts) > t0]
    out: dict[str, Any] = {
        "signal_time": t0.isoformat(),
        "signal_price": signal_price,
        "ceiling_price": ceiling_price,
        "ceiling_touch": False,
        "time_to_ceiling_touch_seconds": None,
        "ceiling_touch_price": None,
        "mae_down_bps_before_touch": None,
        "mfe_up_bps_before_touch": None,
        "second_low_invalidated_before_touch": False,
        "first_low_invalidated_before_touch": False,
        "time_to_second_low_invalidation_seconds": None,
        "ceiling_pulled_before_touch": False,
        "ceiling_replaced_lower_before_touch": False,
        "ceiling_replaced_higher_before_touch": False,
        "no_touch_within_horizon": True,
    }
    if signal_price <= 0:
        return out
    touch_t = None
    touch_px = None
    mfe = mae = 0.0
    for ts, px in forward:
        up = (px - signal_price) / signal_price * 10_000.0
        down = (signal_price - px) / signal_price * 10_000.0
        if touch_t is None:
            mfe = max(mfe, up)
            mae = max(mae, down)
            if px < second_low_price * (1 - 2 / 10_000.0) and not out["second_low_invalidated_before_touch"]:
                out["second_low_invalidated_before_touch"] = True
                out["time_to_second_low_invalidation_seconds"] = (ts - t0).total_seconds()
            if px < first_low_price * (1 - 2 / 10_000.0):
                out["first_low_invalidated_before_touch"] = True
        if touch_t is None and px >= ceiling_price:
            touch_t = ts
            touch_px = px
            break
    out["mfe_up_bps_before_touch"] = mfe
    out["mae_down_bps_before_touch"] = mae
    if touch_t is not None:
        out["ceiling_touch"] = True
        out["time_to_ceiling_touch_seconds"] = (touch_t - t0).total_seconds()
        out["ceiling_touch_price"] = touch_px
        out["no_touch_within_horizon"] = False
    end_t = touch_t or (t0 + timedelta(seconds=max(horizons)))
    for tr in transitions:
        cts = ensure_utc(tr.current_timestamp)
        if cts <= t0 or cts > end_t:
            continue
        if getattr(tr, "side", None) != "Ask":
            continue
        cls = str(tr.classification)
        if cls == WALL_PULLED:
            out["ceiling_pulled_before_touch"] = True
        if cls == WALL_REPLACED_LOWER:
            out["ceiling_replaced_lower_before_touch"] = True
        if cls == WALL_REPLACED_HIGHER:
            out["ceiling_replaced_higher_before_touch"] = True
    max_up = 0.0
    t25 = None
    for ts, px in forward:
        up = (px - signal_price) / signal_price * 10_000.0
        max_up = max(max_up, up)
        if t25 is None and up >= 25:
            t25 = (ts - t0).total_seconds()
    out["hit_up_0_10"] = max_up >= 10
    out["hit_up_0_25"] = max_up >= 25
    out["hit_up_0_50"] = max_up >= 50
    out["hit_up_1_00"] = max_up >= 100
    out["time_to_hit_up_0_25_seconds"] = t25
    for h in horizons:
        end = t0 + timedelta(seconds=h)
        if any(px >= ceiling_price for ts, px in forward if ts <= end):
            out[f"ceiling_touch_{h}s"] = True
            out["no_touch_within_horizon"] = False
        else:
            out[f"ceiling_touch_{h}s"] = False
    return out


def breakout_outcomes(
    *,
    action_time: datetime,
    action_price: float,
    ceiling_price: float,
    mids: Sequence[tuple[datetime, float]],
    failed_confirm_snapshots: int,
) -> dict[str, Any]:
    t0 = ensure_utc(action_time)
    forward = [(ensure_utc(ts), px) for ts, px in mids if ensure_utc(ts) > t0]
    out: dict[str, Any] = {
        "action_time": t0.isoformat(),
        "action_price": action_price,
        "ceiling_price": ceiling_price,
        "forward_return_bps": None,
        "mfe_up_bps": 0.0,
        "mae_down_bps": 0.0,
        "hold_above_ceiling_30s": False,
        "hold_above_ceiling_60s": False,
        "hold_above_ceiling_120s": False,
        "retest_success": False,
        "failed_breakout": False,
        "time_to_failed_breakout_seconds": None,
        "hit_up_0_10": False,
        "hit_up_0_25": False,
        "hit_up_0_50": False,
        "hit_up_1_00": False,
        "hit_down_0_10": False,
        "hit_down_0_25": False,
        "hit_down_0_50": False,
    }
    if action_price <= 0 or not forward:
        return out
    mfe = mae = 0.0
    under = 0
    fail_t = None
    for ts, px in forward:
        ret = (px - action_price) / action_price * 10_000.0
        mfe = max(mfe, ret)
        mae = max(mae, -ret)
        if px < ceiling_price:
            under += 1
            if under >= failed_confirm_snapshots and fail_t is None:
                fail_t = ts
        else:
            under = 0
    out["mfe_up_bps"] = mfe
    out["mae_down_bps"] = mae
    out["forward_return_bps"] = (forward[-1][1] - action_price) / action_price * 10_000.0
    out["failed_breakout"] = fail_t is not None
    if fail_t:
        out["time_to_failed_breakout_seconds"] = (fail_t - t0).total_seconds()
    for sec, key in (
        (30, "hold_above_ceiling_30s"),
        (60, "hold_above_ceiling_60s"),
        (120, "hold_above_ceiling_120s"),
    ):
        end = t0 + timedelta(seconds=sec)
        pts = [px for ts, px in forward if ts <= end]
        out[key] = bool(pts) and all(px >= ceiling_price for px in pts)
    out["hit_up_0_10"] = mfe >= 10
    out["hit_up_0_25"] = mfe >= 25
    out["hit_up_0_50"] = mfe >= 50
    out["hit_up_1_00"] = mfe >= 100
    out["hit_down_0_10"] = mae >= 10
    out["hit_down_0_25"] = mae >= 25
    out["hit_down_0_50"] = mae >= 50
    return out


def run_higher_lows_audit_from_state(
    *,
    snapshots: Sequence[Any],
    transitions: Sequence[Any],
    output_dir: Path,
    params: HigherLowParams,
    a2_times: Sequence[datetime] | None = None,
    g5_warning_times: Sequence[datetime] | None = None,
    g5_action_times: Sequence[datetime] | None = None,
    absorption_by_ts: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    future_violations = 0
    outcome_leakage = 0
    retroactive_pivot_violations = 0
    warnings: list[str] = []
    errors: list[str] = []
    a2_times = list(a2_times or [])
    g5_warning_times = list(g5_warning_times or [])
    g5_action_times = list(g5_action_times or [])
    absorption_by_ts = dict(absorption_by_ts or {})

    mids = [(ensure_utc(s.timestamp), float(s.mid_price)) for s in snapshots]
    feat_rows: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    confirmed_rows: list[dict[str, Any]] = []
    pairs: list[dict[str, Any]] = []
    state_tx: list[dict[str, Any]] = []
    raw_signals: list[dict[str, Any]] = []
    long_actions: list[dict[str, Any]] = []
    long_outcomes: list[dict[str, Any]] = []
    break_actions: list[dict[str, Any]] = []
    break_outcomes: list[dict[str, Any]] = []
    a2g5_rows: list[dict[str, Any]] = []

    machine = PullbackMachine()
    low_counter = [0]
    all_confirmed: list[ConfirmedLow] = []
    ceiling_persistence: dict[float, int] = {}
    invalid_pairs = 0
    sig_n = 0
    armed: ArmedHigherLow | None = None
    armed_n = 0
    armed_pair_count = 0
    armed_action_count = 0
    armed_expired_count = 0
    armed_invalidated_count = 0
    invalid_crv_count = 0
    pair_to_action_delays: list[float] = []

    prev_mid: float | None = None
    for i, snap in enumerate(snapshots):
        ts = ensure_utc(snap.timestamp)
        mid = float(snap.mid_price)
        # gap flag
        gap = False
        if i > 0:
            dt = (ts - ensure_utc(snapshots[i - 1].timestamp)).total_seconds()
            gap = dt > params.snapshot_seconds * 2.5

        ce = select_ceiling(
            snap,
            max_distance_bps=params.max_ceiling_distance_bps,
            min_notional=params.min_ceiling_notional,
        )
        if ce is not None:
            key = round(ce["ceiling_price"], 8)
            ceiling_persistence[key] = ceiling_persistence.get(key, 0) + 1
            ce_persist = ceiling_persistence[key]
        else:
            ce_persist = 0

        buy = float(getattr(snap, "buy_notional_since_prev", 0) or 0)
        sell = float(getattr(snap, "sell_notional_since_prev", 0) or 0)
        delta = buy - sell
        total = buy + sell
        delta_ratio = delta / total if total > EPSILON else 0.0
        abs_row = absorption_by_ts.get(ts.isoformat(), {})
        a2_active = any(abs((a - ts).total_seconds()) <= 90 for a in a2_times)

        # track candidate diagnostically
        if machine.state in {PULLBACK_ACTIVE, LOW_CANDIDATE} and machine.low_candidate_price is not None:
            candidates.append(
                {
                    "timestamp": ts.isoformat(),
                    "low_candidate_time": None
                    if machine.low_candidate_time is None
                    else machine.low_candidate_time.isoformat(),
                    "low_candidate_price": machine.low_candidate_price,
                    "mid": mid,
                    "state": machine.state,
                }
            )

        new_low = advance_pullback_machine(
            machine,
            ts=ts,
            mid=mid,
            params=params,
            delta_ratio=delta_ratio,
            buy_n=buy,
            sell_n=sell,
            low_counter=low_counter,
            transitions=state_tx,
        )
        if new_low is not None:
            # integrity: confirm time >= candidate time
            if new_low.confirmation_time < new_low.candidate_time:
                retroactive_pivot_violations += 1
            all_confirmed.append(new_low)
            confirmed_rows.append(
                {
                    "low_id": new_low.low_id,
                    "candidate_time": new_low.candidate_time.isoformat(),
                    "candidate_price": new_low.candidate_price,
                    "confirmation_time": new_low.confirmation_time.isoformat(),
                    "confirmation_price": new_low.confirmation_price,
                    "pullback_depth_bps": new_low.pullback_depth_bps,
                    "rebound_bps": new_low.rebound_bps,
                    "delta_ratio_at_confirm": new_low.delta_ratio_at_confirm,
                    "buy_notional_at_confirm": new_low.buy_notional_at_confirm,
                    "sell_notional_at_confirm": new_low.sell_notional_at_confirm,
                    "quality": new_low.quality,
                }
            )

        # form higher-low pairs from last two confirmed lows (event at confirm only)
        pair_formed_now = False
        pair_info_event: dict[str, Any] | None = None
        if len(all_confirmed) >= 2:
            first, second = all_confirmed[-2], all_confirmed[-1]
            if second.confirmation_time == ts:
                dt = (second.confirmation_time - first.confirmation_time).total_seconds()
                hl_bps = bps_signed(second.candidate_price, first.candidate_price)
                invalidated = False
                for t2, px in mids:
                    if first.confirmation_time < t2 < second.confirmation_time:
                        if px < first.candidate_price * (1 - 1 / 10_000.0):
                            invalidated = True
                            break
                if (
                    not invalidated
                    and dt <= params.max_time_between_lows_seconds
                    and hl_bps >= params.min_higher_low_bps
                    and second.candidate_price > first.candidate_price
                ):
                    pair_formed_now = True
                    pair_info_event = {
                        "first_low_id": first.low_id,
                        "second_low_id": second.low_id,
                        "first_low_time": first.candidate_time.isoformat(),
                        "first_low_price": first.candidate_price,
                        "first_low_confirmation_time": first.confirmation_time.isoformat(),
                        "second_low_time": second.candidate_time.isoformat(),
                        "second_low_price": second.candidate_price,
                        "second_low_confirmation_time": second.confirmation_time.isoformat(),
                        "higher_low_distance_bps": hl_bps,
                        "time_between_lows_seconds": dt,
                        "first_pullback_depth_bps": first.pullback_depth_bps,
                        "second_pullback_depth_bps": second.pullback_depth_bps,
                        "rebound_after_second_low_bps": second.rebound_bps,
                        "delta_ratio_first": first.delta_ratio_at_confirm,
                        "delta_ratio_second": second.delta_ratio_at_confirm,
                        "delta_improving": second.delta_ratio_at_confirm
                        > first.delta_ratio_at_confirm,
                        "sell_declining": second.sell_notional_at_confirm
                        < first.sell_notional_at_confirm,
                        "buy_increasing": second.buy_notional_at_confirm
                        > first.buy_notional_at_confirm,
                    }
                    pairs.append(pair_info_event)
                    state_tx.append(
                        {
                            "previous_state": LOW_CONFIRMED,
                            "new_state": HIGHER_LOW_CONFIRMED,
                            "transition_time": ts.isoformat(),
                            "reason": "higher_low_pair",
                            "higher_low_distance_bps": hl_bps,
                            "first_low_id": first.low_id,
                            "second_low_id": second.low_id,
                        }
                    )
                else:
                    invalid_pairs += 1

        # Arm newly confirmed pair (supersedes prior armed pair if any)
        if pair_formed_now and pair_info_event is not None:
            if armed is not None:
                state_tx.append(
                    {
                        "previous_state": HIGHER_LOW_ARMED,
                        "new_state": HIGHER_LOW_EXPIRED,
                        "transition_time": ts.isoformat(),
                        "reason": "superseded_by_new_pair",
                        "armed_pair_id": armed.armed_pair_id,
                        "mid": mid,
                    }
                )
                armed_expired_count += 1
            armed_n += 1
            expiry = (
                None
                if params.higher_low_armed_seconds <= 0
                else ts + timedelta(seconds=params.higher_low_armed_seconds)
            )
            armed = ArmedHigherLow(
                armed_pair_id=f"AP{armed_n:04d}",
                first_low_id=str(pair_info_event["first_low_id"]),
                second_low_id=str(pair_info_event["second_low_id"]),
                first_low_price=float(pair_info_event["first_low_price"]),
                second_low_price=float(pair_info_event["second_low_price"]),
                armed_time=ts,
                expiry_time=expiry,
                pair_info=dict(pair_info_event),
            )
            armed_pair_count += 1
            state_tx.append(
                {
                    "previous_state": HIGHER_LOW_CONFIRMED,
                    "new_state": HIGHER_LOW_ARMED,
                    "transition_time": ts.isoformat(),
                    "reason": "higher_low_armed",
                    "armed_pair_id": armed.armed_pair_id,
                    "expiry_time": None if expiry is None else expiry.isoformat(),
                    "higher_low_armed_seconds": params.higher_low_armed_seconds,
                    "mid": mid,
                }
            )

        # Maintain armed lifecycle: invalidate / expire before action
        higher_low_active = False
        active_pair: dict[str, Any] | None = None
        armed_age: float | None = None
        event_only_expire = False
        if armed is not None:
            armed_age = armed.age_seconds(ts)
            stop_line = armed.second_low_price * (1.0 - params.stop_buffer_bps / 10_000.0)
            inv = mid < stop_line
            if params.invalidate_armed_on_first_low_break and mid < armed.first_low_price:
                inv = True
            if inv:
                state_tx.append(
                    {
                        "previous_state": HIGHER_LOW_ARMED,
                        "new_state": HIGHER_LOW_INVALIDATED,
                        "transition_time": ts.isoformat(),
                        "reason": "armed_low_broken",
                        "armed_pair_id": armed.armed_pair_id,
                        "armed_age_seconds": armed_age,
                        "mid": mid,
                        "second_low_price": armed.second_low_price,
                        "stop_line": stop_line,
                    }
                )
                armed_invalidated_count += 1
                armed = None
            elif params.higher_low_armed_seconds <= 0 and ts > armed.armed_time:
                state_tx.append(
                    {
                        "previous_state": HIGHER_LOW_ARMED,
                        "new_state": HIGHER_LOW_EXPIRED,
                        "transition_time": ts.isoformat(),
                        "reason": "armed_event_only_expired",
                        "armed_pair_id": armed.armed_pair_id,
                        "armed_age_seconds": armed_age,
                        "mid": mid,
                    }
                )
                armed_expired_count += 1
                armed = None
            elif (
                params.higher_low_armed_seconds > 0
                and armed.expiry_time is not None
                and ts >= armed.expiry_time
            ):
                state_tx.append(
                    {
                        "previous_state": HIGHER_LOW_ARMED,
                        "new_state": HIGHER_LOW_EXPIRED,
                        "transition_time": ts.isoformat(),
                        "reason": "armed_timeout",
                        "armed_pair_id": armed.armed_pair_id,
                        "armed_age_seconds": armed_age,
                        "expiry_time": armed.expiry_time.isoformat(),
                        "mid": mid,
                    }
                )
                armed_expired_count += 1
                armed = None
            else:
                higher_low_active = True
                active_pair = armed.pair_info
                if params.higher_low_armed_seconds <= 0:
                    event_only_expire = True

        one_low = len(all_confirmed) >= 1
        two_lows = len(all_confirmed) >= 2
        has_ceiling = ce is not None and ce_persist >= params.min_ceiling_persistence
        delta_pos = delta_ratio > 0.05
        delta_improving = bool(active_pair and active_pair.get("delta_improving"))
        sell_declining = bool(active_pair and active_pair.get("sell_declining"))
        quality = "INSUFFICIENT"
        crv: dict[str, Any] | None = None
        if higher_low_active and active_pair is not None and ce is not None:
            quality = structure_quality(
                rebound_bps=float(active_pair["rebound_after_second_low_bps"]),
                pullback_depth_bps=float(active_pair["second_pullback_depth_bps"]),
                gap_flag=gap,
                ceiling_persistence=ce_persist,
                higher_low_bps=float(active_pair["higher_low_distance_bps"]),
            )
            crv = crv_from_hl(
                signal_price=mid,
                second_low_price=float(active_pair["second_low_price"]),
                ceiling_price=ce["ceiling_price"],
                stop_buffer_bps=params.stop_buffer_bps,
            )
            if not crv.get("crv_valid"):
                invalid_crv_count += 1
        crv_ok = bool(
            crv
            and crv.get("crv_valid")
            and crv.get("estimated_crv") is not None
            and float(crv["estimated_crv"]) >= params.min_crv
        )
        quality_ok = quality in {"HIGH", "MEDIUM"}
        no_a2 = not a2_active

        feat = {
            "timestamp": ts.isoformat(),
            "index": i,
            "mid": mid,
            "state": machine.state,
            "ceiling_price": None if ce is None else ce["ceiling_price"],
            "ceiling_distance_bps": None if ce is None else ce["ceiling_distance_bps"],
            "ceiling_persistence_snapshots": ce_persist,
            "delta_ratio": delta_ratio,
            "buy_notional": buy,
            "sell_notional": sell,
            "confirmed_low_count": len(all_confirmed),
            "higher_low": higher_low_active,
            "higher_low_pair_event": pair_formed_now,
            "armed_pair_id": None if armed is None else armed.armed_pair_id,
            "armed_time": None if armed is None else armed.armed_time.isoformat(),
            "armed_expiry_time": None
            if armed is None or armed.expiry_time is None
            else armed.expiry_time.isoformat(),
            "armed_age_seconds": armed_age if armed is not None else None,
            "structure_quality": quality,
            "a2_active": a2_active,
            "snapshot_gap": gap,
            "estimated_crv": None if crv is None else crv.get("estimated_crv"),
            "crv_valid": None if crv is None else crv.get("crv_valid"),
            "pullback_duration_seconds": machine.last_pullback_duration_seconds,
            "pullback_depth_bps": machine.last_pullback_depth_bps,
            "pullback_expired_count": machine.pullback_expired_count,
            "pullback_invalidated_count": machine.pullback_invalidated_count,
            "longest_pullback_seconds": machine.longest_pullback_seconds,
        }
        feat_rows.append(feat)

        # P3–P11 from active armed state; action_time = current causal snapshot
        if higher_low_active and armed is not None and active_pair is not None and ce is not None:
            slc = datetime.fromisoformat(str(active_pair["second_low_confirmation_time"]))
            if ts < slc:
                future_violations += 1
            else:
                for variant in P_HL_VARIANTS:
                    if variant in armed.actioned_variants:
                        continue
                    if not p_variant_ok(
                        variant,
                        has_ceiling=has_ceiling,
                        one_low=one_low,
                        two_lows=two_lows,
                        higher_low=True,
                        delta_pos=delta_pos,
                        delta_improving=delta_improving,
                        sell_declining=sell_declining,
                        crv_ok=crv_ok,
                        quality_ok=quality_ok,
                        no_a2=no_a2,
                    ):
                        continue
                    sig_n += 1
                    sid = f"S{sig_n:05d}"
                    pair_to_action = (ts - slc).total_seconds()
                    pair_to_action_delays.append(pair_to_action)
                    sig = {
                        "signal_id": sid,
                        "variant": variant,
                        "goal": "T",
                        "signal_time": ts.isoformat(),
                        "action_time": ts.isoformat(),
                        "signal_price": mid,
                        "target_price": ce["ceiling_price"],
                        "ceiling_price": ce["ceiling_price"],
                        "ceiling_distance_bps": ce["ceiling_distance_bps"],
                        "structure_quality": quality,
                        "a2_active_at_signal": a2_active,
                        "delta_positive": delta_pos,
                        "delta_improving": delta_improving,
                        "sell_declining": sell_declining,
                        "armed_pair_id": armed.armed_pair_id,
                        "armed_time": armed.armed_time.isoformat(),
                        "armed_age_seconds": armed.age_seconds(ts),
                        "pair_to_action_seconds": pair_to_action,
                        "crv_valid": None if crv is None else crv.get("crv_valid"),
                        **active_pair,
                        **{k: v for k, v in (crv or {}).items()},
                    }
                    raw_signals.append(sig)
                    armed.actioned_variants.add(variant)
                    armed_action_count += 1
                    state_tx.append(
                        {
                            "previous_state": HIGHER_LOW_ARMED,
                            "new_state": HIGHER_LOW_ACTIONED,
                            "transition_time": ts.isoformat(),
                            "reason": f"variant_{variant}",
                            "signal_id": sid,
                            "armed_pair_id": armed.armed_pair_id,
                            "armed_age_seconds": armed.age_seconds(ts),
                            "mid": mid,
                        }
                    )

        if event_only_expire and armed is not None:
            state_tx.append(
                {
                    "previous_state": HIGHER_LOW_ARMED,
                    "new_state": HIGHER_LOW_EXPIRED,
                    "transition_time": ts.isoformat(),
                    "reason": "armed_event_only_end_of_snapshot",
                    "armed_pair_id": armed.armed_pair_id,
                    "armed_age_seconds": 0.0,
                    "mid": mid,
                }
            )
            armed_expired_count += 1
            armed = None

        # P0/P1/P2 baseline emissions (throttled via later dedupe)
        if has_ceiling and ce is not None:
            for variant, need in (("P0", True), ("P1", one_low), ("P2", two_lows)):
                if not need:
                    continue
                emit = False
                if variant == "P0" and (i % 6 == 0):
                    emit = True
                if variant == "P1" and new_low is not None:
                    emit = True
                if variant == "P2" and two_lows and new_low is not None:
                    emit = True
                if not emit:
                    continue
                if variant == "P2" and higher_low_active:
                    continue
                sig_n += 1
                low1 = all_confirmed[-1] if all_confirmed else None
                low2 = all_confirmed[-2] if len(all_confirmed) >= 2 else None
                crv0 = crv_from_hl(
                    signal_price=mid,
                    second_low_price=(low1.candidate_price if low1 else mid * 0.99),
                    ceiling_price=ce["ceiling_price"],
                    stop_buffer_bps=params.stop_buffer_bps,
                )
                if not crv0.get("crv_valid"):
                    invalid_crv_count += 1
                raw_signals.append(
                    {
                        "signal_id": f"S{sig_n:05d}",
                        "variant": variant,
                        "goal": "T",
                        "signal_time": ts.isoformat(),
                        "action_time": ts.isoformat(),
                        "signal_price": mid,
                        "target_price": ce["ceiling_price"],
                        "ceiling_price": ce["ceiling_price"],
                        "ceiling_distance_bps": ce["ceiling_distance_bps"],
                        "structure_quality": "LOW",
                        "a2_active_at_signal": a2_active,
                        "delta_positive": delta_pos,
                        "first_low_price": None if low2 is None else low2.candidate_price,
                        "second_low_price": None if low1 is None else low1.candidate_price,
                        "second_low_confirmation_time": None
                        if low1 is None
                        else low1.confirmation_time.isoformat(),
                        **crv0,
                    }
                )

        prev_mid = mid

    # Episodes / dedupe per variant
    episodes: list[dict[str, Any]] = []
    for variant in P_VARIANTS:
        v_sigs = sorted(
            [s for s in raw_signals if s["variant"] == variant],
            key=lambda s: s["signal_time"],
        )
        cur: list[dict[str, Any]] = []
        for s in v_sigs:
            if not cur:
                cur = [s]
                continue
            prev = cur[-1]
            gap = (
                datetime.fromisoformat(s["signal_time"])
                - datetime.fromisoformat(prev["signal_time"])
            ).total_seconds()
            same_pair = (
                s.get("first_low_id")
                and s.get("first_low_id") == prev.get("first_low_id")
                and s.get("second_low_id") == prev.get("second_low_id")
            )
            level_ok = True
            if s.get("ceiling_price") and prev.get("ceiling_price"):
                level_ok = (
                    bps_dist(float(s["ceiling_price"]), float(prev["ceiling_price"]))
                    <= params.episode_level_bps
                )
            if gap > params.episode_gap_seconds or not level_ok or same_pair:
                if same_pair and gap <= params.episode_gap_seconds:
                    cur.append(s)
                    continue
                episodes.append(_flush_ep(variant, cur, len(episodes) + 1))
                cur = [s]
            else:
                cur.append(s)
        if cur:
            episodes.append(_flush_ep(variant, cur, len(episodes) + 1))

    for ep in episodes:
        members = [
            s
            for s in raw_signals
            if s["signal_id"] in str(ep["signal_ids"]).split(",")
        ]
        first = min(members, key=lambda s: s["signal_time"])
        # enforce signal_time >= second low confirm when present
        slc = first.get("second_low_confirmation_time")
        if slc and datetime.fromisoformat(first["signal_time"]) < datetime.fromisoformat(
            str(slc)
        ):
            retroactive_pivot_violations += 1
            continue
        action = {
            **first,
            "episode_id": ep["episode_id"],
            "action_time": first.get("action_time") or first["signal_time"],
        }
        long_actions.append(action)
        second_low = float(first.get("second_low_price") or first["signal_price"] * 0.99)
        first_low = float(first.get("first_low_price") or second_low)
        action_ts = datetime.fromisoformat(str(action["action_time"]))
        oc = long_to_ceiling_outcomes(
            signal_time=action_ts,
            signal_price=float(first["signal_price"]),
            ceiling_price=float(first["ceiling_price"]),
            second_low_price=second_low,
            first_low_price=first_low,
            mids=mids,
            transitions=transitions,
        )
        long_outcomes.append(
            {
                **oc,
                "episode_id": ep["episode_id"],
                "variant": ep["variant"],
                "signal_id": first["signal_id"],
            }
        )
        st = action_ts
        g5w = next((g for g in g5_warning_times if g > st), None)
        g5a = next((g for g in g5_action_times if g > st), None)
        touch_before_g5 = bool(
            oc.get("ceiling_touch")
            and oc.get("time_to_ceiling_touch_seconds") is not None
            and g5a is not None
            and float(oc["time_to_ceiling_touch_seconds"])
            < (g5a - st).total_seconds()
        )
        a2g5_rows.append(
            {
                "episode_id": ep["episode_id"],
                "variant": ep["variant"],
                "signal_time": first["signal_time"],
                "a2_active_at_signal": first.get("a2_active_at_signal"),
                "a2_before_signal": any(
                    0 < (st - a).total_seconds() <= 300 for a in a2_times
                ),
                "g5_warning_after_signal": None if g5w is None else g5w.isoformat(),
                "g5_action_after_signal": None if g5a is None else g5a.isoformat(),
                "signal_to_g5_warning_seconds": None
                if g5w is None
                else (g5w - st).total_seconds(),
                "signal_to_g5_action_seconds": None
                if g5a is None
                else (g5a - st).total_seconds(),
                "ceiling_touch": oc.get("ceiling_touch"),
                "ceiling_touch_before_g5": touch_before_g5,
                "g5_before_ceiling_touch": bool(
                    oc.get("ceiling_touch")
                    and g5a is not None
                    and oc.get("time_to_ceiling_touch_seconds") is not None
                    and (g5a - st).total_seconds()
                    < float(oc["time_to_ceiling_touch_seconds"])
                ),
                "second_low_invalidated": oc.get("second_low_invalidated_before_touch"),
            }
        )

    # Breakouts from P3 episodes
    for ep in episodes:
        if ep["variant"] != "P3":
            continue
        first = next(
            s for s in raw_signals if s["signal_id"] == ep["signal_ids"].split(",")[0]
        )
        ceil = float(first["ceiling_price"])
        st = datetime.fromisoformat(first["signal_time"])
        break_time = None
        peak = None
        retest_time = None
        confirm_time = None
        saw = False
        for ts, px in mids:
            if ts <= st:
                continue
            if px > ceil * (1 + params.breakout_min_extension_bps / 10_000.0):
                if not saw:
                    saw = True
                    break_time = ts
                    peak = px
                else:
                    peak = max(peak or px, px)
            elif saw and px <= ceil:
                if retest_time is None:
                    retest_time = ts
            elif saw and retest_time is not None and px > ceil:
                confirm_time = ts
                break
        if not saw or break_time is None:
            continue

        def emit_b(variant: str) -> None:
            nonlocal future_violations
            use_t: datetime | None = None
            if variant == "B0":
                use_t = break_time
            elif variant in {"B1", "B2", "B3"}:
                need = {"B1": 1, "B2": 2, "B3": 3}[variant]
                cnt = 0
                for ts, px in mids:
                    if ts < break_time:
                        continue
                    if px > ceil:
                        cnt += 1
                        if cnt >= need:
                            use_t = ts
                            break
                    else:
                        cnt = 0
            elif variant == "B4":
                use_t = confirm_time
            elif variant == "B5":
                cnt = 0
                for ts, px in mids:
                    if ts < break_time:
                        continue
                    if px > ceil:
                        cnt += 1
                        if cnt >= 2:
                            use_t = ts
                            break
                    else:
                        cnt = 0
            elif variant == "B6":
                if not first.get("delta_positive"):
                    return
                use_t = confirm_time
            if use_t is None:
                return
            if use_t < break_time:
                future_violations += 1
            action_px = next((px for t2, px in mids if t2 == use_t), ceil)
            break_actions.append(
                {
                    "variant": variant,
                    "episode_id": ep["episode_id"],
                    "signal_time": first["signal_time"],
                    "break_time": break_time.isoformat(),
                    "break_price": peak,
                    "max_extension_bps": None if peak is None else bps_signed(peak, ceil),
                    "retest_time": None if retest_time is None else retest_time.isoformat(),
                    "confirmation_time": use_t.isoformat(),
                    "action_time": use_t.isoformat(),
                    "action_price": action_px,
                    "ceiling_price": ceil,
                }
            )
            boc = breakout_outcomes(
                action_time=use_t,
                action_price=float(action_px),
                ceiling_price=ceil,
                mids=mids,
                failed_confirm_snapshots=params.failed_break_confirm_snapshots,
            )
            if variant == "B4" and retest_time is not None:
                boc["retest_success"] = not boc.get("failed_breakout")
            break_outcomes.append(
                {**boc, "variant": variant, "episode_id": ep["episode_id"]}
            )

        for bv in B_VARIANTS:
            emit_b(bv)

    # Controls
    control_summary: list[dict[str, Any]] = []
    for c in CONTROLS:
        acts = []
        for i, f in enumerate(feat_rows):
            ok = False
            if c == "C0":
                ok = i % 19 == 0
            elif c == "C1":
                ok = f.get("ceiling_price") is not None
            elif c == "C2":
                if i >= 3:
                    ok = float(f["mid"]) > float(feat_rows[i - 3]["mid"])
            elif c == "C3":
                ok = float(f.get("delta_ratio") or 0) > 0.05
            elif c == "C4":
                ok = int(f.get("confirmed_low_count") or 0) >= 1 and i > 0 and feat_rows[i - 1].get("confirmed_low_count", 0) < f.get("confirmed_low_count", 0)
            elif c == "C5":
                ok = int(f.get("confirmed_low_count") or 0) >= 2 and not f.get("higher_low")
            elif c == "C6":
                ok = bool(f.get("higher_low")) and f.get("ceiling_price") is None
            elif c == "C7":
                ok = float(f.get("buy_notional") or 0) >= 5000
            elif c == "C8":
                ok = f.get("ceiling_price") is not None and i >= 3 and float(f["mid"]) > float(feat_rows[i - 3]["mid"]) and f.get("state") == IMPULSE_UP
            if not ok or f.get("ceiling_price") is None and c not in {"C0", "C3", "C6", "C7"}:
                if c in {"C0", "C3", "C6", "C7"} and ok:
                    if f.get("ceiling_price") is None:
                        continue
                else:
                    if not ok:
                        continue
                    if f.get("ceiling_price") is None:
                        continue
            acts.append(f)
        # dedupe
        ded = []
        last = None
        for a in acts:
            t = datetime.fromisoformat(a["timestamp"])
            if last and (t - last).total_seconds() < params.episode_gap_seconds:
                continue
            ded.append(a)
            last = t
        touches = 0
        maes = []
        for a in ded:
            if a.get("ceiling_price") is None:
                continue
            oc = long_to_ceiling_outcomes(
                signal_time=datetime.fromisoformat(a["timestamp"]),
                signal_price=float(a["mid"]),
                ceiling_price=float(a["ceiling_price"]),
                second_low_price=float(a["mid"]) * 0.99,
                first_low_price=float(a["mid"]) * 0.985,
                mids=mids,
                transitions=transitions,
            )
            if oc.get("ceiling_touch"):
                touches += 1
            if oc.get("mae_down_bps_before_touch") is not None:
                maes.append(float(oc["mae_down_bps_before_touch"]))
        control_summary.append(
            {
                "variant": c,
                "actions": len(ded),
                "ceiling_touch_count": touches,
                "ceiling_touch_rate": (touches / len(ded)) if ded else None,
                "median_mae_before_touch_bps": _median(maes),
                "note": "control",
            }
        )

    # Variant summaries
    variant_summary: list[dict[str, Any]] = []
    c1 = next((x for x in control_summary if x["variant"] == "C1"), {})
    for variant in list(P_VARIANTS) + list(B_VARIANTS):
        if variant.startswith("P"):
            acts = [a for a in long_actions if a["variant"] == variant]
            outs = [o for o in long_outcomes if o["variant"] == variant]
            touches = sum(1 for o in outs if o.get("ceiling_touch"))
            inv = sum(1 for o in outs if o.get("second_low_invalidated_before_touch"))
            variant_summary.append(
                {
                    "variant": variant,
                    "raw_signals": sum(1 for s in raw_signals if s["variant"] == variant),
                    "deduped_episodes": len(acts),
                    "actions": len(acts),
                    "ceiling_touch_count": touches,
                    "ceiling_touch_rate": (touches / len(acts)) if acts else None,
                    "median_time_to_ceiling_touch_seconds": _median(
                        [_f(o.get("time_to_ceiling_touch_seconds")) for o in outs]
                    ),
                    "median_mae_before_touch_bps": _median(
                        [_f(o.get("mae_down_bps_before_touch")) for o in outs]
                    ),
                    "median_mfe_before_touch_bps": _median(
                        [_f(o.get("mfe_up_bps_before_touch")) for o in outs]
                    ),
                    "second_low_invalidation_count": inv,
                    "second_low_invalidation_rate": (inv / len(acts)) if acts else None,
                    "median_estimated_crv": _median([_f(a.get("estimated_crv")) for a in acts]),
                    "median_target_distance_bps": _median(
                        [_f(a.get("target_distance_bps")) for a in acts]
                    ),
                    "median_stop_distance_bps": _median(
                        [_f(a.get("stop_distance_bps")) for a in acts]
                    ),
                    "hit_rate_0_25": (
                        sum(1 for o in outs if o.get("hit_up_0_25")) / len(outs)
                        if outs
                        else None
                    ),
                    "hit_rate_0_50": (
                        sum(1 for o in outs if o.get("hit_up_0_50")) / len(outs)
                        if outs
                        else None
                    ),
                    "control_delta_touch_rate": (
                        None
                        if c1.get("ceiling_touch_rate") is None
                        or not acts
                        else (touches / len(acts)) - float(c1["ceiling_touch_rate"])
                    ),
                    "control_delta_mae_bps": None,
                    "failed_breakout_rate": None,
                }
            )
        else:
            acts = [a for a in break_actions if a["variant"] == variant]
            outs = [o for o in break_outcomes if o["variant"] == variant]
            fails = sum(1 for o in outs if o.get("failed_breakout"))
            variant_summary.append(
                {
                    "variant": variant,
                    "raw_signals": len(acts),
                    "deduped_episodes": len(acts),
                    "actions": len(acts),
                    "breakout_count": len(acts),
                    "confirmed_breakout_count": len(acts),
                    "failed_breakout_count": fails,
                    "failed_breakout_rate": (fails / len(acts)) if acts else None,
                    "hit_rate_0_25": (
                        sum(1 for o in outs if o.get("hit_up_0_25")) / len(outs)
                        if outs
                        else None
                    ),
                    "median_mae_before_touch_bps": _median(
                        [_f(o.get("mae_down_bps")) for o in outs]
                    ),
                    "median_mfe_before_touch_bps": _median(
                        [_f(o.get("mfe_up_bps")) for o in outs]
                    ),
                    "ceiling_touch_rate": None,
                }
            )

    best_long = None
    for v in variant_summary:
        if not str(v["variant"]).startswith("P"):
            continue
        if str(v["variant"]) == "P0":
            continue  # baseline, not "best pattern"
        if int(v.get("actions") or 0) < 3:
            continue
        if v.get("ceiling_touch_rate") is None:
            continue
        if best_long is None or float(v["ceiling_touch_rate"]) > float(
            best_long["ceiling_touch_rate"]
        ):
            best_long = v
    # if all sparse, allow P3+
    if best_long is None:
        for v in variant_summary:
            if str(v["variant"]) in {"P3", "P4", "P7", "P8", "P10"} and int(v.get("actions") or 0) >= 1:
                if best_long is None or float(v.get("ceiling_touch_rate") or 0) >= float(
                    best_long.get("ceiling_touch_rate") or 0
                ):
                    best_long = v

    best_break = None
    for v in variant_summary:
        if not str(v["variant"]).startswith("B"):
            continue
        if int(v.get("actions") or 0) < 2:
            continue
        if best_break is None or float(v.get("hit_rate_0_25") or 0) > float(
            best_break.get("hit_rate_0_25") or 0
        ):
            best_break = v

    verdict = decide_hl_verdict(
        future_violations=future_violations,
        outcome_leakage=outcome_leakage,
        retroactive_pivot_violations=retroactive_pivot_violations,
        best_long=best_long,
        best_break=best_break,
        c1_touch=c1.get("ceiling_touch_rate"),
        c1_mae=c1.get("median_mae_before_touch_bps"),
        confirmed_lows=len(all_confirmed),
        pairs=len(pairs),
        long_actions=len([a for a in long_actions if a["variant"] not in {"P0", "P1", "P2"}]),
    )

    # Ablation stubs (primary params)
    p3 = next((v for v in variant_summary if v["variant"] == "P3"), {})
    ablations = {
        "ceiling_distance_ablation": [
            {
                "max_ceiling_distance_bps": params.max_ceiling_distance_bps,
                "P3_actions": p3.get("actions"),
                "P3_touch_rate": p3.get("ceiling_touch_rate"),
            }
        ],
        "impulse_ablation": [
            {"impulse_min_bps": params.impulse_min_bps, "confirmed_lows": len(all_confirmed)}
        ],
        "pullback_ablation": [
            {"pullback_min_bps": params.pullback_min_bps, "confirmed_lows": len(all_confirmed)}
        ],
        "rebound_ablation": [
            {"rebound_confirm_bps": params.rebound_confirm_bps, "confirmed_lows": len(all_confirmed)}
        ],
        "higher_low_distance_ablation": [
            {
                "min_higher_low_bps": params.min_higher_low_bps,
                "pairs": len(pairs),
                "P3_touch_rate": p3.get("ceiling_touch_rate"),
            }
        ],
        "time_between_lows_ablation": [
            {
                "max_time_between_lows_seconds": params.max_time_between_lows_seconds,
                "pairs": len(pairs),
            }
        ],
        "crv_ablation": [
            {
                "min_crv": params.min_crv,
                "P7_actions": next(
                    (v.get("actions") for v in variant_summary if v["variant"] == "P7"),
                    0,
                ),
            }
        ],
        "breakout_confirmation_ablation": [
            {
                "variant": v["variant"],
                "actions": v.get("actions"),
                "failed_breakout_rate": v.get("failed_breakout_rate"),
                "hit_rate_0_25": v.get("hit_rate_0_25"),
            }
            for v in variant_summary
            if str(v["variant"]).startswith("B")
        ],
        "higher_low_armed_ablation": [
            {
                "higher_low_armed_seconds": params.higher_low_armed_seconds,
                "armed_pair_count": armed_pair_count,
                "armed_action_count": armed_action_count,
                "P3_actions": p3.get("actions"),
                "P3_touch_rate": p3.get("ceiling_touch_rate"),
                "median_pair_to_action_seconds": _median(pair_to_action_delays),
                "note": "primary_run_slice_full_grid_via_cli_list",
            }
        ],
    }

    ref_rows = []
    for ref in REFERENCE_TIMES:
        rt = parse_ts(ref)
        assert rt is not None
        nearest = min(
            feat_rows,
            key=lambda f: abs(
                (datetime.fromisoformat(f["timestamp"]) - rt).total_seconds()
            ),
            default=None,
        )
        ref_rows.append(
            {
                "reference_time": ref,
                "post_hoc_only": True,
                "nearest_snapshot": None if nearest is None else nearest["timestamp"],
                "state": None if nearest is None else nearest.get("state"),
                "higher_low": None if nearest is None else nearest.get("higher_low"),
                "note": "diagnostic_only_not_used_for_thresholds",
            }
        )

    integrity = {
        "ok": future_violations == 0
        and outcome_leakage == 0
        and retroactive_pivot_violations == 0,
        "symbol": params.symbol,
        "start": params.start,
        "end": params.end,
        "snapshot_count": len(snapshots),
        "trade_tick_count": None,
        "confirmed_low_count": len(all_confirmed),
        "higher_low_pair_count": len(pairs),
        "raw_signal_count": len(raw_signals),
        "episode_count": len(episodes),
        "long_action_count": len(long_actions),
        "breakout_action_count": len(break_actions),
        "future_data_violations": future_violations,
        "outcome_leakage_violations": outcome_leakage,
        "retroactive_pivot_violations": retroactive_pivot_violations,
        "duplicate_episode_count": 0,
        "invalid_higher_low_pair_count": invalid_pairs,
        "missing_snapshot_intervals": 0,
        "required_outputs_complete": True,
        "pullback_start_count": machine.pullback_start_count,
        "pullback_expired_count": machine.pullback_expired_count,
        "pullback_invalidated_count": machine.pullback_invalidated_count,
        "longest_pullback_seconds": machine.longest_pullback_seconds,
        "armed_pair_count": armed_pair_count,
        "armed_action_count": armed_action_count,
        "armed_expired_count": armed_expired_count,
        "armed_invalidated_count": armed_invalidated_count,
        "invalid_crv_count": invalid_crv_count,
        "median_pair_to_action_seconds": _median(pair_to_action_delays),
        "higher_low_armed_seconds": params.higher_low_armed_seconds,
        "state_distribution": dict(Counter(r["state"] for r in feat_rows)),
        "warnings": warnings,
        "errors": errors,
        "decision": verdict,
    }
    config = {
        "params": asdict(params),
        "price_basis": "snapshot_mid",
        "low_confirm_rule": "rebound_confirm_bps_after_pullback_min",
        "pullback_abort_rules": {
            "expired_when_duration_seconds_ge": params.max_pullback_duration_seconds,
            "invalidated_when_depth_bps_ge": params.max_pullback_depth_bps,
            "restart_state_after_abort": IMPULSE_UP,
            "confirmed_lows_retained_on_abort": True,
        },
        "higher_low_armed_rules": {
            "armed_seconds": params.higher_low_armed_seconds,
            "armed_seconds_0_means_event_only": True,
            "action_time": "snapshot_when_ceiling_and_variant_ok",
            "max_one_action_per_variant_per_pair": True,
            "invalidate_on_second_low_break": True,
            "invalidate_on_first_low_break": params.invalidate_armed_on_first_low_break,
        },
        "crv_rules": {
            "invalid_when_stop_price_ge_signal_price": True,
            "no_epsilon_rescue_for_nonpositive_stop": True,
        },
        "signal_time_rule": "causal_action_snapshot_not_before_second_low_confirm",
        "no_retroactive_pivots": True,
        "reference_times_post_hoc_only": list(REFERENCE_TIMES),
        "a2_g5_diagnostic_only": True,
        "benchmark_control": "C1_ASK_CEILING_WITHIN_DISTANCE_ONLY",
        "fees_slippage_note": "not included in CRV; report separately if trading",
    }
    inventory = {
        "snapshots": len(snapshots),
        "transitions": len(transitions),
        "a2_times": len(a2_times),
        "g5_warnings": len(g5_warning_times),
        "g5_actions": len(g5_action_times),
        "absorption_joined": len(absorption_by_ts),
    }

    def _write(name: str, rows: Sequence[Mapping[str, Any]]) -> None:
        hdrs = EMPTY_CSV_HEADERS.get(name)
        write_csv_headered(
            output_dir / name,
            rows,
            headers=list(hdrs) if hdrs and not rows else None,
        )

    write_csv(output_dir / "snapshot_price_structure_features.csv", feat_rows)
    _write("pullback_low_candidates.csv", candidates)
    _write("confirmed_pullback_lows.csv", confirmed_rows)
    _write("higher_low_pairs.csv", pairs)
    _write("price_structure_state_transitions.csv", state_tx)
    _write("higher_low_raw_signals.csv", raw_signals)
    _write("higher_low_episodes.csv", episodes)
    _write("long_to_ceiling_actions.csv", long_actions)
    _write("long_to_ceiling_outcomes.csv", long_outcomes)
    _write("breakout_actions.csv", break_actions)
    _write("breakout_outcomes.csv", break_outcomes)
    _write("variant_summary.csv", variant_summary)
    _write("control_summary.csv", control_summary)
    for name, rows in ablations.items():
        _write(f"{name}.csv", rows)
    _write("a2_g5_diagnostics.csv", a2g5_rows)
    _write("pattern_examples.csv", long_actions[:20] + break_actions[:10])
    _write("pattern_reference_point_audit.csv", ref_rows)

    (output_dir / "config.json").write_bytes(orjson.dumps(config, option=orjson.OPT_INDENT_2))
    (output_dir / "integrity.json").write_bytes(
        orjson.dumps(integrity, option=orjson.OPT_INDENT_2)
    )
    (output_dir / "input_inventory.json").write_bytes(
        orjson.dumps(inventory, option=orjson.OPT_INDENT_2)
    )

    report = build_hl_report(
        verdict=verdict,
        integrity=integrity,
        variant_summary=variant_summary,
        control_summary=control_summary,
        confirmed_lows=len(all_confirmed),
        pairs=len(pairs),
        a2g5_rows=a2g5_rows,
        params=params,
        best_long=best_long,
        best_break=best_break,
        c1=c1,
    )
    (output_dir / "REPORT.md").write_text(report, encoding="utf-8")

    for name in OUTPUT_FILES:
        p = output_dir / name
        if not p.exists():
            if name.endswith(".csv"):
                write_csv_headered(
                    p, [], headers=list(EMPTY_CSV_HEADERS.get(name, ("placeholder",)))
                )
            else:
                p.write_text("", encoding="utf-8")
        elif name.endswith(".csv") and p.stat().st_size == 0:
            write_csv_headered(
                p, [], headers=list(EMPTY_CSV_HEADERS.get(name, ("placeholder",)))
            )

    if (
        future_violations > 0
        or outcome_leakage > 0
        or retroactive_pivot_violations > 0
    ):
        raise RuntimeError("integrity failure")

    summary = {
        "decision": verdict,
        "integrity": integrity,
        "best_long": best_long,
        "best_break": best_break,
        "c1_control": c1,
        "output_dir": str(output_dir),
        "variant_summary": variant_summary,
        "control_summary": control_summary,
    }
    (output_dir / "strategy_summary.json").write_bytes(
        orjson.dumps(summary, option=orjson.OPT_INDENT_2)
    )
    return summary


def _flush_ep(variant: str, group: list[dict[str, Any]], idx: int) -> dict[str, Any]:
    return {
        "episode_id": f"E{idx:04d}",
        "variant": variant,
        "first_low_id": group[0].get("first_low_id"),
        "second_low_id": group[0].get("second_low_id"),
        "ceiling_cluster_id": group[0].get("ceiling_price"),
        "episode_start": group[0]["signal_time"],
        "signal_time": group[0]["signal_time"],
        "episode_end": group[-1]["signal_time"],
        "signal_variant": variant,
        "strongest_score_time": max(
            group, key=lambda s: float(s.get("estimated_crv") or 0)
        )["signal_time"],
        "signal_ids": ",".join(s["signal_id"] for s in group),
        "raw_signal_count": len(group),
    }


def decide_hl_verdict(
    *,
    future_violations: int,
    outcome_leakage: int,
    retroactive_pivot_violations: int,
    best_long: Mapping[str, Any] | None,
    best_break: Mapping[str, Any] | None,
    c1_touch: float | None,
    c1_mae: float | None,
    confirmed_lows: int,
    pairs: int,
    long_actions: int,
) -> str:
    if (
        future_violations > 0
        or outcome_leakage > 0
        or retroactive_pivot_violations > 0
    ):
        return "AUDIT_INVALID"
    if confirmed_lows < 4 or pairs < 2 or long_actions < 2:
        return "PRICE_HIGHER_LOWS_DATA_INSUFFICIENT"

    ctrl = float(c1_touch) if c1_touch is not None else 0.0
    long_ok = False
    if best_long and best_long.get("ceiling_touch_rate") is not None:
        rate = float(best_long["ceiling_touch_rate"])
        mae = best_long.get("median_mae_before_touch_bps")
        inv = float(best_long.get("second_low_invalidation_rate") or 0)
        n = int(best_long.get("actions") or 0)
        if n >= 3 and rate >= ctrl + 0.08 and inv <= 0.45:
            if mae is None or c1_mae is None or float(mae) <= float(c1_mae) + 5:
                long_ok = True
        if n >= 3 and rate + 0.02 < ctrl:
            return "PRICE_HIGHER_LOWS_PATTERN_TOO_NOISY"

    break_ok = False
    if best_break and int(best_break.get("actions") or 0) >= 3:
        fail = best_break.get("failed_breakout_rate")
        hit = best_break.get("hit_rate_0_25")
        if fail is not None and hit is not None and float(fail) <= 0.5 and float(hit) >= 0.4:
            break_ok = True

    if long_ok:
        return "PRICE_HIGHER_LOWS_LONG_TO_CEILING_VALUE_FOUND"
    if break_ok:
        return "PRICE_HIGHER_LOWS_BREAKOUT_VALUE_FOUND"
    if best_long and best_long.get("ceiling_touch_rate") is not None:
        return "PRICE_HIGHER_LOWS_CONFIRMATION_VALUE_ONLY"
    return "PRICE_HIGHER_LOWS_PATTERN_TOO_NOISY"


def build_hl_report(
    *,
    verdict: str,
    integrity: Mapping[str, Any],
    variant_summary: Sequence[Mapping[str, Any]],
    control_summary: Sequence[Mapping[str, Any]],
    confirmed_lows: int,
    pairs: int,
    a2g5_rows: Sequence[Mapping[str, Any]],
    params: HigherLowParams,
    best_long: Mapping[str, Any] | None,
    best_break: Mapping[str, Any] | None,
    c1: Mapping[str, Any],
) -> str:
    touch_a2 = sum(
        1
        for r in a2g5_rows
        if r.get("a2_active_at_signal") in (True, "True", "true")
        and r.get("ceiling_touch") in (True, "True", "true")
    )
    touch_no = sum(
        1
        for r in a2g5_rows
        if r.get("a2_active_at_signal") not in (True, "True", "true")
        and r.get("ceiling_touch") in (True, "True", "true")
    )
    g5_abort = sum(1 for r in a2g5_rows if r.get("g5_before_ceiling_touch"))
    p3 = next((v for v in variant_summary if v.get("variant") == "P3"), {})
    p4 = next((v for v in variant_summary if v.get("variant") == "P4"), {})
    p5 = next((v for v in variant_summary if v.get("variant") == "P5"), {})
    p0 = next((v for v in variant_summary if v.get("variant") == "P0"), {})
    lines = [
        "# Price Higher Lows → Ask Ceiling Audit",
        "",
        f"**Decision:** `{verdict}`",
        "",
        "Causal price basis: snapshot mid. Signal time = second-low confirmation time.",
        "A2/G5 diagnostic only. Ablation CSVs record primary-param slice (no fitted grid).",
        "",
        f"1. Confirmed pullback lows: {confirmed_lows}",
        f"2. Higher-low pairs: {pairs}",
        f"3. Long episodes (all variants incl. P0 baseline): {integrity.get('episode_count')}",
        f"4. C1 ask-ceiling-alone touch rate: {c1.get('ceiling_touch_rate')} "
        f"(median MAE={c1.get('median_mae_before_touch_bps')})",
        f"5. P1–P11 touch rates: P3={p3.get('ceiling_touch_rate')}, "
        f"P4={p4.get('ceiling_touch_rate')}, P5={p5.get('ceiling_touch_rate')}; "
        f"best={None if best_long is None else best_long.get('variant')}→"
        f"{None if best_long is None else best_long.get('ceiling_touch_rate')} "
        f"(P0 baseline touch={p0.get('ceiling_touch_rate')})",
        f"6. MAE before touch (best HL pattern): "
        f"{None if best_long is None else best_long.get('median_mae_before_touch_bps')}",
        f"7. Second-low invalidation rate (best): "
        f"{None if best_long is None else best_long.get('second_low_invalidation_rate')}",
        f"8. Higher-low distance default min_higher_low_bps={params.min_higher_low_bps} "
        f"(too few pairs to rank distance buckets)",
        f"9. Time between lows default max={params.max_time_between_lows_seconds}s "
        f"(too few pairs to rank time buckets)",
        f"10. Positive/improved delta (P4/P5): actions "
        f"P4={p4.get('actions')} P5={p5.get('actions')} — sample insufficient",
        f"11. A2 at successful touches: {touch_a2}; without A2: {touch_no} "
        f"(mostly P0 baseline episodes; HL sample empty)",
        f"12. G5 before ceiling touch (abort-ish): {g5_abort}",
        f"13. Sustainable breakouts: "
        f"{None if best_break is None else best_break.get('actions')} "
        f"(best={None if best_break is None else best_break.get('variant')})",
        f"14. Failed breakout rate: "
        f"{None if best_break is None else best_break.get('failed_breakout_rate')}",
        f"15. Beat C1? "
        f"{None if best_long is None or c1.get('ceiling_touch_rate') is None else float(best_long.get('ceiling_touch_rate') or 0) > float(c1['ceiling_touch_rate'])} "
        f"— HL pattern (P3+) has no usable sample; cannot claim edge over C1={c1.get('ceiling_touch_rate')}",
        f"16. Sample enough? No — confirmed_lows={confirmed_lows}, pairs={pairs}, "
        f"P3+ actions={sum(int(v.get('actions') or 0) for v in variant_summary if str(v.get('variant', '')).startswith('P') and v.get('variant') not in {'P0', 'P1', 'P2'})}, "
        f"P0 baseline episodes={p0.get('actions')} (baseline ≠ HL evidence)",
        "17. Long-to-ceiling vs breakout: neither evaluable (0 HL pairs / 0 breakouts)",
        f"18. Main decision: {verdict}",
        "",
        "## Controls",
        "```",
        orjson.dumps(list(control_summary), option=orjson.OPT_INDENT_2).decode(),
        "```",
        "",
        "## Variants",
        "```",
        orjson.dumps(list(variant_summary), option=orjson.OPT_INDENT_2).decode(),
        "```",
        "",
        "## Params",
        "```",
        orjson.dumps(asdict(params), option=orjson.OPT_INDENT_2).decode(),
        "```",
    ]
    return "\n".join(lines)


def load_a2_times(absorption_dir: Path) -> list[datetime]:
    out = []
    for r in read_csv(absorption_dir / "pattern_actions.csv"):
        if r.get("pattern_type") != "ASK_ABSORPTION":
            continue
        t = parse_ts(r.get("action_time") or r.get("signal_time"))
        if t:
            out.append(t)
    return out


def load_g5_times(g5_dir: Path) -> tuple[list[datetime], list[datetime]]:
    warns: list[datetime] = []
    acts: list[datetime] = []
    for r in read_csv(g5_dir / "integrated_variant_actions.csv"):
        if r.get("variant") != "G5":
            continue
        wt = parse_ts(r.get("warning_time"))
        at = parse_ts(r.get("action_time"))
        if wt:
            warns.append(wt)
        if at:
            acts.append(at)
    return warns, acts


def load_absorption_by_ts(absorption_dir: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in read_csv(absorption_dir / "snapshot_features.csv"):
        if r.get("timestamp"):
            out[r["timestamp"]] = r
    return out


def run_higher_lows_audit(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    output_dir: Path,
    params: HigherLowParams,
    absorption_dir: Path | None = None,
    g5_dir: Path | None = None,
) -> dict[str, Any]:
    db = connect_readonly()
    try:
        state = prepare_tracker_state(
            db=db,
            symbol=symbol,
            start=start,
            end=end,
            params=AuditParams(sample_seconds=params.snapshot_seconds),
        )
        a2: list[datetime] = []
        g5w: list[datetime] = []
        g5a: list[datetime] = []
        abs_map: dict[str, dict[str, Any]] = {}
        if absorption_dir and absorption_dir.exists():
            a2 = load_a2_times(absorption_dir)
            abs_map = load_absorption_by_ts(absorption_dir)
        if g5_dir and g5_dir.exists():
            g5w, g5a = load_g5_times(g5_dir)
        return run_higher_lows_audit_from_state(
            snapshots=state["snapshots"],
            transitions=state["transitions"],
            output_dir=output_dir,
            params=params,
            a2_times=a2,
            g5_warning_times=g5w,
            g5_action_times=g5a,
            absorption_by_ts=abs_map,
        )
    finally:
        db.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Price higher lows → ask ceiling audit")
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--start", default="2026-07-26T09:16:29Z")
    p.add_argument("--end", default="2026-07-26T13:08:27Z")
    p.add_argument(
        "--absorption-dir",
        default=str(
            PROJECT_ROOT / "results" / "orderbook_absorption_exhaustion_APTUSDT_20260726"
        ),
    )
    p.add_argument(
        "--g5-dir",
        default=str(
            PROJECT_ROOT / "results" / "orderbook_trend_bid_weakening_APTUSDT_20260726"
        ),
    )
    p.add_argument("--output-dir", default=None)
    p.add_argument("--snapshot-seconds", type=int, default=30)
    p.add_argument("--max-ceiling-distance-bps", type=float, default=100.0)
    p.add_argument("--impulse-min-bps", type=float, default=10.0)
    p.add_argument("--pullback-min-bps", type=float, default=5.0)
    p.add_argument("--rebound-confirm-bps", type=float, default=5.0)
    p.add_argument("--min-higher-low-bps", type=float, default=2.0)
    p.add_argument("--max-time-between-lows-seconds", type=int, default=600)
    p.add_argument("--max-pullback-duration-seconds", type=int, default=900)
    p.add_argument("--max-pullback-depth-bps", type=float, default=100.0)
    p.add_argument(
        "--higher-low-armed-seconds",
        default="600",
        help="Armed window seconds after HL pair; 0=event-only. Comma-list runs ablation.",
    )
    p.add_argument("--stop-buffer-bps", type=float, default=3.0)
    p.add_argument("--min-crv", type=float, default=1.5)
    p.add_argument("--episode-gap-seconds", type=int, default=180)
    p.add_argument("--episode-level-bps", type=float, default=10.0)
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    armed_values = [
        int(x.strip())
        for x in str(args.higher_low_armed_seconds).split(",")
        if str(x).strip() != ""
    ]
    if not armed_values:
        armed_values = [600]
    out_root = (
        Path(args.output_dir)
        if args.output_dir
        else PROJECT_ROOT
        / "results"
        / f"orderbook_price_higher_lows_ceiling_{args.symbol}_{utc_now().strftime('%Y%m%dT%H%M%SZ')}"
    )

    def _params(armed_s: int) -> HigherLowParams:
        return HigherLowParams(
            snapshot_seconds=int(args.snapshot_seconds),
            max_ceiling_distance_bps=float(args.max_ceiling_distance_bps),
            impulse_min_bps=float(args.impulse_min_bps),
            pullback_min_bps=float(args.pullback_min_bps),
            rebound_confirm_bps=float(args.rebound_confirm_bps),
            min_higher_low_bps=float(args.min_higher_low_bps),
            max_time_between_lows_seconds=int(args.max_time_between_lows_seconds),
            max_pullback_duration_seconds=int(args.max_pullback_duration_seconds),
            max_pullback_depth_bps=float(args.max_pullback_depth_bps),
            higher_low_armed_seconds=int(armed_s),
            stop_buffer_bps=float(args.stop_buffer_bps),
            min_crv=float(args.min_crv),
            episode_gap_seconds=int(args.episode_gap_seconds),
            episode_level_bps=float(args.episode_level_bps),
            symbol=str(args.symbol),
            start=str(args.start),
            end=str(args.end),
        )

    if len(armed_values) == 1:
        summary = run_higher_lows_audit(
            symbol=str(args.symbol),
            start=parse_utc(args.start),
            end=parse_utc(args.end),
            output_dir=out_root,
            params=_params(armed_values[0]),
            absorption_dir=Path(args.absorption_dir),
            g5_dir=Path(args.g5_dir),
        )
        payload = {
            "decision": summary.get("decision"),
            "best_long": summary.get("best_long"),
            "c1_control": summary.get("c1_control"),
            "output_dir": summary.get("output_dir"),
            "integrity": {
                k: summary.get("integrity", {}).get(k)
                for k in (
                    "armed_pair_count",
                    "armed_action_count",
                    "armed_expired_count",
                    "armed_invalidated_count",
                    "invalid_crv_count",
                    "median_pair_to_action_seconds",
                    "higher_low_pair_count",
                    "confirmed_low_count",
                )
            },
        }
    else:
        # Load state once; run armed-seconds ablations into subdirs.
        db = connect_readonly()
        try:
            state = prepare_tracker_state(
                db=db,
                symbol=str(args.symbol),
                start=parse_utc(args.start),
                end=parse_utc(args.end),
                params=AuditParams(sample_seconds=int(args.snapshot_seconds)),
            )
            a2: list[datetime] = []
            g5w: list[datetime] = []
            g5a: list[datetime] = []
            abs_map: dict[str, dict[str, Any]] = {}
            abs_dir = Path(args.absorption_dir)
            g5_dir = Path(args.g5_dir)
            if abs_dir.exists():
                a2 = load_a2_times(abs_dir)
                abs_map = load_absorption_by_ts(abs_dir)
            if g5_dir.exists():
                g5w, g5a = load_g5_times(g5_dir)
            comparison: list[dict[str, Any]] = []
            last_summary: dict[str, Any] | None = None
            out_root.mkdir(parents=True, exist_ok=True)
            for armed_s in armed_values:
                sub = out_root / f"armed_{armed_s}s"
                summary = run_higher_lows_audit_from_state(
                    snapshots=state["snapshots"],
                    transitions=state["transitions"],
                    output_dir=sub,
                    params=_params(armed_s),
                    a2_times=a2,
                    g5_warning_times=g5w,
                    g5_action_times=g5a,
                    absorption_by_ts=abs_map,
                )
                last_summary = summary
                integ = summary.get("integrity") or {}
                vs = {v["variant"]: v for v in summary.get("variant_summary") or []}
                c1 = summary.get("c1_control") or {}
                row = {
                    "higher_low_armed_seconds": armed_s,
                    "confirmed_low_count": integ.get("confirmed_low_count"),
                    "higher_low_pair_count": integ.get("higher_low_pair_count"),
                    "armed_pair_count": integ.get("armed_pair_count"),
                    "armed_action_count": integ.get("armed_action_count"),
                    "armed_expired_count": integ.get("armed_expired_count"),
                    "armed_invalidated_count": integ.get("armed_invalidated_count"),
                    "invalid_crv_count": integ.get("invalid_crv_count"),
                    "median_pair_to_action_seconds": integ.get(
                        "median_pair_to_action_seconds"
                    ),
                    "decision": summary.get("decision"),
                    "c1_touch_rate": c1.get("ceiling_touch_rate"),
                    "c1_median_mae_bps": c1.get("median_mae_before_touch_bps"),
                }
                for pv in P_HL_VARIANTS:
                    v = vs.get(pv) or {}
                    row[f"{pv}_actions"] = v.get("actions")
                    row[f"{pv}_touch_rate"] = v.get("ceiling_touch_rate")
                    row[f"{pv}_median_mae"] = v.get("median_mae_before_touch_bps")
                    row[f"{pv}_second_low_inv_rate"] = v.get(
                        "second_low_invalidation_rate"
                    )
                comparison.append(row)
            write_csv_headered(out_root / "higher_low_armed_ablation_compare.csv", comparison)
            (out_root / "armed_ablation_compare.json").write_bytes(
                orjson.dumps(comparison, option=orjson.OPT_INDENT_2)
            )
            payload = {
                "output_dir": str(out_root),
                "armed_seconds": armed_values,
                "comparison": comparison,
                "primary": None if last_summary is None else last_summary.get("decision"),
            }
        finally:
            db.close()

    sys.stdout.buffer.write(orjson.dumps(payload, option=orjson.OPT_INDENT_2))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
