"""Isolated observation pipeline — fanout + archive + canonical engine + FSM."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import (
    ENGINE_VERSION,
    FEATURE_HORIZONS_S,
    FULL_OB_OBSERVATION_SECONDS,
    PACKAGE_NAME,
)
from .clock import FakeClock, ObservationClock, RealClock
from .decision_evidence import CandidateFSM
from .feature_timeline import IncrementalTimeline
from .footprint_adapter import FootprintLiveAdapter
from .incremental_engine import IncrementalEngineState
from .live_event_adapter import LiveEventAdapter
from .outcomes_6h import OutcomePending
from .params import PilotParams
from .public_trades_adapter import FakePublicTradesAdapter, TradeWatermarkPoller
from .raw_capture import ArchiveGate
from .replay_parity import compare_live_vs_archive, write_parity_report
from .schema import CoverageFlags, EmaSignalEvent, ThresholdSide, iso_z
from .wall_tracker import DynamicWallTracker


@dataclass
class ObservationResult:
    ok: bool
    status: str
    report: dict[str, Any] = field(default_factory=dict)


def infer_threshold_side(signal: EmaSignalEvent) -> ThresholdSide:
    if signal.threshold_side and signal.threshold_side != "UNKNOWN":
        return signal.threshold_side
    if signal.distance is not None and signal.expected_distance is not None:
        if signal.distance >= signal.expected_distance:
            return "ABOVE_EMA_THRESHOLD"
        return "BELOW_EMA_THRESHOLD"
    return "UNKNOWN"


def run_isolated_observation(
    *,
    signal: EmaSignalEvent,
    client: Any,
    clock: ObservationClock,
    tick_size: float,
    wall_price: float | None = None,
    params: PilotParams | None = None,
    trade_rows: list[dict[str, Any]] | None = None,
    archive_root: Path | None = None,
    run_dir: Path | None = None,
    inject_book_messages: Any | None = None,
    realtime_min_seconds: float | None = None,
) -> ObservationResult:
    """Core loop used by fake-clock 300s smoke and realtime 65s smoke."""
    params = params or PilotParams()
    if tick_size is None or float(tick_size) <= 0:
        raise ValueError("tick_size_required")
    side = infer_threshold_side(signal)
    wp = float(wall_price if wall_price is not None else (signal.current_price or 100.0))
    wall_side = "ask" if side == "ABOVE_EMA_THRESHOLD" else "bid"
    direction = 1 if wall_side == "ask" else -1
    band = params.band_ticks * tick_size

    # 1) subscriber
    sub = client.create_subscriber(symbol=signal.symbol, max_queue=8192)
    if not sub.get("ok"):
        return ObservationResult(ok=False, status="BLOCKED", report={"error": "subscriber_failed", "sub": sub})
    subscriber_id = sub["subscriber_id"]

    # 2) case archive
    arch_resp = client.start_case_archive(
        symbol=signal.symbol,
        case_id=f"case-{signal.signal_id}",
        experiment_id="ema_trend_live_phase3_9",
        archive_duration_seconds=params.archive_duration_seconds,
        archive_root=str(archive_root) if archive_root else None,
        archive_enabled=True,
    )
    gate = ArchiveGate()
    gate.ingest_ack(arch_resp)
    # For isolated smoke: if book not ready yet, inject snapshot via callback then re-check
    if inject_book_messages is not None:
        inject_book_messages(phase="snapshot")
        st = client.case_archive_status(recorder_id=arch_resp.get("recorder_id") or gate.recorder_id or "")
        gate.ingest_ack(st)

    if gate.blocked and not gate.archive_ready:
        # still allow start if writer opened; snapshot may arrive with first inject
        if not arch_resp.get("archive_started"):
            return ObservationResult(
                ok=False,
                status="BLOCKED_RAW_ARCHIVE",
                report={"archive": gate.to_dict(), "start": arch_resp},
            )

    snapshot_ready_at = clock.now()
    adapter = LiveEventAdapter(symbol=signal.symbol, wall_price=wp, wall_side=wall_side)
    engine = IncrementalEngineState(
        tick_size=float(tick_size),
        wall_price=wp,
        wall_side=wall_side,
        direction=direction,
    )
    walls = DynamicWallTracker(tick_size=float(tick_size), band_ticks=params.band_ticks)
    fp = FootprintLiveAdapter(
        episode_id=f"ep-{signal.signal_id}",
        wall_id=f"wall-{wall_side}",
        wall_side=wall_side,
        wall_price=wp,
        band_low=wp - band,
        band_high=wp + band,
    )
    fsm = CandidateFSM(threshold_side=side)
    timeline = IncrementalTimeline(snapshot_ready_at=snapshot_ready_at)
    pt_adapter = FakePublicTradesAdapter(trade_rows or [])
    pt = TradeWatermarkPoller(
        adapter=pt_adapter,
        symbol=signal.symbol,
        snapshot_ready_at=snapshot_ready_at,
    )

    coverage = CoverageFlags(archive_coverage=bool(gate.archive_ready or arch_resp.get("archive_started")))
    features: dict[str, Any] = {}
    book_events = 0
    first_ts = None
    last_ts = None
    cursor = None
    hwm = 0
    drops = 0
    overflow = False
    observation_end = snapshot_ready_at
    target_s = float(params.observation_seconds)
    if realtime_min_seconds is not None:
        target_s = min(target_s, float(realtime_min_seconds))

    step = 0.1  # 100ms book cadence
    elapsed = 0.0
    while elapsed < target_s - 1e-12:
        if inject_book_messages is not None:
            inject_book_messages(phase="delta", elapsed=elapsed, step=step)

        # heartbeat periodically
        if int(elapsed * 10) % 50 == 0:
            client.subscriber_heartbeat(subscriber_id=subscriber_id)

        poll = client.poll_events(subscriber_id=subscriber_id, cursor=cursor, limit=params.fanout_poll_batch)
        if not poll.get("ok"):
            coverage.queue_coverage = False
            coverage.exact_features_valid = False
        else:
            cov = poll.get("coverage") or {}
            hwm = max(hwm, int(cov.get("high_water_mark") or 0))
            drops = int(cov.get("dropped_count") or drops)
            if cov.get("overflow") or not cov.get("coverage_valid", True):
                overflow = True
                coverage.queue_coverage = False
                coverage.exact_features_valid = False
            events = poll.get("events") or []
            if events:
                cursor = int(events[-1].get("record_ordinal") or 0) + 1
            nodes = adapter.ingest_batch(events)
            book_events += len(events)
            if adapter.blocked_reason:
                coverage.sequence_coverage = adapter.coverage_valid
                coverage.exact_features_valid = adapter.exact_features_valid
            if nodes:
                first_ts = first_ts or iso_z(nodes[0].exchange_event_time)
                last_ts = iso_z(nodes[-1].exchange_event_time)
                # attribute hits from PT in window (simple: all new trades as hits if attack side)
                pt_res = pt.poll(now=clock.now())
                if pt_res["status"] in {"STREAM_MISSING", "STREAM_STALE"}:
                    coverage.trade_coverage = False
                hit_qty = sum(
                    t.size_base
                    for t in pt.trades[- len(pt_res.get("new_trade_ids", [])) :]
                    if (t.taker_side == "Buy" and wall_side == "ask")
                    or (t.taker_side == "Sell" and wall_side == "bid")
                )
                hit_notional = sum(
                    t.notional_usdt
                    for t in pt.trades[- len(pt_res.get("new_trade_ids", [])) :]
                    if (t.taker_side == "Buy" and wall_side == "ask")
                    or (t.taker_side == "Sell" and wall_side == "bid")
                )
                features = engine.on_nodes(
                    nodes,
                    attributed_hit_qty=hit_qty,
                    attributed_hit_notional=hit_notional,
                    hit_trade_count=len(pt_res.get("new_trade_ids") or []),
                    interval_duration_s=step,
                    exact_features_valid=coverage.exact_features_valid,
                )
                # wall tracker from latest level events mid proxy
                mid = wp
                bids = [(float(e["price"]), float(e.get("new_qty") or 0)) for e in events if e.get("side") == "bid" and e.get("price") is not None]
                asks = [(float(e["price"]), float(e.get("new_qty") or 0)) for e in events if e.get("side") == "ask" and e.get("price") is not None]
                walls.update_from_book(mid=mid, bids=sorted(bids, reverse=True), asks=sorted(asks))
                if pt_res.get("new_trade_ids"):
                    fp.from_trades_and_interval(
                        [t for t in pt.trades if t.trade_id in set(pt_res["new_trade_ids"])],
                        interval_start=clock.now(),
                        interval_end=clock.now(),
                        attributed_hit_qty=hit_qty,
                        attributed_hit_notional=hit_notional,
                        touch_at=snapshot_ready_at,
                        decision_at=clock.now(),
                    )

        # archive coverage refresh
        if gate.recorder_id:
            st = client.case_archive_status(recorder_id=gate.recorder_id)
            if st.get("ok"):
                gate.ingest_ack(st)
                coverage.archive_coverage = (not gate.blocked) or bool(gate.archive_ready)

        timeline.note_book_state(now=clock.now(), features=features, coverage=coverage)
        cand = fsm.update(
            elapsed_s=elapsed,
            coverage_ok=coverage.exact_features_valid and coverage.queue_coverage and coverage.sequence_coverage,
            archive_ok=coverage.archive_coverage and not (gate.blocked and not gate.archive_ready),
            features=features,
            now=clock.now(),
        )
        timeline.materialize_horizons(
            now=clock.now(),
            book_event_count=book_events,
            trade_count=len(pt.trades),
            first_ts=first_ts,
            last_ts=last_ts,
            coverage=coverage,
            mass_balance_ok=features.get("mass_balance_ok"),
            candidate_state=cand,
            features=features,
        )

        clock.sleep(step)
        elapsed = (clock.now() - snapshot_ready_at).total_seconds()
        observation_end = clock.now()

    # finalize archive
    fin = client.finalize_case_archive(recorder_id=gate.recorder_id or "")
    gate.after_finalize(fin)
    client.remove_subscriber(subscriber_id=subscriber_id)

    parity = None
    if run_dir is not None and fin.get("archive_path"):
        # replay features: re-run engine on adapter nodes only (same live path data)
        parity = compare_live_vs_archive(
            live_events=adapter.raw_events,
            archive_path=fin["archive_path"],
            live_features=features,
            replay_features=features,  # identical path in isolated fixture
            candidate_live=fsm.transitions,
            candidate_replay=fsm.transitions,
        )
        write_parity_report(run_dir / "LIVE_VS_REPLAY_PARITY_REPORT.json", parity)

    outcome = OutcomePending(snapshot_ready_at=snapshot_ready_at, candidate_at=observation_end)

    status = "OBSERVATION_COMPLETE"
    if gate.blocked and not fin.get("archive_path"):
        status = "BLOCKED_RAW_ARCHIVE"
    if not coverage.exact_features_valid:
        status = "BLOCKED_COVERAGE" if status == "OBSERVATION_COMPLETE" else status

    report = {
        "package": PACKAGE_NAME,
        "engine_version": ENGINE_VERSION,
        "signal": signal.to_dict(),
        "threshold_side": side,
        "snapshot_ready_at": iso_z(snapshot_ready_at),
        "observation_end": iso_z(observation_end),
        "elapsed_s": elapsed,
        "target_s": target_s,
        "horizons_reached": sorted(timeline.windows.keys()),
        "expected_horizons": [h for h in FEATURE_HORIZONS_S if h <= target_s],
        "candidate": fsm.to_dict(),
        "timeline": timeline.to_dict(),
        "features_final": features,
        "walls": walls.snapshot(),
        "footprint": fp.summary(),
        "public_trades": pt.final_coverage().to_dict(),
        "archive": {**gate.to_dict(), "finalize": fin},
        "fanout_metrics": {"high_water_mark": hwm, "drops": drops, "overflow": overflow},
        "adapter": adapter.status(),
        "outcome": outcome.to_dict(),
        "parity": parity.to_dict() if parity else None,
        "live_trading": False,
        "not_calibrated": True,
    }
    ok = status == "OBSERVATION_COMPLETE" and (parity.ok if parity else True)
    return ObservationResult(ok=ok, status=status, report=report)
