"""Canonical typed WallFlow timeline view over existing attribution + 100ms flow.

Does not re-attribute trades or re-run a second replay engine. Emits masterplan
event types as a deterministic, serializable view of already-computed inputs.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Literal

from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.wall_flow_attribution import (
    WallFlowEvent as AttributionIntervalEvent,
)
from obfull_research_engine.timeparse import format_utc_z

TypedEventType = Literal[
    "ZONE_AVAILABLE",
    "ZONE_FIRST_TOUCH",
    "WALL_VISIBLE",
    "WALL_OBSERVATION_AT_ZONE_TOUCH",
    "WALL_FIRST_TOUCH",
    "BOOK_DECREASE",
    "WALL_REFILL",
    "WALL_PULL",
    "AGGRESSOR_TRADE",
    "WALL_MOVE",
    "DECISION",
]

TYPED_EVENT_TYPES: tuple[str, ...] = (
    "ZONE_AVAILABLE",
    "ZONE_FIRST_TOUCH",
    "WALL_VISIBLE",
    "WALL_OBSERVATION_AT_ZONE_TOUCH",
    "WALL_FIRST_TOUCH",
    "BOOK_DECREASE",
    "WALL_REFILL",
    "WALL_PULL",
    "AGGRESSOR_TRADE",
    "WALL_MOVE",
    "DECISION",
)

PHASES: tuple[str, ...] = ("PRE_TOUCH", "TOUCH_TO_TRIGGER", "POST_TRIGGER_FORENSIC", "AT_EVENT")


@dataclass(frozen=True)
class TypedWallFlowEvent:
    """Masterplan-shaped timeline row (canonical research view)."""

    event_id: str
    episode_id: str
    symbol: str
    zone_id: str
    wall_id: str
    wall_side: str
    wall_price: float
    defended_band_low: float
    defended_band_high: float
    event_type: str
    exchange_event_time: str
    collector_received_at: str | None
    event_available_at: str
    receive_time_is_proxy: bool
    book_sequence: int | None
    replay_epoch: int | None
    price: float | None
    size_base: float | None
    notional_usdt: float | None
    aggressor_side: str | None
    source_record_id: str | None
    flow_attribution_confidence: str | None
    availability_confidence: str | None
    coverage_ok: bool
    look_ahead: bool
    phase: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


REQUIRED_TYPED_FIELDS: tuple[str, ...] = tuple(f.name for f in fields(TypedWallFlowEvent))


def _as_dt(x: Any) -> datetime:
    if isinstance(x, datetime):
        return x if x.tzinfo else x.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(x).replace("Z", "+00:00"))


def _phase(ts: datetime, touch: datetime, decision: datetime) -> str:
    if ts < touch:
        return "PRE_TOUCH"
    if ts <= decision:
        return "TOUCH_TO_TRIGGER"
    return "POST_TRIGGER_FORENSIC"


def _row(
    *,
    event_id: str,
    episode_id: str,
    symbol: str,
    zone_id: str,
    wall_id: str,
    wall_side: str,
    wall_price: float,
    band_low: float,
    band_high: float,
    event_type: str,
    exchange_event_time: datetime,
    event_available_at: datetime,
    collector_received_at: datetime | None,
    receive_time_is_proxy: bool,
    book_sequence: int | None,
    replay_epoch: int | None,
    price: float | None,
    size_base: float | None,
    notional_usdt: float | None,
    aggressor_side: str | None,
    source_record_id: str | None,
    flow_attribution_confidence: str | None,
    availability_confidence: str | None,
    coverage_ok: bool,
    look_ahead: bool,
    touch_at: datetime,
    decision_at: datetime,
    force_phase: str | None = None,
) -> TypedWallFlowEvent:
    if event_type not in TYPED_EVENT_TYPES:
        raise ValueError(f"invalid event_type={event_type}")
    phase = force_phase or _phase(exchange_event_time, touch_at, decision_at)
    return TypedWallFlowEvent(
        event_id=str(event_id),
        episode_id=str(episode_id or ""),
        symbol=str(symbol or "BTCUSDT"),
        zone_id=str(zone_id or ""),
        wall_id=str(wall_id),
        wall_side=str(wall_side),
        wall_price=float(wall_price),
        defended_band_low=float(band_low),
        defended_band_high=float(band_high),
        event_type=event_type,
        exchange_event_time=format_utc_z(exchange_event_time),
        collector_received_at=format_utc_z(collector_received_at) if collector_received_at else None,
        event_available_at=format_utc_z(event_available_at),
        receive_time_is_proxy=bool(receive_time_is_proxy),
        book_sequence=book_sequence,
        replay_epoch=replay_epoch,
        price=price,
        size_base=size_base,
        notional_usdt=notional_usdt,
        aggressor_side=aggressor_side,
        source_record_id=source_record_id,
        flow_attribution_confidence=flow_attribution_confidence,
        availability_confidence=availability_confidence,
        coverage_ok=bool(coverage_ok),
        look_ahead=bool(look_ahead),
        phase=phase,
    )


def iter_typed_wall_flow_events(
    *,
    event_id: str,
    episode_id: str,
    symbol: str,
    zone_id: str,
    wall_id: str,
    wall_side: str,
    wall_price: float,
    band_low: float,
    band_high: float,
    touch_at: datetime,
    decision_at: datetime,
    zone_available_at: datetime | None,
    wall_visible_at: datetime | None,
    band_events: Iterable[AttributionIntervalEvent],
    flow_100ms: list[dict[str, Any]] | None,
    wall_move_events: list[dict[str, Any]] | None,
    coverage_ok: bool,
    flow_attribution_confidence: str | None,
    availability_confidence: str | None,
    receive_time_is_proxy: bool = True,
    include_post_decision: bool = False,
) -> Iterator[TypedWallFlowEvent]:
    """Yield typed events causally ordered; default excludes post-decision mass rows."""
    touch_at = _as_dt(touch_at)
    decision_at = _as_dt(decision_at)
    za = _as_dt(zone_available_at) if zone_available_at is not None else touch_at
    yield _row(
        event_id=event_id,
        episode_id=episode_id,
        symbol=symbol,
        zone_id=zone_id,
        wall_id=wall_id,
        wall_side=wall_side,
        wall_price=wall_price,
        band_low=band_low,
        band_high=band_high,
        event_type="ZONE_AVAILABLE",
        exchange_event_time=za,
        event_available_at=za,
        collector_received_at=None,
        receive_time_is_proxy=True,
        book_sequence=None,
        replay_epoch=None,
        price=wall_price,
        size_base=None,
        notional_usdt=None,
        aggressor_side=None,
        source_record_id=None,
        flow_attribution_confidence=flow_attribution_confidence,
        availability_confidence=availability_confidence,
        coverage_ok=coverage_ok,
        look_ahead=False,
        touch_at=touch_at,
        decision_at=decision_at,
        force_phase="AT_EVENT",
    )
    yield _row(
        event_id=event_id,
        episode_id=episode_id,
        symbol=symbol,
        zone_id=zone_id,
        wall_id=wall_id,
        wall_side=wall_side,
        wall_price=wall_price,
        band_low=band_low,
        band_high=band_high,
        event_type="ZONE_FIRST_TOUCH",
        exchange_event_time=touch_at,
        event_available_at=touch_at,
        collector_received_at=None,
        receive_time_is_proxy=receive_time_is_proxy,
        book_sequence=None,
        replay_epoch=None,
        price=wall_price,
        size_base=None,
        notional_usdt=None,
        aggressor_side=None,
        source_record_id=None,
        flow_attribution_confidence=flow_attribution_confidence,
        availability_confidence=availability_confidence,
        coverage_ok=coverage_ok,
        look_ahead=False,
        touch_at=touch_at,
        decision_at=decision_at,
        force_phase="AT_EVENT",
    )
    if wall_visible_at is not None:
        wv = _as_dt(wall_visible_at)
        yield _row(
            event_id=event_id,
            episode_id=episode_id,
            symbol=symbol,
            zone_id=zone_id,
            wall_id=wall_id,
            wall_side=wall_side,
            wall_price=wall_price,
            band_low=band_low,
            band_high=band_high,
            event_type="WALL_VISIBLE",
            exchange_event_time=wv,
            event_available_at=wv,
            collector_received_at=None,
            receive_time_is_proxy=receive_time_is_proxy,
            book_sequence=None,
            replay_epoch=None,
            price=wall_price,
            size_base=None,
            notional_usdt=None,
            aggressor_side=None,
            source_record_id=None,
            flow_attribution_confidence=flow_attribution_confidence,
            availability_confidence=availability_confidence,
            coverage_ok=coverage_ok,
            look_ahead=False,
            touch_at=touch_at,
            decision_at=decision_at,
        )

    # Observation at zone touch from nearest pre/at-touch attribution interval
    touch_obs: AttributionIntervalEvent | None = None
    for ev in band_events:
        end = _as_dt(ev.interval_end_exchange_time)
        if end <= touch_at:
            touch_obs = ev
        elif touch_obs is None and end >= touch_at:
            touch_obs = ev
            break
    if touch_obs is not None:
        yield _row(
            event_id=event_id,
            episode_id=episode_id or touch_obs.episode_id,
            symbol=symbol or touch_obs.symbol,
            zone_id=zone_id or touch_obs.zone_id,
            wall_id=wall_id,
            wall_side=wall_side,
            wall_price=wall_price,
            band_low=band_low,
            band_high=band_high,
            event_type="WALL_OBSERVATION_AT_ZONE_TOUCH",
            exchange_event_time=_as_dt(touch_obs.interval_end_exchange_time),
            event_available_at=_as_dt(touch_obs.attribution_available_at),
            collector_received_at=None,
            receive_time_is_proxy=receive_time_is_proxy,
            book_sequence=touch_obs.book_sequence_end,
            replay_epoch=touch_obs.replay_epoch,
            price=wall_price,
            size_base=float(touch_obs.queue_after),
            notional_usdt=None,
            aggressor_side=touch_obs.aggressor_side,
            source_record_id=(touch_obs.source_record_ids[0] if touch_obs.source_record_ids else None),
            flow_attribution_confidence=touch_obs.attribution_confidence,
            availability_confidence=availability_confidence,
            coverage_ok=bool(touch_obs.coverage_ok and coverage_ok),
            look_ahead=bool(touch_obs.look_ahead),
            touch_at=touch_at,
            decision_at=decision_at,
            force_phase="AT_EVENT",
        )
        yield _row(
            event_id=event_id,
            episode_id=episode_id or touch_obs.episode_id,
            symbol=symbol or touch_obs.symbol,
            zone_id=zone_id or touch_obs.zone_id,
            wall_id=wall_id,
            wall_side=wall_side,
            wall_price=wall_price,
            band_low=band_low,
            band_high=band_high,
            event_type="WALL_FIRST_TOUCH",
            exchange_event_time=touch_at,
            event_available_at=touch_at,
            collector_received_at=None,
            receive_time_is_proxy=receive_time_is_proxy,
            book_sequence=touch_obs.book_sequence_end,
            replay_epoch=touch_obs.replay_epoch,
            price=wall_price,
            size_base=None,
            notional_usdt=None,
            aggressor_side=None,
            source_record_id=None,
            flow_attribution_confidence=flow_attribution_confidence,
            availability_confidence=availability_confidence,
            coverage_ok=coverage_ok,
            look_ahead=False,
            touch_at=touch_at,
            decision_at=decision_at,
            force_phase="AT_EVENT",
        )

    # Map attribution intervals → BOOK_DECREASE / PULL / REFILL / AGGRESSOR_TRADE
    for ev in band_events:
        end = _as_dt(ev.interval_end_exchange_time)
        avail = _as_dt(ev.attribution_available_at)
        if (not include_post_decision) and end > decision_at:
            continue
        if ev.attribution_confidence == "INVALID":
            continue
        common = dict(
            event_id=event_id,
            episode_id=episode_id or ev.episode_id,
            symbol=symbol or ev.symbol,
            zone_id=zone_id or ev.zone_id,
            wall_id=wall_id,
            wall_side=wall_side,
            wall_price=wall_price,
            band_low=band_low,
            band_high=band_high,
            exchange_event_time=end,
            event_available_at=avail,
            collector_received_at=None,
            receive_time_is_proxy=receive_time_is_proxy,
            book_sequence=ev.book_sequence_end,
            replay_epoch=ev.replay_epoch,
            price=wall_price,
            aggressor_side=ev.aggressor_side,
            source_record_id=(ev.source_record_ids[0] if ev.source_record_ids else None),
            flow_attribution_confidence=ev.attribution_confidence,
            availability_confidence=availability_confidence,
            coverage_ok=bool(ev.coverage_ok and coverage_ok),
            look_ahead=bool(ev.look_ahead),
            touch_at=touch_at,
            decision_at=decision_at,
        )
        book_dec = max(float(ev.queue_before) - float(ev.queue_after), 0.0)
        if book_dec > 1e-15:
            yield _row(**common, event_type="BOOK_DECREASE", size_base=book_dec, notional_usdt=None)
        if float(ev.residual_pull_qty) > 1e-15:
            yield _row(**common, event_type="WALL_PULL", size_base=float(ev.residual_pull_qty), notional_usdt=None)
        if float(ev.net_refill_qty) > 1e-15:
            yield _row(**common, event_type="WALL_REFILL", size_base=float(ev.net_refill_qty), notional_usdt=None)
        if float(ev.attributed_hit_qty) > 1e-15 and ev.attributed_trade_ids:
            yield _row(
                **common,
                event_type="AGGRESSOR_TRADE",
                size_base=float(ev.attributed_hit_qty),
                notional_usdt=float(ev.attributed_hit_notional),
            )

    for mv in wall_move_events or []:
        ts_raw = mv.get("exchange_event_time") or mv.get("event_time") or mv.get("bucket_start")
        if ts_raw is None:
            continue
        ts = _as_dt(ts_raw)
        if (not include_post_decision) and ts > decision_at:
            continue
        yield _row(
            event_id=event_id,
            episode_id=episode_id,
            symbol=symbol,
            zone_id=zone_id,
            wall_id=wall_id,
            wall_side=wall_side,
            wall_price=float(mv.get("wall_price") or wall_price),
            band_low=band_low,
            band_high=band_high,
            event_type="WALL_MOVE",
            exchange_event_time=ts,
            event_available_at=_as_dt(mv.get("available_at") or ts),
            collector_received_at=None,
            receive_time_is_proxy=receive_time_is_proxy,
            book_sequence=mv.get("book_sequence"),
            replay_epoch=mv.get("replay_epoch"),
            price=float(mv.get("wall_price") or wall_price),
            size_base=None,
            notional_usdt=None,
            aggressor_side=None,
            source_record_id=str(mv.get("source_record_id")) if mv.get("source_record_id") else None,
            flow_attribution_confidence=flow_attribution_confidence,
            availability_confidence=availability_confidence,
            coverage_ok=coverage_ok,
            look_ahead=bool(mv.get("look_ahead") or False),
            touch_at=touch_at,
            decision_at=decision_at,
        )

    yield _row(
        event_id=event_id,
        episode_id=episode_id,
        symbol=symbol,
        zone_id=zone_id,
        wall_id=wall_id,
        wall_side=wall_side,
        wall_price=wall_price,
        band_low=band_low,
        band_high=band_high,
        event_type="DECISION",
        exchange_event_time=decision_at,
        event_available_at=decision_at,
        collector_received_at=None,
        receive_time_is_proxy=receive_time_is_proxy,
        book_sequence=None,
        replay_epoch=None,
        price=wall_price,
        size_base=None,
        notional_usdt=None,
        aggressor_side=None,
        source_record_id=None,
        flow_attribution_confidence=flow_attribution_confidence,
        availability_confidence=availability_confidence,
        coverage_ok=coverage_ok,
        look_ahead=False,
        touch_at=touch_at,
        decision_at=decision_at,
        force_phase="AT_EVENT",
    )
    # flow_100ms retained as parallel bucket view; typed stream does not duplicate bucket math
    _ = flow_100ms


def materialize_typed_timeline(**kwargs: Any) -> list[dict[str, Any]]:
    rows = [e.to_dict() for e in iter_typed_wall_flow_events(**kwargs)]
    rows.sort(
        key=lambda r: (
            str(r["exchange_event_time"]),
            TYPED_EVENT_TYPES.index(r["event_type"]) if r["event_type"] in TYPED_EVENT_TYPES else 99,
            str(r.get("source_record_id") or ""),
        )
    )
    return rows


def typed_timeline_hash(rows: list[dict[str, Any]]) -> str:
    blob = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


TYPED_TIMELINE_SCHEMA_VERSION = "typed_wall_flow_timeline_v1"
