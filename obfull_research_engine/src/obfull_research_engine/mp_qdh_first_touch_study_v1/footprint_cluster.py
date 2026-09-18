"""Canonical FootprintClusterEvent from already-attributed public trade IDs.

View-only: does not re-query trades, does not add hits to QDH, and leaves
normalized IE / absorption / vacuum as NOT_CALIBRATED.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.wall_flow_attribution import (
    WallFlowEvent as AttributionIntervalEvent,
)
from obfull_research_engine.timeparse import format_utc_z

from . import ABSORPTION_RATIO_STATUS, VACUUM_SCORE_STATUS

NORMALIZED_IE_STATUS = "NOT_CALIBRATED"


@dataclass(frozen=True)
class FootprintClusterEvent:
    cluster_id: str
    event_id: str
    episode_id: str
    wall_id: str
    wall_side: str
    wall_price: float
    band_low: float
    band_high: float
    attack_direction: str
    exchange_start: str
    exchange_end: str
    max_input_available_at: str
    computed_at: str
    replay_epoch: int | None
    trade_count: int
    unique_trade_count: int
    trade_ids_hash: str
    buy_qty: float
    sell_qty: float
    buy_notional: float
    sell_notional: float
    signed_delta_notional: float
    attributed_hit_qty: float
    attributed_hit_notional: float
    opposite_flow_qty: float
    interarrival_p50_ms: float | None
    interarrival_p90_ms: float | None
    attack_rate_qty_per_s: float | None
    attack_rate_notional_per_s: float | None
    microprice_before: float | None
    microprice_after: float | None
    progress_ticks: float | None
    progress_bps: float | None
    impact_efficiency: float | None
    impact_efficiency_status: str
    normalized_impact_efficiency: None
    normalized_impact_efficiency_status: str
    absorption_ratio: None
    absorption_ratio_status: str
    vacuum_score: None
    vacuum_score_status: str
    flow_attribution_confidence: str | None
    availability_confidence: str | None
    coverage_ok: bool
    look_ahead: bool
    attributed_trade_ids: tuple[str, ...]
    qdh_hit_trade_ids_hash: str
    adds_to_qdh_hits: bool

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["attributed_trade_ids"] = list(self.attributed_trade_ids)
        return d


REQUIRED_FOOTPRINT_FIELDS: tuple[str, ...] = tuple(f.name for f in fields(FootprintClusterEvent))


def _as_dt(x: Any) -> datetime:
    if isinstance(x, datetime):
        return x if x.tzinfo else x.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(x).replace("Z", "+00:00"))


def deterministic_trade_ids_hash(trade_ids: Sequence[str]) -> str:
    uniq = sorted({str(t) for t in trade_ids})
    blob = "\n".join(uniq).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _percentile(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return float(sorted_vals[f])
    return float(sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f))


def collect_qdh_attributed_trade_ids(
    band_events: Iterable[AttributionIntervalEvent],
    *,
    decision_at: datetime,
) -> list[str]:
    """Same trade IDs that feed QDH hit mass (pre-decision, non-INVALID)."""
    decision_at = _as_dt(decision_at)
    out: list[str] = []
    seen: set[str] = set()
    for ev in band_events:
        if ev.attribution_confidence == "INVALID":
            continue
        end = _as_dt(ev.interval_end_exchange_time)
        if end > decision_at:
            continue
        for tid in ev.attributed_trade_ids or []:
            s = str(tid)
            if s in seen:
                continue
            seen.add(s)
            out.append(s)
    return out


def build_footprint_cluster_event(
    *,
    event_id: str,
    episode_id: str,
    wall_id: str,
    wall_side: str,
    wall_price: float,
    band_low: float,
    band_high: float,
    band_events: Iterable[AttributionIntervalEvent],
    decision_at: datetime,
    touch_at: datetime,
    coverage_ok: bool,
    flow_attribution_confidence: str | None,
    availability_confidence: str | None,
    progress_bps: float | None = None,
    attributed_hit_notional_override: float | None = None,
    microprice_before: float | None = None,
    microprice_after: float | None = None,
    computed_at: datetime | None = None,
) -> FootprintClusterEvent:
    decision_at = _as_dt(decision_at)
    touch_at = _as_dt(touch_at)
    computed = computed_at or datetime.now(tz=timezone.utc)

    events = [
        ev
        for ev in band_events
        if ev.attribution_confidence != "INVALID"
        and _as_dt(ev.interval_end_exchange_time) <= decision_at
    ]
    trade_ids = collect_qdh_attributed_trade_ids(events, decision_at=decision_at)
    # enforce uniqueness already in collector
    assert len(trade_ids) == len(set(trade_ids))
    tid_hash = deterministic_trade_ids_hash(trade_ids)

    hit_qty = sum(float(ev.attributed_hit_qty) for ev in events)
    hit_notional = (
        float(attributed_hit_notional_override)
        if attributed_hit_notional_override is not None
        else sum(float(ev.attributed_hit_notional) for ev in events)
    )
    side = str(wall_side).lower()
    if side == "ask":
        buy_qty, sell_qty = hit_qty, 0.0
        buy_notional, sell_notional = hit_notional, 0.0
        attack_direction = "BUY_ATTACK_ASK"
    elif side == "bid":
        buy_qty, sell_qty = 0.0, hit_qty
        buy_notional, sell_notional = 0.0, hit_notional
        attack_direction = "SELL_ATTACK_BID"
    else:
        raise ValueError(f"unsupported wall_side={wall_side}")

    ends = sorted(_as_dt(ev.interval_end_exchange_time) for ev in events) if events else [touch_at]
    starts = sorted(_as_dt(ev.interval_start_exchange_time) for ev in events) if events else [touch_at]
    exchange_start = starts[0]
    exchange_end = ends[-1]
    max_avail = max((_as_dt(ev.attribution_available_at) for ev in events), default=decision_at)
    epochs = {ev.replay_epoch for ev in events if ev.replay_epoch is not None}
    replay_epoch = next(iter(epochs)) if len(epochs) == 1 else (None if not epochs else None)
    look_ahead = any(bool(ev.look_ahead) for ev in events)

    # Interarrival from interval end times with hits (proxy pacing; no second trade query)
    hit_ends = sorted(
        _as_dt(ev.interval_end_exchange_time) for ev in events if float(ev.attributed_hit_qty) > 1e-15
    )
    gaps_ms = [
        (hit_ends[i] - hit_ends[i - 1]).total_seconds() * 1000.0 for i in range(1, len(hit_ends))
    ]
    gaps_sorted = sorted(gaps_ms)
    p50 = _percentile(gaps_sorted, 0.50)
    p90 = _percentile(gaps_sorted, 0.90)

    dur_s = max((exchange_end - exchange_start).total_seconds(), 0.0)
    rate_qty = (hit_qty / dur_s) if dur_s > 0 else None
    rate_notional = (hit_notional / dur_s) if dur_s > 0 else None

    if hit_notional > 0.0 and progress_bps is not None:
        ie = float(progress_bps) / (hit_notional / 1_000_000.0)
        ie_status = "OK"
    else:
        ie = None
        ie_status = "NOT_AVAILABLE"

    confs = [str(ev.attribution_confidence).upper() for ev in events]
    worst = "HIGH"
    for c in confs:
        if c == "LOW":
            worst = "LOW"
        elif c == "MEDIUM" and worst == "HIGH":
            worst = "MEDIUM"
    if flow_attribution_confidence:
        worst = str(flow_attribution_confidence)

    cluster_id = hashlib.sha256(
        f"{event_id}|{wall_id}|{tid_hash}|{format_utc_z(exchange_start)}|{format_utc_z(exchange_end)}".encode()
    ).hexdigest()[:24]

    progress_ticks = None
    if progress_bps is not None:
        # informational only; tick size not re-derived here
        progress_ticks = None

    return FootprintClusterEvent(
        cluster_id=cluster_id,
        event_id=str(event_id),
        episode_id=str(episode_id or ""),
        wall_id=str(wall_id),
        wall_side=str(wall_side),
        wall_price=float(wall_price),
        band_low=float(band_low),
        band_high=float(band_high),
        attack_direction=attack_direction,
        exchange_start=format_utc_z(exchange_start),
        exchange_end=format_utc_z(exchange_end),
        max_input_available_at=format_utc_z(max_avail),
        computed_at=format_utc_z(computed),
        replay_epoch=replay_epoch,
        trade_count=len(trade_ids),
        unique_trade_count=len(trade_ids),
        trade_ids_hash=tid_hash,
        buy_qty=float(buy_qty),
        sell_qty=float(sell_qty),
        buy_notional=float(buy_notional),
        sell_notional=float(sell_notional),
        signed_delta_notional=float(buy_notional - sell_notional),
        attributed_hit_qty=float(hit_qty),
        attributed_hit_notional=float(hit_notional),
        opposite_flow_qty=0.0,
        interarrival_p50_ms=p50,
        interarrival_p90_ms=p90,
        attack_rate_qty_per_s=rate_qty,
        attack_rate_notional_per_s=rate_notional,
        microprice_before=microprice_before,
        microprice_after=microprice_after,
        progress_ticks=progress_ticks,
        progress_bps=progress_bps,
        impact_efficiency=ie,
        impact_efficiency_status=ie_status,
        normalized_impact_efficiency=None,
        normalized_impact_efficiency_status=NORMALIZED_IE_STATUS,
        absorption_ratio=None,
        absorption_ratio_status=ABSORPTION_RATIO_STATUS,
        vacuum_score=None,
        vacuum_score_status=VACUUM_SCORE_STATUS,
        flow_attribution_confidence=worst,
        availability_confidence=availability_confidence,
        coverage_ok=bool(coverage_ok),
        look_ahead=bool(look_ahead),
        attributed_trade_ids=tuple(trade_ids),
        qdh_hit_trade_ids_hash=tid_hash,
        adds_to_qdh_hits=False,
    )


FOOTPRINT_SCHEMA_VERSION = "footprint_cluster_event_v1"
