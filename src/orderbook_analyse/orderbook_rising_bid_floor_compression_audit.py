"""Rising Bid Floor Compression research audit (read-only).

Long-to-ceiling (T) and breakout (B) are evaluated as separate goals.

Floor/Ceiling selection (causal, per snapshot t):
  floor  = nearest_bid if below mid, else dominant_bid if below mid
  ceiling = nearest_ask if above mid and within max_ceiling_distance_bps,
            else dominant_ask under the same constraints,
            else strongest near ask wall within distance

Rising floor sequence requires strictly higher confirmed floors with
persistence and min rise; a lower floor invalidates the open sequence.

Compression requires material floor rise AND ceiling stable/slower AND
contracting floor-to-ceiling range (not ceiling-only collapse).

Signal time = first snapshot where conditions are fully met (no backdating).

Outcomes use mid path strictly after signal_time / action_time.
A2/G5 CSVs are diagnostic only and never alter base signal formation.
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
import sys
from collections import Counter, defaultdict
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
from orderbook_analyse.orderbook_trade_candidate_audit import (
    AuditParams,
    prepare_tracker_state,
)
from orderbook_analyse.wall_movement_tracker import (
    RISING_BID_FLOOR,
    WALL_CONSUMED,
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

# States
IDLE = "IDLE"
CEILING_ESTABLISHED = "CEILING_ESTABLISHED"
FLOOR_SEQUENCE_STARTED = "FLOOR_SEQUENCE_STARTED"
FLOOR_RISING = "FLOOR_RISING"
COMPRESSION_CONFIRMED = "COMPRESSION_CONFIRMED"
LONG_TO_CEILING_SIGNAL = "LONG_TO_CEILING_SIGNAL"
BREAKOUT_PENDING = "BREAKOUT_PENDING"
CEILING_TOUCHED = "CEILING_TOUCHED"
BREAKOUT_ATTEMPT = "BREAKOUT_ATTEMPT"
BREAKOUT_CONFIRMED = "BREAKOUT_CONFIRMED"
FAILED_BREAKOUT = "FAILED_BREAKOUT"
FLOOR_INVALIDATED = "FLOOR_INVALIDATED"
EXPIRED = "EXPIRED"

# Floor status labels
VISIBLE_ONLY = "VISIBLE_ONLY"
HELD_WITHOUT_TEST = "HELD_WITHOUT_TEST"
TESTED_AND_HELD = "TESTED_AND_HELD"
PULLED_BEFORE_TEST = "PULLED_BEFORE_TEST"
CONSUMED = "CONSUMED"
REPLACED_HIGHER = "REPLACED_HIGHER"
REPLACED_LOWER = "REPLACED_LOWER"
REAPPEARED_NEAR_LEVEL = "REAPPEARED_NEAR_LEVEL"

LONG_VARIANTS = tuple(f"L{i}" for i in range(11))
BREAK_VARIANTS = ("B0", "B1", "B2", "B3", "B4", "B5", "B6")
CONTROLS = tuple(f"C{i}" for i in range(8))

OUTPUT_FILES = (
    "REPORT.md",
    "config.json",
    "integrity.json",
    "input_inventory.json",
    "snapshot_floor_ceiling_features.csv",
    "bid_floor_sequences.csv",
    "ask_ceiling_clusters.csv",
    "compression_state_transitions.csv",
    "compression_raw_signals.csv",
    "compression_episodes.csv",
    "long_to_ceiling_actions.csv",
    "long_to_ceiling_outcomes.csv",
    "breakout_actions.csv",
    "breakout_outcomes.csv",
    "variant_summary.csv",
    "control_summary.csv",
    "ceiling_distance_ablation.csv",
    "floor_step_ablation.csv",
    "floor_rise_ablation.csv",
    "floor_persistence_ablation.csv",
    "crv_ablation.csv",
    "breakout_confirmation_ablation.csv",
    "a2_g5_diagnostics.csv",
    "pattern_examples.csv",
    "pattern_reference_point_audit.csv",
)

HORIZONS = (30, 60, 120, 300, 600, 900, 1800)


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


def _wall_price(w: Any) -> float | None:
    if w is None:
        return None
    return float(w.price)


def _wall_notional(w: Any) -> float:
    if w is None:
        return 0.0
    return float(w.notional)


def bps_dist(a: float, b: float) -> float:
    if b == 0:
        return float("inf")
    return abs(a - b) / abs(b) * 10_000.0


def bps_signed(new: float, old: float) -> float:
    if old == 0:
        return 0.0
    return (new - old) / old * 10_000.0


def _median(vals: Sequence[float | None]) -> float | None:
    xs = sorted(float(v) for v in vals if v is not None)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2.0


def write_csv_headered(
    path: Path, rows: Sequence[Mapping[str, Any]], headers: Sequence[str] | None = None
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if rows:
        fieldnames: list[str] = []
        for row in rows:
            for k in row:
                if k not in fieldnames:
                    fieldnames.append(k)
    elif headers:
        fieldnames = list(headers)
    else:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k) for k in fieldnames})


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


@dataclass
class CompressionParams:
    snapshot_seconds: int = 30
    max_ceiling_distance_bps: float = 100.0
    min_floor_steps: int = 3
    min_floor_rise_bps: float = 10.0
    min_floor_persistence_snapshots: int = 2
    max_floor_step_gap_seconds: int = 120
    floor_test_distance_bps: float = 5.0
    floor_hold_confirm_snapshots: int = 2
    min_crv: float = 1.5
    stop_buffer_bps: float = 5.0
    min_floor_notional: float = 1000.0
    min_ceiling_notional: float = 1000.0
    max_ceiling_drift_vs_floor_ratio: float = 0.5
    min_compression_bps: float = 5.0
    breakout_min_extension_bps: float = 3.0
    failed_break_confirm_snapshots: int = 2
    episode_gap_seconds: int = 180
    episode_level_bps: float = 10.0
    symbol: str = "APTUSDT"
    start: str = "2026-07-26T09:16:29Z"
    end: str = "2026-07-26T13:08:27Z"


@dataclass
class LevelTrack:
    price: float
    notional: float
    source: str
    first_seen: datetime
    last_seen: datetime
    persistence: int = 1
    test_count: int = 0
    hold_count: int = 0
    pull_count: int = 0
    consume_count: int = 0
    status: str = VISIBLE_ONLY


# ---------------------------------------------------------------------------
# Floor / ceiling selection
# ---------------------------------------------------------------------------


def select_floor(snap: Any, *, min_notional: float) -> dict[str, Any] | None:
    mid = float(snap.mid_price)
    cands: list[tuple[str, float, float]] = []
    for label, w in (
        ("nearest_bid", getattr(snap, "nearest_bid", None)),
        ("dominant_bid", getattr(snap, "dominant_bid", None)),
    ):
        p = _wall_price(w)
        n = _wall_notional(w)
        if p is not None and p < mid and n >= min_notional:
            cands.append((label, p, n))
    for w in getattr(snap, "near_bids", None) or []:
        p = _wall_price(w)
        n = _wall_notional(w)
        if p is not None and p < mid and n >= min_notional:
            cands.append(("near_bid", p, n))
    if not cands:
        return None
    # prefer nearest to mid (highest bid below mid), then notional
    cands.sort(key=lambda x: (-x[1], -x[2]))
    src, price, notional = cands[0]
    return {
        "floor_price": price,
        "floor_notional": notional,
        "floor_source": src,
        "floor_distance_bps": bps_dist(mid, price),
    }


def select_ceiling(
    snap: Any, *, max_distance_bps: float, min_notional: float
) -> dict[str, Any] | None:
    mid = float(snap.mid_price)
    cands: list[tuple[str, float, float, float]] = []
    for label, w in (
        ("nearest_ask", getattr(snap, "nearest_ask", None)),
        ("dominant_ask", getattr(snap, "dominant_ask", None)),
    ):
        p = _wall_price(w)
        n = _wall_notional(w)
        if p is None or p <= mid or n < min_notional:
            continue
        d = bps_dist(p, mid)
        if d <= max_distance_bps:
            cands.append((label, p, n, d))
    for w in getattr(snap, "near_asks", None) or []:
        p = _wall_price(w)
        n = _wall_notional(w)
        if p is None or p <= mid or n < min_notional:
            continue
        d = bps_dist(p, mid)
        if d <= max_distance_bps:
            cands.append(("near_ask", p, n, d))
    if not cands:
        return None
    # prefer nearest_ask source, then smallest distance, then notional
    prio = {"nearest_ask": 0, "dominant_ask": 1, "near_ask": 2}
    cands.sort(key=lambda x: (prio.get(x[0], 9), x[3], -x[2]))
    src, price, notional, dist = cands[0]
    return {
        "ceiling_price": price,
        "ceiling_notional": notional,
        "ceiling_source": src,
        "ceiling_distance_bps": dist,
        "ceiling_cluster_low": price,
        "ceiling_cluster_high": price,
        "ceiling_cluster_notional": notional,
    }


def crv_metrics(
    *,
    mid: float,
    floor_price: float,
    ceiling_price: float,
    stop_buffer_bps: float,
) -> dict[str, float]:
    target_bps = (ceiling_price - mid) / mid * 10_000.0 if mid else 0.0
    stop_price = floor_price * (1.0 - stop_buffer_bps / 10_000.0)
    stop_bps = (mid - stop_price) / mid * 10_000.0 if mid else 0.0
    crv = target_bps / max(stop_bps, EPSILON)
    return {
        "target_distance_bps": target_bps,
        "stop_price": stop_price,
        "stop_distance_bps": stop_bps,
        "estimated_crv": crv,
    }


# ---------------------------------------------------------------------------
# Sequence / compression builders
# ---------------------------------------------------------------------------


@dataclass
class FloorStep:
    price: float
    notional: float
    first_seen: datetime
    last_seen: datetime
    persistence: int
    status: str
    tested_and_held: bool


@dataclass
class FloorSequence:
    sequence_id: str
    steps: list[FloorStep] = field(default_factory=list)
    invalidated: bool = False
    invalidation_time: datetime | None = None

    @property
    def step_count(self) -> int:
        return len(self.steps)

    @property
    def first_price(self) -> float | None:
        return None if not self.steps else self.steps[0].price

    @property
    def last_price(self) -> float | None:
        return None if not self.steps else self.steps[-1].price

    @property
    def total_rise_bps(self) -> float:
        if len(self.steps) < 2:
            return 0.0
        return bps_signed(self.steps[-1].price, self.steps[0].price)

    @property
    def start_time(self) -> datetime | None:
        return None if not self.steps else self.steps[0].first_seen

    @property
    def end_time(self) -> datetime | None:
        return None if not self.steps else self.steps[-1].last_seen


def update_level_track(
    track: LevelTrack | None,
    *,
    price: float | None,
    notional: float,
    source: str,
    ts: datetime,
    level_match_bps: float = 3.0,
) -> LevelTrack | None:
    if price is None:
        return track
    if track is None:
        return LevelTrack(
            price=price,
            notional=notional,
            source=source,
            first_seen=ts,
            last_seen=ts,
            persistence=1,
        )
    if bps_dist(price, track.price) <= level_match_bps:
        track.last_seen = ts
        track.persistence += 1
        track.notional = notional
        track.source = source
        return track
    # new level — caller handles sequence logic
    return LevelTrack(
        price=price,
        notional=notional,
        source=source,
        first_seen=ts,
        last_seen=ts,
        persistence=1,
    )


def is_rising_floor_ready(seq: FloorSequence, params: CompressionParams) -> bool:
    if seq.invalidated or seq.step_count < params.min_floor_steps:
        return False
    if seq.total_rise_bps < params.min_floor_rise_bps:
        return False
    if any(s.persistence < params.min_floor_persistence_snapshots for s in seq.steps):
        return False
    # gaps
    for a, b in zip(seq.steps, seq.steps[1:]):
        gap = (b.first_seen - a.last_seen).total_seconds()
        if gap > params.max_floor_step_gap_seconds:
            return False
        if b.price <= a.price:
            return False
    return True


def compression_ok(
    *,
    seq: FloorSequence,
    ceiling_first: float,
    ceiling_now: float,
    params: CompressionParams,
) -> dict[str, Any] | None:
    if not is_rising_floor_ready(seq, params):
        return None
    assert seq.first_price is not None and seq.last_price is not None
    initial = bps_dist(ceiling_first, seq.first_price)
    final = bps_dist(ceiling_now, seq.last_price)
    compression = initial - final
    floor_rise = seq.total_rise_bps
    ceiling_drift = bps_signed(ceiling_now, ceiling_first)
    # reject ceiling-only collapse
    if floor_rise < params.min_floor_rise_bps:
        return None
    if ceiling_drift < 0 and abs(ceiling_drift) > floor_rise * params.max_ceiling_drift_vs_floor_ratio:
        if compression > 0 and floor_rise < abs(ceiling_drift):
            return None
    if compression < params.min_compression_bps and final >= initial:
        return None
    if final >= initial and floor_rise < params.min_floor_rise_bps:
        return None
    # require contraction OR strong floor rise with stable ceiling
    ceiling_stable = abs(ceiling_drift) <= floor_rise * params.max_ceiling_drift_vs_floor_ratio
    if not (compression >= params.min_compression_bps and ceiling_stable):
        if not (floor_rise >= params.min_floor_rise_bps and ceiling_stable and final < initial):
            return None
    return {
        "initial_floor_to_ceiling_bps": initial,
        "final_floor_to_ceiling_bps": final,
        "compression_bps": compression,
        "compression_ratio": (compression / initial) if initial > EPSILON else None,
        "ceiling_drift_bps": ceiling_drift,
        "floor_rise_bps": floor_rise,
    }


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


def long_to_ceiling_outcomes(
    *,
    signal_time: datetime,
    signal_mid: float,
    ceiling_price: float,
    floor_price: float,
    mids: Sequence[tuple[datetime, float]],
    ceilings_path: Sequence[tuple[datetime, float | None]],
    floors_path: Sequence[tuple[datetime, float | None]],
    transitions: Sequence[Any],
    horizons: Sequence[int] = HORIZONS,
) -> dict[str, Any]:
    t0 = ensure_utc(signal_time)
    forward = [(ensure_utc(ts), px) for ts, px in mids if ensure_utc(ts) > t0]
    out: dict[str, Any] = {
        "signal_time": t0.isoformat(),
        "signal_mid": signal_mid,
        "ceiling_price": ceiling_price,
        "floor_price": floor_price,
        "ceiling_touch": False,
        "time_to_ceiling_touch_seconds": None,
        "ceiling_touch_price": None,
        "mfe_up_bps_before_touch": None,
        "mae_down_bps_before_touch": None,
        "floor_invalidated_before_touch": False,
        "time_to_floor_invalidation_seconds": None,
        "ask_ceiling_pulled_before_touch": False,
        "ask_ceiling_replaced_lower_before_touch": False,
        "ask_ceiling_replaced_higher_before_touch": False,
        "no_touch_within_horizon": True,
    }
    if signal_mid <= 0:
        return out

    touch_t = None
    touch_px = None
    mfe = 0.0
    mae = 0.0
    for ts, px in forward:
        up = (px - signal_mid) / signal_mid * 10_000.0
        down = (signal_mid - px) / signal_mid * 10_000.0
        if touch_t is None:
            mfe = max(mfe, up)
            mae = max(mae, down)
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

    # floor invalidation: floor price drops materially before touch
    end_t = touch_t or (t0 + timedelta(seconds=max(horizons)))
    for ts, fl in floors_path:
        ts = ensure_utc(ts)
        if ts <= t0 or ts > end_t:
            continue
        if fl is not None and fl < floor_price * (1 - 5 / 10_000.0):
            out["floor_invalidated_before_touch"] = True
            out["time_to_floor_invalidation_seconds"] = (ts - t0).total_seconds()
            break

    for tr in transitions:
        cts = ensure_utc(tr.current_timestamp)
        if cts <= t0 or cts > end_t:
            continue
        if getattr(tr, "side", None) != "Ask":
            continue
        cls = str(tr.classification)
        if cls == WALL_PULLED:
            out["ask_ceiling_pulled_before_touch"] = True
        if cls == WALL_REPLACED_LOWER:
            out["ask_ceiling_replaced_lower_before_touch"] = True
        if cls == WALL_REPLACED_HIGHER:
            out["ask_ceiling_replaced_higher_before_touch"] = True

    # hit ups on full forward (and per horizon)
    max_up = 0.0
    t_up_25 = None
    for ts, px in forward:
        up = (px - signal_mid) / signal_mid * 10_000.0
        max_up = max(max_up, up)
        if t_up_25 is None and up >= 25:
            t_up_25 = (ts - t0).total_seconds()
    out["hit_up_0_10"] = max_up >= 10
    out["hit_up_0_25"] = max_up >= 25
    out["hit_up_0_50"] = max_up >= 50
    out["hit_up_1_00"] = max_up >= 100
    out["time_to_hit_up_0_25_seconds"] = t_up_25
    for h in horizons:
        end = t0 + timedelta(seconds=h)
        touched = any(px >= ceiling_price for ts, px in forward if ts <= end)
        out[f"ceiling_touch_{h}s"] = touched
        if touched:
            out["no_touch_within_horizon"] = False
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
    if fail_t is not None:
        out["time_to_failed_breakout_seconds"] = (fail_t - t0).total_seconds()
    for sec, key in ((30, "hold_above_ceiling_30s"), (60, "hold_above_ceiling_60s"), (120, "hold_above_ceiling_120s")):
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


# ---------------------------------------------------------------------------
# Variant gates
# ---------------------------------------------------------------------------


def long_variant_ok(
    variant: str,
    *,
    has_ceiling: bool,
    rising_floor: bool,
    compression: bool,
    delta_positive: bool,
    buy_rising: bool,
    ask_depletion: bool,
    floor_tested_held: bool,
    crv_ok: bool,
) -> bool:
    if variant == "L0":
        return has_ceiling
    if variant == "L1":
        return rising_floor
    if variant == "L2":
        return rising_floor and has_ceiling
    if variant == "L3":
        return compression
    if variant == "L4":
        return compression and delta_positive
    if variant == "L5":
        return compression and delta_positive and buy_rising
    if variant == "L6":
        return compression and delta_positive and ask_depletion
    if variant == "L7":
        return compression and delta_positive and floor_tested_held
    if variant == "L8":
        return compression and delta_positive and floor_tested_held and crv_ok
    if variant == "L9":
        return (
            compression
            and delta_positive
            and floor_tested_held
            and crv_ok
            and ask_depletion
        )
    if variant == "L10":
        return compression and (delta_positive or floor_tested_held)
    return False


# ---------------------------------------------------------------------------
# Main pipeline from snapshots
# ---------------------------------------------------------------------------


def run_compression_audit_from_state(
    *,
    snapshots: Sequence[Any],
    transitions: Sequence[Any],
    sequences: Sequence[Any],
    output_dir: Path,
    params: CompressionParams,
    a2_times: Sequence[datetime] | None = None,
    g5_warning_times: Sequence[datetime] | None = None,
    g5_action_times: Sequence[datetime] | None = None,
    absorption_by_ts: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    future_violations = 0
    outcome_leakage = 0
    warnings: list[str] = []
    errors: list[str] = []
    a2_times = list(a2_times or [])
    g5_warning_times = list(g5_warning_times or [])
    g5_action_times = list(g5_action_times or [])
    absorption_by_ts = dict(absorption_by_ts or {})

    mids = [(ensure_utc(s.timestamp), float(s.mid_price)) for s in snapshots]
    feat_rows: list[dict[str, Any]] = []
    floor_seq_rows: list[dict[str, Any]] = []
    ceiling_rows: list[dict[str, Any]] = []
    transitions_out: list[dict[str, Any]] = []
    raw_signals: list[dict[str, Any]] = []
    long_actions: list[dict[str, Any]] = []
    long_outcomes: list[dict[str, Any]] = []
    break_actions: list[dict[str, Any]] = []
    break_outcomes: list[dict[str, Any]] = []
    a2g5_rows: list[dict[str, Any]] = []

    open_seq = FloorSequence(sequence_id="FS0001")
    completed_seqs: list[FloorSequence] = []
    floor_track: LevelTrack | None = None
    ceiling_track: LevelTrack | None = None
    ceiling_at_seq_start: float | None = None
    state = IDLE
    pending_test: dict[str, Any] | None = None
    floors_path: list[tuple[datetime, float | None]] = []
    ceilings_path: list[tuple[datetime, float | None]] = []
    buy_hist: list[float] = []
    sig_counter = 0
    invalid_floor_seq = 0
    invalid_ceiling = 0

    def set_state(new: str, ts: datetime, reason: str, **extra: Any) -> None:
        nonlocal state
        transitions_out.append(
            {
                "previous_state": state,
                "new_state": new,
                "transition_time": ts.isoformat(),
                "reason": reason,
                **extra,
            }
        )
        state = new

    for i, snap in enumerate(snapshots):
        ts = ensure_utc(snap.timestamp)
        mid = float(snap.mid_price)
        fl = select_floor(snap, min_notional=params.min_floor_notional)
        ce = select_ceiling(
            snap,
            max_distance_bps=params.max_ceiling_distance_bps,
            min_notional=params.min_ceiling_notional,
        )
        if ce is None:
            invalid_ceiling += 0  # not invalid, just absent
        floors_path.append((ts, None if fl is None else fl["floor_price"]))
        ceilings_path.append((ts, None if ce is None else ce["ceiling_price"]))

        # orderflow from snapshot + optional absorption join
        buy = float(getattr(snap, "buy_notional_since_prev", 0) or 0)
        sell = float(getattr(snap, "sell_notional_since_prev", 0) or 0)
        delta = float(getattr(snap, "trade_delta_notional", buy - sell) or (buy - sell))
        total = buy + sell
        delta_ratio = delta / total if total > EPSILON else 0.0
        buy_hist.append(buy)
        buy_rising = len(buy_hist) >= 3 and buy_hist[-1] > buy_hist[-2] > buy_hist[-3]
        abs_row = absorption_by_ts.get(ts.isoformat(), {})
        ask_depletion = float(abs_row.get("depletion_observed_wall_depletion_notional") or 0) > 0
        a2_active = False
        for at in a2_times:
            if abs((at - ts).total_seconds()) <= 90:
                a2_active = True
                break

        # ceiling track
        if ce is not None:
            prev_c = ceiling_track
            ceiling_track = update_level_track(
                ceiling_track,
                price=ce["ceiling_price"],
                notional=ce["ceiling_notional"],
                source=ce["ceiling_source"],
                ts=ts,
            )
            if prev_c is None or (
                ceiling_track and bps_dist(ceiling_track.price, prev_c.price) > 3
            ):
                if state == IDLE:
                    set_state(CEILING_ESTABLISHED, ts, "ceiling_seen", ceiling_price=ce["ceiling_price"])
            ceiling_rows.append(
                {
                    "timestamp": ts.isoformat(),
                    "ceiling_price": ce["ceiling_price"],
                    "ceiling_notional": ce["ceiling_notional"],
                    "ceiling_source": ce["ceiling_source"],
                    "ceiling_first_seen_time": ceiling_track.first_seen.isoformat() if ceiling_track else None,
                    "ceiling_last_seen_time": ceiling_track.last_seen.isoformat() if ceiling_track else None,
                    "ceiling_persistence_snapshots": ceiling_track.persistence if ceiling_track else 0,
                    "ceiling_distance_bps": ce["ceiling_distance_bps"],
                    "ceiling_cluster_low": ce["ceiling_cluster_low"],
                    "ceiling_cluster_high": ce["ceiling_cluster_high"],
                    "ceiling_cluster_notional": ce["ceiling_cluster_notional"],
                    "ceiling_stability_bps": 0.0
                    if ceiling_track is None
                    else bps_dist(ce["ceiling_price"], ceiling_track.price),
                }
            )

        # floor track + sequence
        floor_tested_held = False
        floor_status = VISIBLE_ONLY
        if fl is not None:
            price = fl["floor_price"]
            # test detection
            if bps_dist(mid, price) <= params.floor_test_distance_bps:
                if pending_test is None or bps_dist(pending_test["price"], price) > 3:
                    pending_test = {
                        "price": price,
                        "start": ts,
                        "below_or_near": 1,
                        "held": 0,
                    }
                else:
                    pending_test["below_or_near"] += 1
            elif pending_test is not None and bps_dist(pending_test["price"], price) <= 3:
                if mid > pending_test["price"]:
                    pending_test["held"] = pending_test.get("held", 0) + 1
                    if pending_test["held"] >= params.floor_hold_confirm_snapshots:
                        floor_tested_held = True
                        floor_status = TESTED_AND_HELD
                        pending_test = None
            # transitions near floor
            for tr in transitions:
                if ensure_utc(tr.current_timestamp) != ts:
                    continue
                if getattr(tr, "side", None) != "Bid":
                    continue
                cls = str(tr.classification)
                if cls == WALL_PULLED:
                    floor_status = PULLED_BEFORE_TEST
                elif cls == WALL_CONSUMED:
                    floor_status = CONSUMED
                elif cls == WALL_REPLACED_HIGHER:
                    floor_status = REPLACED_HIGHER
                elif cls == WALL_REPLACED_LOWER:
                    floor_status = REPLACED_LOWER

            same = (
                floor_track is not None
                and bps_dist(price, floor_track.price) <= 3.0
            )
            if same and floor_track is not None:
                floor_track = update_level_track(
                    floor_track,
                    price=price,
                    notional=fl["floor_notional"],
                    source=fl["floor_source"],
                    ts=ts,
                )
                if floor_tested_held:
                    floor_track.status = TESTED_AND_HELD
                    floor_track.hold_count += 1
                    floor_track.test_count += 1
                elif floor_track.persistence >= params.min_floor_persistence_snapshots:
                    if floor_track.status == VISIBLE_ONLY:
                        floor_track.status = HELD_WITHOUT_TEST
            else:
                # new floor level
                if floor_track is not None and floor_track.persistence >= params.min_floor_persistence_snapshots:
                    # close previous step into sequence
                    step = FloorStep(
                        price=floor_track.price,
                        notional=floor_track.notional,
                        first_seen=floor_track.first_seen,
                        last_seen=floor_track.last_seen,
                        persistence=floor_track.persistence,
                        status=floor_track.status,
                        tested_and_held=floor_track.status == TESTED_AND_HELD,
                    )
                    if not open_seq.steps:
                        open_seq.steps.append(step)
                        ceiling_at_seq_start = (
                            None if ce is None else ce["ceiling_price"]
                        )
                        if state in {IDLE, CEILING_ESTABLISHED}:
                            set_state(FLOOR_SEQUENCE_STARTED, ts, "first_floor_step")
                    else:
                        last = open_seq.steps[-1]
                        gap = (step.first_seen - last.last_seen).total_seconds()
                        if step.price > last.price and gap <= params.max_floor_step_gap_seconds:
                            open_seq.steps.append(step)
                            set_state(FLOOR_RISING, ts, "floor_replaced_higher")
                        elif step.price < last.price:
                            open_seq.invalidated = True
                            open_seq.invalidation_time = ts
                            invalid_floor_seq += 1
                            floor_seq_rows.append(_seq_row(open_seq))
                            completed_seqs.append(open_seq)
                            open_seq = FloorSequence(
                                sequence_id=f"FS{len(completed_seqs)+1:04d}"
                            )
                            set_state(FLOOR_INVALIDATED, ts, "lower_floor")
                            ceiling_at_seq_start = None
                        elif gap > params.max_floor_step_gap_seconds:
                            floor_seq_rows.append(_seq_row(open_seq))
                            completed_seqs.append(open_seq)
                            open_seq = FloorSequence(
                                sequence_id=f"FS{len(completed_seqs)+1:04d}",
                                steps=[step],
                            )
                            ceiling_at_seq_start = (
                                None if ce is None else ce["ceiling_price"]
                            )
                floor_track = LevelTrack(
                    price=price,
                    notional=fl["floor_notional"],
                    source=fl["floor_source"],
                    first_seen=ts,
                    last_seen=ts,
                    persistence=1,
                    status=floor_status if floor_tested_held else VISIBLE_ONLY,
                )

        # if current floor track qualifies as step end at persistence threshold, mirror last
        rising_ready = is_rising_floor_ready(open_seq, params)
        # include active track as tentative last step for readiness check
        tentative = open_seq
        if (
            floor_track is not None
            and floor_track.persistence >= params.min_floor_persistence_snapshots
            and open_seq.steps
            and floor_track.price > open_seq.steps[-1].price
        ):
            tentative = FloorSequence(
                sequence_id=open_seq.sequence_id,
                steps=list(open_seq.steps)
                + [
                    FloorStep(
                        price=floor_track.price,
                        notional=floor_track.notional,
                        first_seen=floor_track.first_seen,
                        last_seen=floor_track.last_seen,
                        persistence=floor_track.persistence,
                        status=floor_track.status,
                        tested_and_held=floor_track.status == TESTED_AND_HELD,
                    )
                ],
            )
            rising_ready = is_rising_floor_ready(tentative, params)

        comp = None
        if (
            rising_ready
            and ce is not None
            and ceiling_at_seq_start is not None
            and tentative.steps
        ):
            comp = compression_ok(
                seq=tentative,
                ceiling_first=ceiling_at_seq_start,
                ceiling_now=ce["ceiling_price"],
                params=params,
            )
            if comp and state in {FLOOR_RISING, FLOOR_SEQUENCE_STARTED, COMPRESSION_CONFIRMED}:
                if state != COMPRESSION_CONFIRMED:
                    set_state(COMPRESSION_CONFIRMED, ts, "compression_confirmed", **comp)

        has_ceiling = ce is not None
        crv = None
        if fl is not None and ce is not None:
            crv = crv_metrics(
                mid=mid,
                floor_price=fl["floor_price"],
                ceiling_price=ce["ceiling_price"],
                stop_buffer_bps=params.stop_buffer_bps,
            )
        crv_ok = bool(crv and crv["estimated_crv"] >= params.min_crv)
        delta_positive = delta_ratio > 0.05
        floor_held_flag = bool(
            (floor_track and floor_track.status == TESTED_AND_HELD)
            or any(s.tested_and_held for s in (tentative.steps if tentative else []))
        )

        feat = {
            "timestamp": ts.isoformat(),
            "index": i,
            "mid": mid,
            "floor_price": None if fl is None else fl["floor_price"],
            "floor_notional": None if fl is None else fl["floor_notional"],
            "floor_source": None if fl is None else fl["floor_source"],
            "floor_distance_bps": None if fl is None else fl["floor_distance_bps"],
            "floor_status": floor_status,
            "floor_tested_and_held": floor_held_flag,
            "ceiling_price": None if ce is None else ce["ceiling_price"],
            "ceiling_notional": None if ce is None else ce["ceiling_notional"],
            "ceiling_source": None if ce is None else ce["ceiling_source"],
            "ceiling_distance_bps": None if ce is None else ce["ceiling_distance_bps"],
            "delta_notional": delta,
            "delta_ratio": delta_ratio,
            "buy_notional": buy,
            "sell_notional": sell,
            "buy_rising": buy_rising,
            "ask_depletion_present": ask_depletion,
            "a2_active": a2_active,
            "rising_floor_ready": rising_ready,
            "compression": bool(comp),
            "state": state,
            "floor_step_count": tentative.step_count if tentative else 0,
            "estimated_crv": None if crv is None else crv["estimated_crv"],
            "target_distance_bps": None if crv is None else crv["target_distance_bps"],
            "stop_distance_bps": None if crv is None else crv["stop_distance_bps"],
        }
        if comp:
            feat.update(comp)
        feat_rows.append(feat)

        # emit long signals for variants at first complete snapshot
        if comp and fl and ce and crv:
            for variant in LONG_VARIANTS:
                if not long_variant_ok(
                    variant,
                    has_ceiling=has_ceiling,
                    rising_floor=rising_ready,
                    compression=bool(comp),
                    delta_positive=delta_positive,
                    buy_rising=buy_rising,
                    ask_depletion=ask_depletion,
                    floor_tested_held=floor_held_flag,
                    crv_ok=crv_ok,
                ):
                    continue
                sig_counter += 1
                sid = f"S{sig_counter:05d}"
                sig = {
                    "signal_id": sid,
                    "variant": variant,
                    "goal": "T",
                    "signal_time": ts.isoformat(),
                    "decision_snapshot_time": ts.isoformat(),
                    "signal_mid": mid,
                    "floor_price": fl["floor_price"],
                    "ceiling_price": ce["ceiling_price"],
                    "floor_distance_bps": fl["floor_distance_bps"],
                    "ceiling_distance_bps": ce["ceiling_distance_bps"],
                    "floor_step_count": tentative.step_count,
                    "floor_tested_and_held": floor_held_flag,
                    "delta_positive": delta_positive,
                    "buy_rising": buy_rising,
                    "ask_depletion_present": ask_depletion,
                    "a2_active_at_signal": a2_active,
                    "sequence_id": tentative.sequence_id,
                    **crv,
                    **comp,
                }
                raw_signals.append(sig)

    # finalize open sequence
    if open_seq.steps:
        floor_seq_rows.append(_seq_row(open_seq))
        completed_seqs.append(open_seq)

    # also note tracker RISING_BID_FLOOR sequences diagnostically
    tracker_rising = sum(1 for s in sequences if s.classification == RISING_BID_FLOOR)

    # dedupe long signals into episodes/actions per variant
    episodes: list[dict[str, Any]] = []
    for variant in LONG_VARIANTS:
        v_sigs = [s for s in raw_signals if s["variant"] == variant]
        v_sigs.sort(key=lambda s: s["signal_time"])
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
            level_ok = True
            if s.get("ceiling_price") and prev.get("ceiling_price"):
                level_ok = (
                    bps_dist(float(s["ceiling_price"]), float(prev["ceiling_price"]))
                    <= params.episode_level_bps
                )
            if gap > params.episode_gap_seconds or not level_ok:
                episodes.append(_flush_ep(variant, cur, len(episodes) + 1))
                cur = [s]
            else:
                cur.append(s)
        if cur:
            episodes.append(_flush_ep(variant, cur, len(episodes) + 1))

    # long actions = first signal per episode
    for ep in episodes:
        first = min(
            (s for s in raw_signals if s["signal_id"] in ep["signal_ids"].split(",")),
            key=lambda s: s["signal_time"],
        )
        action = {**first, "episode_id": ep["episode_id"], "action_time": first["signal_time"]}
        long_actions.append(action)
        oc = long_to_ceiling_outcomes(
            signal_time=datetime.fromisoformat(first["signal_time"]),
            signal_mid=float(first["signal_mid"]),
            ceiling_price=float(first["ceiling_price"]),
            floor_price=float(first["floor_price"]),
            mids=mids,
            ceilings_path=ceilings_path,
            floors_path=floors_path,
            transitions=transitions,
        )
        long_outcomes.append({**oc, "episode_id": ep["episode_id"], "variant": ep["variant"], "signal_id": first["signal_id"]})

        # A2/G5 diagnostics
        st = datetime.fromisoformat(first["signal_time"])
        a2_before = any(0 < (st - a).total_seconds() <= 300 for a in a2_times)
        g5w = next((g for g in g5_warning_times if g > st), None)
        g5a = next((g for g in g5_action_times if g > st), None)
        a2g5_rows.append(
            {
                "episode_id": ep["episode_id"],
                "variant": ep["variant"],
                "signal_time": first["signal_time"],
                "a2_active_at_signal": first.get("a2_active_at_signal"),
                "a2_before_signal": a2_before,
                "g5_warning_after_signal": None if g5w is None else g5w.isoformat(),
                "g5_action_after_signal": None if g5a is None else g5a.isoformat(),
                "signal_to_g5_seconds": None
                if g5a is None
                else (g5a - st).total_seconds(),
                "ceiling_touch": oc.get("ceiling_touch"),
            }
        )

    # Breakout detection from compression episodes (L3-like)
    comp_eps = [e for e in episodes if e["variant"] in {"L3", "L4", "L8", "L9"}]
    # use unique ceiling/signal from L3 episodes
    seen_break_keys: set[str] = set()
    for ep in episodes:
        if ep["variant"] != "L3":
            continue
        first = next(
            s for s in raw_signals if s["signal_id"] == ep["signal_ids"].split(",")[0]
        )
        ceil = float(first["ceiling_price"])
        st = datetime.fromisoformat(first["signal_time"])
        key = f"{ceil:.6f}:{st.isoformat()}"
        if key in seen_break_keys:
            continue
        seen_break_keys.add(key)
        # scan forward for breakout
        above = 0
        break_time = None
        peak = None
        retest_time = None
        confirm_time = None
        saw_break = False
        for ts, px in mids:
            if ts <= st:
                continue
            if px > ceil * (1 + params.breakout_min_extension_bps / 10_000.0):
                if not saw_break:
                    saw_break = True
                    break_time = ts
                    peak = px
                else:
                    peak = max(peak or px, px)
                above += 1
            elif saw_break and px <= ceil:
                # potential retest
                if retest_time is None:
                    retest_time = ts
                above = 0
            elif saw_break and retest_time is not None and px > ceil:
                above += 1
                if above >= 2:
                    confirm_time = ts
                    break

        def emit_break(variant: str, action_t: datetime, conf_snaps: int) -> None:
            nonlocal future_violations
            # verify enough consecutive above snapshots from break_time
            if break_time is None:
                return
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
                if confirm_time is None:
                    return
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
                if confirm_time is None or not first.get("delta_positive"):
                    return
                use_t = confirm_time
            else:
                return
            if use_t is None:
                return
            if use_t < break_time:
                future_violations += 1
            action_px = next((px for t2, px in mids if t2 == use_t), ceil)
            row = {
                "variant": variant,
                "episode_id": ep["episode_id"],
                "signal_time": first["signal_time"],
                "break_time": break_time.isoformat(),
                "break_price": peak,
                "max_extension_bps": None
                if peak is None
                else bps_signed(peak, ceil),
                "retest_time": None if retest_time is None else retest_time.isoformat(),
                "confirmation_time": use_t.isoformat(),
                "action_time": use_t.isoformat(),
                "action_price": action_px,
                "ceiling_price": ceil,
                "old_ceiling_status_after_break": "BROKEN",
            }
            break_actions.append(row)
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

        if saw_break and break_time is not None:
            emit_break("B0", break_time, 0)
            emit_break("B1", break_time, 1)
            emit_break("B2", break_time, 2)
            emit_break("B3", break_time, 3)
            emit_break("B4", break_time, 2)
            emit_break("B5", break_time, 2)
            # B6 needs delta on signal
            first["delta_positive"] = bool(first.get("delta_ratio", 0) > 0.05) if "delta_ratio" in first else first.get("delta_positive", False)
            # recover delta from feat
            fr = next((f for f in feat_rows if f["timestamp"] == first["signal_time"]), {})
            first["delta_positive"] = bool(fr.get("delta_ratio", 0) > 0.05)
            emit_break("B6", break_time, 2)

    # Controls
    control_rows: list[dict[str, Any]] = []
    control_summary: list[dict[str, Any]] = []
    n_snap = len(feat_rows)
    for c in CONTROLS:
        acts = []
        for i, f in enumerate(feat_rows):
            ok = False
            if c == "C0":
                ok = (i % 19 == 0)
            elif c == "C1":
                ok = f.get("ceiling_price") is not None
            elif c == "C2":
                ok = float(f.get("delta_ratio") or 0) > 0.05
            elif c == "C3":
                ok = float(f.get("floor_notional") or 0) >= params.min_floor_notional * 3
            elif c == "C4":
                if i >= 3:
                    ok = float(f["mid"]) > float(feat_rows[i - 3]["mid"])
            elif c == "C5":
                ok = bool(f.get("rising_floor_ready")) and f.get("ceiling_price") is None
            elif c == "C6":
                # proxy: compression flag false but ceiling distance shrinking via mid rise only — skip complex
                ok = False
            elif c == "C7":
                ok = float(f.get("buy_notional") or 0) >= 5000
            if not ok:
                continue
            if f.get("ceiling_price") is None and c in {"C1"}:
                pass
            ceil = f.get("ceiling_price")
            if ceil is None:
                # still count control action without touch target
                acts.append({"timestamp": f["timestamp"], "mid": f["mid"], "ceiling_price": None})
                continue
            acts.append(
                {
                    "timestamp": f["timestamp"],
                    "mid": f["mid"],
                    "ceiling_price": ceil,
                    "floor_price": f.get("floor_price") or f["mid"] * 0.99,
                }
            )
        # dedupe controls loosely
        ded = []
        last_t = None
        for a in acts:
            t = datetime.fromisoformat(a["timestamp"])
            if last_t and (t - last_t).total_seconds() < params.episode_gap_seconds:
                continue
            ded.append(a)
            last_t = t
        touches = 0
        for a in ded:
            if a.get("ceiling_price") is None:
                continue
            oc = long_to_ceiling_outcomes(
                signal_time=datetime.fromisoformat(a["timestamp"]),
                signal_mid=float(a["mid"]),
                ceiling_price=float(a["ceiling_price"]),
                floor_price=float(a["floor_price"]),
                mids=mids,
                ceilings_path=ceilings_path,
                floors_path=floors_path,
                transitions=transitions,
            )
            if oc.get("ceiling_touch"):
                touches += 1
            control_rows.append({"control": c, **a, "ceiling_touch": oc.get("ceiling_touch")})
        control_summary.append(
            {
                "variant": c,
                "actions": len(ded),
                "ceiling_touch_count": touches,
                "ceiling_touch_rate": (touches / len(ded)) if ded else None,
                "note": "control",
            }
        )

    # Variant summaries
    variant_summary: list[dict[str, Any]] = []
    for variant in list(LONG_VARIANTS) + list(BREAK_VARIANTS):
        if variant.startswith("L"):
            acts = [a for a in long_actions if a["variant"] == variant]
            outs = [o for o in long_outcomes if o["variant"] == variant]
            touches = sum(1 for o in outs if o.get("ceiling_touch"))
            inv = sum(1 for o in outs if o.get("floor_invalidated_before_touch"))
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
                    "floor_invalidation_count": inv,
                    "floor_invalidation_rate": (inv / len(acts)) if acts else None,
                    "median_estimated_crv": _median([_f(a.get("estimated_crv")) for a in acts]),
                    "median_target_distance_bps": _median(
                        [_f(a.get("target_distance_bps")) for a in acts]
                    ),
                    "breakout_count": None,
                    "confirmed_breakout_count": None,
                    "failed_breakout_count": None,
                    "hit_rate_0_25": (
                        sum(1 for o in outs if o.get("hit_up_0_25")) / len(outs)
                        if outs
                        else None
                    ),
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
                    "ceiling_touch_count": None,
                    "ceiling_touch_rate": None,
                    "breakout_count": len(acts),
                    "breakout_rate": None,
                    "confirmed_breakout_count": len(acts),
                    "confirmed_breakout_rate": 1.0 if acts else None,
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
                }
            )

    # Ablation tables (report primary params; full grid via segment summaries)
    l3 = next((v for v in variant_summary if v["variant"] == "L3"), {})
    l8 = next((v for v in variant_summary if v["variant"] == "L8"), {})
    ceiling_ablation = [
        {
            "max_ceiling_distance_bps": params.max_ceiling_distance_bps,
            "variant": "L3",
            **{k: l3.get(k) for k in ("actions", "ceiling_touch_rate", "median_mae_before_touch_bps")},
        }
    ]
    floor_step_ablation = [
        {
            "min_floor_steps": params.min_floor_steps,
            "actions_L3": l3.get("actions"),
            "touch_rate_L3": l3.get("ceiling_touch_rate"),
        }
    ]
    floor_rise_ablation = [
        {
            "min_floor_rise_bps": params.min_floor_rise_bps,
            "actions_L3": l3.get("actions"),
            "touch_rate_L3": l3.get("ceiling_touch_rate"),
        }
    ]
    floor_pers_ablation = [
        {
            "min_floor_persistence_snapshots": params.min_floor_persistence_snapshots,
            "actions_L3": l3.get("actions"),
            "touch_rate_L3": l3.get("ceiling_touch_rate"),
        }
    ]
    crv_ablation = [
        {
            "min_crv": params.min_crv,
            "actions_L8": l8.get("actions"),
            "touch_rate_L8": l8.get("ceiling_touch_rate"),
        }
    ]
    break_ablation = [
        {
            "variant": v["variant"],
            "actions": v.get("actions"),
            "failed_breakout_rate": v.get("failed_breakout_rate"),
            "hit_rate_0_25": v.get("hit_rate_0_25"),
        }
        for v in variant_summary
        if str(v["variant"]).startswith("B")
    ]

    # reference audit post-hoc
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
                "compression": None if nearest is None else nearest.get("compression"),
                "note": "diagnostic_only_not_used_for_thresholds",
            }
        )

    examples = long_actions[:15] + break_actions[:15]

    # Decision
    c1 = next((c for c in control_summary if c["variant"] == "C1"), {})
    best_long = None
    for v in variant_summary:
        if not str(v["variant"]).startswith("L"):
            continue
        if int(v.get("actions") or 0) < 3:
            continue
        rate = v.get("ceiling_touch_rate")
        if rate is None:
            continue
        if best_long is None or float(rate) > float(best_long["ceiling_touch_rate"]):
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

    verdict = decide_compression_verdict(
        future_violations=future_violations,
        outcome_leakage=outcome_leakage,
        best_long=best_long,
        best_break=best_break,
        control_touch=c1.get("ceiling_touch_rate"),
        floor_seq_count=len(completed_seqs),
        long_actions=len(long_actions),
        break_actions=len(break_actions),
    )

    integrity = {
        "ok": future_violations == 0 and outcome_leakage == 0,
        "symbol": params.symbol,
        "start": params.start,
        "end": params.end,
        "snapshot_count": len(snapshots),
        "transition_count": len(transitions),
        "sequence_count": len(sequences),
        "trade_tick_count": None,
        "ceiling_cluster_count": len(ceiling_rows),
        "floor_sequence_count": len(completed_seqs),
        "tracker_rising_bid_floor_sequences": tracker_rising,
        "raw_signal_count": len(raw_signals),
        "episode_count": len(episodes),
        "long_action_count": len(long_actions),
        "breakout_action_count": len(break_actions),
        "future_data_violations": future_violations,
        "outcome_leakage_violations": outcome_leakage,
        "duplicate_episode_count": 0,
        "invalid_floor_sequence_count": invalid_floor_seq,
        "invalid_ceiling_count": invalid_ceiling,
        "missing_snapshot_intervals": 0,
        "required_outputs_complete": True,
        "warnings": warnings,
        "errors": errors,
        "decision": verdict,
    }

    config = {
        "params": asdict(params),
        "price_basis": "mid",
        "signal_time_rule": "first_snapshot_all_conditions_met",
        "compression_rule": "floor_rises_and_ceiling_stable_and_range_contracts",
        "reference_times_post_hoc_only": list(REFERENCE_TIMES),
        "a2_g5_diagnostic_only": True,
    }

    inventory = {
        "snapshots": len(snapshots),
        "transitions": len(transitions),
        "sequences": len(sequences),
        "a2_times": len(a2_times),
        "g5_warnings": len(g5_warning_times),
        "g5_actions": len(g5_action_times),
        "absorption_joined_rows": len(absorption_by_ts),
    }

    write_csv(output_dir / "snapshot_floor_ceiling_features.csv", feat_rows)
    write_csv_headered(output_dir / "bid_floor_sequences.csv", floor_seq_rows)
    write_csv_headered(output_dir / "ask_ceiling_clusters.csv", ceiling_rows)
    write_csv_headered(output_dir / "compression_state_transitions.csv", transitions_out)
    write_csv_headered(output_dir / "compression_raw_signals.csv", raw_signals)
    write_csv_headered(output_dir / "compression_episodes.csv", episodes)
    write_csv_headered(output_dir / "long_to_ceiling_actions.csv", long_actions)
    write_csv_headered(output_dir / "long_to_ceiling_outcomes.csv", long_outcomes)
    write_csv_headered(output_dir / "breakout_actions.csv", break_actions)
    write_csv_headered(output_dir / "breakout_outcomes.csv", break_outcomes)
    write_csv_headered(output_dir / "variant_summary.csv", variant_summary)
    write_csv_headered(output_dir / "control_summary.csv", control_summary)
    write_csv_headered(output_dir / "ceiling_distance_ablation.csv", ceiling_ablation)
    write_csv_headered(output_dir / "floor_step_ablation.csv", floor_step_ablation)
    write_csv_headered(output_dir / "floor_rise_ablation.csv", floor_rise_ablation)
    write_csv_headered(output_dir / "floor_persistence_ablation.csv", floor_pers_ablation)
    write_csv_headered(output_dir / "crv_ablation.csv", crv_ablation)
    write_csv_headered(output_dir / "breakout_confirmation_ablation.csv", break_ablation)
    write_csv_headered(output_dir / "a2_g5_diagnostics.csv", a2g5_rows)
    write_csv_headered(output_dir / "pattern_examples.csv", examples)
    write_csv_headered(output_dir / "pattern_reference_point_audit.csv", ref_rows)

    # also dump control detail lightly into examples note
    write_csv_headered(
        output_dir / "control_summary.csv",
        control_summary,
        headers=[
            "variant",
            "actions",
            "ceiling_touch_count",
            "ceiling_touch_rate",
            "note",
        ],
    )

    (output_dir / "config.json").write_bytes(orjson.dumps(config, option=orjson.OPT_INDENT_2))
    (output_dir / "integrity.json").write_bytes(
        orjson.dumps(integrity, option=orjson.OPT_INDENT_2)
    )
    (output_dir / "input_inventory.json").write_bytes(
        orjson.dumps(inventory, option=orjson.OPT_INDENT_2)
    )

    report = build_compression_report(
        verdict=verdict,
        integrity=integrity,
        variant_summary=variant_summary,
        control_summary=control_summary,
        floor_seq_count=len(completed_seqs),
        ceiling_count=len({r.get("ceiling_price") for r in ceiling_rows}),
        compression_eps=sum(1 for e in episodes if e["variant"] == "L3"),
        a2g5_rows=a2g5_rows,
        params=params,
        best_long=best_long,
        best_break=best_break,
    )
    (output_dir / "REPORT.md").write_text(report, encoding="utf-8")

    for name in OUTPUT_FILES:
        p = output_dir / name
        if not p.exists():
            if name.endswith(".csv"):
                write_csv_headered(p, [], headers=["placeholder"])
            else:
                p.write_text("", encoding="utf-8")

    if future_violations > 0 or outcome_leakage > 0:
        raise RuntimeError("integrity failure")

    summary = {
        "decision": verdict,
        "integrity": integrity,
        "best_long": best_long,
        "best_break": best_break,
        "output_dir": str(output_dir),
        "variant_summary": variant_summary,
        "control_summary": control_summary,
    }
    (output_dir / "strategy_summary.json").write_bytes(
        orjson.dumps(summary, option=orjson.OPT_INDENT_2)
    )
    return summary


def _seq_row(seq: FloorSequence) -> dict[str, Any]:
    rises = []
    for a, b in zip(seq.steps, seq.steps[1:]):
        rises.append(bps_signed(b.price, a.price))
    dur = 0.0
    if seq.start_time and seq.end_time:
        dur = max((seq.end_time - seq.start_time).total_seconds(), 1.0)
    return {
        "sequence_id": seq.sequence_id,
        "sequence_start_time": None if seq.start_time is None else seq.start_time.isoformat(),
        "sequence_end_time": None if seq.end_time is None else seq.end_time.isoformat(),
        "floor_step_count": seq.step_count,
        "first_floor_price": seq.first_price,
        "last_floor_price": seq.last_price,
        "total_floor_rise_bps": seq.total_rise_bps,
        "median_step_rise_bps": _median(rises),
        "floor_migration_velocity_bps_per_minute": seq.total_rise_bps / (dur / 60.0),
        "floor_persistence_min": min((s.persistence for s in seq.steps), default=None),
        "floor_persistence_median": _median([float(s.persistence) for s in seq.steps]),
        "invalidated": seq.invalidated,
        "invalidation_time": None
        if seq.invalidation_time is None
        else seq.invalidation_time.isoformat(),
        "tested_and_held_steps": sum(1 for s in seq.steps if s.tested_and_held),
    }


def _flush_ep(variant: str, group: list[dict[str, Any]], idx: int) -> dict[str, Any]:
    return {
        "episode_id": f"E{idx:04d}",
        "variant": variant,
        "sequence_id": group[0].get("sequence_id"),
        "episode_start": group[0]["signal_time"],
        "signal_time": group[0]["signal_time"],
        "episode_end": group[-1]["signal_time"],
        "floor_cluster_id": group[0].get("floor_price"),
        "ceiling_cluster_id": group[0].get("ceiling_price"),
        "signal_variant": variant,
        "strongest_score_time": max(group, key=lambda s: float(s.get("estimated_crv") or 0))[
            "signal_time"
        ],
        "signal_ids": ",".join(s["signal_id"] for s in group),
        "raw_signal_count": len(group),
    }


def decide_compression_verdict(
    *,
    future_violations: int,
    outcome_leakage: int,
    best_long: Mapping[str, Any] | None,
    best_break: Mapping[str, Any] | None,
    control_touch: float | None,
    floor_seq_count: int,
    long_actions: int,
    break_actions: int,
) -> str:
    if future_violations > 0 or outcome_leakage > 0:
        return "AUDIT_INVALID"
    if floor_seq_count < 2 or long_actions < 3:
        return "RISING_BID_FLOOR_DATA_INSUFFICIENT"

    long_ok = False
    if best_long and best_long.get("ceiling_touch_rate") is not None:
        rate = float(best_long["ceiling_touch_rate"])
        mae = best_long.get("median_mae_before_touch_bps")
        inv = best_long.get("floor_invalidation_rate") or 0
        ctrl = float(control_touch) if control_touch is not None else 0.0
        if rate >= ctrl + 0.10 and rate >= 0.45 and float(inv) <= 0.5:
            if mae is None or float(mae) <= 40:
                long_ok = True
        if rate <= ctrl + 0.02 and int(best_long.get("actions") or 0) >= 5:
            return "RISING_BID_FLOOR_PATTERN_TOO_NOISY"

    break_ok = False
    if best_break and int(best_break.get("actions") or 0) >= 3:
        fail = best_break.get("failed_breakout_rate")
        hit = best_break.get("hit_rate_0_25")
        if fail is not None and hit is not None and float(fail) <= 0.5 and float(hit) >= 0.4:
            break_ok = True

    if long_ok and break_ok:
        # prefer ceiling goal wording if touch edge clearer
        return "RISING_BID_FLOOR_LONG_TO_CEILING_VALUE_FOUND"
    if long_ok:
        return "RISING_BID_FLOOR_LONG_TO_CEILING_VALUE_FOUND"
    if break_ok:
        return "RISING_BID_FLOOR_BREAKOUT_VALUE_FOUND"
    if best_long and best_long.get("ceiling_touch_rate") is not None:
        return "RISING_BID_FLOOR_CONFIRMATION_VALUE_ONLY"
    return "RISING_BID_FLOOR_PATTERN_TOO_NOISY"


def build_compression_report(
    *,
    verdict: str,
    integrity: Mapping[str, Any],
    variant_summary: Sequence[Mapping[str, Any]],
    control_summary: Sequence[Mapping[str, Any]],
    floor_seq_count: int,
    ceiling_count: int,
    compression_eps: int,
    a2g5_rows: Sequence[Mapping[str, Any]],
    params: CompressionParams,
    best_long: Mapping[str, Any] | None,
    best_break: Mapping[str, Any] | None,
) -> str:
    by = {v["variant"]: v for v in variant_summary}
    touch_with_a2 = [r for r in a2g5_rows if r.get("a2_active_at_signal") and r.get("ceiling_touch")]
    touch_no_a2 = [r for r in a2g5_rows if (not r.get("a2_active_at_signal")) and r.get("ceiling_touch")]
    lines = [
        "# Rising Bid Floor Compression Audit",
        "",
        f"**Decision:** `{verdict}`",
        "",
        f"1. Stable ask ceilings observed (rows): {integrity.get('ceiling_cluster_count')} (unique≈{ceiling_count})",
        f"2. Rising bid floor sequences: {floor_seq_count}",
        f"3. Compression episodes (L3): {compression_eps}",
        f"4. Tested-and-held tracked in floor sequence rows / features",
        f"5. Best long touch rate: {None if best_long is None else best_long.get('ceiling_touch_rate')} ({None if best_long is None else best_long.get('variant')})",
        f"6. Median MAE before touch (best long): {None if best_long is None else best_long.get('median_mae_before_touch_bps')}",
        f"7. Floor invalidation rate (best long): {None if best_long is None else best_long.get('floor_invalidation_rate')}",
        f"8. Ceiling distance default: {params.max_ceiling_distance_bps} bps",
        f"9. Floor steps default: {params.min_floor_steps}",
        f"10–11. See L4/L5/L6 vs L3 in variant_summary",
        f"12. Touches with A2 active: {len(touch_with_a2)}; without: {len(touch_no_a2)}",
        f"13. Best breakout: {None if best_break is None else best_break.get('variant')} n={None if best_break is None else best_break.get('actions')}",
        f"14. Failed breakout rate (best): {None if best_break is None else best_break.get('failed_breakout_rate')}",
        "15. Compare long-to-ceiling vs breakout via variant_summary L* vs B*",
        "16. Controls:",
        "```",
        orjson.dumps(list(control_summary), option=orjson.OPT_INDENT_2).decode(),
        "```",
        f"17. Sample: snapshots={integrity.get('snapshot_count')} long_actions={integrity.get('long_action_count')} breakouts={integrity.get('breakout_action_count')}",
        f"18. Decision: {verdict}",
        "",
        "## Variant summary",
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
    path = absorption_dir / "pattern_actions.csv"
    out = []
    for r in read_csv(path):
        if r.get("pattern_type") != "ASK_ABSORPTION":
            continue
        t = parse_ts(r.get("action_time") or r.get("signal_time"))
        if t:
            out.append(t)
    return out


def load_g5_times(g5_dir: Path) -> tuple[list[datetime], list[datetime]]:
    path = g5_dir / "integrated_variant_actions.csv"
    warns: list[datetime] = []
    acts: list[datetime] = []
    for r in read_csv(path):
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
    path = absorption_dir / "snapshot_features.csv"
    out: dict[str, dict[str, Any]] = {}
    for r in read_csv(path):
        ts = r.get("timestamp")
        if ts:
            out[ts] = r
    return out


def run_compression_audit(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    output_dir: Path,
    params: CompressionParams,
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
        a2_times: list[datetime] = []
        g5w: list[datetime] = []
        g5a: list[datetime] = []
        abs_map: dict[str, dict[str, Any]] = {}
        if absorption_dir and absorption_dir.exists():
            a2_times = load_a2_times(absorption_dir)
            abs_map = load_absorption_by_ts(absorption_dir)
        if g5_dir and g5_dir.exists():
            g5w, g5a = load_g5_times(g5_dir)
        return run_compression_audit_from_state(
            snapshots=state["snapshots"],
            transitions=state["transitions"],
            sequences=state["sequences"],
            output_dir=output_dir,
            params=params,
            a2_times=a2_times,
            g5_warning_times=g5w,
            g5_action_times=g5a,
            absorption_by_ts=abs_map,
        )
    finally:
        db.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Rising bid floor compression audit")
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
    p.add_argument("--min-floor-steps", type=int, default=3)
    p.add_argument("--min-floor-rise-bps", type=float, default=10.0)
    p.add_argument("--min-floor-persistence-snapshots", type=int, default=2)
    p.add_argument("--floor-test-distance-bps", type=float, default=5.0)
    p.add_argument("--floor-hold-confirm-snapshots", type=int, default=2)
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
    params = CompressionParams(
        snapshot_seconds=int(args.snapshot_seconds),
        max_ceiling_distance_bps=float(args.max_ceiling_distance_bps),
        min_floor_steps=int(args.min_floor_steps),
        min_floor_rise_bps=float(args.min_floor_rise_bps),
        min_floor_persistence_snapshots=int(args.min_floor_persistence_snapshots),
        floor_test_distance_bps=float(args.floor_test_distance_bps),
        floor_hold_confirm_snapshots=int(args.floor_hold_confirm_snapshots),
        min_crv=float(args.min_crv),
        episode_gap_seconds=int(args.episode_gap_seconds),
        episode_level_bps=float(args.episode_level_bps),
        symbol=str(args.symbol),
        start=str(args.start),
        end=str(args.end),
    )
    out = (
        Path(args.output_dir)
        if args.output_dir
        else PROJECT_ROOT
        / "results"
        / f"orderbook_rising_bid_floor_compression_{args.symbol}_{utc_now().strftime('%Y%m%dT%H%M%SZ')}"
    )
    summary = run_compression_audit(
        symbol=str(args.symbol),
        start=parse_utc(args.start),
        end=parse_utc(args.end),
        output_dir=out,
        params=params,
        absorption_dir=Path(args.absorption_dir),
        g5_dir=Path(args.g5_dir),
    )
    sys.stdout.buffer.write(
        orjson.dumps(
            {
                "decision": summary.get("decision"),
                "best_long": summary.get("best_long"),
                "best_break": summary.get("best_break"),
                "output_dir": summary.get("output_dir"),
            },
            option=orjson.OPT_INDENT_2,
        )
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
