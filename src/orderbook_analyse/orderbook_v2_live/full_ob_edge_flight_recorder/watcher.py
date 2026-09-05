"""Lifecycle + edge watcher for Full-OB flight recorder."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any


class SymbolLifecycle(str, Enum):
    IDLE = "IDLE"
    ARMED = "ARMED"
    SUBSCRIBING = "SUBSCRIBING"
    SYNCING = "SYNCING"
    BOOK_READY = "BOOK_READY"
    CAPTURING = "CAPTURING"
    FIGHT_ACTIVE = "FIGHT_ACTIVE"
    POST_CAPTURE = "POST_CAPTURE"
    COOLDOWN = "COOLDOWN"
    REARMED = "REARMED"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class EdgeLevel:
    kind: str
    price: float
    profile_id: str
    cutoff: datetime


def distance_bps(mid: float, edge: float) -> float:
    if mid <= 0:
        return float("inf")
    return abs(mid - edge) / mid * 10_000.0


def classify_zone_state(
    *,
    mid: float,
    edge: EdgeLevel | None,
    capture_bps: float,
    arm_bps: float,
    disarm_bps: float,
) -> str:
    if edge is None or mid <= 0:
        return "UNKNOWN"
    dist = distance_bps(mid, edge.price)
    if dist <= capture_bps:
        return "IN"
    if dist >= disarm_bps:
        return "OUT"
    return "APPROACH"


def in_edge_zone(zone: str) -> bool:
    return zone == "IN"


@dataclass
class WatchSample:
    ts: datetime
    mid: float
    nearest: EdgeLevel | None
    distance_bps: float
    approach_bps_per_sec: float
    inside_value_area: bool | None = None
    zone_state: str = "UNKNOWN"


@dataclass
class WatchDecision:
    action: str  # none|arm|disarm|trigger|extend
    reason: str
    sample: WatchSample | None = None
    edge: EdgeLevel | None = None
    marker: str | None = None
    trigger_source: str | None = None
    prior_zone_state: str = "UNKNOWN"
    trigger_zone_state: str = "UNKNOWN"
    edge_entry_crossed: bool = False
    bootstrap_status: str = "N/A"


@dataclass
class SymbolWatch:
    lifecycle: SymbolLifecycle = SymbolLifecycle.IDLE
    last_sample: WatchSample | None = None
    armed_edge: EdgeLevel | None = None
    event_id: str | None = None
    cooldown_until: datetime | None = None
    capture_started_at: datetime | None = None
    crossed: bool = False
    outside_since: datetime | None = None
    reclaim_seen: bool = False
    acceptance_active: bool = False
    post_until: datetime | None = None
    frozen_edges: tuple[EdgeLevel, ...] = ()
    frozen_profile: dict[str, Any] = field(default_factory=dict)
    pre_trigger_incomplete: bool = False
    lifecycle_log: list[dict[str, Any]] = field(default_factory=list)
    zone_state: str = "UNKNOWN"
    saw_outside: bool = False
    trigger_edge: EdgeLevel | None = None
    pending_profile_update: dict[str, Any] | None = None
    result_kind: str | None = None
    result_ts: datetime | None = None
    bootstrap_noted: bool = False
    bootstrap_observation_count: int = 0
    bootstrap_status: str = "N/A"

    def transition(self, new: SymbolLifecycle, *, ts: datetime, reason: str) -> None:
        if new is self.lifecycle:
            return
        self.lifecycle_log.append(
            {
                "ts": ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                "from": self.lifecycle.value,
                "to": new.value,
                "reason": reason,
            }
        )
        self.lifecycle = new


class EdgeWatcher:
    def __init__(
        self,
        *,
        arm_bps: float,
        capture_bps: float,
        disarm_bps: float,
        fast_approach_bps_per_sec: float,
        cooldown_minutes: float,
        acceptance_hold_sec: float,
    ) -> None:
        self.arm_bps = arm_bps
        self.capture_bps = capture_bps
        self.disarm_bps = disarm_bps
        self.fast_approach = fast_approach_bps_per_sec
        self.cooldown = timedelta(minutes=cooldown_minutes)
        self.acceptance_hold_sec = acceptance_hold_sec
        self._states: dict[str, SymbolWatch] = {}
        self._edges: dict[str, tuple[EdgeLevel, ...]] = {}

    def state(self, symbol: str) -> SymbolWatch:
        sym = symbol.upper()
        st = self._states.get(sym)
        if st is None:
            st = SymbolWatch()
            self._states[sym] = st
        return st

    def set_edges(self, symbol: str, edges: tuple[EdgeLevel, ...], profile: dict[str, Any]) -> str:
        sym = symbol.upper()
        st = self.state(sym)
        if st.lifecycle in {
            SymbolLifecycle.CAPTURING,
            SymbolLifecycle.FIGHT_ACTIVE,
            SymbolLifecycle.POST_CAPTURE,
        }:
            st.pending_profile_update = {"edges": edges, "profile": dict(profile)}
            return "PROFILE_UPDATE_DURING_CAPTURE"
        self._edges[sym] = edges
        st.frozen_profile = dict(profile)
        return "UPDATED"

    def evaluate(self, symbol: str, mid: float, now: datetime | None = None) -> WatchDecision:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        sym = symbol.upper()
        st = self.state(sym)
        edges = st.frozen_edges or self._edges.get(sym) or ()
        if not edges or mid <= 0:
            return WatchDecision(action="none", reason="no_edges_or_mid")

        nearest = min(edges, key=lambda e: distance_bps(mid, e.price))
        dist = distance_bps(mid, nearest.price)
        approach = 0.0
        if st.last_sample is not None:
            dt = (now - st.last_sample.ts).total_seconds()
            if dt > 0:
                approach = (st.last_sample.distance_bps - dist) / dt

        vahs = [e for e in edges if e.kind.endswith("VAH")]
        vals = [e for e in edges if e.kind.endswith("VAL")]
        inside = None
        if vahs and vals:
            inside = max(v.price for v in vals) <= mid <= min(v.price for v in vahs)

        prior_zone = st.zone_state
        zone = classify_zone_state(
            mid=mid,
            edge=nearest,
            capture_bps=self.capture_bps,
            arm_bps=self.arm_bps,
            disarm_bps=self.disarm_bps,
        )
        if not in_edge_zone(zone):
            st.saw_outside = True
        st.zone_state = zone

        sample = WatchSample(
            ts=now,
            mid=mid,
            nearest=nearest,
            distance_bps=dist,
            approach_bps_per_sec=approach,
            inside_value_area=inside,
            zone_state=zone,
        )
        st.last_sample = sample

        if st.cooldown_until and now < st.cooldown_until and st.lifecycle is SymbolLifecycle.COOLDOWN:
            return WatchDecision(
                action="none",
                reason="cooldown",
                sample=sample,
                edge=nearest,
                prior_zone_state=prior_zone,
                trigger_zone_state=zone,
            )

        if st.lifecycle is SymbolLifecycle.COOLDOWN and (st.cooldown_until is None or now >= st.cooldown_until):
            if zone == "OUT":
                st.transition(SymbolLifecycle.REARMED, ts=now, reason="rearm_zone_reached")
                st.armed_edge = None
            return WatchDecision(
                action="none",
                reason="wait_rearm" if zone != "OUT" else "rearmed",
                sample=sample,
                edge=nearest,
            )

        if st.lifecycle in {
            SymbolLifecycle.CAPTURING,
            SymbolLifecycle.FIGHT_ACTIVE,
            SymbolLifecycle.POST_CAPTURE,
        }:
            outside = False
            marker = None
            if nearest.kind.endswith("VAH") and mid > nearest.price:
                outside = True
            if nearest.kind.endswith("VAL") and mid < nearest.price:
                outside = True
            if outside:
                if st.outside_since is None:
                    st.outside_since = now
                st.crossed = True
                if (now - st.outside_since).total_seconds() >= self.acceptance_hold_sec:
                    st.acceptance_active = True
                    if st.result_kind is None:
                        st.result_kind = "BREAKOUT_ACCEPTED"
                        st.result_ts = now
                    st.transition(SymbolLifecycle.FIGHT_ACTIVE, ts=now, reason="acceptance")
            else:
                if st.outside_since is not None and st.crossed:
                    st.reclaim_seen = True
                    marker = "EDGE_RETOUCH"
                    if st.result_kind is None:
                        st.result_kind = "RECLAIM"
                        st.result_ts = now
                st.outside_since = None
                st.acceptance_active = False
            trig = st.trigger_edge
            if trig is not None and nearest.kind != trig.kind:
                marker = "SECONDARY_EDGE_TRIGGER"
            return WatchDecision(
                action="extend",
                reason="fight_active" if st.acceptance_active else ("reclaim" if st.reclaim_seen else "capturing"),
                sample=sample,
                edge=nearest,
                marker=marker,
                prior_zone_state=prior_zone,
                trigger_zone_state=zone,
            )

        entered = in_edge_zone(zone) and not in_edge_zone(prior_zone)

        if st.lifecycle is SymbolLifecycle.REARMED:
            if entered:
                return WatchDecision(
                    action="trigger",
                    reason="cross_in",
                    sample=sample,
                    edge=nearest,
                    trigger_source="CROSS_IN",
                    prior_zone_state=prior_zone,
                    trigger_zone_state=zone,
                    edge_entry_crossed=True,
                    bootstrap_status="N/A",
                )
            return WatchDecision(action="none", reason="rearmed_waiting", sample=sample, edge=nearest)

        if st.lifecycle in {SymbolLifecycle.IDLE, SymbolLifecycle.BOOK_READY}:
            if in_edge_zone(zone) and not st.saw_outside:
                # Bootstrap inside edge is an audit marker only — never open persistent capture.
                if not st.bootstrap_noted:
                    st.bootstrap_noted = True
                    st.bootstrap_observation_count += 1
                    st.bootstrap_status = "BOOTSTRAP_ALREADY_IN_EDGE_ZONE"
                    return WatchDecision(
                        action="bootstrap_observe",
                        reason="bootstrap_already_in_zone",
                        sample=sample,
                        edge=nearest,
                        trigger_source="BOOTSTRAP_ALREADY_IN_EDGE_ZONE",
                        prior_zone_state=prior_zone,
                        trigger_zone_state=zone,
                        edge_entry_crossed=False,
                        bootstrap_status="BOOTSTRAP_ALREADY_IN_EDGE_ZONE",
                    )
                return WatchDecision(
                    action="none",
                    reason="bootstrap_waiting_exit_for_rearm",
                    sample=sample,
                    edge=nearest,
                    bootstrap_status="BOOTSTRAP_ALREADY_IN_EDGE_ZONE",
                    prior_zone_state=prior_zone,
                    trigger_zone_state=zone,
                )
            if entered:
                return WatchDecision(
                    action="trigger",
                    reason="cross_in",
                    sample=sample,
                    edge=nearest,
                    trigger_source="CROSS_IN",
                    prior_zone_state=prior_zone,
                    trigger_zone_state=zone,
                    edge_entry_crossed=True,
                    bootstrap_status="N/A",
                )
            if dist <= self.arm_bps and not in_edge_zone(zone):
                st.transition(SymbolLifecycle.ARMED, ts=now, reason="arm_distance")
                st.armed_edge = nearest
                return WatchDecision(action="arm", reason="within_arm_bps", sample=sample, edge=nearest)
            return WatchDecision(action="none", reason="idle_far", sample=sample, edge=nearest)

        if st.lifecycle is SymbolLifecycle.ARMED:
            if dist > self.disarm_bps:
                st.transition(SymbolLifecycle.IDLE, ts=now, reason="hysteresis_disarm")
                st.armed_edge = None
                return WatchDecision(action="disarm", reason="hysteresis_exit", sample=sample, edge=nearest)
            if entered:
                return WatchDecision(
                    action="trigger",
                    reason="cross_in",
                    sample=sample,
                    edge=nearest,
                    trigger_source="CROSS_IN",
                    prior_zone_state=prior_zone,
                    trigger_zone_state=zone,
                    edge_entry_crossed=True,
                    bootstrap_status="N/A",
                )
            return WatchDecision(action="none", reason="armed_waiting", sample=sample, edge=nearest)

        return WatchDecision(action="none", reason=f"lifecycle_{st.lifecycle.value}", sample=sample, edge=nearest)

    def begin_capture(self, symbol: str, event_id: str, edges: tuple[EdgeLevel, ...], now: datetime) -> None:
        st = self.state(symbol)
        st.event_id = event_id
        st.frozen_edges = edges
        st.capture_started_at = now
        st.crossed = False
        st.reclaim_seen = False
        st.acceptance_active = False
        st.outside_since = None
        st.result_kind = None
        st.result_ts = None
        st.bootstrap_status = "N/A"
        st.transition(SymbolLifecycle.CAPTURING, ts=now, reason="event_start")

    def end_capture(self, symbol: str, now: datetime) -> None:
        st = self.state(symbol)
        st.event_id = None
        st.capture_started_at = None
        st.post_until = None
        st.frozen_edges = ()
        st.trigger_edge = None
        st.cooldown_until = now + self.cooldown
        st.transition(SymbolLifecycle.COOLDOWN, ts=now, reason="event_end")
