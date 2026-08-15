"""Pure 1m → 5m aggregation for liquidation_data rows (DB-free)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from research.regime_scanner.derivatives.config import (
    BUCKET_SECONDS,
    EXPECTED_ROWS_PER_5M,
    SEQUENCE_GAP_SECONDS,
    SOURCE_DATABASE_DEFAULT,
    SOURCE_TABLE,
)
from research.regime_scanner.derivatives.hashing import bucket_source_hash
from research.regime_scanner.derivatives.normalize import (
    NormalizationError,
    decimal_to_float,
    normalize_source_row,
    normalize_symbol as normalize_symbol_strict,
)


def ensure_utc(ts: datetime) -> datetime:
    """Normalize to aware UTC. Naive values are treated as UTC (source audit)."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def floor_5m(ts: datetime) -> datetime:
    ts = ensure_utc(ts)
    # Minute-start event time: floor to 5m boundary.
    epoch = int(ts.timestamp())
    floored = epoch - (epoch % BUCKET_SECONDS)
    return datetime.fromtimestamp(floored, tz=timezone.utc)


def parse_utc(value: str | datetime) -> datetime:
    """CLI/user timestamps must be explicitly timezone-aware (no silent local)."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError(f"timestamp must be timezone-aware UTC: {value!r}")
        return value.astimezone(timezone.utc)
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        raise ValueError(f"timestamp must be timezone-aware UTC: {value!r}")
    return dt.astimezone(timezone.utc)


def normalize_symbol(symbol: str) -> str:
    try:
        return normalize_symbol_strict(symbol)
    except NormalizationError as exc:
        raise ValueError(str(exc)) from exc


@dataclass(frozen=True)
class SourceMinuteRow:
    timestamp: datetime
    symbol: str
    open_interest: float | None
    open_interest_value: float | None
    long_liq_usd: float | None
    short_liq_usd: float | None
    total_liq_usd: float | None
    buy_volume: float | None
    sell_volume: float | None
    spread: float | None


@dataclass
class RejectedRow:
    symbol: str
    timestamp: str | None
    reason: str
    detail: str = ""
    exception_type: str = ""
    affected_field: str = ""
    source_python_type: str = ""
    safe_example: str = ""
    category: str = "domain_reject"  # technical_normalization_error | domain_reject


@dataclass
class BucketRecord:
    symbol: str
    bucket_start: datetime
    bucket_end: datetime
    open_interest: float | None
    open_interest_usd: float | None
    long_liquidation_usd: float | None
    short_liquidation_usd: float | None
    total_liquidation_usd: float | None
    liquidation_event_count: int | None
    buy_volume: float | None
    sell_volume: float | None
    total_volume: float | None
    delta: float | None
    delta_ratio: float | None
    spread_mean: float | None
    spread_max: float | None
    source_first_timestamp: datetime
    source_last_timestamp: datetime
    source_row_count: int
    expected_source_rows: int
    coverage_ratio: float
    data_available: bool
    gap_before_seconds: int | None
    sequence_id: int
    source_database: str
    source_table: str
    import_version: str
    source_hash: str
    reject_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in (
            "bucket_start",
            "bucket_end",
            "source_first_timestamp",
            "source_last_timestamp",
        ):
            d[k] = self.__dict__[k].isoformat().replace("+00:00", "Z")
        return d


@dataclass
class AggregationResult:
    buckets: list[BucketRecord] = field(default_factory=list)
    rejects: list[RejectedRow] = field(default_factory=list)
    rows_read: int = 0
    rows_rejected: int = 0


def parse_source_row(row: Mapping[str, Any] | Any) -> SourceMinuteRow | RejectedRow:
    """Normalize any backend row into SourceMinuteRow; technical errors are classified."""
    from research.regime_scanner.derivatives.normalize import (
        TECHNICAL_REASONS,
        safe_example,
        type_name,
    )

    try:
        canon = normalize_source_row(row)
    except NormalizationError as exc:
        raw_ts = None
        raw_sym = ""
        raw_type = ""
        try:
            if isinstance(row, Mapping):
                raw_ts = row.get("timestamp")
                raw_sym = str(row.get("symbol", ""))
                raw_type = type_name(raw_ts)
            else:
                raw_type = type(row).__name__
        except Exception:  # noqa: BLE001
            pass
        reason = exc.reason
        category = (
            "technical_normalization_error"
            if reason in TECHNICAL_REASONS or reason.startswith("technical_")
            else "domain_reject"
        )
        # Map legacy-ish aliases for diagnostics
        if reason == "invalid_timestamp" and "naive" in str(exc).lower():
            reason = "technical_normalization_error"
            category = "technical_normalization_error"
        return RejectedRow(
            symbol=raw_sym,
            timestamp=str(raw_ts) if raw_ts is not None else None,
            reason=reason,
            detail=str(exc),
            exception_type=type(exc).__name__,
            affected_field=exc.field or "",
            source_python_type=raw_type,
            safe_example=safe_example(raw_ts) if raw_ts is not None else "",
            category=category,
        )
    except Exception as exc:  # noqa: BLE001 — programming / unexpected
        return RejectedRow(
            symbol="",
            timestamp=None,
            reason="technical_normalization_error",
            detail=f"unexpected normalize failure: {type(exc).__name__}: {exc}",
            exception_type=type(exc).__name__,
            affected_field="",
            source_python_type=type(row).__name__,
            safe_example="",
            category="technical_normalization_error",
        )

    ts = canon["timestamp"]
    symbol = canon["symbol"]

    # Event-time is minute start; non-zero seconds are domain rejects.
    if ts.second != 0:
        return RejectedRow(
            symbol=symbol,
            timestamp=ts.isoformat(),
            reason="domain_reject",
            detail="timestamp must be UTC minute-start (non-zero seconds)",
            exception_type="",
            affected_field="timestamp",
            source_python_type="datetime",
            safe_example=ts.isoformat(),
            category="domain_reject",
        )

    return SourceMinuteRow(
        timestamp=ts,
        symbol=symbol,
        open_interest=decimal_to_float(canon["open_interest"]),
        open_interest_value=decimal_to_float(canon["open_interest_value"]),
        long_liq_usd=decimal_to_float(canon["long_liq_usd"]),
        short_liq_usd=decimal_to_float(canon["short_liq_usd"]),
        total_liq_usd=decimal_to_float(canon["total_liq_usd"]),
        buy_volume=decimal_to_float(canon["buy_volume"]),
        sell_volume=decimal_to_float(canon["sell_volume"]),
        spread=decimal_to_float(canon["spread"]),
    )


def _sum_nullable(values: Sequence[float | None]) -> float | None:
    nums = [v for v in values if v is not None]
    if not nums:
        return None
    return float(sum(nums))


def _mean_nullable(values: Sequence[float | None]) -> float | None:
    nums = [v for v in values if v is not None]
    if not nums:
        return None
    return float(sum(nums) / len(nums))


def _max_nullable(values: Sequence[float | None]) -> float | None:
    nums = [v for v in values if v is not None]
    if not nums:
        return None
    return float(max(nums))


def aggregate_symbol_minutes(
    rows: Iterable[SourceMinuteRow],
    *,
    import_version: str,
    source_database: str = SOURCE_DATABASE_DEFAULT,
    source_table: str = SOURCE_TABLE,
    expected_rows: int = EXPECTED_ROWS_PER_5M,
    sequence_gap_seconds: int = SEQUENCE_GAP_SECONDS,
) -> AggregationResult:
    """Aggregate sorted minute rows for a single symbol into 5m buckets.

    Semantics (frozen):
    - bucket_start = floor(ts, 5m); bucket_end = bucket_start + 5m
    - include minutes where bucket_start <= ts < bucket_end
    - OI / OI USD = last snapshot by timestamp within bucket
    - liquidations / volumes = sums (None stays None if all None; sum of present otherwise)
    - delta = buy_volume - sell_volume when both present; else None
    - delta_ratio = delta / total_volume when total_volume > 0
    - incomplete buckets (row_count < expected): data_available=False, still emitted
    - missing buckets: not emitted (no forward-fill, no invented zeros)
    - gap_before_seconds measured from previous source minute; new sequence_id if gap >= threshold
    """
    result = AggregationResult()
    sorted_rows = sorted(rows, key=lambda r: (r.symbol, r.timestamp))
    result.rows_read = len(sorted_rows)
    if not sorted_rows:
        return result

    # Deduplicate identical (symbol, timestamp) keeping last occurrence.
    dedup: dict[tuple[str, datetime], SourceMinuteRow] = {}
    for r in sorted_rows:
        key = (r.symbol, r.timestamp)
        if key in dedup:
            result.rejects.append(
                RejectedRow(
                    symbol=r.symbol,
                    timestamp=r.timestamp.isoformat(),
                    reason="duplicate_minute",
                    detail="kept last row for timestamp",
                )
            )
            result.rows_rejected += 1
        dedup[key] = r
    minutes = sorted(dedup.values(), key=lambda r: r.timestamp)

    # Assign sequence ids on source minute stream.
    sequence_id = 1
    prev_ts: datetime | None = None
    minute_meta: list[tuple[SourceMinuteRow, int, int | None]] = []
    for m in minutes:
        gap_before: int | None = None
        if prev_ts is not None:
            gap = int((m.timestamp - prev_ts).total_seconds())
            # expected step is 60s; gap_before is excess beyond one minute? Spec: gap_before_seconds
            # between consecutive source minutes before this row.
            gap_before = gap
            if gap >= sequence_gap_seconds:
                sequence_id += 1
        minute_meta.append((m, sequence_id, gap_before))
        prev_ts = m.timestamp

    # Group by bucket
    from collections import defaultdict

    groups: dict[datetime, list[tuple[SourceMinuteRow, int, int | None]]] = defaultdict(list)
    for item in minute_meta:
        m = item[0]
        bs = floor_5m(m.timestamp)
        be = bs + timedelta(seconds=BUCKET_SECONDS)
        # Causality: minute at exactly bucket_end belongs to next bucket (half-open).
        if not (bs <= m.timestamp < be):
            result.rejects.append(
                RejectedRow(
                    symbol=m.symbol,
                    timestamp=m.timestamp.isoformat(),
                    reason="outside_bucket",
                    detail="internal bucketing error",
                )
            )
            result.rows_rejected += 1
            continue
        groups[bs].append(item)

    for bucket_start in sorted(groups.keys()):
        items = sorted(groups[bucket_start], key=lambda x: x[0].timestamp)
        bucket_end = bucket_start + timedelta(seconds=BUCKET_SECONDS)
        rows_m = [x[0] for x in items]
        seq_ids = {x[1] for x in items}
        # Use sequence at first minute of bucket; if mixed (shouldn't happen often), take max.
        sequence = max(x[1] for x in items)
        # gap_before for bucket = gap before first minute in bucket
        gap_before = items[0][2]

        source_row_count = len(rows_m)
        coverage = source_row_count / float(expected_rows)
        data_available = source_row_count == expected_rows and len(seq_ids) == 1

        # OI last snapshot
        last = rows_m[-1]
        oi = last.open_interest
        oi_usd = last.open_interest_value

        long_sum = _sum_nullable([r.long_liq_usd for r in rows_m])
        short_sum = _sum_nullable([r.short_liq_usd for r in rows_m])
        # Prefer recomputed total when both sides present; else source total sum
        if long_sum is not None and short_sum is not None:
            total_liq = long_sum + short_sum
        else:
            total_liq = _sum_nullable([r.total_liq_usd for r in rows_m])

        # liquidation_event_count: count minutes with any positive liq (not inventing for missing)
        event_count = 0
        for r in rows_m:
            vals = [r.long_liq_usd, r.short_liq_usd, r.total_liq_usd]
            if any(v is not None and v > 0 for v in vals):
                event_count += 1

        buy = _sum_nullable([r.buy_volume for r in rows_m])
        sell = _sum_nullable([r.sell_volume for r in rows_m])
        if buy is not None and sell is not None:
            total_vol = buy + sell
            delta = buy - sell
            delta_ratio = (delta / total_vol) if total_vol > 0 else None
        else:
            total_vol = None
            delta = None
            delta_ratio = None

        spread_mean = _mean_nullable([r.spread for r in rows_m])
        spread_max = _max_nullable([r.spread for r in rows_m])

        field_payload = {
            "open_interest": oi,
            "open_interest_usd": oi_usd,
            "long_liquidation_usd": long_sum,
            "short_liquidation_usd": short_sum,
            "total_liquidation_usd": total_liq,
            "buy_volume": buy,
            "sell_volume": sell,
            "spread_mean": spread_mean,
            "spread_max": spread_max,
            "source_row_count": source_row_count,
            "source_first": rows_m[0].timestamp.isoformat(),
            "source_last": rows_m[-1].timestamp.isoformat(),
        }
        sh = bucket_source_hash(
            symbol=rows_m[0].symbol,
            bucket_start_iso=bucket_start.isoformat().replace("+00:00", "Z"),
            import_version=import_version,
            field_payload=field_payload,
        )

        reject_reason = None
        if not data_available:
            reject_reason = "incomplete_bucket" if source_row_count < expected_rows else "sequence_mix"

        result.buckets.append(
            BucketRecord(
                symbol=rows_m[0].symbol,
                bucket_start=bucket_start,
                bucket_end=bucket_end,
                open_interest=oi,
                open_interest_usd=oi_usd,
                long_liquidation_usd=long_sum,
                short_liquidation_usd=short_sum,
                total_liquidation_usd=total_liq,
                liquidation_event_count=event_count,
                buy_volume=buy,
                sell_volume=sell,
                total_volume=total_vol,
                delta=delta,
                delta_ratio=delta_ratio,
                spread_mean=spread_mean,
                spread_max=spread_max,
                source_first_timestamp=rows_m[0].timestamp,
                source_last_timestamp=rows_m[-1].timestamp,
                source_row_count=source_row_count,
                expected_source_rows=expected_rows,
                coverage_ratio=coverage,
                data_available=data_available,
                gap_before_seconds=gap_before,
                sequence_id=sequence,
                source_database=source_database,
                source_table=source_table,
                import_version=import_version,
                source_hash=sh,
                reject_reason=reject_reason,
            )
        )

    return result


def aggregate_rows(
    raw_rows: Iterable[Mapping[str, Any]],
    *,
    import_version: str,
    source_database: str = SOURCE_DATABASE_DEFAULT,
    source_table: str = SOURCE_TABLE,
) -> AggregationResult:
    parsed: list[SourceMinuteRow] = []
    result = AggregationResult()
    for row in raw_rows:
        result.rows_read += 1
        out = parse_source_row(row)
        if isinstance(out, RejectedRow):
            result.rejects.append(out)
            result.rows_rejected += 1
            continue
        parsed.append(out)

    by_symbol: dict[str, list[SourceMinuteRow]] = {}
    for r in parsed:
        by_symbol.setdefault(r.symbol, []).append(r)

    for sym in sorted(by_symbol.keys()):
        part = aggregate_symbol_minutes(
            by_symbol[sym],
            import_version=import_version,
            source_database=source_database,
            source_table=source_table,
        )
        result.buckets.extend(part.buckets)
        result.rejects.extend(part.rejects)
        result.rows_rejected += part.rows_rejected
        # rows_read already counted at parse; don't double-count part.rows_read

    result.buckets.sort(key=lambda b: (b.symbol, b.bucket_start))
    return result
