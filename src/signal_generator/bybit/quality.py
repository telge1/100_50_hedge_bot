"""Data-quality audit for candles_1m backfills."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Sequence

from signal_generator.bybit.history import expected_candle_count, ensure_utc
from signal_generator.db.candles import Candle1m


@dataclass(slots=True)
class GapRecord:
    symbol: str
    previous_open_time: datetime
    next_open_time: datetime
    gap_seconds: int
    missing_candle_count: int
    gap_class: str = "INTERNAL_DATA_GAP"


@dataclass(slots=True)
class OhlcError:
    symbol: str
    open_time: datetime
    reason: str


@dataclass(slots=True)
class CrossCheckSample:
    symbol: str
    position: str
    open_time: datetime
    field: str
    bybit_value: str
    clickhouse_value: str
    match: bool


@dataclass(slots=True)
class SymbolQualityReport:
    symbol: str
    expected: int
    unique_final: int
    physical_rows: int
    min_open_time: datetime | None
    max_open_time: datetime | None
    duplicate_logical_keys: int
    gap_count: int
    largest_gap_seconds: int
    gaps: list[GapRecord] = field(default_factory=list)
    ohlc_errors: list[OhlcError] = field(default_factory=list)
    close_time_errors: int = 0
    crosscheck_pass: bool | None = None
    effective_start: datetime | None = None
    requested_start: datetime | None = None
    requested_end: datetime | None = None
    pre_listing_minutes: int = 0
    missing_candle_count: int = 0
    final_status: str | None = None
    ohlc_error_count: int | None = None

    def to_quality_row(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "final_status": self.final_status or "",
            "expected": self.expected,
            "unique_final": self.unique_final,
            "physical_rows": self.physical_rows,
            "requested_start": self.requested_start.isoformat() if self.requested_start else "",
            "effective_start": self.effective_start.isoformat() if self.effective_start else "",
            "min_open_time": self.min_open_time.isoformat() if self.min_open_time else "",
            "max_open_time": self.max_open_time.isoformat() if self.max_open_time else "",
            "pre_listing_minutes": self.pre_listing_minutes,
            "duplicate_logical_keys": self.duplicate_logical_keys,
            "gap_count": self.gap_count,
            "largest_gap_seconds": self.largest_gap_seconds,
            "missing_candle_count": self.missing_candle_count,
            "ohlc_error_count": (
                self.ohlc_error_count
                if self.ohlc_error_count is not None
                else len(self.ohlc_errors)
            ),
            "close_time_errors": self.close_time_errors,
            "crosscheck_pass": self.crosscheck_pass,
        }


def classify_coverage_window(
    *,
    requested_start: datetime,
    requested_end: datetime,
    effective_start: datetime | None,
    max_open_time: datetime | None,
) -> tuple[int, list[GapRecord]]:
    """Return pre-listing minutes and optional trailing gap after last observed candle.

    Pre-listing ``[requested_start, effective_start)`` is NOT an INTERNAL_DATA_GAP.
    Trailing missing minutes after last candle until ``requested_end`` ARE internal gaps
    (possible delisting / trading interruption) once listing has started.
    """
    requested_start = ensure_utc(requested_start)
    requested_end = ensure_utc(requested_end)
    pre_listing = 0
    trailing: list[GapRecord] = []
    if effective_start is None:
        return pre_listing, trailing
    effective_start = ensure_utc(effective_start)
    if effective_start > requested_start:
        pre_listing = int((effective_start - requested_start).total_seconds() // 60)
    if max_open_time is None:
        return pre_listing, trailing
    max_open_time = ensure_utc(max_open_time)
    # Last expected open_time in [start, end) is end - 1m
    last_expected = requested_end - timedelta(minutes=1)
    if max_open_time < last_expected:
        next_ot = max_open_time + timedelta(minutes=1)
        gap_seconds = int((requested_end - next_ot).total_seconds())
        missing = int((last_expected - max_open_time).total_seconds() // 60)
        if missing > 0:
            trailing.append(
                GapRecord(
                    symbol="",
                    previous_open_time=max_open_time,
                    next_open_time=requested_end,
                    gap_seconds=gap_seconds,
                    missing_candle_count=missing,
                    gap_class="INTERNAL_DATA_GAP",
                )
            )
    return pre_listing, trailing


def classify_final_status(
    *,
    unique_final: int,
    gap_count: int,
    ohlc_error_count: int,
    close_time_errors: int,
    checkpoint_status: str | None = None,
) -> str:
    if checkpoint_status == "FAILED":
        return "FAILED"
    if checkpoint_status == "PARTIAL":
        return "PARTIAL"
    if unique_final <= 0:
        return "NO_HISTORY"
    if gap_count == 0 and ohlc_error_count == 0 and close_time_errors == 0:
        return "COMPLETE_CLEAN"
    return "COMPLETE_WITH_INTERNAL_GAPS"


def aggregate_quality_counts(reports: Sequence[SymbolQualityReport]) -> dict[str, int]:
    counts = {
        "COMPLETE_CLEAN": 0,
        "COMPLETE_WITH_INTERNAL_GAPS": 0,
        "PARTIAL": 0,
        "FAILED": 0,
        "NO_HISTORY": 0,
    }
    for r in reports:
        key = r.final_status or "PARTIAL"
        counts[key] = counts.get(key, 0) + 1
    return counts



def _as_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _as_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        return ensure_utc(value)
    raise TypeError(f"Expected datetime, got {type(value)}")


def find_gaps(candles: Sequence[dict[str, Any] | Candle1m], *, symbol: str) -> list[GapRecord]:
    """Detect open_time gaps (expected step = 60s)."""
    times: list[datetime] = []
    for c in candles:
        if isinstance(c, Candle1m):
            times.append(ensure_utc(c.open_time))
        else:
            times.append(_as_utc(c["open_time"]))
    times = sorted(times)
    gaps: list[GapRecord] = []
    for prev, nxt in zip(times, times[1:]):
        delta = int((nxt - prev).total_seconds())
        if delta != 60:
            missing = max(delta // 60 - 1, 0)
            gaps.append(
                GapRecord(
                    symbol=symbol,
                    previous_open_time=prev,
                    next_open_time=nxt,
                    gap_seconds=delta,
                    missing_candle_count=missing,
                )
            )
    return gaps


def check_ohlc(candles: Sequence[dict[str, Any] | Candle1m], *, symbol: str) -> list[OhlcError]:
    errors: list[OhlcError] = []
    for c in candles:
        if isinstance(c, Candle1m):
            ot = ensure_utc(c.open_time)
            o, h, l, cl = map(_as_decimal, (c.open, c.high, c.low, c.close))
            vol, turn = map(_as_decimal, (c.volume, c.turnover))
        else:
            ot = _as_utc(c["open_time"])
            o = _as_decimal(c["open"])
            h = _as_decimal(c["high"])
            l = _as_decimal(c["low"])
            cl = _as_decimal(c["close"])
            vol = _as_decimal(c["volume"])
            turn = _as_decimal(c["turnover"])
        if h < max(o, cl):
            errors.append(OhlcError(symbol, ot, "high < max(open, close)"))
        if l > min(o, cl):
            errors.append(OhlcError(symbol, ot, "low > min(open, close)"))
        if h < l:
            errors.append(OhlcError(symbol, ot, "high < low"))
        if vol < 0:
            errors.append(OhlcError(symbol, ot, "volume < 0"))
        if turn < 0:
            errors.append(OhlcError(symbol, ot, "turnover < 0"))
    return errors


def check_close_times(candles: Sequence[dict[str, Any] | Candle1m]) -> int:
    bad = 0
    for c in candles:
        if isinstance(c, Candle1m):
            ot, ct = ensure_utc(c.open_time), ensure_utc(c.close_time)
        else:
            ot, ct = _as_utc(c["open_time"]), _as_utc(c["close_time"])
        if int((ct - ot).total_seconds()) != 60:
            bad += 1
    return bad


def count_logical_duplicates(candles: Sequence[dict[str, Any] | Candle1m]) -> int:
    keys: dict[tuple[Any, ...], int] = {}
    for c in candles:
        if isinstance(c, Candle1m):
            key = (c.exchange, c.symbol, c.interval, ensure_utc(c.open_time))
        else:
            key = (
                c.get("exchange"),
                c.get("symbol"),
                c.get("interval"),
                _as_utc(c["open_time"]),
            )
        keys[key] = keys.get(key, 0) + 1
    return sum(1 for n in keys.values() if n > 1)


def pick_sample_indices(n: int) -> dict[str, int]:
    if n <= 0:
        return {}
    if n == 1:
        return {"first": 0}
    if n == 2:
        return {"first": 0, "last": 1}
    return {"first": 0, "middle": n // 2, "last": n - 1}


def compare_candle_fields(
    *,
    symbol: str,
    position: str,
    bybit: Candle1m,
    stored: dict[str, Any],
) -> list[CrossCheckSample]:
    samples: list[CrossCheckSample] = []
    pairs = [
        ("open_time", ensure_utc(bybit.open_time).isoformat(), _as_utc(stored["open_time"]).isoformat()),
        ("open", str(bybit.open), str(_as_decimal(stored["open"]))),
        ("high", str(bybit.high), str(_as_decimal(stored["high"]))),
        ("low", str(bybit.low), str(_as_decimal(stored["low"]))),
        ("close", str(bybit.close), str(_as_decimal(stored["close"]))),
        ("volume", str(bybit.volume), str(_as_decimal(stored["volume"]))),
        ("turnover", str(bybit.turnover), str(_as_decimal(stored["turnover"]))),
    ]
    for field_name, bv, sv in pairs:
        # Decimal string compare via Decimal to ignore trailing zeros.
        if field_name == "open_time":
            match = bv == sv
        else:
            match = _as_decimal(bv) == _as_decimal(sv)
        samples.append(
            CrossCheckSample(
                symbol=symbol,
                position=position,
                open_time=ensure_utc(bybit.open_time),
                field=field_name,
                bybit_value=bv,
                clickhouse_value=sv,
                match=match,
            )
        )
    return samples


def audit_symbol_candles(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    final_rows: Sequence[dict[str, Any]],
    physical_rows: int,
    bybit_samples: Sequence[Candle1m] | None = None,
    effective_start: datetime | None = None,
    checkpoint_status: str | None = None,
) -> tuple[SymbolQualityReport, list[CrossCheckSample]]:
    """Audit FINAL candle rows.

    ``start``/``end`` are the requested window. Gaps are evaluated only on observed
    candles (and optional trailing gap to ``end`` after listing). Pre-listing time
    before ``effective_start`` is reported separately and is not an internal gap.
    """
    start = ensure_utc(start)
    end = ensure_utc(end)
    gaps = find_gaps(final_rows, symbol=symbol)
    for g in gaps:
        g.gap_class = "INTERNAL_DATA_GAP"
    ohlc_errors = check_ohlc(final_rows, symbol=symbol)
    close_errs = check_close_times(final_rows)
    min_ot = _as_utc(final_rows[0]["open_time"]) if final_rows else None
    max_ot = _as_utc(final_rows[-1]["open_time"]) if final_rows else None
    eff = ensure_utc(effective_start) if effective_start is not None else min_ot

    pre_listing, trailing = classify_coverage_window(
        requested_start=start,
        requested_end=end,
        effective_start=eff,
        max_open_time=max_ot,
    )
    for g in trailing:
        g.symbol = symbol
        gaps.append(g)

    coverage_start = eff or start
    expected = expected_candle_count(coverage_start, end) if final_rows or eff else 0

    cross: list[CrossCheckSample] = []
    cross_pass: bool | None = None
    if bybit_samples is not None and final_rows:
        by_ot = {_as_utc(r["open_time"]): r for r in final_rows}
        cross_pass = True
        for sample in bybit_samples:
            stored = by_ot.get(ensure_utc(sample.open_time))
            if stored is None:
                cross_pass = False
                cross.append(
                    CrossCheckSample(
                        symbol=symbol,
                        position="missing",
                        open_time=ensure_utc(sample.open_time),
                        field="open_time",
                        bybit_value=ensure_utc(sample.open_time).isoformat(),
                        clickhouse_value="",
                        match=False,
                    )
                )
                continue
            samples = compare_candle_fields(
                symbol=symbol,
                position="sample",
                bybit=sample,
                stored=stored,
            )
            if not all(s.match for s in samples):
                cross_pass = False
            cross.extend(samples)

    missing_total = sum(g.missing_candle_count for g in gaps)
    final_status = classify_final_status(
        unique_final=len(final_rows),
        gap_count=len(gaps),
        ohlc_error_count=len(ohlc_errors),
        close_time_errors=close_errs,
        checkpoint_status=checkpoint_status,
    )

    report = SymbolQualityReport(
        symbol=symbol,
        expected=expected,
        unique_final=len(final_rows),
        physical_rows=physical_rows,
        min_open_time=min_ot,
        max_open_time=max_ot,
        duplicate_logical_keys=0,  # FINAL rows are unique by definition
        gap_count=len(gaps),
        largest_gap_seconds=max((g.gap_seconds for g in gaps), default=0),
        gaps=gaps[:50],
        ohlc_errors=ohlc_errors[:50],
        close_time_errors=close_errs,
        crosscheck_pass=cross_pass,
        effective_start=eff,
        requested_start=start,
        requested_end=end,
        pre_listing_minutes=pre_listing,
        missing_candle_count=missing_total,
        final_status=final_status,
        ohlc_error_count=len(ohlc_errors),
    )
    return report, cross
