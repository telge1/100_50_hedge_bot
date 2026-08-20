"""Historical Bybit OB200 NDJSON file OrderBookEventSource."""

from __future__ import annotations

import re
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from orderbook_analyse.ob_data_source.ndjson_parse import (
    Ob200Message,
    Ob200ParseError,
    parse_ob200_line,
)
from orderbook_analyse.ob_data_source.protocol import BootstrapRef, CoverageReport
from orderbook_analyse.orderbook_replay import BookLevelEvent, ReplayError

FILENAME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})_(?P<symbol>[A-Za-z0-9]+)_ob200\.data$"
)


class Ob200FileSourceError(ReplayError):
    """Hard failure while reading OB200 files."""


def _ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _day_range(start: datetime, end: datetime) -> list[date]:
    start = _ensure_utc(start)
    end = _ensure_utc(end)
    if end < start:
        raise Ob200FileSourceError("end before start")
    days: list[date] = []
    d = start.date()
    last = end.date()
    while d <= last:
        days.append(d)
        d += timedelta(days=1)
    return days


class _FileRef:
    __slots__ = ("day", "symbol", "path")

    def __init__(self, day: date, symbol: str, path: Path) -> None:
        self.day = day
        self.symbol = symbol
        self.path = path


class Ob200FileOrderBookEventSource:
    """Stream OB200 NDJSON day files into BookLevelEvent for OrderBookReplayer."""

    source_name = "files"

    def __init__(
        self,
        files_root: Path | str,
        *,
        file_pattern: str = "*/*.data",
        strict: bool = True,
        boundary_dedupe: bool = True,
    ) -> None:
        self.files_root = Path(files_root)
        self.file_pattern = file_pattern
        self.strict = strict
        self.boundary_dedupe = boundary_dedupe
        if not self.files_root.exists():
            raise Ob200FileSourceError(f"files_root does not exist: {self.files_root}")

    def _index_files(self, symbol: str) -> dict[date, _FileRef]:
        symbol = symbol.upper()
        by_day: dict[date, _FileRef] = {}
        for path in sorted(self.files_root.glob(self.file_pattern)):
            if not path.is_file():
                continue
            m = FILENAME_RE.match(path.name)
            if m is None:
                continue
            if m.group("symbol").upper() != symbol:
                continue
            day = date.fromisoformat(m.group("date"))
            by_day[day] = _FileRef(day=day, symbol=symbol, path=path)
        return by_day

    def _resolve_day_files(self, symbol: str, start: datetime, end: datetime) -> list[_FileRef]:
        start = _ensure_utc(start)
        end = _ensure_utc(end)
        index = self._index_files(symbol)
        days = _day_range(start, end)
        missing = [d for d in days if d not in index]
        if missing:
            raise Ob200FileSourceError(
                f"missing day file(s) for {symbol}: "
                + ", ".join(d.isoformat() for d in missing)
            )
        return [index[d] for d in days]

    def find_bootstrap(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> BootstrapRef:
        """First snapshot of the start-day file; must have exchange_ts <= start."""
        start = _ensure_utc(start)
        end = _ensure_utc(end)
        if end < start:
            raise Ob200FileSourceError("end before start")
        files = self._resolve_day_files(symbol, start, end)
        start_file = files[0]
        first_snapshot: Ob200Message | None = None
        with start_file.path.open("rb") as fh:
            for line_no, line in enumerate(fh, start=1):
                if not line.strip():
                    continue
                try:
                    msg = parse_ob200_line(
                        line,
                        expected_symbol=symbol.upper(),
                        source_file=str(start_file.path),
                        source_line=line_no,
                    )
                except Ob200ParseError as exc:
                    raise Ob200FileSourceError(str(exc)) from exc
                if msg.message_type == "snapshot":
                    first_snapshot = msg
                    break
                raise Ob200FileSourceError(
                    f"delta before snapshot at {start_file.path}:{line_no}"
                )
        if first_snapshot is None:
            raise Ob200FileSourceError(
                f"no snapshot in start-day file {start_file.path}"
            )
        if first_snapshot.exchange_ts > start:
            raise Ob200FileSourceError(
                f"no snapshot at or before start={start.isoformat()}; "
                f"first snapshot at {first_snapshot.exchange_ts.isoformat()}"
            )
        return BootstrapRef(
            exchange_ts=first_snapshot.exchange_ts,
            update_id=first_snapshot.update_id,
            cross_sequence=first_snapshot.cross_sequence,
        )

    def iter_messages(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        *,
        dedupe_counter: list[int] | None = None,
    ) -> Iterator[Ob200Message]:
        """Yield unique messages from day-start snapshot through ``end`` (incl. warmup)."""
        start = _ensure_utc(start)
        end = _ensure_utc(end)
        symbol = symbol.upper()
        _ = self.find_bootstrap(symbol, start, end)
        files = self._resolve_day_files(symbol, start, end)

        recent_keys: deque[tuple] = deque(maxlen=64)
        recent_set: set[tuple] = set()
        last_u: int | None = None
        last_seq: int | None = None
        last_ts: datetime | None = None
        saw_snapshot = False

        for ref in files:
            with ref.path.open("rb") as fh:
                for line_no, line in enumerate(fh, start=1):
                    if not line.strip():
                        continue
                    try:
                        msg = parse_ob200_line(
                            line,
                            expected_symbol=symbol,
                            source_file=str(ref.path),
                            source_line=line_no,
                        )
                    except Ob200ParseError as exc:
                        if self.strict:
                            raise Ob200FileSourceError(str(exc)) from exc
                        continue
                    if msg.exchange_ts > end:
                        return
                    if last_ts is not None and msg.exchange_ts < last_ts:
                        raise Ob200FileSourceError(
                            f"timestamp moved backwards at {ref.path}:{line_no}: "
                            f"{last_ts.isoformat()} -> {msg.exchange_ts.isoformat()}"
                        )
                    last_ts = msg.exchange_ts

                    key = msg.dedupe_key()
                    if self.boundary_dedupe and key in recent_set:
                        if dedupe_counter is not None:
                            dedupe_counter[0] += 1
                        continue
                    if self.boundary_dedupe:
                        if len(recent_keys) == recent_keys.maxlen:
                            old = recent_keys[0]
                            recent_set.discard(old)
                        recent_keys.append(key)
                        recent_set.add(key)

                    if not saw_snapshot:
                        if msg.message_type != "snapshot":
                            raise Ob200FileSourceError(
                                f"delta before snapshot at {ref.path}:{line_no}"
                            )
                        saw_snapshot = True
                    elif msg.message_type == "delta":
                        if last_u is None:
                            raise Ob200FileSourceError("delta before snapshot")
                        expected = last_u + 1
                        if msg.update_id != expected:
                            raise Ob200FileSourceError(
                                f"update_id gap at {ref.path}:{line_no}: "
                                f"expected {expected}, got {msg.update_id}"
                            )
                        if last_seq is not None and msg.cross_sequence < last_seq:
                            raise Ob200FileSourceError(
                                f"cross_sequence backwards at {ref.path}:{line_no}: "
                                f"{last_seq} -> {msg.cross_sequence}"
                            )
                    # mid-stream snapshot reseats (allowed; replayer clears book)
                    last_u = msg.update_id
                    last_seq = msg.cross_sequence
                    yield msg

    def iter_events(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> Iterator[BookLevelEvent]:
        for msg in self.iter_messages(symbol, start, end):
            yield from msg.to_book_level_events()

    def coverage(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> CoverageReport:
        start = _ensure_utc(start)
        end = _ensure_utc(end)
        symbol = symbol.upper()
        report = CoverageReport(
            symbol=symbol,
            requested_start=start,
            requested_end=end,
            source=self.source_name,
        )
        try:
            files = self._resolve_day_files(symbol, start, end)
            boot = self.find_bootstrap(symbol, start, end)
        except Ob200FileSourceError as exc:
            report.valid = False
            report.reason = str(exc)
            return report

        report.files_used = [str(f.path) for f in files]
        report.actual_first_ts = boot.exchange_ts
        dedupe_counter = [0]
        events_emitted = 0
        snaps = deltas = 0
        last_ts: datetime | None = None
        try:
            for msg in self.iter_messages(
                symbol, start, end, dedupe_counter=dedupe_counter
            ):
                if msg.message_type == "snapshot":
                    snaps += 1
                else:
                    deltas += 1
                events_emitted += len(msg.bids) + len(msg.asks)
                last_ts = msg.exchange_ts
                report.messages_read += 1
        except Ob200FileSourceError as exc:
            report.snapshots = snaps
            report.deltas = deltas
            report.events_emitted = events_emitted
            report.boundary_dedupes = dedupe_counter[0]
            report.actual_last_ts = last_ts
            report.valid = False
            reason = str(exc)
            if "update_id gap" in reason:
                report.update_gaps = 1
            if "cross_sequence backwards" in reason:
                report.sequence_backwards = 1
            if "timestamp moved backwards" in reason:
                report.timestamp_backwards = 1
            if "invalid JSON" in reason:
                report.invalid_json = 1
            report.reason = reason
            return report

        report.snapshots = snaps
        report.deltas = deltas
        report.events_emitted = events_emitted
        report.boundary_dedupes = dedupe_counter[0]
        report.actual_last_ts = last_ts
        report.valid = snaps >= 1
        report.reason = "ok" if report.valid else "no_snapshot"
        return report
