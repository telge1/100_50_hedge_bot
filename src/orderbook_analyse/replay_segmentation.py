"""Replay segment discovery for full-history orderbook analysis (Phase 0/1).

Continuity (aligned with orderbook_replay / bybit_recorder):
  - primary: current_update_id == previous_update_id + 1
  - cross_sequence may jump forward; backwards without snapshot is anomalous
  - a complete snapshot resets state and starts a new segment

Segmentation works on message keys, not individual bid/ask level rows:

  (exchange_ts, update_id, cross_sequence, message_type)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from orderbook_analyse.dynamic_wall_detector import ReadOnlyClickHouse, _ensure_aware


@dataclass(frozen=True)
class OrderbookMessage:
    exchange_ts: datetime
    update_id: int
    cross_sequence: int
    message_type: str
    bid_level_count: int
    ask_level_count: int
    total_level_count: int

    @property
    def is_snapshot(self) -> bool:
        return self.message_type == "snapshot"

    @property
    def is_delta(self) -> bool:
        return self.message_type == "delta"

    def is_complete(self, min_levels_per_side: int) -> bool:
        return (
            self.is_snapshot
            and self.bid_level_count >= min_levels_per_side
            and self.ask_level_count >= min_levels_per_side
        )


@dataclass
class SnapshotInventoryRow:
    snapshot_ts: datetime
    update_id: int
    cross_sequence: int
    bid_level_count: int
    ask_level_count: int
    total_level_count: int
    is_complete: bool

    def to_row(self) -> dict[str, Any]:
        return {
            "snapshot_ts": self.snapshot_ts.isoformat(),
            "update_id": self.update_id,
            "cross_sequence": self.cross_sequence,
            "bid_level_count": self.bid_level_count,
            "ask_level_count": self.ask_level_count,
            "total_level_count": self.total_level_count,
            "is_complete": self.is_complete,
        }


@dataclass
class ReplaySegment:
    segment_id: str
    symbol: str
    segment_start_ts: datetime
    segment_end_ts: datetime
    bootstrap_snapshot_ts: datetime
    bootstrap_update_id: int
    bootstrap_cross_sequence: int
    first_delta_update_id: int | None
    last_update_id: int
    last_cross_sequence: int
    message_count: int
    delta_message_count: int
    snapshot_message_count: int
    duration_sec: float
    bid_snapshot_levels: int
    ask_snapshot_levels: int
    is_replayable: bool
    discard_reason: str | None
    end_reason: str

    def to_row(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "symbol": self.symbol,
            "segment_start_ts": self.segment_start_ts.isoformat(),
            "segment_end_ts": self.segment_end_ts.isoformat(),
            "bootstrap_snapshot_ts": self.bootstrap_snapshot_ts.isoformat(),
            "bootstrap_update_id": self.bootstrap_update_id,
            "bootstrap_cross_sequence": self.bootstrap_cross_sequence,
            "first_delta_update_id": self.first_delta_update_id,
            "last_update_id": self.last_update_id,
            "last_cross_sequence": self.last_cross_sequence,
            "message_count": self.message_count,
            "delta_message_count": self.delta_message_count,
            "snapshot_message_count": self.snapshot_message_count,
            "duration_sec": self.duration_sec,
            "bid_snapshot_levels": self.bid_snapshot_levels,
            "ask_snapshot_levels": self.ask_snapshot_levels,
            "is_replayable": self.is_replayable,
            "discard_reason": self.discard_reason,
            "end_reason": self.end_reason,
        }


@dataclass
class ReplayGap:
    gap_id: str
    symbol: str
    gap_start_ts: datetime
    gap_end_ts: datetime
    previous_update_id: int | None
    next_update_id: int | None
    missing_update_count: int
    previous_cross_sequence: int | None
    next_cross_sequence: int | None
    next_message_type: str | None
    next_snapshot_complete: bool | None
    recovered_at_snapshot_ts: datetime | None
    discarded_duration_sec: float
    reason: str

    def to_row(self) -> dict[str, Any]:
        return {
            "gap_id": self.gap_id,
            "symbol": self.symbol,
            "gap_start_ts": self.gap_start_ts.isoformat(),
            "gap_end_ts": self.gap_end_ts.isoformat(),
            "previous_update_id": self.previous_update_id,
            "next_update_id": self.next_update_id,
            "missing_update_count": self.missing_update_count,
            "previous_cross_sequence": self.previous_cross_sequence,
            "next_cross_sequence": self.next_cross_sequence,
            "next_message_type": self.next_message_type,
            "next_snapshot_complete": self.next_snapshot_complete,
            "recovered_at_snapshot_ts": None
            if self.recovered_at_snapshot_ts is None
            else self.recovered_at_snapshot_ts.isoformat(),
            "discarded_duration_sec": self.discarded_duration_sec,
            "reason": self.reason,
        }


@dataclass
class SegmentationResult:
    messages: list[OrderbookMessage] = field(default_factory=list)
    snapshots: list[SnapshotInventoryRow] = field(default_factory=list)
    segments: list[ReplaySegment] = field(default_factory=list)
    gaps: list[ReplayGap] = field(default_factory=list)
    incomplete_snapshot_count: int = 0
    complete_snapshot_count: int = 0
    backwards_sequence_count: int = 0
    update_id_gap_events: int = 0


def missing_update_count(previous_update_id: int, next_update_id: int) -> int:
    """Number of missing update_ids strictly between previous and next."""
    return max(int(next_update_id) - int(previous_update_id) - 1, 0)


def load_orderbook_messages(
    db: ReadOnlyClickHouse,
    *,
    symbol: str,
    start: datetime,
    end: datetime,
) -> list[OrderbookMessage]:
    """Load orderbook messages aggregated to message-key level (read-only)."""
    start = _ensure_aware(start)
    end = _ensure_aware(end)
    rows = db.query(
        """
        SELECT
            exchange_ts,
            update_id,
            cross_sequence,
            message_type,
            countIf(side = 'bid') AS bid_level_count,
            countIf(side = 'ask') AS ask_level_count,
            count() AS total_level_count
        FROM orderbook_deltas
        WHERE symbol = %(symbol)s
          AND exchange_ts >= %(start)s
          AND exchange_ts <= %(end)s
        GROUP BY exchange_ts, update_id, cross_sequence, message_type
        ORDER BY exchange_ts ASC, cross_sequence ASC, update_id ASC, message_type ASC
        """,
        parameters={"symbol": symbol, "start": start, "end": end},
    ).result_rows
    out: list[OrderbookMessage] = []
    for row in rows:
        ts = _ensure_aware(row[0])
        msg_type = str(row[3])
        out.append(
            OrderbookMessage(
                exchange_ts=ts,
                update_id=int(row[1]),
                cross_sequence=int(row[2]),
                message_type=msg_type,
                bid_level_count=int(row[4] or 0),
                ask_level_count=int(row[5] or 0),
                total_level_count=int(row[6] or 0),
            )
        )
    return out


def build_snapshot_inventory(
    messages: Sequence[OrderbookMessage],
    *,
    min_snapshot_levels_per_side: int,
) -> list[SnapshotInventoryRow]:
    rows: list[SnapshotInventoryRow] = []
    for msg in messages:
        if not msg.is_snapshot:
            continue
        complete = msg.is_complete(min_snapshot_levels_per_side)
        rows.append(
            SnapshotInventoryRow(
                snapshot_ts=msg.exchange_ts,
                update_id=msg.update_id,
                cross_sequence=msg.cross_sequence,
                bid_level_count=msg.bid_level_count,
                ask_level_count=msg.ask_level_count,
                total_level_count=msg.total_level_count,
                is_complete=complete,
            )
        )
    return rows


def discover_replay_segments(
    messages: Sequence[OrderbookMessage],
    *,
    symbol: str,
    min_snapshot_levels_per_side: int = 150,
    segment_minutes_min: float = 5.0,
    analysis_end: datetime | None = None,
) -> SegmentationResult:
    """Walk message stream and emit replayable segments + gaps.

    Pure function over an in-memory message list (CH or synthetic fixtures).
    """
    result = SegmentationResult(messages=list(messages))
    result.snapshots = build_snapshot_inventory(
        messages, min_snapshot_levels_per_side=min_snapshot_levels_per_side
    )
    result.complete_snapshot_count = sum(1 for s in result.snapshots if s.is_complete)
    result.incomplete_snapshot_count = sum(1 for s in result.snapshots if not s.is_complete)

    if not messages:
        return result

    analysis_end_ts = _ensure_aware(analysis_end) if analysis_end is not None else None
    min_duration_sec = float(segment_minutes_min) * 60.0

    seg_n = 0
    gap_n = 0
    active: dict[str, Any] | None = None
    awaiting_snapshot = False
    discard_start_ts: datetime | None = None
    discard_prev_u: int | None = None
    discard_prev_seq: int | None = None
    discard_reason: str | None = None

    def _close_active(
        *,
        end_ts: datetime,
        end_reason: str,
        last_u: int,
        last_seq: int,
    ) -> None:
        nonlocal active, seg_n
        assert active is not None
        start_ts: datetime = active["start_ts"]
        duration = max((end_ts - start_ts).total_seconds(), 0.0)
        seg_n += 1
        replayable = duration >= min_duration_sec
        if not replayable:
            end_reason_out = "insufficient_duration"
            discard = "insufficient_duration"
        else:
            end_reason_out = end_reason
            discard = None
        result.segments.append(
            ReplaySegment(
                segment_id=f"S{seg_n:04d}",
                symbol=symbol,
                segment_start_ts=start_ts,
                segment_end_ts=end_ts,
                bootstrap_snapshot_ts=active["boot_ts"],
                bootstrap_update_id=int(active["boot_u"]),
                bootstrap_cross_sequence=int(active["boot_seq"]),
                first_delta_update_id=active.get("first_delta_u"),
                last_update_id=last_u,
                last_cross_sequence=last_seq,
                message_count=int(active["message_count"]),
                delta_message_count=int(active["delta_count"]),
                snapshot_message_count=int(active["snapshot_count"]),
                duration_sec=duration,
                bid_snapshot_levels=int(active["bid_levels"]),
                ask_snapshot_levels=int(active["ask_levels"]),
                is_replayable=replayable,
                discard_reason=discard,
                end_reason=end_reason_out,
            )
        )
        active = None

    def _emit_gap(
        *,
        start_ts: datetime,
        end_ts: datetime,
        prev_u: int | None,
        next_u: int | None,
        prev_seq: int | None,
        next_seq: int | None,
        next_type: str | None,
        next_complete: bool | None,
        recovered_ts: datetime | None,
        reason: str,
    ) -> None:
        nonlocal gap_n
        gap_n += 1
        missing = 0
        if prev_u is not None and next_u is not None:
            missing = missing_update_count(prev_u, next_u)
        result.gaps.append(
            ReplayGap(
                gap_id=f"G{gap_n:04d}",
                symbol=symbol,
                gap_start_ts=start_ts,
                gap_end_ts=end_ts,
                previous_update_id=prev_u,
                next_update_id=next_u,
                missing_update_count=missing,
                previous_cross_sequence=prev_seq,
                next_cross_sequence=next_seq,
                next_message_type=next_type,
                next_snapshot_complete=next_complete,
                recovered_at_snapshot_ts=recovered_ts,
                discarded_duration_sec=max((end_ts - start_ts).total_seconds(), 0.0),
                reason=reason,
            )
        )

    def _start_segment(msg: OrderbookMessage) -> None:
        nonlocal active, awaiting_snapshot, discard_start_ts
        active = {
            "start_ts": msg.exchange_ts,
            "boot_ts": msg.exchange_ts,
            "boot_u": msg.update_id,
            "boot_seq": msg.cross_sequence,
            "last_u": msg.update_id,
            "last_seq": msg.cross_sequence,
            "last_ts": msg.exchange_ts,
            "first_delta_u": None,
            "message_count": 1,
            "delta_count": 0,
            "snapshot_count": 1,
            "bid_levels": msg.bid_level_count,
            "ask_levels": msg.ask_level_count,
        }
        awaiting_snapshot = False
        discard_start_ts = None

    for msg in messages:
        complete_snap = msg.is_complete(min_snapshot_levels_per_side)

        # Waiting for recovery snapshot after a hard break.
        if awaiting_snapshot:
            if msg.is_snapshot and complete_snap:
                assert discard_start_ts is not None
                _emit_gap(
                    start_ts=discard_start_ts,
                    end_ts=msg.exchange_ts,
                    prev_u=discard_prev_u,
                    next_u=msg.update_id,
                    prev_seq=discard_prev_seq,
                    next_seq=msg.cross_sequence,
                    next_type=msg.message_type,
                    next_complete=True,
                    recovered_ts=msg.exchange_ts,
                    reason=discard_reason or "update_id_gap",
                )
                _start_segment(msg)
            elif msg.is_snapshot and not complete_snap:
                # incomplete snapshot does not recover
                continue
            else:
                continue
            continue

        # No active segment: only a complete snapshot can start one.
        if active is None:
            if msg.is_snapshot and complete_snap:
                _start_segment(msg)
            elif msg.is_snapshot and not complete_snap:
                # recorded but not usable as bootstrap
                continue
            else:
                # orphan deltas before first complete snapshot
                continue
            continue

        # Active segment present.
        last_u = int(active["last_u"])
        last_seq = int(active["last_seq"])

        # Explicit snapshot reset (even without update_id gap).
        if msg.is_snapshot:
            if complete_snap:
                prev_end = active.get("last_ts", active["start_ts"])
                had_gap = msg.update_id != last_u + 1
                if had_gap:
                    result.update_id_gap_events += 1
                end_reason = "update_id_gap" if had_gap else "next_snapshot_reset"
                gap_reason = "update_id_gap" if had_gap else "next_snapshot_reset"
                _close_active(
                    end_ts=prev_end,
                    end_reason=end_reason,
                    last_u=last_u,
                    last_seq=last_seq,
                )
                _emit_gap(
                    start_ts=prev_end,
                    end_ts=msg.exchange_ts,
                    prev_u=last_u,
                    next_u=msg.update_id,
                    prev_seq=last_seq,
                    next_seq=msg.cross_sequence,
                    next_type="snapshot",
                    next_complete=True,
                    recovered_ts=msg.exchange_ts,
                    reason=gap_reason,
                )
                _start_segment(msg)
            else:
                # Incomplete snapshot breaks continuity → discard until complete snap.
                result.update_id_gap_events += 1 if msg.update_id != last_u + 1 else 0
                prev_end = active.get("last_ts", active["start_ts"])
                _close_active(
                    end_ts=prev_end,
                    end_reason="incomplete_snapshot",
                    last_u=last_u,
                    last_seq=last_seq,
                )
                awaiting_snapshot = True
                discard_start_ts = prev_end
                discard_prev_u = last_u
                discard_prev_seq = last_seq
                discard_reason = "incomplete_snapshot"
            continue

        # Delta message
        if msg.cross_sequence < last_seq:
            result.backwards_sequence_count += 1
            prev_end = active.get("last_ts", active["start_ts"])
            _close_active(
                end_ts=prev_end,
                end_reason="cross_sequence_backwards",
                last_u=last_u,
                last_seq=last_seq,
            )
            awaiting_snapshot = True
            discard_start_ts = prev_end
            discard_prev_u = last_u
            discard_prev_seq = last_seq
            discard_reason = "cross_sequence_backwards"
            continue

        if msg.update_id != last_u + 1:
            result.update_id_gap_events += 1
            prev_end = active.get("last_ts", active["start_ts"])
            _close_active(
                end_ts=prev_end,
                end_reason="update_id_gap",
                last_u=last_u,
                last_seq=last_seq,
            )
            # If this delta is the problem, discard until next complete snapshot.
            awaiting_snapshot = True
            discard_start_ts = prev_end
            discard_prev_u = last_u
            discard_prev_seq = last_seq
            discard_reason = "update_id_gap"
            # Do not apply the gapped delta.
            continue

        # Continuous delta — extend segment.
        active["last_u"] = msg.update_id
        active["last_seq"] = msg.cross_sequence
        active["last_ts"] = msg.exchange_ts
        active["message_count"] += 1
        active["delta_count"] += 1
        if active["first_delta_u"] is None:
            active["first_delta_u"] = msg.update_id

    # Close trailing active segment at analysis end / last message.
    if active is not None:
        end_ts = active.get("last_ts", active["start_ts"])
        if analysis_end_ts is not None and analysis_end_ts > end_ts:
            # Prefer last message ts as factual segment end (no data after).
            pass
        end_reason = "analysis_end"
        if analysis_end_ts is not None and end_ts < analysis_end_ts:
            end_reason = "analysis_end"
        _close_active(
            end_ts=end_ts,
            end_reason=end_reason,
            last_u=int(active["last_u"]),
            last_seq=int(active["last_seq"]),
        )

    # Trailing discard zone without recovery.
    if awaiting_snapshot and discard_start_ts is not None:
        last_ts = messages[-1].exchange_ts
        _emit_gap(
            start_ts=discard_start_ts,
            end_ts=last_ts,
            prev_u=discard_prev_u,
            next_u=None,
            prev_seq=discard_prev_seq,
            next_seq=None,
            next_type=None,
            next_complete=None,
            recovered_ts=None,
            reason=discard_reason or "missing_following_data",
        )

    return result


def segmentation_integrity_checks(
    result: SegmentationResult,
    *,
    min_snapshot_levels_per_side: int,
) -> dict[str, Any]:
    """Validate segment/gap invariants for integrity.json."""
    errors: list[str] = []
    warnings: list[str] = []

    # Chronological + non-overlapping segments
    prev_end: datetime | None = None
    for seg in result.segments:
        if seg.segment_end_ts < seg.segment_start_ts:
            errors.append(f"{seg.segment_id}: end before start")
        if prev_end is not None and seg.segment_start_ts < prev_end:
            errors.append(f"{seg.segment_id}: overlaps previous segment")
        prev_end = max(prev_end or seg.segment_end_ts, seg.segment_end_ts)
        # bootstrap must be complete snapshot
        boot_ok = any(
            s.snapshot_ts == seg.bootstrap_snapshot_ts
            and s.update_id == seg.bootstrap_update_id
            and s.cross_sequence == seg.bootstrap_cross_sequence
            and s.is_complete
            for s in result.snapshots
        )
        if not boot_ok:
            # allow synthetic when inventory missing the exact row
            if seg.bid_snapshot_levels < min_snapshot_levels_per_side or (
                seg.ask_snapshot_levels < min_snapshot_levels_per_side
            ):
                errors.append(f"{seg.segment_id}: bootstrap snapshot incomplete")

    # Within-segment update_id continuity (recompute from messages)
    by_seg_msgs: dict[str, list[OrderbookMessage]] = {}
    # Map using time windows of replayable+nonreplayable segments
    for seg in result.segments:
        msgs = [
            m
            for m in result.messages
            if seg.segment_start_ts <= m.exchange_ts <= seg.segment_end_ts
            and (
                m.update_id >= seg.bootstrap_update_id
                or m.exchange_ts == seg.bootstrap_snapshot_ts
            )
        ]
        # tighter: from bootstrap message through last_update_id chronologically
        filtered: list[OrderbookMessage] = []
        started = False
        for m in result.messages:
            if (
                m.exchange_ts == seg.bootstrap_snapshot_ts
                and m.update_id == seg.bootstrap_update_id
                and m.is_snapshot
            ):
                started = True
            if not started:
                continue
            if m.exchange_ts > seg.segment_end_ts:
                break
            if m.exchange_ts == seg.segment_end_ts and m.update_id > seg.last_update_id:
                break
            filtered.append(m)
            if (
                m.update_id == seg.last_update_id
                and m.cross_sequence == seg.last_cross_sequence
                and m.exchange_ts == seg.segment_end_ts
            ):
                break
        by_seg_msgs[seg.segment_id] = filtered
        prev_u: int | None = None
        for m in filtered:
            if prev_u is not None and m.is_delta and m.update_id != prev_u + 1:
                errors.append(
                    f"{seg.segment_id}: internal update_id gap {prev_u}->{m.update_id}"
                )
            if m.is_snapshot and prev_u is not None and m.update_id != seg.bootstrap_update_id:
                # only bootstrap snapshot expected at start
                if m.exchange_ts != seg.bootstrap_snapshot_ts:
                    errors.append(f"{seg.segment_id}: unexpected mid-segment snapshot")
            prev_u = m.update_id

    # Gaps unique by (start, end, prev_u, next_u, reason)
    seen_gaps: set[tuple[Any, ...]] = set()
    for g in result.gaps:
        key = (
            g.gap_start_ts.isoformat(),
            g.gap_end_ts.isoformat(),
            g.previous_update_id,
            g.next_update_id,
            g.reason,
        )
        if key in seen_gaps:
            errors.append(f"duplicate gap {g.gap_id}")
        seen_gaps.add(key)
        if g.recovered_at_snapshot_ts is not None:
            recovered_ok = any(
                s.is_complete and s.snapshot_ts == g.recovered_at_snapshot_ts
                for s in result.snapshots
            )
            if not recovered_ok:
                warnings.append(
                    f"{g.gap_id}: recovery snapshot not in complete inventory"
                )

    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "segment_count": len(result.segments),
        "gap_count": len(result.gaps),
        "checked_internal_continuity": True,
    }
