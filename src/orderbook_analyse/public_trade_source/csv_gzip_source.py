"""CSV.GZ historical Bybit public-trade source."""

from __future__ import annotations

import csv
import gzip
import re
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from orderbook_analyse.public_trade_source.csv_parse import (
    PublicTradeParseError,
    parse_csv_trade_row,
)
from orderbook_analyse.public_trade_source.protocol import (
    NormalizedPublicTrade,
    TradeCoverageReport,
)

FILENAME_RE = re.compile(
    r"^(?P<symbol>[A-Za-z0-9]+)(?P<date>\d{4}-\d{2}-\d{2})\.csv\.gz$"
)


class PublicTradeFileSourceError(RuntimeError):
    """Hard failure reading public-trade files."""


def _ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _day_range(start: datetime, end: datetime) -> list[date]:
    """Calendar days that overlap half-open UTC window [start, end)."""
    start = _ensure_utc(start)
    end = _ensure_utc(end)
    if end < start:
        raise PublicTradeFileSourceError("end before start")
    if end == start:
        return []
    days: list[date] = []
    d = start.date()
    while True:
        day_start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        if day_start >= end:
            break
        days.append(d)
        d += timedelta(days=1)
    return days


class CsvGzipPublicTradeSource:
    """Stream Bybit public-trade ``.csv.gz`` day files."""

    source_name = "files"

    def __init__(
        self,
        files_root: Path | str,
        *,
        file_pattern: str = "*.csv.gz",
        strict: bool = True,
        allow_partial_coverage: bool = False,
        dedupe: bool = True,
    ) -> None:
        self.files_root = Path(files_root)
        self.file_pattern = file_pattern
        self.strict = strict
        self.allow_partial_coverage = allow_partial_coverage
        self.dedupe = dedupe
        if not self.files_root.exists():
            raise PublicTradeFileSourceError(f"files_root does not exist: {self.files_root}")

    def _index_files(self, symbol: str) -> dict[date, Path]:
        symbol = symbol.upper()
        by_day: dict[date, Path] = {}
        for path in sorted(self.files_root.glob(self.file_pattern)):
            if not path.is_file():
                continue
            m = FILENAME_RE.match(path.name)
            if m is None:
                continue
            if m.group("symbol").upper() != symbol:
                continue
            day = date.fromisoformat(m.group("date"))
            by_day[day] = path
        return by_day

    def _plan_files(
        self, symbol: str, start: datetime, end: datetime
    ) -> tuple[list[Path], list[str], list[str], list[str]]:
        """Return (found_paths, expected_names, found_names, missing_dates)."""
        symbol = symbol.upper()
        index = self._index_files(symbol)
        days = _day_range(start, end)
        expected = [f"{symbol}{d.isoformat()}.csv.gz" for d in days]
        found_paths: list[Path] = []
        found_names: list[str] = []
        missing: list[str] = []
        for d, name in zip(days, expected, strict=True):
            if d in index:
                found_paths.append(index[d])
                found_names.append(name)
            else:
                missing.append(d.isoformat())
        return found_paths, expected, found_names, missing

    def coverage(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> TradeCoverageReport:
        start = _ensure_utc(start)
        end = _ensure_utc(end)
        symbol = symbol.upper()
        report = TradeCoverageReport(
            symbol=symbol,
            requested_start=start,
            requested_end=end,
            source=self.source_name,
        )
        try:
            paths, expected, found, missing = self._plan_files(symbol, start, end)
        except PublicTradeFileSourceError as exc:
            report.valid = False
            report.reason = str(exc)
            return report

        report.files_expected = expected
        report.files_found = found
        report.missing_dates = missing
        report.partial = bool(missing)

        if missing and not self.allow_partial_coverage:
            report.valid = False
            report.reason = f"missing_dates:{','.join(missing)}"
            return report
        if not paths:
            report.valid = False
            report.reason = "no_files_in_window"
            return report

        try:
            first_ts = last_ts = None
            for trade in self._iter_from_paths(
                paths, symbol=symbol, start=start, end=end, report=report
            ):
                if first_ts is None:
                    first_ts = trade.trade_ts
                last_ts = trade.trade_ts
            report.actual_first_ts = first_ts
            report.actual_last_ts = last_ts
        except PublicTradeFileSourceError as exc:
            report.valid = False
            report.reason = str(exc)
            return report

        if report.trades_emitted == 0:
            report.valid = False
            report.reason = "no_trades_in_window"
            return report

        report.valid = True
        if report.partial:
            report.reason = f"partial_missing:{','.join(missing)}"
        else:
            report.reason = "ok"
        return report

    def iter_trades(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> Iterator[NormalizedPublicTrade]:
        start = _ensure_utc(start)
        end = _ensure_utc(end)
        symbol = symbol.upper()
        paths, _expected, _found, missing = self._plan_files(symbol, start, end)
        if missing and not self.allow_partial_coverage:
            raise PublicTradeFileSourceError(
                f"missing day file(s) for {symbol}: {', '.join(missing)}"
            )
        if not paths:
            raise PublicTradeFileSourceError(f"no trade files for {symbol} in window")
        yield from self._iter_from_paths(paths, symbol=symbol, start=start, end=end)

    def _iter_from_paths(
        self,
        paths: list[Path],
        *,
        symbol: str,
        start: datetime,
        end: datetime,
        report: TradeCoverageReport | None = None,
    ) -> Iterator[NormalizedPublicTrade]:
        recent_ids: deque[str] = deque(maxlen=100_000)
        recent_set: set[str] = set()
        last_ts: datetime | None = None

        for path in paths:
            with gzip.open(path, "rt", newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                if reader.fieldnames is None:
                    raise PublicTradeFileSourceError(f"empty CSV header in {path}")
                for line_no, row in enumerate(reader, start=2):
                    if report is not None:
                        report.rows_read += 1
                    try:
                        trade = parse_csv_trade_row(
                            row,
                            expected_symbol=symbol,
                            source=self.source_name,
                            source_file=str(path),
                            source_line=line_no,
                        )
                    except PublicTradeParseError as exc:
                        if report is not None:
                            report.invalid_rows += 1
                        if self.strict:
                            raise PublicTradeFileSourceError(str(exc)) from exc
                        continue

                    if trade.trade_ts < start:
                        continue
                    if trade.trade_ts >= end:
                        # half-open [start, end); files are chronological
                        break

                    if last_ts is not None and trade.trade_ts < last_ts:
                        raise PublicTradeFileSourceError(
                            f"timestamp moved backwards at {path}:{line_no}"
                        )
                    last_ts = trade.trade_ts

                    if self.dedupe:
                        tid = trade.trade_id
                        if tid in recent_set:
                            if report is not None:
                                report.duplicate_trades += 1
                            continue
                        if len(recent_ids) == recent_ids.maxlen:
                            old = recent_ids[0]
                            recent_set.discard(old)
                        recent_ids.append(tid)
                        recent_set.add(tid)

                    if report is not None:
                        report.trades_emitted += 1
                        if trade.side == "Buy":
                            report.buy_count += 1
                        else:
                            report.sell_count += 1
                        if trade.notional_mismatch:
                            report.notional_mismatches += 1

                    yield trade
