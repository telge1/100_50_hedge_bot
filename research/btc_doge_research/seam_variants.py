"""Read-only reconstruction variants for the Phase-1B seam audit."""

from __future__ import annotations

import bisect
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any, Callable

from .config import PilotWindow
from .contracts import parse_utc
from .ob200_parser import FullBookEvent, OB200SegmentReader
from .pilot_runner import discover_sources


@dataclass(frozen=True)
class EventState:
    event_time: datetime
    receive_time: datetime
    update_id: int
    exchange_sequence: int
    raw_event_type: str
    mid: Decimal
    best_bid: Decimal
    best_ask: Decimal
    spread: Decimal
    bid_qty_l50: Decimal
    ask_qty_l50: Decimal
    imbalance_l50: Decimal
    source_file: str
    source_record: int

    @classmethod
    def from_book(cls, event: FullBookEvent, source_file: str) -> "EventState":
        if event.receive_time is None:
            raise ValueError("receive time required for Phase-1B")
        bid_qty = sum((qty for _, qty in event.bids[:50]), Decimal("0"))
        ask_qty = sum((qty for _, qty in event.asks[:50]), Decimal("0"))
        total = bid_qty + ask_qty
        bid, ask = event.bids[0][0], event.asks[0][0]
        return cls(
            event_time=event.event_time,
            receive_time=event.receive_time,
            update_id=event.update_id,
            exchange_sequence=event.exchange_sequence,
            raw_event_type=event.raw_event_type,
            mid=(bid + ask) / Decimal("2"),
            best_bid=bid,
            best_ask=ask,
            spread=ask - bid,
            bid_qty_l50=bid_qty,
            ask_qty_l50=ask_qty,
            imbalance_l50=(
                (bid_qty - ask_qty) / total if total else Decimal("0")
            ),
            source_file=source_file,
            source_record=event.source_record,
        )


@dataclass(frozen=True)
class CHBucket:
    bucket_time: datetime
    first_source_ts: datetime
    last_source_ts: datetime
    last_update_id: int
    processed_updates: int
    parser_version: str
    created_at: datetime
    quality_flags: str
    mid: Decimal
    best_bid: Decimal
    best_ask: Decimal
    spread: Decimal
    bid_qty_l50: Decimal
    ask_qty_l50: Decimal
    imbalance_l50: Decimal


VARIANTS = (
    "FIRST_EVENT_IN_SECOND",
    "LAST_EVENT_IN_SECOND",
    "LAST_EVENT_AT_OR_BEFORE_SECOND_START",
    "LAST_EVENT_AT_OR_BEFORE_SECOND_END",
    "NEAREST_EVENT_TO_SECOND_START",
    "NEAREST_EVENT_TO_SECOND_END",
    "EVENT_TIME_LAST",
    "RECEIVE_TIME_LAST",
    "CURRENT_PHASE1_IMPLEMENTATION",
)


def aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return float(ordered[index])


def describe(values: list[float], prefix: str) -> dict[str, Any]:
    return {
        f"{prefix}_count": len(values),
        f"{prefix}_mean": statistics.fmean(values) if values else None,
        f"{prefix}_p50": percentile(values, 0.50),
        f"{prefix}_p90": percentile(values, 0.90),
        f"{prefix}_p95": percentile(values, 0.95),
        f"{prefix}_p99": percentile(values, 0.99),
        f"{prefix}_max": max(values) if values else None,
    }


def load_event_states(
    symbol: str, start: datetime, end: datetime
) -> tuple[list[EventState], list[str]]:
    # Include the preceding received event at hour boundaries. The live clock
    # carries that state into the next bucket.
    warm_start = start - timedelta(seconds=2)
    read_end = end + timedelta(seconds=2)
    discovery = PilotWindow(
        pilot_id="phase1b_discovery",
        symbol=symbol,
        start=warm_start,
        end=read_end,
        reference="phase1b",
    )
    states: list[EventState] = []
    files: list[str] = []
    for source in discover_sources(discovery):
        files.append(source.relative_path)
        reader = OB200SegmentReader(source, symbol)
        for event in reader.iter_full_books(warm_start, read_end):
            states.append(EventState.from_book(event, source.relative_path))
        if reader.audit.u_gaps or not reader.audit.full_file_consumed:
            raise RuntimeError(f"invalid raw segment: {source.relative_path}")
    states.sort(key=lambda event: (event.receive_time, event.source_file, event.source_record))
    return states, files


def load_ch_buckets(
    client: Any, symbol: str, start: datetime, end: datetime
) -> list[CHBucket]:
    result = client.query(
        """
        SELECT bucket_start,first_source_ts,last_source_ts,last_update_seq,
               processed_updates,parser_version,created_at,quality_flags,
               mid_price,best_bid_price,best_ask_price,spread_abs,
               bid_qty_l50,ask_qty_l50,imbalance_l50
        FROM orderbook_analysis.orderbook_features_1s_v2 FINAL
        WHERE symbol={symbol:String} AND depth=200 AND parser_version='ob200_v3'
          AND bucket_start >= {start:DateTime64(3,'UTC')}
          AND bucket_start < {end:DateTime64(3,'UTC')}
        ORDER BY bucket_start
        """,
        {"symbol": symbol, "start": start, "end": end},
    ).result_rows
    return [
        CHBucket(
            bucket_time=aware(row[0]),
            first_source_ts=aware(row[1]),
            last_source_ts=aware(row[2]),
            last_update_id=int(row[3]),
            processed_updates=int(row[4]),
            parser_version=str(row[5]),
            created_at=aware(row[6]),
            quality_flags=str(row[7]),
            mid=Decimal(str(row[8])),
            best_bid=Decimal(str(row[9])),
            best_ask=Decimal(str(row[10])),
            spread=Decimal(str(row[11])),
            bid_qty_l50=Decimal(str(row[12])),
            ask_qty_l50=Decimal(str(row[13])),
            imbalance_l50=Decimal(str(row[14])),
        )
        for row in result
    ]


def _last_before(
    events: list[EventState],
    values: list[datetime],
    boundary: datetime,
    *,
    inclusive: bool,
) -> EventState | None:
    index = (
        bisect.bisect_right(values, boundary)
        if inclusive
        else bisect.bisect_left(values, boundary)
    )
    return events[index - 1] if index else None


def selected_variants(
    events: list[EventState], bucket: datetime
) -> dict[str, tuple[EventState | None, bool]]:
    bucket_end = bucket + timedelta(seconds=1)
    by_event = sorted(events, key=lambda event: (event.event_time, event.update_id))
    event_times = [event.event_time for event in by_event]
    receive_times = [event.receive_time for event in events]
    in_event_second = [
        event for event in by_event if bucket <= event.event_time < bucket_end
    ]
    in_receive_second = [
        event for event in events if bucket <= event.receive_time < bucket_end
    ]

    def nearest(
        candidates: list[EventState],
        key: Callable[[EventState], datetime],
        boundary: datetime,
    ) -> EventState | None:
        return min(
            candidates,
            key=lambda event: (
                abs((key(event) - boundary).total_seconds()),
                key(event),
                event.update_id,
            ),
            default=None,
        )

    event_asof_end = _last_before(
        by_event, event_times, bucket_end, inclusive=False
    )
    receive_asof_end = _last_before(
        events, receive_times, bucket_end, inclusive=False
    )
    return {
        "FIRST_EVENT_IN_SECOND": (
            in_event_second[0] if in_event_second else None,
            bool(in_event_second),
        ),
        "LAST_EVENT_IN_SECOND": (
            in_event_second[-1] if in_event_second else None,
            bool(in_event_second),
        ),
        "LAST_EVENT_AT_OR_BEFORE_SECOND_START": (
            _last_before(by_event, event_times, bucket, inclusive=True),
            False,
        ),
        "LAST_EVENT_AT_OR_BEFORE_SECOND_END": (
            event_asof_end,
            bool(in_event_second),
        ),
        "NEAREST_EVENT_TO_SECOND_START": (
            nearest(by_event, lambda event: event.event_time, bucket),
            False,
        ),
        "NEAREST_EVENT_TO_SECOND_END": (
            nearest(by_event, lambda event: event.event_time, bucket_end),
            False,
        ),
        "EVENT_TIME_LAST": (event_asof_end, bool(in_event_second)),
        "RECEIVE_TIME_LAST": (
            receive_asof_end,
            bool(in_receive_second),
        ),
        "CURRENT_PHASE1_IMPLEMENTATION": (
            event_asof_end,
            bool(in_event_second),
        ),
    }


def signature(value: EventState | CHBucket) -> tuple[Decimal, ...]:
    stored = Decimal("0.00000001")
    return (
        value.mid.quantize(stored, rounding=ROUND_DOWN),
        value.best_bid.quantize(stored, rounding=ROUND_DOWN),
        value.best_ask.quantize(stored, rounding=ROUND_DOWN),
        value.spread.quantize(stored, rounding=ROUND_DOWN),
        value.bid_qty_l50.quantize(stored, rounding=ROUND_DOWN),
        value.ask_qty_l50.quantize(stored, rounding=ROUND_DOWN),
        value.imbalance_l50.quantize(stored, rounding=ROUND_DOWN),
    )


def compare_window(
    client: Any, symbol: str, start_raw: str, end_raw: str
) -> dict[str, Any]:
    start, end = parse_utc(start_raw), parse_utc(end_raw)
    events, files = load_event_states(symbol, start, end)
    buckets = load_ch_buckets(client, symbol, start, end)
    tick = Decimal("0.1") if symbol == "BTCUSDT" else Decimal("0.00001")
    accumulators: dict[str, dict[str, Any]] = {
        variant: {
            "paired": 0,
            "missing": 0,
            "exact": 0,
            "tolerance": 0,
            "mid": [],
            "mid_bps": [],
            "bid": [],
            "ask": [],
            "spread": [],
            "bid_qty": [],
            "ask_qty": [],
            "imbalance": [],
            "event_age_start_ms": [],
            "event_age_end_ms": [],
            "receive_age_end_ms": [],
            "genuine": 0,
            "carried": 0,
            "quality_match": 0,
        }
        for variant in VARIANTS
    }
    details: list[dict[str, Any]] = []
    for ch in buckets:
        variants = selected_variants(events, ch.bucket_time)
        detail = {"ch": ch, "variants": variants, "events": events}
        details.append(detail)
        for variant, (selected, genuine) in variants.items():
            acc = accumulators[variant]
            if selected is None:
                acc["missing"] += 1
                continue
            acc["paired"] += 1
            selected_sig = signature(selected)
            ch_sig = signature(ch)
            exact = selected_sig == ch_sig
            mid_error = abs(selected.mid - ch.mid)
            bid_error = abs(selected.best_bid - ch.best_bid)
            ask_error = abs(selected.best_ask - ch.best_ask)
            spread_error = abs(selected.spread - ch.spread)
            imbalance_error = abs(selected.imbalance_l50 - ch.imbalance_l50)
            tolerance = (
                mid_error <= tick / 2
                and bid_error <= tick
                and ask_error <= tick
                and spread_error <= tick
                and abs(selected.bid_qty_l50 - ch.bid_qty_l50) <= Decimal("0.00000001")
                and abs(selected.ask_qty_l50 - ch.ask_qty_l50) <= Decimal("0.00000001")
                and imbalance_error <= Decimal("0.00000001")
            )
            acc["exact"] += int(exact)
            acc["tolerance"] += int(tolerance)
            acc["mid"].append(float(mid_error))
            acc["mid_bps"].append(float(mid_error / ch.mid * Decimal("10000")))
            acc["bid"].append(float(bid_error))
            acc["ask"].append(float(ask_error))
            acc["spread"].append(float(spread_error))
            acc["bid_qty"].append(float(abs(selected.bid_qty_l50 - ch.bid_qty_l50)))
            acc["ask_qty"].append(float(abs(selected.ask_qty_l50 - ch.ask_qty_l50)))
            acc["imbalance"].append(float(imbalance_error))
            acc["event_age_start_ms"].append(
                (selected.event_time - ch.bucket_time).total_seconds() * 1000
            )
            acc["event_age_end_ms"].append(
                (ch.bucket_time + timedelta(seconds=1) - selected.event_time).total_seconds()
                * 1000
            )
            acc["receive_age_end_ms"].append(
                (ch.bucket_time + timedelta(seconds=1) - selected.receive_time).total_seconds()
                * 1000
            )
            acc["genuine"] += int(genuine)
            acc["carried"] += int(not genuine)
            ch_genuine = ch.quality_flags != "carried_forward"
            acc["quality_match"] += int(genuine == ch_genuine)

    variant_rows: list[dict[str, Any]] = []
    for variant, acc in accumulators.items():
        paired = acc["paired"]
        row = {
            "symbol": symbol,
            "window_start": start_raw,
            "window_end": end_raw,
            "variant": variant,
            "ch_seconds": len(buckets),
            "paired_seconds": paired,
            "missing_raw_seconds": acc["missing"],
            "missing_ch_seconds": int((end - start).total_seconds()) - len(buckets),
            "exact_matches": acc["exact"],
            "tolerance_matches": acc["tolerance"],
            "mismatch_rate_pct": (
                100 * (paired - acc["tolerance"]) / paired if paired else None
            ),
            "genuine_count": acc["genuine"],
            "carried_forward_count": acc["carried"],
            "genuine_cf_matches": acc["quality_match"],
            **describe(acc["mid"], "mid_abs_error"),
            **describe(acc["mid_bps"], "mid_error_bps"),
            **describe(acc["bid"], "best_bid_abs_error"),
            **describe(acc["ask"], "best_ask_abs_error"),
            **describe(acc["spread"], "spread_abs_error"),
            **describe(acc["bid_qty"], "bid_qty_l50_abs_error"),
            **describe(acc["ask_qty"], "ask_qty_l50_abs_error"),
            **describe(acc["imbalance"], "imbalance_l50_abs_error"),
            **describe(acc["event_age_start_ms"], "event_age_from_bucket_start_ms"),
            **describe(acc["event_age_end_ms"], "event_age_to_bucket_end_ms"),
            **describe(acc["receive_age_end_ms"], "receive_age_to_bucket_end_ms"),
        }
        variant_rows.append(row)
    return {
        "symbol": symbol,
        "start": start,
        "end": end,
        "source_files": files,
        "events": events,
        "ch_buckets": buckets,
        "details": details,
        "variant_rows": variant_rows,
    }
