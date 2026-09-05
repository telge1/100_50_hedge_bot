"""Nested profile edge signals — decoupled from capture lifecycle (nested_profile_edge_signal_v1)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.watcher import EdgeLevel

NESTED_SIGNAL_CONTRACT = "nested_profile_edge_signal_v1"
PROFILE_CALCULATION_VERSION = "1"
UNFROZEN_RESEARCH_PARAMETER = "profile_watch_ttl_after_cutoff"


class ProfileSignalLifecycle(str, Enum):
    PROFILE_OBSERVING = "PROFILE_OBSERVING"
    PROFILE_ARMED = "PROFILE_ARMED"
    PROFILE_CROSS_IN = "PROFILE_CROSS_IN"
    PROFILE_INSIDE = "PROFILE_INSIDE"
    PROFILE_REARMED = "PROFILE_REARMED"
    PROFILE_EXPIRED = "PROFILE_EXPIRED"


def as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return as_utc(dt).isoformat().replace("+00:00", "Z")


def _price_key(price: float) -> str:
    """Stable decimal key — avoid raw float repr in IDs."""
    return f"{float(price):.8f}".rstrip("0").rstrip(".")


def stable_profile_id(
    *,
    symbol: str,
    session_start: datetime,
    cutoff: datetime,
    window_minutes: int,
    vah: float,
    val: float,
    poc: float,
    profile_basis: str,
    profile_fallback_used: bool,
    calculation_version: str = PROFILE_CALCULATION_VERSION,
) -> str:
    start = as_utc(session_start).strftime("%Y%m%dT%H%M%S")
    end = as_utc(cutoff).strftime("%Y%m%dT%H%M%S")
    payload = "|".join(
        [
            symbol.upper(),
            profile_basis,
            str(window_minutes),
            start,
            end,
            calculation_version,
            "1" if profile_fallback_used else "0",
            _price_key(vah),
            _price_key(val),
            _price_key(poc),
        ]
    )
    digest = hashlib.sha256(payload.encode()).hexdigest()[:12]
    return f"{symbol.upper()}_{start}_{end}_v{calculation_version}_{digest}"


def profile_basis_from_meta(meta: dict[str, Any]) -> tuple[str, bool, bool]:
    tpo_src = str(meta.get("tpo_source") or "")
    fallback = tpo_src == "volume_proxy_fallback" or bool(meta.get("profile_fallback_used"))
    true_tpo = tpo_src == "tpo_bracket_presence_1m_causal"
    basis = "VOLUME" if fallback else ("TPO" if true_tpo else "MIXED")
    return basis, fallback, true_tpo


def extract_vah_val_poc(edges: tuple[EdgeLevel, ...], meta: dict[str, Any]) -> tuple[float, float, float]:
    vah = float(meta.get("volume_vah") or 0)
    val = float(meta.get("volume_val") or 0)
    poc = float(meta.get("volume_poc") or 0)
    for e in edges:
        if e.kind == "TPO_VAH":
            vah = float(e.price)
        elif e.kind == "TPO_VAL":
            val = float(e.price)
    if poc <= 0:
        poc = (vah + val) / 2.0
    return vah, val, poc


def distance_bps(mid: float, edge: float) -> float:
    if mid <= 0:
        return float("inf")
    return abs(mid - edge) / mid * 10_000.0


def classify_zone(dist: float, *, capture_bps: float, disarm_bps: float) -> str:
    if dist <= capture_bps:
        return "IN"
    if dist >= disarm_bps:
        return "OUT"
    return "APPROACH"


def nearest_vah_val_edge(mid: float, vah: float, val: float) -> tuple[str, float]:
    if distance_bps(mid, vah) <= distance_bps(mid, val):
        return "TPO_VAH", vah
    return "TPO_VAL", val


@dataclass
class NestedSignalRecord:
    nested_signal_contract: str
    nested_signal_id: str
    parent_fight_event_id: str
    continuity_epoch_id: int
    parent_segment_index: int
    symbol: str
    signal_ts: str
    receive_time_ns: int | None
    profile_id: str
    profile_basis: str
    profile_window_minutes: int
    profile_start_ts: str
    profile_end_ts: str
    profile_calculation_version: str
    profile_fallback_used: bool
    true_tpo_computed: bool
    vah: float
    val: float
    poc: float
    edge: str
    edge_side: str
    edge_price: float
    trigger_price: float
    distance_bps: float
    arm_threshold_bps: float
    entry_threshold_bps: float
    rearm_threshold_bps: float
    arm_ts: str | None
    cross_ts: str
    arm_cycle_id: int
    causal_cutoff_ts: str
    capture_status: str
    signal_capture_continuous: bool
    signal_research_eligible: bool
    dedup_key: str
    prior_zone_state: str
    trigger_zone_state: str
    bootstrap_status: str = "N/A"

    def to_dict(self) -> dict[str, Any]:
        return {
            "nested_signal_contract": self.nested_signal_contract,
            "nested_signal_id": self.nested_signal_id,
            "parent_fight_event_id": self.parent_fight_event_id,
            "continuity_epoch_id": self.continuity_epoch_id,
            "parent_segment_index": self.parent_segment_index,
            "symbol": self.symbol,
            "signal_ts": self.signal_ts,
            "receive_time_ns": self.receive_time_ns,
            "profile_id": self.profile_id,
            "profile_basis": self.profile_basis,
            "profile_window_minutes": self.profile_window_minutes,
            "profile_start_ts": self.profile_start_ts,
            "profile_end_ts": self.profile_end_ts,
            "profile_calculation_version": self.profile_calculation_version,
            "profile_fallback_used": self.profile_fallback_used,
            "true_tpo_computed": self.true_tpo_computed,
            "vah": self.vah,
            "val": self.val,
            "poc": self.poc,
            "edge": self.edge,
            "edge_side": self.edge_side,
            "edge_price": self.edge_price,
            "trigger_price": self.trigger_price,
            "distance_bps": self.distance_bps,
            "arm_threshold_bps": self.arm_threshold_bps,
            "entry_threshold_bps": self.entry_threshold_bps,
            "rearm_threshold_bps": self.rearm_threshold_bps,
            "arm_ts": self.arm_ts,
            "cross_ts": self.cross_ts,
            "arm_cycle_id": self.arm_cycle_id,
            "causal_cutoff_ts": self.causal_cutoff_ts,
            "capture_status": self.capture_status,
            "signal_capture_continuous": self.signal_capture_continuous,
            "signal_research_eligible": self.signal_research_eligible,
            "dedup_key": self.dedup_key,
            "prior_zone_state": self.prior_zone_state,
            "trigger_zone_state": self.trigger_zone_state,
            "bootstrap_status": self.bootstrap_status,
        }


@dataclass
class EdgeTrackState:
    edge_kind: str
    edge_price: float
    edge_side: str
    lifecycle: ProfileSignalLifecycle = ProfileSignalLifecycle.PROFILE_OBSERVING
    zone_state: str = "UNKNOWN"
    arm_cycle_id: int = 0
    arm_ts: datetime | None = None
    saw_outside: bool = False
    bootstrap_noted: bool = False
    emitted_signals: set[str] = field(default_factory=set)


@dataclass
class ProfileWatchState:
    profile_id: str
    symbol: str
    session_start: datetime
    cutoff: datetime
    window_minutes: int
    vah: float
    val: float
    poc: float
    profile_basis: str
    profile_fallback_used: bool
    true_tpo_computed: bool
    lifecycle: ProfileSignalLifecycle = ProfileSignalLifecycle.PROFILE_OBSERVING
    vah_track: EdgeTrackState | None = None
    val_track: EdgeTrackState | None = None
    registered_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.vah_track is None:
            self.vah_track = EdgeTrackState("TPO_VAH", self.vah, "UPPER")
        if self.val_track is None:
            self.val_track = EdgeTrackState("TPO_VAL", self.val, "LOWER")

    @property
    def signal_eligible(self) -> bool:
        return self.lifecycle is not ProfileSignalLifecycle.PROFILE_EXPIRED


@dataclass
class NestedSignalTickResult:
    new_signal: NestedSignalRecord | None = None
    lifecycle: ProfileSignalLifecycle | None = None
    zone_state: str | None = None
    bootstrap: bool = False


@dataclass
class ProfileSignalRegistry:
    arm_bps: float = 50.0
    capture_bps: float = 20.0
    disarm_bps: float = 75.0
    window_minutes: int = 30
    max_active_profiles: int = 8
    calculation_version: str = PROFILE_CALCULATION_VERSION
    _profiles: dict[str, dict[str, ProfileWatchState]] = field(default_factory=dict)
    nested_signal_count: int = 0
    secondary_edge_observation_count: int = 0
    duplicate_secondary_trigger_suppressed_count: int = 0
    profile_arm_count: int = 0
    profile_rearm_count: int = 0
    profile_expiry_count: int = 0
    _dedup_keys: set[str] = field(default_factory=set)

    def active_watch_count(self, symbol: str) -> int:
        sym = symbol.upper()
        return sum(1 for p in self._profiles.get(sym, {}).values() if p.signal_eligible)

    def register_profile(
        self,
        *,
        symbol: str,
        session_start: datetime,
        cutoff: datetime,
        edges: tuple[EdgeLevel, ...],
        meta: dict[str, Any],
        now: datetime,
        mid: float | None = None,
    ) -> ProfileWatchState | None:
        sym = symbol.upper()
        now = as_utc(now)
        cutoff = as_utc(cutoff)
        if now < cutoff:
            return None
        basis, fallback, true_tpo = profile_basis_from_meta(meta)
        vah, val, poc = extract_vah_val_poc(edges, meta)
        pid = stable_profile_id(
            symbol=sym,
            session_start=session_start,
            cutoff=cutoff,
            window_minutes=int(meta.get("bracket_minutes") or self.window_minutes),
            vah=vah,
            val=val,
            poc=poc,
            profile_basis=basis,
            profile_fallback_used=fallback,
            calculation_version=self.calculation_version,
        )
        by_sym = self._profiles.setdefault(sym, {})
        if pid in by_sym:
            return by_sym[pid]
        st = ProfileWatchState(
            profile_id=pid,
            symbol=sym,
            session_start=as_utc(session_start),
            cutoff=cutoff,
            window_minutes=int(meta.get("bracket_minutes") or self.window_minutes),
            vah=vah,
            val=val,
            poc=poc,
            profile_basis=basis,
            profile_fallback_used=fallback,
            true_tpo_computed=true_tpo,
            registered_at=now,
        )
        by_sym[pid] = st
        self._evict_if_needed(sym)
        if mid is not None and mid > 0:
            self._bootstrap_on_register(st, mid, now)
        return st

    def _evict_if_needed(self, sym: str) -> None:
        by_sym = self._profiles.get(sym, {})
        if len(by_sym) <= self.max_active_profiles:
            return
        # Oldest cutoff first (deterministic eviction). Must delete from map or
        # len(by_sym) never shrinks and pop() raises IndexError (live crash 2026-09-04).
        ordered = sorted(by_sym.values(), key=lambda p: p.cutoff)
        while len(by_sym) > self.max_active_profiles and ordered:
            victim = ordered.pop(0)
            victim.lifecycle = ProfileSignalLifecycle.PROFILE_EXPIRED
            by_sym.pop(victim.profile_id, None)
            self.profile_expiry_count += 1

    def _bootstrap_on_register(self, st: ProfileWatchState, mid: float, now: datetime) -> None:
        for track in (st.vah_track, st.val_track):
            if track is None:
                continue
            dist = distance_bps(mid, track.edge_price)
            zone = classify_zone(dist, capture_bps=self.capture_bps, disarm_bps=self.disarm_bps)
            track.zone_state = zone
            if zone == "IN" and not track.saw_outside:
                track.bootstrap_noted = True

    def expire_all(self, symbol: str) -> None:
        sym = symbol.upper()
        for st in self._profiles.get(sym, {}).values():
            if st.lifecycle is not ProfileSignalLifecycle.PROFILE_EXPIRED:
                st.lifecycle = ProfileSignalLifecycle.PROFILE_EXPIRED
                self.profile_expiry_count += 1

    def _evaluate_edge_track(
        self,
        track: EdgeTrackState,
        *,
        mid: float,
        now: datetime,
    ) -> bool:
        dist = distance_bps(mid, track.edge_price)
        prior = track.zone_state
        zone = classify_zone(dist, capture_bps=self.capture_bps, disarm_bps=self.disarm_bps)
        if zone != "IN":
            track.saw_outside = True
        if zone == "IN" and not track.saw_outside and not track.bootstrap_noted:
            track.bootstrap_noted = True
            track.zone_state = zone
            return False
        if zone == "IN" and not track.saw_outside and track.bootstrap_noted:
            track.zone_state = zone
            return False
        entered = zone == "IN" and prior not in ("IN",)
        if track.lifecycle in {
            ProfileSignalLifecycle.PROFILE_OBSERVING,
            ProfileSignalLifecycle.PROFILE_REARMED,
            ProfileSignalLifecycle.PROFILE_ARMED,
        }:
            if zone != "IN" and dist <= self.arm_bps and track.lifecycle in {
                ProfileSignalLifecycle.PROFILE_OBSERVING,
                ProfileSignalLifecycle.PROFILE_REARMED,
            }:
                track.lifecycle = ProfileSignalLifecycle.PROFILE_ARMED
                track.arm_ts = now
                track.arm_cycle_id += 1
                self.profile_arm_count += 1
            if (
                entered
                and track.saw_outside
                and track.lifecycle
                in {ProfileSignalLifecycle.PROFILE_ARMED, ProfileSignalLifecycle.PROFILE_REARMED}
            ):
                track.lifecycle = ProfileSignalLifecycle.PROFILE_CROSS_IN
                track.zone_state = zone
                return True
        if zone == "IN" and track.lifecycle == ProfileSignalLifecycle.PROFILE_CROSS_IN:
            track.lifecycle = ProfileSignalLifecycle.PROFILE_INSIDE
        if zone == "OUT" and track.lifecycle in {
            ProfileSignalLifecycle.PROFILE_INSIDE,
            ProfileSignalLifecycle.PROFILE_CROSS_IN,
            ProfileSignalLifecycle.PROFILE_ARMED,
        }:
            track.lifecycle = ProfileSignalLifecycle.PROFILE_REARMED
            self.profile_rearm_count += 1
        track.zone_state = zone
        return False

    def evaluate_profile(
        self,
        st: ProfileWatchState,
        *,
        mid: float,
        now: datetime,
    ) -> NestedSignalTickResult:
        if not st.signal_eligible or mid <= 0:
            return NestedSignalTickResult()
        if as_utc(now) < st.cutoff:
            return NestedSignalTickResult()
        crossed_tracks: list[EdgeTrackState] = []
        for track in (st.vah_track, st.val_track):
            if track is None:
                continue
            if self._evaluate_edge_track(track, mid=mid, now=as_utc(now)):
                crossed_tracks.append(track)
        if crossed_tracks:
            return NestedSignalTickResult(
                lifecycle=ProfileSignalLifecycle.PROFILE_CROSS_IN,
                zone_state="IN",
            )
        return NestedSignalTickResult(lifecycle=st.lifecycle)

    def build_signal_if_cross(
        self,
        st: ProfileWatchState,
        *,
        mid: float,
        now: datetime,
        parent_fight_event_id: str,
        continuity_epoch_id: int,
        parent_segment_index: int,
        receive_time_ns: int | None,
        prior_zone: str,
        capture_continuous: bool,
        capture_research_eligible: bool,
    ) -> NestedSignalRecord | None:
        cross_tracks: list[EdgeTrackState] = []
        for track in (st.vah_track, st.val_track):
            if track is not None and track.lifecycle is ProfileSignalLifecycle.PROFILE_CROSS_IN:
                cross_tracks.append(track)
        if not cross_tracks:
            return None
        # When both edges are IN on the same tick, pick the nearest edge (matches live nearest-edge semantics).
        track = min(cross_tracks, key=lambda t: distance_bps(mid, t.edge_price))
        dedup_key = f"{st.symbol}|{st.profile_id}|{track.edge_kind}|{track.arm_cycle_id}"
        if dedup_key in track.emitted_signals or dedup_key in self._dedup_keys:
            track.lifecycle = ProfileSignalLifecycle.PROFILE_INSIDE
            return None
        dist = distance_bps(mid, track.edge_price)
        nested_id = (
            f"{parent_fight_event_id}_ns_{st.profile_id[-12:]}_{track.arm_cycle_id}_{track.edge_side[0]}"
        )
        rec = NestedSignalRecord(
            nested_signal_contract=NESTED_SIGNAL_CONTRACT,
            nested_signal_id=nested_id,
            parent_fight_event_id=parent_fight_event_id,
            continuity_epoch_id=continuity_epoch_id,
            parent_segment_index=parent_segment_index,
            symbol=st.symbol,
            signal_ts=iso(now) or "",
            receive_time_ns=receive_time_ns,
            profile_id=st.profile_id,
            profile_basis=st.profile_basis,
            profile_window_minutes=st.window_minutes,
            profile_start_ts=iso(st.session_start) or "",
            profile_end_ts=iso(st.cutoff) or "",
            profile_calculation_version=self.calculation_version,
            profile_fallback_used=st.profile_fallback_used,
            true_tpo_computed=st.true_tpo_computed,
            vah=st.vah,
            val=st.val,
            poc=st.poc,
            edge=track.edge_kind,
            edge_side=track.edge_side,
            edge_price=float(track.edge_price),
            trigger_price=float(mid),
            distance_bps=float(dist),
            arm_threshold_bps=self.arm_bps,
            entry_threshold_bps=self.capture_bps,
            rearm_threshold_bps=self.disarm_bps,
            arm_ts=iso(track.arm_ts),
            cross_ts=iso(now) or "",
            arm_cycle_id=track.arm_cycle_id,
            causal_cutoff_ts=iso(st.cutoff) or "",
            capture_status="PARENT_CAPTURE_OPEN",
            signal_capture_continuous=capture_continuous,
            signal_research_eligible=capture_research_eligible and capture_continuous,
            dedup_key=dedup_key,
            prior_zone_state=prior_zone,
            trigger_zone_state="IN",
        )
        track.emitted_signals.add(dedup_key)
        self._dedup_keys.add(dedup_key)
        self.nested_signal_count += 1
        track.lifecycle = ProfileSignalLifecycle.PROFILE_INSIDE
        for other in cross_tracks:
            if other is not track and other.lifecycle is ProfileSignalLifecycle.PROFILE_CROSS_IN:
                other.lifecycle = ProfileSignalLifecycle.PROFILE_INSIDE
        return rec

    def note_secondary_observation(self) -> None:
        self.secondary_edge_observation_count += 1
        self.duplicate_secondary_trigger_suppressed_count += 1

    def health_metrics(self, symbol: str) -> dict[str, Any]:
        sym = symbol.upper()
        return {
            "secondary_edge_observation_count": self.secondary_edge_observation_count,
            "nested_signal_count": self.nested_signal_count,
            "duplicate_secondary_trigger_suppressed_count": self.duplicate_secondary_trigger_suppressed_count,
            "profile_arm_count": self.profile_arm_count,
            "profile_rearm_count": self.profile_rearm_count,
            "profile_expiry_count": self.profile_expiry_count,
            "active_profile_watch_count": self.active_watch_count(sym),
        }


def replay_minute_series(
    registry: ProfileSignalRegistry,
    *,
    symbol: str,
    session_start: datetime,
    cutoff: datetime,
    vah: float,
    val: float,
    poc: float,
    minute_mids: list[tuple[datetime, float]],
    parent_fight_event_id: str,
    profile_fallback_used: bool = True,
) -> list[NestedSignalRecord]:
    """Offline causal replay helper for historical regression."""
    meta = {
        "bracket_minutes": registry.window_minutes,
        "tpo_source": "volume_proxy_fallback" if profile_fallback_used else "tpo_bracket_presence_1m_causal",
        "volume_vah": vah,
        "volume_val": val,
        "volume_poc": poc,
    }
    edges = (
        EdgeLevel("TPO_VAH", vah, "replay", cutoff),
        EdgeLevel("TPO_VAL", val, "replay", cutoff),
    )
    signals: list[NestedSignalRecord] = []
    first_mid = minute_mids[0][1] if minute_mids else 80.0
    st = registry.register_profile(
        symbol=symbol,
        session_start=session_start,
        cutoff=cutoff,
        edges=edges,
        meta=meta,
        now=cutoff + __import__("datetime").timedelta(seconds=1),
        mid=first_mid if first_mid < min(vah, val) * 0.99 else 80.0,
    )
    assert st is not None
    prior = "OUT"
    for ts, mid in sorted(minute_mids, key=lambda x: x[0]):
        if ts < cutoff:
            continue
        tick = registry.evaluate_profile(st, mid=mid, now=ts)
        prior = tick.zone_state or prior
        if tick.lifecycle is ProfileSignalLifecycle.PROFILE_CROSS_IN:
            sig = registry.build_signal_if_cross(
                st,
                mid=mid,
                now=ts,
                parent_fight_event_id=parent_fight_event_id,
                continuity_epoch_id=0,
                parent_segment_index=0,
                receive_time_ns=None,
                prior_zone=prior,
                capture_continuous=False,
                capture_research_eligible=False,
            )
            if sig:
                signals.append(sig)
    return signals
