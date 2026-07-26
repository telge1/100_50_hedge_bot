"""Integrated trend + bid-weakening audit (research only).

Combines autonomous bid-weakening warnings with a causal as-of regime join
and a newly built support-break confirmation. Diagnostic only — no live orders.

Strict causality:
- trend state = last regime row with decision_time <= warning_time
- support identified before break start
- outcomes use prices strictly after action_time
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Sequence

import orjson

from orderbook_analyse.dynamic_wall_detector import PROJECT_ROOT, parse_utc, utc_now, write_csv
from orderbook_analyse.orderbook_trade_candidate_audit import _ensure_utc, _fmt

logger = logging.getLogger(__name__)

# Actions
STOP_LONG_ADDS = "STOP_LONG_ADDS"
LONG_EXIT_WARNING = "LONG_EXIT_WARNING"
HEDGE_PREPARE = "HEDGE_PREPARE"
PARTIAL_LONG_EXIT_CANDIDATE = "PARTIAL_LONG_EXIT_CANDIDATE"
FULL_EXIT_OR_SHORT_CONFIRMATION = "FULL_EXIT_OR_SHORT_CONFIRMATION"
NO_ACTION = "NO_ACTION"
RANGE_CONTEXT_INSUFFICIENT = "RANGE_CONTEXT_INSUFFICIENT"
TREND_CONTEXT_UNAVAILABLE = "TREND_CONTEXT_UNAVAILABLE"

# Quality
WEAK_WARNING = "WEAK_WARNING"
STRONG_WARNING = "STRONG_WARNING"
VERY_STRONG_WARNING = "VERY_STRONG_WARNING"

# Regime sets (repo SimpleRegime labels, unchanged)
BULLISH_INTACT = frozenset({"strong_bullish_trend", "bullish_trend"})
BULLISH_WEAK = frozenset({"bullish_trend_with_trend_weakness"})
BEARISH = frozenset(
    {"strong_bearish_trend", "bearish_trend", "bearish_trend_with_trend_weakness"}
)
TRANSITION = frozenset({"transition"})
TREND_FILTER_REGIMES = frozenset(
    {
        "transition",
        "bullish_trend_with_trend_weakness",
        "bearish_trend",
        "bearish_trend_with_trend_weakness",
        "strong_bearish_trend",
    }
)

HORIZONS_SEC = (30, 60, 180, 300, 600, 900)
DOWN_PCT = (0.10, 0.25, 0.50)
UP_PCT = (0.10, 0.25, 0.50)
REFERENCE_TIMES = (
    "2026-07-26T10:00:00+00:00",
    "2026-07-26T10:55:00+00:00",
    "2026-07-26T11:25:00+00:00",
    "2026-07-26T12:55:00+00:00",
)

VARIANTS = ("G0", "G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8")


@dataclass
class IntegratedParams:
    strong_min_score: int = 8
    weak_max_score: int = 5
    support_break_confirm_snapshots: int = 2
    support_break_min_depth_bps: float = 5.0
    support_reclaim_tolerance_bps: float = 5.0
    support_break_max_confirm_seconds: int = 180
    episode_gap_seconds: int = 300
    one_action_per_episode: bool = True
    swing_lookback_seconds: int = 600
    session_end: str | None = None


@dataclass
class FeatureRow:
    timestamp: datetime
    index: int
    mid: Decimal
    nearest_bid: Decimal | None
    dominant_bid: Decimal | None
    local_high: Decimal | None
    local_low: Decimal | None
    local_support: Decimal | None
    lower_high_confirmed: bool
    active_bid_wall_notional_sum: Decimal | None = None
    active_bid_wall_notional_change_pct: float | None = None
    nearest_bid_change_bps: float | None = None
    bid_wall_shift_lower_count: int = 0
    bid_wall_shift_higher_count: int = 0
    trade_delta_60s: Decimal | None = None


@dataclass
class WarningRow:
    warning_id: str
    warning_time: datetime
    warning_index: int
    score: int
    feature_count: int
    features_true: list[str]
    mid: Decimal
    local_high: Decimal | None
    nearest_bid: Decimal | None
    dominant_bid: Decimal | None
    dominant_bid_notional: Decimal | None
    active_bid_wall_count: int
    active_bid_wall_notional_sum: Decimal | None
    nearest_ask: Decimal | None
    dominant_ask: Decimal | None
    active_ask_wall_notional_sum: Decimal | None
    bid_ask_notional_ratio: float | None
    trade_delta: Decimal | None
    oi_change: Decimal | None
    lower_high_confirmed: bool
    local_support: Decimal | None
    terminal_state: str | None
    terminal_time: datetime | None


@dataclass
class RegimeRow:
    decision_time: datetime
    candle_timestamp: datetime | None
    regime_5m: str
    regime_15m: str
    regime_30m: str
    combined_regime: str
    previous_combined_regime: str | None
    trend_direction: str
    trend_strength: str
    trend_weakness: bool
    transition_detected: bool


def _parse_ts(value: str | None) -> datetime | None:
    if value is None or value == "":
        return None
    return _ensure_utc(parse_utc(value) if "T" in value or " " in value else parse_utc(value))


def _dec(value: str | None) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _bool(value: str | None) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def _bps_below(level: Decimal, price: Decimal) -> float:
    if level == 0:
        return 0.0
    return float((level - price) / level * Decimal("10000"))


def load_warnings(path: Path) -> list[WarningRow]:
    rows: list[WarningRow] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            rows.append(
                WarningRow(
                    warning_id=r["warning_id"],
                    warning_time=_ensure_utc(parse_utc(r["warning_time"])),
                    warning_index=int(r["warning_index"]),
                    score=int(float(r["score"])),
                    feature_count=int(float(r["feature_count"])),
                    features_true=[x for x in r["features_true"].split(",") if x],
                    mid=Decimal(r["mid"]),
                    local_high=_dec(r.get("local_high")),
                    nearest_bid=_dec(r.get("nearest_bid")),
                    dominant_bid=_dec(r.get("dominant_bid")),
                    dominant_bid_notional=_dec(r.get("dominant_bid_notional")),
                    active_bid_wall_count=int(float(r.get("active_bid_wall_count") or 0)),
                    active_bid_wall_notional_sum=_dec(r.get("active_bid_wall_notional_sum")),
                    nearest_ask=_dec(r.get("nearest_ask")),
                    dominant_ask=_dec(r.get("dominant_ask")),
                    active_ask_wall_notional_sum=_dec(r.get("active_ask_wall_notional_sum")),
                    bid_ask_notional_ratio=_float(r.get("bid_ask_notional_ratio")),
                    trade_delta=_dec(r.get("trade_delta")),
                    oi_change=_dec(r.get("oi_change")),
                    lower_high_confirmed=_bool(r.get("lower_high_confirmed")),
                    local_support=_dec(r.get("local_support")),
                    terminal_state=r.get("terminal_state") or None,
                    terminal_time=_parse_ts(r.get("terminal_time")),
                )
            )
    return rows


def load_features(path: Path) -> list[FeatureRow]:
    rows: list[FeatureRow] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            rows.append(
                FeatureRow(
                    timestamp=_ensure_utc(parse_utc(r["timestamp"])),
                    index=int(float(r["index"])),
                    mid=Decimal(r["mid"]),
                    nearest_bid=_dec(r.get("nearest_bid")),
                    dominant_bid=_dec(r.get("dominant_bid")),
                    local_high=_dec(r.get("local_high")),
                    local_low=_dec(r.get("local_low")),
                    local_support=_dec(r.get("local_support")),
                    lower_high_confirmed=_bool(r.get("lower_high_confirmed")),
                    active_bid_wall_notional_sum=_dec(r.get("active_bid_wall_notional_sum")),
                    active_bid_wall_notional_change_pct=_float(
                        r.get("active_bid_wall_notional_change_pct")
                    ),
                    nearest_bid_change_bps=_float(r.get("nearest_bid_change_bps")),
                    bid_wall_shift_lower_count=int(
                        float(r.get("bid_wall_shift_lower_count") or 0)
                    ),
                    bid_wall_shift_higher_count=int(
                        float(r.get("bid_wall_shift_higher_count") or 0)
                    ),
                    trade_delta_60s=_dec(r.get("trade_delta_60s")),
                )
            )
    return rows


def load_regimes(path: Path) -> list[RegimeRow]:
    rows: list[RegimeRow] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            rows.append(
                RegimeRow(
                    decision_time=_ensure_utc(parse_utc(r["decision_time"])),
                    candle_timestamp=_parse_ts(r.get("candle_timestamp")),
                    regime_5m=str(r.get("regime_5m") or "unavailable"),
                    regime_15m=str(r.get("regime_15m") or "unavailable"),
                    regime_30m=str(r.get("regime_30m") or "unavailable"),
                    combined_regime=str(r.get("combined_regime") or "unavailable"),
                    previous_combined_regime=r.get("previous_combined_regime") or None,
                    trend_direction=str(r.get("trend_direction") or "unavailable"),
                    trend_strength=str(r.get("trend_strength") or "unavailable"),
                    trend_weakness=_bool(r.get("trend_weakness")),
                    transition_detected=_bool(r.get("transition_detected")),
                )
            )
    rows.sort(key=lambda x: x.decision_time)
    return rows


def join_regime_as_of(
    regimes: Sequence[RegimeRow], *, as_of: datetime
) -> dict[str, Any]:
    """Last fully known regime with decision_time <= as_of."""
    t = _ensure_utc(as_of)
    chosen: RegimeRow | None = None
    for row in regimes:
        if row.decision_time <= t:
            chosen = row
        else:
            break
    if chosen is None:
        return {
            "trend_state_time": None,
            "trend_state_age_seconds": None,
            "combined_regime": TREND_CONTEXT_UNAVAILABLE,
            "trend_direction": TREND_CONTEXT_UNAVAILABLE,
            "trend_strength": TREND_CONTEXT_UNAVAILABLE,
            "trend_weakness": None,
            "transition_detected": None,
            "regime_5m": TREND_CONTEXT_UNAVAILABLE,
            "regime_15m": TREND_CONTEXT_UNAVAILABLE,
            "regime_30m": TREND_CONTEXT_UNAVAILABLE,
            "trend_data_available": False,
            "trend_join_reason": "NO_REGIME_AT_OR_BEFORE_WARNING",
            "disagreement_5m_15m": None,
            "disagreement_15m_30m": None,
            "all_timeframes_transition": None,
            "short_term_transition_only": None,
            "higher_timeframe_bullish_weakness": None,
        }
    age = (t - chosen.decision_time).total_seconds()
    d_5_15 = chosen.regime_5m != chosen.regime_15m
    d_15_30 = chosen.regime_15m != chosen.regime_30m
    all_tr = (
        chosen.regime_5m == "transition"
        and chosen.regime_15m == "transition"
        and chosen.regime_30m == "transition"
    )
    short_only = chosen.regime_5m == "transition" and chosen.regime_15m != "transition"
    htf_weak = chosen.regime_15m in BULLISH_WEAK or chosen.regime_30m in BULLISH_WEAK
    return {
        "trend_state_time": chosen.decision_time,
        "trend_state_age_seconds": age,
        "combined_regime": chosen.combined_regime,
        "trend_direction": chosen.trend_direction,
        "trend_strength": chosen.trend_strength,
        "trend_weakness": chosen.trend_weakness,
        "transition_detected": chosen.transition_detected,
        "regime_5m": chosen.regime_5m,
        "regime_15m": chosen.regime_15m,
        "regime_30m": chosen.regime_30m,
        "trend_data_available": True,
        "trend_join_reason": "LAST_DECISION_TIME_LE_WARNING",
        "disagreement_5m_15m": d_5_15,
        "disagreement_15m_30m": d_15_30,
        "all_timeframes_transition": all_tr,
        "short_term_transition_only": short_only,
        "higher_timeframe_bullish_weakness": htf_weak,
    }


def classify_warning_quality(
    warning: WarningRow,
    *,
    params: IntegratedParams,
    support_break_valid: bool,
    trend: MappingLike,
) -> str:
    feats = set(warning.features_true)
    structural = bool(
        feats
        & {
            "bid_notional_drop",
            "dominant_bid_notional_drop",
            "nearest_bid_retreat",
            "bid_wall_shift_lower",
            "bid_wall_count_drop",
        }
    )
    delta_neg = bool(
        feats & {"trade_delta_negative", "trade_delta_soft_negative"}
        or (warning.trade_delta is not None and warning.trade_delta < 0)
    )
    strong = (
        warning.score >= params.strong_min_score
        and warning.lower_high_confirmed
        and delta_neg
        and structural
    )
    if not strong:
        if (
            warning.score <= params.weak_max_score
            and not warning.lower_high_confirmed
            and not structural
        ):
            return WEAK_WARNING
        if warning.score <= params.weak_max_score:
            return WEAK_WARNING
        # medium / incomplete strong → treat as weak for matrix conservatism
        return WEAK_WARNING

    trend_ok = False
    if trend.get("trend_data_available"):
        regime = str(trend.get("combined_regime"))
        trend_ok = (
            regime in {"transition", "bullish_trend_with_trend_weakness"}
            or bool(trend.get("all_timeframes_transition"))
            or (
                bool(trend.get("short_term_transition_only"))
                and bool(trend.get("higher_timeframe_bullish_weakness"))
            )
            or regime in BEARISH
        )
    if strong and support_break_valid and trend_ok:
        return VERY_STRONG_WARNING
    return STRONG_WARNING


MappingLike = dict[str, Any]


def find_confirmed_swing_low(
    features: Sequence[FeatureRow],
    *,
    as_of: datetime,
    lookback_seconds: int,
) -> tuple[Decimal, datetime] | None:
    """Causal swing low: pivot confirmed by a later bar with ts <= as_of."""
    t = _ensure_utc(as_of)
    start = t - timedelta(seconds=lookback_seconds)
    window = [f for f in features if start <= f.timestamp <= t]
    if len(window) < 3:
        return None
    best: tuple[Decimal, datetime] | None = None
    for i in range(1, len(window) - 1):
        # pivot at i confirmed because window[i+1] exists and <= as_of
        a, b, c = window[i - 1].mid, window[i].mid, window[i + 1].mid
        if b < a and b < c:
            cand = (b, window[i].timestamp)
            if best is None or cand[0] < best[0] or (
                cand[0] == best[0] and cand[1] > best[1]
            ):
                best = cand
    return best


def select_support_level(
    warning: WarningRow,
    features: Sequence[FeatureRow],
    *,
    params: IntegratedParams,
) -> dict[str, Any]:
    """Pick causal support known before any break after the warning."""
    as_of = warning.warning_time
    prior = [f for f in features if f.timestamp < as_of]
    last_prior = prior[-1] if prior else None

    swing = find_confirmed_swing_low(
        features, as_of=as_of, lookback_seconds=params.swing_lookback_seconds
    )
    if swing is not None:
        level, ident = swing
        return {
            "support_level": level,
            "support_source": "CONFIRMED_SWING_LOW",
            "support_identified_time": ident,
        }
    if last_prior is not None and last_prior.nearest_bid is not None:
        return {
            "support_level": last_prior.nearest_bid,
            "support_source": "PRIOR_NEAREST_BID_WALL",
            "support_identified_time": last_prior.timestamp,
        }
    if last_prior is not None and last_prior.local_support is not None:
        return {
            "support_level": last_prior.local_support,
            "support_source": "PRIOR_LOCAL_SUPPORT",
            "support_identified_time": last_prior.timestamp,
        }
    if warning.local_support is not None:
        # identified at warning time; break can only start on later snapshots
        return {
            "support_level": warning.local_support,
            "support_source": "WARNING_LOCAL_SUPPORT",
            "support_identified_time": warning.warning_time,
        }
    return {
        "support_level": None,
        "support_source": None,
        "support_identified_time": None,
    }


def evaluate_support_break(
    *,
    support_level: Decimal | None,
    support_source: str | None,
    support_identified_time: datetime | None,
    warning_time: datetime,
    features: Sequence[FeatureRow],
    params: IntegratedParams,
) -> dict[str, Any]:
    base = {
        "support_level": support_level,
        "support_source": support_source,
        "support_identified_time": support_identified_time,
        "support_break_start_time": None,
        "support_break_confirm_time": None,
        "support_break_depth_bps": None,
        "support_break_confirm_snapshots": 0,
        "support_reclaimed": False,
        "support_reclaim_time": None,
        "support_break_valid": False,
        "support_break_reject_reason": None,
    }
    if support_level is None or support_identified_time is None:
        base["support_break_reject_reason"] = "NO_SUPPORT_LEVEL"
        return base

    # Snapshots strictly after warning and after identification (no same-snapshot break)
    after = [
        f
        for f in features
        if f.timestamp > max(_ensure_utc(warning_time), _ensure_utc(support_identified_time))
    ]
    if not after:
        base["support_break_reject_reason"] = "NO_POST_WARNING_SNAPSHOTS"
        return base

    min_depth = Decimal(str(params.support_break_min_depth_bps)) / Decimal("10000")
    reclaim_tol = Decimal(str(params.support_reclaim_tolerance_bps)) / Decimal("10000")
    break_floor = support_level * (Decimal("1") - min_depth)
    reclaim_level = support_level * (Decimal("1") - reclaim_tol)

    start_time: datetime | None = None
    under_count = 0
    max_depth = 0.0
    deadline: datetime | None = None

    for feat in after:
        under = feat.mid < break_floor
        reclaimed = feat.mid >= reclaim_level
        if start_time is None:
            if under:
                # first breach — start, but this snapshot does not finish confirm alone
                start_time = feat.timestamp
                under_count = 1
                max_depth = max(max_depth, _bps_below(support_level, feat.mid))
                deadline = start_time + timedelta(
                    seconds=params.support_break_max_confirm_seconds
                )
                if (
                    _ensure_utc(support_identified_time) >= _ensure_utc(start_time)
                ):
                    base["support_break_reject_reason"] = "SUPPORT_NOT_BEFORE_BREAK"
                    return base
            continue

        if deadline is not None and feat.timestamp > deadline:
            base.update(
                {
                    "support_break_start_time": start_time,
                    "support_break_confirm_snapshots": under_count,
                    "support_break_depth_bps": max_depth,
                    "support_break_reject_reason": "CONFIRM_TIMEOUT",
                }
            )
            return base

        if reclaimed and not under:
            base.update(
                {
                    "support_break_start_time": start_time,
                    "support_reclaimed": True,
                    "support_reclaim_time": feat.timestamp,
                    "support_break_confirm_snapshots": under_count,
                    "support_break_depth_bps": max_depth,
                    "support_break_reject_reason": "RECLAIMED_BEFORE_CONFIRM",
                }
            )
            return base

        if under:
            under_count += 1
            max_depth = max(max_depth, _bps_below(support_level, feat.mid))
            if under_count >= params.support_break_confirm_snapshots:
                # confirm on this later snapshot (count includes start)
                if under_count < 2:
                    continue
                base.update(
                    {
                        "support_break_start_time": start_time,
                        "support_break_confirm_time": feat.timestamp,
                        "support_break_depth_bps": max_depth,
                        "support_break_confirm_snapshots": under_count,
                        "support_break_valid": True,
                        "support_break_reject_reason": None,
                    }
                )
                return base
        else:
            # not under and not reclaimed (between break_floor and reclaim) — soft; reset count but keep start
            under_count = 0

    if start_time is None:
        base["support_break_reject_reason"] = "NO_BREAK"
    else:
        base.update(
            {
                "support_break_start_time": start_time,
                "support_break_confirm_snapshots": under_count,
                "support_break_depth_bps": max_depth,
                "support_break_reject_reason": "UNCONFIRMED_AT_SESSION_END",
            }
        )
    return base


def build_episodes(
    warnings: Sequence[WarningRow],
    features: Sequence[FeatureRow],
    *,
    params: IntegratedParams,
) -> list[dict[str, Any]]:
    episodes: list[dict[str, Any]] = []
    current: list[WarningRow] = []
    feat_by_ts = {f.timestamp: f for f in features}

    def flush(group: list[WarningRow]) -> None:
        if not group:
            return
        eid = f"E{len(episodes) + 1:04d}"
        strongest = max(group, key=lambda w: (w.score, w.warning_time))
        episodes.append(
            {
                "episode_id": eid,
                "warnings": list(group),
                "first_warning": group[0],
                "strongest_warning": strongest,
                "peak_score": max(w.score for w in group),
                "start": group[0].warning_time,
                "end": group[-1].warning_time,
            }
        )

    for w in warnings:
        if not current:
            current = [w]
            continue
        prev = current[-1]
        gap = (w.warning_time - prev.warning_time).total_seconds()
        new_high = False
        if prev.local_high is not None and w.mid > prev.local_high:
            new_high = True
        # bid rebuild between warnings
        rebuild = False
        between = [
            f
            for f in features
            if prev.warning_time < f.timestamp <= w.warning_time
        ]
        if between:
            rebuild = any(
                (f.active_bid_wall_notional_change_pct or 0) > 0
                and (f.nearest_bid_change_bps or 0) > 0
                for f in between
            )
        invalidated = prev.terminal_state == "WARNING_FAILED"
        if (
            gap > params.episode_gap_seconds
            or new_high
            or rebuild
            or invalidated
        ):
            flush(current)
            current = [w]
        else:
            current.append(w)
    flush(current)
    return episodes


def map_action(
    *,
    quality: str,
    combined_regime: str,
    support_break_valid: bool,
    trend_available: bool,
) -> str:
    if not trend_available:
        # Without trend, only stop adds / warn on strong structure; never full exit
        if quality == WEAK_WARNING:
            return STOP_LONG_ADDS
        if support_break_valid and quality in {STRONG_WARNING, VERY_STRONG_WARNING}:
            return HEDGE_PREPARE
        if quality in {STRONG_WARNING, VERY_STRONG_WARNING}:
            return LONG_EXIT_WARNING
        return NO_ACTION

    regime = combined_regime
    if regime in BULLISH_INTACT:
        if quality == WEAK_WARNING and not support_break_valid:
            return STOP_LONG_ADDS
        if quality in {STRONG_WARNING, VERY_STRONG_WARNING} and not support_break_valid:
            return LONG_EXIT_WARNING
        if quality in {STRONG_WARNING, VERY_STRONG_WARNING} and support_break_valid:
            return HEDGE_PREPARE
        return STOP_LONG_ADDS

    if regime in BULLISH_WEAK:
        if quality == WEAK_WARNING and not support_break_valid:
            return STOP_LONG_ADDS
        if quality in {STRONG_WARNING, VERY_STRONG_WARNING} and not support_break_valid:
            return LONG_EXIT_WARNING
        if quality in {STRONG_WARNING, VERY_STRONG_WARNING} and support_break_valid:
            return PARTIAL_LONG_EXIT_CANDIDATE
        return STOP_LONG_ADDS

    if regime in TRANSITION or regime == "neutral":
        if regime == "neutral":
            return RANGE_CONTEXT_INSUFFICIENT
        if quality == WEAK_WARNING and not support_break_valid:
            return STOP_LONG_ADDS
        if quality in {STRONG_WARNING, VERY_STRONG_WARNING} and not support_break_valid:
            return LONG_EXIT_WARNING
        if quality in {STRONG_WARNING, VERY_STRONG_WARNING} and support_break_valid:
            return HEDGE_PREPARE
        return STOP_LONG_ADDS

    if regime in BEARISH:
        if quality in {STRONG_WARNING, VERY_STRONG_WARNING} and support_break_valid:
            return FULL_EXIT_OR_SHORT_CONFIRMATION
        if quality in {STRONG_WARNING, VERY_STRONG_WARNING}:
            return HEDGE_PREPARE
        return STOP_LONG_ADDS

    return NO_ACTION


def variant_passes(
    variant: str,
    *,
    warning: WarningRow,
    quality: str,
    trend: MappingLike,
    support_break_valid: bool,
    params: IntegratedParams,
) -> bool:
    score_ok = warning.score >= params.strong_min_score
    lh = warning.lower_high_confirmed
    feats = set(warning.features_true)
    delta_ok = bool(
        feats & {"trade_delta_negative", "trade_delta_soft_negative"}
        or (warning.trade_delta is not None and warning.trade_delta < 0)
    )
    trend_ok = bool(
        trend.get("trend_data_available")
        and str(trend.get("combined_regime")) in TREND_FILTER_REGIMES
    )
    g1 = score_ok and lh

    if variant == "G0":
        return True
    if variant == "G1":
        return g1
    if variant == "G2":
        return g1 and delta_ok
    if variant == "G3":
        return g1 and trend_ok
    if variant == "G4":
        return g1 and support_break_valid
    if variant == "G5":
        return g1 and trend_ok and support_break_valid
    if variant == "G6":
        return trend_ok and support_break_valid
    if variant == "G7":
        return (
            g1
            and trend.get("trend_data_available")
            and trend.get("regime_5m") == "transition"
            and trend.get("regime_15m")
            in {"transition", "bullish_trend_with_trend_weakness"}
        )
    if variant == "G8":
        tf_transition = sum(
            1
            for k in ("regime_5m", "regime_15m", "regime_30m")
            if trend.get(k) == "transition"
        )
        return g1 and tf_transition >= 2 and support_break_valid
    return False


def simulate_outcomes(
    *,
    action_time: datetime,
    entry_mid: Decimal,
    price_path: Sequence[tuple[datetime, Decimal]],
    end: datetime,
    local_high: Decimal | None,
) -> list[dict[str, Any]]:
    t0 = _ensure_utc(action_time)
    rows: list[dict[str, Any]] = []
    high_ref = local_high or entry_mid

    def path_until(te: datetime) -> list[tuple[datetime, Decimal]]:
        end_t = _ensure_utc(te)
        return [( _ensure_utc(ts), px) for ts, px in price_path if t0 < _ensure_utc(ts) <= end_t]

    horizons: list[tuple[str, datetime | None]] = [
        (str(h), t0 + timedelta(seconds=h)) for h in HORIZONS_SEC
    ]
    horizons.append(("session_end", None))

    for label, t_end in horizons:
        te = _ensure_utc(end) if t_end is None else min(_ensure_utc(end), _ensure_utc(t_end))
        path = path_until(te)
        row: dict[str, Any] = {
            "horizon": label,
            "forward_return_pct": 0.0,
            "max_favourable_down_pct": 0.0,
            "max_adverse_up_pct": 0.0,
            "first_touch_direction": None,
            "first_touch_time": None,
            "time_to_new_high": None,
            "new_high_before_down": False,
        }
        for thr in DOWN_PCT:
            key = f"{thr:.2f}".replace(".", "_")
            row[f"first_touch_down_{key}"] = False
            row[f"time_to_down_{key}"] = None
        for thr in UP_PCT:
            key = f"{thr:.2f}".replace(".", "_")
            row[f"first_touch_up_{key}"] = False
            row[f"time_to_up_{key}"] = None
        if not path:
            rows.append(row)
            continue
        last = path[-1][1]
        row["forward_return_pct"] = float((last - entry_mid) / entry_mid * Decimal("100"))
        mfe = mae = 0.0
        first_dir = None
        first_time = None
        t_new_high = None
        down_hit_any = False
        for ts, px in path:
            up = float((px - entry_mid) / entry_mid * Decimal("100"))
            dn = float((entry_mid - px) / entry_mid * Decimal("100"))
            elapsed = int((_ensure_utc(ts) - t0).total_seconds())
            if up > mae:
                mae = up
            if dn > mfe:
                mfe = dn
            if t_new_high is None and px > high_ref:
                t_new_high = elapsed
            for thr in DOWN_PCT:
                key = f"{thr:.2f}".replace(".", "_")
                if not row[f"first_touch_down_{key}"] and dn >= thr:
                    row[f"first_touch_down_{key}"] = True
                    row[f"time_to_down_{key}"] = elapsed
                    down_hit_any = True
                    if first_dir is None:
                        first_dir, first_time = "DOWN", ts
            for thr in UP_PCT:
                key = f"{thr:.2f}".replace(".", "_")
                if not row[f"first_touch_up_{key}"] and up >= thr:
                    row[f"first_touch_up_{key}"] = True
                    row[f"time_to_up_{key}"] = elapsed
                    if first_dir is None:
                        first_dir, first_time = "UP", ts
        row["max_favourable_down_pct"] = round(mfe, 6)
        row["max_adverse_up_pct"] = round(mae, 6)
        row["first_touch_direction"] = first_dir
        row["first_touch_time"] = None if first_time is None else first_time.isoformat()
        row["time_to_new_high"] = t_new_high
        row["new_high_before_down"] = bool(
            t_new_high is not None and not down_hit_any
        ) or bool(
            t_new_high is not None
            and row.get("time_to_down_0_10") is not None
            and t_new_high < row["time_to_down_0_10"]
        )
        rows.append(row)
    return rows


def run_integrated_audit(
    *,
    warnings_path: Path,
    features_path: Path,
    regimes_path: Path,
    output_dir: Path,
    params: IntegratedParams,
    timeline_path: Path | None = None,
) -> dict[str, Any]:
    del timeline_path  # reserved for future diagnostics
    output_dir.mkdir(parents=True, exist_ok=True)
    warnings = load_warnings(warnings_path)
    features = load_features(features_path)
    regimes = load_regimes(regimes_path)
    if not warnings:
        summary = {"decision": "INTEGRATED_WARNING_SIGNAL_FAILED", "reason": "NO_WARNINGS"}
        (output_dir / "strategy_summary.json").write_bytes(
            orjson.dumps(summary, option=orjson.OPT_INDENT_2)
        )
        return summary

    end = (
        _ensure_utc(parse_utc(params.session_end))
        if params.session_end
        else max(f.timestamp for f in features)
    )
    price_path = [(f.timestamp, f.mid) for f in features]

    context_rows: list[dict[str, Any]] = []
    support_rows: list[dict[str, Any]] = []
    warning_enriched: list[dict[str, Any]] = []

    for w in warnings:
        trend = join_regime_as_of(regimes, as_of=w.warning_time)
        support_sel = select_support_level(w, features, params=params)
        brk = evaluate_support_break(
            support_level=support_sel["support_level"],
            support_source=support_sel["support_source"],
            support_identified_time=support_sel["support_identified_time"],
            warning_time=w.warning_time,
            features=features,
            params=params,
        )
        quality = classify_warning_quality(
            w,
            params=params,
            support_break_valid=bool(brk["support_break_valid"]),
            trend=trend,
        )
        enriched = {
            "warning": w,
            "trend": trend,
            "break": brk,
            "quality": quality,
        }
        warning_enriched.append(enriched)
        context_rows.append(
            {
                "warning_id": w.warning_id,
                "warning_time": w.warning_time.isoformat(),
                "score": w.score,
                "features_true": ",".join(w.features_true),
                "warning_quality": quality,
                "mid": _fmt(w.mid),
                "lower_high_confirmed": w.lower_high_confirmed,
                "trade_delta": _fmt(w.trade_delta),
                "bid_ask_notional_ratio": w.bid_ask_notional_ratio,
                **{
                    k: (
                        v.isoformat()
                        if isinstance(v, datetime)
                        else _fmt(v)
                        if isinstance(v, Decimal)
                        else v
                    )
                    for k, v in trend.items()
                },
                **{
                    k: (
                        v.isoformat()
                        if isinstance(v, datetime)
                        else _fmt(v)
                        if isinstance(v, Decimal)
                        else v
                    )
                    for k, v in brk.items()
                },
            }
        )
        support_rows.append(
            {
                "warning_id": w.warning_id,
                "warning_time": w.warning_time.isoformat(),
                **{
                    k: (
                        v.isoformat()
                        if isinstance(v, datetime)
                        else _fmt(v)
                        if isinstance(v, Decimal)
                        else v
                    )
                    for k, v in brk.items()
                },
            }
        )

    episodes = build_episodes(warnings, features, params=params)
    # map warning_id -> enriched
    by_id = {e["warning"].warning_id: e for e in warning_enriched}

    episode_rows: list[dict[str, Any]] = []
    action_rows: list[dict[str, Any]] = []
    forward_rows: list[dict[str, Any]] = []

    for ep in episodes:
        first: WarningRow = ep["first_warning"]
        strongest: WarningRow = ep["strongest_warning"]
        first_en = by_id[first.warning_id]
        strong_en = by_id[strongest.warning_id]
        # first lower high in episode
        first_lh = next(
            (w.warning_time for w in ep["warnings"] if w.lower_high_confirmed), None
        )
        # first support break among episode warnings
        first_break = None
        for w in ep["warnings"]:
            br = by_id[w.warning_id]["break"]
            if br.get("support_break_valid") and br.get("support_break_confirm_time"):
                first_break = br
                break
        # first trend change vs previous regime at first warning
        trend_change = None
        if first_en["trend"].get("trend_data_available"):
            # look for regime_change around episode using previous_combined if available
            trend_change = first_en["trend"].get("combined_regime")

        episode_rows.append(
            {
                "episode_id": ep["episode_id"],
                "warning_count": len(ep["warnings"]),
                "first_warning_id": first.warning_id,
                "first_warning_time": first.warning_time.isoformat(),
                "strongest_warning_id": strongest.warning_id,
                "strongest_warning_time": strongest.warning_time.isoformat(),
                "peak_score": ep["peak_score"],
                "first_lower_high_time": None
                if first_lh is None
                else first_lh.isoformat(),
                "first_trend_state": trend_change,
                "first_support_break_confirm_time": None
                if first_break is None
                else first_break["support_break_confirm_time"].isoformat(),
                "support_break_valid_any": first_break is not None,
            }
        )

        for variant in VARIANTS:
            # Choose representative warning for variant gate
            candidates = ep["warnings"]
            chosen = None
            for w in candidates:
                en = by_id[w.warning_id]
                if variant_passes(
                    variant,
                    warning=w,
                    quality=en["quality"],
                    trend=en["trend"],
                    support_break_valid=bool(en["break"]["support_break_valid"]),
                    params=params,
                ):
                    chosen = w
                    break
            if chosen is None:
                continue
            # Prefer strongest passing warning if one-action-per-episode
            if params.one_action_per_episode:
                passing = [
                    w
                    for w in candidates
                    if variant_passes(
                        variant,
                        warning=w,
                        quality=by_id[w.warning_id]["quality"],
                        trend=by_id[w.warning_id]["trend"],
                        support_break_valid=bool(
                            by_id[w.warning_id]["break"]["support_break_valid"]
                        ),
                        params=params,
                    )
                ]
                if not passing:
                    continue
                # first executable action time among passing (earliest confirm/action)
                def action_time_for(w: WarningRow) -> datetime:
                    en = by_id[w.warning_id]
                    br = en["break"]
                    if variant in {"G4", "G5", "G6", "G8"} and br.get(
                        "support_break_confirm_time"
                    ):
                        return br["support_break_confirm_time"]
                    if (
                        variant in {"G4", "G5", "G6", "G8"}
                        and not br.get("support_break_valid")
                    ):
                        # should not happen due to gate
                        return w.warning_time
                    return w.warning_time

                chosen = min(passing, key=lambda w: (action_time_for(w), -w.score))

            en = by_id[chosen.warning_id]
            br = en["break"]
            trend = en["trend"]
            quality = en["quality"]
            if variant in {"G4", "G5", "G6", "G8"}:
                action_time = br.get("support_break_confirm_time")
                if action_time is None:
                    continue
            else:
                action_time = chosen.warning_time

            action = map_action(
                quality=quality,
                combined_regime=str(trend.get("combined_regime")),
                support_break_valid=bool(br.get("support_break_valid")),
                trend_available=bool(trend.get("trend_data_available")),
            )
            reason_parts = [
                f"variant={variant}",
                f"quality={quality}",
                f"regime={trend.get('combined_regime')}",
                f"break={br.get('support_break_valid')}",
            ]
            mid_at_action = next(
                (f.mid for f in features if f.timestamp == action_time), chosen.mid
            )
            action_row = {
                "warning_id": chosen.warning_id,
                "episode_id": ep["episode_id"],
                "warning_time": chosen.warning_time.isoformat(),
                "action_time": action_time.isoformat(),
                "variant": variant,
                "action": action,
                "warning_score": chosen.score,
                "warning_quality": quality,
                "features_true": ",".join(chosen.features_true),
                "trend_state": trend.get("combined_regime"),
                "trend_state_time": None
                if trend.get("trend_state_time") is None
                else trend["trend_state_time"].isoformat(),
                "trend_state_age_seconds": trend.get("trend_state_age_seconds"),
                "regime_5m": trend.get("regime_5m"),
                "regime_15m": trend.get("regime_15m"),
                "regime_30m": trend.get("regime_30m"),
                "support_level": _fmt(br.get("support_level")),
                "support_break_confirm_time": None
                if br.get("support_break_confirm_time") is None
                else br["support_break_confirm_time"].isoformat(),
                "support_break_depth_bps": br.get("support_break_depth_bps"),
                "lower_high_confirmed": chosen.lower_high_confirmed,
                "trade_delta": _fmt(chosen.trade_delta),
                "bid_ask_notional_ratio": chosen.bid_ask_notional_ratio,
                "mid": _fmt(mid_at_action),
                "warning_to_action_delay_seconds": (
                    _ensure_utc(action_time) - _ensure_utc(chosen.warning_time)
                ).total_seconds(),
                "support_break_delay_seconds": None
                if br.get("support_break_confirm_time") is None
                else (
                    _ensure_utc(br["support_break_confirm_time"])
                    - _ensure_utc(chosen.warning_time)
                ).total_seconds(),
                "reason": ";".join(reason_parts),
            }
            action_rows.append(action_row)
            outs = simulate_outcomes(
                action_time=action_time,
                entry_mid=mid_at_action,
                price_path=price_path,
                end=end,
                local_high=chosen.local_high,
            )
            for out in outs:
                forward_rows.append(
                    {
                        "warning_id": chosen.warning_id,
                        "episode_id": ep["episode_id"],
                        "variant": variant,
                        "action": action,
                        "action_time": action_time.isoformat(),
                        **out,
                    }
                )

    # Variant summaries (episode-level, session_end)
    variant_summary: list[dict[str, Any]] = []
    for variant in VARIANTS:
        acts = [a for a in action_rows if a["variant"] == variant]
        sess = [
            f
            for f in forward_rows
            if f["variant"] == variant and f["horizon"] == "session_end"
        ]
        n = len(acts)
        def hit_rate(key: str) -> float | None:
            if not sess:
                return None
            return round(sum(1 for r in sess if r.get(key)) / len(sess), 6)

        leads = [
            r["time_to_down_0_25"]
            for r in sess
            if r.get("first_touch_down_0_25") and r.get("time_to_down_0_25") is not None
        ]
        false_n = sum(
            1
            for r in sess
            if not r.get("first_touch_down_0_10")
            and (r.get("new_high_before_down") or (r.get("max_adverse_up_pct") or 0) > 0.1)
        )
        variant_summary.append(
            {
                "variant": variant,
                "actions_total": n,
                "episodes_covered": len({a["episode_id"] for a in acts}),
                "hit_rate_down_0_10": hit_rate("first_touch_down_0_10"),
                "hit_rate_down_0_25": hit_rate("first_touch_down_0_25"),
                "hit_rate_down_0_50": hit_rate("first_touch_down_0_50"),
                "continued_up_without_hit": sum(
                    1
                    for r in sess
                    if r.get("time_to_new_high") is not None
                    and not r.get("first_touch_down_0_25")
                ),
                "new_high_before_down": sum(
                    1 for r in sess if r.get("new_high_before_down")
                ),
                "median_lead_seconds_down_0_25": None
                if not leads
                else sorted(leads)[len(leads) // 2],
                "false_warning_count": false_n,
                "median_max_adverse_up_pct": None
                if not sess
                else sorted(r["max_adverse_up_pct"] for r in sess)[len(sess) // 2],
                "median_max_favourable_down_pct": None
                if not sess
                else sorted(r["max_favourable_down_pct"] for r in sess)[len(sess) // 2],
                "median_warning_to_action_delay": None
                if not acts
                else sorted(a["warning_to_action_delay_seconds"] for a in acts)[
                    len(acts) // 2
                ],
            }
        )

    # Ablation answers (diagnostic)
    def vs(name: str) -> dict[str, Any]:
        return next(r for r in variant_summary if r["variant"] == name)

    g0, g1, g3, g4, g5, g6, g7 = vs("G0"), vs("G1"), vs("G3"), vs("G4"), vs("G5"), vs("G6"), vs("G7")
    ablation = {
        "q1_g3_vs_g1_false_reduction": None
        if g1["actions_total"] == 0
        else {
            "g1_false": g1["false_warning_count"],
            "g3_false": g3["false_warning_count"],
            "g1_hit_0_25": g1["hit_rate_down_0_25"],
            "g3_hit_0_25": g3["hit_rate_down_0_25"],
            "actions_g1": g1["actions_total"],
            "actions_g3": g3["actions_total"],
        },
        "q2_combined_regime_value_under_ubiquitous_weakness": {
            "note": "trend_weakness often True; compare G1 vs G3 action counts and precision",
            "g1_actions": g1["actions_total"],
            "g3_actions": g3["actions_total"],
            "g1_hit_0_25": g1["hit_rate_down_0_25"],
            "g3_hit_0_25": g3["hit_rate_down_0_25"],
        },
        "q3_g7_vs_g3": {
            "g3_actions": g3["actions_total"],
            "g7_actions": g7["actions_total"],
            "g3_hit_0_25": g3["hit_rate_down_0_25"],
            "g7_hit_0_25": g7["hit_rate_down_0_25"],
        },
        "q4_g4_precision_vs_g1": {
            "g1_hit_0_25": g1["hit_rate_down_0_25"],
            "g4_hit_0_25": g4["hit_rate_down_0_25"],
            "g1_false": g1["false_warning_count"],
            "g4_false": g4["false_warning_count"],
        },
        "q5_lead_lost_g4_g5": {
            "g1_median_delay": g1["median_warning_to_action_delay"],
            "g4_median_delay": g4["median_warning_to_action_delay"],
            "g5_median_delay": g5["median_warning_to_action_delay"],
            "g1_median_lead_0_25": g1["median_lead_seconds_down_0_25"],
            "g4_median_lead_0_25": g4["median_lead_seconds_down_0_25"],
            "g5_median_lead_0_25": g5["median_lead_seconds_down_0_25"],
        },
        "q6_g5_vs_g6": {
            "g5_actions": g5["actions_total"],
            "g6_actions": g6["actions_total"],
            "g5_hit_0_25": g5["hit_rate_down_0_25"],
            "g6_hit_0_25": g6["hit_rate_down_0_25"],
            "g5_false": g5["false_warning_count"],
            "g6_false": g6["false_warning_count"],
        },
        "q7_bid_weakening_early_warning": {
            "note": "Compare G4/G5 action delay vs G1 warning time lead into down 0.25",
            "g1_lead": g1["median_lead_seconds_down_0_25"],
            "g4_delay": g4["median_warning_to_action_delay"],
            "g5_delay": g5["median_warning_to_action_delay"],
        },
        "q8_best_use_case": _infer_use_case(action_rows, forward_rows),
    }

    # False warnings / missed reversals (episode based on G0 session outcomes)
    false_rows = []
    missed_rows = []
    g0_sess = {
        f["episode_id"]: f
        for f in forward_rows
        if f["variant"] == "G0" and f["horizon"] == "session_end"
    }
    for ep in episodes:
        eid = ep["episode_id"]
        s = g0_sess.get(eid)
        if s is None:
            continue
        if not s.get("first_touch_down_0_10"):
            false_rows.append(
                {
                    "episode_id": eid,
                    "first_warning_id": ep["first_warning"].warning_id,
                    "reason": "NO_DOWN_0_10",
                    "max_adverse_up_pct": s.get("max_adverse_up_pct"),
                    "time_to_new_high": s.get("time_to_new_high"),
                }
            )
        # missed: down 0.25 happened on G0 but G5 had no action
        g5_has = any(a["episode_id"] == eid and a["variant"] == "G5" for a in action_rows)
        if s.get("first_touch_down_0_25") and not g5_has:
            missed_rows.append(
                {
                    "episode_id": eid,
                    "first_warning_id": ep["first_warning"].warning_id,
                    "reason": "G5_FILTER_MISSED_DOWN_0_25",
                    "time_to_down_0_25": s.get("time_to_down_0_25"),
                }
            )

    # Post-hoc reference points only
    ref_rows = []
    for ref in REFERENCE_TIMES:
        ref_ts = _ensure_utc(parse_utc(ref))
        nearby = [
            w
            for w in warnings
            if abs((w.warning_time - ref_ts).total_seconds()) <= 900
        ]
        trend = join_regime_as_of(regimes, as_of=ref_ts)
        ref_rows.append(
            {
                "reference_time": ref,
                "nearby_warning_count": len(nearby),
                "nearby_warning_ids": ",".join(w.warning_id for w in nearby),
                "regime_at_reference": trend.get("combined_regime"),
                "trend_state_time": None
                if trend.get("trend_state_time") is None
                else trend["trend_state_time"].isoformat(),
                "note": "POST_HOC_ONLY_NOT_USED_IN_DETECTION",
            }
        )

    write_csv(output_dir / "integrated_warning_context.csv", context_rows)
    write_csv(output_dir / "integrated_warning_episodes.csv", episode_rows)
    write_csv(output_dir / "integrated_support_breaks.csv", support_rows)
    write_csv(output_dir / "integrated_variant_actions.csv", action_rows)
    write_csv(output_dir / "integrated_forward_outcomes.csv", forward_rows)
    write_csv(output_dir / "integrated_variant_summary.csv", variant_summary)
    write_csv(
        output_dir / "integrated_ablation_summary.csv",
        [{"question": k, "payload": orjson.dumps(v).decode()} for k, v in ablation.items()],
    )
    write_csv(output_dir / "integrated_false_warnings.csv", false_rows)
    write_csv(output_dir / "integrated_missed_reversals.csv", missed_rows)
    write_csv(output_dir / "integrated_reference_point_audit.csv", ref_rows)

    decision = _decide(variant_summary, ablation)
    summary = {
        "decision": decision,
        "warning_count": len(warnings),
        "episode_count": len(episodes),
        "actions_by_variant": {r["variant"]: r["actions_total"] for r in variant_summary},
        "variant_summary": variant_summary,
        "ablation": ablation,
        "params": asdict(params),
        "inputs": {
            "warnings": str(warnings_path),
            "features": str(features_path),
            "regimes": str(regimes_path),
        },
        "limitations": [
            "Research only; no live orders.",
            "Old bid-weakening support_break_confirmed is ignored; break rebuilt causally.",
            "Reference times are post-hoc only.",
            "Range position unavailable under SimpleRegime neutral → RANGE_CONTEXT_INSUFFICIENT.",
        ],
    }
    (output_dir / "strategy_summary.json").write_bytes(
        orjson.dumps(summary, option=orjson.OPT_INDENT_2)
    )
    (output_dir / "REPORT.md").write_text(render_report(summary), encoding="utf-8")
    return summary


def _infer_use_case(
    action_rows: Sequence[dict[str, Any]], forward_rows: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    by_action: dict[str, list[dict[str, Any]]] = {}
    for a in action_rows:
        if a["variant"] not in {"G1", "G3", "G4", "G5"}:
            continue
        by_action.setdefault(a["action"], []).append(a)
    stats = {}
    for action, acts in by_action.items():
        sess = [
            f
            for f in forward_rows
            if f["horizon"] == "session_end"
            and f["variant"] in {"G1", "G3", "G4", "G5"}
            and f["action"] == action
        ]
        if not sess:
            continue
        stats[action] = {
            "n": len(sess),
            "hit_0_25": round(
                sum(1 for r in sess if r.get("first_touch_down_0_25")) / len(sess), 6
            ),
            "median_adverse_up": sorted(r["max_adverse_up_pct"] for r in sess)[
                len(sess) // 2
            ],
        }
    return stats


def _decide(variant_summary: Sequence[dict[str, Any]], ablation: dict[str, Any]) -> str:
    g0 = next(r for r in variant_summary if r["variant"] == "G0")
    g5 = next(r for r in variant_summary if r["variant"] == "G5")
    g6 = next(r for r in variant_summary if r["variant"] == "G6")
    if g5["actions_total"] == 0 and g6["actions_total"] == 0:
        return "INTEGRATED_WARNING_SIGNAL_INCONCLUSIVE"
    g5_hit = g5["hit_rate_down_0_25"] or 0
    g0_hit = g0["hit_rate_down_0_25"] or 0
    g6_hit = g6["hit_rate_down_0_25"] or 0
    if g5_hit > g0_hit and g5["false_warning_count"] < g0["false_warning_count"]:
        if g5_hit >= g6_hit and g5["actions_total"] > 0:
            return "INTEGRATED_WARNING_SIGNAL_PROMISING"
    if g5["actions_total"] and (g5_hit <= g0_hit):
        # maybe only stop-adds useful
        return "INTEGRATED_WARNING_SIGNAL_USEFUL_ONLY_FOR_STOP_ADDS"
    if g5_hit <= g6_hit and g6["actions_total"]:
        return "INTEGRATED_WARNING_SIGNAL_NO_INCREMENTAL_VALUE"
    return "INTEGRATED_WARNING_SIGNAL_INCONCLUSIVE"


def render_report(summary: dict[str, Any]) -> str:
    lines = [
        "# Integrated Trend + Bid-Weakening Audit",
        "",
        f"Decision: **{summary.get('decision')}**",
        f"Warnings: {summary.get('warning_count')}",
        f"Episodes: {summary.get('episode_count')}",
        "",
        "## Actions by variant",
        "",
    ]
    for k, v in (summary.get("actions_by_variant") or {}).items():
        lines.append(f"- {k}: {v}")
    lines.extend(["", "## Ablation (summary payloads in CSV/JSON)", ""])
    for q, payload in (summary.get("ablation") or {}).items():
        lines.append(f"- {q}: `{payload}`")
    lines.extend(["", "## Limitations", ""])
    for lim in summary.get("limitations") or []:
        lines.append(f"- {lim}")
    lines.append("")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Integrated trend + bid-weakening audit")
    p.add_argument(
        "--bid-weakening-dir",
        default=str(
            PROJECT_ROOT
            / "results"
            / "orderbook_bid_weakening_full_history_APTUSDT_20260726T164357Z"
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
    p.add_argument("--strong-min-score", type=int, default=8)
    p.add_argument("--weak-max-score", type=int, default=5)
    p.add_argument("--support-break-confirm-snapshots", type=int, default=2)
    p.add_argument("--support-break-min-depth-bps", type=float, default=5.0)
    p.add_argument("--support-reclaim-tolerance-bps", type=float, default=5.0)
    p.add_argument("--support-break-max-confirm-seconds", type=int, default=180)
    p.add_argument("--episode-gap-seconds", type=int, default=300)
    p.add_argument("--one-action-per-episode", action="store_true", default=True)
    p.add_argument("--no-one-action-per-episode", action="store_true", default=False)
    p.add_argument("--session-end", default="2026-07-26T13:08:27Z")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    bw = Path(args.bid_weakening_dir)
    out = Path(args.output_dir) if args.output_dir else (
        PROJECT_ROOT
        / "results"
        / f"orderbook_trend_bid_weakening_{utc_now().strftime('%Y%m%dT%H%M%SZ')}"
    )
    params = IntegratedParams(
        strong_min_score=int(args.strong_min_score),
        weak_max_score=int(args.weak_max_score),
        support_break_confirm_snapshots=int(args.support_break_confirm_snapshots),
        support_break_min_depth_bps=float(args.support_break_min_depth_bps),
        support_reclaim_tolerance_bps=float(args.support_reclaim_tolerance_bps),
        support_break_max_confirm_seconds=int(args.support_break_max_confirm_seconds),
        episode_gap_seconds=int(args.episode_gap_seconds),
        one_action_per_episode=not bool(args.no_one_action_per_episode),
        session_end=str(args.session_end),
    )
    summary = run_integrated_audit(
        warnings_path=bw / "bid_weakening_warnings.csv",
        features_path=bw / "bid_weakening_features.csv",
        regimes_path=Path(args.regime_snapshots),
        timeline_path=bw / "bid_weakening_state_timeline.csv",
        output_dir=out,
        params=params,
    )
    summary["output_dir"] = str(out)
    (out / "strategy_summary.json").write_bytes(
        orjson.dumps(summary, option=orjson.OPT_INDENT_2)
    )
    sys.stdout.buffer.write(
        orjson.dumps(
            {
                "decision": summary.get("decision"),
                "episodes": summary.get("episode_count"),
                "actions_by_variant": summary.get("actions_by_variant"),
                "output_dir": str(out),
            },
            option=orjson.OPT_INDENT_2,
        )
    )
    sys.stdout.buffer.write(b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
