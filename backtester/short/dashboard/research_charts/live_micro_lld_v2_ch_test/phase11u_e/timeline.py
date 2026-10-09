"""Compact historical remaining-pool checks over one shared candle index.

Uses RemainingPoolProjection V1 and the Phase 11O range index. It does not
publish pools, query a candle table, or replace the original engine.
"""

from __future__ import annotations

import bisect
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from data.models import ensure_utc
from indicators.live_micro_lld_v2.availability import bar_close

from ..phase11o_candle_ranges import CandleRangeIndex
from ..remaining_pool_projection_v1 import build_fixed_segments, remaining_from_penetration
from ..remaining_pool_projection_v1_config import DEFAULT_SEGMENT_COUNT, STRENGTH_CLASS_COLORS

EPS = 1e-9


class TimelineError(Exception):
    def __init__(self, status: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class PoolObservation:
    pool_id: str
    side: str
    bottom: float
    top: float
    strength: float
    strength_class: str
    remaining_bottom: float
    remaining_top: float
    fully_traversed: bool
    visible_segments: int
    penetration_high: float | None
    penetration_low: float | None
    fixed_edges: tuple[float, float]


@dataclass
class TimelineResult:
    processed: int = 0
    upper_monotonic_failures: int = 0
    lower_monotonic_failures: int = 0
    strength_failures: int = 0
    fixed_segment_failures: int = 0
    causality_failures: int = 0
    active_to_inactive: int = 0
    reappearance: int = 0
    bounds_lifecycle_changes: int = 0
    full_still_active: int = 0
    full_then_inactive: int = 0
    removal_causes: dict = field(default_factory=dict)
    full_examples: list = field(default_factory=list)
    failure_examples: list = field(default_factory=list)
    close_digest: str = ""
    _active: dict = field(default_factory=dict)
    _seen: set = field(default_factory=set)
    _generation: dict = field(default_factory=dict)
    _full_recorded: set = field(default_factory=set)
    _digests: list = field(default_factory=list)
    _last_close: datetime | None = None


def index_from_candles(candles, timeframe: str) -> CandleRangeIndex:
    closes: list[int] = []
    highs: list[float] = []
    lows: list[float] = []
    previous = -1
    for candle in candles:
        close_unix = int(bar_close(ensure_utc(candle.timestamp), timeframe).timestamp())
        if close_unix <= previous:
            raise TimelineError("INVALID_REQUEST", "candle closes are not strictly increasing")
        previous = close_unix
        closes.append(close_unix)
        highs.append(float(candle.high))
        lows.append(float(candle.low))
    return CandleRangeIndex(closes, highs, lows)


def _fixed_edges(bottom: float, top: float, remaining_bottom: float, remaining_top: float, strength_class: str) -> tuple[float, float, int]:
    segments = build_fixed_segments(
        original_bottom=bottom,
        original_top=top,
        remaining_bottom=remaining_bottom,
        remaining_top=remaining_top,
        strength_class=strength_class,
        segment_count=DEFAULT_SEGMENT_COUNT,
    )
    if not segments or segments[0].segment_bottom != float(bottom) or segments[-1].segment_top != float(top):
        raise TimelineError("FIXED_SEGMENT_PARITY", "segment grid left the original bounds")
    if segments[0].display_color != STRENGTH_CLASS_COLORS[strength_class]:
        raise TimelineError("FIXED_SEGMENT_PARITY", "strength color does not match the original class")
    visible = sum(1 for seg in segments if seg.segment_visible and seg.visible_top > seg.visible_bottom)
    return float(segments[0].segment_bottom), float(segments[-1].segment_top), visible


def project_close(index: CandleRangeIndex, pools, decision_close: datetime) -> list[PoolObservation]:
    """Project one close. Later candles stay in the index and are not queried."""
    dc = int(ensure_utc(decision_close).timestamp())
    if not index.close_unix or dc < index.close_unix[0] or dc > index.close_unix[-1]:
        raise TimelineError("INSUFFICIENT_CANDLE_COVERAGE", "decision close is outside the candle index")
    pos = bisect.bisect_left(index.close_unix, dc)
    if pos >= index.n or index.close_unix[pos] != dc:
        raise TimelineError("INSUFFICIENT_CANDLE_COVERAGE", "decision close has no closed candle")
    out: list[PoolObservation] = []
    for row in pools:
        known = int(ensure_utc(row.known_at).timestamp())
        if known > dc:
            raise TimelineError("HISTORICAL_CAUSALITY", f"{row.pool_id} is known after the decision close")
        if row.strength_class not in STRENGTH_CLASS_COLORS:
            raise TimelineError("INVALID_REQUEST", f"unknown strength class {row.strength_class}")
        span = index.eligible_index_span(known, dc)
        if span is None:
            pen_h = pen_l = None
        else:
            if span[1] >= index.n or index.close_unix[span[1]] > dc or index.close_unix[span[0]] <= known:
                raise TimelineError("HISTORICAL_CAUSALITY", "eligible span includes a non-causal candle")
            pen_h, pen_l = index.query(*span)
        side = str(row.side).lower()
        rem_t, rem_b, _frac, full, used_h, used_l = remaining_from_penetration(
            side,
            row.bottom,
            row.top,
            pen_h if side == "upper" else None,
            pen_l if side == "lower" else None,
        )
        edge_b, edge_t, visible = _fixed_edges(row.bottom, row.top, rem_b, rem_t, row.strength_class)
        out.append(
            PoolObservation(
                pool_id=row.pool_id,
                side=side,
                bottom=float(row.bottom),
                top=float(row.top),
                strength=float(row.strength),
                strength_class=row.strength_class,
                remaining_bottom=float(rem_b),
                remaining_top=float(rem_t),
                fully_traversed=bool(full),
                visible_segments=visible,
                penetration_high=used_h,
                penetration_low=used_l,
                fixed_edges=(edge_b, edge_t),
            )
        )
    return out


def _example(close: datetime, obs: PoolObservation, status: str) -> dict:
    return {
        "pool_id": obs.pool_id,
        "side": obs.side,
        "bottom": obs.bottom,
        "top": obs.top,
        "decision_close": close.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "wick_high": obs.penetration_high,
        "wick_low": obs.penetration_low,
        "original_active": status == "ACTIVE",
        "remaining_status": "FULLY_TRAVERSED" if obs.fully_traversed else "PARTIAL",
    }


def observe_close(book: TimelineResult, decision_close: datetime, observations: list[PoolObservation]) -> None:
    dc = ensure_utc(decision_close)
    if book._last_close is not None and dc <= book._last_close:
        raise TimelineError("INVALID_REQUEST", "decision closes are not chronological")
    book._last_close = dc
    current = {obs.pool_id: obs for obs in observations}
    if len(current) != len(observations):
        raise TimelineError("AMBIGUOUS_PUBLICATION", "duplicate pool id in one close")
    for pool_id, previous in list(book._active.items()):
        if pool_id in current:
            continue
        book.active_to_inactive += 1
        book.removal_causes[pool_id] = "UNCLASSIFIED"
        if previous.fully_traversed:
            book.full_then_inactive += 1
        del book._active[pool_id]
    digest_rows = []
    for pool_id, obs in current.items():
        previous = book._active.get(pool_id)
        generation = book._generation.get(pool_id, 0)
        if previous is None:
            if pool_id in book._seen:
                book.reappearance += 1
                generation += 1
                book._generation[pool_id] = generation
            book._seen.add(pool_id)
        else:
            same_life = (
                previous.side == obs.side
                and previous.bottom == obs.bottom
                and previous.top == obs.top
            )
            if not same_life:
                book.bounds_lifecycle_changes += 1
                generation += 1
                book._generation[pool_id] = generation
            else:
                if previous.strength != obs.strength or previous.strength_class != obs.strength_class:
                    book.strength_failures += 1
                    book.failure_examples.append({"close": dc.strftime("%Y-%m-%dT%H:%M:%SZ"), "pool_id": pool_id, "kind": "STRENGTH"})
                if previous.fixed_edges != obs.fixed_edges:
                    book.fixed_segment_failures += 1
                if obs.side == "upper" and (
                    obs.remaining_bottom + EPS < previous.remaining_bottom or (previous.fully_traversed and not obs.fully_traversed)
                ):
                    book.upper_monotonic_failures += 1
                if obs.side == "lower" and (
                    obs.remaining_top > previous.remaining_top + EPS or (previous.fully_traversed and not obs.fully_traversed)
                ):
                    book.lower_monotonic_failures += 1
        if obs.fully_traversed and (pool_id, generation) not in book._full_recorded:
            book.full_still_active += 1
            book._full_recorded.add((pool_id, generation))
            if len(book.full_examples) < 12:
                book.full_examples.append(_example(dc, obs, "ACTIVE"))
        book._active[pool_id] = obs
        digest_rows.append(
            (
                pool_id,
                obs.side,
                obs.bottom,
                obs.top,
                obs.strength_class,
                round(obs.remaining_bottom, 8),
                round(obs.remaining_top, 8),
                obs.fully_traversed,
            )
        )
    book._digests.append(
        hashlib.sha256(json.dumps(sorted(digest_rows), separators=(",", ":")).encode("utf-8")).hexdigest()
    )
    book.processed += 1
    book.close_digest = hashlib.sha256("".join(book._digests).encode("utf-8")).hexdigest()


def removal_classification(book: TimelineResult) -> str:
    if not book.removal_causes:
        return "NOT_APPLICABLE"
    if any(cause != "UNCLASSIFIED" for cause in book.removal_causes.values()):
        return "FAIL"
    return "UNCLASSIFIED"
