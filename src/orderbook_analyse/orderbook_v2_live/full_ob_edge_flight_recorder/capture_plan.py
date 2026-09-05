"""Capture timing contract full_ob_edge_capture_timing_v1 (pure functions)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.watcher import EdgeLevel

TIMING_CONTRACT = "full_ob_edge_capture_timing_v1"
FULL_OB_DELTA_PERIOD_SEC = 0.2  # Bybit full OB cadence; prebuffer tolerance cap


def as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return as_utc(dt).isoformat().replace("+00:00", "Z")


def compute_minimum_capture_end(trigger_ts: datetime, min_post_seconds: float) -> datetime:
    return as_utc(trigger_ts) + timedelta(seconds=float(min_post_seconds))


def compute_hard_capture_end(trigger_ts: datetime, max_seconds: float) -> datetime:
    return as_utc(trigger_ts) + timedelta(seconds=float(max_seconds))


def compute_normal_end(
    *,
    minimum_capture_end_ts: datetime,
    result_ts: datetime | None,
    result_tail_seconds: float,
) -> datetime:
    minimum_capture_end_ts = as_utc(minimum_capture_end_ts)
    if result_ts is None:
        return minimum_capture_end_ts
    return max(minimum_capture_end_ts, as_utc(result_ts) + timedelta(seconds=float(result_tail_seconds)))


def classify_fight_extension(
    *,
    in_edge_zone: bool,
    outside_open: bool,
    reclaim_unresolved: bool,
    acceptance_pending: bool,
    result_tail_active: bool,
) -> str | None:
    if acceptance_pending:
        return "BREAKOUT_ACCEPTANCE_PENDING"
    if outside_open:
        return "OUTSIDE_EXCURSION_STILL_OPEN"
    if reclaim_unresolved:
        return "RECLAIM_NOT_RESOLVED"
    if in_edge_zone:
        return "PRICE_STILL_IN_EDGE_ZONE"
    if result_tail_active:
        return "RESULT_TAIL_ACTIVE"
    return None


@dataclass
class SegmentRecord:
    continuation_index: int
    directory: str
    segment_first_ts: str | None = None
    segment_last_ts: str | None = None
    segment_first_u: int | None = None
    segment_last_u: int | None = None
    previous_segment_sha256: str | None = None
    segment_sha256: str | None = None
    segment_finalization_reason: str | None = None
    next_segment_expected: bool = True


@dataclass
class CapturePlan:
    fight_event_id: str
    symbol_event_id: str
    symbol: str
    trigger_ts: datetime
    trigger_receive_time_ns: int | None
    trigger_u: int | None
    trigger_seq: int | None
    trigger_source: str
    edge: str
    edge_type: str
    edge_price: float | None
    edge_price_at_trigger: float | None
    profile_session_start: str | None
    profile_cutoff_ts: str | None
    profile_contract_version: str | None
    market_price_at_trigger: float | None
    distance_to_edge_bps: float | None
    prior_zone_state: str
    trigger_zone_state: str
    edge_entry_crossed: bool
    bootstrap_status: str
    dedup_key: str
    minimum_capture_end_ts: datetime
    hard_capture_end_ts: datetime
    normal_end_ts: datetime
    result_ts: datetime | None = None
    result_kind: str | None = None
    actual_final_ts: datetime | None = None
    extension_count: int = 0
    extension_reason: str | None = None
    retouch_count: int = 0
    secondary_edge_count: int = 0
    secondary_edge_observation_count: int = 0
    nested_signal_count: int = 0
    nested_signals: list[dict[str, Any]] = field(default_factory=list)
    nested_extension_applied_ids: list[str] = field(default_factory=list)
    signal_analysis_contracts: list[dict[str, Any]] = field(default_factory=list)
    pre_trigger_seconds_actual: float = 0.0
    first_persisted_ts: datetime | None = None
    process_uptime_at_trigger_sec: float = 0.0
    data_quality: str = "OK"
    incomplete_reasons: list[str] = field(default_factory=list)
    stale_update_count: int = 0
    u_gap_count: int = 0  # apply_epoch gaps (Bybit WS apply) — alias source_feed
    apply_epoch_u_gap_count: int = 0
    persisted_capture_u_gap_count: int = 0
    persisted_missing_u_estimate: int = 0
    queue_drop_count: int = 0
    writer_queue_drop_count: int = 0
    transport_reconnect_count: int = 0
    resync_boundary_count: int = 0
    continuity_epoch_count: int = 1
    unobserved_interval_count: int = 0
    unobserved_duration_seconds: float = 0.0
    missing_update_id_count: int = 0
    resync_checkpoint_attempt_count: int = 0
    resync_checkpoint_success_count: int = 0
    resync_checkpoint_failure_count: int = 0
    checkpoint_persist_failed: bool = False
    continuous_capture: bool = True
    replayable_by_epochs: bool = True
    markers: list[dict[str, Any]] = field(default_factory=list)
    segments: list[SegmentRecord] = field(default_factory=list)
    trigger_edge: EdgeLevel | None = None
    research_eligible: bool = True
    trigger_quality: str = "REAL_CROSS_IN"
    bootstrap_persistent_capture: bool = False

    def note_incomplete(self, reason: str) -> None:
        if reason not in self.incomplete_reasons:
            self.incomplete_reasons.append(reason)
        self.data_quality = "INCOMPLETE"
        if reason in {
            "QUEUE_DROP",
            "U_GAP",
            "RESYNC",
            "PERSISTED_U_GAP",
            "WRITER_ERROR",
            "INVALID_RECORD_TS",
            "UNOBSERVED_TRANSPORT_GAP",
            "CHECKPOINT_PERSIST_FAILED",
        }:
            self.research_eligible = False

    def recompute_research_flags(self) -> None:
        """Strict research eligibility (continuous capture required)."""
        genuine = self.trigger_quality == "REAL_CROSS_IN" and self.trigger_source == "CROSS_IN"
        full_pre = "PREBUFFER_EMPTY" not in self.incomplete_reasons and not any(
            r.startswith("PRE_TRIGGER") or r == "PREBUFFER_TS_INVERSION" for r in self.incomplete_reasons
        )
        # post_capture completeness judged at finalize via timing; keep conservative here
        self.continuous_capture = (
            self.transport_reconnect_count == 0
            and self.unobserved_interval_count == 0
            and self.persisted_capture_u_gap_count == 0
            and self.apply_epoch_u_gap_count == 0
            and self.writer_queue_drop_count == 0
            and not self.checkpoint_persist_failed
        )
        self.replayable_by_epochs = (
            not self.checkpoint_persist_failed
            and self.resync_checkpoint_failure_count == 0
            and self.continuity_epoch_count >= 1
        )
        # Strict continuous-capture research gate. Prebuffer incompleteness is tracked
        # separately via incomplete_reasons and may still allow research_eligible until
        # finalize policy decides; do not auto-clear eligibility solely for empty prebuffer.
        self.research_eligible = bool(
            genuine
            and self.continuous_capture
            and self.writer_queue_drop_count == 0
            and self.apply_epoch_u_gap_count == 0
            and self.persisted_capture_u_gap_count == 0
            and self.replayable_by_epochs
            and "QUEUE_DROP" not in self.incomplete_reasons
            and "CHECKPOINT_PERSIST_FAILED" not in self.incomplete_reasons
        )
        # Keep fail-closed for explicit incomplete research blockers already noted.
        if any(
            r in self.incomplete_reasons
            for r in (
                "QUEUE_DROP",
                "U_GAP",
                "RESYNC",
                "PERSISTED_U_GAP",
                "WRITER_ERROR",
                "UNOBSERVED_TRANSPORT_GAP",
                "CHECKPOINT_PERSIST_FAILED",
            )
        ):
            self.research_eligible = False
        _ = full_pre  # documented for future finalize post-capture gate

    def add_marker(self, marker_type: str, now: datetime, **extra: Any) -> dict[str, Any]:
        rec = {
            "channel": "marker",
            "marker_type": marker_type,
            "ts": iso(now),
            "fight_event_id": self.fight_event_id,
            **extra,
        }
        self.markers.append(rec)
        return rec

    def to_manifest_fields(self, *, now: datetime | None = None) -> dict[str, Any]:
        self.recompute_research_flags()
        final = self.actual_final_ts or now
        post = None
        total = None
        if final is not None:
            post = (as_utc(final) - as_utc(self.trigger_ts)).total_seconds()
            if self.first_persisted_ts is not None:
                total = (as_utc(final) - as_utc(self.first_persisted_ts)).total_seconds()
            else:
                total = post
        return {
            "timing_contract": TIMING_CONTRACT,
            "resync_checkpoint_contract": "full_ob_resync_checkpoint_v1",
            "fight_event_id": self.fight_event_id,
            "symbol_event_id": self.symbol_event_id,
            "trigger_ts": iso(self.trigger_ts),
            "trigger_receive_time_ns": self.trigger_receive_time_ns,
            "trigger_u": self.trigger_u,
            "trigger_seq": self.trigger_seq,
            "trigger_source": self.trigger_source,
            "trigger_quality": self.trigger_quality,
            "research_eligible": self.research_eligible,
            "continuous_capture": self.continuous_capture,
            "replayable_by_epochs": self.replayable_by_epochs,
            "bootstrap_persistent_capture": self.bootstrap_persistent_capture,
            "edge": self.edge,
            "edge_type": self.edge_type,
            "edge_price": self.edge_price,
            "edge_price_at_trigger": self.edge_price_at_trigger,
            "profile_session_start": self.profile_session_start,
            "profile_cutoff_ts": self.profile_cutoff_ts,
            "profile_contract_version": self.profile_contract_version,
            "market_price_at_trigger": self.market_price_at_trigger,
            "distance_to_edge_bps": self.distance_to_edge_bps,
            "prior_zone_state": self.prior_zone_state,
            "trigger_zone_state": self.trigger_zone_state,
            "edge_entry_crossed": self.edge_entry_crossed,
            "bootstrap_status": self.bootstrap_status,
            "dedup_key": self.dedup_key,
            "minimum_capture_end_ts": iso(self.minimum_capture_end_ts),
            "result_ts": iso(self.result_ts),
            "normal_end_ts": iso(self.normal_end_ts),
            "hard_capture_end_ts": iso(self.hard_capture_end_ts),
            "actual_final_ts": iso(final) if final else None,
            "total_capture_seconds": total,
            "pre_trigger_seconds_actual": self.pre_trigger_seconds_actual,
            "post_trigger_seconds_actual": post,
            "extension_count": self.extension_count,
            "extension_reason": self.extension_reason,
            "segment_count": len(self.segments),
            "retouch_count": self.retouch_count,
            "secondary_edge_observation_count": self.secondary_edge_observation_count,
            "nested_signal_count": self.nested_signal_count,
            "nested_signals": list(self.nested_signals),
            "data_quality": self.data_quality,
            "incomplete_reasons": list(self.incomplete_reasons),
            "u_gap_count": self.u_gap_count,
            "source_feed_u_gap_count": self.u_gap_count,
            "apply_epoch_u_gap_count": self.apply_epoch_u_gap_count or self.u_gap_count,
            "persisted_capture_u_gap_count": self.persisted_capture_u_gap_count,
            "persisted_missing_u_estimate": self.persisted_missing_u_estimate,
            "queue_drop_count": self.queue_drop_count,
            "writer_queue_drop_count": self.writer_queue_drop_count or self.queue_drop_count,
            "stale_update_count": self.stale_update_count,
            "transport_reconnect_count": self.transport_reconnect_count,
            "resync_boundary_count": self.resync_boundary_count,
            "continuity_epoch_count": self.continuity_epoch_count,
            "unobserved_interval_count": self.unobserved_interval_count,
            "unobserved_duration_seconds": self.unobserved_duration_seconds,
            "missing_update_id_count": self.missing_update_id_count,
            "resync_checkpoint_attempt_count": self.resync_checkpoint_attempt_count,
            "resync_checkpoint_success_count": self.resync_checkpoint_success_count,
            "resync_checkpoint_failure_count": self.resync_checkpoint_failure_count,
            "checkpoint_persist_failed": self.checkpoint_persist_failed,
            "segments": [s.__dict__ for s in self.segments],
        }
