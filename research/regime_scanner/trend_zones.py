"""Causal Support/Resistance zone tracker (research Phase B).

Sits *above* ``trend_structure`` — does not modify pivots/BOS/CHoCH/G6/HTF/V6+V2.
Phase B: denoised birth anchors, capped merge expansion, contact episodes with
mutually exclusive outcomes (rejection / breakout / false breakout / ambiguous).

Band width is frozen at birth; later ATR does not reshape existing zones.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import pandas as pd

from research.regime_scanner.trend_structure import MarketStructureState, StructureEvent

ZoneRole = Literal["support", "resistance"]
ZoneState = Literal["forming", "active", "broken", "invalidated"]
WidthMode = Literal["pct_fixed", "atr_mult", "max_pct_atr", "max_pct_atr_cap"]
# Phase-B merge policies (M0–M3). Legacy Phase-A names still accepted as aliases.
MergeMode = Literal[
    "reinforce_only",  # M0
    "expand_cap_01atr",  # M1
    "expand_cap_25pct_birth",  # M2
    "no_expand_separate",  # M3
    "center_in_zone",
    "bands_overlap",
    "mid_atr_distance",
]
EpisodeMode = Literal["bars_outside", "atr_distance", "bars_and_atr"]
ActivationMode = Literal["immediate", "second_episode", "first_rejection", "two_reactions"]
RejectionMode = Literal[
    "close_outside",  # R0
    "close_plus_05atr_3",  # R1
    "two_falling_extremes",  # R2
    "expansion_candle",  # R3
    "score_ge_2",
    "score_ge_3",
    "score_ge_4",
    "failed_break_event",  # legacy
    "move_05atr_6",  # legacy
]
BreakMode = Literal[
    "close_beyond",  # B0
    "close_plus_01atr",  # B1
    "two_closes",  # B2
    "strong_close_expansion",  # B3
    "close_no_reclaim_2",  # B4
    "close_plus_bos_choch",  # legacy
]
ContactOutcome = Literal[
    "REJECTION_CONFIRMED",
    "BREAKOUT_CONFIRMED",
    "FALSE_BREAKOUT",
    "AMBIGUOUS",
    "STILL_INSIDE_ZONE",
    "EXPIRED_WITHOUT_REACTION",
]

# Phase B birth: confirmed pivots (via market structure / synth) + failed breaks only.
RESISTANCE_BIRTH = frozenset({"failed_breakout", "confirmed_pivot_high"})
SUPPORT_BIRTH = frozenset({"failed_breakdown", "confirmed_pivot_low"})

# Reinforce / touch only — never birth.
REINFORCE_EVENTS = frozenset(
    {
        "higher_high",
        "lower_high",
        "equal_high",
        "higher_low",
        "lower_low",
        "equal_low",
        "structure_test_high",
        "structure_test_low",
        "bullish_retest_holds",
        "bearish_retest_holds",
        "retest_fails",
        "liquidity_sweep_high",
        "liquidity_sweep_low",
        "failed_breakout",
        "failed_breakdown",
    }
)
BREAK_CONTEXT = frozenset(
    {"bullish_bos", "bearish_bos", "bullish_choch", "bearish_choch"}
)

# Outcome priority (highest first). Exactly one final outcome per contact episode.
OUTCOME_PRIORITY: tuple[ContactOutcome, ...] = (
    "FALSE_BREAKOUT",
    "BREAKOUT_CONFIRMED",
    "REJECTION_CONFIRMED",
    "STILL_INSIDE_ZONE",
    "EXPIRED_WITHOUT_REACTION",
    "AMBIGUOUS",
)


def _ts(value: object) -> pd.Timestamp:
    t = pd.Timestamp(value)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _iso(value: object | None) -> str | None:
    if value is None:
        return None
    return _ts(value).isoformat()


def _finite(value: object) -> float | None:
    try:
        x = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if x != x:
        return None
    return x


def event_id(ev: StructureEvent) -> str:
    return (
        f"{ev.event_type}|{_iso(ev.event_time)}|{ev.level}|"
        f"{_iso(ev.reference_pivot_time)}|{ev.reference_pivot_price}"
    )


@dataclass(frozen=True)
class ZoneConfig:
    """Phase-B research knobs — audits override; not production-calibrated."""

    timeframe: str = "30m"
    width_mode: WidthMode = "max_pct_atr"
    width_pct: float = 0.10
    width_atr_mult: float = 0.20
    width_atr_cap_pct: float = 0.35
    merge_mode: MergeMode = "expand_cap_01atr"
    merge_atr_mult: float = 0.25
    merge_max_expand_atr: float = 0.10
    merge_max_expand_frac_birth: float = 0.25
    episode_mode: EpisodeMode = "bars_outside"
    episode_min_bars_outside: int = 2
    episode_min_atr_distance: float = 0.5
    activation_mode: ActivationMode = "second_episode"
    rejection_mode: RejectionMode = "close_plus_05atr_3"
    break_mode: BreakMode = "close_no_reclaim_2"
    approach_atr: float = 0.50
    contact_window_bars: int = 3
    false_break_max_bars: int = 3
    expand_on_merge: bool = True  # interpreted through merge_mode caps
    max_zones: int = 2048
    phase: str = "B"
    # RAM guards — per-bar pressure/approach logs explode on multi-month replays
    log_pressure_every_bar: bool = False
    log_approach_every_bar: bool = False
    log_lifecycle: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def variant_key(self) -> str:
        return (
            f"W={self.width_mode}:{self.width_pct}:{self.width_atr_mult}|"
            f"M={self.merge_mode}|A={self.activation_mode}|"
            f"R={self.rejection_mode}|B={self.break_mode}|"
            f"C={self.contact_window_bars}|Ap={self.approach_atr}"
        )


def default_zone_config() -> ZoneConfig:
    return ZoneConfig()


@dataclass
class ContactBar:
    timestamp: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    atr: float | None


@dataclass
class ContactEpisode:
    episode_id: str
    zone_id: str
    role: ZoneRole
    started_at: pd.Timestamp
    ended_at: pd.Timestamp | None = None
    outcome: ContactOutcome | None = None
    outcome_at: pd.Timestamp | None = None
    bars: list[ContactBar] = field(default_factory=list)
    window_bars_seen: int = 0
    approaching: bool = False
    breakout_candidate: bool = False
    breakdown_candidate: bool = False
    breakout_confirmed: bool = False
    breakdown_confirmed: bool = False
    false_breakout: bool = False
    false_breakdown: bool = False
    resistance_rejection_confirmed: bool = False
    support_rejection_confirmed: bool = False
    buying_weakness_confirmed: bool = False
    selling_weakness_confirmed: bool = False
    rejection_confirmed_at: pd.Timestamp | None = None
    breakout_confirmed_at: pd.Timestamp | None = None
    reclaim_timestamp: pd.Timestamp | None = None
    reaction_mfe_atr: float | None = None
    close_distance_atr: float | None = None
    maximum_excursion_outside_atr: float | None = None
    confirmation_delay_bars: int | None = None
    breakout_delay_bars: int | None = None
    reclaim_within_2: bool | None = None
    rejection_score: int = 0
    pending_break_close_at: pd.Timestamp | None = None
    pending_break_close_price: float | None = None
    max_excursion_outside: float = 0.0
    closed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "episode_id": self.episode_id,
            "zone_id": self.zone_id,
            "role": self.role,
            "started_at": _iso(self.started_at),
            "ended_at": _iso(self.ended_at),
            "outcome": self.outcome,
            "outcome_at": _iso(self.outcome_at),
            "window_bars_seen": self.window_bars_seen,
            "n_bars": len(self.bars),
            "breakout_candidate": self.breakout_candidate,
            "breakdown_candidate": self.breakdown_candidate,
            "breakout_confirmed": self.breakout_confirmed,
            "breakdown_confirmed": self.breakdown_confirmed,
            "false_breakout": self.false_breakout,
            "false_breakdown": self.false_breakdown,
            "resistance_rejection_confirmed": self.resistance_rejection_confirmed,
            "support_rejection_confirmed": self.support_rejection_confirmed,
            "buying_weakness_confirmed": self.buying_weakness_confirmed,
            "selling_weakness_confirmed": self.selling_weakness_confirmed,
            "rejection_confirmed_at": _iso(self.rejection_confirmed_at),
            "breakout_confirmed_at": _iso(self.breakout_confirmed_at),
            "reclaim_timestamp": _iso(self.reclaim_timestamp),
            "reaction_mfe_atr": self.reaction_mfe_atr,
            "close_distance_atr": self.close_distance_atr,
            "maximum_excursion_outside_atr": self.maximum_excursion_outside_atr,
            "confirmation_delay_bars": self.confirmation_delay_bars,
            "breakout_delay_bars": self.breakout_delay_bars,
            "reclaim_within_2": self.reclaim_within_2,
            "rejection_score": self.rejection_score,
            "closed": self.closed,
        }


@dataclass
class PriceZone:
    zone_id: str
    timeframe: str
    role: ZoneRole
    state: ZoneState
    lower_bound: float
    upper_bound: float
    center_price: float
    width_abs: float
    width_atr: float | None
    created_at: pd.Timestamp
    birth_lower: float = 0.0
    birth_upper: float = 0.0
    birth_width_abs: float = 0.0
    cumulative_expansion: float = 0.0
    rejected_merge_count: int = 0
    separate_zone_created_from_reject: int = 0
    confirmed_at: pd.Timestamp | None = None
    last_contact_at: pd.Timestamp | None = None
    last_touch_episode_at: pd.Timestamp | None = None
    last_rejection_at: pd.Timestamp | None = None
    broken_at: pd.Timestamp | None = None
    invalidated_at: pd.Timestamp | None = None
    contact_count: int = 0
    touch_episode_count: int = 0
    confirmed_rejection_count: int = 0
    failed_break_count: int = 0
    successful_break_count: int = 0
    source_event_ids: list[str] = field(default_factory=list)
    source_event_types: list[str] = field(default_factory=list)
    source_pivot_timestamps: list[str] = field(default_factory=list)
    source_prices: list[float] = field(default_factory=list)
    last_price_side: str | None = None
    episode_active: bool = False
    rearm_required: bool = False
    previous_role: ZoneRole | None = None
    flip_candidate: bool = False
    retest_contact_at: pd.Timestamp | None = None
    retest_rejection_at: pd.Timestamp | None = None
    birth_atr: float | None = None
    pending_rejection_from: pd.Timestamp | None = None
    pending_rejection_bars: int = 0
    pending_rejection_extreme: float | None = None
    outside_streak: int = 0
    beyond_close_streak: int = 0
    last_bounds_note: str = ""
    # Pressure diagnostics (Phase B audit only)
    time_near_zone: int = 0
    sum_rebound_atr: float = 0.0
    last_rebound_atr: float | None = None
    rebound_count: int = 0
    candle_density_inside: int = 0
    reaction_extremes: list[float] = field(default_factory=list)
    active_contact: ContactEpisode | None = None

    def overlaps_price(self, price: float) -> bool:
        return self.lower_bound <= float(price) <= self.upper_bound

    def overlaps_band(self, lo: float, hi: float) -> bool:
        return not (hi < self.lower_bound or lo > self.upper_bound)

    def contains_mid(self, mid: float) -> bool:
        return self.overlaps_price(mid)

    @property
    def birth_bounds(self) -> tuple[float, float]:
        return (self.birth_lower, self.birth_upper)

    @property
    def current_bounds(self) -> tuple[float, float]:
        return (self.lower_bound, self.upper_bound)

    def to_dict(self) -> dict[str, Any]:
        return {
            "zone_id": self.zone_id,
            "timeframe": self.timeframe,
            "role": self.role,
            "state": self.state,
            "lower_bound": self.lower_bound,
            "upper_bound": self.upper_bound,
            "center_price": self.center_price,
            "width_abs": self.width_abs,
            "width_atr": self.width_atr,
            "birth_lower": self.birth_lower,
            "birth_upper": self.birth_upper,
            "birth_width_abs": self.birth_width_abs,
            "cumulative_expansion": self.cumulative_expansion,
            "rejected_merge_count": self.rejected_merge_count,
            "created_at": _iso(self.created_at),
            "confirmed_at": _iso(self.confirmed_at),
            "last_contact_at": _iso(self.last_contact_at),
            "last_touch_episode_at": _iso(self.last_touch_episode_at),
            "last_rejection_at": _iso(self.last_rejection_at),
            "broken_at": _iso(self.broken_at),
            "invalidated_at": _iso(self.invalidated_at),
            "contact_count": self.contact_count,
            "touch_episode_count": self.touch_episode_count,
            "confirmed_rejection_count": self.confirmed_rejection_count,
            "failed_break_count": self.failed_break_count,
            "successful_break_count": self.successful_break_count,
            "source_event_ids": list(self.source_event_ids),
            "source_event_types": list(self.source_event_types),
            "source_pivot_timestamps": list(self.source_pivot_timestamps),
            "source_prices": list(self.source_prices),
            "last_price_side": self.last_price_side,
            "episode_active": self.episode_active,
            "rearm_required": self.rearm_required,
            "previous_role": self.previous_role,
            "flip_candidate": self.flip_candidate,
            "retest_contact_at": _iso(self.retest_contact_at),
            "retest_rejection_at": _iso(self.retest_rejection_at),
            "birth_atr": self.birth_atr,
            "last_bounds_note": self.last_bounds_note,
            "time_near_zone": self.time_near_zone,
            "sum_rebound_atr": self.sum_rebound_atr,
            "last_rebound_atr": self.last_rebound_atr,
            "rebound_count": self.rebound_count,
            "candle_density_inside": self.candle_density_inside,
        }


@dataclass
class ZoneContext:
    timeframe: str
    decision_time: str
    active_zones: list[dict[str, Any]] = field(default_factory=list)
    forming_zones: list[dict[str, Any]] = field(default_factory=list)
    broken_zones: list[dict[str, Any]] = field(default_factory=list)
    events_this_bar: list[dict[str, Any]] = field(default_factory=list)
    zone_count_active: int = 0
    zone_count_forming: int = 0
    contact_outcomes_this_bar: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_half_width(
    *,
    center: float,
    atr: float | None,
    cfg: ZoneConfig,
) -> tuple[float, float | None]:
    """Return (half_width_abs, width_in_atr). Frozen at call site."""
    pct_hw = abs(center) * float(cfg.width_pct) / 100.0
    atr_hw = None if atr is None or atr <= 0 else float(atr) * float(cfg.width_atr_mult)
    mode = cfg.width_mode
    if mode == "pct_fixed":
        return pct_hw, None if atr is None or atr <= 0 else pct_hw / float(atr)
    if mode == "atr_mult":
        if atr_hw is None:
            return pct_hw, None
        return atr_hw, float(cfg.width_atr_mult)
    if mode == "max_pct_atr":
        if atr_hw is None:
            return pct_hw, None
        hw = max(pct_hw, atr_hw)
        return hw, hw / float(atr)
    # max_pct_atr_cap — W3 style hard upper bound
    cap_hw = abs(center) * float(cfg.width_atr_cap_pct) / 100.0
    if atr_hw is None:
        return min(pct_hw, cap_hw), None
    hw = min(max(pct_hw, atr_hw), cap_hw)
    return hw, hw / float(atr)


def _role_for_birth(event_type: str) -> ZoneRole | None:
    if event_type in RESISTANCE_BIRTH:
        return "resistance"
    if event_type in SUPPORT_BIRTH:
        return "support"
    return None


def _normalize_merge_mode(mode: str) -> str:
    aliases = {
        "center_in_zone": "reinforce_only",
        "bands_overlap": "expand_cap_01atr",
        "mid_atr_distance": "no_expand_separate",
        "M0": "reinforce_only",
        "M1": "expand_cap_01atr",
        "M2": "expand_cap_25pct_birth",
        "M3": "no_expand_separate",
    }
    return aliases.get(mode, mode)


def _candle_fields(candle: dict[str, Any] | pd.Series) -> dict[str, float | pd.Timestamp]:
    row = candle if isinstance(candle, dict) else candle.to_dict()
    o = _finite(row.get("open"))
    h = _finite(row.get("high"))
    l = _finite(row.get("low"))
    c = _finite(row.get("close"))
    ts = row.get("timestamp")
    if o is None or h is None or l is None or c is None or ts is None:
        raise ValueError("candle requires open/high/low/close/timestamp")
    return {"open": o, "high": h, "low": l, "close": c, "timestamp": _ts(ts)}


def _is_expansion_candle(bar: ContactBar, *, bearish: bool) -> bool:
    body = abs(bar.close - bar.open)
    rng = max(bar.high - bar.low, 1e-12)
    if body / rng < 0.55:
        return False
    if bearish:
        return bar.close < bar.open
    return bar.close > bar.open


def _rejection_score(zone: PriceZone, ep: ContactEpisode) -> int:
    """R4 diagnostic score within contact bars (typically ≤ window)."""
    if not ep.bars:
        return 0
    score = 0
    bars = ep.bars
    if zone.role == "resistance":
        if all(b.close <= zone.upper_bound for b in bars):
            score += 1
        highs = [b.high for b in bars]
        if len(highs) >= 2 and sum(1 for i in range(1, len(highs)) if highs[i] <= highs[i - 1]) >= 2:
            score += 1
        elif len(highs) >= 3 and highs[-1] < highs[0] and highs[-2] <= highs[0]:
            score += 1
        closes = [b.close for b in bars]
        if len(closes) >= 2 and sum(1 for i in range(1, len(closes)) if closes[i] < closes[i - 1]) >= 2:
            score += 1
        # upper wick rejection on last contact bar
        last = bars[-1]
        upper_wick = last.high - max(last.open, last.close)
        body = abs(last.close - last.open)
        if upper_wick > body and last.high >= zone.lower_bound:
            score += 1
        if any(_is_expansion_candle(b, bearish=True) for b in bars):
            score += 1
        touch_low = min(b.low for b in bars)
        if last.close < touch_low and len(bars) >= 2:
            score += 1
    else:
        if all(b.close >= zone.lower_bound for b in bars):
            score += 1
        lows = [b.low for b in bars]
        if len(lows) >= 2 and sum(1 for i in range(1, len(lows)) if lows[i] >= lows[i - 1]) >= 2:
            score += 1
        closes = [b.close for b in bars]
        if len(closes) >= 2 and sum(1 for i in range(1, len(closes)) if closes[i] > closes[i - 1]) >= 2:
            score += 1
        last = bars[-1]
        lower_wick = min(last.open, last.close) - last.low
        body = abs(last.close - last.open)
        if lower_wick > body and last.low <= zone.upper_bound:
            score += 1
        if any(_is_expansion_candle(b, bearish=False) for b in bars):
            score += 1
        touch_high = max(b.high for b in bars)
        if last.close > touch_high and len(bars) >= 2:
            score += 1
    return score


class TrendZoneTracker:
    """Causal 30m S/R zone tracker (Phase B)."""

    def __init__(self, cfg: ZoneConfig | None = None) -> None:
        self.cfg = cfg or default_zone_config()
        self.zones: list[PriceZone] = []
        self.contact_episodes: list[ContactEpisode] = []
        self._seen_event_ids: set[str] = set()
        self._seen_pivot_keys: set[str] = set()
        self._prev_high_pivot_ts: str | None = None
        self._prev_low_pivot_ts: str | None = None
        self._seq = 0
        self._ep_seq = 0
        self.separate_zone_created_count = 0
        self.lifecycle_log: list[dict[str, Any]] = []
        self.merge_log: list[dict[str, Any]] = []
        self.merge_reject_log: list[dict[str, Any]] = []
        self.touch_log: list[dict[str, Any]] = []
        self.rejection_log: list[dict[str, Any]] = []
        self.break_log: list[dict[str, Any]] = []
        self.flip_log: list[dict[str, Any]] = []
        self.anchor_log: list[dict[str, Any]] = []
        self.outcome_log: list[dict[str, Any]] = []
        self.false_break_log: list[dict[str, Any]] = []
        self.pressure_log: list[dict[str, Any]] = []
        self.approach_log: list[dict[str, Any]] = []

    def reset_logs(self) -> None:
        for lst in (
            self.lifecycle_log,
            self.merge_log,
            self.merge_reject_log,
            self.touch_log,
            self.rejection_log,
            self.break_log,
            self.flip_log,
            self.anchor_log,
            self.outcome_log,
            self.false_break_log,
            self.pressure_log,
            self.approach_log,
        ):
            lst.clear()

    def _log_life(self, **payload: Any) -> None:
        if self.cfg.log_lifecycle:
            self.lifecycle_log.append(payload)

    def _new_zone_id(self, role: ZoneRole, center: float, when: pd.Timestamp) -> str:
        self._seq += 1
        return f"{self.cfg.timeframe}:{role}:{_iso(when)}:{center:.6f}:{self._seq}"

    def _new_episode_id(self, zone_id: str, when: pd.Timestamp) -> str:
        self._ep_seq += 1
        return f"{zone_id}:ep:{_iso(when)}:{self._ep_seq}"

    def _price_side(self, zone: PriceZone, close: float) -> str:
        if close > zone.upper_bound:
            return "above"
        if close < zone.lower_bound:
            return "below"
        return "inside"

    def _band_contact(self, zone: PriceZone, high: float, low: float) -> bool:
        return not (high < zone.lower_bound or low > zone.upper_bound)

    def _distance_to_zone_atr(self, zone: PriceZone, close: float, atr: float | None) -> float | None:
        if atr is None or atr <= 0:
            return None
        if close > zone.upper_bound:
            return (close - zone.upper_bound) / atr
        if close < zone.lower_bound:
            return (zone.lower_bound - close) / atr
        return 0.0

    def _approaching(self, zone: PriceZone, close: float, atr: float | None) -> bool:
        dist = self._distance_to_zone_atr(zone, close, atr)
        if dist is None:
            return False
        return 0.0 < dist <= float(self.cfg.approach_atr)

    def _find_merge_target(
        self, role: ZoneRole, center: float, lo: float, hi: float, atr: float | None
    ) -> PriceZone | None:
        candidates = [z for z in self.zones if z.role == role and z.state in {"forming", "active"}]
        for z in candidates:
            if z.contains_mid(center) or z.overlaps_band(lo, hi):
                return z
            if atr is not None and atr > 0:
                if abs(z.center_price - center) <= float(self.cfg.merge_atr_mult) * float(atr):
                    return z
        return None

    def _add_source(self, zone: PriceZone, ev: StructureEvent, eid: str) -> None:
        if eid in zone.source_event_ids:
            return
        zone.source_event_ids.append(eid)
        zone.source_event_types.append(ev.event_type)
        if ev.reference_pivot_time is not None:
            pts = _iso(ev.reference_pivot_time)
            if pts and pts not in zone.source_pivot_timestamps:
                zone.source_pivot_timestamps.append(pts)
        if ev.level is not None:
            zone.source_prices.append(float(ev.level))

    def _try_expand(
        self, zone: PriceZone, lo: float, hi: float, atr: float | None, decision_time: pd.Timestamp, reason: str
    ) -> bool:
        """Apply merge expansion per Phase-B policy. Returns True if reinforced (with or without expand)."""
        mode = _normalize_merge_mode(self.cfg.merge_mode)
        old_lo, old_hi = zone.lower_bound, zone.upper_bound
        new_lo, new_hi = min(old_lo, lo), max(old_hi, hi)
        expand_lo = old_lo - new_lo
        expand_hi = new_hi - old_hi
        expand_total = expand_lo + expand_hi

        if expand_total <= 1e-15:
            zone.last_bounds_note = "reinforced_no_expand"
            return True

        if mode == "reinforce_only":
            zone.rejected_merge_count += 1
            self.merge_reject_log.append(
                {
                    "event_available_timestamp": _iso(decision_time),
                    "zone_id": zone.zone_id,
                    "reason": "M0_reinforce_only_no_expand",
                    "anchor_lo": lo,
                    "anchor_hi": hi,
                    "old_lower": old_lo,
                    "old_upper": old_hi,
                }
            )
            zone.last_bounds_note = "reinforced_no_expand"
            return True  # still reinforce sources; bounds unchanged

        if mode == "no_expand_separate":
            # Too far to absorb without expansion → caller should birth separate
            zone.rejected_merge_count += 1
            self.merge_reject_log.append(
                {
                    "event_available_timestamp": _iso(decision_time),
                    "zone_id": zone.zone_id,
                    "reason": "M3_reject_expand_want_separate",
                    "anchor_lo": lo,
                    "anchor_hi": hi,
                    "old_lower": old_lo,
                    "old_upper": old_hi,
                }
            )
            return False

        if mode == "expand_cap_01atr":
            cap = 0.0 if atr is None or atr <= 0 else float(self.cfg.merge_max_expand_atr) * float(atr)
            if expand_total > cap + 1e-15:
                # Clamp expansion to cap proportionally
                if expand_total > 0 and cap > 0:
                    scale = cap / expand_total
                    new_lo = old_lo - expand_lo * scale
                    new_hi = old_hi + expand_hi * scale
                    expand_total = (old_lo - new_lo) + (new_hi - old_hi)
                else:
                    zone.rejected_merge_count += 1
                    self.merge_reject_log.append(
                        {
                            "event_available_timestamp": _iso(decision_time),
                            "zone_id": zone.zone_id,
                            "reason": "M1_expand_exceeds_01atr",
                            "requested_expand": expand_lo + expand_hi,
                            "cap": cap,
                        }
                    )
                    zone.last_bounds_note = "reinforced_no_expand_cap"
                    return True

        if mode == "expand_cap_25pct_birth":
            birth_w = zone.birth_width_abs if zone.birth_width_abs > 0 else zone.width_abs
            max_cum = float(self.cfg.merge_max_expand_frac_birth) * birth_w
            room = max(0.0, max_cum - zone.cumulative_expansion)
            if expand_total > room + 1e-15:
                if room <= 1e-15:
                    zone.rejected_merge_count += 1
                    self.merge_reject_log.append(
                        {
                            "event_available_timestamp": _iso(decision_time),
                            "zone_id": zone.zone_id,
                            "reason": "M2_cumulative_expand_cap",
                            "cumulative_expansion": zone.cumulative_expansion,
                            "max_cum": max_cum,
                        }
                    )
                    zone.last_bounds_note = "reinforced_no_expand_cum_cap"
                    return True
                scale = room / expand_total
                new_lo = old_lo - expand_lo * scale
                new_hi = old_hi + expand_hi * scale
                expand_total = (old_lo - new_lo) + (new_hi - old_hi)

        zone.lower_bound = new_lo
        zone.upper_bound = new_hi
        zone.center_price = 0.5 * (new_lo + new_hi)
        zone.width_abs = new_hi - new_lo
        zone.cumulative_expansion += expand_total
        zone.last_bounds_note = f"expanded_{reason}"
        self.merge_log.append(
            {
                "event_available_timestamp": _iso(decision_time),
                "zone_id": zone.zone_id,
                "merge_mode": mode,
                "old_lower": old_lo,
                "old_upper": old_hi,
                "new_lower": new_lo,
                "new_upper": new_hi,
                "expand_total": expand_total,
                "cumulative_expansion": zone.cumulative_expansion,
                "birth_lower": zone.birth_lower,
                "birth_upper": zone.birth_upper,
                "reason_codes": reason,
            }
        )
        return True

    def _birth_or_merge(
        self,
        *,
        ev: StructureEvent,
        eid: str,
        atr: float | None,
        decision_time: pd.Timestamp,
    ) -> PriceZone | None:
        role = _role_for_birth(ev.event_type)
        if role is None or ev.level is None:
            return None
        center = float(ev.level)
        pivot_key = None
        if ev.reference_pivot_time is not None:
            pivot_key = f"{role}|{_iso(ev.reference_pivot_time)}|{center:.8f}"
            if pivot_key in self._seen_pivot_keys:
                return None
        half, width_atr = compute_half_width(center=center, atr=atr, cfg=self.cfg)
        lo, hi = center - half, center + half
        target = self._find_merge_target(role, center, lo, hi, atr)
        if target is not None:
            absorbed = self._try_expand(target, lo, hi, atr, decision_time, "merge")
            if absorbed:
                self._add_source(target, ev, eid)
                if pivot_key:
                    self._seen_pivot_keys.add(pivot_key)
                self.anchor_log.append(
                    {
                        "event_available_timestamp": _iso(decision_time),
                        "event_id": eid,
                        "event_type": ev.event_type,
                        "role": role,
                        "price": center,
                        "action": "merged_into",
                        "zone_id": target.zone_id,
                        "lower_bound": target.lower_bound,
                        "upper_bound": target.upper_bound,
                        "birth_lower": target.birth_lower,
                        "birth_upper": target.birth_upper,
                        "cumulative_expansion": target.cumulative_expansion,
                    }
                )
                return target
            # M3: create separate zone below
            self.separate_zone_created_count += 1
            target.separate_zone_created_from_reject += 1

        initial_state: ZoneState = "active" if self.cfg.activation_mode == "immediate" else "forming"
        z = PriceZone(
            zone_id=self._new_zone_id(role, center, decision_time),
            timeframe=self.cfg.timeframe,
            role=role,
            state=initial_state,
            lower_bound=lo,
            upper_bound=hi,
            center_price=center,
            width_abs=2.0 * half,
            width_atr=width_atr,
            created_at=decision_time,
            birth_lower=lo,
            birth_upper=hi,
            birth_width_abs=2.0 * half,
            confirmed_at=decision_time if initial_state == "active" else None,
            birth_atr=atr,
            last_bounds_note="born_frozen_width",
        )
        self._add_source(z, ev, eid)
        if pivot_key:
            self._seen_pivot_keys.add(pivot_key)
        self.zones.append(z)
        if len(self.zones) > self.cfg.max_zones:
            self.zones.sort(
                key=lambda x: (0 if x.state in {"invalidated", "broken"} else 1, x.created_at)
            )
            self.zones = self.zones[-self.cfg.max_zones :]
        self.anchor_log.append(
            {
                "event_available_timestamp": _iso(decision_time),
                "event_id": eid,
                "event_type": ev.event_type,
                "role": role,
                "price": center,
                "action": "birth",
                "zone_id": z.zone_id,
                "lower_bound": lo,
                "upper_bound": hi,
                "birth_lower": lo,
                "birth_upper": hi,
                "width_abs": z.width_abs,
                "birth_atr": atr,
            }
        )
        self._log_life(
            event_available_timestamp=_iso(decision_time),
            candle_timestamp=_iso(ev.event_time),
            zone_id=z.zone_id,
            event_kind="birth",
            role=z.role,
            state=z.state,
            lower_bound=lo,
            upper_bound=hi,
            reason_codes=f"birth:{ev.event_type}",
            inputs=f"level={center};atr={atr}",
        )
        return z

    def _maybe_activate(self, zone: PriceZone, decision_time: pd.Timestamp, reason: str) -> None:
        if zone.state != "forming":
            return
        mode = self.cfg.activation_mode
        ok = False
        if mode == "immediate":
            ok = True
        elif mode == "second_episode":
            ok = zone.touch_episode_count >= 2
        elif mode == "first_rejection":
            ok = zone.confirmed_rejection_count >= 1
        elif mode == "two_reactions":
            ok = (zone.confirmed_rejection_count + zone.failed_break_count) >= 2
        if ok:
            zone.state = "active"
            zone.confirmed_at = decision_time
            self._log_life(
                event_available_timestamp=_iso(decision_time),
                candle_timestamp=_iso(decision_time),
                zone_id=zone.zone_id,
                event_kind="activate",
                role=zone.role,
                state=zone.state,
                lower_bound=zone.lower_bound,
                upper_bound=zone.upper_bound,
                reason_codes=reason,
                inputs=f"episodes={zone.touch_episode_count};rej={zone.confirmed_rejection_count}",
            )

    def _rearm_satisfied(
        self,
        zone: PriceZone,
        close: float,
        atr: float | None,
        *,
        outside_streak: int | None = None,
    ) -> bool:
        mode = self.cfg.episode_mode
        dist = self._distance_to_zone_atr(zone, close, atr)
        streak = zone.outside_streak if outside_streak is None else int(outside_streak)
        bars_ok = streak >= int(self.cfg.episode_min_bars_outside)
        dist_ok = dist is not None and dist >= float(self.cfg.episode_min_atr_distance)
        if mode == "bars_outside":
            return bars_ok
        if mode == "atr_distance":
            return bool(dist_ok)
        return bars_ok and bool(dist_ok)

    def _start_contact_episode(
        self, zone: PriceZone, decision_time: pd.Timestamp, bar: ContactBar
    ) -> ContactEpisode:
        ep = ContactEpisode(
            episode_id=self._new_episode_id(zone.zone_id, decision_time),
            zone_id=zone.zone_id,
            role=zone.role,
            started_at=decision_time,
            bars=[bar],
            window_bars_seen=1,
        )
        zone.active_contact = ep
        zone.touch_episode_count += 1
        zone.last_touch_episode_at = decision_time
        zone.episode_active = True
        zone.rearm_required = False
        self.touch_log.append(
            {
                "event_available_timestamp": _iso(decision_time),
                "zone_id": zone.zone_id,
                "episode_id": ep.episode_id,
                "role": zone.role,
                "state": zone.state,
                "lower_bound": zone.lower_bound,
                "upper_bound": zone.upper_bound,
                "reason_codes": "contact_episode_start",
                "touch_episode_count": zone.touch_episode_count,
            }
        )
        self._maybe_activate(zone, decision_time, "activate_on_episode")
        return ep

    def _finalize_outcome(
        self, zone: PriceZone, ep: ContactEpisode, decision_time: pd.Timestamp, outcome: ContactOutcome
    ) -> None:
        if ep.closed:
            return
        ep.closed = True
        ep.outcome = outcome
        ep.outcome_at = decision_time
        ep.ended_at = decision_time
        zone.active_contact = None
        zone.episode_active = False
        zone.rearm_required = True
        self.contact_episodes.append(ep)
        self.outcome_log.append(
            {
                "event_available_timestamp": _iso(decision_time),
                "zone_id": zone.zone_id,
                "episode_id": ep.episode_id,
                "role": zone.role,
                "outcome": outcome,
                "rejection_score": ep.rejection_score,
                "breakout_confirmed": ep.breakout_confirmed or ep.breakdown_confirmed,
                "false_break": ep.false_breakout or ep.false_breakdown,
                "confirmation_delay_bars": ep.confirmation_delay_bars,
                "breakout_delay_bars": ep.breakout_delay_bars,
                "reaction_mfe_atr": ep.reaction_mfe_atr,
                "close_distance_atr": ep.close_distance_atr,
                "maximum_excursion_outside_atr": ep.maximum_excursion_outside_atr,
                "lower_bound": zone.lower_bound,
                "upper_bound": zone.upper_bound,
                "birth_lower": zone.birth_lower,
                "birth_upper": zone.birth_upper,
            }
        )
        self._log_life(
            event_available_timestamp=_iso(decision_time),
            zone_id=zone.zone_id,
            event_kind="contact_outcome",
            role=zone.role,
            state=zone.state,
            lower_bound=zone.lower_bound,
            upper_bound=zone.upper_bound,
            reason_codes=outcome,
            inputs=ep.episode_id,
        )

    def _mark_broken(self, zone: PriceZone, decision_time: pd.Timestamp, reason: str) -> None:
        zone.state = "broken"
        zone.broken_at = decision_time
        zone.successful_break_count += 1
        zone.previous_role = zone.role
        zone.flip_candidate = True
        zone.episode_active = False
        self.break_log.append(
            {
                "event_available_timestamp": _iso(decision_time),
                "zone_id": zone.zone_id,
                "role": zone.role,
                "reason_codes": reason,
                "lower_bound": zone.lower_bound,
                "upper_bound": zone.upper_bound,
            }
        )
        self.flip_log.append(
            {
                "event_available_timestamp": _iso(decision_time),
                "zone_id": zone.zone_id,
                "event_kind": "flip_candidate_after_break",
                "previous_role": zone.previous_role,
                "role": zone.role,
                "reason_codes": reason,
            }
        )

    def _eval_rejection(self, zone: PriceZone, ep: ContactEpisode, atr: float | None) -> bool:
        """Return True if rejection confirmed under configured mode (causal bars only)."""
        # Need a real multi-bar reaction window — never confirm on the birth/first touch bar alone.
        if len(ep.bars) < 2:
            return False
        mode = self.cfg.rejection_mode
        bars = ep.bars
        last = bars[-1]
        score = _rejection_score(zone, ep)
        ep.rejection_score = score

        # Must not currently be in an unreclaimed close-beyond state
        if zone.role == "resistance" and last.close > zone.upper_bound:
            return False
        if zone.role == "support" and last.close < zone.lower_bound:
            return False

        def mfe() -> float | None:
            if atr is None or atr <= 0:
                return None
            if zone.role == "resistance":
                return (zone.lower_bound - min(b.low for b in bars)) / atr
            return (max(b.high for b in bars) - zone.upper_bound) / atr

        no_break = (
            all(b.close <= zone.upper_bound for b in bars)
            if zone.role == "resistance"
            else all(b.close >= zone.lower_bound for b in bars)
        )
        # Expected side after reaction: away from the zone interior
        if zone.role == "resistance":
            on_side = last.close <= zone.upper_bound and last.close < zone.center_price
            moved_away = last.close < bars[0].close or last.low < zone.lower_bound
        else:
            on_side = last.close >= zone.lower_bound and last.close > zone.center_price
            moved_away = last.close > bars[0].close or last.high > zone.upper_bound
        r0 = no_break and on_side and moved_away

        move_ok = False
        if atr is not None and atr > 0:
            if zone.role == "resistance":
                move_ok = (zone.center_price - last.close) / atr >= 0.5 or (
                    zone.lower_bound - min(b.low for b in bars)
                ) / atr >= 0.5
            else:
                move_ok = (last.close - zone.center_price) / atr >= 0.5 or (
                    max(b.high for b in bars) - zone.upper_bound
                ) / atr >= 0.5

        falling_highs = False
        rising_lows = False
        if len(bars) >= 3:
            highs = [b.high for b in bars[-3:]]
            lows = [b.low for b in bars[-3:]]
            falling_highs = highs[1] <= highs[0] and highs[2] < highs[0]
            rising_lows = lows[1] >= lows[0] and lows[2] > lows[0]
        elif len(bars) >= 2:
            falling_highs = bars[-1].high < bars[-2].high
            rising_lows = bars[-1].low > bars[-2].low

        expansion = any(
            _is_expansion_candle(b, bearish=(zone.role == "resistance")) for b in bars
        )

        ok = False
        if mode == "close_outside":
            ok = r0
        elif mode in {"close_plus_05atr_3", "move_05atr_6"}:
            ok = r0 and move_ok
        elif mode == "two_falling_extremes":
            ok = r0 and move_ok and (falling_highs if zone.role == "resistance" else rising_lows)
        elif mode == "expansion_candle":
            ok = r0 and move_ok and expansion
        elif mode == "score_ge_2":
            ok = score >= 2 and no_break
        elif mode == "score_ge_3":
            ok = score >= 3 and no_break
        elif mode == "score_ge_4":
            ok = score >= 4 and no_break
        elif mode == "failed_break_event":
            ok = zone.failed_break_count > 0 and r0
        else:
            ok = r0 and move_ok

        if ok:
            ep.reaction_mfe_atr = mfe()
            ep.confirmation_delay_bars = max(0, len(bars) - 1)
            if zone.role == "resistance":
                ep.resistance_rejection_confirmed = True
                ep.buying_weakness_confirmed = True
            else:
                ep.support_rejection_confirmed = True
                ep.selling_weakness_confirmed = True
            ep.rejection_confirmed_at = last.timestamp
        return ok

    def _eval_breakout_candidate(self, zone: PriceZone, close: float, atr: float | None) -> bool:
        if zone.role == "resistance":
            return close > zone.upper_bound
        return close < zone.lower_bound

    def _break_distance_ok(self, zone: PriceZone, close: float, atr: float | None) -> bool:
        mode = self.cfg.break_mode
        if mode == "close_beyond":
            return True
        need = 0.0 if atr is None or atr <= 0 else 0.10 * float(atr)
        if zone.role == "resistance":
            return close >= zone.upper_bound + need
        return close <= zone.lower_bound - need

    def _strong_expansion_break(self, bar: ContactBar, zone: PriceZone) -> bool:
        if zone.role == "resistance":
            return _is_expansion_candle(bar, bearish=False) and bar.close > zone.upper_bound
        return _is_expansion_candle(bar, bearish=True) and bar.close < zone.lower_bound

    def _process_contact_bar(
        self,
        zone: PriceZone,
        *,
        bar: ContactBar,
        atr: float | None,
        decision_time: pd.Timestamp,
        structure_events: list[StructureEvent],
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        contact = self._band_contact(zone, bar.high, bar.low)
        side = self._price_side(zone, bar.close)
        zone.last_price_side = side
        outside_streak_before = zone.outside_streak

        if self._approaching(zone, bar.close, atr) and zone.state in {"forming", "active"}:
            zone.time_near_zone += 1
            if self.cfg.log_approach_every_bar:
                self.approach_log.append(
                    {
                        "event_available_timestamp": _iso(decision_time),
                        "zone_id": zone.zone_id,
                        "role": zone.role,
                        "close": bar.close,
                        "dist_atr": self._distance_to_zone_atr(zone, bar.close, atr),
                        "reason_codes": "approaching_zone",
                    }
                )

        if not contact and side != "inside":
            zone.outside_streak += 1
        elif contact:
            zone.outside_streak = 0
            zone.candle_density_inside += 1

        # Failed break reinforce
        types = {e.event_type for e in structure_events}
        if zone.role == "resistance" and (
            "failed_breakout" in types or "liquidity_sweep_high" in types
        ):
            if contact or any(
                e.level is not None and zone.overlaps_price(float(e.level))
                for e in structure_events
                if e.event_type in {"failed_breakout", "liquidity_sweep_high"}
            ):
                zone.failed_break_count += 1
                for e in structure_events:
                    if e.event_type in {"failed_breakout", "liquidity_sweep_high"}:
                        self._add_source(zone, e, event_id(e))

        if zone.role == "support" and (
            "failed_breakdown" in types or "liquidity_sweep_low" in types
        ):
            if contact or any(
                e.level is not None and zone.overlaps_price(float(e.level))
                for e in structure_events
                if e.event_type in {"failed_breakdown", "liquidity_sweep_low"}
            ):
                zone.failed_break_count += 1
                for e in structure_events:
                    if e.event_type in {"failed_breakdown", "liquidity_sweep_low"}:
                        self._add_source(zone, e, event_id(e))

        if zone.state == "broken" and zone.flip_candidate:
            if contact:
                zone.contact_count += 1
                zone.last_contact_at = decision_time
                if zone.retest_contact_at is None:
                    zone.retest_contact_at = decision_time
                    self.flip_log.append(
                        {
                            "event_available_timestamp": _iso(decision_time),
                            "zone_id": zone.zone_id,
                            "event_kind": "retest_contact",
                            "previous_role": zone.previous_role,
                            "role": zone.role,
                            "lower_bound": zone.lower_bound,
                            "upper_bound": zone.upper_bound,
                            "reason_codes": "pullback_into_broken_zone",
                        }
                    )
                else:
                    zone.retest_contact_at = decision_time
            return events

        if zone.state not in {"forming", "active"}:
            return events

        ep = zone.active_contact

        # Start / continue contact episode
        if contact:
            zone.contact_count += 1
            zone.last_contact_at = decision_time
            if ep is None or ep.closed:
                can_start = False
                if zone.touch_episode_count == 0 and not zone.rearm_required:
                    can_start = True
                elif zone.rearm_required and self._rearm_satisfied(
                    zone, bar.close, atr, outside_streak=outside_streak_before
                ):
                    can_start = True
                elif zone.touch_episode_count == 0:
                    can_start = True
                if can_start:
                    ep = self._start_contact_episode(zone, decision_time, bar)
                    events.append({"kind": "touch_episode", "zone_id": zone.zone_id})
            else:
                ep.bars.append(bar)
                ep.window_bars_seen += 1
        elif ep is not None and not ep.closed:
            # still in observation window after leaving band
            ep.bars.append(bar)
            ep.window_bars_seen += 1

        if ep is None or ep.closed:
            return events

        # --- Breakout / false-break / rejection evaluation inside episode ---
        close_beyond = self._eval_breakout_candidate(zone, bar.close, atr)
        wick_beyond = (
            (bar.high > zone.upper_bound and zone.role == "resistance")
            or (bar.low < zone.lower_bound and zone.role == "support")
        ) and not close_beyond

        if wick_beyond:
            events.append({"kind": "sweep_or_failed_break_candidate", "zone_id": zone.zone_id})

        if close_beyond:
            if zone.role == "resistance":
                ep.breakout_candidate = True
            else:
                ep.breakdown_candidate = True
            dist = None
            if atr and atr > 0:
                if zone.role == "resistance":
                    dist = (bar.close - zone.upper_bound) / atr
                    ep.max_excursion_outside = max(ep.max_excursion_outside, bar.high - zone.upper_bound)
                else:
                    dist = (zone.lower_bound - bar.close) / atr
                    ep.max_excursion_outside = max(ep.max_excursion_outside, zone.lower_bound - bar.low)
            ep.close_distance_atr = dist
            if ep.pending_break_close_at is None:
                ep.pending_break_close_at = decision_time
                ep.pending_break_close_price = bar.close
            zone.beyond_close_streak += 1
        else:
            # reclaim check
            if ep.pending_break_close_at is not None:
                bars_since = sum(1 for b in ep.bars if b.timestamp >= ep.pending_break_close_at)
                reclaimed = (
                    bar.close <= zone.upper_bound
                    if zone.role == "resistance"
                    else bar.close >= zone.lower_bound
                )
                if reclaimed and bars_since <= int(self.cfg.false_break_max_bars):
                    ep.reclaim_within_2 = bars_since <= 2
                    ep.reclaim_timestamp = decision_time
                    if atr and atr > 0:
                        ep.maximum_excursion_outside_atr = ep.max_excursion_outside / atr
                    if zone.role == "resistance":
                        ep.false_breakout = True
                    else:
                        ep.false_breakdown = True
                    self.false_break_log.append(
                        {
                            "event_available_timestamp": _iso(decision_time),
                            "zone_id": zone.zone_id,
                            "episode_id": ep.episode_id,
                            "role": zone.role,
                            "reclaim_timestamp": _iso(decision_time),
                            "maximum_excursion_outside_atr": ep.maximum_excursion_outside_atr,
                            "reason_codes": "false_break_reclaim",
                        }
                    )
                    self._finalize_outcome(zone, ep, decision_time, "FALSE_BREAKOUT")
                    zone.beyond_close_streak = 0
                    return events
            if not close_beyond:
                zone.beyond_close_streak = 0

        # Confirm breakout by mode
        confirm_break = False
        reason = ""
        mode = self.cfg.break_mode
        if close_beyond and self._break_distance_ok(zone, bar.close, atr):
            if mode == "close_beyond":
                confirm_break, reason = True, "B0_close_beyond"
            elif mode == "close_plus_01atr":
                confirm_break, reason = True, "B1_close_plus_01atr"
            elif mode == "two_closes":
                if zone.beyond_close_streak >= 2:
                    confirm_break, reason = True, "B2_two_closes"
            elif mode == "strong_close_expansion":
                if self._strong_expansion_break(bar, zone):
                    confirm_break, reason = True, "B3_strong_close_expansion"
            elif mode == "close_no_reclaim_2":
                # Candidate only until 2 bars without reclaim — confirm when bars_since>=2 still beyond
                if ep.pending_break_close_at is not None:
                    bars_since = sum(1 for b in ep.bars if b.timestamp >= ep.pending_break_close_at)
                    still_beyond = close_beyond
                    if bars_since >= 2 and still_beyond:
                        confirm_break, reason = True, "B4_close_no_reclaim_2"
                        ep.reclaim_within_2 = False
            elif mode == "close_plus_bos_choch":
                if any(e.event_type in BREAK_CONTEXT for e in structure_events):
                    confirm_break, reason = True, "B_close_plus_bos_choch"

        if confirm_break and not (ep.false_breakout or ep.false_breakdown):
            if zone.role == "resistance":
                ep.breakout_confirmed = True
            else:
                ep.breakdown_confirmed = True
            ep.breakout_confirmed_at = decision_time
            ep.breakout_delay_bars = max(0, len(ep.bars) - 1)
            self._mark_broken(zone, decision_time, reason)
            self._finalize_outcome(zone, ep, decision_time, "BREAKOUT_CONFIRMED")
            return events

        # Rejection (only if no pending unreclaimed breakout candidate under B4 waiting)
        waiting_b4 = (
            mode == "close_no_reclaim_2"
            and ep.pending_break_close_at is not None
            and not (ep.false_breakout or ep.false_breakdown)
            and not (ep.breakout_confirmed or ep.breakdown_confirmed)
        )
        if not waiting_b4 and not close_beyond:
            if self._eval_rejection(zone, ep, atr):
                zone.confirmed_rejection_count += 1
                zone.last_rejection_at = decision_time
                if ep.reaction_mfe_atr is not None:
                    zone.last_rebound_atr = ep.reaction_mfe_atr
                    zone.sum_rebound_atr += ep.reaction_mfe_atr
                    zone.rebound_count += 1
                if zone.role == "resistance":
                    zone.reaction_extremes.append(min(b.low for b in ep.bars))
                else:
                    zone.reaction_extremes.append(max(b.high for b in ep.bars))
                self.rejection_log.append(
                    {
                        "event_available_timestamp": _iso(decision_time),
                        "zone_id": zone.zone_id,
                        "episode_id": ep.episode_id,
                        "role": zone.role,
                        "rejection_score": ep.rejection_score,
                        "reaction_mfe_atr": ep.reaction_mfe_atr,
                        "confirmation_delay_bars": ep.confirmation_delay_bars,
                        "reason_codes": self.cfg.rejection_mode,
                        "buying_weakness_confirmed": ep.buying_weakness_confirmed,
                        "selling_weakness_confirmed": ep.selling_weakness_confirmed,
                    }
                )
                self._maybe_activate(zone, decision_time, "activate_on_rejection")
                self._finalize_outcome(zone, ep, decision_time, "REJECTION_CONFIRMED")
                return events

        # Window expiry classification
        window = int(self.cfg.contact_window_bars)
        if ep.window_bars_seen >= window and not contact:
            if ep.breakout_candidate or ep.breakdown_candidate:
                self._finalize_outcome(zone, ep, decision_time, "AMBIGUOUS")
            else:
                self._finalize_outcome(zone, ep, decision_time, "EXPIRED_WITHOUT_REACTION")
            return events
        if ep.window_bars_seen >= window and contact:
            # Still grinding inside after window
            if ep.breakout_candidate or ep.breakdown_candidate:
                self._finalize_outcome(zone, ep, decision_time, "AMBIGUOUS")
            else:
                # Allow one more bar of observation then still-inside
                if ep.window_bars_seen >= window + 2:
                    self._finalize_outcome(zone, ep, decision_time, "STILL_INSIDE_ZONE")
            return events

        return events

    def update(
        self,
        candle: dict[str, Any] | pd.Series,
        structure_events: list[StructureEvent],
        market_structure: MarketStructureState,
        atr: float | None,
    ) -> ZoneContext:
        fields = _candle_fields(candle)
        decision_time = _ts(fields["timestamp"])
        if isinstance(candle, dict) and candle.get("close_time") is not None:
            decision_time = _ts(candle["close_time"])
        elif hasattr(candle, "get") and candle.get("close_time") is not None:  # type: ignore[union-attr]
            decision_time = _ts(candle.get("close_time"))  # type: ignore[union-attr]

        high = float(fields["high"])
        low = float(fields["low"])
        close = float(fields["close"])
        open_ = float(fields["open"])
        atr_f = _finite(atr)
        bar = ContactBar(
            timestamp=decision_time, open=open_, high=high, low=low, close=close, atr=atr_f
        )
        bar_events: list[dict[str, Any]] = []
        outcomes_before = len(self.outcome_log)

        # Birth only from Phase-B anchors; HH/LH etc. reinforce only
        for ev in structure_events:
            eid = event_id(ev)
            if eid in self._seen_event_ids:
                continue
            self._seen_event_ids.add(eid)

            if ev.event_type in {"failed_breakout", "failed_breakdown"}:
                z = self._birth_or_merge(ev=ev, eid=eid, atr=atr_f, decision_time=decision_time)
                if z is not None:
                    bar_events.append({"kind": "anchor", "zone_id": z.zone_id, "event": ev.event_type})
            elif ev.event_type in REINFORCE_EVENTS and ev.level is not None:
                for z in self.zones:
                    if z.state in {"forming", "active"} and z.overlaps_price(float(ev.level)):
                        self._add_source(z, ev, eid)

            if ev.event_type in BREAK_CONTEXT and ev.level is not None:
                for z in self.zones:
                    if z.overlaps_price(float(ev.level)) or abs(float(ev.level) - z.center_price) <= z.width_abs:
                        self._log_life(
                            event_available_timestamp=_iso(decision_time),
                            candle_timestamp=_iso(ev.event_time),
                            zone_id=z.zone_id,
                            event_kind="bos_choch_context",
                            role=z.role,
                            state=z.state,
                            lower_bound=z.lower_bound,
                            upper_bound=z.upper_bound,
                            reason_codes=ev.event_type,
                            inputs=f"level={ev.level}",
                        )

        # Confirmed pivot highs/lows as primary birth anchors (not HH/LH labels)
        sh = market_structure.last_confirmed_swing_high
        sl = market_structure.last_confirmed_swing_low
        if sh is not None:
            pts = _iso(sh.pivot_timestamp)
            if pts and pts != self._prev_high_pivot_ts:
                synth = StructureEvent(
                    event_type="confirmed_pivot_high",
                    timeframe=self.cfg.timeframe,
                    event_time=decision_time,
                    level=float(sh.price),
                    reference_pivot_time=_ts(sh.pivot_timestamp),
                    reference_pivot_price=float(sh.price),
                    direction="bearish",
                    reason_codes=("confirmed_pivot_high",),
                )
                eid = event_id(synth)
                if eid not in self._seen_event_ids:
                    self._seen_event_ids.add(eid)
                    self._birth_or_merge(ev=synth, eid=eid, atr=atr_f, decision_time=decision_time)
                self._prev_high_pivot_ts = pts
        if sl is not None:
            pts = _iso(sl.pivot_timestamp)
            if pts and pts != self._prev_low_pivot_ts:
                synth = StructureEvent(
                    event_type="confirmed_pivot_low",
                    timeframe=self.cfg.timeframe,
                    event_time=decision_time,
                    level=float(sl.price),
                    reference_pivot_time=_ts(sl.pivot_timestamp),
                    reference_pivot_price=float(sl.price),
                    direction="bullish",
                    reason_codes=("confirmed_pivot_low",),
                )
                eid = event_id(synth)
                if eid not in self._seen_event_ids:
                    self._seen_event_ids.add(eid)
                    self._birth_or_merge(ev=synth, eid=eid, atr=atr_f, decision_time=decision_time)
                self._prev_low_pivot_ts = pts

        for z in list(self.zones):
            evs = self._process_contact_bar(
                z, bar=bar, atr=atr_f, decision_time=decision_time, structure_events=structure_events
            )
            bar_events.extend(evs)

        # Pressure snapshot (diagnostic) — only when explicitly enabled (RAM)
        if self.cfg.log_pressure_every_bar:
            for z in self.zones:
                if z.state not in {"forming", "active", "broken"}:
                    continue
                avg_reb = (z.sum_rebound_atr / z.rebound_count) if z.rebound_count else None
                decay = None
                if z.rebound_count >= 2 and z.last_rebound_atr is not None and avg_reb:
                    decay = z.last_rebound_atr - avg_reb
                desc_highs = None
                asc_lows = None
                if len(z.reaction_extremes) >= 3:
                    xs = z.reaction_extremes[-3:]
                    if z.role == "support":
                        desc_highs = xs[0] >= xs[1] >= xs[2]
                    else:
                        asc_lows = xs[0] <= xs[1] <= xs[2]
                pressure_state = "active_support" if z.role == "support" and z.state == "active" else (
                    "active_resistance" if z.role == "resistance" and z.state == "active" else z.state
                )
                if z.role == "support" and z.state == "active" and z.touch_episode_count >= 2 and (
                    decay is not None and decay < 0
                ):
                    pressure_state = "support_under_pressure"
                if z.active_contact and (z.active_contact.breakdown_candidate or z.active_contact.breakout_candidate):
                    pressure_state = (
                        "breakdown_candidate" if z.role == "support" else "breakout_candidate"
                    )
                if z.state == "broken":
                    pressure_state = (
                        "confirmed_breakdown" if z.previous_role == "support" or z.role == "support" else "confirmed_breakout"
                    )
                self.pressure_log.append(
                    {
                        "event_available_timestamp": _iso(decision_time),
                        "zone_id": z.zone_id,
                        "role": z.role,
                        "state": z.state,
                        "pressure_state": pressure_state,
                        "time_near_zone": z.time_near_zone,
                        "touch_episode_count": z.touch_episode_count,
                        "average_rebound_atr": avg_reb,
                        "last_rebound_atr": z.last_rebound_atr,
                        "rebound_decay": decay,
                        "descending_reaction_highs": desc_highs,
                        "ascending_reaction_lows": asc_lows,
                        "candle_density_inside_zone": z.candle_density_inside,
                        "lower_bound": z.lower_bound,
                        "upper_bound": z.upper_bound,
                        "cumulative_expansion": z.cumulative_expansion,
                    }
                )

        active = [z.to_dict() for z in self.zones if z.state == "active"]
        forming = [z.to_dict() for z in self.zones if z.state == "forming"]
        broken = [z.to_dict() for z in self.zones if z.state == "broken"]
        return ZoneContext(
            timeframe=self.cfg.timeframe,
            decision_time=_iso(decision_time) or "",
            active_zones=active,
            forming_zones=forming,
            broken_zones=broken,
            events_this_bar=bar_events,
            zone_count_active=len(active),
            zone_count_forming=len(forming),
            contact_outcomes_this_bar=self.outcome_log[outcomes_before:],
        )

    def pressure_snapshots(self, decision_time: object | None = None) -> list[dict[str, Any]]:
        """End-of-replay pressure rows (cheap; call once)."""
        when = _iso(decision_time) if decision_time is not None else None
        rows: list[dict[str, Any]] = []
        for z in self.zones:
            avg_reb = (z.sum_rebound_atr / z.rebound_count) if z.rebound_count else None
            decay = None
            if z.rebound_count >= 2 and z.last_rebound_atr is not None and avg_reb:
                decay = z.last_rebound_atr - avg_reb
            desc_highs = None
            asc_lows = None
            if len(z.reaction_extremes) >= 3:
                xs = z.reaction_extremes[-3:]
                if z.role == "support":
                    desc_highs = xs[0] >= xs[1] >= xs[2]
                else:
                    asc_lows = xs[0] <= xs[1] <= xs[2]
            pressure_state = (
                "active_support"
                if z.role == "support" and z.state == "active"
                else (
                    "active_resistance"
                    if z.role == "resistance" and z.state == "active"
                    else z.state
                )
            )
            if z.role == "support" and z.state == "active" and z.touch_episode_count >= 2 and (
                decay is not None and decay < 0
            ):
                pressure_state = "support_under_pressure"
            if z.state == "broken":
                pressure_state = (
                    "confirmed_breakdown"
                    if (z.previous_role == "support" or z.role == "support")
                    else "confirmed_breakout"
                )
            rows.append(
                {
                    "event_available_timestamp": when,
                    "zone_id": z.zone_id,
                    "role": z.role,
                    "state": z.state,
                    "pressure_state": pressure_state,
                    "time_near_zone": z.time_near_zone,
                    "touch_episode_count": z.touch_episode_count,
                    "average_rebound_atr": avg_reb,
                    "last_rebound_atr": z.last_rebound_atr,
                    "rebound_decay": decay,
                    "descending_reaction_highs": desc_highs,
                    "ascending_reaction_lows": asc_lows,
                    "candle_density_inside_zone": z.candle_density_inside,
                    "lower_bound": z.lower_bound,
                    "upper_bound": z.upper_bound,
                    "cumulative_expansion": z.cumulative_expansion,
                }
            )
        return rows


# --- Variant factories (Phase B) ---

def width_variant(name: str, base: ZoneConfig | None = None) -> ZoneConfig:
    cfg = default_zone_config() if base is None else ZoneConfig(**base.to_dict())
    table = {
        "W0": dict(width_mode="pct_fixed", width_pct=0.10),
        "W1": dict(width_mode="pct_fixed", width_pct=0.15),
        "W2": dict(width_mode="max_pct_atr", width_pct=0.10, width_atr_mult=0.20),
        "W3": dict(
            width_mode="max_pct_atr_cap",
            width_pct=0.10,
            width_atr_mult=0.15,
            width_atr_cap_pct=0.30,
        ),
        # legacy aliases kept for old audit imports
        "W4": dict(width_mode="max_pct_atr", width_pct=0.10, width_atr_mult=0.20),
        "W5": dict(
            width_mode="max_pct_atr_cap",
            width_pct=0.15,
            width_atr_mult=0.25,
            width_atr_cap_pct=0.50,
        ),
    }
    return ZoneConfig(**{**cfg.to_dict(), **table[name]})


def merge_variant(name: str, base: ZoneConfig) -> ZoneConfig:
    table = {
        "M0": dict(merge_mode="reinforce_only", expand_on_merge=False),
        "M1": dict(merge_mode="expand_cap_01atr", merge_max_expand_atr=0.10, expand_on_merge=True),
        "M2": dict(
            merge_mode="expand_cap_25pct_birth",
            merge_max_expand_frac_birth=0.25,
            expand_on_merge=True,
        ),
        "M3": dict(merge_mode="no_expand_separate", expand_on_merge=False),
    }
    return ZoneConfig(**{**base.to_dict(), **table[name]})


def episode_variant(name: str, base: ZoneConfig) -> ZoneConfig:
    table = {
        "E0": dict(episode_mode="bars_outside", episode_min_bars_outside=2),
        "E1": dict(episode_mode="atr_distance", episode_min_atr_distance=0.5),
        "E2": dict(
            episode_mode="bars_and_atr",
            episode_min_bars_outside=2,
            episode_min_atr_distance=0.5,
        ),
    }
    return ZoneConfig(**{**base.to_dict(), **table[name]})


def activation_variant(name: str, base: ZoneConfig) -> ZoneConfig:
    table = {
        "A0": dict(activation_mode="immediate"),
        "A1": dict(activation_mode="second_episode"),
        "A2": dict(activation_mode="first_rejection"),
        "A3": dict(activation_mode="two_reactions"),
    }
    return ZoneConfig(**{**base.to_dict(), **table[name]})


def rejection_variant(name: str, base: ZoneConfig) -> ZoneConfig:
    table = {
        "R0": dict(rejection_mode="close_outside"),
        "R1": dict(rejection_mode="close_plus_05atr_3"),
        "R2": dict(rejection_mode="two_falling_extremes"),
        "R3": dict(rejection_mode="expansion_candle"),
        "R4_2": dict(rejection_mode="score_ge_2"),
        "R4_3": dict(rejection_mode="score_ge_3"),
        "R4_4": dict(rejection_mode="score_ge_4"),
    }
    return ZoneConfig(**{**base.to_dict(), **table[name]})


def break_variant(name: str, base: ZoneConfig) -> ZoneConfig:
    table = {
        "B0": dict(break_mode="close_beyond"),
        "B1": dict(break_mode="close_plus_01atr"),
        "B2": dict(break_mode="two_closes"),
        "B3": dict(break_mode="strong_close_expansion"),
        "B4": dict(break_mode="close_no_reclaim_2"),
    }
    return ZoneConfig(**{**base.to_dict(), **table[name]})


def contact_window_variant(name: str, base: ZoneConfig) -> ZoneConfig:
    table = {"C1": dict(contact_window_bars=2), "C2": dict(contact_window_bars=3), "C3": dict(contact_window_bars=4)}
    return ZoneConfig(**{**base.to_dict(), **table[name]})


__all__ = [
    "ZoneConfig",
    "PriceZone",
    "ZoneContext",
    "ContactEpisode",
    "ContactOutcome",
    "TrendZoneTracker",
    "compute_half_width",
    "event_id",
    "default_zone_config",
    "width_variant",
    "merge_variant",
    "episode_variant",
    "activation_variant",
    "rejection_variant",
    "break_variant",
    "contact_window_variant",
    "OUTCOME_PRIORITY",
]
