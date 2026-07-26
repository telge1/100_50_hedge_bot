"""Causal bid-weakening / short-term reversal warning audit (research only).

Detects whether bid liquidity thinning after an uptrend precedes short-term
down moves. Warnings are diagnostic only — bid thinning alone is never a
confirmed reversal.

Strict causality: at time ``t`` only data with timestamp ``<= t`` may drive
state transitions. Forward outcomes use prices strictly after the warning time.

Does not place live orders. Does not modify existing entry audits.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

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
    _ensure_utc,
    _fmt,
    prepare_tracker_state,
)
from orderbook_analyse.wall_movement_tracker import (
    WALL_PULLED,
    WALL_REPLACED_HIGHER,
    WALL_REPLACED_LOWER,
    SequenceRecord,
    SnapshotRecord,
    TransitionRecord,
)

logger = logging.getLogger(__name__)

TREND_UP_HEALTHY = "TREND_UP_HEALTHY"
BID_SUPPORT_WEAKENING = "BID_SUPPORT_WEAKENING"
REVERSAL_WARNING = "REVERSAL_WARNING"
REVERSAL_CONFIRMED = "REVERSAL_CONFIRMED"
WARNING_FAILED = "WARNING_FAILED"
EXPIRED = "EXPIRED"

HORIZONS_SEC = (30, 60, 180, 300, 600)
DOWN_THRESHOLDS_PCT = (0.10, 0.25, 0.50)
UP_THRESHOLDS_PCT = (0.10, 0.25, 0.50)


@dataclass
class BidWeakeningParams:
    sample_seconds: int = 30
    target_bps: float = 10.0
    near_min_distance_pct: float = 0.10
    near_max_distance_pct: float = 1.50
    near_top_n: int = 3
    warning_min_feature_count: int = 3
    warning_confirm_snapshots: int = 2
    warning_max_age_seconds: int = 300
    bid_notional_drop_pct: float = 25.0
    bid_wall_count_drop: int = 1
    nearest_bid_retreat_bps: float = 5.0
    bid_ask_ratio_drop_pct: float = 20.0
    local_high_lookback_seconds: int = 600
    lower_high_tolerance_bps: float = 8.0
    trend_up_min_bps: float = 3.0
    mid_down_confirm_snapshots: int = 2
    mid_down_confirm_bps: float = 5.0
    support_break_bps: float = 5.0
    false_warning_min_down_pct: float = 0.10
    cooldown_seconds: int = 90


@dataclass
class SnapshotFeatures:
    timestamp: datetime
    index: int
    mid: Decimal
    mid_change_bps_30s: float | None = None
    mid_change_bps_60s: float | None = None
    mid_change_bps_180s: float | None = None
    nearest_bid: Decimal | None = None
    nearest_bid_distance_bps: float | None = None
    nearest_bid_change_bps: float | None = None
    dominant_bid: Decimal | None = None
    dominant_bid_notional: Decimal | None = None
    dominant_bid_notional_change_pct: float | None = None
    active_bid_wall_count: int = 0
    active_bid_wall_notional_sum: Decimal = Decimal("0")
    active_bid_wall_notional_change_pct: float | None = None
    bid_wall_pull_count: int = 0
    bid_wall_shift_lower_count: int = 0
    bid_wall_shift_higher_count: int = 0
    nearest_ask: Decimal | None = None
    dominant_ask: Decimal | None = None
    dominant_ask_notional: Decimal | None = None
    active_ask_wall_count: int = 0
    active_ask_wall_notional_sum: Decimal = Decimal("0")
    ask_notional_change_pct: float | None = None
    bid_ask_notional_ratio: float | None = None
    trade_delta_30s: Decimal = Decimal("0")
    trade_delta_60s: Decimal = Decimal("0")
    trade_delta_180s: Decimal = Decimal("0")
    oi_change_30s: Decimal | None = None
    oi_change_60s: Decimal | None = None
    oi_change_180s: Decimal | None = None
    local_high: Decimal | None = None
    local_low: Decimal | None = None
    bars_since_local_high: int | None = None
    lower_high_confirmed: bool = False
    support_break_confirmed: bool = False
    local_support: Decimal | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "index": self.index,
            "mid": _fmt(self.mid),
            "mid_change_bps_30s": _r(self.mid_change_bps_30s),
            "mid_change_bps_60s": _r(self.mid_change_bps_60s),
            "mid_change_bps_180s": _r(self.mid_change_bps_180s),
            "nearest_bid": _fmt(self.nearest_bid),
            "nearest_bid_distance_bps": _r(self.nearest_bid_distance_bps),
            "nearest_bid_change_bps": _r(self.nearest_bid_change_bps),
            "dominant_bid": _fmt(self.dominant_bid),
            "dominant_bid_notional": _fmt(self.dominant_bid_notional),
            "dominant_bid_notional_change_pct": _r(self.dominant_bid_notional_change_pct),
            "active_bid_wall_count": self.active_bid_wall_count,
            "active_bid_wall_notional_sum": _fmt(self.active_bid_wall_notional_sum),
            "active_bid_wall_notional_change_pct": _r(
                self.active_bid_wall_notional_change_pct
            ),
            "bid_wall_pull_count": self.bid_wall_pull_count,
            "bid_wall_shift_lower_count": self.bid_wall_shift_lower_count,
            "bid_wall_shift_higher_count": self.bid_wall_shift_higher_count,
            "nearest_ask": _fmt(self.nearest_ask),
            "dominant_ask": _fmt(self.dominant_ask),
            "dominant_ask_notional": _fmt(self.dominant_ask_notional),
            "active_ask_wall_count": self.active_ask_wall_count,
            "active_ask_wall_notional_sum": _fmt(self.active_ask_wall_notional_sum),
            "ask_notional_change_pct": _r(self.ask_notional_change_pct),
            "bid_ask_notional_ratio": _r(self.bid_ask_notional_ratio),
            "trade_delta_30s": _fmt(self.trade_delta_30s),
            "trade_delta_60s": _fmt(self.trade_delta_60s),
            "trade_delta_180s": _fmt(self.trade_delta_180s),
            "oi_change_30s": _fmt(self.oi_change_30s),
            "oi_change_60s": _fmt(self.oi_change_60s),
            "oi_change_180s": _fmt(self.oi_change_180s),
            "local_high": _fmt(self.local_high),
            "local_low": _fmt(self.local_low),
            "bars_since_local_high": self.bars_since_local_high,
            "lower_high_confirmed": self.lower_high_confirmed,
            "support_break_confirmed": self.support_break_confirmed,
            "local_support": _fmt(self.local_support),
        }


@dataclass
class WarningEvent:
    warning_id: str
    warning_time: datetime
    warning_index: int
    state: str
    score: int
    feature_count: int
    features_true: list[str]
    mid: Decimal
    local_high: Decimal | None
    nearest_bid: Decimal | None
    dominant_bid: Decimal | None
    dominant_bid_notional: Decimal | None
    active_bid_wall_count: int
    active_bid_wall_notional_sum: Decimal
    nearest_ask: Decimal | None
    dominant_ask: Decimal | None
    active_ask_wall_notional_sum: Decimal
    bid_ask_notional_ratio: float | None
    trade_delta: Decimal
    oi_change: Decimal | None
    lower_high_confirmed: bool
    support_break_confirmed: bool
    local_support: Decimal | None
    expire_deadline: datetime
    terminal_state: str | None = None
    terminal_time: datetime | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "warning_id": self.warning_id,
            "warning_time": self.warning_time.isoformat(),
            "warning_index": self.warning_index,
            "state": self.state,
            "score": self.score,
            "feature_count": self.feature_count,
            "features_true": ",".join(self.features_true),
            "mid": _fmt(self.mid),
            "local_high": _fmt(self.local_high),
            "nearest_bid": _fmt(self.nearest_bid),
            "dominant_bid": _fmt(self.dominant_bid),
            "dominant_bid_notional": _fmt(self.dominant_bid_notional),
            "active_bid_wall_count": self.active_bid_wall_count,
            "active_bid_wall_notional_sum": _fmt(self.active_bid_wall_notional_sum),
            "nearest_ask": _fmt(self.nearest_ask),
            "dominant_ask": _fmt(self.dominant_ask),
            "active_ask_wall_notional_sum": _fmt(self.active_ask_wall_notional_sum),
            "bid_ask_notional_ratio": _r(self.bid_ask_notional_ratio),
            "trade_delta": _fmt(self.trade_delta),
            "oi_change": _fmt(self.oi_change),
            "lower_high_confirmed": self.lower_high_confirmed,
            "support_break_confirmed": self.support_break_confirmed,
            "local_support": _fmt(self.local_support),
            "terminal_state": self.terminal_state,
            "terminal_time": None
            if self.terminal_time is None
            else self.terminal_time.isoformat(),
        }


def _r(value: float | None, digits: int = 6) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _pct_change(new: Decimal, old: Decimal) -> float | None:
    if old == 0:
        return None
    return float((new - old) / abs(old) * Decimal("100"))


def _bps_change(new: Decimal, old: Decimal) -> float | None:
    if old == 0:
        return None
    return float((new - old) / old * Decimal("10000"))


def _wall_price(wall: Any) -> Decimal | None:
    return None if wall is None else wall.price


def _wall_notional(wall: Any) -> Decimal | None:
    return None if wall is None else wall.notional


def _active_walls(snap: SnapshotRecord, *, side: str) -> list[Any]:
    if side == "bid":
        walls = list(snap.near_bids) if snap.near_bids else list(snap.top_bid_walls)
        if snap.nearest_bid is not None and snap.nearest_bid not in walls:
            walls = [snap.nearest_bid, *walls]
        if snap.dominant_bid is not None and snap.dominant_bid not in walls:
            walls = [*walls, snap.dominant_bid]
    else:
        walls = list(snap.near_asks) if snap.near_asks else list(snap.top_ask_walls)
        if snap.nearest_ask is not None and snap.nearest_ask not in walls:
            walls = [snap.nearest_ask, *walls]
        if snap.dominant_ask is not None and snap.dominant_ask not in walls:
            walls = [*walls, snap.dominant_ask]
    # unique by price
    seen: set[Decimal] = set()
    out: list[Any] = []
    for w in walls:
        if w is None or w.price in seen:
            continue
        if not getattr(w, "is_wall", True):
            continue
        seen.add(w.price)
        out.append(w)
    return out


def _find_index_at_or_before(
    snapshots: Sequence[SnapshotRecord], *, as_of: datetime, max_index: int
) -> int | None:
    t = _ensure_utc(as_of)
    best: int | None = None
    for i in range(0, max_index + 1):
        if _ensure_utc(snapshots[i].timestamp) <= t:
            best = i
        else:
            break
    return best


def _sum_delta(
    snapshots: Sequence[SnapshotRecord], *, end_index: int, lookback_seconds: int
) -> Decimal:
    end_ts = _ensure_utc(snapshots[end_index].timestamp)
    start_ts = end_ts - timedelta(seconds=lookback_seconds)
    total = Decimal("0")
    for i in range(end_index + 1):
        ts = _ensure_utc(snapshots[i].timestamp)
        if start_ts < ts <= end_ts:
            total += snapshots[i].trade_delta_notional
    return total


def _sum_oi(
    snapshots: Sequence[SnapshotRecord], *, end_index: int, lookback_seconds: int
) -> Decimal | None:
    end_ts = _ensure_utc(snapshots[end_index].timestamp)
    start_ts = end_ts - timedelta(seconds=lookback_seconds)
    changes: list[Decimal] = []
    for i in range(end_index + 1):
        ts = _ensure_utc(snapshots[i].timestamp)
        if start_ts < ts <= end_ts and snapshots[i].oi_change_since_prev is not None:
            changes.append(snapshots[i].oi_change_since_prev)  # type: ignore[arg-type]
    if not changes:
        return None
    return sum(changes, Decimal("0"))


def _mid_change_bps(
    snapshots: Sequence[SnapshotRecord], *, end_index: int, lookback_seconds: int
) -> float | None:
    end_ts = _ensure_utc(snapshots[end_index].timestamp)
    target = end_ts - timedelta(seconds=lookback_seconds)
    j = _find_index_at_or_before(snapshots, as_of=target, max_index=end_index)
    if j is None or j == end_index:
        return None
    return _bps_change(snapshots[end_index].mid_price, snapshots[j].mid_price)


def _transitions_between(
    transitions: Sequence[TransitionRecord],
    *,
    start: datetime,
    end: datetime,
    side: str,
) -> list[TransitionRecord]:
    a = _ensure_utc(start)
    b = _ensure_utc(end)
    return [
        t
        for t in transitions
        if t.side == side and a < _ensure_utc(t.current_timestamp) <= b
    ]


def compute_local_extremes(
    snapshots: Sequence[SnapshotRecord],
    *,
    end_index: int,
    lookback_seconds: int,
) -> tuple[Decimal, Decimal, int, Decimal | None]:
    """Return (local_high, local_low, bars_since_high, support_candidate).

    Support candidate = nearest bid at the bar of the local high (causal).
    """
    end_ts = _ensure_utc(snapshots[end_index].timestamp)
    start_ts = end_ts - timedelta(seconds=lookback_seconds)
    window_idx = [
        i
        for i in range(end_index + 1)
        if start_ts <= _ensure_utc(snapshots[i].timestamp) <= end_ts
    ]
    if not window_idx:
        window_idx = [end_index]
    highs = [(i, snapshots[i].mid_price) for i in window_idx]
    high_i, local_high = max(highs, key=lambda x: (x[1], x[0]))
    local_low = min(snapshots[i].mid_price for i in window_idx)
    bars_since = end_index - high_i
    support = _wall_price(snapshots[high_i].nearest_bid) or snapshots[high_i].mid_price
    return local_high, local_low, bars_since, support


def compute_features_at(
    *,
    index: int,
    snapshots: Sequence[SnapshotRecord],
    transitions: Sequence[TransitionRecord],
    params: BidWeakeningParams,
    prior_local_high: Decimal | None = None,
) -> SnapshotFeatures:
    """Causal feature vector using only snapshots/transitions ``<= index``."""
    snap = snapshots[index]
    prev = snapshots[index - 1] if index > 0 else None
    bid_walls = _active_walls(snap, side="bid")
    ask_walls = _active_walls(snap, side="ask")
    bid_sum = sum((w.notional for w in bid_walls), Decimal("0"))
    ask_sum = sum((w.notional for w in ask_walls), Decimal("0"))
    nb = snap.nearest_bid
    db = snap.dominant_bid or snap.strongest_bid
    na = snap.nearest_ask
    da = snap.dominant_ask or snap.strongest_ask

    nearest_bid_dist = None
    if nb is not None and snap.mid_price != 0:
        nearest_bid_dist = float(
            (snap.mid_price - nb.price) / snap.mid_price * Decimal("10000")
        )

    nearest_bid_chg = None
    if prev is not None and prev.nearest_bid is not None and nb is not None:
        nearest_bid_chg = _bps_change(nb.price, prev.nearest_bid.price)

    dom_bid_chg = None
    if prev is not None:
        prev_db = prev.dominant_bid or prev.strongest_bid
        if prev_db is not None and db is not None:
            dom_bid_chg = _pct_change(db.notional, prev_db.notional)

    bid_sum_chg = None
    ask_sum_chg = None
    if prev is not None:
        prev_bid_sum = sum(
            (w.notional for w in _active_walls(prev, side="bid")), Decimal("0")
        )
        prev_ask_sum = sum(
            (w.notional for w in _active_walls(prev, side="ask")), Decimal("0")
        )
        bid_sum_chg = _pct_change(bid_sum, prev_bid_sum) if prev_bid_sum else None
        ask_sum_chg = _pct_change(ask_sum, prev_ask_sum) if prev_ask_sum else None

    interval_start = prev.timestamp if prev is not None else snap.timestamp
    bid_tx = _transitions_between(
        transitions, start=interval_start, end=snap.timestamp, side="bid"
    )
    pull_n = sum(1 for t in bid_tx if t.classification == WALL_PULLED)
    shift_lower = sum(1 for t in bid_tx if t.classification == WALL_REPLACED_LOWER)
    shift_higher = sum(1 for t in bid_tx if t.classification == WALL_REPLACED_HIGHER)

    ratio = None
    if ask_sum > 0:
        ratio = float(bid_sum / ask_sum)

    local_high, local_low, bars_since, support = compute_local_extremes(
        snapshots,
        end_index=index,
        lookback_seconds=params.local_high_lookback_seconds,
    )

    lower_high = False
    if prior_local_high is not None:
        tol = Decimal(str(params.lower_high_tolerance_bps)) / Decimal("10000")
        threshold = prior_local_high * (Decimal("1") - tol)
        # Price has left the high and failed to reclaim the prior swing high.
        if (
            bars_since >= 1
            and snap.mid_price <= threshold
            and local_high <= prior_local_high
        ):
            lower_high = True
        # Or the current bar itself is a lower swing high.
        elif bars_since == 0 and local_high <= threshold and local_high < prior_local_high:
            lower_high = True

    support_break = False
    if support is not None:
        break_tol = Decimal(str(params.support_break_bps)) / Decimal("10000")
        if snap.mid_price < support * (Decimal("1") - break_tol):
            support_break = True

    return SnapshotFeatures(
        timestamp=snap.timestamp,
        index=index,
        mid=snap.mid_price,
        mid_change_bps_30s=_mid_change_bps(snapshots, end_index=index, lookback_seconds=30),
        mid_change_bps_60s=_mid_change_bps(snapshots, end_index=index, lookback_seconds=60),
        mid_change_bps_180s=_mid_change_bps(
            snapshots, end_index=index, lookback_seconds=180
        ),
        nearest_bid=_wall_price(nb),
        nearest_bid_distance_bps=nearest_bid_dist,
        nearest_bid_change_bps=nearest_bid_chg,
        dominant_bid=_wall_price(db),
        dominant_bid_notional=_wall_notional(db),
        dominant_bid_notional_change_pct=dom_bid_chg,
        active_bid_wall_count=len(bid_walls),
        active_bid_wall_notional_sum=bid_sum,
        active_bid_wall_notional_change_pct=bid_sum_chg,
        bid_wall_pull_count=pull_n,
        bid_wall_shift_lower_count=shift_lower,
        bid_wall_shift_higher_count=shift_higher,
        nearest_ask=_wall_price(na),
        dominant_ask=_wall_price(da),
        dominant_ask_notional=_wall_notional(da),
        active_ask_wall_count=len(ask_walls),
        active_ask_wall_notional_sum=ask_sum,
        ask_notional_change_pct=ask_sum_chg,
        bid_ask_notional_ratio=ratio,
        trade_delta_30s=_sum_delta(snapshots, end_index=index, lookback_seconds=30),
        trade_delta_60s=_sum_delta(snapshots, end_index=index, lookback_seconds=60),
        trade_delta_180s=_sum_delta(snapshots, end_index=index, lookback_seconds=180),
        oi_change_30s=_sum_oi(snapshots, end_index=index, lookback_seconds=30),
        oi_change_60s=_sum_oi(snapshots, end_index=index, lookback_seconds=60),
        oi_change_180s=_sum_oi(snapshots, end_index=index, lookback_seconds=180),
        local_high=local_high,
        local_low=local_low,
        bars_since_local_high=bars_since,
        lower_high_confirmed=lower_high,
        support_break_confirmed=support_break,
        local_support=support,
    )


def warning_score_components(
    feat: SnapshotFeatures,
    *,
    params: BidWeakeningParams,
    baseline: SnapshotFeatures | None,
) -> list[tuple[str, int]]:
    """Diagnostic score components (research only)."""
    comps: list[tuple[str, int]] = []
    drop = feat.active_bid_wall_notional_change_pct
    if drop is not None and drop <= -params.bid_notional_drop_pct:
        comps.append(("bid_notional_drop", 2))
    elif drop is not None and drop < 0:
        comps.append(("bid_notional_soft_drop", 1))

    if baseline is not None:
        count_drop = baseline.active_bid_wall_count - feat.active_bid_wall_count
        if count_drop >= params.bid_wall_count_drop:
            comps.append(("bid_wall_count_drop", 2))

    if (
        feat.nearest_bid_change_bps is not None
        and feat.nearest_bid_change_bps <= -params.nearest_bid_retreat_bps
    ):
        comps.append(("nearest_bid_retreat", 2))

    if feat.bid_wall_pull_count > 0:
        comps.append(("bid_wall_pulled", 2))
    if feat.bid_wall_shift_lower_count > 0:
        comps.append(("bid_wall_shift_lower", 1))

    if (
        feat.dominant_bid_notional_change_pct is not None
        and feat.dominant_bid_notional_change_pct <= -params.bid_notional_drop_pct
    ):
        comps.append(("dominant_bid_notional_drop", 2))

    if baseline is not None and baseline.bid_ask_notional_ratio and feat.bid_ask_notional_ratio:
        ratio_chg = (
            (feat.bid_ask_notional_ratio - baseline.bid_ask_notional_ratio)
            / abs(baseline.bid_ask_notional_ratio)
            * 100
        )
        if ratio_chg <= -params.bid_ask_ratio_drop_pct:
            comps.append(("bid_ask_ratio_drop", 2))

    if feat.ask_notional_change_pct is not None and feat.ask_notional_change_pct > 0:
        comps.append(("ask_notional_up", 1))

    if feat.trade_delta_60s < 0:
        comps.append(("trade_delta_negative", 2))
    elif feat.trade_delta_30s < 0:
        comps.append(("trade_delta_soft_negative", 1))

    if feat.bars_since_local_high is not None and feat.bars_since_local_high >= 1:
        comps.append(("no_new_high", 1))
    if feat.lower_high_confirmed:
        comps.append(("lower_high", 2))

    if feat.oi_change_60s is not None and feat.oi_change_60s < 0 and feat.trade_delta_60s < 0:
        comps.append(("oi_supports_down", 1))

    return comps


def is_trend_up_healthy(feat: SnapshotFeatures, prev: SnapshotFeatures | None) -> bool:
    mid_up = (feat.mid_change_bps_60s or 0) > 0 or (feat.mid_change_bps_30s or 0) > 0
    bid_up = (
        feat.nearest_bid_change_bps is not None and feat.nearest_bid_change_bps >= 0
    ) or (feat.bid_wall_shift_higher_count > 0)
    support_ok = (
        feat.active_bid_wall_notional_change_pct is None
        or feat.active_bid_wall_notional_change_pct >= -5.0
    )
    return bool(mid_up and (bid_up or support_ok) and not feat.support_break_confirmed)


def is_bid_weakening(
    feat: SnapshotFeatures,
    *,
    params: BidWeakeningParams,
    baseline: SnapshotFeatures | None,
) -> bool:
    signals = 0
    if (
        feat.active_bid_wall_notional_change_pct is not None
        and feat.active_bid_wall_notional_change_pct < 0
    ):
        signals += 1
    if baseline is not None and feat.active_bid_wall_count < baseline.active_bid_wall_count:
        signals += 1
    if feat.nearest_bid_change_bps is not None and feat.nearest_bid_change_bps < 0:
        signals += 1
    if (
        feat.dominant_bid_notional_change_pct is not None
        and feat.dominant_bid_notional_change_pct < 0
    ):
        signals += 1
    if feat.bid_wall_pull_count > 0 or feat.bid_wall_shift_lower_count > 0:
        signals += 1
    return signals >= 2


def bid_rebuilt(feat: SnapshotFeatures, *, params: BidWeakeningParams) -> bool:
    rebuilt = 0
    if (
        feat.active_bid_wall_notional_change_pct is not None
        and feat.active_bid_wall_notional_change_pct > 0
    ):
        rebuilt += 1
    if feat.nearest_bid_change_bps is not None and feat.nearest_bid_change_bps > 0:
        rebuilt += 1
    if feat.bid_wall_shift_higher_count > 0:
        rebuilt += 1
    if feat.ask_notional_change_pct is not None and feat.ask_notional_change_pct < 0:
        # ask thinning while bids rebuild — bullish recovery
        rebuilt += 1
    return rebuilt >= 2


def new_high_vs_warning(feat: SnapshotFeatures, warning: WarningEvent) -> bool:
    if warning.local_high is None:
        return feat.mid > warning.mid
    return feat.mid > warning.local_high


def reversal_confirmed_now(
    feat: SnapshotFeatures,
    *,
    params: BidWeakeningParams,
    down_streak: int,
) -> bool:
    if feat.support_break_confirmed:
        return True
    if down_streak >= params.mid_down_confirm_snapshots and (
        (feat.mid_change_bps_60s or 0) <= -params.mid_down_confirm_bps
        or (feat.mid_change_bps_30s or 0) <= -params.mid_down_confirm_bps
    ):
        return True
    return False


@dataclass
class MachineState:
    state: str = TREND_UP_HEALTHY
    weakening_streak: int = 0
    warning_streak: int = 0
    down_streak: int = 0
    baseline: SnapshotFeatures | None = None
    active_warning: WarningEvent | None = None
    session_high_memory: Decimal | None = None
    last_warning_time: datetime | None = None


def advance_state(
    *,
    machine: MachineState,
    feat: SnapshotFeatures,
    params: BidWeakeningParams,
    warning_seq: int,
) -> tuple[MachineState, WarningEvent | None, dict[str, Any]]:
    """Advance causal state machine by one snapshot. Returns (machine, new_warning, timeline_row)."""
    comps = warning_score_components(feat, params=params, baseline=machine.baseline)
    score = sum(p for _, p in comps)
    feature_names = [n for n, _ in comps]
    feature_count = len(feature_names)
    new_warning: WarningEvent | None = None

    # Update session high memory causally
    if machine.session_high_memory is None or feat.mid >= machine.session_high_memory:
        # Only update remembered swing when making equal/higher highs
        if feat.bars_since_local_high == 0:
            machine.session_high_memory = feat.local_high

    if (feat.mid_change_bps_30s or 0) < 0:
        machine.down_streak += 1
    else:
        machine.down_streak = 0

    prev_state = machine.state

    # Active warning lifecycle first
    if machine.active_warning is not None:
        w = machine.active_warning
        age = (_ensure_utc(feat.timestamp) - _ensure_utc(w.warning_time)).total_seconds()
        if age > params.warning_max_age_seconds:
            w.terminal_state = EXPIRED
            w.terminal_time = feat.timestamp
            machine.state = EXPIRED
            machine.active_warning = None
            machine.weakening_streak = 0
            machine.warning_streak = 0
        elif new_high_vs_warning(feat, w) or bid_rebuilt(feat, params=params):
            w.terminal_state = WARNING_FAILED
            w.terminal_time = feat.timestamp
            machine.state = WARNING_FAILED
            machine.active_warning = None
            machine.weakening_streak = 0
            machine.warning_streak = 0
        elif reversal_confirmed_now(feat, params=params, down_streak=machine.down_streak):
            w.terminal_state = REVERSAL_CONFIRMED
            w.terminal_time = feat.timestamp
            w.support_break_confirmed = feat.support_break_confirmed
            machine.state = REVERSAL_CONFIRMED
            machine.active_warning = None
            machine.weakening_streak = 0
            machine.warning_streak = 0
        else:
            machine.state = REVERSAL_WARNING
    else:
        # No active warning — classify environment
        weakening = is_bid_weakening(feat, params=params, baseline=machine.baseline)
        if weakening:
            machine.weakening_streak += 1
            if machine.baseline is None:
                machine.baseline = feat
            machine.state = BID_SUPPORT_WEAKENING
        elif is_trend_up_healthy(feat, machine.baseline):
            machine.weakening_streak = 0
            machine.warning_streak = 0
            machine.baseline = feat
            machine.state = TREND_UP_HEALTHY
        else:
            # Neutral / mixed — keep prior non-warning state lightly
            if machine.state not in {WARNING_FAILED, EXPIRED, REVERSAL_CONFIRMED}:
                if machine.weakening_streak > 0:
                    machine.state = BID_SUPPORT_WEAKENING
                else:
                    machine.state = TREND_UP_HEALTHY

        # Escalate to warning when enough confirmations
        enough_features = feature_count >= params.warning_min_feature_count
        if (
            machine.state == BID_SUPPORT_WEAKENING
            and enough_features
            and machine.weakening_streak >= params.warning_confirm_snapshots
        ):
            cooldown_ok = (
                machine.last_warning_time is None
                or (
                    _ensure_utc(feat.timestamp) - _ensure_utc(machine.last_warning_time)
                ).total_seconds()
                >= params.cooldown_seconds
            )
            if cooldown_ok:
                # Bid thinning alone must NOT instantly confirm reversal —
                # only open REVERSAL_WARNING.
                wid = f"W{warning_seq:04d}"
                new_warning = WarningEvent(
                    warning_id=wid,
                    warning_time=feat.timestamp,
                    warning_index=feat.index,
                    state=REVERSAL_WARNING,
                    score=score,
                    feature_count=feature_count,
                    features_true=feature_names,
                    mid=feat.mid,
                    local_high=feat.local_high,
                    nearest_bid=feat.nearest_bid,
                    dominant_bid=feat.dominant_bid,
                    dominant_bid_notional=feat.dominant_bid_notional,
                    active_bid_wall_count=feat.active_bid_wall_count,
                    active_bid_wall_notional_sum=feat.active_bid_wall_notional_sum,
                    nearest_ask=feat.nearest_ask,
                    dominant_ask=feat.dominant_ask,
                    active_ask_wall_notional_sum=feat.active_ask_wall_notional_sum,
                    bid_ask_notional_ratio=feat.bid_ask_notional_ratio,
                    trade_delta=feat.trade_delta_60s,
                    oi_change=feat.oi_change_60s,
                    lower_high_confirmed=feat.lower_high_confirmed,
                    support_break_confirmed=False,
                    local_support=feat.local_support,
                    expire_deadline=feat.timestamp
                    + timedelta(seconds=params.warning_max_age_seconds),
                )
                machine.active_warning = new_warning
                machine.last_warning_time = feat.timestamp
                machine.state = REVERSAL_WARNING
                machine.warning_streak = 0
            else:
                machine.warning_streak = 0
        else:
            if not weakening:
                machine.warning_streak = 0

    timeline = {
        "timestamp": feat.timestamp.isoformat(),
        "index": feat.index,
        "prev_state": prev_state,
        "state": machine.state,
        "score": score,
        "feature_count": feature_count,
        "features_true": ",".join(feature_names),
        "weakening_streak": machine.weakening_streak,
        "warning_streak": machine.warning_streak,
        "down_streak": machine.down_streak,
        "active_warning_id": None
        if machine.active_warning is None
        else machine.active_warning.warning_id,
        "mid": _fmt(feat.mid),
        "lower_high_confirmed": feat.lower_high_confirmed,
        "support_break_confirmed": feat.support_break_confirmed,
    }
    return machine, new_warning, timeline


def simulate_warning_outcomes(
    *,
    warning: WarningEvent,
    price_path: Sequence[tuple[datetime, Decimal]],
    end: datetime,
    params: BidWeakeningParams,
) -> list[dict[str, Any]]:
    """Forward metrics strictly after warning_time (no same-snapshot lookahead)."""
    t0 = _ensure_utc(warning.warning_time)
    entry = warning.mid
    rows: list[dict[str, Any]] = []

    def _path_until(t_end: datetime) -> list[tuple[datetime, Decimal]]:
        te = _ensure_utc(t_end)
        return [( _ensure_utc(ts), px) for ts, px in price_path if t0 < _ensure_utc(ts) <= te]

    horizons: list[tuple[str, datetime | None]] = [
        (str(h), t0 + timedelta(seconds=h)) for h in HORIZONS_SEC
    ]
    horizons.append(("session_end", None))

    for label, t_end in horizons:
        te = _ensure_utc(end) if t_end is None else min(_ensure_utc(end), _ensure_utc(t_end))
        path = _path_until(te)
        if not path:
            rows.append(
                {
                    "warning_id": warning.warning_id,
                    "horizon": label,
                    "warning_time": warning.warning_time.isoformat(),
                    "forward_return_pct": 0.0,
                    "max_adverse_up_pct": 0.0,
                    "max_favourable_down_pct": 0.0,
                    "warning_failed": False,
                    "reversal_confirmed": warning.terminal_state == REVERSAL_CONFIRMED,
                    **{f"first_touch_down_{x:.2f}".replace(".", "_"): False for x in DOWN_THRESHOLDS_PCT},
                    **{f"first_touch_up_{x:.2f}".replace(".", "_"): False for x in UP_THRESHOLDS_PCT},
                    **{f"time_to_down_{x:.2f}".replace(".", "_"): None for x in DOWN_THRESHOLDS_PCT},
                    **{f"time_to_up_{x:.2f}".replace(".", "_"): None for x in UP_THRESHOLDS_PCT},
                    "time_to_new_high": None,
                }
            )
            continue

        last = path[-1][1]
        fwd = float((last - entry) / entry * Decimal("100"))
        mfe_down = 0.0
        mae_up = 0.0
        t_down: dict[float, int | None] = {x: None for x in DOWN_THRESHOLDS_PCT}
        t_up: dict[float, int | None] = {x: None for x in UP_THRESHOLDS_PCT}
        touch_down = {x: False for x in DOWN_THRESHOLDS_PCT}
        touch_up = {x: False for x in UP_THRESHOLDS_PCT}
        time_to_new_high: int | None = None
        high_ref = warning.local_high or warning.mid

        for ts, px in path:
            up = float((px - entry) / entry * Decimal("100"))
            dn = float((entry - px) / entry * Decimal("100"))
            if up > mae_up:
                mae_up = up
            if dn > mfe_down:
                mfe_down = dn
            elapsed = int((_ensure_utc(ts) - t0).total_seconds())
            for thr in DOWN_THRESHOLDS_PCT:
                if not touch_down[thr] and dn >= thr:
                    touch_down[thr] = True
                    t_down[thr] = elapsed
            for thr in UP_THRESHOLDS_PCT:
                if not touch_up[thr] and up >= thr:
                    touch_up[thr] = True
                    t_up[thr] = elapsed
            if time_to_new_high is None and px > high_ref:
                time_to_new_high = elapsed

        warning_failed = (
            warning.terminal_state == WARNING_FAILED
            or (time_to_new_high is not None and not any(touch_down.values()))
        )
        row = {
            "warning_id": warning.warning_id,
            "horizon": label,
            "warning_time": warning.warning_time.isoformat(),
            "forward_return_pct": round(fwd, 6),
            "max_adverse_up_pct": round(mae_up, 6),
            "max_favourable_down_pct": round(mfe_down, 6),
            "warning_failed": warning_failed,
            "reversal_confirmed": warning.terminal_state == REVERSAL_CONFIRMED,
            "time_to_new_high": time_to_new_high,
            "terminal_state": warning.terminal_state,
        }
        for thr in DOWN_THRESHOLDS_PCT:
            key = f"{thr:.2f}".replace(".", "_")
            row[f"first_touch_down_{key}"] = touch_down[thr]
            row[f"time_to_down_{key}"] = t_down[thr]
        for thr in UP_THRESHOLDS_PCT:
            key = f"{thr:.2f}".replace(".", "_")
            row[f"first_touch_up_{key}"] = touch_up[thr]
            row[f"time_to_up_{key}"] = t_up[thr]
        rows.append(row)
    return rows


def run_bid_weakening_audit_from_snapshots(
    *,
    snapshots: Sequence[SnapshotRecord],
    transitions: Sequence[TransitionRecord],
    sequences: Sequence[SequenceRecord],
    price_path: Sequence[tuple[datetime, Decimal]],
    params: BidWeakeningParams,
    end: datetime,
    output_dir: Path,
) -> dict[str, Any]:
    del sequences  # available for future extensions; keep API stable
    output_dir.mkdir(parents=True, exist_ok=True)

    feature_rows: list[dict[str, Any]] = []
    timeline_rows: list[dict[str, Any]] = []
    warnings: list[WarningEvent] = []
    machine = MachineState()
    warning_seq = 0
    prior_swing_high: Decimal | None = None

    start_i = 1
    for i in range(start_i, len(snapshots)):
        # Prior swing high = previous local high memory (causal)
        feat = compute_features_at(
            index=i,
            snapshots=snapshots,
            transitions=transitions,
            params=params,
            prior_local_high=prior_swing_high,
        )
        # Update prior swing when a completed local high is left behind
        if feat.bars_since_local_high == 1 and feat.local_high is not None:
            # Just left a high — remember it for lower-high detection
            prior_swing_high = feat.local_high
        elif (
            feat.local_high is not None
            and (prior_swing_high is None or feat.local_high > prior_swing_high)
            and feat.bars_since_local_high == 0
        ):
            # Still at high — do not yet lock prior; session memory handles equality
            pass

        feature_rows.append(feat.to_row())
        machine, new_w, timeline = advance_state(
            machine=machine,
            feat=feat,
            params=params,
            warning_seq=warning_seq + 1,
        )
        timeline_rows.append(timeline)
        if new_w is not None:
            warning_seq += 1
            new_w.warning_id = f"W{warning_seq:04d}"
            warnings.append(new_w)

    # Close dangling warning as expired at session end
    if machine.active_warning is not None:
        w = machine.active_warning
        w.terminal_state = EXPIRED
        w.terminal_time = snapshots[-1].timestamp
        machine.active_warning = None

    forward_rows: list[dict[str, Any]] = []
    false_rows: list[dict[str, Any]] = []
    confirmed_rows: list[dict[str, Any]] = []
    for w in warnings:
        outs = simulate_warning_outcomes(
            warning=w, price_path=price_path, end=end, params=params
        )
        forward_rows.extend(outs)
        session = next(r for r in outs if r["horizon"] == "session_end")
        if w.terminal_state == REVERSAL_CONFIRMED:
            confirmed_rows.append({**w.to_row(), **{k: session[k] for k in session if k != "warning_id"}})
        down_ok = any(
            session.get(f"first_touch_down_{x:.2f}".replace('.', '_'))
            for x in DOWN_THRESHOLDS_PCT
            if x >= params.false_warning_min_down_pct
        )
        if w.terminal_state == WARNING_FAILED or (
            not down_ok and w.terminal_state != REVERSAL_CONFIRMED
        ):
            false_rows.append(
                {
                    **w.to_row(),
                    "reason": w.terminal_state or "NO_SUFFICIENT_DRAWDOWN",
                    "max_favourable_down_pct": session["max_favourable_down_pct"],
                    "max_adverse_up_pct": session["max_adverse_up_pct"],
                    "time_to_down_0_10": session.get("time_to_down_0_10"),
                    "time_to_down_0_25": session.get("time_to_down_0_25"),
                    "time_to_down_0_50": session.get("time_to_down_0_50"),
                    "time_to_new_high": session.get("time_to_new_high"),
                }
            )

    # Threshold / lead-time summary for long management
    threshold_rows: list[dict[str, Any]] = []
    for thr in DOWN_THRESHOLDS_PCT:
        key = f"{thr:.2f}".replace(".", "_")
        leads: list[int] = []
        hit = 0
        for w in warnings:
            session = next(
                r
                for r in forward_rows
                if r["warning_id"] == w.warning_id and r["horizon"] == "session_end"
            )
            if session.get(f"first_touch_down_{key}"):
                hit += 1
                t = session.get(f"time_to_down_{key}")
                if t is not None:
                    leads.append(int(t))
        continued_up = sum(
            1
            for w in warnings
            for r in forward_rows
            if r["warning_id"] == w.warning_id
            and r["horizon"] == "session_end"
            and r.get("time_to_new_high") is not None
            and not r.get(f"first_touch_down_{key}")
        )
        threshold_rows.append(
            {
                "down_threshold_pct": thr,
                "warnings_total": len(warnings),
                "warnings_hit_threshold": hit,
                "hit_rate": None if not warnings else round(hit / len(warnings), 6),
                "avg_lead_seconds": None
                if not leads
                else round(sum(leads) / len(leads), 3),
                "median_lead_seconds": None
                if not leads
                else sorted(leads)[len(leads) // 2],
                "false_or_no_drawdown_count": len(warnings) - hit,
                "continued_up_without_hit": continued_up,
            }
        )

    write_csv(output_dir / "bid_weakening_features.csv", feature_rows)
    write_csv(output_dir / "bid_weakening_state_timeline.csv", timeline_rows)
    write_csv(output_dir / "bid_weakening_warnings.csv", [w.to_row() for w in warnings])
    write_csv(output_dir / "bid_weakening_forward_outcomes.csv", forward_rows)
    write_csv(output_dir / "bid_weakening_false_warnings.csv", false_rows)
    write_csv(output_dir / "bid_weakening_confirmed_reversals.csv", confirmed_rows)
    write_csv(output_dir / "bid_weakening_threshold_summary.csv", threshold_rows)

    summary: dict[str, Any] = {
        "decision": (
            "BID_WEAKENING_SIGNAL_PROMISING"
            if any(w.terminal_state == REVERSAL_CONFIRMED for w in warnings)
            else "BID_WEAKENING_SIGNAL_INCONCLUSIVE"
            if warnings
            else "BID_WEAKENING_SIGNAL_NONE"
        ),
        "warning_count": len(warnings),
        "confirmed_reversal_count": sum(
            1 for w in warnings if w.terminal_state == REVERSAL_CONFIRMED
        ),
        "warning_failed_count": sum(
            1 for w in warnings if w.terminal_state == WARNING_FAILED
        ),
        "expired_count": sum(1 for w in warnings if w.terminal_state == EXPIRED),
        "false_warning_row_count": len(false_rows),
        "threshold_summary": threshold_rows,
        "params": asdict(params),
        "limitations": [
            "Research diagnostic only; bid thinning alone is not a confirmed reversal.",
            "No live orders; no manual timestamps in detection logic.",
            "Forward outcomes evaluated strictly after warning_time.",
            "Wall pull/shift counts use transition classifications between snapshots.",
        ],
    }
    (output_dir / "strategy_summary.json").write_bytes(
        orjson.dumps(summary, option=orjson.OPT_INDENT_2)
    )
    (output_dir / "REPORT.md").write_text(render_report(summary), encoding="utf-8")
    return summary


def render_report(summary: dict[str, Any]) -> str:
    lines = [
        "# Bid Weakening / Reversal Warning Audit",
        "",
        f"Decision: **{summary.get('decision')}**",
        f"Warnings: {summary.get('warning_count')}",
        f"Confirmed reversals: {summary.get('confirmed_reversal_count')}",
        f"Failed warnings: {summary.get('warning_failed_count')}",
        f"Expired: {summary.get('expired_count')}",
        "",
        "## Long-management lead times",
        "",
    ]
    for row in summary.get("threshold_summary") or []:
        lines.append(
            f"- Down {row['down_threshold_pct']}%: hit_rate={row['hit_rate']}, "
            f"avg_lead={row['avg_lead_seconds']}s, "
            f"false/no-drawdown={row['false_or_no_drawdown_count']}, "
            f"continued_up={row['continued_up_without_hit']}"
        )
    lines.extend(["", "## Limitations", ""])
    for lim in summary.get("limitations") or []:
        lines.append(f"- {lim}")
    lines.append("")
    return "\n".join(lines)


def to_audit_params(params: BidWeakeningParams) -> AuditParams:
    return AuditParams(
        sample_seconds=params.sample_seconds,
        target_bps=params.target_bps,
        near_min_distance_pct=params.near_min_distance_pct,
        near_max_distance_pct=params.near_max_distance_pct,
        near_top_n=params.near_top_n,
    )


def params_from_args(args: argparse.Namespace) -> BidWeakeningParams:
    return BidWeakeningParams(
        sample_seconds=int(args.sample_seconds),
        target_bps=float(args.target_bps),
        near_min_distance_pct=float(args.near_min_distance_pct),
        near_max_distance_pct=float(args.near_max_distance_pct),
        near_top_n=int(args.near_top_n),
        warning_min_feature_count=int(args.warning_min_feature_count),
        warning_confirm_snapshots=int(args.warning_confirm_snapshots),
        warning_max_age_seconds=int(args.warning_max_age_seconds),
        bid_notional_drop_pct=float(args.bid_notional_drop_pct),
        bid_wall_count_drop=int(args.bid_wall_count_drop),
        nearest_bid_retreat_bps=float(args.nearest_bid_retreat_bps),
        bid_ask_ratio_drop_pct=float(args.bid_ask_ratio_drop_pct),
        local_high_lookback_seconds=int(args.local_high_lookback_seconds),
        lower_high_tolerance_bps=float(args.lower_high_tolerance_bps),
    )


def run_audit(args: argparse.Namespace) -> dict[str, Any]:
    start = parse_utc(args.start)
    end = parse_utc(args.end)
    params = params_from_args(args)
    out_dir = Path(args.output_dir) if args.output_dir else (
        PROJECT_ROOT
        / "results"
        / f"orderbook_bid_weakening_reversal_{utc_now().strftime('%Y%m%dT%H%M%SZ')}"
    )
    db = connect_readonly()
    try:
        state = prepare_tracker_state(
            db=db,
            symbol=args.symbol,
            start=start,
            end=end,
            params=to_audit_params(params),
        )
        summary = run_bid_weakening_audit_from_snapshots(
            snapshots=state["snapshots"],
            transitions=state["transitions"],
            sequences=state["sequences"],
            price_path=state["price_path"],
            params=params,
            end=end,
            output_dir=out_dir,
        )
        summary["symbol"] = args.symbol
        summary["start"] = start.isoformat()
        summary["end"] = end.isoformat()
        summary["output_dir"] = str(out_dir)
        (out_dir / "strategy_summary.json").write_bytes(
            orjson.dumps(summary, option=orjson.OPT_INDENT_2)
        )
        return summary
    finally:
        db.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Causal bid-weakening / reversal warning audit"
    )
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--sample-seconds", type=int, default=30)
    p.add_argument("--target-bps", type=float, default=10.0)
    p.add_argument("--near-min-distance-pct", type=float, default=0.10)
    p.add_argument("--near-max-distance-pct", type=float, default=1.50)
    p.add_argument("--near-top-n", type=int, default=3)
    p.add_argument("--warning-min-feature-count", type=int, default=3)
    p.add_argument("--warning-confirm-snapshots", type=int, default=2)
    p.add_argument("--warning-max-age-seconds", type=int, default=300)
    p.add_argument("--bid-notional-drop-pct", type=float, default=25.0)
    p.add_argument("--bid-wall-count-drop", type=int, default=1)
    p.add_argument("--nearest-bid-retreat-bps", type=float, default=5.0)
    p.add_argument("--bid-ask-ratio-drop-pct", type=float, default=20.0)
    p.add_argument("--local-high-lookback-seconds", type=int, default=600)
    p.add_argument("--lower-high-tolerance-bps", type=float, default=8.0)
    p.add_argument("--output-dir", default=None)
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    summary = run_audit(args)
    sys.stdout.buffer.write(
        orjson.dumps(
            {
                "decision": summary.get("decision"),
                "warnings": summary.get("warning_count"),
                "confirmed": summary.get("confirmed_reversal_count"),
                "output_dir": summary.get("output_dir"),
            },
            option=orjson.OPT_INDENT_2,
        )
    )
    sys.stdout.buffer.write(b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
