"""Stream-parse a Bybit ob200 .data file (NDJSON) and emit 1-second feature rows.

ROOT CAUSE NOTE (fixed in this version):
  Previous version: when bucket_ms jumped by >1 second (no raw events in between),
  the parser went directly from current_bucket_ms to the new bucket_ms, skipping all
  intermediate empty seconds. Those seconds had valid book state but were never emitted.

  Fix: after emitting the current bucket, iterate over every missing second in
  [current_bucket_ms + 1000, new_bucket_ms) and emit a carry-forward row for each,
  using the last valid book state and quality_flag='carried_forward'.

PARSER VERSION: ob200_v2  (carry-forward semantics added)

SEMANTICS:
- type=snapshot: full book replacement; resets sequence tracking.
- type=delta: incremental update; qty=0 removes a level.
- Bids sorted descending, asks ascending.
- data.u: monotonic update counter; gaps → book invalid until next snapshot.
- Feature row for second T: last valid book state within T.
- Empty seconds (no raw events): carry-forward of last known valid book,
  quality_flag='carried_forward', all event-based activity metrics = 0.
- No lookahead: row for T emitted only when ts ≥ (T+1)×1000 or at file end.
- Day boundary: [00:00:00, 23:59:59] per UTC day. A next-day midnight snapshot
  triggers the final emit of 23:59:59 but is itself placed in the next partition.

STATISTICS (DayParseStats) — all counts are mutually exclusive:
  expected_seconds        = 86400 for a complete UTC day
  event_seconds           = seconds that had ≥1 raw event emitted normally
  carried_forward_seconds = seconds with 0 raw events but valid carry-forward emitted
  invalid_seconds         = seconds emitted with is_valid=0 (seq gap, crossed book, etc.)
  missing_seconds         = expected_seconds - emitted_seconds  (should be 0 after fix)
  coverage_ratio          = emitted_seconds / expected_seconds
  valid_ratio             = (event_seconds + carried_forward_seconds) / expected_seconds
"""
from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from orderbook_analyse.orderbook_v2.book import (
    ZERO,
    BookState,
    apply_delta,
    apply_snapshot,
    sorted_asks,
    sorted_bids,
)
from orderbook_analyse.orderbook_v2.features import compute_features

# Coverage window per file: first second of the day (inclusive)
# and last second of the day (inclusive = day_start + 86399).
# A midnight snapshot at next_day 00:00:00 is NOT within this window.
_DAY_SECONDS = 86400


@dataclass
class DayParseStats:
    # Raw source
    total_lines: int = 0
    n_snapshots: int = 0
    n_deltas: int = 0
    n_seq_gaps: int = 0
    n_seq_dups: int = 0
    duplicate_source_lines: int = 0
    source_min_ts_ms: int = 0
    source_max_ts_ms: int = 0
    sha256: str = ""
    compressed_bytes: int = 0
    raw_record_count: int = 0

    # Coverage (all mutually exclusive)
    expected_seconds: int = _DAY_SECONDS
    event_seconds: int = 0          # seconds with ≥1 raw event (emitted normally)
    carried_forward_seconds: int = 0  # empty seconds, valid carry-forward emitted
    invalid_seconds: int = 0        # emitted with is_valid=0
    missing_seconds: int = 0        # not emitted at all (should be 0 after fix)

    # Derived
    n_crossed: int = 0
    quality_flags: list[str] = field(default_factory=list)

    @property
    def emitted_seconds(self) -> int:
        return self.event_seconds + self.carried_forward_seconds + self.invalid_seconds

    @property
    def coverage_ratio(self) -> float:
        if self.expected_seconds == 0:
            return 0.0
        return self.emitted_seconds / self.expected_seconds

    @property
    def valid_ratio(self) -> float:
        if self.expected_seconds == 0:
            return 0.0
        return (self.event_seconds + self.carried_forward_seconds) / self.expected_seconds


def _sha256_of_zip(zip_path: Path) -> str:
    h = hashlib.sha256()
    with zip_path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _compute_dynamics(
    delta_events: list[dict[str, Any]],
    prev_book: BookState,
) -> dict[str, Any]:
    """Compute delta-derived dynamics for a second from a list of delta data-dicts."""
    bid_added = ZERO
    bid_removed = ZERO
    ask_added = ZERO
    ask_removed = ZERO
    bid_add_n = 0
    bid_rem_n = 0
    ask_add_n = 0
    ask_rem_n = 0
    ofi = ZERO

    prev_best_bid = max(prev_book.bids.keys(), default=ZERO)
    prev_best_ask = min(prev_book.asks.keys(), default=ZERO)

    for d in delta_events:
        for item in d.get("b") or []:
            p = Decimal(item[0])
            q = Decimal(item[1])
            old_q = prev_book.bids.get(p, ZERO)
            if q == ZERO:
                if old_q > ZERO:
                    bid_removed += old_q
                    bid_rem_n += 1
                    if p == prev_best_bid:
                        ofi -= old_q
            else:
                delta_q = q - old_q
                if delta_q > ZERO:
                    bid_added += delta_q
                    bid_add_n += 1
                else:
                    bid_removed += abs(delta_q)
                    bid_rem_n += 1
                if p == prev_best_bid:
                    ofi += (q - old_q)

        for item in d.get("a") or []:
            p = Decimal(item[0])
            q = Decimal(item[1])
            old_q = prev_book.asks.get(p, ZERO)
            if q == ZERO:
                if old_q > ZERO:
                    ask_removed += old_q
                    ask_rem_n += 1
                    if p == prev_best_ask:
                        ofi += old_q
            else:
                delta_q = q - old_q
                if delta_q > ZERO:
                    ask_added += delta_q
                    ask_add_n += 1
                else:
                    ask_removed += abs(delta_q)
                    ask_rem_n += 1
                if p == prev_best_ask:
                    ofi -= (q - old_q)

    return {
        "bid_qty_added": bid_added,
        "bid_qty_removed": bid_removed,
        "ask_qty_added": ask_added,
        "ask_qty_removed": ask_removed,
        "bid_add_count": bid_add_n,
        "bid_remove_count": bid_rem_n,
        "ask_add_count": ask_add_n,
        "ask_remove_count": ask_rem_n,
        "ofi": ofi,
    }


def _zero_dynamics() -> dict[str, Any]:
    """Activity metrics for a carry-forward second: no events → all zero (not None)."""
    return {
        "bid_qty_added": ZERO,
        "bid_qty_removed": ZERO,
        "ask_qty_added": ZERO,
        "ask_qty_removed": ZERO,
        "bid_add_count": 0,
        "bid_remove_count": 0,
        "ask_add_count": 0,
        "ask_remove_count": 0,
        "ofi": ZERO,
    }


def parse_day_zip(
    zip_path: Path,
    *,
    exchange: str = "bybit",
    market: str = "linear",
    symbol: str,
    depth: int = 200,
    day_start_ms: int | None = None,  # override for testing; auto-derived from first event
) -> tuple[list[dict[str, Any]], DayParseStats]:
    """Parse one ob200 day ZIP, return (feature_rows, stats).

    Emits exactly one row per second in [day_start, day_start + 86399] when a
    valid book state is available (either from a raw event or carry-forward).
    Seconds without any valid state produce an invalid row.

    Coverage window is inferred from the first event's second: floor(ts_ms/1000)*1000.
    The window covers [day_start_ms, day_start_ms + 86399*1000] inclusive.
    """
    stats = DayParseStats()
    stats.sha256 = _sha256_of_zip(zip_path)
    stats.compressed_bytes = zip_path.stat().st_size

    book: BookState = BookState(bids={}, asks={}, last_u=0, last_seq=0, is_valid=False)
    has_initial_snapshot = False

    current_bucket_ms: int | None = None
    # Window end (inclusive): derived after first event
    window_end_ms: int | None = None

    # Last valid book state (used for carry-forward)
    last_valid_book: BookState | None = None
    last_valid_ts_ms: int = 0

    # Per-bucket accumulators
    first_ts_in_bucket: int = 0
    n_updates_in_bucket: int = 0
    bucket_quality_flags: list[str] = []
    delta_data_in_bucket: list[dict[str, Any]] = []
    book_at_bucket_start: BookState | None = None
    prev_mid_for_change: Decimal | None = None

    feature_rows: list[dict[str, Any]] = []
    seen_us: set[int] = set()

    def _mid_of(bk: BookState) -> Decimal | None:
        bds = sorted_bids(bk)
        aks = sorted_asks(bk)
        if bds and aks:
            return (bds[0][0] + aks[0][0]) / Decimal("2")
        return None

    def emit_event_bucket(bucket_ms: int, last_ts: int) -> None:
        """Emit a bucket that had ≥1 raw event."""
        nonlocal stats
        if last_valid_book is not None and last_valid_book.is_valid:
            dyn: dict[str, Any] = {}
            mid_change: Decimal | None = None

            if delta_data_in_bucket and book_at_bucket_start is not None:
                dyn = _compute_dynamics(delta_data_in_bucket, book_at_bucket_start)
                if prev_mid_for_change is not None:
                    cur_mid = _mid_of(last_valid_book)
                    if cur_mid is not None:
                        mid_change = cur_mid - prev_mid_for_change

            row = compute_features(
                last_valid_book, bucket_ms, first_ts_in_bucket, last_ts,
                n_updates_in_bucket,
                exchange=exchange, market=market, symbol=symbol, depth=depth,
                quality_flags=bucket_quality_flags if bucket_quality_flags else None,
                **dyn,
                mid_price_change=mid_change,
                imbalance_l10_change=None,
                imbalance_l50_change=None,
            )
            feature_rows.append(row)
            if row["is_valid"]:
                stats.event_seconds += 1
                if "crossed" in row["quality_flags"]:
                    stats.n_crossed += 1
            else:
                stats.invalid_seconds += 1
        else:
            row = compute_features(
                book if last_valid_book is None else last_valid_book,
                bucket_ms, first_ts_in_bucket or bucket_ms, last_ts or bucket_ms,
                n_updates_in_bucket,
                exchange=exchange, market=market, symbol=symbol, depth=depth,
                quality_flags=(bucket_quality_flags or []) + (
                    [] if has_initial_snapshot else ["no_start_snapshot"]
                ),
            )
            feature_rows.append(row)
            stats.invalid_seconds += 1

    def emit_carry_forward(bucket_ms: int, carry_book: BookState) -> None:
        """Emit a carry-forward row for an empty second.

        Uses the last valid book state. All event-based activity metrics are 0
        (not None) to distinguish "no activity" from "not computable".
        No information from future events is used.
        """
        nonlocal stats
        row = compute_features(
            carry_book, bucket_ms, bucket_ms, bucket_ms,
            processed_updates=0,
            exchange=exchange, market=market, symbol=symbol, depth=depth,
            quality_flags=["carried_forward"],
            **_zero_dynamics(),
            mid_price_change=None,   # no change: no events this second
            imbalance_l10_change=None,
            imbalance_l50_change=None,
        )
        feature_rows.append(row)
        # carried_forward counts as valid coverage (book is valid, just no new events)
        if row["is_valid"]:
            stats.carried_forward_seconds += 1
        else:
            stats.invalid_seconds += 1

    def fill_gap_and_emit(from_bucket_ms: int, to_bucket_ms: int) -> None:
        """Emit current bucket then carry-forward for every empty second up to to_bucket_ms.

        ROOT CAUSE FIX: this function replaces the old direct jump from
        current_bucket_ms to bucket_ms when bucket_ms > current_bucket_ms + 1000.
        """
        # 1. Emit the just-completed bucket (which had events)
        emit_event_bucket(from_bucket_ms, last_valid_ts_ms)

        # 2. For every completely empty second between from and to, emit carry-forward.
        #    Only emit if within the day window and if we have a valid book.
        carry_book = last_valid_book
        next_ms = from_bucket_ms + 1000
        while next_ms < to_bucket_ms:
            if window_end_ms is None or next_ms <= window_end_ms:
                if carry_book is not None and carry_book.is_valid:
                    emit_carry_forward(next_ms, carry_book)
                else:
                    # No valid book available: emit invalid placeholder
                    row = compute_features(
                        BookState(bids={}, asks={}, last_u=0, last_seq=0, is_valid=False),
                        next_ms, next_ms, next_ms, 0,
                        exchange=exchange, market=market, symbol=symbol, depth=depth,
                        quality_flags=["carried_forward", "no_valid_book"],
                    )
                    feature_rows.append(row)
                    stats.invalid_seconds += 1
            next_ms += 1000

    def reset_bucket(new_bucket_ms: int, new_ts_ms: int) -> None:
        nonlocal current_bucket_ms, first_ts_in_bucket, n_updates_in_bucket
        nonlocal bucket_quality_flags, delta_data_in_bucket, book_at_bucket_start
        nonlocal prev_mid_for_change, last_valid_book
        # Update prev_mid from carry-forward state (the last valid book before this bucket)
        if last_valid_book is not None and last_valid_book.is_valid:
            book_at_bucket_start = last_valid_book
            m = _mid_of(last_valid_book)
            prev_mid_for_change = m
        else:
            book_at_bucket_start = None
            prev_mid_for_change = None
        last_valid_book = None
        first_ts_in_bucket = new_ts_ms
        n_updates_in_bucket = 0
        bucket_quality_flags = []
        delta_data_in_bucket = []
        current_bucket_ms = new_bucket_ms

    def floor_second_ms(ts_ms: int) -> int:
        return (ts_ms // 1000) * 1000

    raw_record_count = 0

    with zipfile.ZipFile(zip_path) as zf:
        inner = zf.infolist()[0]
        with zf.open(inner) as fh:
            for raw_line in fh:
                raw_line = raw_line.rstrip(b"\n")
                if not raw_line:
                    continue
                stats.total_lines += 1
                raw_record_count += 1
                obj = json.loads(raw_line)
                msg_type = obj.get("type")
                ts_ms: int = obj.get("ts", 0)
                data = obj.get("data", {})

                if stats.source_min_ts_ms == 0 or ts_ms < stats.source_min_ts_ms:
                    stats.source_min_ts_ms = ts_ms
                if ts_ms > stats.source_max_ts_ms:
                    stats.source_max_ts_ms = ts_ms

                bucket_ms = floor_second_ms(ts_ms)

                # Initialise window from the very first event
                if current_bucket_ms is None:
                    if day_start_ms is not None:
                        _day_start = day_start_ms
                    else:
                        _day_start = bucket_ms
                    window_end_ms = _day_start + (_DAY_SECONDS - 1) * 1000
                    current_bucket_ms = bucket_ms
                    first_ts_in_bucket = ts_ms
                    book_at_bucket_start = book

                # ----- KEY FIX: handle bucket boundary with gap filling -----
                elif bucket_ms > current_bucket_ms:
                    # Emit current bucket + carry-forward for every skipped second
                    fill_gap_and_emit(current_bucket_ms, bucket_ms)
                    # Skip events beyond the day window (e.g. midnight snapshot of next day)
                    if window_end_ms is not None and bucket_ms > window_end_ms:
                        # Process the event (update book state) but don't start new tracking bucket
                        # We still need to apply the event to keep book state consistent.
                        pass
                    reset_bucket(bucket_ms, ts_ms)
                # ---- same bucket: just accumulate ----

                # Apply the event to book state
                if msg_type == "snapshot":
                    stats.n_snapshots += 1
                    book = apply_snapshot(data)
                    has_initial_snapshot = True
                    last_valid_book = book
                    last_valid_ts_ms = ts_ms
                    n_updates_in_bucket += 1

                elif msg_type == "delta":
                    stats.n_deltas += 1
                    new_u = data.get("u", 0)
                    if new_u in seen_us:
                        stats.n_seq_dups += 1
                        stats.duplicate_source_lines += 1
                        continue
                    seen_us.add(new_u)

                    book, warnings = apply_delta(book, data)
                    for w in warnings:
                        if w.startswith("seq_gap"):
                            stats.n_seq_gaps += 1
                            bucket_quality_flags.append(w)
                        elif w.startswith("seq_dup"):
                            stats.n_seq_dups += 1

                    if book.is_valid:
                        last_valid_book = book
                        last_valid_ts_ms = ts_ms
                        delta_data_in_bucket.append(data)
                    else:
                        bucket_quality_flags.append("invalid_after_gap")

                    n_updates_in_bucket += 1

    # Emit the final bucket (last bucket in file)
    if current_bucket_ms is not None:
        if window_end_ms is None or current_bucket_ms <= window_end_ms:
            emit_event_bucket(current_bucket_ms, last_valid_ts_ms)
            # Fill any remaining carry-forward up to the end of the day window
            if window_end_ms is not None:
                next_ms = current_bucket_ms + 1000
                carry_book = last_valid_book
                while next_ms <= window_end_ms:
                    if carry_book is not None and carry_book.is_valid:
                        emit_carry_forward(next_ms, carry_book)
                    else:
                        row = compute_features(
                            BookState(bids={}, asks={}, last_u=0, last_seq=0, is_valid=False),
                            next_ms, next_ms, next_ms, 0,
                            exchange=exchange, market=market, symbol=symbol, depth=depth,
                            quality_flags=["carried_forward", "no_valid_book"],
                        )
                        feature_rows.append(row)
                        stats.invalid_seconds += 1
                    next_ms += 1000

    # Compute missing_seconds = what was expected but not emitted
    stats.missing_seconds = max(0, stats.expected_seconds - stats.emitted_seconds)
    stats.raw_record_count = raw_record_count
    stats.quality_flags = list(set(stats.quality_flags))
    return feature_rows, stats
