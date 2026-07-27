"""Phase 5: causal descriptive pattern candidates (no signals, no outcomes)."""

from __future__ import annotations

import logging
import math
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from orderbook_analyse.dynamic_wall_detector import _ensure_aware
from orderbook_analyse.replay_segmentation import ReplayGap, ReplaySegment

logger = logging.getLogger(__name__)

PATTERN_VERSION = "phase5_v1"
FORBIDDEN_OUTCOME_COLUMNS = frozenset(
    {
        "return_after",
        "mfe_",
        "mae_",
        "max_profit_",
        "max_adverse_",
        "future_",
        "label_profitable",
        "target",
        "win",
        "loss",
    }
)

PHASE5_OUTPUT_FILES = [
    "pattern_candidates.csv",
    "pattern_feature_matrix.csv",
    "pattern_transitions_context.csv",
    "pattern_summary_by_symbol.csv",
    "pattern_summary_by_segment.csv",
    "pattern_summary_by_type.csv",
    "pattern_integrity_errors.csv",
]


class PatternCandidateError(ValueError):
    pass


@dataclass
class PatternParams:
    timeframe: str = "1m"
    lookback_bars: int = 5
    min_wall_age_sec: float = 120.0
    min_wall_samples: int = 2
    near_distance_bps: float = 100.0
    strong_wall_multiple: float = 3.0
    dominant_depth_share: float = 0.05
    delta_ratio_threshold: float = 0.20
    oi_change_threshold_pct: float = 0.10
    price_change_threshold_pct: float = 0.05
    wall_growth_threshold_pct: float = 20.0
    wall_imbalance_threshold: float = 0.5
    cooldown_bars: int = 3
    replacement_window_bars: int = 5
    output_mode: str = "all"


def parse_pattern_timeframe(raw: str | None) -> str:
    tf = (raw or "1m").strip().lower()
    if tf not in {"1m", "5m"}:
        raise PatternCandidateError(f"unsupported pattern timeframe {raw!r}; supported: 1m,5m")
    return tf


def validate_pattern_params(params: PatternParams) -> PatternParams:
    params.timeframe = parse_pattern_timeframe(params.timeframe)
    if params.lookback_bars < 1:
        raise PatternCandidateError("pattern-lookback-bars must be >= 1")
    if params.min_wall_age_sec < 0:
        raise PatternCandidateError("pattern-min-wall-age-sec must be >= 0")
    if params.min_wall_samples < 1:
        raise PatternCandidateError("pattern-min-wall-samples must be >= 1")
    if params.cooldown_bars < 0:
        raise PatternCandidateError("pattern-cooldown-bars must be >= 0")
    if params.output_mode not in {"all", "lifecycle_only", "composite_only"}:
        raise PatternCandidateError(
            f"unsupported pattern-output-mode {params.output_mode!r}"
        )
    for name, val in (
        ("pattern-delta-ratio-threshold", params.delta_ratio_threshold),
        ("pattern-oi-change-threshold-pct", params.oi_change_threshold_pct),
        ("pattern-price-change-threshold-pct", params.price_change_threshold_pct),
        ("pattern-wall-growth-threshold-pct", params.wall_growth_threshold_pct),
        ("pattern-wall-imbalance-threshold", params.wall_imbalance_threshold),
        ("pattern-near-distance-bps", params.near_distance_bps),
        ("pattern-strong-wall-multiple", params.strong_wall_multiple),
        ("pattern-dominant-depth-share", params.dominant_depth_share),
    ):
        if not math.isfinite(float(val)) or float(val) < 0:
            raise PatternCandidateError(f"{name} must be finite and >= 0")
    return params


def _iso_to_dt(v: Any) -> datetime:
    if isinstance(v, datetime):
        return _ensure_aware(v)
    return _ensure_aware(datetime.fromisoformat(str(v).replace("Z", "+00:00")))


def _safe_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def _fmt_ts(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S")


def make_pattern_id(
    *,
    symbol: str,
    segment_id: str,
    timeframe: str,
    pattern_type: str,
    pattern_ts: datetime,
    sequence_id: str | None = None,
    transition_ts: datetime | None = None,
) -> str:
    """Derive pattern_id from the same canonical identity as candidate_key."""
    base = f"{symbol}:{segment_id}:{timeframe}:{pattern_type}:{_fmt_ts(pattern_ts)}"
    # Sequence suffix keeps IDs unique when multiple walls share the same bucket.
    if sequence_id:
        base = f"{base}:{sequence_id}"
    # Distinct causal transition events in the same bucket stay distinct.
    if transition_ts is not None:
        base = f"{base}:T{_fmt_ts(transition_ts)}"
    return base


def make_candidate_key(
    *,
    symbol: str,
    segment_id: str,
    timeframe: str,
    pattern_type: str,
    pattern_ts: datetime,
    source_wall_sequence_id: str | None = None,
    source_transition_type: str | None = None,
    source_transition_ts: datetime | str | None = None,
    subject_key: str | None = None,
) -> tuple[str, ...]:
    """Canonical key for defensive candidate deduplication (no UUID / serial)."""
    tr_ts = ""
    if source_transition_ts is not None and source_transition_ts != "":
        if isinstance(source_transition_ts, datetime):
            tr_ts = source_transition_ts.isoformat()
        else:
            tr_ts = str(source_transition_ts)
    return (
        str(symbol),
        str(segment_id),
        str(timeframe),
        str(pattern_type),
        pattern_ts.isoformat(),
        str(source_wall_sequence_id or ""),
        str(source_transition_type or ""),
        tr_ts,
        str(subject_key or ""),
    )


PATTERN_FAMILY: dict[str, str] = {}


def _reg(ptype: str, family: str) -> str:
    PATTERN_FAMILY[ptype] = family
    return ptype


# Wall lifecycle
for _side in ("BID", "ASK"):
    for _ev in (
        "APPEARED",
        "PERSISTENT",
        "GREW",
        "SHRANK",
        "MOVED_TOWARD_PRICE",
        "MOVED_AWAY_FROM_PRICE",
        "TESTED",
        "TRADED_THROUGH",
        "CONFIRMED_BREAK",
        "DISAPPEARED_UNTESTED",
    ):
        _reg(f"{_side}_WALL_{_ev}", "WALL_LIFECYCLE")

# Price/delta
for _p in (
    "PRICE_DOWN_DELTA_POSITIVE",
    "PRICE_UP_DELTA_NEGATIVE",
    "PRICE_FLAT_DELTA_POSITIVE",
    "PRICE_FLAT_DELTA_NEGATIVE",
):
    _reg(_p, "PRICE_DELTA_DIVERGENCE")

# Price/OI
for _p in (
    "PRICE_UP_OI_UP",
    "PRICE_UP_OI_DOWN",
    "PRICE_DOWN_OI_UP",
    "PRICE_DOWN_OI_DOWN",
    "PRICE_FLAT_OI_UP",
    "PRICE_FLAT_OI_DOWN",
):
    _reg(_p, "PRICE_OI")

# Wall+flow
for _p in (
    "BID_WALL_WITH_SELL_PRESSURE",
    "BID_WALL_WITH_BUY_PRESSURE",
    "ASK_WALL_WITH_BUY_PRESSURE",
    "ASK_WALL_WITH_SELL_PRESSURE",
    "BID_WALL_GROWING_WITH_SELL_PRESSURE",
    "ASK_WALL_GROWING_WITH_BUY_PRESSURE",
    "BID_WALL_PERSISTENT_PRICE_NOT_FALLING",
    "ASK_WALL_PERSISTENT_PRICE_NOT_RISING",
    "BID_WALL_TESTED_WITH_SELL_PRESSURE",
    "ASK_WALL_TESTED_WITH_BUY_PRESSURE",
    "BID_WALL_BREAK_WITH_SELL_PRESSURE",
    "ASK_WALL_BREAK_WITH_BUY_PRESSURE",
):
    _reg(_p, "WALL_FLOW")

for _p in ("BID_WALL_DOMINANCE", "ASK_WALL_DOMINANCE", "BALANCED_WALL_LIQUIDITY"):
    _reg(_p, "WALL_IMBALANCE")

for _p in (
    "BUY_LIQUIDATION_CLUSTER",
    "SELL_LIQUIDATION_CLUSTER",
    "WALL_TEST_WITH_LIQUIDATIONS",
    "WALL_BREAK_WITH_LIQUIDATIONS",
):
    _reg(_p, "LIQUIDATION")

_reg("BID_ABSORPTION_CANDIDATE", "ABSORPTION_CANDIDATE")
_reg("ASK_ABSORPTION_CANDIDATE", "ABSORPTION_CANDIDATE")
_reg("BID_WALL_FAILURE_CANDIDATE", "WALL_FAILURE_CANDIDATE")
_reg("ASK_WALL_FAILURE_CANDIDATE", "WALL_FAILURE_CANDIDATE")
_reg("BID_WALL_PULLING_CANDIDATE", "WALL_PULLING_CANDIDATE")
_reg("ASK_WALL_PULLING_CANDIDATE", "WALL_PULLING_CANDIDATE")
_reg("WALL_REPLACEMENT_LOWER", "WALL_REPLACEMENT")
_reg("WALL_REPLACEMENT_HIGHER", "WALL_REPLACEMENT")

TRANSITION_ONCE = frozenset(
    {
        "BID_WALL_APPEARED",
        "ASK_WALL_APPEARED",
        "BID_WALL_TESTED",
        "ASK_WALL_TESTED",
        "BID_WALL_TRADED_THROUGH",
        "ASK_WALL_TRADED_THROUGH",
        "BID_WALL_CONFIRMED_BREAK",
        "ASK_WALL_CONFIRMED_BREAK",
        "BID_WALL_DISAPPEARED_UNTESTED",
        "ASK_WALL_DISAPPEARED_UNTESTED",
        "BID_WALL_FAILURE_CANDIDATE",
        "ASK_WALL_FAILURE_CANDIDATE",
        "BID_WALL_TESTED_WITH_SELL_PRESSURE",
        "ASK_WALL_TESTED_WITH_BUY_PRESSURE",
        "BID_WALL_BREAK_WITH_SELL_PRESSURE",
        "ASK_WALL_BREAK_WITH_BUY_PRESSURE",
        "WALL_TEST_WITH_LIQUIDATIONS",
        "WALL_BREAK_WITH_LIQUIDATIONS",
        "BID_WALL_PULLING_CANDIDATE",
        "ASK_WALL_PULLING_CANDIDATE",
    }
)

# Pure lifecycle size/move events: once per (type, sequence, transition_ts).
# Prevents duplicate GREW/SHRANK/MOVED from repeated transition rows in one bucket
# while still allowing distinct growth events at different timestamps.
TRANSITION_ONCE_PER_EVENT = frozenset(
    {
        "BID_WALL_GREW",
        "ASK_WALL_GREW",
        "BID_WALL_SHRANK",
        "ASK_WALL_SHRANK",
        "BID_WALL_MOVED_TOWARD_PRICE",
        "ASK_WALL_MOVED_TOWARD_PRICE",
        "BID_WALL_MOVED_AWAY_FROM_PRICE",
        "ASK_WALL_MOVED_AWAY_FROM_PRICE",
    }
)


@dataclass
class _WallAsOf:
    sequence_id: str
    side: str
    appeared_ts: datetime | None = None
    last_transition_ts: datetime | None = None
    last_transition_type: str | None = None
    price: float | None = None
    notional: float | None = None
    prev_notional: float | None = None
    distance_bps: float | None = None
    prev_distance_bps: float | None = None
    sample_count: int = 0
    tested: bool = False
    traded_through: bool = False
    broken: bool = False
    disappeared: bool = False
    grew: bool = False
    shrank: bool = False
    moved_toward: bool = False
    moved_away: bool = False
    first_test_ts: datetime | None = None
    first_traded_ts: datetime | None = None
    confirmed_break_ts: datetime | None = None
    disappeared_ts: datetime | None = None


@dataclass
class PatternCandidateResult:
    params: PatternParams
    candidates: list[dict[str, Any]] = field(default_factory=list)
    features: list[dict[str, Any]] = field(default_factory=list)
    transition_contexts: list[dict[str, Any]] = field(default_factory=list)
    summary_by_symbol: list[dict[str, Any]] = field(default_factory=list)
    summary_by_segment: list[dict[str, Any]] = field(default_factory=list)
    summary_by_type: list[dict[str, Any]] = field(default_factory=list)
    integrity_errors: list[dict[str, Any]] = field(default_factory=list)
    timelines: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    ok: bool = True
    error_message: str | None = None


def _sources_present(row: Mapping[str, Any]) -> set[str]:
    raw = str(row.get("data_sources_present") or "")
    return {p for p in raw.split("|") if p}


def _price_regime(chg: float | None, thr: float) -> str | None:
    if chg is None:
        return None
    if chg <= -thr:
        return "DOWN"
    if chg >= thr:
        return "UP"
    if abs(chg) < thr:
        return "FLAT"
    return None


def _delta_regime(ratio: float | None, thr: float) -> str | None:
    if ratio is None:
        return None
    if ratio >= thr:
        return "POSITIVE"
    if ratio <= -thr:
        return "NEGATIVE"
    return None


def _oi_regime(chg: float | None, thr: float) -> str | None:
    if chg is None:
        return None
    if chg >= thr:
        return "UP"
    if chg <= -thr:
        return "DOWN"
    return None


def compute_rolling_features(
    window: Sequence[Mapping[str, Any]],
    *,
    lookback: int,
) -> dict[str, Any]:
    """Rolling features from oldest→newest window ending at current bar."""
    bars = list(window[-lookback:]) if lookback > 0 else list(window)
    out: dict[str, Any] = {
        "price_change_1bar_pct": None,
        "price_change_3bar_pct": None,
        "price_change_5bar_pct": None,
        "range_1bar_pct": None,
        "range_5bar_pct": None,
        "rolling_price_change_pct": None,
        "delta_notional_1bar": None,
        "delta_notional_3bar": None,
        "delta_notional_5bar": None,
        "delta_ratio_1bar": None,
        "delta_ratio_3bar": None,
        "delta_ratio_5bar": None,
        "rolling_delta_notional": None,
        "rolling_delta_ratio": None,
        "buy_share_1bar": None,
        "sell_share_1bar": None,
        "oi_change_1bar_pct": None,
        "oi_change_3bar_pct": None,
        "oi_change_5bar_pct": None,
        "rolling_oi_change_pct": None,
        "liquidation_count_1bar": None,
        "liquidation_count_5bar": None,
        "liquidation_notional_1bar": None,
        "liquidation_notional_5bar": None,
        "buy_liquidation_notional_5bar": None,
        "sell_liquidation_notional_5bar": None,
        "has_trades": False,
        "has_oi": False,
        "has_liquidations_field": False,
    }
    if not bars:
        return out
    cur = bars[-1]

    def _slice(n: int) -> list[Mapping[str, Any]]:
        return bars[-n:] if len(bars) >= n else list(bars)

    def _price_chg(n: int) -> float | None:
        sl = _slice(n)
        if not sl:
            return None
        o = _safe_float(sl[0].get("open_price"))
        c = _safe_float(sl[-1].get("close_price"))
        if o is None or c is None or o == 0:
            return None
        return (c - o) / o * 100.0

    def _range_pct(n: int) -> float | None:
        sl = _slice(n)
        highs = [_safe_float(b.get("high_price")) for b in sl]
        lows = [_safe_float(b.get("low_price")) for b in sl]
        opens = [_safe_float(b.get("open_price")) for b in sl]
        highs_f = [h for h in highs if h is not None]
        lows_f = [x for x in lows if x is not None]
        o0 = opens[0] if opens else None
        if not highs_f or not lows_f or o0 is None or o0 == 0:
            return None
        return (max(highs_f) - min(lows_f)) / o0 * 100.0

    def _delta_sum(n: int) -> float | None:
        sl = _slice(n)
        vals = [_safe_float(b.get("delta_notional")) for b in sl]
        present = [v for v in vals if v is not None]
        if not present:
            return None
        return float(sum(present))

    def _delta_ratio(n: int) -> float | None:
        sl = _slice(n)
        dn = [_safe_float(b.get("delta_notional")) for b in sl]
        tn = [_safe_float(b.get("total_notional")) for b in sl]
        if all(v is None for v in dn) and all(v is None for v in tn):
            return None
        dsum = sum(v or 0.0 for v in dn)
        tsum = sum(v or 0.0 for v in tn)
        if tsum == 0:
            return None
        return dsum / tsum

    def _oi_chg(n: int) -> float | None:
        sl = _slice(n)
        o = _safe_float(sl[0].get("oi_open"))
        c = _safe_float(sl[-1].get("oi_close"))
        if o is None or c is None or o == 0:
            return None
        return (c - o) / o * 100.0

    out["price_change_1bar_pct"] = _price_chg(1)
    out["price_change_3bar_pct"] = _price_chg(3)
    out["price_change_5bar_pct"] = _price_chg(5)
    out["rolling_price_change_pct"] = _price_chg(lookback)
    out["range_1bar_pct"] = _range_pct(1)
    out["range_5bar_pct"] = _range_pct(5)
    out["delta_notional_1bar"] = _delta_sum(1)
    out["delta_notional_3bar"] = _delta_sum(3)
    out["delta_notional_5bar"] = _delta_sum(5)
    out["rolling_delta_notional"] = _delta_sum(lookback)
    out["delta_ratio_1bar"] = _delta_ratio(1)
    out["delta_ratio_3bar"] = _delta_ratio(3)
    out["delta_ratio_5bar"] = _delta_ratio(5)
    out["rolling_delta_ratio"] = _delta_ratio(lookback)
    buy = _safe_float(cur.get("buy_notional"))
    sell = _safe_float(cur.get("sell_notional"))
    total = _safe_float(cur.get("total_notional"))
    if total is not None and total > 0 and buy is not None:
        out["buy_share_1bar"] = buy / total
    if total is not None and total > 0 and sell is not None:
        out["sell_share_1bar"] = sell / total
    # Missing trade fields must not be treated as genuine zero flow.
    out["has_trades"] = "trades" in _sources_present(cur) or (
        cur.get("trade_count") is not None or cur.get("total_notional") is not None
    )
    out["oi_change_1bar_pct"] = _oi_chg(1)
    out["oi_change_3bar_pct"] = _oi_chg(3)
    out["oi_change_5bar_pct"] = _oi_chg(5)
    out["rolling_oi_change_pct"] = _oi_chg(lookback)
    out["has_oi"] = cur.get("oi_open") is not None or cur.get("oi_close") is not None
    lc1 = _safe_float(cur.get("liquidation_count"))
    ln1 = _safe_float(cur.get("liquidation_notional"))
    out["liquidation_count_1bar"] = lc1
    out["liquidation_notional_1bar"] = ln1
    sl5 = _slice(5)
    if any(b.get("liquidation_count") is not None for b in sl5):
        out["has_liquidations_field"] = True
        out["liquidation_count_5bar"] = float(
            sum(_safe_float(b.get("liquidation_count")) or 0.0 for b in sl5)
        )
        out["liquidation_notional_5bar"] = float(
            sum(_safe_float(b.get("liquidation_notional")) or 0.0 for b in sl5)
        )
        out["buy_liquidation_notional_5bar"] = float(
            sum(_safe_float(b.get("buy_liquidation_notional")) or 0.0 for b in sl5)
        )
        out["sell_liquidation_notional_5bar"] = float(
            sum(_safe_float(b.get("sell_liquidation_notional")) or 0.0 for b in sl5)
        )
    return out


def _apply_transition(state: dict[str, _WallAsOf], tr: Mapping[str, Any]) -> None:
    sid = str(tr.get("wall_sequence_id") or "")
    if not sid:
        return
    side = str(tr.get("side") or "").lower()
    ttype = str(tr.get("transition_type") or "")
    ts = _iso_to_dt(tr["transition_ts"]) if tr.get("transition_ts") else None
    w = state.get(sid)
    if w is None:
        w = _WallAsOf(sequence_id=sid, side=side)
        state[sid] = w
    if ts is not None:
        w.last_transition_ts = ts
    w.last_transition_type = ttype
    if tr.get("current_price") not in (None, ""):
        w.price = _safe_float(tr.get("current_price"))
    elif tr.get("previous_price") not in (None, ""):
        w.price = _safe_float(tr.get("previous_price"))
    if tr.get("current_notional") not in (None, ""):
        w.prev_notional = w.notional
        w.notional = _safe_float(tr.get("current_notional"))
    if tr.get("previous_notional") not in (None, "") and w.prev_notional is None:
        w.prev_notional = _safe_float(tr.get("previous_notional"))
    if tr.get("current_distance_bps") not in (None, ""):
        w.prev_distance_bps = w.distance_bps
        w.distance_bps = _safe_float(tr.get("current_distance_bps"))
    if tr.get("previous_distance_bps") not in (None, "") and w.prev_distance_bps is None:
        w.prev_distance_bps = _safe_float(tr.get("previous_distance_bps"))

    if ttype == "APPEARED":
        w.appeared_ts = ts
        w.sample_count = max(w.sample_count, 1)
        w.disappeared = False
    elif ttype == "PERSISTED":
        w.sample_count += 1
    elif ttype == "GREW":
        w.grew = True
        w.sample_count += 1
    elif ttype == "SHRANK":
        w.shrank = True
        w.sample_count += 1
    elif ttype == "MOVED_TOWARD_PRICE":
        w.moved_toward = True
    elif ttype == "MOVED_AWAY_FROM_PRICE":
        w.moved_away = True
    elif ttype == "TESTED":
        w.tested = True
        if w.first_test_ts is None:
            w.first_test_ts = ts
    elif ttype == "TRADED_THROUGH":
        w.traded_through = True
        if w.first_traded_ts is None:
            w.first_traded_ts = ts
    elif ttype == "BROKEN":
        w.broken = True
        if w.confirmed_break_ts is None:
            w.confirmed_break_ts = ts
    elif ttype == "DISAPPEARED":
        w.disappeared = True
        w.disappeared_ts = ts


def _wall_age_sec(w: _WallAsOf, as_of: datetime) -> float | None:
    if w.appeared_ts is None:
        return None
    return max(0.0, (as_of - w.appeared_ts).total_seconds())


def _notional_change_pct(w: _WallAsOf) -> float | None:
    if w.notional is None or w.prev_notional is None or w.prev_notional == 0:
        return None
    return (w.notional - w.prev_notional) / abs(w.prev_notional) * 100.0


def _distance_change_bps(w: _WallAsOf) -> float | None:
    if w.distance_bps is None or w.prev_distance_bps is None:
        return None
    return w.distance_bps - w.prev_distance_bps


def _active_near_wall(
    walls: Mapping[str, _WallAsOf],
    *,
    side: str,
    params: PatternParams,
    as_of: datetime,
) -> _WallAsOf | None:
    cands = [
        w
        for w in walls.values()
        if w.side == side and not w.disappeared and not w.broken
    ]
    if not cands:
        return None
    near = [
        w
        for w in cands
        if w.distance_bps is not None and w.distance_bps <= params.near_distance_bps
    ]
    pool = near or cands
    return min(pool, key=lambda w: (w.distance_bps if w.distance_bps is not None else 1e18, w.sequence_id))


def decide_phase5_patterns(
    *,
    ok: bool,
    gap_count: int,
    has_failures: bool,
    has_success: bool,
) -> str:
    if not ok:
        return "FULL_HISTORY_PATTERN_CANDIDATES_FAILED"
    if has_failures and has_success:
        return "FULL_HISTORY_PATTERN_CANDIDATES_PARTIAL"
    if has_failures and not has_success:
        return "FULL_HISTORY_PATTERN_CANDIDATES_FAILED"
    if gap_count > 0:
        return "FULL_HISTORY_PATTERN_CANDIDATES_COMPLETE_WITH_GAPS"
    return "FULL_HISTORY_PATTERN_CANDIDATES_COMPLETE"


class _Deduper:
    def __init__(self, cooldown_bars: int) -> None:
        self.cooldown = cooldown_bars
        self.last_emit_bar: dict[tuple[str, ...], int] = {}
        self.once: set[tuple[str, ...]] = set()

    def allow(
        self,
        *,
        pattern_type: str,
        segment_id: str,
        side: str | None,
        sequence_id: str | None,
        bar_index: int,
        force_event: bool,
        transition_ts: str | None = None,
    ) -> bool:
        if pattern_type in TRANSITION_ONCE and sequence_id:
            key: tuple[str, ...] = (pattern_type, sequence_id)
            if key in self.once:
                return False
            if force_event:
                self.once.add(key)
                self.last_emit_bar[(segment_id, pattern_type, sequence_id or side or "")] = bar_index
                return True
            return False
        if pattern_type in TRANSITION_ONCE_PER_EVENT and sequence_id:
            key = (pattern_type, sequence_id, transition_ts or "")
            if key in self.once:
                return False
            if force_event:
                self.once.add(key)
                self.last_emit_bar[(segment_id, pattern_type, sequence_id or side or "")] = bar_index
                return True
            return False
        state_key = (segment_id, pattern_type, sequence_id or side or "")
        last = self.last_emit_bar.get(state_key)
        if force_event:
            self.last_emit_bar[state_key] = bar_index
            return True
        if last is None:
            self.last_emit_bar[state_key] = bar_index
            return True
        if bar_index - last > self.cooldown:
            self.last_emit_bar[state_key] = bar_index
            return True
        return False


def _bucket_in_gap(ts: datetime, gaps: Sequence[ReplayGap]) -> bool:
    for g in gaps:
        if _ensure_aware(g.gap_start_ts) <= ts <= _ensure_aware(g.gap_end_ts):
            return True
    return False


def detect_patterns_for_segment(
    *,
    symbol: str,
    segment: ReplaySegment,
    timeline_rows: Sequence[Mapping[str, Any]],
    transitions: Sequence[Mapping[str, Any]],
    gaps: Sequence[ReplayGap],
    params: PatternParams,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return candidates, features, transition contexts, integrity errors for one segment."""
    params = validate_pattern_params(params)
    seg_id = segment.segment_id
    rows = sorted(
        [
            r
            for r in timeline_rows
            if str(r.get("wall_segment_id") or r.get("segment_id") or seg_id) == seg_id
            or (
                _ensure_aware(segment.segment_start_ts)
                <= _iso_to_dt(r["bucket_end"])
                <= _ensure_aware(segment.segment_end_ts)
            )
        ],
        key=lambda r: _iso_to_dt(r["bucket_end"]),
    )
    # Prefer rows tagged with this segment when present
    tagged = [r for r in rows if str(r.get("wall_segment_id") or "") == seg_id]
    if tagged:
        rows = tagged

    seg_transitions = sorted(
        [t for t in transitions if str(t.get("segment_id") or "") == seg_id],
        key=lambda t: (_iso_to_dt(t["transition_ts"]), str(t.get("transition_type"))),
    )

    candidates: list[dict[str, Any]] = []
    features: list[dict[str, Any]] = []
    tr_ctx: list[dict[str, Any]] = []
    errs: list[dict[str, Any]] = []
    walls: dict[str, _WallAsOf] = {}
    window: list[Mapping[str, Any]] = []
    deduper = _Deduper(params.cooldown_bars)
    seen_candidate_keys: set[tuple[str, ...]] = set()
    duplicate_suppressed_box = [0]
    tr_i = 0
    gone_bid: list[tuple[datetime, float]] = []
    gone_ask: list[tuple[datetime, float]] = []

    for bar_index, row in enumerate(rows):
        bucket_end = _iso_to_dt(row["bucket_end"])
        bucket_start = _iso_to_dt(row["bucket_start"])
        if _bucket_in_gap(bucket_end, gaps):
            # Discard gap-era transitions; never carry wall/rolling state across gaps.
            while tr_i < len(seg_transitions):
                tts = _iso_to_dt(seg_transitions[tr_i]["transition_ts"])
                if tts > bucket_end:
                    break
                tr_i += 1
            window = []
            walls.clear()
            gone_bid.clear()
            gone_ask.clear()
            continue
        # apply transitions up to bucket_end
        new_events: list[Mapping[str, Any]] = []
        while tr_i < len(seg_transitions):
            tts = _iso_to_dt(seg_transitions[tr_i]["transition_ts"])
            if tts > bucket_end:
                break
            tr = seg_transitions[tr_i]
            _apply_transition(walls, tr)
            new_events.append(tr)
            tr_i += 1
            # track disappearances for replacement
            if tr.get("transition_type") == "DISAPPEARED":
                sid = str(tr.get("wall_sequence_id"))
                w = walls.get(sid)
                if w and w.price is not None:
                    if w.side == "bid":
                        gone_bid.append((tts, w.price))
                    elif w.side == "ask":
                        gone_ask.append((tts, w.price))

        window.append(row)
        if len(window) > max(params.lookback_bars, 5):
            window = window[-max(params.lookback_bars, 5) :]

        roll = compute_rolling_features(window, lookback=params.lookback_bars)
        price_reg = _price_regime(roll["rolling_price_change_pct"], params.price_change_threshold_pct)
        delta_reg = _delta_regime(roll["rolling_delta_ratio"], params.delta_ratio_threshold)
        oi_reg = _oi_regime(roll["rolling_oi_change_pct"], params.oi_change_threshold_pct)
        sources = _sources_present(row)
        has_price = row.get("close_price") is not None
        has_walls = bool(row.get("wall_data_present"))
        has_trades = roll["has_trades"] or "trades" in sources
        has_oi = roll["has_oi"] or "oi" in sources
        has_liq = roll["has_liquidations_field"] or "liquidations" in sources

        # transition contexts for events in this bucket
        for tr in new_events:
            tr_ctx.append(
                {
                    "pattern_id": None,
                    "symbol": symbol,
                    "segment_id": seg_id,
                    "transition_ts": tr.get("transition_ts"),
                    "transition_type": tr.get("transition_type"),
                    "wall_sequence_id": tr.get("wall_sequence_id"),
                    "side": tr.get("side"),
                    "wall_price": tr.get("current_price") or tr.get("previous_price"),
                    "previous_notional": tr.get("previous_notional"),
                    "current_notional": tr.get("current_notional"),
                    "notional_change_pct": tr.get("notional_change_pct"),
                    "previous_distance_bps": tr.get("previous_distance_bps"),
                    "current_distance_bps": tr.get("current_distance_bps"),
                    "price_change_pct_at_transition": row.get("price_change_pct"),
                    "delta_notional_at_transition": row.get("delta_notional"),
                    "delta_ratio_at_transition": row.get("delta_ratio"),
                    "oi_change_pct_at_transition": row.get("oi_change_pct"),
                    "liquidation_notional_at_transition": row.get("liquidation_notional"),
                    "context_quadrant": row.get("context_quadrant"),
                    "source_bucket_end": bucket_end.isoformat(),
                }
            )

        pending: list[dict[str, Any]] = []
        # Lifecycle size/move/test/break patterns are ONLY emitted from the
        # transition loop below — never again from as-of wall state.

        def add(
            ptype: str,
            *,
            side: str | None = None,
            sequence_id: str | None = None,
            force: bool = False,
            reason: str = "",
            source_transition_ts: datetime | None = None,
            source_transition_type: str | None = None,
            subject_key: str | None = None,
        ) -> None:
            pending.append(
                {
                    "pattern_type": ptype,
                    "side": side or "",
                    "sequence_id": sequence_id,
                    "force": force,
                    "reason": reason,
                    "source_transition_ts": source_transition_ts,
                    "source_transition_type": source_transition_type,
                    "subject_key": subject_key or "",
                }
            )

        # Price/delta divergence (needs trades)
        if has_price and has_trades and price_reg and delta_reg:
            if price_reg == "DOWN" and delta_reg == "POSITIVE":
                add("PRICE_DOWN_DELTA_POSITIVE", reason="price down with positive rolling delta")
            if price_reg == "UP" and delta_reg == "NEGATIVE":
                add("PRICE_UP_DELTA_NEGATIVE", reason="price up with negative rolling delta")
            if price_reg == "FLAT" and delta_reg == "POSITIVE":
                add("PRICE_FLAT_DELTA_POSITIVE", reason="price flat with positive rolling delta")
            if price_reg == "FLAT" and delta_reg == "NEGATIVE":
                add("PRICE_FLAT_DELTA_NEGATIVE", reason="price flat with negative rolling delta")

        # Price/OI
        if has_price and has_oi and price_reg and oi_reg:
            add(f"PRICE_{price_reg}_OI_{oi_reg}", reason="price/oi constellation")

        # Wall imbalance
        imb = _safe_float(row.get("wall_notional_imbalance"))
        if has_walls and imb is not None:
            if imb >= params.wall_imbalance_threshold:
                add("BID_WALL_DOMINANCE", side="bid", reason="bid wall notional dominance")
            elif imb <= -params.wall_imbalance_threshold:
                add("ASK_WALL_DOMINANCE", side="ask", reason="ask wall notional dominance")
            else:
                add("BALANCED_WALL_LIQUIDITY", reason="balanced wall notional")

        # Liquidation clusters
        buy_liq = _safe_float(row.get("buy_liquidation_notional")) or 0.0
        sell_liq = _safe_float(row.get("sell_liquidation_notional")) or 0.0
        liq_n = _safe_float(row.get("liquidation_notional")) or 0.0
        if has_liq and liq_n > 0:
            if buy_liq > sell_liq:
                add("BUY_LIQUIDATION_CLUSTER", reason="buy liquidation notional dominates bar")
            if sell_liq > buy_liq:
                add("SELL_LIQUIDATION_CLUSTER", reason="sell liquidation notional dominates bar")

        # Transition-driven pure lifecycle (single source of truth)
        for tr in new_events:
            sid = str(tr.get("wall_sequence_id") or "")
            side = str(tr.get("side") or "").lower()
            ttype = str(tr.get("transition_type") or "")
            tts = _iso_to_dt(tr["transition_ts"]) if tr.get("transition_ts") else None
            w = walls.get(sid)
            prefix = "BID" if side == "bid" else "ASK" if side == "ask" else None
            if prefix is None:
                continue

            def _life(ptype: str, *, reason: str) -> None:
                add(
                    ptype,
                    side=side,
                    sequence_id=sid,
                    force=True,
                    reason=reason,
                    source_transition_ts=tts,
                    source_transition_type=ttype,
                    subject_key=sid,
                )

            if ttype == "APPEARED":
                _life(f"{prefix}_WALL_APPEARED", reason="wall appeared")
            elif ttype == "GREW":
                _life(f"{prefix}_WALL_GREW", reason="wall grew")
            elif ttype == "SHRANK":
                _life(f"{prefix}_WALL_SHRANK", reason="wall shrank")
            elif ttype == "MOVED_TOWARD_PRICE":
                _life(f"{prefix}_WALL_MOVED_TOWARD_PRICE", reason="moved toward")
            elif ttype == "MOVED_AWAY_FROM_PRICE":
                _life(f"{prefix}_WALL_MOVED_AWAY_FROM_PRICE", reason="moved away")
            elif ttype == "TESTED":
                _life(f"{prefix}_WALL_TESTED", reason="tested as-of")
                if has_liq and liq_n > 0:
                    add(
                        "WALL_TEST_WITH_LIQUIDATIONS",
                        side=side,
                        sequence_id=sid,
                        force=True,
                        reason="test with liquidations",
                        source_transition_ts=tts,
                        source_transition_type=ttype,
                        subject_key=sid,
                    )
                if side == "bid" and delta_reg == "NEGATIVE":
                    add(
                        "BID_WALL_TESTED_WITH_SELL_PRESSURE",
                        side=side,
                        sequence_id=sid,
                        force=True,
                        reason="tested+sell pressure",
                        source_transition_ts=tts,
                        source_transition_type=ttype,
                        subject_key=sid,
                    )
                if side == "ask" and delta_reg == "POSITIVE":
                    add(
                        "ASK_WALL_TESTED_WITH_BUY_PRESSURE",
                        side=side,
                        sequence_id=sid,
                        force=True,
                        reason="tested+buy pressure",
                        source_transition_ts=tts,
                        source_transition_type=ttype,
                        subject_key=sid,
                    )
            elif ttype == "TRADED_THROUGH":
                _life(f"{prefix}_WALL_TRADED_THROUGH", reason="traded through")
            elif ttype == "BROKEN":
                _life(f"{prefix}_WALL_CONFIRMED_BREAK", reason="confirmed break")
                add(
                    f"{prefix}_WALL_FAILURE_CANDIDATE",
                    side=side,
                    sequence_id=sid,
                    force=True,
                    reason="descriptive wall failure candidate",
                    source_transition_ts=tts,
                    source_transition_type=ttype,
                    subject_key=sid,
                )
                if has_liq and liq_n > 0:
                    add(
                        "WALL_BREAK_WITH_LIQUIDATIONS",
                        side=side,
                        sequence_id=sid,
                        force=True,
                        reason="break with liquidations",
                        source_transition_ts=tts,
                        source_transition_type=ttype,
                        subject_key=sid,
                    )
                if side == "bid" and delta_reg == "NEGATIVE":
                    add(
                        "BID_WALL_BREAK_WITH_SELL_PRESSURE",
                        side=side,
                        sequence_id=sid,
                        force=True,
                        reason="break+sell pressure",
                        source_transition_ts=tts,
                        source_transition_type=ttype,
                        subject_key=sid,
                    )
                if side == "ask" and delta_reg == "POSITIVE":
                    add(
                        "ASK_WALL_BREAK_WITH_BUY_PRESSURE",
                        side=side,
                        sequence_id=sid,
                        force=True,
                        reason="break+buy pressure",
                        source_transition_ts=tts,
                        source_transition_type=ttype,
                        subject_key=sid,
                    )
            elif ttype == "DISAPPEARED":
                if w and not w.tested and not w.broken:
                    _life(f"{prefix}_WALL_DISAPPEARED_UNTESTED", reason="disappeared before test")
                    add(
                        f"{prefix}_WALL_PULLING_CANDIDATE",
                        side=side,
                        sequence_id=sid,
                        force=True,
                        reason="Order removal versus execution is unknown.",
                        source_transition_ts=tts,
                        source_transition_type=ttype,
                        subject_key=sid,
                    )
            # PERSISTED does not emit a pure lifecycle row here; PERSISTENT comes
            # only from as-of age/sample conditions below.

        # As-of wall state: composites + PERSISTENT only (never re-emit GREW/SHRANK/…)
        for side, prefix in (("bid", "BID"), ("ask", "ASK")):
            w = _active_near_wall(walls, side=side, params=params, as_of=bucket_end)
            if w is None:
                continue
            age = _wall_age_sec(w, bucket_end) or 0.0
            if age >= params.min_wall_age_sec and w.sample_count >= params.min_wall_samples:
                add(
                    f"{prefix}_WALL_PERSISTENT",
                    side=side,
                    sequence_id=w.sequence_id,
                    reason="persistent near wall",
                    subject_key=w.sequence_id,
                )
            if has_trades and delta_reg == "NEGATIVE" and side == "bid":
                add("BID_WALL_WITH_SELL_PRESSURE", side=side, sequence_id=w.sequence_id, reason="bid wall + sell pressure", subject_key=w.sequence_id)
            if has_trades and delta_reg == "POSITIVE" and side == "bid":
                add("BID_WALL_WITH_BUY_PRESSURE", side=side, sequence_id=w.sequence_id, reason="bid wall + buy pressure", subject_key=w.sequence_id)
            if has_trades and delta_reg == "POSITIVE" and side == "ask":
                add("ASK_WALL_WITH_BUY_PRESSURE", side=side, sequence_id=w.sequence_id, reason="ask wall + buy pressure", subject_key=w.sequence_id)
            if has_trades and delta_reg == "NEGATIVE" and side == "ask":
                add("ASK_WALL_WITH_SELL_PRESSURE", side=side, sequence_id=w.sequence_id, reason="ask wall + sell pressure", subject_key=w.sequence_id)
            nchg = _notional_change_pct(w)
            if (
                nchg is not None
                and nchg >= params.wall_growth_threshold_pct
                and side == "bid"
                and delta_reg == "NEGATIVE"
            ):
                add(
                    "BID_WALL_GROWING_WITH_SELL_PRESSURE",
                    side=side,
                    sequence_id=w.sequence_id,
                    reason="growing bid wall with sell pressure",
                    subject_key=w.sequence_id,
                )
            if (
                nchg is not None
                and nchg >= params.wall_growth_threshold_pct
                and side == "ask"
                and delta_reg == "POSITIVE"
            ):
                add(
                    "ASK_WALL_GROWING_WITH_BUY_PRESSURE",
                    side=side,
                    sequence_id=w.sequence_id,
                    reason="growing ask wall with buy pressure",
                    subject_key=w.sequence_id,
                )
            if age >= params.min_wall_age_sec and side == "bid" and price_reg in {"FLAT", "UP"}:
                add(
                    "BID_WALL_PERSISTENT_PRICE_NOT_FALLING",
                    side=side,
                    sequence_id=w.sequence_id,
                    reason="persistent bid wall while price not falling",
                    subject_key=w.sequence_id,
                )
            if age >= params.min_wall_age_sec and side == "ask" and price_reg in {"FLAT", "DOWN"}:
                add(
                    "ASK_WALL_PERSISTENT_PRICE_NOT_RISING",
                    side=side,
                    sequence_id=w.sequence_id,
                    reason="persistent ask wall while price not rising",
                    subject_key=w.sequence_id,
                )

            # Absorption candidates (descriptive only)
            if (
                age >= params.min_wall_age_sec
                and not w.broken
                and side == "bid"
                and delta_reg == "NEGATIVE"
                and price_reg in {"FLAT", "UP"}
            ):
                add(
                    "BID_ABSORPTION_CANDIDATE",
                    side=side,
                    sequence_id=w.sequence_id,
                    reason="Descriptive candidate only; passive absorption is not proven.",
                    subject_key=w.sequence_id,
                )
            if (
                age >= params.min_wall_age_sec
                and not w.broken
                and side == "ask"
                and delta_reg == "POSITIVE"
                and price_reg in {"FLAT", "DOWN"}
            ):
                add(
                    "ASK_ABSORPTION_CANDIDATE",
                    side=side,
                    sequence_id=w.sequence_id,
                    reason="Descriptive candidate only; passive absorption is not proven.",
                    subject_key=w.sequence_id,
                )

            # Replacement: new appear after recent gone
            if any(tr.get("transition_type") == "APPEARED" and str(tr.get("side")).lower() == side for tr in new_events):
                if side == "bid" and w.price is not None:
                    for gts, gprice in gone_bid[-params.replacement_window_bars :]:
                        if 0 < (bucket_end - gts).total_seconds() <= params.replacement_window_bars * 60 and w.price < gprice:
                            add(
                                "WALL_REPLACEMENT_LOWER",
                                side=side,
                                sequence_id=w.sequence_id,
                                force=True,
                                reason="bid wall replaced lower (no merge/split claim)",
                                subject_key=w.sequence_id,
                            )
                            break
                if side == "ask" and w.price is not None:
                    for gts, gprice in gone_ask[-params.replacement_window_bars :]:
                        if 0 < (bucket_end - gts).total_seconds() <= params.replacement_window_bars * 60 and w.price > gprice:
                            add(
                                "WALL_REPLACEMENT_HIGHER",
                                side=side,
                                sequence_id=w.sequence_id,
                                force=True,
                                reason="ask wall replaced higher (no merge/split claim)",
                                subject_key=w.sequence_id,
                            )
                            break

        # Emit pending with cooldown + defensive candidate_key dedupe
        for item in pending:
            ptype = item["pattern_type"]
            side = item["side"]
            sequence_id = item["sequence_id"]
            force = bool(item["force"])
            reason = item["reason"]
            src_tr_ts_dt = item.get("source_transition_ts")
            src_tr_type = item.get("source_transition_type")
            subject_key = item.get("subject_key") or ""

            if params.output_mode == "lifecycle_only" and PATTERN_FAMILY.get(ptype) != "WALL_LIFECYCLE":
                continue
            if params.output_mode == "composite_only" and PATTERN_FAMILY.get(ptype) == "WALL_LIFECYCLE":
                continue

            tr_ts_key = src_tr_ts_dt.isoformat() if isinstance(src_tr_ts_dt, datetime) else None
            if not deduper.allow(
                pattern_type=ptype,
                segment_id=seg_id,
                side=side or None,
                sequence_id=sequence_id,
                bar_index=bar_index,
                force_event=force,
                transition_ts=tr_ts_key,
            ):
                continue

            w = walls.get(sequence_id) if sequence_id else None
            if w is None and side:
                w = _active_near_wall(walls, side=side, params=params, as_of=bucket_end)

            # data completeness
            family = PATTERN_FAMILY.get(ptype, "UNKNOWN")
            data_complete = True
            if family in {"PRICE_DELTA_DIVERGENCE", "WALL_FLOW", "ABSORPTION_CANDIDATE"} and not has_trades:
                data_complete = False
            if family == "PRICE_OI" and not has_oi:
                data_complete = False
            if family == "LIQUIDATION" and not has_liq:
                data_complete = False
            if family in {"WALL_LIFECYCLE", "WALL_FAILURE_CANDIDATE", "WALL_PULLING_CANDIDATE"} and not (has_walls or sequence_id):
                data_complete = False
            if not has_price:
                data_complete = False

            # causal wall flags as-of pattern_ts (=bucket_end)
            tested = bool(w.tested) if w else bool(row.get("nearest_bid_wall_tested" if side == "bid" else "nearest_ask_wall_tested"))
            broken = bool(w.broken) if w else bool(row.get("nearest_bid_wall_broken" if side == "bid" else "nearest_ask_wall_broken"))
            traded = bool(w.traded_through) if w else False
            if w and w.first_test_ts and w.first_test_ts > bucket_end:
                tested = False
            if w and w.confirmed_break_ts and w.confirmed_break_ts > bucket_end:
                broken = False
            if w and w.first_traded_ts and w.first_traded_ts > bucket_end:
                traded = False

            # integrity: never emit TESTED/BROKEN before transition
            if ptype.endswith("_WALL_TESTED") or ptype.endswith("_TESTED_WITH_SELL_PRESSURE") or ptype.endswith("_TESTED_WITH_BUY_PRESSURE"):
                if w is None or not w.tested or (w.first_test_ts and w.first_test_ts > bucket_end):
                    continue
            if "CONFIRMED_BREAK" in ptype or "FAILURE_CANDIDATE" in ptype or "BREAK_WITH_" in ptype:
                if w is None or not w.broken or (w.confirmed_break_ts and w.confirmed_break_ts > bucket_end):
                    continue

            # Prefer the causal transition that created this lifecycle candidate.
            if isinstance(src_tr_ts_dt, datetime) and src_tr_ts_dt <= bucket_end:
                src_tr_ts = src_tr_ts_dt.isoformat()
            elif w and w.last_transition_ts and w.last_transition_ts <= bucket_end:
                src_tr_ts = w.last_transition_ts.isoformat()
                src_tr_type = w.last_transition_type
            else:
                src_tr_ts = None
                src_tr_type = None

            seq_for_id = sequence_id or (w.sequence_id if w else None)
            ckey = make_candidate_key(
                symbol=symbol,
                segment_id=seg_id,
                timeframe=params.timeframe,
                pattern_type=ptype,
                pattern_ts=bucket_end,
                source_wall_sequence_id=seq_for_id,
                source_transition_type=src_tr_type,
                source_transition_ts=src_tr_ts,
                subject_key=subject_key or seq_for_id,
            )
            if ckey in seen_candidate_keys:
                duplicate_suppressed_box[0] += 1
                continue
            seen_candidate_keys.add(ckey)

            pattern_id = make_pattern_id(
                symbol=symbol,
                segment_id=seg_id,
                timeframe=params.timeframe,
                pattern_type=ptype,
                pattern_ts=bucket_end,
                sequence_id=seq_for_id,
                transition_ts=src_tr_ts_dt if isinstance(src_tr_ts_dt, datetime) else None,
            )
            sample_ts = row.get("wall_sample_ts")
            if sample_ts and _iso_to_dt(sample_ts) > bucket_end:
                errs.append(
                    {
                        "symbol": symbol,
                        "segment_id": seg_id,
                        "pattern_id": pattern_id,
                        "pattern_ts": bucket_end.isoformat(),
                        "error_type": "LOOKAHEAD_WALL_SAMPLE",
                        "error_message": "wall sample after pattern_ts",
                        "details": str(sample_ts),
                    }
                )
                continue

            cand = {
                "symbol": symbol,
                "segment_id": seg_id,
                "timeframe": params.timeframe,
                "pattern_id": pattern_id,
                "pattern_ts": bucket_end.isoformat(),
                "bucket_start": bucket_start.isoformat(),
                "bucket_end": bucket_end.isoformat(),
                "pattern_type": ptype,
                "pattern_family": family,
                "pattern_side": side or (w.side if w else None),
                "pattern_state": "ACTIVE",
                "pattern_version": PATTERN_VERSION,
                "source_wall_sample_ts": sample_ts,
                "source_wall_sequence_id": seq_for_id,
                "source_transition_ts": src_tr_ts,
                "source_transition_type": src_tr_type,
                "wall_price": w.price if w else row.get(f"nearest_{side}_wall_price" if side else None),
                "wall_notional": w.notional if w else None,
                "wall_multiple": row.get(f"nearest_{side}_wall_multiple") if side else None,
                "wall_percentile": row.get(f"nearest_{side}_wall_percentile") if side else None,
                "wall_depth_share": row.get(f"nearest_{side}_wall_depth_share") if side else None,
                "wall_distance_bps": w.distance_bps if w else (row.get(f"nearest_{side}_wall_distance_bps") if side else None),
                "wall_age_sec": _wall_age_sec(w, bucket_end) if w else None,
                "wall_sample_count": w.sample_count if w else None,
                "wall_notional_change_pct": _notional_change_pct(w) if w else None,
                "wall_distance_change_bps": _distance_change_bps(w) if w else None,
                "wall_tested_as_of_pattern": tested,
                "wall_traded_through_as_of_pattern": traded,
                "wall_broken_as_of_pattern": broken,
                "bid_wall_count": row.get("bid_wall_count"),
                "ask_wall_count": row.get("ask_wall_count"),
                "bid_wall_total_notional": row.get("bid_wall_total_notional"),
                "ask_wall_total_notional": row.get("ask_wall_total_notional"),
                "wall_notional_imbalance": row.get("wall_notional_imbalance"),
                "open_price": row.get("open_price"),
                "high_price": row.get("high_price"),
                "low_price": row.get("low_price"),
                "close_price": row.get("close_price"),
                "price_change_pct": row.get("price_change_pct"),
                "rolling_price_change_pct": roll["rolling_price_change_pct"],
                "trade_count": row.get("trade_count"),
                "total_notional": row.get("total_notional"),
                "buy_notional": row.get("buy_notional"),
                "sell_notional": row.get("sell_notional"),
                "delta_notional": row.get("delta_notional"),
                "delta_ratio": row.get("delta_ratio"),
                "rolling_delta_notional": roll["rolling_delta_notional"],
                "rolling_delta_ratio": roll["rolling_delta_ratio"],
                "oi_open": row.get("oi_open"),
                "oi_close": row.get("oi_close"),
                "oi_change_pct": row.get("oi_change_pct"),
                "rolling_oi_change_pct": roll["rolling_oi_change_pct"],
                "liquidation_count": row.get("liquidation_count"),
                "liquidation_notional": row.get("liquidation_notional"),
                "spread_bps_close": row.get("spread_bps_close"),
                "context_quadrant": row.get("context_quadrant"),
                "data_complete": data_complete,
                "wall_data_stale": row.get("wall_data_stale"),
                "candidate_reason": reason,
                "is_trading_signal": False,
            }
            candidates.append(cand)

            feat = {
                "pattern_id": pattern_id,
                "symbol": symbol,
                "segment_id": seg_id,
                "pattern_ts": bucket_end.isoformat(),
                "pattern_type": ptype,
                "pattern_family": family,
                "pattern_side": cand["pattern_side"],
                "price_change_1bar_pct": roll["price_change_1bar_pct"],
                "price_change_3bar_pct": roll["price_change_3bar_pct"],
                "price_change_5bar_pct": roll["price_change_5bar_pct"],
                "range_1bar_pct": roll["range_1bar_pct"],
                "range_5bar_pct": roll["range_5bar_pct"],
                "spread_bps": row.get("spread_bps_close"),
                "spread_change_bps": None,
                "delta_notional_1bar": roll["delta_notional_1bar"],
                "delta_notional_3bar": roll["delta_notional_3bar"],
                "delta_notional_5bar": roll["delta_notional_5bar"],
                "delta_ratio_1bar": roll["delta_ratio_1bar"],
                "delta_ratio_3bar": roll["delta_ratio_3bar"],
                "delta_ratio_5bar": roll["delta_ratio_5bar"],
                "buy_share_1bar": roll["buy_share_1bar"],
                "sell_share_1bar": roll["sell_share_1bar"],
                "oi_change_1bar_pct": roll["oi_change_1bar_pct"],
                "oi_change_3bar_pct": roll["oi_change_3bar_pct"],
                "oi_change_5bar_pct": roll["oi_change_5bar_pct"],
                "price_oi_quadrant": row.get("context_quadrant"),
                "liquidation_count_1bar": roll["liquidation_count_1bar"],
                "liquidation_count_5bar": roll["liquidation_count_5bar"],
                "liquidation_notional_1bar": roll["liquidation_notional_1bar"],
                "liquidation_notional_5bar": roll["liquidation_notional_5bar"],
                "buy_liquidation_notional_5bar": roll["buy_liquidation_notional_5bar"],
                "sell_liquidation_notional_5bar": roll["sell_liquidation_notional_5bar"],
                "nearest_bid_wall_distance_bps": row.get("nearest_bid_wall_distance_bps"),
                "nearest_ask_wall_distance_bps": row.get("nearest_ask_wall_distance_bps"),
                "nearest_bid_wall_multiple": row.get("nearest_bid_wall_multiple"),
                "nearest_ask_wall_multiple": row.get("nearest_ask_wall_multiple"),
                "nearest_bid_wall_depth_share": row.get("nearest_bid_wall_depth_share"),
                "nearest_ask_wall_depth_share": row.get("nearest_ask_wall_depth_share"),
                "nearest_bid_wall_age_sec": row.get("nearest_bid_wall_age_sec"),
                "nearest_ask_wall_age_sec": row.get("nearest_ask_wall_age_sec"),
                "strongest_bid_wall_distance_bps": row.get("strongest_bid_wall_distance_bps"),
                "strongest_ask_wall_distance_bps": row.get("strongest_ask_wall_distance_bps"),
                "strongest_bid_wall_multiple": row.get("strongest_bid_wall_multiple"),
                "strongest_ask_wall_multiple": row.get("strongest_ask_wall_multiple"),
                "bid_wall_total_notional": row.get("bid_wall_total_notional"),
                "ask_wall_total_notional": row.get("ask_wall_total_notional"),
                "wall_notional_imbalance": row.get("wall_notional_imbalance"),
                "active_wall_side": cand["pattern_side"],
                "active_wall_age_sec": cand["wall_age_sec"],
                "active_wall_sample_count": cand["wall_sample_count"],
                "active_wall_notional": cand["wall_notional"],
                "active_wall_notional_change_pct": cand["wall_notional_change_pct"],
                "active_wall_distance_bps": cand["wall_distance_bps"],
                "active_wall_distance_change_bps": cand["wall_distance_change_bps"],
                "active_wall_tested": tested,
                "active_wall_traded_through": traded,
                "active_wall_broken": broken,
                "wall_data_present": row.get("wall_data_present"),
                "wall_data_stale": row.get("wall_data_stale"),
                "data_complete": data_complete,
            }
            features.append(feat)

            # backfill pattern_id on matching transition context rows in this bucket
            for ctx in tr_ctx:
                if (
                    ctx.get("pattern_id") is None
                    and ctx.get("source_bucket_end") == bucket_end.isoformat()
                    and ctx.get("wall_sequence_id") == seq_for_id
                ):
                    ctx["pattern_id"] = pattern_id

    if duplicate_suppressed_box[0]:
        logger.debug(
            "pattern candidates suppressed %s duplicate candidate_key rows for %s/%s",
            duplicate_suppressed_box[0],
            symbol,
            seg_id,
        )


    return candidates, features, tr_ctx, errs


def build_summaries(
    candidates: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    by_sym: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    by_seg: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    by_type: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for c in candidates:
        by_sym[(str(c["symbol"]), str(c["pattern_type"]))].append(c)
        by_seg[(str(c["symbol"]), str(c["segment_id"]), str(c["pattern_type"]))].append(c)
        by_type[str(c["pattern_type"])].append(c)

    def _avg(vals: list[float | None]) -> float | None:
        xs = [v for v in vals if v is not None]
        return sum(xs) / len(xs) if xs else None

    sym_rows = []
    for (sym, ptype), items in sorted(by_sym.items()):
        ts = sorted(str(i["pattern_ts"]) for i in items)
        sym_rows.append(
            {
                "symbol": sym,
                "pattern_type": ptype,
                "pattern_family": PATTERN_FAMILY.get(ptype),
                "candidate_count": len(items),
                "segment_count": len({i["segment_id"] for i in items}),
                "first_pattern_ts": ts[0] if ts else None,
                "last_pattern_ts": ts[-1] if ts else None,
                "bid_count": sum(1 for i in items if i.get("pattern_side") == "bid"),
                "ask_count": sum(1 for i in items if i.get("pattern_side") == "ask"),
                "data_complete_count": sum(1 for i in items if i.get("data_complete")),
                "data_incomplete_count": sum(1 for i in items if not i.get("data_complete")),
                "average_wall_age_sec": _avg([_safe_float(i.get("wall_age_sec")) for i in items]),
                "average_wall_distance_bps": _avg([_safe_float(i.get("wall_distance_bps")) for i in items]),
                "average_wall_multiple": _avg([_safe_float(i.get("wall_multiple")) for i in items]),
                "average_delta_ratio": _avg([_safe_float(i.get("rolling_delta_ratio")) for i in items]),
                "average_oi_change_pct": _avg([_safe_float(i.get("rolling_oi_change_pct")) for i in items]),
                "total_liquidation_notional": float(
                    sum((_safe_float(i.get("liquidation_notional")) or 0.0) for i in items)
                ),
            }
        )

    seg_rows = []
    for (sym, seg, ptype), items in sorted(by_seg.items()):
        ts = sorted(str(i["pattern_ts"]) for i in items)
        seg_rows.append(
            {
                "symbol": sym,
                "segment_id": seg,
                "pattern_type": ptype,
                "candidate_count": len(items),
                "first_pattern_ts": ts[0] if ts else None,
                "last_pattern_ts": ts[-1] if ts else None,
            }
        )

    type_rows = []
    for ptype, items in sorted(by_type.items()):
        ts = sorted(str(i["pattern_ts"]) for i in items)
        symbols = sorted({str(i["symbol"]) for i in items})
        type_rows.append(
            {
                "pattern_type": ptype,
                "pattern_family": PATTERN_FAMILY.get(ptype),
                "candidate_count": len(items),
                "symbol_count": len(symbols),
                "segment_count": len({(i["symbol"], i["segment_id"]) for i in items}),
                "symbols": "|".join(symbols),
                "first_pattern_ts": ts[0] if ts else None,
                "last_pattern_ts": ts[-1] if ts else None,
            }
        )
    return sym_rows, seg_rows, type_rows


def enrich_pattern_timeline(
    timeline_rows: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    by_bucket: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for c in candidates:
        by_bucket[str(c["bucket_end"])].append(c)
    out = []
    for row in timeline_rows:
        bend = str(row.get("bucket_end"))
        items = by_bucket.get(bend, [])
        types = sorted({str(i["pattern_type"]) for i in items})
        fams = sorted({str(i["pattern_family"]) for i in items})
        ids = sorted({str(i["pattern_id"]) for i in items})
        base = dict(row)
        base.update(
            {
                "pattern_count": len(ids),
                "pattern_types": "|".join(types),
                "pattern_families": "|".join(fams),
                "pattern_ids": "|".join(ids),
                "has_wall_lifecycle_pattern": any(f == "WALL_LIFECYCLE" for f in fams),
                "has_price_delta_divergence": any(f == "PRICE_DELTA_DIVERGENCE" for f in fams),
                "has_price_oi_pattern": any(f == "PRICE_OI" for f in fams),
                "has_liquidation_pattern": any(f == "LIQUIDATION" for f in fams),
                "has_absorption_candidate": any(f == "ABSORPTION_CANDIDATE" for f in fams),
                "has_wall_failure_candidate": any(f == "WALL_FAILURE_CANDIDATE" for f in fams),
            }
        )
        out.append(base)
    return out


def check_pattern_integrity(
    *,
    candidates: Sequence[Mapping[str, Any]],
    features: Sequence[Mapping[str, Any]],
    segments: Sequence[ReplaySegment],
    gaps: Sequence[ReplayGap],
) -> dict[str, Any]:
    errs: list[str] = []
    warns: list[str] = []
    ids = [c.get("pattern_id") for c in candidates]
    if len(ids) != len(set(ids)):
        errs.append("duplicate pattern_id")
    if len(features) != len(candidates):
        errs.append("feature_matrix count != candidate count")
    feat_ids = [f.get("pattern_id") for f in features]
    if set(feat_ids) != set(ids):
        errs.append("feature_matrix pattern_id set mismatch")
    seg_by = {s.segment_id: s for s in segments}
    for c in candidates:
        if c.get("is_trading_signal") is not False:
            errs.append(f"is_trading_signal not False {c.get('pattern_id')}")
        for k, v in c.items():
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                errs.append(f"non-finite {k} in {c.get('pattern_id')}")
            sk = str(k).lower()
            if any(sk.startswith(p) or p in sk for p in FORBIDDEN_OUTCOME_COLUMNS):
                errs.append(f"forbidden outcome column {k}")
        seg = seg_by.get(str(c.get("segment_id")))
        if seg is None:
            errs.append(f"unknown segment {c.get('segment_id')}")
            continue
        pts = _iso_to_dt(c["pattern_ts"])
        if pts < _ensure_aware(seg.segment_start_ts) or pts > _ensure_aware(seg.segment_end_ts):
            errs.append(f"pattern_ts outside segment {c.get('pattern_id')}")
        if c.get("source_wall_sample_ts"):
            if _iso_to_dt(c["source_wall_sample_ts"]) > pts:
                errs.append(f"wall sample after pattern_ts {c.get('pattern_id')}")
        if c.get("source_transition_ts"):
            if _iso_to_dt(c["source_transition_ts"]) > pts:
                errs.append(f"transition after pattern_ts {c.get('pattern_id')}")
        if c.get("bucket_end"):
            if _iso_to_dt(c["bucket_end"]) > pts:
                errs.append(f"bucket_end after pattern_ts {c.get('pattern_id')}")
        if _bucket_in_gap(pts, gaps):
            errs.append(f"pattern inside gap {c.get('pattern_id')}")
    for f in features:
        for k in f:
            sk = str(k).lower()
            if any(sk.startswith(p) or p == sk for p in ("target", "win", "loss", "label_profitable")):
                errs.append(f"forbidden outcome column in features {k}")
            if sk.startswith(("return_after", "mfe_", "mae_", "max_profit_", "max_adverse_", "future_")):
                errs.append(f"forbidden outcome column in features {k}")
    return {"ok": len(errs) == 0, "errors": errs, "warnings": warns}


def run_pattern_candidates(
    *,
    symbol: str,
    segments: Sequence[ReplaySegment],
    gaps: Sequence[ReplayGap],
    timelines_with_walls: Mapping[str, Sequence[Mapping[str, Any]]],
    transitions: Sequence[Mapping[str, Any]],
    params: PatternParams,
) -> PatternCandidateResult:
    t0 = time.perf_counter()
    result = PatternCandidateResult(params=params)
    try:
        params = validate_pattern_params(params)
        result.params = params
    except PatternCandidateError as exc:
        result.ok = False
        result.error_message = str(exc)
        result.stats = {
            "pattern_candidates_requested": True,
            "pattern_candidates_ok": False,
            "error_message": str(exc),
        }
        return result

    primary_tf = params.timeframe
    primary_rows = list(timelines_with_walls.get(primary_tf) or [])
    if not primary_rows:
        result.warnings.append(f"no timeline_with_walls for {primary_tf}")
        result.ok = True
        result.stats = _empty_stats(params, runtime=time.perf_counter() - t0)
        return result

    all_cands: list[dict[str, Any]] = []
    all_feats: list[dict[str, Any]] = []
    all_ctx: list[dict[str, Any]] = []
    all_errs: list[dict[str, Any]] = []
    seg_fail = 0
    seg_ok = 0

    replayable = [s for s in segments if s.is_replayable]
    for seg in replayable:
        try:
            cands, feats, ctx, errs = detect_patterns_for_segment(
                symbol=symbol,
                segment=seg,
                timeline_rows=primary_rows,
                transitions=transitions,
                gaps=gaps,
                params=params,
            )
            all_cands.extend(cands)
            all_feats.extend(feats)
            all_ctx.extend(ctx)
            all_errs.extend(errs)
            seg_ok += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception("pattern candidates failed for %s", seg.segment_id)
            seg_fail += 1
            all_errs.append(
                {
                    "symbol": symbol,
                    "segment_id": seg.segment_id,
                    "pattern_id": None,
                    "pattern_ts": None,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "details": "",
                }
            )

    # stable sort
    all_cands.sort(key=lambda r: (str(r["pattern_ts"]), str(r["pattern_type"]), str(r["pattern_id"])))
    id_order = {c["pattern_id"]: i for i, c in enumerate(all_cands)}
    all_feats.sort(key=lambda r: id_order.get(str(r["pattern_id"]), 10**9))

    sym_s, seg_s, type_s = build_summaries(all_cands)
    integ = check_pattern_integrity(
        candidates=all_cands, features=all_feats, segments=segments, gaps=gaps
    )
    for e in integ.get("errors") or []:
        all_errs.append(
            {
                "symbol": symbol,
                "segment_id": None,
                "pattern_id": None,
                "pattern_ts": None,
                "error_type": "INTEGRITY",
                "error_message": e,
                "details": "",
            }
        )

    result.candidates = all_cands
    result.features = all_feats
    result.transition_contexts = all_ctx
    result.summary_by_symbol = sym_s
    result.summary_by_segment = seg_s
    result.summary_by_type = type_s
    result.integrity_errors = all_errs
    result.timelines[primary_tf] = enrich_pattern_timeline(primary_rows, all_cands)
    # optional 5m if present and primary is 1m
    if primary_tf == "1m" and timelines_with_walls.get("5m"):
        # re-run lightweight for 5m only if requested via timeframe — skip dual by default
        pass

    fam_counts = Counter(str(c["pattern_family"]) for c in all_cands)
    result.ok = bool(integ.get("ok")) and seg_fail == 0
    if seg_fail and seg_ok:
        result.ok = False  # decide_phase5 can still mark PARTIAL via has_failures
    result.stats = {
        "pattern_candidates_requested": True,
        "pattern_candidates_ok": bool(integ.get("ok")) and seg_fail == 0,
        "pattern_timeframe": params.timeframe,
        "pattern_lookback_bars": params.lookback_bars,
        "pattern_candidate_count": len(all_cands),
        "pattern_type_count": len({c["pattern_type"] for c in all_cands}),
        "pattern_family_count": len(fam_counts),
        "pattern_symbols_count": len({c["symbol"] for c in all_cands}) if all_cands else 0,
        "pattern_segments_count": len({c["segment_id"] for c in all_cands}),
        "pattern_data_complete_count": sum(1 for c in all_cands if c.get("data_complete")),
        "pattern_data_incomplete_count": sum(1 for c in all_cands if not c.get("data_complete")),
        "pattern_wall_lifecycle_count": fam_counts.get("WALL_LIFECYCLE", 0),
        "pattern_price_delta_count": fam_counts.get("PRICE_DELTA_DIVERGENCE", 0),
        "pattern_price_oi_count": fam_counts.get("PRICE_OI", 0),
        "pattern_wall_flow_count": fam_counts.get("WALL_FLOW", 0),
        "pattern_liquidation_count": fam_counts.get("LIQUIDATION", 0),
        "pattern_absorption_candidate_count": fam_counts.get("ABSORPTION_CANDIDATE", 0),
        "pattern_wall_failure_candidate_count": fam_counts.get("WALL_FAILURE_CANDIDATE", 0),
        "pattern_integrity_error_count": len(all_errs),
        "pattern_segments_ok": seg_ok,
        "pattern_segments_failed": seg_fail,
        "pattern_runtime_sec": time.perf_counter() - t0,
        "integrity_ok": bool(integ.get("ok")),
        "top_pattern_types": [
            {"pattern_type": r["pattern_type"], "count": r["candidate_count"]}
            for r in sorted(type_s, key=lambda x: -int(x["candidate_count"]))[:10]
        ],
    }
    if not integ.get("ok"):
        result.error_message = "; ".join(integ.get("errors") or [])
    return result


def _empty_stats(params: PatternParams, *, runtime: float) -> dict[str, Any]:
    return {
        "pattern_candidates_requested": True,
        "pattern_candidates_ok": True,
        "pattern_timeframe": params.timeframe,
        "pattern_lookback_bars": params.lookback_bars,
        "pattern_candidate_count": 0,
        "pattern_type_count": 0,
        "pattern_family_count": 0,
        "pattern_symbols_count": 0,
        "pattern_segments_count": 0,
        "pattern_data_complete_count": 0,
        "pattern_data_incomplete_count": 0,
        "pattern_wall_lifecycle_count": 0,
        "pattern_price_delta_count": 0,
        "pattern_price_oi_count": 0,
        "pattern_wall_flow_count": 0,
        "pattern_liquidation_count": 0,
        "pattern_absorption_candidate_count": 0,
        "pattern_wall_failure_candidate_count": 0,
        "pattern_integrity_error_count": 0,
        "pattern_segments_ok": 0,
        "pattern_segments_failed": 0,
        "pattern_runtime_sec": runtime,
        "integrity_ok": True,
        "top_pattern_types": [],
    }


CANDIDATE_HEADERS = [
    "symbol", "segment_id", "timeframe", "pattern_id", "pattern_ts", "bucket_start", "bucket_end",
    "pattern_type", "pattern_family", "pattern_side", "pattern_state", "pattern_version",
    "source_wall_sample_ts", "source_wall_sequence_id", "source_transition_ts", "source_transition_type",
    "wall_price", "wall_notional", "wall_multiple", "wall_percentile", "wall_depth_share",
    "wall_distance_bps", "wall_age_sec", "wall_sample_count", "wall_notional_change_pct", "wall_distance_change_bps",
    "wall_tested_as_of_pattern", "wall_traded_through_as_of_pattern", "wall_broken_as_of_pattern",
    "bid_wall_count", "ask_wall_count", "bid_wall_total_notional", "ask_wall_total_notional", "wall_notional_imbalance",
    "open_price", "high_price", "low_price", "close_price", "price_change_pct", "rolling_price_change_pct",
    "trade_count", "total_notional", "buy_notional", "sell_notional", "delta_notional", "delta_ratio",
    "rolling_delta_notional", "rolling_delta_ratio",
    "oi_open", "oi_close", "oi_change_pct", "rolling_oi_change_pct",
    "liquidation_count", "liquidation_notional", "spread_bps_close", "context_quadrant",
    "data_complete", "wall_data_stale", "candidate_reason", "is_trading_signal",
]

FEATURE_HEADERS = [
    "pattern_id", "symbol", "segment_id", "pattern_ts", "pattern_type", "pattern_family", "pattern_side",
    "price_change_1bar_pct", "price_change_3bar_pct", "price_change_5bar_pct", "range_1bar_pct", "range_5bar_pct",
    "spread_bps", "spread_change_bps",
    "delta_notional_1bar", "delta_notional_3bar", "delta_notional_5bar",
    "delta_ratio_1bar", "delta_ratio_3bar", "delta_ratio_5bar", "buy_share_1bar", "sell_share_1bar",
    "oi_change_1bar_pct", "oi_change_3bar_pct", "oi_change_5bar_pct", "price_oi_quadrant",
    "liquidation_count_1bar", "liquidation_count_5bar", "liquidation_notional_1bar", "liquidation_notional_5bar",
    "buy_liquidation_notional_5bar", "sell_liquidation_notional_5bar",
    "nearest_bid_wall_distance_bps", "nearest_ask_wall_distance_bps",
    "nearest_bid_wall_multiple", "nearest_ask_wall_multiple",
    "nearest_bid_wall_depth_share", "nearest_ask_wall_depth_share",
    "nearest_bid_wall_age_sec", "nearest_ask_wall_age_sec",
    "strongest_bid_wall_distance_bps", "strongest_ask_wall_distance_bps",
    "strongest_bid_wall_multiple", "strongest_ask_wall_multiple",
    "bid_wall_total_notional", "ask_wall_total_notional", "wall_notional_imbalance",
    "active_wall_side", "active_wall_age_sec", "active_wall_sample_count", "active_wall_notional",
    "active_wall_notional_change_pct", "active_wall_distance_bps", "active_wall_distance_change_bps",
    "active_wall_tested", "active_wall_traded_through", "active_wall_broken",
    "wall_data_present", "wall_data_stale", "data_complete",
]

TRANSITION_CONTEXT_HEADERS = [
    "pattern_id", "symbol", "segment_id", "transition_ts", "transition_type", "wall_sequence_id", "side",
    "wall_price", "previous_notional", "current_notional", "notional_change_pct",
    "previous_distance_bps", "current_distance_bps",
    "price_change_pct_at_transition", "delta_notional_at_transition", "delta_ratio_at_transition",
    "oi_change_pct_at_transition", "liquidation_notional_at_transition", "context_quadrant", "source_bucket_end",
]

SUMMARY_SYMBOL_HEADERS = [
    "symbol", "pattern_type", "pattern_family", "candidate_count", "segment_count",
    "first_pattern_ts", "last_pattern_ts", "bid_count", "ask_count",
    "data_complete_count", "data_incomplete_count",
    "average_wall_age_sec", "average_wall_distance_bps", "average_wall_multiple",
    "average_delta_ratio", "average_oi_change_pct", "total_liquidation_notional",
]

SUMMARY_SEGMENT_HEADERS = [
    "symbol", "segment_id", "pattern_type", "candidate_count", "first_pattern_ts", "last_pattern_ts",
]

SUMMARY_TYPE_HEADERS = [
    "pattern_type", "pattern_family", "candidate_count", "symbol_count", "segment_count",
    "symbols", "first_pattern_ts", "last_pattern_ts",
]

INTEGRITY_ERROR_HEADERS = [
    "symbol", "segment_id", "pattern_id", "pattern_ts", "error_type", "error_message", "details",
]

PATTERN_TIMELINE_EXTRA_HEADERS = [
    "pattern_count", "pattern_types", "pattern_families", "pattern_ids",
    "has_wall_lifecycle_pattern", "has_price_delta_divergence", "has_price_oi_pattern",
    "has_liquidation_pattern", "has_absorption_candidate", "has_wall_failure_candidate",
]


def phase5_output_files(timeframes: Sequence[str] | None = None) -> list[str]:
    files = list(PHASE5_OUTPUT_FILES)
    for tf in timeframes or ("1m",):
        files.append(f"pattern_timeline_{tf}.csv")
    return files
