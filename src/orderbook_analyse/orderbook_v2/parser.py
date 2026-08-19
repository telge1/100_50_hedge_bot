"""Stream-parse a Bybit ob200 .data file (NDJSON) and emit 1-second feature rows.

ROOT CAUSE NOTE (fixed in this version):
  Previous version: when bucket_ms jumped by >1 second (no raw events in between),
  the parser went directly from current_bucket_ms to the new bucket_ms, skipping all
  intermediate empty seconds. Those seconds had valid book state but were never emitted.

  Fix: after emitting the current bucket, iterate over every missing second in
  [current_bucket_ms + 1000, new_bucket_ms) and emit a carry-forward row for each,
  using the last valid book state and quality_flag='carried_forward'.

PARSER VERSION: ob200_v3  (causal UTC calendar window + previous-day warmup)

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
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

from orderbook_analyse.orderbook_v2 import PARSER_VERSION
from orderbook_analyse.orderbook_v2.book import (
    ZERO,
    BookState,
    apply_delta,
    apply_snapshot,
    sorted_asks,
    sorted_bids,
)
from orderbook_analyse.orderbook_v2.features import compute_features

# Calendar window of source_date D is always:
#   [D 00:00:00 UTC, D+1 00:00:00 UTC)
# last feature bucket = D 23:59:59. Never derived from first/last event.
_DAY_SECONDS = 86400
_DAY_MS = _DAY_SECONDS * 1000


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
    overflow_events: int = 0
    warmup_events: int = 0
    skipped_duplicate_events: int = 0
    final_book: BookState | None = None
    seen_us: set[int] = field(default_factory=set)

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


def calendar_day_start_ms(day: date | str) -> int:
    """UTC midnight of source_date as milliseconds."""
    if isinstance(day, str):
        day = date.fromisoformat(day)
    dt = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def validate_calendar_feature_rows(
    rows: Sequence[dict[str, Any]],
    *,
    day_start_ms: int,
    parser_version: str = PARSER_VERSION,
    expected_seconds: int | None = None,
) -> tuple[bool, str]:
    """Manifest COMPLETE guard: exact calendar coverage, no overflow rows."""
    n_exp = expected_seconds if expected_seconds is not None else _DAY_SECONDS
    emit_end = day_start_ms + n_exp * 1000
    last_bucket = emit_end - 1000
    if len(rows) != n_exp:
        return False, f"WINDOW_MISALIGNED emitted={len(rows)} expected={n_exp}"
    buckets = []
    for row in rows:
        bs = row["bucket_start"]
        if getattr(bs, "tzinfo", None) is None:
            ms = int(bs.replace(tzinfo=timezone.utc).timestamp() * 1000)
        else:
            ms = int(bs.timestamp() * 1000)
        buckets.append(ms)
        if ms < day_start_ms or ms >= emit_end:
            return False, f"WINDOW_MISALIGNED overflow_or_underflow bucket_ms={ms}"
        if row.get("parser_version") != parser_version:
            return False, f"WINDOW_MISALIGNED parser_version={row.get('parser_version')}"
    distinct = len(set(buckets))
    if distinct != n_exp:
        return False, f"WINDOW_MISALIGNED distinct={distinct}"
    if min(buckets) != day_start_ms:
        return False, f"WINDOW_MISALIGNED min={min(buckets)} expected={day_start_ms}"
    if max(buckets) != last_bucket:
        return False, f"WINDOW_MISALIGNED max={max(buckets)} expected={last_bucket}"
    event = cf = invalid = 0
    for row in rows:
        flags = row.get("quality_flags") or ""
        if row.get("is_valid") != 1:
            invalid += 1
        elif "carried_forward" in str(flags).split(","):
            cf += 1
        else:
            event += 1
    if event + cf + invalid != n_exp:
        return False, (
            f"WINDOW_MISALIGNED event+cf+invalid={event + cf + invalid}"
        )
    return True, "ok"


def iter_zip_ndjson(zip_path: Path):
    """Yield (type, ts_ms, data) from the first member of an ob200 ZIP."""
    with zipfile.ZipFile(zip_path) as zf:
        inner = zf.infolist()[0]
        with zf.open(inner) as fh:
            for raw_line in fh:
                raw_line = raw_line.rstrip(b"\n")
                if not raw_line:
                    continue
                obj = json.loads(raw_line)
                yield str(obj.get("type") or ""), int(obj.get("ts") or 0), obj.get("data") or {}


def zip_event_bounds(zip_path: Path) -> dict[str, Any]:
    first = last = None
    n = 0
    n_snap = n_delta = 0
    inner_name = ""
    with zipfile.ZipFile(zip_path) as zf:
        inner_name = zf.infolist()[0].filename
    for msg_type, ts_ms, data in iter_zip_ndjson(zip_path):
        n += 1
        if msg_type == "snapshot":
            n_snap += 1
        elif msg_type == "delta":
            n_delta += 1
        rec = {
            "type": msg_type, "ts_ms": ts_ms,
            "u": data.get("u"), "seq": data.get("seq"),
        }
        if first is None:
            first = rec
        last = rec
    return {
        "inner_name": inner_name,
        "n_events": n,
        "n_snapshots": n_snap,
        "n_deltas": n_delta,
        "first": first,
        "last": last,
        "size_bytes": zip_path.stat().st_size,
    }


def events_in_second(zip_path: Path, bucket_ms: int) -> list[dict[str, Any]]:
    end_ms = bucket_ms + 1000
    out: list[dict[str, Any]] = []
    for msg_type, ts_ms, data in iter_zip_ndjson(zip_path):
        if bucket_ms <= ts_ms < end_ms:
            out.append({
                "type": msg_type, "ts_ms": ts_ms,
                "u": data.get("u"), "seq": data.get("seq"),
            })
        elif ts_ms >= end_ms and out:
            break
    return out


def last_event_before(zip_path: Path, ts_exclusive_ms: int) -> dict[str, Any] | None:
    last = None
    for msg_type, ts_ms, data in iter_zip_ndjson(zip_path):
        if ts_ms < ts_exclusive_ms:
            last = {
                "type": msg_type, "ts_ms": ts_ms,
                "u": data.get("u"), "seq": data.get("seq"),
            }
        else:
            break
    return last


def first_unique_event(zip_path: Path, seen_us: set[int]) -> dict[str, Any] | None:
    for msg_type, ts_ms, data in iter_zip_ndjson(zip_path):
        u_val = data.get("u")
        if u_val in seen_us:
            continue
        return {
            "type": msg_type, "ts_ms": ts_ms,
            "u": u_val, "seq": data.get("seq"),
        }
    return None


def warmup_us(zip_path: Path) -> set[int]:
    seen: set[int] = set()
    for _t, _ts, data in iter_zip_ndjson(zip_path):
        u_val = data.get("u")
        if u_val is not None:
            seen.add(u_val)
    return seen


def validate_warmup_sequence(
    warmup_zip: Path,
    day_zip: Path,
    *,
    day_start_ms: int,
) -> tuple[bool, str]:
    """Causal D-1 → D handoff. Duplicate boundary snapshots are allowed."""
    before = last_event_before(warmup_zip, day_start_ms)
    if before is None:
        return False, "ETH_WARMUP_SEQUENCE_INVALID no_event_before_midnight"
    seen = warmup_us(warmup_zip)
    first_new = first_unique_event(day_zip, seen)
    if first_new is None:
        return True, "ok_all_day_events_already_in_warmup"
    if first_new["type"] == "snapshot":
        return True, "ok_day_opens_with_snapshot"
    prev = last_event_before(warmup_zip, int(first_new["ts_ms"]))
    prev_u = prev.get("u") if prev else None
    new_u = first_new.get("u")
    if isinstance(prev_u, int) and isinstance(new_u, int) and new_u == prev_u + 1:
        return True, "ok_delta_continues_warmup_u"
    return (
        False,
        f"ETH_WARMUP_SEQUENCE_INVALID first_new={first_new} prev_u={prev_u}",
    )


def parse_day_zip(
    zip_path: Path,
    *,
    exchange: str = "bybit",
    market: str = "linear",
    symbol: str,
    depth: int = 200,
    day_start_ms: int | None = None,
    warmup_zips: Sequence[Path] = (),
    initial_book: BookState | None = None,
    seen_us: set[int] | None = None,
    expected_seconds: int | None = None,
) -> tuple[list[dict[str, Any]], DayParseStats]:
    """Parse one ob200 calendar day, optionally after causal warmup ZIP(s).

    Feature window is [day_start_ms, day_start_ms + 86400s). It is never inferred
    from the first or last event. Warmup ZIPs are ingested first: events before
    day_start update BookState only; events inside the window emit features;
    events at/after end_exclusive are not emitted for this source_date.
    Duplicate u values (boundary snapshot in D-1 tail and D head) are skipped.
    """
    stats = DayParseStats()
    stats.sha256 = _sha256_of_zip(zip_path)
    stats.compressed_bytes = zip_path.stat().st_size
    if expected_seconds is not None:
        stats.expected_seconds = expected_seconds

    book: BookState = initial_book or BookState(
        bids={}, asks={}, last_u=0, last_seq=0, is_valid=False
    )
    has_initial_snapshot = bool(initial_book is not None and initial_book.is_valid)

    current_bucket_ms: int | None = None
    window_end_ms: int | None = None
    emit_start_ms: int | None = None
    emit_end_ms: int | None = None

    last_valid_book: BookState | None = (
        initial_book if initial_book is not None and initial_book.is_valid else None
    )
    last_valid_ts_ms: int = 0
    day_closed = False

    first_ts_in_bucket: int = 0
    n_updates_in_bucket: int = 0
    bucket_quality_flags: list[str] = []
    delta_data_in_bucket: list[dict[str, Any]] = []
    book_at_bucket_start: BookState | None = None
    prev_mid_for_change: Decimal | None = None

    feature_rows: list[dict[str, Any]] = []
    seen: set[int] = set(seen_us or ())

    def _mid_of(bk: BookState) -> Decimal | None:
        bds = sorted_bids(bk)
        aks = sorted_asks(bk)
        if bds and aks:
            return (bds[0][0] + aks[0][0]) / Decimal("2")
        return None

    def _ensure_calendar_window(first_bucket_ms: int) -> None:
        nonlocal emit_start_ms, emit_end_ms, window_end_ms
        if emit_start_ms is not None:
            return
        start = day_start_ms if day_start_ms is not None else first_bucket_ms
        emit_start_ms = start
        emit_end_ms = start + stats.expected_seconds * 1000
        window_end_ms = emit_end_ms - 1000

    def emit_invalid_empty(bucket_ms: int) -> None:
        nonlocal stats
        flags = ["no_valid_book"]
        if not has_initial_snapshot:
            flags.append("no_start_snapshot")
        row = compute_features(
            BookState(bids={}, asks={}, last_u=0, last_seq=0, is_valid=False),
            bucket_ms, bucket_ms, bucket_ms, 0,
            exchange=exchange, market=market, symbol=symbol, depth=depth,
            quality_flags=flags,
        )
        feature_rows.append(row)
        stats.invalid_seconds += 1

    def emit_event_bucket(bucket_ms: int, last_ts: int) -> None:
        nonlocal stats
        if n_updates_in_bucket == 0:
            if last_valid_book is not None and last_valid_book.is_valid:
                emit_carry_forward(bucket_ms, last_valid_book)
            else:
                emit_invalid_empty(bucket_ms)
            return
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
            extra = list(bucket_quality_flags or [])
            if not has_initial_snapshot:
                extra.append("no_start_snapshot")
            if "no_valid_book" not in extra:
                extra.append("no_valid_book")
            row = compute_features(
                book if last_valid_book is None else last_valid_book,
                bucket_ms, first_ts_in_bucket or bucket_ms, last_ts or bucket_ms,
                n_updates_in_bucket,
                exchange=exchange, market=market, symbol=symbol, depth=depth,
                quality_flags=extra,
            )
            feature_rows.append(row)
            stats.invalid_seconds += 1

    def emit_carry_forward(bucket_ms: int, carry_book: BookState) -> None:
        nonlocal stats
        row = compute_features(
            carry_book, bucket_ms, bucket_ms, bucket_ms,
            processed_updates=0,
            exchange=exchange, market=market, symbol=symbol, depth=depth,
            quality_flags=["carried_forward"],
            **_zero_dynamics(),
            mid_price_change=None,
            imbalance_l10_change=None,
            imbalance_l50_change=None,
        )
        feature_rows.append(row)
        if row["is_valid"]:
            stats.carried_forward_seconds += 1
        else:
            stats.invalid_seconds += 1

    def emit_empty_range(from_ms: int, to_ms_exclusive: int) -> None:
        next_ms = from_ms
        carry_book = last_valid_book
        while next_ms < to_ms_exclusive:
            if emit_end_ms is not None and next_ms >= emit_end_ms:
                break
            if window_end_ms is not None and next_ms > window_end_ms:
                break
            if carry_book is not None and carry_book.is_valid:
                emit_carry_forward(next_ms, carry_book)
            else:
                emit_invalid_empty(next_ms)
            next_ms += 1000

    def fill_gap_and_emit(from_bucket_ms: int, to_bucket_ms: int) -> None:
        emit_event_bucket(from_bucket_ms, last_valid_ts_ms)
        emit_empty_range(from_bucket_ms + 1000, to_bucket_ms)

    def reset_bucket(new_bucket_ms: int, new_ts_ms: int) -> None:
        nonlocal current_bucket_ms, first_ts_in_bucket, n_updates_in_bucket
        nonlocal bucket_quality_flags, delta_data_in_bucket, book_at_bucket_start
        nonlocal prev_mid_for_change, last_valid_book
        if last_valid_book is not None and last_valid_book.is_valid:
            book_at_bucket_start = last_valid_book
            prev_mid_for_change = _mid_of(last_valid_book)
        else:
            book_at_bucket_start = None
            prev_mid_for_change = None
        first_ts_in_bucket = new_ts_ms
        n_updates_in_bucket = 0
        bucket_quality_flags = []
        delta_data_in_bucket = []
        current_bucket_ms = new_bucket_ms

    def close_day() -> None:
        nonlocal current_bucket_ms, day_closed
        if day_closed:
            return
        if emit_start_ms is None or window_end_ms is None or emit_end_ms is None:
            return
        if current_bucket_ms is None:
            emit_empty_range(emit_start_ms, emit_end_ms)
        elif current_bucket_ms <= window_end_ms:
            emit_event_bucket(current_bucket_ms, last_valid_ts_ms)
            emit_empty_range(current_bucket_ms + 1000, emit_end_ms)
        current_bucket_ms = None
        day_closed = True

    def apply_book(msg_type: str, ts_ms: int, data: dict[str, Any], *, in_emit_bucket: bool) -> None:
        nonlocal book, has_initial_snapshot, last_valid_book, last_valid_ts_ms
        nonlocal n_updates_in_bucket
        u_val = data.get("u", 0)
        if u_val in seen:
            stats.n_seq_dups += 1
            stats.duplicate_source_lines += 1
            stats.skipped_duplicate_events += 1
            return
        if msg_type == "snapshot":
            stats.n_snapshots += 1
            seen.add(u_val)
            book = apply_snapshot(data)
            has_initial_snapshot = True
            last_valid_book = book
            last_valid_ts_ms = ts_ms
            if in_emit_bucket:
                n_updates_in_bucket += 1
            return
        if msg_type != "delta":
            return
        stats.n_deltas += 1
        seen.add(u_val)
        book, warnings = apply_delta(book, data)
        for w in warnings:
            if w.startswith("seq_gap"):
                stats.n_seq_gaps += 1
                if in_emit_bucket:
                    bucket_quality_flags.append(w)
            elif w.startswith("seq_dup"):
                stats.n_seq_dups += 1
        if book.is_valid:
            last_valid_book = book
            last_valid_ts_ms = ts_ms
            if in_emit_bucket:
                delta_data_in_bucket.append(data)
        elif in_emit_bucket:
            bucket_quality_flags.append("invalid_after_gap")
        if in_emit_bucket:
            n_updates_in_bucket += 1

    def ingest_event(msg_type: str, ts_ms: int, data: dict[str, Any], *, from_warmup: bool) -> None:
        nonlocal current_bucket_ms, first_ts_in_bucket, book_at_bucket_start
        bucket_ms = (ts_ms // 1000) * 1000
        _ensure_calendar_window(bucket_ms)
        assert emit_start_ms is not None and emit_end_ms is not None and window_end_ms is not None

        if bucket_ms < emit_start_ms:
            stats.warmup_events += 1
            apply_book(msg_type, ts_ms, data, in_emit_bucket=False)
            return

        if bucket_ms >= emit_end_ms:
            stats.overflow_events += 1
            close_day()
            apply_book(msg_type, ts_ms, data, in_emit_bucket=False)
            return

        if current_bucket_ms is None:
            if bucket_ms > emit_start_ms:
                emit_empty_range(emit_start_ms, bucket_ms)
            reset_bucket(bucket_ms, ts_ms)
        elif bucket_ms > current_bucket_ms:
            fill_gap_and_emit(current_bucket_ms, bucket_ms)
            if bucket_ms < emit_end_ms:
                reset_bucket(bucket_ms, ts_ms)

        apply_book(msg_type, ts_ms, data, in_emit_bucket=True)
        if from_warmup:
            stats.warmup_events += 1

    def ingest_zip(path: Path, *, from_warmup: bool) -> int:
        n = 0
        with zipfile.ZipFile(path) as zf:
            inner = zf.infolist()[0]
            with zf.open(inner) as fh:
                for raw_line in fh:
                    raw_line = raw_line.rstrip(b"\n")
                    if not raw_line:
                        continue
                    stats.total_lines += 1
                    n += 1
                    obj = json.loads(raw_line)
                    ts_ms = int(obj.get("ts") or 0)
                    if stats.source_min_ts_ms == 0 or ts_ms < stats.source_min_ts_ms:
                        stats.source_min_ts_ms = ts_ms
                    if ts_ms > stats.source_max_ts_ms:
                        stats.source_max_ts_ms = ts_ms
                    ingest_event(
                        str(obj.get("type") or ""),
                        ts_ms,
                        obj.get("data") or {},
                        from_warmup=from_warmup,
                    )
        return n

    if day_start_ms is not None:
        _ensure_calendar_window(day_start_ms)

    raw_record_count = 0
    for wz in warmup_zips:
        ingest_zip(Path(wz), from_warmup=True)
    stats.source_min_ts_ms = 0
    stats.source_max_ts_ms = 0
    raw_record_count = ingest_zip(zip_path, from_warmup=False)

    if emit_start_ms is None and day_start_ms is not None:
        _ensure_calendar_window(day_start_ms)
    close_day()

    stats.missing_seconds = max(0, stats.expected_seconds - stats.emitted_seconds)
    stats.raw_record_count = raw_record_count
    stats.quality_flags = list(set(stats.quality_flags))
    stats.final_book = book
    stats.seen_us = seen
    return feature_rows, stats

