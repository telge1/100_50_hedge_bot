"""Phase 2: segment-wise orderbook replay smoke (read-only).

Reuses OrderBookReplayer / load_events. Does not implement walls or signals.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence

from orderbook_analyse.dynamic_wall_detector import (
    ReadOnlyClickHouse,
    _ensure_aware,
    load_events,
)
from orderbook_analyse.orderbook_replay import (
    BookLevelEvent,
    OrderBookReplayer,
    OrderBookState,
    ReplayError,
    group_messages,
)
from orderbook_analyse.replay_segmentation import ReplaySegment

logger = logging.getLogger(__name__)

# Soft warning threshold for in-memory level rows per segment (Phase 2 docs).
LEVEL_ROW_WARN_THRESHOLD = 5_000_000


@dataclass
class SegmentReplayResult:
    segment_id: str
    symbol: str
    segment_start_ts: datetime
    segment_end_ts: datetime
    bootstrap_snapshot_ts: datetime
    bootstrap_update_id: int
    expected_last_update_id: int
    actual_last_update_id: int | None
    expected_last_cross_sequence: int
    actual_last_cross_sequence: int | None
    messages_loaded: int
    snapshot_messages_loaded: int
    delta_messages_loaded: int
    events_or_level_rows_loaded: int
    duration_sec: float
    warmup_seconds: int
    feature_emission_start_ts: datetime | None
    post_warmup_duration_sec: float | None
    replay_status: str
    invariants_ok: bool
    error_type: str | None = None
    error_message: str | None = None
    runtime_sec: float = 0.0
    error_ts: datetime | None = None
    previous_update_id: int | None = None
    current_update_id: int | None = None
    previous_cross_sequence: int | None = None
    current_cross_sequence: int | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "symbol": self.symbol,
            "segment_start_ts": self.segment_start_ts.isoformat(),
            "segment_end_ts": self.segment_end_ts.isoformat(),
            "bootstrap_snapshot_ts": self.bootstrap_snapshot_ts.isoformat(),
            "bootstrap_update_id": self.bootstrap_update_id,
            "expected_last_update_id": self.expected_last_update_id,
            "actual_last_update_id": self.actual_last_update_id,
            "expected_last_cross_sequence": self.expected_last_cross_sequence,
            "actual_last_cross_sequence": self.actual_last_cross_sequence,
            "messages_loaded": self.messages_loaded,
            "snapshot_messages_loaded": self.snapshot_messages_loaded,
            "delta_messages_loaded": self.delta_messages_loaded,
            "events_or_level_rows_loaded": self.events_or_level_rows_loaded,
            "duration_sec": self.duration_sec,
            "warmup_seconds": self.warmup_seconds,
            "feature_emission_start_ts": None
            if self.feature_emission_start_ts is None
            else self.feature_emission_start_ts.isoformat(),
            "post_warmup_duration_sec": self.post_warmup_duration_sec,
            "replay_status": self.replay_status,
            "invariants_ok": self.invariants_ok,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "runtime_sec": self.runtime_sec,
        }

    def error_row(self) -> dict[str, Any] | None:
        if self.replay_status.startswith("REPLAY_OK") or self.replay_status.startswith(
            "SKIPPED"
        ):
            return None
        return {
            "segment_id": self.segment_id,
            "symbol": self.symbol,
            "error_ts": None
            if self.error_ts is None
            else self.error_ts.isoformat(),
            "previous_update_id": self.previous_update_id,
            "current_update_id": self.current_update_id,
            "previous_cross_sequence": self.previous_cross_sequence,
            "current_cross_sequence": self.current_cross_sequence,
            "error_type": self.error_type,
            "error_message": self.error_message,
        }


@dataclass
class BookEndState:
    segment_id: str
    symbol: str
    end_ts: datetime
    last_update_id: int
    last_cross_sequence: int
    best_bid: Decimal | None
    best_ask: Decimal | None
    mid_price: Decimal | None
    spread: Decimal | None
    spread_bps: float | None
    active_bid_levels: int
    active_ask_levels: int
    active_levels: int
    bid_depth_notional: Decimal
    ask_depth_notional: Decimal
    total_depth_notional: Decimal

    def to_row(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "symbol": self.symbol,
            "end_ts": self.end_ts.isoformat(),
            "last_update_id": self.last_update_id,
            "last_cross_sequence": self.last_cross_sequence,
            "best_bid": None if self.best_bid is None else format(self.best_bid, "f"),
            "best_ask": None if self.best_ask is None else format(self.best_ask, "f"),
            "mid_price": None if self.mid_price is None else format(self.mid_price, "f"),
            "spread": None if self.spread is None else format(self.spread, "f"),
            "spread_bps": self.spread_bps,
            "active_bid_levels": self.active_bid_levels,
            "active_ask_levels": self.active_ask_levels,
            "active_levels": self.active_levels,
            "bid_depth_notional": format(self.bid_depth_notional, "f"),
            "ask_depth_notional": format(self.ask_depth_notional, "f"),
            "total_depth_notional": format(self.total_depth_notional, "f"),
        }


def _side_depth_notional(levels: Mapping[Decimal, Decimal]) -> Decimal:
    total = Decimal("0")
    for price, qty in levels.items():
        total += price * qty
    return total


def book_end_state_from_book(
    book: OrderBookState,
    *,
    segment_id: str,
    symbol: str,
    end_ts: datetime,
) -> BookEndState:
    bb = book.best_bid()
    ba = book.best_ask()
    mid = book.mid_price()
    spread = book.spread()
    spread_bps: float | None = None
    if mid is not None and spread is not None and mid != 0:
        spread_bps = float(spread / mid * Decimal("10000"))
    bid_n = _side_depth_notional(book.bids)
    ask_n = _side_depth_notional(book.asks)
    return BookEndState(
        segment_id=segment_id,
        symbol=symbol,
        end_ts=_ensure_aware(end_ts),
        last_update_id=int(book.last_update_id or 0),
        last_cross_sequence=int(book.last_seq or 0),
        best_bid=bb,
        best_ask=ba,
        mid_price=mid,
        spread=spread,
        spread_bps=spread_bps,
        active_bid_levels=len(book.bids),
        active_ask_levels=len(book.asks),
        active_levels=book.active_level_count(),
        bid_depth_notional=bid_n,
        ask_depth_notional=ask_n,
        total_depth_notional=bid_n + ask_n,
    )


def filter_events_to_segment(
    events: Sequence[BookLevelEvent],
    *,
    bootstrap_update_id: int,
    bootstrap_cross_sequence: int,
    segment_end_ts: datetime,
    last_update_id: int,
) -> list[BookLevelEvent]:
    """Keep bootstrap snapshot + in-bound deltas; keep unexpected in-bound snapshots for detection."""
    end = _ensure_aware(segment_end_ts)
    out: list[BookLevelEvent] = []
    for ev in events:
        ts = _ensure_aware(ev.exchange_ts)
        if ts > end:
            continue
        if ev.message_type == "snapshot":
            if (
                ev.update_id == bootstrap_update_id
                and ev.cross_sequence == bootstrap_cross_sequence
            ):
                out.append(ev)
                continue
            # Unexpected mid-segment snapshot within bounds → keep so replay fails loudly
            if bootstrap_update_id <= ev.update_id <= last_update_id:
                out.append(ev)
            continue
        if ev.update_id < bootstrap_update_id:
            continue
        if ev.update_id > last_update_id:
            continue
        out.append(ev)
    return out


def load_segment_events(
    db: ReadOnlyClickHouse,
    *,
    symbol: str,
    segment: ReplaySegment,
) -> list[BookLevelEvent]:
    """Load level rows for one segment via existing load_events + strict filter.

    ``load_events`` bounds by ``exchange_ts <= end``. The ClickHouse client may
    truncate datetime query params to whole seconds (column is DateTime64(3)),
    which drops messages in the final sub-second of a segment. We therefore
    query with ``segment_end_ts + 1s`` and re-apply strict segment bounds in
    Python (update_id / timestamp / bootstrap snapshot).
    """
    query_end = _ensure_aware(segment.segment_end_ts) + timedelta(seconds=1)
    raw = load_events(
        db,
        symbol=symbol,
        snapshot_ts=segment.bootstrap_snapshot_ts,
        snapshot_u=segment.bootstrap_update_id,
        snapshot_seq=segment.bootstrap_cross_sequence,
        end=query_end,
    )
    return filter_events_to_segment(
        raw,
        bootstrap_update_id=segment.bootstrap_update_id,
        bootstrap_cross_sequence=segment.bootstrap_cross_sequence,
        segment_end_ts=segment.segment_end_ts,
        last_update_id=segment.last_update_id,
    )


def sample_grid(
    segment_start: datetime,
    segment_end: datetime,
    *,
    interval_seconds: int,
) -> list[datetime]:
    """Deterministic sample times: start + n * interval, within [start, end]."""
    if interval_seconds <= 0:
        return []
    start = _ensure_aware(segment_start)
    end = _ensure_aware(segment_end)
    out: list[datetime] = []
    t = start
    step = timedelta(seconds=interval_seconds)
    while t <= end:
        out.append(t)
        t = t + step
    return out


def check_book_invariants(book: OrderBookState) -> list[str]:
    errors: list[str] = []
    if not book.has_snapshot:
        errors.append("book has no snapshot applied")
    if not book.bids:
        errors.append("empty bid side")
    if not book.asks:
        errors.append("empty ask side")
    bb = book.best_bid()
    ba = book.best_ask()
    if bb is not None and ba is not None and bb >= ba:
        errors.append(f"crossed book best_bid={bb} >= best_ask={ba}")
    for side_name, side in (("bid", book.bids), ("ask", book.asks)):
        for price, qty in side.items():
            if qty < 0:
                errors.append(f"negative qty on {side_name} {price}")
            if qty == 0:
                errors.append(f"zero qty level remains on {side_name} {price}")
    return errors


def replay_segment_events(
    events: Sequence[BookLevelEvent],
    *,
    segment: ReplaySegment,
    warmup_seconds: int = 300,
    sample_interval_seconds: int = 60,
) -> tuple[SegmentReplayResult, BookEndState | None, list[dict[str, Any]]]:
    """Replay one segment's events with OrderBookReplayer; return result + end state + samples."""
    t0 = time.perf_counter()
    start = _ensure_aware(segment.segment_start_ts)
    end = _ensure_aware(segment.segment_end_ts)
    boot_ts = _ensure_aware(segment.bootstrap_snapshot_ts)
    feature_start = start + timedelta(seconds=max(int(warmup_seconds), 0))
    post_warmup = max((end - feature_start).total_seconds(), 0.0)
    has_post_warmup = feature_start < end

    base = SegmentReplayResult(
        segment_id=segment.segment_id,
        symbol=segment.symbol,
        segment_start_ts=start,
        segment_end_ts=end,
        bootstrap_snapshot_ts=boot_ts,
        bootstrap_update_id=segment.bootstrap_update_id,
        expected_last_update_id=segment.last_update_id,
        actual_last_update_id=None,
        expected_last_cross_sequence=segment.last_cross_sequence,
        actual_last_cross_sequence=None,
        messages_loaded=0,
        snapshot_messages_loaded=0,
        delta_messages_loaded=0,
        events_or_level_rows_loaded=len(events),
        duration_sec=segment.duration_sec,
        warmup_seconds=int(warmup_seconds),
        feature_emission_start_ts=feature_start if has_post_warmup else None,
        post_warmup_duration_sec=post_warmup if has_post_warmup else 0.0,
        replay_status="REPLAY_FAILED_LOAD",
        invariants_ok=False,
    )

    if not events:
        base.replay_status = "REPLAY_FAILED_BOOTSTRAP"
        base.error_type = "bootstrap_missing"
        base.error_message = "no events loaded for segment"
        base.runtime_sec = time.perf_counter() - t0
        return base, None, []

    snap_msgs = 0
    delta_msgs = 0
    msg_count = 0
    for message_type, _u, _s, _ts, _levels in group_messages(events):
        msg_count += 1
        if message_type == "snapshot":
            snap_msgs += 1
        else:
            delta_msgs += 1
    base.messages_loaded = msg_count
    base.snapshot_messages_loaded = snap_msgs
    base.delta_messages_loaded = delta_msgs

    if snap_msgs < 1:
        base.replay_status = "REPLAY_FAILED_BOOTSTRAP"
        base.error_type = "bootstrap_missing"
        base.error_message = "filtered events contain no bootstrap snapshot"
        base.runtime_sec = time.perf_counter() - t0
        return base, None, []

    if snap_msgs > 1:
        base.replay_status = "REPLAY_FAILED_INVARIANT"
        base.error_type = "unexpected_mid_segment_snapshot"
        base.error_message = (
            f"expected exactly 1 snapshot in segment events, got {snap_msgs}"
        )
        base.runtime_sec = time.perf_counter() - t0
        return base, None, []

    # Capture samples during replay
    samples_wanted = sample_grid(
        start, end, interval_seconds=int(sample_interval_seconds)
    )
    remaining = list(samples_wanted)
    sample_rows: list[dict[str, Any]] = []

    replayer = OrderBookReplayer()
    prev_u: int | None = None
    prev_seq: int | None = None

    try:
        for message_type, update_id, seq, ts, levels in group_messages(events):
            ts = _ensure_aware(ts)
            if ts < boot_ts and message_type != "snapshot":
                base.replay_status = "REPLAY_FAILED_INVARIANT"
                base.error_type = "message_before_bootstrap"
                base.error_message = f"message {update_id} before bootstrap ts"
                base.error_ts = ts
                base.current_update_id = update_id
                base.runtime_sec = time.perf_counter() - t0
                return base, None, sample_rows
            if ts > end:
                base.replay_status = "REPLAY_FAILED_INVARIANT"
                base.error_type = "message_after_segment_end"
                base.error_message = f"message {update_id} after segment_end"
                base.error_ts = ts
                base.current_update_id = update_id
                base.runtime_sec = time.perf_counter() - t0
                return base, None, sample_rows

            # Sample book before applying messages with ts > sample
            while remaining and remaining[0] < ts:
                st = remaining.pop(0)
                if replayer.book.has_snapshot:
                    sample_rows.append(
                        _sample_row(segment.segment_id, st, replayer.book)
                    )

            try:
                replayer.apply_message(message_type, update_id, seq, ts, levels)
            except ReplayError as exc:
                msg = str(exc)
                if "update_id gap" in msg:
                    status = "REPLAY_FAILED_GAP"
                    et = "update_id_gap"
                elif "cross_sequence" in msg:
                    status = "REPLAY_FAILED_SEQUENCE"
                    et = "cross_sequence_backwards"
                elif "delta before snapshot" in msg or "no snapshot" in msg:
                    status = "REPLAY_FAILED_BOOTSTRAP"
                    et = "bootstrap"
                else:
                    status = "REPLAY_FAILED_SEQUENCE"
                    et = "replay_error"
                base.replay_status = status
                base.error_type = et
                base.error_message = msg
                base.error_ts = ts
                base.previous_update_id = prev_u
                base.current_update_id = update_id
                base.previous_cross_sequence = prev_seq
                base.current_cross_sequence = seq
                base.runtime_sec = time.perf_counter() - t0
                return base, None, sample_rows

            while remaining and remaining[0] == ts:
                st = remaining.pop(0)
                sample_rows.append(_sample_row(segment.segment_id, st, replayer.book))

            prev_u = update_id
            prev_seq = seq

        while remaining:
            st = remaining.pop(0)
            if st <= end and replayer.book.has_snapshot:
                sample_rows.append(_sample_row(segment.segment_id, st, replayer.book))

    except Exception as exc:  # noqa: BLE001 — surface as load/replay failure
        base.replay_status = "REPLAY_FAILED_LOAD"
        base.error_type = type(exc).__name__
        base.error_message = str(exc)
        base.runtime_sec = time.perf_counter() - t0
        return base, None, sample_rows

    book = replayer.book
    base.actual_last_update_id = book.last_update_id
    base.actual_last_cross_sequence = book.last_seq

    inv_errors = check_book_invariants(book)
    if book.last_update_id != segment.last_update_id:
        inv_errors.append(
            f"end update_id mismatch expected={segment.last_update_id} "
            f"actual={book.last_update_id}"
        )
    if book.last_seq != segment.last_cross_sequence:
        inv_errors.append(
            f"end cross_sequence mismatch expected={segment.last_cross_sequence} "
            f"actual={book.last_seq}"
        )
    if book.last_exchange_ts is not None and _ensure_aware(book.last_exchange_ts) > end:
        inv_errors.append("last exchange_ts after segment_end")

    if inv_errors:
        base.replay_status = "REPLAY_FAILED_INVARIANT"
        base.error_type = "invariant"
        base.error_message = "; ".join(inv_errors)
        base.invariants_ok = False
        base.runtime_sec = time.perf_counter() - t0
        return base, None, sample_rows

    end_state = book_end_state_from_book(
        book,
        segment_id=segment.segment_id,
        symbol=segment.symbol,
        end_ts=book.last_exchange_ts or end,
    )
    base.invariants_ok = True
    if has_post_warmup:
        base.replay_status = "REPLAY_OK"
    else:
        base.replay_status = "REPLAY_OK_NO_POST_WARMUP"
        base.feature_emission_start_ts = None
        base.post_warmup_duration_sec = 0.0
    base.runtime_sec = time.perf_counter() - t0
    return base, end_state, sample_rows


def _sample_row(segment_id: str, sample_ts: datetime, book: OrderBookState) -> dict[str, Any]:
    bb = book.best_bid()
    ba = book.best_ask()
    mid = book.mid_price()
    spread = book.spread()
    spread_bps = None
    if mid is not None and spread is not None and mid != 0:
        spread_bps = float(spread / mid * Decimal("10000"))
    bid_n = _side_depth_notional(book.bids)
    ask_n = _side_depth_notional(book.asks)
    return {
        "segment_id": segment_id,
        "sample_ts": _ensure_aware(sample_ts).isoformat(),
        "last_update_id": book.last_update_id,
        "best_bid": None if bb is None else format(bb, "f"),
        "best_ask": None if ba is None else format(ba, "f"),
        "mid_price": None if mid is None else format(mid, "f"),
        "spread_bps": spread_bps,
        "active_bid_levels": len(book.bids),
        "active_ask_levels": len(book.asks),
        "bid_depth_notional": format(bid_n, "f"),
        "ask_depth_notional": format(ask_n, "f"),
    }


def replay_all_segments(
    db: ReadOnlyClickHouse,
    *,
    symbol: str,
    segments: Sequence[ReplaySegment],
    warmup_seconds: int = 300,
    sample_interval_seconds: int = 60,
) -> dict[str, Any]:
    """Replay each segment; skip non-replayable with SKIPPED_NOT_REPLAYABLE."""
    results: list[SegmentReplayResult] = []
    end_states: list[BookEndState] = []
    samples: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    warnings: list[str] = []

    for seg in segments:
        if not seg.is_replayable:
            results.append(
                SegmentReplayResult(
                    segment_id=seg.segment_id,
                    symbol=symbol,
                    segment_start_ts=seg.segment_start_ts,
                    segment_end_ts=seg.segment_end_ts,
                    bootstrap_snapshot_ts=seg.bootstrap_snapshot_ts,
                    bootstrap_update_id=seg.bootstrap_update_id,
                    expected_last_update_id=seg.last_update_id,
                    actual_last_update_id=None,
                    expected_last_cross_sequence=seg.last_cross_sequence,
                    actual_last_cross_sequence=None,
                    messages_loaded=0,
                    snapshot_messages_loaded=0,
                    delta_messages_loaded=0,
                    events_or_level_rows_loaded=0,
                    duration_sec=seg.duration_sec,
                    warmup_seconds=warmup_seconds,
                    feature_emission_start_ts=None,
                    post_warmup_duration_sec=None,
                    replay_status="SKIPPED_NOT_REPLAYABLE",
                    invariants_ok=False,
                    error_type=None,
                    error_message=seg.discard_reason,
                )
            )
            continue

        logger.info(
            "Replaying %s %s .. %s (u=%s..%s)",
            seg.segment_id,
            seg.segment_start_ts.isoformat(),
            seg.segment_end_ts.isoformat(),
            seg.bootstrap_update_id,
            seg.last_update_id,
        )
        load_t0 = time.perf_counter()
        try:
            events = load_segment_events(db, symbol=symbol, segment=seg)
        except Exception as exc:  # noqa: BLE001
            rr = SegmentReplayResult(
                segment_id=seg.segment_id,
                symbol=symbol,
                segment_start_ts=seg.segment_start_ts,
                segment_end_ts=seg.segment_end_ts,
                bootstrap_snapshot_ts=seg.bootstrap_snapshot_ts,
                bootstrap_update_id=seg.bootstrap_update_id,
                expected_last_update_id=seg.last_update_id,
                actual_last_update_id=None,
                expected_last_cross_sequence=seg.last_cross_sequence,
                actual_last_cross_sequence=None,
                messages_loaded=0,
                snapshot_messages_loaded=0,
                delta_messages_loaded=0,
                events_or_level_rows_loaded=0,
                duration_sec=seg.duration_sec,
                warmup_seconds=warmup_seconds,
                feature_emission_start_ts=None,
                post_warmup_duration_sec=None,
                replay_status="REPLAY_FAILED_LOAD",
                invariants_ok=False,
                error_type=type(exc).__name__,
                error_message=str(exc),
                runtime_sec=time.perf_counter() - load_t0,
            )
            results.append(rr)
            er = rr.error_row()
            if er:
                errors.append(er)
            continue

        n_rows = len(events)
        if n_rows >= LEVEL_ROW_WARN_THRESHOLD:
            warnings.append(
                f"{seg.segment_id}: loaded {n_rows} level rows in memory "
                f"(>= {LEVEL_ROW_WARN_THRESHOLD}); consider Phase 2b chunking"
            )

        rr, end_state, seg_samples = replay_segment_events(
            events,
            segment=seg,
            warmup_seconds=warmup_seconds,
            sample_interval_seconds=sample_interval_seconds,
        )
        # include load time in runtime
        rr.runtime_sec = float(rr.runtime_sec) + (time.perf_counter() - load_t0)
        results.append(rr)
        samples.extend(seg_samples)
        if end_state is not None:
            end_states.append(end_state)
        er = rr.error_row()
        if er:
            errors.append(er)

    ok = [r for r in results if r.replay_status.startswith("REPLAY_OK")]
    failed = [
        r
        for r in results
        if r.replay_status.startswith("REPLAY_FAILED")
    ]
    skipped = [r for r in results if r.replay_status.startswith("SKIPPED")]
    no_warmup = [r for r in ok if r.replay_status == "REPLAY_OK_NO_POST_WARMUP"]

    return {
        "results": results,
        "end_states": end_states,
        "samples": samples,
        "errors": errors,
        "warnings": warnings,
        "stats": {
            "segments_total": len(segments),
            "segments_replayable": sum(1 for s in segments if s.is_replayable),
            "segments_replayed": len(ok) + len(failed),
            "segments_replay_ok": len(ok),
            "segments_replay_failed": len(failed),
            "segments_skipped": len(skipped),
            "segments_no_post_warmup": len(no_warmup),
            "messages_loaded_total": sum(r.messages_loaded for r in results),
            "level_rows_loaded_total": sum(r.events_or_level_rows_loaded for r in results),
            "replay_runtime_sec_total": sum(r.runtime_sec for r in results),
            "replay_invariants_ok": all(r.invariants_ok for r in ok) and len(failed) == 0,
        },
    }


def decide_phase2(
    *,
    phase01_decision: str,
    gap_count: int,
    stats: Mapping[str, Any],
) -> str:
    ok = int(stats.get("segments_replay_ok") or 0)
    failed = int(stats.get("segments_replay_failed") or 0)
    replayable = int(stats.get("segments_replayable") or 0)
    if replayable == 0:
        return "FULL_HISTORY_SEGMENT_REPLAY_FAILED"
    if failed > 0 and ok == 0:
        return "FULL_HISTORY_SEGMENT_REPLAY_FAILED"
    if failed > 0:
        return "FULL_HISTORY_SEGMENT_REPLAY_PARTIAL"
    if gap_count > 0 or "WITH_GAPS" in phase01_decision:
        return "FULL_HISTORY_SEGMENT_REPLAY_COMPLETE_WITH_GAPS"
    return "FULL_HISTORY_SEGMENT_REPLAY_COMPLETE"
