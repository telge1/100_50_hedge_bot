"""Single-pass multi-cutoff causal replay for efficient validation."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from orderbook_analyse.ob200_v3_raw_discovery.files import SegmentRef
from orderbook_analyse.ob_data_source.ndjson_parse import parse_ob200_obj
from orderbook_analyse.orderbook_v2_live.clock import LiveSecondClock
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
)
from .engine import _annotate_row, _msg_to_data, segments_up_to


@dataclass
class CutoffSnapshot:
    as_of_exclusive_ms: int
    finalized: list[ContractBucket]
    provisional: list[ContractBucket]
    instrumentation: ReplayInstrumentation


def run_single_pass_cutoffs(
    segments: list[SegmentRef],
    *,
    symbol: str,
    as_of_cutoffs_ms: list[int],
) -> dict[int, CutoffSnapshot]:
    """Process raw archive once; snapshot finalized buckets at each ascending cutoff."""
    if not as_of_cutoffs_ms:
        return {}

    cutoffs = sorted(set(as_of_cutoffs_ms))
    active = segments_up_to(segments, cutoffs[-1])
    clock = LiveSecondClock(symbol)
    results: dict[int, CutoffSnapshot] = {}
    cutoff_idx = 0
    seed_checkpoint_ts_ms: int | None = None
    seen_first_checkpoint = False
    max_read: int | None = None
    max_applied: int | None = None
    current_segment: str | None = None
    # All rows emitted by ingest() during the pass — source of truth for finalized buckets.
    accumulated: list[tuple[dict[str, Any], str | None]] = []

    def snapshot_at(as_of_ms: int) -> CutoffSnapshot:
        nonlocal seed_checkpoint_ts_ms, max_applied, current_segment
        finalized: list[ContractBucket] = []
        provisional: list[ContractBucket] = []
        seen: set[int] = set()

        for row, seg_path in accumulated:
            if not row.get("is_valid"):
                continue
            bs = row_bucket_ms(row)
            if bs in seen or bucket_end_ms(bs) > as_of_ms:
                continue
            seen.add(bs)
            cb = _annotate_row(
                row,
                as_of_exclusive_ms=as_of_ms,
                segment_path=seg_path,
                seed_checkpoint_ts_ms=seed_checkpoint_ts_ms,
                max_event_applied_ms=max_applied or 0,
            )
            finalized.append(cb)

        # Supplement with close_through on clock copy for buckets not yet emitted via ingest
        # (e.g. event-free seconds at tail before next event arrives).
        clock_copy = copy.deepcopy(clock)
        for row in clock_copy.close_through(as_of_ms):
            if not row.get("is_valid"):
                continue
            bs = row_bucket_ms(row)
            if bs in seen:
                continue
            seen.add(bs)
            cb = _annotate_row(
                row,
                as_of_exclusive_ms=as_of_ms,
                segment_path=current_segment,
                seed_checkpoint_ts_ms=seed_checkpoint_ts_ms,
                max_event_applied_ms=max_applied or 0,
            )
            if cb.is_final:
                finalized.append(cb)
            else:
                provisional.append(cb)

        if (
            clock_copy.current_bucket_ms is not None
            and clock_copy.last_valid_book
            and clock_copy.last_valid_book.is_valid
        ):
            open_bs = clock_copy.current_bucket_ms
            if open_bs not in seen and not is_bucket_final(open_bs, as_of_ms):
                from orderbook_analyse.orderbook_v2.dynamics import build_event_feature_row

                row = build_event_feature_row(
                    clock_copy.last_valid_book,
                    open_bs,
                    clock_copy.first_ts_in_bucket or open_bs,
                    clock_copy.last_valid_ts_ms,
                    clock_copy.n_updates_in_bucket,
                    exchange=clock_copy.exchange,
                    market=clock_copy.market,
                    symbol=clock_copy.symbol,
                    depth=clock_copy.depth,
                    quality_flags=clock_copy.bucket_quality_flags or None,
                    delta_data=clock_copy.delta_data_in_bucket,
                    book_at_bucket_start=clock_copy.book_at_bucket_start,
                    prev_mid=clock_copy.prev_mid_for_change,
                )
                if row.get("is_valid") == 1:
                    cb = _annotate_row(
                        row,
                        as_of_exclusive_ms=as_of_ms,
                        segment_path=current_segment,
                        seed_checkpoint_ts_ms=seed_checkpoint_ts_ms,
                        max_event_applied_ms=max_applied or 0,
                    )
                    cb.is_final = False
                    provisional.append(cb)

        finalized.sort(key=lambda b: b.bucket_start_ms)
        provisional.sort(key=lambda b: b.bucket_start_ms)
        inst = ReplayInstrumentation(requested_as_of_exclusive_ms=as_of_ms)
        inst.max_raw_event_ts_read_ms = max_read
        inst.max_raw_event_ts_applied_ms = max_applied
        inst.seed_checkpoint_ts_ms = seed_checkpoint_ts_ms
        inst.final_bucket_count = len(finalized)
        inst.provisional_bucket_count = len(provisional)
        if finalized:
            inst.first_final_bucket_ms = finalized[0].bucket_start_ms
            inst.last_final_bucket_ms = finalized[-1].bucket_start_ms
            inst.max_information_time_final_ms = max(b.information_time_ms for b in finalized)
        inst.segments_used = [s.path.name for s in active if int(s.start_utc.timestamp() * 1000) < as_of_ms]
        return CutoffSnapshot(
            as_of_exclusive_ms=as_of_ms,
            finalized=finalized,
            provisional=provisional,
            instrumentation=inst,
        )

    for seg in active:
        current_segment = str(seg.path)
        for obj in iter_segment_lines(seg.path):
            if not is_replayable_line(obj):
                continue
            msg = parse_ob200_obj(
                line_to_replay_payload(obj), expected_symbol=symbol
            )
            ts = msg.raw_ts_ms
            max_read = ts if max_read is None else max(max_read, ts)

            while cutoff_idx < len(cutoffs) and ts >= cutoffs[cutoff_idx]:
                results[cutoffs[cutoff_idx]] = snapshot_at(cutoffs[cutoff_idx])
                cutoff_idx += 1

            if cutoff_idx >= len(cutoffs):
                return results

            if obj.get("type") == "rotation_checkpoint" and not seen_first_checkpoint:
                seed_checkpoint_ts_ms = ts
                seen_first_checkpoint = True

            for row in clock.ingest(msg.message_type, ts, _msg_to_data(msg)):
                accumulated.append((row, current_segment))
            max_applied = ts if max_applied is None else max(max_applied, ts)

    while cutoff_idx < len(cutoffs):
        results[cutoffs[cutoff_idx]] = snapshot_at(cutoffs[cutoff_idx])
        cutoff_idx += 1

    return results


def snapshot_to_replay_result(snap: CutoffSnapshot, symbol: str) -> ReplayResult:
    return ReplayResult(
        symbol=symbol,
        as_of_exclusive_ms=snap.as_of_exclusive_ms,
        finalized=snap.finalized,
        provisional=snap.provisional,
        instrumentation=snap.instrumentation,
    )
