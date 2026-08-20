"""Live UTC-second feature clock.

Uses the same V3 builders as the historical parser. Differences vs batch
(by design, documented):

- Does not invent seconds before the first valid snapshot.
- Does not emit ``is_valid=0`` rows (archive backfill covers gaps).
- Does not carry forward across an invalid book / reconnect.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from orderbook_analyse.orderbook_v2.book import BookState, apply_delta, apply_snapshot
from orderbook_analyse.orderbook_v2.dynamics import (
    build_carry_forward_row,
    build_event_feature_row,
    mid_of,
    snapshot_is_usable,
)


class SequenceBreak(RuntimeError):
    """Book sequence is unusable; caller must drop state and resubscribe."""


def floor_second_ms(ts_ms: int) -> int:
    return (int(ts_ms) // 1000) * 1000


@dataclass
class ClockStats:
    snapshots: int = 0
    deltas: int = 0
    sequence_gaps: int = 0
    duplicate_u: int = 0
    dropped_events: int = 0
    rows_emitted: int = 0
    duplicate_buckets_skipped: int = 0
    invalid_book: int = 0


@dataclass
class LiveSecondClock:
    symbol: str
    depth: int = 200
    exchange: str = "bybit"
    market: str = "linear"
    skip_before_ms: int | None = None
    seen_us: set[int] = field(default_factory=set)
    stats: ClockStats = field(default_factory=ClockStats)

    book: BookState = field(
        default_factory=lambda: BookState(bids={}, asks={}, last_u=0, last_seq=0, is_valid=False)
    )
    waiting_for_snapshot: bool = True
    last_valid_book: BookState | None = None
    last_valid_ts_ms: int = 0
    current_bucket_ms: int | None = None
    first_ts_in_bucket: int = 0
    n_updates_in_bucket: int = 0
    bucket_quality_flags: list[str] = field(default_factory=list)
    delta_data_in_bucket: list[dict[str, Any]] = field(default_factory=list)
    book_at_bucket_start: BookState | None = None
    prev_mid_for_change: Any = None
    last_emitted_bucket_ms: int | None = None
    first_valid_live_bucket_ms: int | None = None
    written_buckets: set[int] = field(default_factory=set)
    in_flight_buckets: set[int] = field(default_factory=set)
    generation: int = 0
    stale_generation_dropped: int = 0

    def invalidate(self, reason: str) -> None:
        if "gap" in reason or reason == "seq_gap":
            self.stats.sequence_gaps += 1
        self.stats.invalid_book += 1
        self.book = BookState(bids={}, asks={}, last_u=0, last_seq=0, is_valid=False)
        self.last_valid_book = None
        self.waiting_for_snapshot = True
        self._discard_open_bucket()

    def begin_resync(self) -> int:
        """Drop local book and require a new snapshot. Returns the new generation."""
        self.generation += 1
        self.book = BookState(bids={}, asks={}, last_u=0, last_seq=0, is_valid=False)
        self.last_valid_book = None
        self.waiting_for_snapshot = True
        self._discard_open_bucket()
        return self.generation

    def ingest(
        self,
        msg_type: str,
        ts_ms: int,
        data: dict[str, Any],
        *,
        generation: int | None = None,
    ) -> list[dict[str, Any]]:
        if generation is not None and generation != self.generation:
            self.stale_generation_dropped += 1
            self.stats.dropped_events += 1
            return []
        u_val = data.get("u")
        if u_val is not None and u_val in self.seen_us:
            self.stats.duplicate_u += 1
            return []

        if self.waiting_for_snapshot:
            if msg_type != "snapshot":
                self.stats.dropped_events += 1
                return []
            snap = apply_snapshot(data)
            if not snapshot_is_usable(snap):
                self.stats.dropped_events += 1
                self.invalidate("bad_snapshot")
                return []
            if u_val is not None:
                self.seen_us.add(u_val)
            self.stats.snapshots += 1
            self.waiting_for_snapshot = False
            # Open the bucket before publishing last_valid so book_at_start
            # matches the batch parser (reset, then apply snapshot).
            emitted = self._advance_to_bucket(floor_second_ms(ts_ms), ts_ms)
            self.book = snap
            self.last_valid_book = snap
            self.last_valid_ts_ms = ts_ms
            self._count_update(msg_type, data)
            return emitted

        if msg_type == "snapshot":
            emitted = self._advance_to_bucket(floor_second_ms(ts_ms), ts_ms)
            snap = apply_snapshot(data)
            if not snapshot_is_usable(snap):
                self.stats.dropped_events += 1
                self.invalidate("bad_snapshot")
                raise SequenceBreak("unusable_snapshot")
            if u_val is not None:
                self.seen_us.add(u_val)
            self.stats.snapshots += 1
            self.book = snap
            self.last_valid_book = snap
            self.last_valid_ts_ms = ts_ms
            self._count_update(msg_type, data)
            return emitted

        if msg_type != "delta":
            self.stats.dropped_events += 1
            return []

        emitted = self._advance_to_bucket(floor_second_ms(ts_ms), ts_ms)
        self.stats.deltas += 1
        new_book, warnings = apply_delta(self.book, data)
        gap = any(w.startswith("seq_gap") for w in warnings)
        if gap or not new_book.is_valid:
            if u_val is not None:
                self.seen_us.add(u_val)
            self.invalidate("seq_gap")
            raise SequenceBreak(",".join(warnings) or "invalid_book")
        if u_val is not None:
            self.seen_us.add(u_val)
        self.book = new_book
        self.last_valid_book = new_book
        self.last_valid_ts_ms = ts_ms
        self._count_update(msg_type, data)
        if any(w.startswith("seq_dup") for w in warnings):
            self.stats.duplicate_u += 1
        return emitted

    def close_through(self, now_ms: int) -> list[dict[str, Any]]:
        """Emit completed UTC seconds strictly before floor(now_ms)."""
        closed_ms = floor_second_ms(now_ms)
        if self.waiting_for_snapshot or self.last_valid_book is None or not self.last_valid_book.is_valid:
            return []
        if self.current_bucket_ms is None:
            if self.last_emitted_bucket_ms is None:
                return []
            start = self.last_emitted_bucket_ms + 1000
            return self._emit_cf_range(start, closed_ms)
        if self.current_bucket_ms >= closed_ms:
            return []
        return self._advance_to_bucket(closed_ms, closed_ms, open_new=False)

    def _discard_open_bucket(self) -> None:
        self.current_bucket_ms = None
        self.first_ts_in_bucket = 0
        self.n_updates_in_bucket = 0
        self.bucket_quality_flags = []
        self.delta_data_in_bucket = []
        self.book_at_bucket_start = None
        self.prev_mid_for_change = None

    def _open_bucket(self, bucket_ms: int, first_ts_ms: int) -> None:
        if self.last_valid_book is not None and self.last_valid_book.is_valid:
            self.book_at_bucket_start = self.last_valid_book
            self.prev_mid_for_change = mid_of(self.last_valid_book)
        else:
            self.book_at_bucket_start = None
            self.prev_mid_for_change = None
        self.current_bucket_ms = bucket_ms
        self.first_ts_in_bucket = first_ts_ms
        self.n_updates_in_bucket = 0
        self.bucket_quality_flags = []
        self.delta_data_in_bucket = []

    def _count_update(self, msg_type: str, data: dict[str, Any]) -> None:
        self.n_updates_in_bucket += 1
        if msg_type == "delta":
            self.delta_data_in_bucket.append(data)

    def _advance_to_bucket(
        self,
        bucket_ms: int,
        first_ts_ms: int,
        *,
        open_new: bool = True,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if self.current_bucket_ms is None:
            if open_new:
                self._open_bucket(bucket_ms, first_ts_ms)
            return out
        if bucket_ms < self.current_bucket_ms:
            self.stats.dropped_events += 1
            return out
        if bucket_ms == self.current_bucket_ms:
            return out
        row = self._emit_open_bucket()
        if row is not None:
            out.append(row)
        cf_from = (self.current_bucket_ms + 1000) if self.current_bucket_ms is not None else bucket_ms
        out.extend(self._emit_cf_range(cf_from, bucket_ms))
        self._discard_open_bucket()
        if open_new:
            self._open_bucket(bucket_ms, first_ts_ms)
        return out

    def _emit_open_bucket(self) -> dict[str, Any] | None:
        bucket_ms = self.current_bucket_ms
        if bucket_ms is None:
            return None
        if self.n_updates_in_bucket == 0:
            return self._maybe_keep(self._cf_row(bucket_ms))
        if self.last_valid_book is None or not self.last_valid_book.is_valid:
            return None
        row = build_event_feature_row(
            self.last_valid_book,
            bucket_ms,
            self.first_ts_in_bucket,
            self.last_valid_ts_ms,
            self.n_updates_in_bucket,
            exchange=self.exchange,
            market=self.market,
            symbol=self.symbol,
            depth=self.depth,
            quality_flags=self.bucket_quality_flags or None,
            delta_data=self.delta_data_in_bucket,
            book_at_bucket_start=self.book_at_bucket_start,
            prev_mid=self.prev_mid_for_change,
        )
        if row.get("is_valid") != 1:
            return None
        return self._maybe_keep(row)

    def _cf_row(self, bucket_ms: int) -> dict[str, Any] | None:
        if self.last_valid_book is None or not self.last_valid_book.is_valid:
            return None
        row = build_carry_forward_row(
            self.last_valid_book,
            bucket_ms,
            exchange=self.exchange,
            market=self.market,
            symbol=self.symbol,
            depth=self.depth,
        )
        if row.get("is_valid") != 1:
            return None
        return row

    def _emit_cf_range(self, from_ms: int, to_ms_exclusive: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        t = from_ms
        while t < to_ms_exclusive:
            row = self._maybe_keep(self._cf_row(t))
            if row is not None:
                out.append(row)
            t += 1000
        return out

    def _maybe_keep(self, row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        bs = row["bucket_start"]
        bucket_ms = int(bs.timestamp() * 1000)
        if self.skip_before_ms is not None and bucket_ms < self.skip_before_ms:
            return None
        if bucket_ms in self.written_buckets or bucket_ms in self.in_flight_buckets:
            self.stats.duplicate_buckets_skipped += 1
            return None
        self.in_flight_buckets.add(bucket_ms)
        self.last_emitted_bucket_ms = bucket_ms
        self.stats.rows_emitted += 1
        if self.first_valid_live_bucket_ms is None:
            self.first_valid_live_bucket_ms = bucket_ms
        return row

    def note_enqueued(self, bucket_ms: int) -> None:
        self.in_flight_buckets.discard(bucket_ms)
        self.written_buckets.add(bucket_ms)

    def note_enqueue_failed(self, bucket_ms: int) -> None:
        self.in_flight_buckets.discard(bucket_ms)
