"""Full-OB Edge Flight Recorder manager — observes existing FullBookOnDemandManager."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from orderbook_analyse.orderbook_v2_live.full_book_state import FULL_DEPTH, RPI_INCLUDED_IN_FULL_OB
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.async_sink import (
    NonBlockingDeltaSink,
)
from orderbook_analyse.orderbook_v2_live.raw_archive.disk import check_disk
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.capture_plan import (
    FULL_OB_DELTA_PERIOD_SEC,
    TIMING_CONTRACT,
    CapturePlan,
    SegmentRecord,
    classify_fight_extension,
    compute_hard_capture_end,
    compute_minimum_capture_end,
    compute_normal_end,
    iso,
)
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.config import (
    CONTRACT_VERSION,
    KEEPER_LEASE_PREFIX,
    FlightRecorderSettings,
    load_flight_recorder_settings,
)
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.event_writer import (
    ActiveEventWriter,
    event_dir,
    new_event_id,
)
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.profiles import ProfileProvider
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.continuity_contract import (
    RECORD_BOOK_DELTA,
    RECORD_INITIAL_CHECKPOINT,
    RECORD_RESYNC_BOUNDARY,
    RECORD_RESYNC_CHECKPOINT,
    RESYNC_CHECKPOINT_CONTRACT,
    annotate_delta_record,
    annotate_marker_record,
    build_checkpoint_record,
    build_resync_boundary_record,
)
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.record_envelope import (
    approx_envelope_bytes,
    build_delta_envelope,
    level_update_count,
)
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.nested_profile_signal import (
    NESTED_SIGNAL_CONTRACT,
    ProfileSignalLifecycle,
    ProfileSignalRegistry,
)
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.signal_analysis_isolation import (
    ANALYSIS_ISOLATION_CONTRACT,
    assign_overlap_clusters,
    contract_from_nested_signal_dict,
    contract_from_parent_plan,
)
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.replay import replay_event_directory
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.ringbuffer import BoundedRawRingBuffer
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.watcher import (
    EdgeLevel,
    EdgeWatcher,
    SymbolLifecycle,
    in_edge_zone,
)

logger = logging.getLogger(__name__)


class FullObEdgeFlightRecorder:
    """Event-driven capture sidecar. Does not open a second Full-OB WebSocket."""

    def __init__(
        self,
        settings: FlightRecorderSettings | None = None,
        *,
        profile_provider: ProfileProvider | None = None,
        full_book_manager: Any | None = None,
        mid_provider: Callable[[str], float | None] | None = None,
    ) -> None:
        self.settings = settings or load_flight_recorder_settings()
        self.full_book = full_book_manager
        self.mid_provider = mid_provider
        self.profiles = profile_provider
        self.watcher = EdgeWatcher(
            arm_bps=self.settings.arm_distance_bps,
            capture_bps=self.settings.capture_distance_bps,
            disarm_bps=self.settings.disarm_distance_bps,
            fast_approach_bps_per_sec=self.settings.fast_approach_bps_per_sec,
            cooldown_minutes=self.settings.cooldown_minutes,
            acceptance_hold_sec=self.settings.acceptance_hold_sec,
        )
        self._buffers: dict[str, BoundedRawRingBuffer] = {}
        self._writers: dict[str, ActiveEventWriter] = {}
        self._sinks: dict[str, NonBlockingDeltaSink] = {}
        self._plans: dict[str, CapturePlan] = {}
        self._lock = threading.RLock()
        self._last_profile_poll = datetime.fromtimestamp(0, tz=timezone.utc)
        self._attached = False
        self._bytes_written_total = 0
        self._started_at = datetime.now(timezone.utc)
        self.signal_count = 0
        self.bootstrap_observation_count = 0
        # Monotonic process-lifetime counters (never reset until process restart).
        self.process_lifetime_queue_drops = 0
        self.symbol_lifetime_queue_drops: dict[str, int] = {s: 0 for s in self.settings.symbols}
        self.last_event_queue_drops: dict[str, int] = {s: 0 for s in self.settings.symbols}
        self.total_research_ineligible_events = 0
        # Continuity / resync checkpoint state (per symbol).
        self._continuity_epoch: dict[str, int] = {}
        self._record_ordinal: dict[str, int] = {}
        self._awaiting_resync_checkpoint: dict[str, bool] = {}
        self._held_pre_checkpoint: dict[str, list[dict[str, Any]]] = {}
        self._epoch_prev_meta: dict[str, dict[str, Any]] = {}
        # Process-lifetime continuity metrics (never reset on segment rotate).
        self.transport_reconnect_count = 0
        self.resync_boundary_count = 0
        self.resync_checkpoint_attempt_count = 0
        self.resync_checkpoint_success_count = 0
        self.resync_checkpoint_failure_count = 0
        self.profile_signal_registry = ProfileSignalRegistry(
            arm_bps=self.settings.arm_distance_bps,
            capture_bps=self.settings.capture_distance_bps,
            disarm_bps=self.settings.disarm_distance_bps,
            window_minutes=self.settings.profile_window_minutes,
            max_active_profiles=self.settings.max_active_profile_watches,
        )
        self._metrics_lock = threading.Lock()
        self._ingress_messages = 0
        self._ingress_levels = 0
        self._metrics_window_started = self._started_at
        self._metrics_window_ingress_messages = 0
        self._metrics_window_ingress_levels = 0
        self._metrics_window_writer_messages = 0
        self._metrics_window_writer_bytes = 0
        self._last_rate_sample = {
            "ingress_messages_per_second": 0.0,
            "ingress_level_updates_per_second": 0.0,
            "writer_messages_per_second": 0.0,
            "writer_bytes_per_second": 0.0,
        }

    @property
    def enabled(self) -> bool:
        return self.settings.enabled

    def attach(self, full_book_manager: Any) -> None:
        self.full_book = full_book_manager
        if self.enabled and full_book_manager is not None and not self._attached:
            full_book_manager.add_observer(self.on_full_ob_message)
            self._attached = True

    def _buf(self, symbol: str) -> BoundedRawRingBuffer:
        sym = symbol.upper()
        b = self._buffers.get(sym)
        if b is None:
            b = BoundedRawRingBuffer(
                window_sec=self.settings.ringbuffer_minutes * 60.0,
                max_messages=self.settings.max_buffer_messages,
                max_bytes=self.settings.max_buffer_bytes,
            )
            self._buffers[sym] = b
        return b

    def _next_ordinal(self, symbol: str) -> int:
        sym = symbol.upper()
        n = self._record_ordinal.get(sym, 0) + 1
        self._record_ordinal[sym] = n
        return n

    def _epoch(self, symbol: str) -> int:
        return int(self._continuity_epoch.get(symbol.upper(), 0))

    def _segment_index(self, symbol: str) -> int:
        w = self._writers.get(symbol.upper())
        return 0 if w is None else int(w.continuation_index)

    def _enqueue(self, symbol: str, record: dict[str, Any]) -> bool:
        """Put one record on the long-lived sink. False = queue full / fail-closed."""
        sym = symbol.upper()
        sink = self._sinks.get(sym)
        plan = self._plans.get(sym)
        if sink is None:
            return False
        record = dict(record)
        record["_approx_bytes"] = int(record.get("_approx_bytes") or approx_envelope_bytes(record))
        if not sink.try_put(record):
            sink.writer.queue_drops += 1
            sink.writer.mark_incomplete("INCOMPLETE_QUEUE_DROP")
            self._note_queue_drop(sym)
            if plan is not None:
                plan.queue_drop_count += 1
                plan.note_incomplete("QUEUE_DROP")
                plan.writer_queue_drop_count = plan.queue_drop_count
            st = self.watcher.state(sym)
            st.transition(SymbolLifecycle.DEGRADED, ts=datetime.now(timezone.utc), reason="QUEUE_FULL")
            return False
        return True

    def _handle_reconnect_phase(
        self,
        *,
        symbol: str,
        payload: dict[str, Any],
        receive_time_ns: int,
        received_at: datetime,
    ) -> None:
        sym = symbol.upper()
        self.transport_reconnect_count += 1
        self._epoch_prev_meta[sym] = {
            "prev_u": payload.get("prev_u"),
            "prev_seq": payload.get("prev_seq"),
            "prev_exchange_ts_ms": payload.get("prev_exchange_ts_ms"),
            "prev_receive_time_ns": payload.get("prev_receive_time_ns"),
            "reason": payload.get("reason") or "transport_reconnect",
            "disconnect_ts": payload.get("disconnect_ts"),
            "reconnect_ts": payload.get("reconnect_ts"),
            "reconnect_count": payload.get("reconnect_count"),
        }
        plan = self._plans.get(sym)
        sink = self._sinks.get(sym)
        # Prebuffer-only: clear ringbuffer so later events cannot claim full prebuffer.
        if sink is None:
            buf = self._buf(sym)
            buf.clear()
            st = self.watcher.state(sym)
            st.pre_trigger_incomplete = True
            return
        if plan is None:
            return
        self.resync_boundary_count += 1
        plan.transport_reconnect_count = getattr(plan, "transport_reconnect_count", 0) + 1
        plan.resync_boundary_count = getattr(plan, "resync_boundary_count", 0) + 1
        plan.unobserved_interval_count = getattr(plan, "unobserved_interval_count", 0) + 1
        plan.continuous_capture = False
        plan.note_incomplete("RESYNC")
        plan.note_incomplete("UNOBSERVED_TRANSPORT_GAP")
        meta = self._epoch_prev_meta[sym]
        # Close prior epoch id stays; new epoch starts only after successful checkpoint.
        boundary = build_resync_boundary_record(
            fight_event_id=plan.fight_event_id,
            continuity_epoch_id=self._epoch(sym),
            record_ordinal=self._next_ordinal(sym),
            symbol=sym,
            segment_index=self._segment_index(sym),
            reason=str(meta.get("reason") or "transport_reconnect"),
            prev_u=meta.get("prev_u"),
            prev_seq=meta.get("prev_seq"),
            prev_exchange_ts_ms=meta.get("prev_exchange_ts_ms"),
            prev_receive_time_ns=meta.get("prev_receive_time_ns"),
            disconnect_ts_iso=str(meta.get("disconnect_ts") or iso(received_at)),
            reconnect_ts_iso=str(meta.get("reconnect_ts") or iso(received_at)),
            receive_time_ns=receive_time_ns,
        )
        if not self._enqueue(sym, boundary):
            plan.checkpoint_persist_failed = True
            plan.resync_checkpoint_failure_count = getattr(plan, "resync_checkpoint_failure_count", 0) + 1
            plan.replayable_by_epochs = False
            plan.research_eligible = False
            self.resync_checkpoint_failure_count += 1
            self._awaiting_resync_checkpoint[sym] = True
            self._held_pre_checkpoint[sym] = []
            return
        self._awaiting_resync_checkpoint[sym] = True
        self._held_pre_checkpoint[sym] = []

    def _handle_resync_ready(
        self,
        *,
        symbol: str,
        payload: dict[str, Any],
        receive_time_ns: int,
    ) -> None:
        sym = symbol.upper()
        plan = self._plans.get(sym)
        sink = self._sinks.get(sym)
        if plan is None or sink is None:
            self._awaiting_resync_checkpoint[sym] = False
            self._held_pre_checkpoint.pop(sym, None)
            return
        if not self._awaiting_resync_checkpoint.get(sym):
            # Initial subscribe without an open boundary (e.g. first book ready) — ignore mid-flight.
            return
        self.resync_checkpoint_attempt_count += 1
        plan.resync_checkpoint_attempt_count = getattr(plan, "resync_checkpoint_attempt_count", 0) + 1
        meta = self._epoch_prev_meta.get(sym) or {}
        new_epoch = self._epoch(sym) + 1
        snap = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if not isinstance(snap, dict) or not (snap.get("b") or snap.get("a")):
            plan.checkpoint_persist_failed = True
            plan.resync_checkpoint_failure_count = getattr(plan, "resync_checkpoint_failure_count", 0) + 1
            plan.replayable_by_epochs = False
            plan.research_eligible = False
            self.resync_checkpoint_failure_count += 1
            self._held_pre_checkpoint[sym] = []
            self._awaiting_resync_checkpoint[sym] = True  # keep gate closed
            return
        # Also persist side-car file for this epoch (immutable copy on disk).
        try:
            sink.writer.write_resync_checkpoint(snap, epoch_id=new_epoch)
        except Exception:
            logger.exception("fr_resync_checkpoint_file_failed %s", sym)
        ck = build_checkpoint_record(
            record_kind=RECORD_RESYNC_CHECKPOINT,
            fight_event_id=plan.fight_event_id,
            continuity_epoch_id=new_epoch,
            record_ordinal=self._next_ordinal(sym),
            symbol=sym,
            topic=str(payload.get("topic") or f"orderbook.full.{sym}"),
            snapshot=snap,
            receive_time_ns=receive_time_ns,
            segment_index=self._segment_index(sym),
            resync_reason=str(meta.get("reason") or "transport_reconnect"),
            reconnect_count=meta.get("reconnect_count"),
            prev_u=meta.get("prev_u"),
            prev_seq=meta.get("prev_seq"),
        )
        # Large checkpoint: approx bytes from levels.
        ck["_approx_bytes"] = 256 + 28 * (len(snap.get("b") or []) + len(snap.get("a") or []))
        if not self._enqueue(sym, ck):
            plan.checkpoint_persist_failed = True
            plan.resync_checkpoint_failure_count = getattr(plan, "resync_checkpoint_failure_count", 0) + 1
            plan.replayable_by_epochs = False
            plan.research_eligible = False
            self.resync_checkpoint_failure_count += 1
            # Fail-closed: do NOT open a new epoch or flush held deltas as valid.
            self._held_pre_checkpoint[sym] = []
            self._awaiting_resync_checkpoint[sym] = True
            return
        self._continuity_epoch[sym] = new_epoch
        plan.continuity_epoch_count = new_epoch + 1
        plan.resync_checkpoint_success_count = getattr(plan, "resync_checkpoint_success_count", 0) + 1
        plan.replayable_by_epochs = not plan.checkpoint_persist_failed
        plan.continuous_capture = False
        plan.research_eligible = False
        self.resync_checkpoint_success_count += 1
        self._awaiting_resync_checkpoint[sym] = False
        held = self._held_pre_checkpoint.pop(sym, [])
        for rec in held:
            annotated = annotate_delta_record(
                rec,
                fight_event_id=plan.fight_event_id,
                continuity_epoch_id=new_epoch,
                record_ordinal=self._next_ordinal(sym),
                segment_index=self._segment_index(sym),
            )
            if not self._enqueue(sym, annotated):
                plan.note_incomplete("QUEUE_DROP")
                break

    def on_full_ob_message(
        self,
        *,
        symbol: str,
        payload: dict[str, Any],
        received_at: datetime,
        receive_time_ns: int,
        phase: str,
        outcome: str | None = None,
        runtime: Any = None,
    ) -> None:
        if not self.enabled or not self.settings.should_watch(symbol):
            return
        sym = symbol.upper()
        if phase == "reconnect":
            self._handle_reconnect_phase(
                symbol=sym,
                payload=payload if isinstance(payload, dict) else {},
                receive_time_ns=receive_time_ns,
                received_at=received_at,
            )
            return
        if phase == "resync_ready":
            self._handle_resync_ready(
                symbol=sym,
                payload=payload if isinstance(payload, dict) else {},
                receive_time_ns=receive_time_ns,
            )
            return

        # Exactly one compact envelope per Bybit WS delta packet.
        record = build_delta_envelope(
            payload,
            receive_time_ns=receive_time_ns,
            phase=phase,
            outcome=outcome,
        )
        record["_approx_bytes"] = approx_envelope_bytes(record)
        levels = int(record.get("level_update_count") or level_update_count(record))
        with self._metrics_lock:
            self._ingress_messages += 1
            self._ingress_levels += levels
            self._metrics_window_ingress_messages += 1
            self._metrics_window_ingress_levels += levels
        buf = self._buf(symbol)
        with self._lock:
            sink = self._sinks.get(sym)
            plan = self._plans.get(sym)
            if sink is not None and plan is not None:
                # Gate: while awaiting checkpoint, hold deltas (do not persist before seed).
                if self._awaiting_resync_checkpoint.get(sym):
                    if plan.checkpoint_persist_failed:
                        return  # fail-closed: drop rather than fake a valid epoch
                    held = self._held_pre_checkpoint.setdefault(sym, [])
                    if len(held) < 50_000:
                        held.append(record)
                    return
                annotated = annotate_delta_record(
                    record,
                    fight_event_id=plan.fight_event_id,
                    continuity_epoch_id=self._epoch(sym),
                    record_ordinal=self._next_ordinal(sym),
                    segment_index=self._segment_index(sym),
                )
                if not self._enqueue(sym, annotated):
                    return
                self._note_apply_outcome(symbol, outcome, received_at)
                return
            if str(payload.get("type") or "delta").lower() == "delta" or phase == "buffer":
                before_ov = buf.overflow_count
                buf.append(record, kind="delta", receive_time_ns=receive_time_ns)
                if buf.overflow_count > before_ov:
                    st = self.watcher.state(symbol)
                    st.transition(SymbolLifecycle.DEGRADED, ts=received_at, reason="BUFFER_OVERFLOW")

    def _note_apply_outcome(self, symbol: str, outcome: str | None, ts: datetime) -> None:
        plan = self._plans.get(symbol.upper())
        if plan is None or not outcome:
            return
        if outcome in {"gap"}:
            plan.u_gap_count += 1
            plan.apply_epoch_u_gap_count = getattr(plan, "apply_epoch_u_gap_count", 0) + 1
            plan.note_incomplete("U_GAP")
            plan.add_marker("U_GAP", ts, apply_outcome=outcome)
        elif outcome in {"u_reset"}:
            plan.note_incomplete("RESYNC")
            plan.add_marker("RESYNC", ts, apply_outcome=outcome)
        elif outcome in {"ignored_stale_u"}:
            plan.stale_update_count += 1

    def ensure_keeper_lease(self, symbol: str) -> None:
        if self.full_book is None:
            return
        lid = f"{KEEPER_LEASE_PREFIX}{symbol.upper()}"
        try:
            self.full_book._acquire(symbol=symbol.upper(), lease_id=lid)
        except Exception as exc:
            logger.warning("fr_keeper_acquire_failed %s %s", symbol, exc)

    def release_keeper_lease(self, symbol: str) -> None:
        if self.full_book is None:
            return
        lid = f"{KEEPER_LEASE_PREFIX}{symbol.upper()}"
        try:
            self.full_book._release(lid)
        except Exception:
            pass

    def poll_profiles(self, now: datetime | None = None) -> None:
        if self.profiles is None:
            return
        now = now or datetime.now(timezone.utc)
        if (now - self._last_profile_poll).total_seconds() < self.settings.profile_poll_sec:
            return
        self._last_profile_poll = now
        for sym in self.settings.symbols:
            try:
                bundle = self.profiles.load(sym, now)
            except Exception:
                logger.exception("fr_profile_load_failed %s", sym)
                continue
            if bundle is None:
                continue
            self.watcher.set_edges(
                sym,
                bundle.edges,
                {
                    "profile_id": bundle.profile_id,
                    "session_start": bundle.session_start.isoformat(),
                    "cutoff": bundle.cutoff.isoformat(),
                    **bundle.meta,
                },
            )

    def tick(self, now: datetime | None = None) -> None:
        if not self.enabled:
            return
        now = now or datetime.now(timezone.utc)
        self.poll_profiles(now)
        if self.full_book is None:
            return
        for sym in self.settings.symbols:
            # Shadow pilot: keep Full-OB alive without an open chart.
            self.ensure_keeper_lease(sym)
            rt = self.full_book.runtimes.get(sym) if self.full_book is not None else None
            mid = None
            book_ready = False
            if rt is not None and rt.book.book_ready:
                mid = rt.book.mid()
                book_ready = True
                st = self.watcher.state(sym)
                if st.lifecycle in {
                    SymbolLifecycle.IDLE,
                    SymbolLifecycle.SYNCING,
                    SymbolLifecycle.SUBSCRIBING,
                    SymbolLifecycle.COOLDOWN,
                } and book_ready:
                    # Reflect sync readiness without forcing ARMED.
                    if st.lifecycle in {SymbolLifecycle.SYNCING, SymbolLifecycle.SUBSCRIBING}:
                        st.transition(SymbolLifecycle.BOOK_READY, ts=now, reason="book_ready")
            if mid is None and self.mid_provider is not None:
                try:
                    mid = self.mid_provider(sym)
                except Exception:
                    mid = None
            if mid is None:
                continue
            decision = self.watcher.evaluate(sym, mid, now)
            st = self.watcher.state(sym)
            if decision.action == "arm":
                st.transition(SymbolLifecycle.SUBSCRIBING, ts=now, reason="activate_full_ob")
                self.ensure_keeper_lease(sym)
                st.transition(SymbolLifecycle.SYNCING, ts=now, reason="await_sync")
                if book_ready:
                    st.transition(SymbolLifecycle.BOOK_READY, ts=now, reason="already_ready")
                    st.transition(SymbolLifecycle.ARMED, ts=now, reason="arm_distance")
            elif decision.action == "disarm":
                pass
            elif decision.action == "bootstrap_observe":
                self._note_bootstrap_observation(sym, decision, now)
            elif decision.action == "trigger":
                self._start_or_merge_event(sym, decision, now, book_ready=book_ready)
            elif decision.action == "extend":
                self._handle_open_event_tick(sym, decision, now)
                rt = self.full_book.runtimes.get(sym) if self.full_book else None
                recv_ns = None if rt is None else getattr(rt.book, "last_receive_time_ns", None)
                self._evaluate_nested_profile_signals(sym, mid=mid, now=now, receive_time_ns=recv_ns)
                self._maybe_rotate_segment(sym, now)
                self._maybe_end_event(sym, now)

            self.ensure_keeper_lease(sym)
            self._maybe_rotate_segment(sym, now)

    def _note_bootstrap_observation(self, symbol: str, decision: Any, now: datetime) -> None:
        """Audit-only: never open .tmp / JSONL.zst for BOOTSTRAP_ALREADY_IN_EDGE_ZONE."""
        sym = symbol.upper()
        st = self.watcher.state(sym)
        self.bootstrap_observation_count += 1
        logger.info(
            "fr_bootstrap_observe symbol=%s edge=%s mid=%s persistent_capture=false",
            sym,
            None if decision.edge is None else decision.edge.kind,
            None if decision.sample is None else decision.sample.mid,
        )
        # Keep Full-OB in RAM ringbuffer only; no writer, no event dir.
        assert sym not in self._writers
        st.bootstrap_status = "BOOTSTRAP_ALREADY_IN_EDGE_ZONE"

    def _note_queue_drop(self, symbol: str, *, n: int = 1) -> None:
        sym = symbol.upper()
        self.process_lifetime_queue_drops += int(n)
        self.symbol_lifetime_queue_drops[sym] = self.symbol_lifetime_queue_drops.get(sym, 0) + int(n)

    def _put_marker(self, symbol: str, record: dict[str, Any]) -> None:
        sym = symbol.upper()
        plan = self._plans.get(sym)
        if plan is not None:
            record = annotate_marker_record(
                record,
                fight_event_id=plan.fight_event_id,
                continuity_epoch_id=self._epoch(sym),
                record_ordinal=self._next_ordinal(sym),
                segment_index=self._segment_index(sym),
            )
        if not self._enqueue(sym, record):
            return

    def _event_root_for(self, writer: ActiveEventWriter) -> Path:
        return writer.directory if writer.continuation_index == 0 else writer.directory.parent

    def _write_event_manifest(self, plan: CapturePlan, writer: ActiveEventWriter, *, extra: dict[str, Any] | None = None) -> None:
        import hashlib
        import json

        root = self._event_root_for(writer)
        body = plan.to_manifest_fields()
        if extra:
            body.update(extra)
        raw = json.dumps(body, indent=2, sort_keys=True, default=str).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        body["event_manifest_sha256"] = digest
        (root / "event_manifest.json").write_text(
            json.dumps(body, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )

    def _register_nested_profile_from_pending(
        self,
        symbol: str,
        upd: dict[str, Any],
        *,
        mid: float | None,
        now: datetime,
    ) -> None:
        if not self.settings.nested_profile_signals_enabled:
            return
        sym = symbol.upper()
        plan = self._plans.get(sym)
        if plan is None:
            return
        prof = dict(upd.get("profile") or {})
        edges = upd.get("edges") or ()
        if not prof or not edges:
            return
        pid = str(prof.get("profile_id") or "")
        if pid and pid == str(plan.profile_contract_version or ""):
            return
        try:
            cutoff_s = prof.get("cutoff")
            start_s = prof.get("session_start")
            if not cutoff_s or not start_s:
                return
            cutoff = datetime.fromisoformat(str(cutoff_s).replace("Z", "+00:00"))
            start = datetime.fromisoformat(str(start_s).replace("Z", "+00:00"))
            if plan.profile_cutoff_ts:
                parent_cut = datetime.fromisoformat(str(plan.profile_cutoff_ts).replace("Z", "+00:00"))
                if cutoff <= parent_cut:
                    return
        except Exception:
            return
        self.profile_signal_registry.register_profile(
            symbol=sym,
            session_start=start,
            cutoff=cutoff,
            edges=tuple(edges),
            meta=prof,
            now=now,
            mid=mid,
        )

    def _evaluate_nested_profile_signals(
        self,
        symbol: str,
        *,
        mid: float,
        now: datetime,
        receive_time_ns: int | None,
    ) -> None:
        if not self.settings.nested_profile_signals_enabled:
            return
        sym = symbol.upper()
        plan = self._plans.get(sym)
        if plan is None or sym not in self._writers:
            return
        by_sym = self.profile_signal_registry._profiles.get(sym, {})
        if not by_sym:
            return
        plan.recompute_research_flags()
        capture_continuous = bool(plan.continuous_capture)
        capture_research = bool(plan.research_eligible)
        for st in list(by_sym.values()):
            if not st.signal_eligible:
                continue
            tick = self.profile_signal_registry.evaluate_profile(st, mid=mid, now=now)
            if tick.lifecycle is not ProfileSignalLifecycle.PROFILE_CROSS_IN:
                continue
            sig = self.profile_signal_registry.build_signal_if_cross(
                st,
                mid=mid,
                now=now,
                parent_fight_event_id=plan.fight_event_id,
                continuity_epoch_id=self._epoch(sym),
                parent_segment_index=self._segment_index(sym),
                receive_time_ns=receive_time_ns,
                prior_zone=tick.zone_state or "UNKNOWN",
                capture_continuous=capture_continuous,
                capture_research_eligible=capture_research,
            )
            if sig is not None:
                self._emit_nested_signal(sym, sig, plan, now)

    def _emit_nested_signal(self, symbol: str, sig: Any, plan: CapturePlan, now: datetime) -> None:
        sym = symbol.upper()
        body = sig.to_dict()
        # Immutable per-signal analysis window (timing contract: pre=ringbuffer, post=min_post).
        analysis = contract_from_nested_signal_dict(
            body,
            pre_seconds=self.settings.pre_seconds,
            min_post_seconds=self.settings.min_post_seconds,
            capture_available_until=plan.hard_capture_end_ts,
            parent_continuous_capture=bool(plan.continuous_capture),
            parent_replayable=bool(plan.replayable_by_epochs),
        )
        body.update(
            {
                "analysis_isolation_contract": ANALYSIS_ISOLATION_CONTRACT,
                "analysis_pre_start_ts": analysis.analysis_pre_start_ts,
                "analysis_post_end_ts": analysis.analysis_post_end_ts,
                "coverage_status": analysis.coverage_status,
                "signal_research_eligible": analysis.research_eligible,
                "research_ineligible_reasons": list(analysis.research_ineligible_reasons),
            }
        )
        if "INSUFFICIENT_SIGNAL_POST_COVERAGE" in analysis.research_ineligible_reasons:
            body["signal_research_eligible"] = False
            body["signal_research_ineligible_reason"] = "INSUFFICIENT_SIGNAL_POST_COVERAGE"
        plan.nested_signals.append(body)
        plan.nested_signal_count = len(plan.nested_signals)
        self._refresh_signal_analysis_contracts(plan)
        self._append_nested_ledger(sym, body)
        self._append_analysis_contracts_ledger(sym, plan)
        rec = plan.add_marker("NESTED_PROFILE_EDGE_SIGNAL", now, nested_signal=body)
        if not self._put_marker(sym, rec):
            plan.note_incomplete("NESTED_SIGNAL_QUEUE_DROP")
            logger.error(
                "fr_nested_signal_queue_drop symbol=%s nested_signal_id=%s",
                sym,
                body.get("nested_signal_id"),
            )
            return
        self._maybe_extend_for_nested_signal(sym, plan, str(body.get("nested_signal_id")), now)
        logger.info(
            "fr_nested_signal symbol=%s id=%s profile=%s edge=%s",
            sym,
            body.get("nested_signal_id"),
            body.get("profile_id"),
            body.get("edge"),
        )

    def _refresh_signal_analysis_contracts(self, plan: CapturePlan) -> None:
        """Rebuild deterministic overlap clusters for parent + nested signals."""
        contracts = [
            contract_from_parent_plan(
                plan,
                pre_seconds=self.settings.pre_seconds,
                min_post_seconds=self.settings.min_post_seconds,
            )
        ]
        for body in plan.nested_signals:
            contracts.append(
                contract_from_nested_signal_dict(
                    body,
                    pre_seconds=self.settings.pre_seconds,
                    min_post_seconds=self.settings.min_post_seconds,
                    capture_available_until=plan.hard_capture_end_ts,
                    parent_continuous_capture=bool(plan.continuous_capture),
                    parent_replayable=bool(plan.replayable_by_epochs),
                )
            )
        clustered = assign_overlap_clusters(contracts)
        plan.signal_analysis_contracts = [c.to_dict() for c in clustered]
        # Propagate overlap ids back onto nested signal bodies (namespaced, immutable profile fields).
        by_id = {c.signal_id: c for c in clustered}
        for body in plan.nested_signals:
            sid = str(body.get("nested_signal_id") or "")
            c = by_id.get(sid)
            if c is None:
                continue
            body["overlap_cluster_id"] = c.overlap_cluster_id
            body["overlapping_signal_ids"] = list(c.overlapping_signal_ids)
            body["independent_observation"] = c.independent_observation

    def _append_nested_ledger(self, symbol: str, body: dict[str, Any]) -> None:
        import json

        writer = self._writers.get(symbol.upper())
        if writer is None:
            return
        root = self._event_root_for(writer)
        ledger = root / "nested_profile_signals.jsonl"
        try:
            with ledger.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(body, sort_keys=True) + "\n")
        except Exception:
            logger.exception("fr_nested_ledger_write_failed %s", symbol)

    def _append_analysis_contracts_ledger(self, symbol: str, plan: CapturePlan) -> None:
        import json

        writer = self._writers.get(symbol.upper())
        if writer is None:
            return
        root = self._event_root_for(writer)
        path = root / "signal_analysis_contracts.jsonl"
        try:
            with path.open("w", encoding="utf-8") as fh:
                for row in plan.signal_analysis_contracts:
                    fh.write(json.dumps(row, sort_keys=True) + "\n")
        except Exception:
            logger.exception("fr_analysis_contracts_write_failed %s", symbol)

    def _maybe_extend_for_nested_signal(
        self,
        symbol: str,
        plan: CapturePlan,
        nested_signal_id: str,
        now: datetime,
    ) -> None:
        if nested_signal_id in plan.nested_extension_applied_ids:
            return
        if now >= plan.hard_capture_end_ts:
            return
        nxt = min(
            plan.normal_end_ts + timedelta(seconds=self.settings.extension_seconds),
            plan.hard_capture_end_ts,
        )
        if nxt <= plan.normal_end_ts:
            return
        plan.nested_extension_applied_ids.append(nested_signal_id)
        plan.extension_count += 1
        plan.extension_reason = "NESTED_PROFILE_EDGE_SIGNAL"
        plan.normal_end_ts = nxt
        rec = plan.add_marker(
            "EXTENSION",
            now,
            extension_count=plan.extension_count,
            extension_reason="NESTED_PROFILE_EDGE_SIGNAL",
            nested_signal_id=nested_signal_id,
            normal_end_ts=iso(plan.normal_end_ts),
        )
        self._put_marker(symbol, rec)

    def _handle_open_event_tick(self, symbol: str, decision: Any, now: datetime) -> None:
        sym = symbol.upper()
        plan = self._plans.get(sym)
        st = self.watcher.state(sym)
        if plan is None:
            return
        if st.pending_profile_update is not None:
            upd = st.pending_profile_update
            st.pending_profile_update = None
            mid = None
            sample = getattr(decision, "sample", None)
            if sample is not None:
                mid = getattr(sample, "mid", None)
            self._register_nested_profile_from_pending(sym, upd, mid=mid, now=now)
            rec = plan.add_marker(
                "PROFILE_UPDATE_DURING_CAPTURE",
                now,
                new_profile=upd.get("profile"),
                frozen_edge_price=plan.edge_price_at_trigger,
            )
            self._put_marker(sym, rec)
        if st.result_ts is not None and plan.result_ts is None:
            plan.result_ts = st.result_ts
            plan.result_kind = st.result_kind
            rec = plan.add_marker(
                "RESULT",
                st.result_ts,
                result_kind=st.result_kind,
            )
            self._put_marker(sym, rec)
            plan.normal_end_ts = max(
                plan.normal_end_ts,
                compute_normal_end(
                    minimum_capture_end_ts=plan.minimum_capture_end_ts,
                    result_ts=plan.result_ts,
                    result_tail_seconds=self.settings.result_tail_seconds,
                ),
            )
        marker = getattr(decision, "marker", None)
        if marker == "EDGE_RETOUCH":
            plan.retouch_count += 1
            rec = plan.add_marker("EDGE_RETOUCH", now, retouch_count=plan.retouch_count)
            self._put_marker(sym, rec)
        elif marker == "SECONDARY_EDGE_TRIGGER":
            plan.secondary_edge_observation_count += 1
            self.profile_signal_registry.note_secondary_observation()
            if not self.settings.nested_profile_signals_enabled:
                plan.secondary_edge_count += 1
                rec = plan.add_marker(
                    "SECONDARY_EDGE_TRIGGER",
                    now,
                    edge=None if decision.edge is None else {"kind": decision.edge.kind, "price": decision.edge.price},
                )
                self._put_marker(sym, rec)

    def _start_or_merge_event(self, symbol: str, decision: Any, now: datetime, *, book_ready: bool) -> None:
        sym = symbol.upper()
        with self._lock:
            if sym in self._writers:
                self._handle_open_event_tick(sym, decision, now)
                return
            trigger_source = getattr(decision, "trigger_source", None) or "CROSS_IN"
            # Hard gate: only real CROSS_IN opens persistent capture.
            if trigger_source != "CROSS_IN" or not getattr(decision, "edge_entry_crossed", False):
                logger.warning(
                    "fr_refuse_non_cross_in_capture symbol=%s source=%s crossed=%s",
                    sym,
                    trigger_source,
                    getattr(decision, "edge_entry_crossed", False),
                )
                if trigger_source == "BOOTSTRAP_ALREADY_IN_EDGE_ZONE":
                    self._note_bootstrap_observation(sym, decision, now)
                return
            if len(self._writers) >= self.settings.max_parallel_events:
                logger.warning("fr_max_parallel_events symbol=%s", sym)
                return
            if not book_ready:
                self.watcher.state(sym).pre_trigger_incomplete = True
            event_id = new_event_id(sym, now)
            self.signal_count += 1
            stw = self.watcher.state(sym)
            rt = self.full_book.runtimes.get(sym) if self.full_book else None
            book = None if rt is None else getattr(rt, "book", None)
            trigger_u = None if book is None else getattr(book, "update_id", None)
            trigger_seq = None if book is None else getattr(book, "seq", None)
            trigger_ns = None if book is None else getattr(book, "last_receive_time_ns", None)
            edge = decision.edge
            edge_kind = None if edge is None else edge.kind
            edge_type = "UPPER" if edge_kind and edge_kind.endswith("VAH") else ("LOWER" if edge_kind and edge_kind.endswith("VAL") else "UNKNOWN")
            edge_price = None if edge is None else edge.price
            prof = dict(stw.frozen_profile)
            mid = None if decision.sample is None else decision.sample.mid
            dist = None if decision.sample is None else decision.sample.distance_bps
            dedup_key = f"{sym}|{edge_kind}|{edge_price}|{now.strftime('%Y%m%dT%H%M')}"
            min_end = compute_minimum_capture_end(now, self.settings.min_post_seconds)
            hard_end = compute_hard_capture_end(now, self.settings.max_seconds)
            plan = CapturePlan(
                fight_event_id=event_id,
                symbol_event_id=event_id,
                symbol=sym,
                trigger_ts=now,
                trigger_receive_time_ns=trigger_ns,
                trigger_u=trigger_u,
                trigger_seq=trigger_seq,
                trigger_source=trigger_source,
                edge=str(edge_kind or ""),
                edge_type=edge_type,
                edge_price=edge_price,
                edge_price_at_trigger=edge_price,
                profile_session_start=str(prof.get("session_start") or ""),
                profile_cutoff_ts=str(prof.get("cutoff") or ("" if edge is None else edge.cutoff.isoformat())),
                profile_contract_version=str(prof.get("profile_id") or CONTRACT_VERSION),
                market_price_at_trigger=mid,
                distance_to_edge_bps=dist,
                prior_zone_state=getattr(decision, "prior_zone_state", None) or "UNKNOWN",
                trigger_zone_state=getattr(decision, "trigger_zone_state", None) or "UNKNOWN",
                edge_entry_crossed=bool(getattr(decision, "edge_entry_crossed", False)),
                bootstrap_status=getattr(decision, "bootstrap_status", None) or "N/A",
                dedup_key=dedup_key,
                minimum_capture_end_ts=min_end,
                hard_capture_end_ts=hard_end,
                normal_end_ts=min_end,
                trigger_edge=edge,
                research_eligible=True,
                trigger_quality="REAL_CROSS_IN",
                bootstrap_persistent_capture=False,
            )
            edges = stw.frozen_edges or self.watcher._edges.get(sym) or ()
            if decision.edge is not None and not edges:
                edges = (decision.edge,)
            self.watcher.begin_capture(sym, event_id, tuple(edges), now)
            directory = event_dir(self.settings.capture_root, sym, now, event_id)
            writer = ActiveEventWriter(
                event_id=event_id,
                symbol=sym,
                directory=directory,
                started_at=now,
                trigger_reason=decision.reason,
                trigger_meta={
                    "trigger_time": now.isoformat().replace("+00:00", "Z"),
                    "trigger_source": trigger_source,
                    "edge": None
                    if decision.edge is None
                    else {"kind": decision.edge.kind, "price": decision.edge.price},
                    "distance_bps": dist,
                    "mid": mid,
                    "edge_type": edge_type,
                    "bootstrap_status": getattr(decision, "bootstrap_status", None) or "N/A",
                    "dedup_key": dedup_key,
                },
                profile_context=dict(self.watcher.state(sym).frozen_profile),
                config_snapshot={
                    "arm_distance_bps": self.settings.arm_distance_bps,
                    "capture_distance_bps": self.settings.capture_distance_bps,
                    "disarm_distance_bps": self.settings.disarm_distance_bps,
                    "ringbuffer_minutes": self.settings.ringbuffer_minutes,
                    "minimum_post_capture_minutes": self.settings.minimum_post_capture_minutes,
                    "reclaim_post_capture_minutes": self.settings.reclaim_post_capture_minutes,
                    "maximum_event_minutes": self.settings.maximum_event_minutes,
                    "extension_minutes": self.settings.extension_minutes,
                    "cooldown_minutes": self.settings.cooldown_minutes,
                    "capture_root": str(self.settings.capture_root),
                    "symbols": sorted(self.settings.symbols),
                },
                continuation_index=0,
                fight_event_id=event_id,
            )
            # Path checks
            root = Path(self.settings.capture_root)
            root.mkdir(parents=True, exist_ok=True)
            if not os_access_writable(root):
                self.watcher.state(sym).transition(SymbolLifecycle.UNAVAILABLE, ts=now, reason="disk_not_writable")
                return
            writer.open()
            flushed = self._buf(sym).flush()
            if flushed:
                writer.prebuffer_start_ns = flushed[0].receive_time_ns
                first_ts = datetime.fromtimestamp(flushed[0].receive_time_ns / 1_000_000_000, tz=timezone.utc)
                p_ts = flushed[0].payload.get("ts") or (flushed[0].payload.get("data") or {}).get("ts")
                if p_ts is not None:
                    first_ts = datetime.fromtimestamp(int(p_ts) / 1000.0, tz=timezone.utc)
                plan.first_persisted_ts = first_ts
                plan.pre_trigger_seconds_actual = max(0.0, (now - first_ts).total_seconds())
            plan.process_uptime_at_trigger_sec = (now - self._started_at).total_seconds()
            if plan.first_persisted_ts is None:
                plan.note_incomplete("PREBUFFER_EMPTY")
                stw.pre_trigger_incomplete = True
            elif plan.first_persisted_ts > now:
                plan.note_incomplete("PREBUFFER_TS_INVERSION")
            rt = self.full_book.runtimes.get(sym) if self.full_book else None
            if rt is not None and rt.last_rest_snapshot:
                writer.write_rest_snapshot(rt.last_rest_snapshot)
            else:
                writer.status = "NO_VALID_INITIAL_SNAPSHOT"
            sink = NonBlockingDeltaSink(
                writer,
                queue_size=self.settings.queue_size,
                batch_max_messages=self.settings.writer_batch_max_messages,
                batch_max_bytes=self.settings.writer_batch_max_bytes,
                flush_interval_sec=self.settings.writer_flush_interval_sec,
            )
            self._writers[sym] = writer
            self._sinks[sym] = sink
            self._plans[sym] = plan
            self._refresh_signal_analysis_contracts(plan)
            self._append_analysis_contracts_ledger(sym, plan)
            self._continuity_epoch[sym] = 0
            self._record_ordinal[sym] = 0
            self._awaiting_resync_checkpoint[sym] = False
            self._held_pre_checkpoint[sym] = []
            plan.continuity_epoch_count = 1
            plan.continuous_capture = True
            plan.replayable_by_epochs = True
            plan.apply_epoch_u_gap_count = 0
            plan.transport_reconnect_count = 0
            plan.resync_boundary_count = 0
            plan.unobserved_interval_count = 0
            plan.resync_checkpoint_attempt_count = 0
            plan.resync_checkpoint_success_count = 0
            plan.resync_checkpoint_failure_count = 0
            plan.checkpoint_persist_failed = False
            plan.writer_queue_drop_count = 0
            # INITIAL_CHECKPOINT must precede flushed prebuffer deltas.
            if rt is not None and rt.last_rest_snapshot:
                init_ck = build_checkpoint_record(
                    record_kind=RECORD_INITIAL_CHECKPOINT,
                    fight_event_id=plan.fight_event_id,
                    continuity_epoch_id=0,
                    record_ordinal=self._next_ordinal(sym),
                    symbol=sym,
                    topic=f"orderbook.full.{sym}",
                    snapshot=rt.last_rest_snapshot,
                    receive_time_ns=trigger_ns,
                    segment_index=0,
                    resync_reason=None,
                )
                init_ck["_approx_bytes"] = 256 + 28 * (
                    len(rt.last_rest_snapshot.get("b") or [])
                    + len(rt.last_rest_snapshot.get("a") or [])
                )
                if not self._enqueue(sym, init_ck):
                    plan.checkpoint_persist_failed = True
                    plan.replayable_by_epochs = False
                    plan.note_incomplete("CHECKPOINT_PERSIST_FAILED")
            for item in flushed:
                annotated = annotate_delta_record(
                    item.payload,
                    fight_event_id=plan.fight_event_id,
                    continuity_epoch_id=0,
                    record_ordinal=self._next_ordinal(sym),
                    segment_index=0,
                )
                if not self._enqueue(sym, annotated):
                    break
            if self._buf(sym).overflow_count:
                writer.buffer_overflow = self._buf(sym).overflow_count
            if self.watcher.state(sym).pre_trigger_incomplete:
                writer.coverage["PRE_TRIGGER_FULL_OB_INCOMPLETE"] = True
            writer.coverage["rpi_included"] = RPI_INCLUDED_IN_FULL_OB
            writer.coverage["public_trades"] = {
                "mode": "canonical_reference_preferred",
                "live_ws_publicTrade": False,
                "note": "Shadow V1 writes empty public_trades_raw.jsonl.zst placeholder; "
                "correlate via public_trades_canonical using event time range in manifest.",
            }
            writer.lifecycle = list(self.watcher.state(sym).lifecycle_log)
            if rt is not None:
                writer.gap_count = rt.gap_count
                writer.reconnect_count = rt.reconnect_count
            stw.trigger_edge = edge
            writer.extra_manifest = plan.to_manifest_fields()
            plan.segments.append(
                SegmentRecord(
                    continuation_index=0,
                    directory=str(directory),
                    next_segment_expected=True,
                )
            )
            self._write_event_manifest(plan, writer)

    def _maybe_end_event(self, symbol: str, now: datetime) -> None:
        sym = symbol.upper()
        st = self.watcher.state(sym)
        writer = self._writers.get(sym)
        plan = self._plans.get(sym)
        if writer is None or plan is None or st.capture_started_at is None:
            return
        if st.result_ts is not None and plan.result_ts is None:
            plan.result_ts = st.result_ts
            plan.result_kind = st.result_kind
        # Monotonic: never rewind an already-extended normal_end_ts.
        base_end = compute_normal_end(
            minimum_capture_end_ts=plan.minimum_capture_end_ts,
            result_ts=plan.result_ts,
            result_tail_seconds=self.settings.result_tail_seconds,
        )
        plan.normal_end_ts = max(plan.normal_end_ts, base_end)
        sample = st.last_sample
        in_zone = bool(sample and in_edge_zone(sample.zone_state))
        result_tail_active = bool(
            plan.result_ts is not None
            and now < plan.result_ts + timedelta(seconds=self.settings.result_tail_seconds)
        )
        ext_reason = classify_fight_extension(
            in_edge_zone=in_zone,
            outside_open=st.outside_since is not None,
            reclaim_unresolved=bool(st.reclaim_seen and st.outside_since is None and not st.acceptance_active and in_zone),
            acceptance_pending=bool(st.acceptance_active),
            result_tail_active=result_tail_active,
        )
        if now >= plan.hard_capture_end_ts:
            plan.actual_final_ts = now
            self._finalize_event(
                sym,
                now,
                "MAX_CAPTURE_DURATION_REACHED",
                outcome_status="UNRESOLVED_AT_CAPTURE_LIMIT",
            )
            return
        if now >= plan.normal_end_ts and ext_reason:
            nxt = min(
                plan.normal_end_ts + timedelta(seconds=self.settings.extension_seconds),
                plan.hard_capture_end_ts,
            )
            if nxt > plan.normal_end_ts:
                plan.extension_count += 1
                plan.extension_reason = ext_reason
                plan.normal_end_ts = nxt
                rec = plan.add_marker(
                    "EXTENSION",
                    now,
                    extension_count=plan.extension_count,
                    extension_reason=ext_reason,
                    normal_end_ts=iso(plan.normal_end_ts),
                )
                self._put_marker(sym, rec)
            return
        if now < plan.normal_end_ts:
            return
        status = "COMPLETE_MIN_POST_ELAPSED"
        outcome = "RESOLVED" if plan.result_ts is not None else "COMPLETE_NO_RESULT_MARKER"
        plan.actual_final_ts = now
        self._finalize_event(sym, now, status, outcome_status=outcome)

    def _maybe_rotate_segment(self, symbol: str, now: datetime) -> None:
        """Close open tmp after segment_minutes / size cap; continue same fight_event_id."""
        sym = symbol.upper()
        writer = self._writers.get(sym)
        if writer is None:
            return
        age = (now - writer.started_at).total_seconds()
        too_old = age >= self.settings.segment_seconds
        too_big = writer.open_tmp_bytes >= self.settings.max_open_tmp_bytes
        if not too_old and not too_big:
            return
        self._rotate_segment(sym, now, reason="SEGMENT_SIZE_LIMIT" if too_big else "SEGMENT_TIME_LIMIT")

    def _rotate_segment(self, symbol: str, now: datetime, *, reason: str) -> None:
        sym = symbol.upper()
        with self._lock:
            old = self._writers.get(sym)
            sink = self._sinks.get(sym)
            plan = self._plans.get(sym)
            if old is None or sink is None:
                return

        def build_new(old_writer: ActiveEventWriter) -> tuple[ActiveEventWriter, dict[str, Any]]:
            # Runs on the long-lived writer thread: finalize old segment, open next.
            # Queue stays the same; producer is never interrupted.
            nxt = old_writer.continuation_index + 1
            fight_id = old_writer.fight_event_id or old_writer.event_id
            old_writer.lifecycle = list(self.watcher.state(sym).lifecycle_log)
            if plan is not None:
                old_writer.extra_manifest = plan.to_manifest_fields()
            try:
                man = old_writer.finalize(
                    ended_at=now,
                    status="SEGMENT_CONTINUED",
                    report_md=self._build_report(old_writer, "SEGMENT_CONTINUED"),
                )
            except Exception:
                logger.exception("fr_segment_finalize_failed %s", fight_id)
                man = {}
            self._bytes_written_total += old_writer.open_tmp_bytes
            seg_sha = (man.get("sha256") or {}).get("full_ob_raw_deltas.jsonl.zst") or man.get("segment_sha256")
            if plan is not None and plan.segments:
                last = plan.segments[-1]
                last.segment_finalization_reason = reason
                last.segment_sha256 = seg_sha
                last.previous_segment_sha256 = old_writer.previous_segment_sha256
                last.segment_first_u = old_writer.first_u
                last.segment_last_u = old_writer.last_u
                last.segment_first_ts = iso(old_writer.segment_first_ts)
                last.segment_last_ts = iso(old_writer.segment_last_ts)
                last.next_segment_expected = True
                if old_writer.persisted_u_gap_count:
                    plan.persisted_capture_u_gap_count = getattr(plan, "persisted_capture_u_gap_count", 0) + old_writer.persisted_u_gap_count
                    plan.persisted_missing_u_estimate = getattr(plan, "persisted_missing_u_estimate", 0) + old_writer.persisted_missing_u_estimate
                    plan.note_incomplete("PERSISTED_U_GAP")
                    plan.research_eligible = False
            event_root = old_writer.directory if old_writer.continuation_index == 0 else old_writer.directory.parent
            new_dir = event_root / f"cont_{nxt:03d}"
            new_writer = ActiveEventWriter(
                event_id=f"{fight_id}_c{nxt:03d}",
                symbol=sym,
                directory=new_dir,
                started_at=now,
                trigger_reason=old_writer.trigger_reason,
                trigger_meta=dict(old_writer.trigger_meta),
                profile_context=dict(old_writer.profile_context),
                config_snapshot=dict(old_writer.config_snapshot),
                continuation_index=nxt,
                fight_event_id=fight_id,
                previous_segment_sha256=seg_sha,
            )
            new_writer.coverage = dict(old_writer.coverage)
            new_writer.coverage["continuation_reason"] = reason
            new_writer.open()
            if plan is not None:
                plan.segments.append(
                    SegmentRecord(
                        continuation_index=nxt,
                        directory=str(new_dir),
                        previous_segment_sha256=seg_sha,
                        next_segment_expected=True,
                    )
                )
                new_writer.extra_manifest = plan.to_manifest_fields()
            with self._lock:
                self._writers[sym] = new_writer
            return new_writer, man

        try:
            sink.rotate_writer(build_new)
        except Exception:
            logger.exception("fr_rotate_segment_failed %s", sym)
            return
        writer = self._writers.get(sym)
        if plan is not None and writer is not None:
            self._write_event_manifest(plan, writer)

    def _finalize_event(
        self,
        symbol: str,
        now: datetime,
        status: str,
        *,
        outcome_status: str | None = None,
    ) -> None:
        with self._lock:
            writer = self._writers.pop(symbol.upper(), None)
            sink = self._sinks.pop(symbol.upper(), None)
            plan = self._plans.pop(symbol.upper(), None)
        if writer is None:
            return
        drain_ok = True
        if sink is not None:
            drain_ok = sink.stop(timeout_sec=15.0)
            self._bytes_written_total += writer.open_tmp_bytes
        st = self.watcher.state(symbol)
        finalization_reason = status
        if not drain_ok:
            status = "INCOMPLETE_WRITER_DRAIN_TIMEOUT"
            finalization_reason = "INCOMPLETE_WRITER_DRAIN_TIMEOUT"
            if plan is not None:
                plan.note_incomplete("WRITER_DRAIN_TIMEOUT")
                plan.research_eligible = False
        if writer.buffer_overflow:
            status = "INCOMPLETE_QUEUE_DROP"
            if plan is not None:
                plan.research_eligible = False
        if sink is not None and sink.drops:
            status = "INCOMPLETE_QUEUE_DROP"
            writer.queue_drops = max(writer.queue_drops, sink.drops)
            if plan is not None:
                # Writer-thread errors increment sink.drops without going through try_put;
                # fold them into plan + monotonic lifetime counters here.
                extra = max(0, int(sink.drops) - int(plan.queue_drop_count))
                if extra:
                    self._note_queue_drop(symbol, n=extra)
                plan.queue_drop_count = max(plan.queue_drop_count, sink.drops)
                plan.note_incomplete("QUEUE_DROP")
                plan.data_quality = "INCOMPLETE_QUEUE_DROP"
                plan.research_eligible = False
            if finalization_reason in {
                "INTERRUPTED_BY_CONTROLLED_RESTART",
                "CONTROLLED_RESTART_AFTER_QUEUE_DROP",
            } or str(finalization_reason).startswith("CONTROLLED_RESTART"):
                finalization_reason = "CONTROLLED_RESTART_AFTER_QUEUE_DROP"
        if sink is not None and sink.error_count:
            if plan is not None:
                plan.note_incomplete("WRITER_ERROR")
                plan.research_eligible = False
        if writer.persisted_u_gap_count:
            if plan is not None:
                plan.persisted_capture_u_gap_count += writer.persisted_u_gap_count
                plan.persisted_missing_u_estimate += writer.persisted_missing_u_estimate
                plan.note_incomplete("PERSISTED_U_GAP")
                plan.research_eligible = False
        if writer.status == "NO_VALID_INITIAL_SNAPSHOT":
            status = "NO_VALID_INITIAL_SNAPSHOT"
            if plan is not None:
                plan.note_incomplete("NO_VALID_INITIAL_SNAPSHOT")
                plan.research_eligible = False
        if st.pre_trigger_incomplete and status.startswith("COMPLETE"):
            status = "INCOMPLETE_PREBUFFER"
            if plan is not None:
                plan.note_incomplete("PREBUFFER_EMPTY")
        if plan is not None and plan.data_quality == "INCOMPLETE" and status.startswith("COMPLETE"):
            status = "INCOMPLETE"
        # Preserve bootstrap / queue-drop research gate in manifests.
        if plan is not None:
            if plan.trigger_source == "BOOTSTRAP_ALREADY_IN_EDGE_ZONE":
                plan.trigger_quality = "BOOTSTRAP_NOT_REAL_CROSS"
                plan.research_eligible = False
                plan.bootstrap_persistent_capture = False
                if plan.queue_drop_count > 0:
                    plan.data_quality = "INCOMPLETE_QUEUE_DROP"
            if plan.queue_drop_count > 0:
                plan.data_quality = "INCOMPLETE_QUEUE_DROP"
                plan.research_eligible = False
            if plan.persisted_capture_u_gap_count > 0:
                plan.data_quality = "INCOMPLETE_PERSISTED_U_GAP"
                plan.research_eligible = False
            plan.actual_final_ts = now
            self.last_event_queue_drops[symbol.upper()] = int(plan.queue_drop_count)
            if not plan.research_eligible:
                self.total_research_ineligible_events += 1
            if plan.segments:
                last = plan.segments[-1]
                last.next_segment_expected = False
                last.segment_finalization_reason = finalization_reason
                last.segment_first_u = writer.first_u
                last.segment_last_u = writer.last_u
                last.segment_first_ts = iso(writer.segment_first_ts)
                last.segment_last_ts = iso(writer.segment_last_ts)
            extra = plan.to_manifest_fields(now=now)
            extra["finalization_reason"] = finalization_reason
            extra["outcome_status"] = outcome_status or finalization_reason
            extra["research_eligible"] = plan.research_eligible
            extra["trigger_quality"] = plan.trigger_quality
            extra["data_quality"] = plan.data_quality
            writer.extra_manifest = extra
        writer.lifecycle = list(st.lifecycle_log)
        writer.sequence = {
            "first_u": writer.first_u,
            "last_u": writer.last_u,
            "first_seq": writer.first_seq,
            "last_seq": writer.last_seq,
            "gap_count": writer.gap_count,
        }
        writer.health = {"finalized_at": now.isoformat()}
        report = self._build_report(writer, status)
        try:
            writer.finalize(ended_at=now, status=status, report_md=report)
            try:
                replay_root = writer.directory if writer.continuation_index == 0 else writer.directory.parent
                replay = replay_event_directory(replay_root)
                (replay_root / "replay_result.json").write_text(
                    __import__("json").dumps(replay, indent=2) + "\n", encoding="utf-8"
                )
                replay_status = replay.get("status") if replay.get("ok") else (replay.get("status") or "INCOMPLETE_SEQUENCE_GAP")
                if plan is not None:
                    extra = dict(writer.extra_manifest)
                    extra["replay_status"] = replay_status
                    if plan.segments:
                        import json as _json

                        man = _json.loads((writer.directory / "manifest.json").read_text())
                        plan.segments[-1].segment_sha256 = (man.get("sha256") or {}).get("full_ob_raw_deltas.jsonl.zst")
                    extra["segments"] = [s.__dict__ for s in plan.segments]
                    writer.extra_manifest = extra
                    self._write_event_manifest(plan, writer, extra={"replay_status": replay_status, "finalization_reason": status, "outcome_status": outcome_status or status})
                if not replay.get("ok") and status.startswith("COMPLETE"):
                    man = writer.directory / "manifest.json"
                    import json as _json

                    m = _json.loads(man.read_text())
                    m["completion_status"] = replay.get("status") or "INCOMPLETE_SEQUENCE_GAP"
                    man.write_text(_json.dumps(m, indent=2, sort_keys=True) + "\n")
            except Exception:
                logger.exception("fr_replay_failed %s", writer.event_id)
        except Exception:
            logger.exception("fr_finalize_failed %s", writer.event_id)
        self.profile_signal_registry.expire_all(symbol)
        self.watcher.end_capture(symbol, now)

    def shutdown(self, *, reason: str = "INTERRUPTED_BY_CONTROLLED_RESTART") -> None:
        now = datetime.now(timezone.utc)
        for sym in list(self._writers.keys()):
            plan = self._plans.get(sym)
            sink = self._sinks.get(sym)
            drops = 0 if sink is None else int(sink.drops)
            if plan is not None:
                plan.note_incomplete("PROCESS_RESTART")
                plan.actual_final_ts = now
                if drops > 0 or plan.queue_drop_count > 0:
                    plan.queue_drop_count = max(plan.queue_drop_count, drops)
                    plan.note_incomplete("QUEUE_DROP")
                    plan.data_quality = "INCOMPLETE_QUEUE_DROP"
                    plan.research_eligible = False
                if plan.trigger_source == "BOOTSTRAP_ALREADY_IN_EDGE_ZONE":
                    plan.trigger_quality = "BOOTSTRAP_NOT_REAL_CROSS"
                    plan.research_eligible = False
            # Prefer explicit restart-after-drop reason when drops occurred.
            final_reason = reason
            if drops > 0 or (plan is not None and plan.queue_drop_count > 0):
                final_reason = "CONTROLLED_RESTART_AFTER_QUEUE_DROP"
            elif reason == "INTERRUPTED_BY_CONTROLLED_RESTART":
                final_reason = "INTERRUPTED_BY_CONTROLLED_RESTART"
            self._finalize_event(sym, now, final_reason, outcome_status=final_reason)

    def _build_report(self, writer: ActiveEventWriter, status: str) -> str:
        edge = writer.trigger_meta.get("edge")
        return "\n".join(
            [
                f"# Full-OB Edge Flight Event `{writer.event_id}`",
                "",
                f"- Symbol: `{writer.symbol}`",
                f"- Status: `{status}`",
                f"- Trigger: `{writer.trigger_reason}`",
                f"- Edge: `{edge}`",
                f"- Mid at trigger: `{writer.trigger_meta.get('mid')}`",
                f"- Distance bps: `{writer.trigger_meta.get('distance_bps')}`",
                f"- Deltas: `{writer.delta_count}`",
                f"- Trades: `{writer.trade_count}`",
                f"- RPI included: `{RPI_INCLUDED_IN_FULL_OB}`",
                "",
                "## Beweisgrenzen",
                "",
                "- Trade↔L2 ohne gemeinsame Order-ID: höchstens `TEMPORALLY_ASSOCIATED`.",
                "- Unmatched L2-Reduktion ≠ Cancellation.",
                "- Full-OB ohne RPI-Liquidität.",
                "",
                f"Contract: `{CONTRACT_VERSION}`",
            ]
        )

    def health_dict(self) -> dict[str, Any]:
        if not self.enabled:
            return {"full_ob_flight_recorder_enabled": False}
        disk = check_disk(
            self.settings.capture_root,
            warn_gb=self.settings.warn_free_disk_gb,
            min_gb=self.settings.min_free_disk_gb,
        )
        now = datetime.now(timezone.utc)
        elapsed = max((now - self._started_at).total_seconds(), 1.0)
        open_tmp = 0
        backlog = 0
        backlog_bytes = 0
        drops = 0
        dropped_levels = 0
        dropped_bytes = 0
        writer_messages = 0
        writer_bytes = 0
        writer_flush = 0
        writer_errors = 0
        batch_size = 0
        high_wm = 0
        oldest_age = None
        for sym, w in self._writers.items():
            open_tmp += w.open_tmp_bytes
            sink = self._sinks.get(sym)
            if sink is not None:
                m = sink.metrics()
                backlog += m["queue_backlog_items"]
                backlog_bytes += m["queue_backlog_bytes"]
                drops += m["queue_drop_count"]
                dropped_levels += m["dropped_price_level_updates"]
                dropped_bytes += m["dropped_bytes_estimate"]
                writer_messages += m["writer_messages"]
                writer_bytes += m["writer_bytes"]
                writer_flush += m["writer_flush_count"]
                writer_errors += m["writer_error_count"]
                batch_size = max(batch_size, m["writer_batch_size"])
                high_wm = max(high_wm, m["queue_high_watermark"])
                age = m["queue_oldest_age_ms"]
                if age is not None:
                    oldest_age = age if oldest_age is None else max(oldest_age, age)
        with self._metrics_lock:
            window_s = max((now - self._metrics_window_started).total_seconds(), 1e-6)
            rates = {
                "ingress_messages_per_second": self._metrics_window_ingress_messages / window_s,
                "ingress_level_updates_per_second": self._metrics_window_ingress_levels / window_s,
                "writer_messages_per_second": (writer_messages - self._metrics_window_writer_messages) / window_s,
                "writer_bytes_per_second": (writer_bytes - self._metrics_window_writer_bytes) / window_s,
            }
            if window_s >= 5.0:
                self._last_rate_sample = rates
                self._metrics_window_started = now
                self._metrics_window_ingress_messages = 0
                self._metrics_window_ingress_levels = 0
                self._metrics_window_writer_messages = writer_messages
                self._metrics_window_writer_bytes = writer_bytes
            else:
                rates = dict(self._last_rate_sample)
            ingress_total = self._ingress_messages
            ingress_levels_total = self._ingress_levels
        written = self._bytes_written_total + open_tmp
        projected = int(written / elapsed * 86400.0)
        warn_daily = projected >= self.settings.projected_daily_warn_bytes
        prebuffer_coverage = {
            sym: (0.0 if self._buffers.get(sym) is None else self._buffers[sym].coverage_seconds())
            for sym in self.settings.symbols
        }
        out = {
            "full_ob_flight_recorder_enabled": True,
            "contract_version": CONTRACT_VERSION,
            "timing_contract": TIMING_CONTRACT,
            "pre_seconds": self.settings.pre_seconds,
            "min_post_seconds": self.settings.min_post_seconds,
            "segment_seconds": self.settings.segment_seconds,
            "hard_cap_seconds": self.settings.max_seconds,
            "symbols": sorted(self.settings.symbols),
            "capture_root": str(self.settings.capture_root),
            "rpi_included": RPI_INCLUDED_IN_FULL_OB,
            "bootstrap_persistent_capture": False,
            "signal_count": self.signal_count,
            "bootstrap_observation_count": self.bootstrap_observation_count,
            "writer_mode": "BATCH_STREAMING_ZSTD",
            "queue_item_contract": "ONE_ITEM_PER_BYBIT_DELTA",
            "writer_batch_max_messages": self.settings.writer_batch_max_messages,
            "writer_flush_interval_sec": self.settings.writer_flush_interval_sec,
            "writer_backlog": backlog,
            "queue_backlog_items": backlog,
            "queue_backlog_bytes": backlog_bytes,
            "queue_oldest_age_ms": oldest_age,
            "queue_high_watermark": high_wm,
            "writer_queue_drops": drops,
            "queue_drop_count": drops,
            "current_event_queue_drops": drops,
            "process_lifetime_queue_drops": self.process_lifetime_queue_drops,
            "symbol_lifetime_queue_drops": dict(self.symbol_lifetime_queue_drops),
            "last_event_queue_drops": dict(self.last_event_queue_drops),
            "total_research_ineligible_events": self.total_research_ineligible_events,
            "current_queue_backlog": backlog,
            "current_writer_alive": all(
                (s.writer_alive if s is not None else True) for s in self._sinks.values()
            ) if self._sinks else True,
            "dropped_messages": drops,
            "dropped_price_level_updates": dropped_levels,
            "dropped_bytes_estimate": dropped_bytes,
            "writer_batch_size": batch_size,
            "writer_flush_count": writer_flush,
            "writer_error_count": writer_errors,
            "writer_messages_total": writer_messages,
            "writer_bytes_total": writer_bytes,
            "ingress_messages_total": ingress_total,
            "ingress_level_updates_total": ingress_levels_total,
            "ingress_messages_per_second": rates["ingress_messages_per_second"],
            "ingress_level_updates_per_second": rates["ingress_level_updates_per_second"],
            "writer_messages_per_second": rates["writer_messages_per_second"],
            "writer_bytes_per_second": rates["writer_bytes_per_second"],
            "queue_capacity": self.settings.queue_size,
            "prebuffer_coverage_seconds": prebuffer_coverage,
            "open_tmp_bytes": open_tmp,
            "disk_free_gb": disk.free_gb,
            "disk_below_warn": disk.below_warn,
            "disk_below_min": disk.below_min,
            "projected_daily_bytes": projected,
            "projected_daily_warn": warn_daily,
            "nested_signal_contract": NESTED_SIGNAL_CONTRACT,
            "nested_profile_signals_enabled": self.settings.nested_profile_signals_enabled,
            "analysis_isolation_contract": ANALYSIS_ISOLATION_CONTRACT,
            "PARENT_CAPTURE_COUNT": len(self._writers),
            "GENUINE_PARENT_SIGNAL_COUNT": self.signal_count,
            "NESTED_PROFILE_EDGE_SIGNAL_COUNT": self.profile_signal_registry.nested_signal_count,
            "SECONDARY_OBSERVATIONS_SUPPRESSED": self.profile_signal_registry.duplicate_secondary_trigger_suppressed_count,
            "secondary_edge_observation_count": self.profile_signal_registry.secondary_edge_observation_count,
            "nested_signal_count": self.profile_signal_registry.nested_signal_count,
            "duplicate_secondary_trigger_suppressed_count": self.profile_signal_registry.duplicate_secondary_trigger_suppressed_count,
            "profile_arm_count": self.profile_signal_registry.profile_arm_count,
            "profile_rearm_count": self.profile_signal_registry.profile_rearm_count,
            "profile_expiry_count": self.profile_signal_registry.profile_expiry_count,
            "ACTIVE_PROFILE_WATCH_COUNT": sum(
                self.profile_signal_registry.active_watch_count(sym) for sym in self.settings.symbols
            ),
            "runtimes": {},
        }
        # Lifetime drops remain visible even when no event is open.
        out["queue_drop_count"] = int(self.process_lifetime_queue_drops)
        out["writer_queue_drops"] = int(self.process_lifetime_queue_drops)
        out["dropped_messages"] = int(self.process_lifetime_queue_drops)
        for sym in self.settings.symbols:
            st = self.watcher.state(sym)
            buf = self._buffers.get(sym)
            sink = self._sinks.get(sym)
            w = self._writers.get(sym)
            plan = self._plans.get(sym)
            span = None
            if buf is not None and len(buf):
                items = buf.snapshot()
                if items:
                    span = (items[-1].receive_time_ns - items[0].receive_time_ns) / 1e9
            sink_m = None if sink is None else sink.metrics()
            out["runtimes"][sym] = {
                "lifecycle": st.lifecycle.value,
                "event_id": st.event_id,
                "fight_event_id": None if plan is None else plan.fight_event_id,
                "continuation_index": None if w is None else w.continuation_index,
                "buffer_messages": 0 if buf is None else len(buf),
                "buffer_bytes": 0 if buf is None else buf.nbytes,
                "buffer_span_seconds": span,
                "prebuffer_coverage_seconds": 0.0 if buf is None else buf.coverage_seconds(),
                "buffer_overflow": 0 if buf is None else buf.overflow_count,
                "capturing": sym in self._writers,
                "bootstrap_status": st.bootstrap_status,
                "bootstrap_observation_count": st.bootstrap_observation_count,
                "bootstrap_persistent_capture": False,
                "writer_backlog": 0 if sink_m is None else sink_m["queue_backlog_items"],
                "queue_drops": 0 if sink_m is None else sink_m["queue_drop_count"],
                "current_event_queue_drops": 0 if plan is None else plan.queue_drop_count,
                "symbol_lifetime_queue_drops": self.symbol_lifetime_queue_drops.get(sym, 0),
                "last_event_queue_drops": self.last_event_queue_drops.get(sym, 0),
                "writer_alive": True if sink_m is None else bool(sink_m.get("writer_alive")),
                "writer_error_count": 0 if sink_m is None else sink_m["writer_error_count"],
                "dropped_price_level_updates": 0 if sink_m is None else sink_m["dropped_price_level_updates"],
                "minimum_capture_end_ts": None if plan is None else iso(plan.minimum_capture_end_ts),
                "normal_end_ts": None if plan is None else iso(plan.normal_end_ts),
                "extension_count": None if plan is None else plan.extension_count,
                "pre_trigger_seconds_actual": None if plan is None else plan.pre_trigger_seconds_actual,
                "trigger_source": None if plan is None else plan.trigger_source,
                "trigger_quality": None if plan is None else plan.trigger_quality,
                "research_eligible": None if plan is None else plan.research_eligible,
                "data_quality": None if plan is None else plan.data_quality,
                "source_feed_u_gap_count": None if plan is None else plan.u_gap_count,
                "apply_epoch_u_gap_count": None if plan is None else getattr(plan, "apply_epoch_u_gap_count", plan.u_gap_count),
                "persisted_capture_u_gap_count": None if plan is None else plan.persisted_capture_u_gap_count,
                "transport_reconnect_count": None if plan is None else getattr(plan, "transport_reconnect_count", 0),
                "resync_boundary_count": None if plan is None else getattr(plan, "resync_boundary_count", 0),
                "continuity_epoch_count": None if plan is None else getattr(plan, "continuity_epoch_count", 1),
                "continuous_capture": None if plan is None else getattr(plan, "continuous_capture", True),
                "replayable_by_epochs": None if plan is None else getattr(plan, "replayable_by_epochs", True),
                "resync_checkpoint_success_count": None if plan is None else getattr(plan, "resync_checkpoint_success_count", 0),
                "resync_checkpoint_failure_count": None if plan is None else getattr(plan, "resync_checkpoint_failure_count", 0),
                "nested_signal_count": None if plan is None else plan.nested_signal_count,
                "secondary_edge_observation_count": None if plan is None else plan.secondary_edge_observation_count,
                **self.profile_signal_registry.health_metrics(sym),
            }
        out["fr_process_transport_reconnect_count"] = self.transport_reconnect_count
        out["fr_process_resync_checkpoint_success_count"] = self.resync_checkpoint_success_count
        out["fr_process_resync_checkpoint_failure_count"] = self.resync_checkpoint_failure_count
        return out


def os_access_writable(path: Path) -> bool:
    import os

    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".fr_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except Exception:
        return False
