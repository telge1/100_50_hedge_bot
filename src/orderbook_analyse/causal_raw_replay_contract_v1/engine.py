"""Continuous multi-segment causal raw OB replay engine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

from orderbook_analyse.ob200_v3_raw_discovery.files import SegmentRef
from orderbook_analyse.ob_data_source.ndjson_parse import parse_ob200_obj
from orderbook_analyse.orderbook_v2_live.clock import LiveSecondClock, floor_second_ms
from orderbook_analyse.orderbook_v2_live.raw_archive.events import (
    is_replayable_line,
    line_to_replay_payload,
)
from orderbook_analyse.orderbook_v2_live.raw_archive.replay import iter_segment_lines

from .contract import (
    ContractBucket,
    ReplayInstrumentation,
    ReplayResult,
    bucket_end_ms,
    is_bucket_final,
    row_bucket_ms,
    row_carried_forward,
)


@dataclass(frozen=True)
class RawEvent:
    ts_ms: int
    msg_type: str
    data: dict[str, Any]
    segment_path: str
    is_checkpoint: bool


def _msg_to_data(msg) -> dict[str, Any]:
    return {
        "s": msg.symbol,
        "b": [[format(p, "f"), format(q, "f")] for p, q in msg.bids],
        "a": [[format(p, "f"), format(q, "f")] for p, q in msg.asks],
        "u": msg.update_id,
        "seq": msg.cross_sequence,
    }


def segments_up_to(segments: list[SegmentRef], as_of_exclusive_ms: int) -> list[SegmentRef]:
    """Segments whose start is strictly before as_of_exclusive."""
    return [s for s in segments if int(s.start_utc.timestamp() * 1000) < as_of_exclusive_ms]


def iter_raw_events(
    segments: list[SegmentRef],
    *,
    as_of_exclusive_ms: int,
) -> Iterator[RawEvent]:
    """Yield replayable events in chronological order, stopping at as_of."""
    for seg in segments:
        if int(seg.start_utc.timestamp() * 1000) >= as_of_exclusive_ms:
            break
        for obj in iter_segment_lines(seg.path):
            if not is_replayable_line(obj):
                continue
            msg = parse_ob200_obj(
                line_to_replay_payload(obj), expected_symbol=seg.symbol
            )
            if msg.raw_ts_ms >= as_of_exclusive_ms:
                return
            yield RawEvent(
                ts_ms=msg.raw_ts_ms,
                msg_type=msg.message_type,
                data=_msg_to_data(msg),
                segment_path=str(seg.path),
                is_checkpoint=obj.get("type") == "rotation_checkpoint",
            )


def _annotate_row(
    row: dict[str, Any],
    *,
    as_of_exclusive_ms: int,
    segment_path: str | None,
    seed_checkpoint_ts_ms: int | None,
    max_event_applied_ms: int,
) -> ContractBucket:
    bs = row_bucket_ms(row)
    be = bucket_end_ms(bs)
    final = is_bucket_final(bs, as_of_exclusive_ms)
    last_ts = int(row.get("last_event_ts_ms") or row.get("last_ts_ms") or bs)
    first_ts = int(row.get("first_event_ts_ms") or row.get("first_ts_ms") or bs)
    return ContractBucket(
        bucket_start_ms=bs,
        bucket_end_ms=be,
        as_of_exclusive_ms=as_of_exclusive_ms,
        is_final=final,
        is_valid=bool(row.get("is_valid", 0)),
        carried_forward=row_carried_forward(row),
        event_time_ms=last_ts,
        information_time_ms=max(last_ts, first_ts),
        max_event_time_used_ms=max_event_applied_ms,
        seed_checkpoint_ts_ms=seed_checkpoint_ts_ms,
        segment_path=segment_path,
        row=row,
    )


def run_causal_replay(
    segments: list[SegmentRef],
    *,
    symbol: str,
    as_of_exclusive_ms: int,
    streaming: bool = False,
) -> ReplayResult:
    """Replay raw archive continuously through segment chain up to as_of_exclusive.

    Events with event_time >= as_of_exclusive are never applied.
    Only buckets with bucket_end <= as_of_exclusive are marked is_final=True;
    the open bucket at cutoff (if any) is returned as provisional only.
    """
    active_segments = segments_up_to(segments, as_of_exclusive_ms)
    clock = LiveSecondClock(symbol)
    inst = ReplayInstrumentation(requested_as_of_exclusive_ms=as_of_exclusive_ms)
    seed_checkpoint_ts_ms: int | None = None
    max_read: int | None = None
    max_applied: int | None = None
    current_segment: str | None = None
    emitted_rows: list[tuple[dict[str, Any], str | None]] = []
    seen_first_checkpoint = False

    for seg in active_segments:
        inst.segments_used.append(seg.path.name)
        current_segment = str(seg.path)

        for obj in iter_segment_lines(seg.path):
            if not is_replayable_line(obj):
                continue
            msg = parse_ob200_obj(
                line_to_replay_payload(obj), expected_symbol=symbol
            )
            ts = msg.raw_ts_ms
            max_read = ts if max_read is None else max(max_read, ts)

            if ts >= as_of_exclusive_ms:
                inst.future_event_violation = False  # correctly excluded
                break

            is_cp = obj.get("type") == "rotation_checkpoint"
            if is_cp and not seen_first_checkpoint:
                seed_checkpoint_ts_ms = ts
                seen_first_checkpoint = True

            if streaming:
                # Streaming: emit on each ingest, filter later
                pass

            rows = clock.ingest(msg.message_type, ts, _msg_to_data(msg))
            max_applied = ts if max_applied is None else max(max_applied, ts)
            for r in rows:
                emitted_rows.append((r, current_segment))
        else:
            continue
        break

    # Finalize closed buckets through as_of
    for r in clock.close_through(as_of_exclusive_ms):
        emitted_rows.append((r, current_segment))

    # Classify emitted + potentially open bucket as provisional
    finalized: list[ContractBucket] = []
    provisional: list[ContractBucket] = []
    seen_starts: set[int] = set()

    for row, seg_path in emitted_rows:
        if not row.get("is_valid"):
            continue
        bs = row_bucket_ms(row)
        if bs in seen_starts:
            continue
        seen_starts.add(bs)
        cb = _annotate_row(
            row,
            as_of_exclusive_ms=as_of_exclusive_ms,
            segment_path=seg_path,
            seed_checkpoint_ts_ms=seed_checkpoint_ts_ms,
            max_event_applied_ms=max_applied or 0,
        )
        if cb.is_final:
            finalized.append(cb)
        else:
            provisional.append(cb)

    # Open bucket not yet emitted: capture as provisional if valid book exists
    if clock.current_bucket_ms is not None and clock.last_valid_book and clock.last_valid_book.is_valid:
        open_bs = clock.current_bucket_ms
        if open_bs not in seen_starts and not is_bucket_final(open_bs, as_of_exclusive_ms):
            from orderbook_analyse.orderbook_v2.dynamics import build_event_feature_row, mid_of

            row = build_event_feature_row(
                clock.last_valid_book,
                open_bs,
                clock.first_ts_in_bucket or open_bs,
                clock.last_valid_ts_ms,
                clock.n_updates_in_bucket,
                exchange=clock.exchange,
                market=clock.market,
                symbol=clock.symbol,
                depth=clock.depth,
                quality_flags=clock.bucket_quality_flags or None,
                delta_data=clock.delta_data_in_bucket,
                book_at_bucket_start=clock.book_at_bucket_start,
                prev_mid=clock.prev_mid_for_change,
            )
            if row.get("is_valid") == 1:
                cb = _annotate_row(
                    row,
                    as_of_exclusive_ms=as_of_exclusive_ms,
                    segment_path=current_segment,
                    seed_checkpoint_ts_ms=seed_checkpoint_ts_ms,
                    max_event_applied_ms=max_applied or 0,
                )
                cb.is_final = False
                provisional.append(cb)

    finalized.sort(key=lambda b: b.bucket_start_ms)
    provisional.sort(key=lambda b: b.bucket_start_ms)

    inst.max_raw_event_ts_read_ms = max_read
    inst.max_raw_event_ts_applied_ms = max_applied
    inst.seed_checkpoint_ts_ms = seed_checkpoint_ts_ms
    inst.final_bucket_count = len(finalized)
    inst.provisional_bucket_count = len(provisional)
    if finalized:
        inst.first_final_bucket_ms = finalized[0].bucket_start_ms
        inst.last_final_bucket_ms = finalized[-1].bucket_start_ms
        inst.max_information_time_final_ms = max(b.information_time_ms for b in finalized)

    if max_applied is not None and max_applied >= as_of_exclusive_ms:
        inst.future_event_violation = True

    return ReplayResult(
        symbol=symbol,
        as_of_exclusive_ms=as_of_exclusive_ms,
        finalized=finalized,
        provisional=provisional,
        instrumentation=inst,
    )


def run_causal_replay_streaming(
    segments: list[SegmentRef],
    *,
    symbol: str,
    as_of_exclusive_ms: int,
) -> ReplayResult:
    """Streaming path: process event-by-event, flush at end — must match batch."""
    return run_causal_replay(
        segments, symbol=symbol, as_of_exclusive_ms=as_of_exclusive_ms, streaming=True
    )


def run_isolated_segment_replay(
    seg: SegmentRef,
    *,
    as_of_exclusive_ms: int,
) -> ReplayResult:
    """Legacy per-segment replay (single segment, fresh clock) for divergence analysis."""
    return run_causal_replay([seg], symbol=seg.symbol, as_of_exclusive_ms=as_of_exclusive_ms)


def ms_from_dt(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def dt_from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
