"""ClickHouse-backed OrderBookEventSource (thin wrap of existing loaders)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterator

from orderbook_analyse.dynamic_wall_detector import (
    ReadOnlyClickHouse,
    connect_readonly,
    find_bootstrap_snapshot,
    load_events,
)
from orderbook_analyse.ob_data_source.protocol import BootstrapRef, CoverageReport
from orderbook_analyse.orderbook_replay import BookLevelEvent, ReplayError


def _ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


class ClickHouseOrderBookEventSource:
    """Delegates to find_bootstrap_snapshot / load_events — no SQL duplication."""

    source_name = "clickhouse"

    def __init__(self, db: ReadOnlyClickHouse | None = None) -> None:
        self._db = db

    @property
    def db(self) -> ReadOnlyClickHouse:
        if self._db is None:
            self._db = connect_readonly()
        return self._db

    def find_bootstrap(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> BootstrapRef:
        start = _ensure_utc(start)
        end = _ensure_utc(end)
        ts, u, seq = find_bootstrap_snapshot(
            self.db, symbol=symbol, start=start, end=end
        )
        return BootstrapRef(exchange_ts=ts, update_id=int(u), cross_sequence=int(seq))

    def iter_events(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> Iterator[BookLevelEvent]:
        start = _ensure_utc(start)
        end = _ensure_utc(end)
        boot = self.find_bootstrap(symbol, start, end)
        events = load_events(
            self.db,
            symbol=symbol,
            snapshot_ts=boot.exchange_ts,
            snapshot_u=boot.update_id,
            snapshot_seq=boot.cross_sequence,
            end=end,
        )
        yield from events

    def coverage(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> CoverageReport:
        start = _ensure_utc(start)
        end = _ensure_utc(end)
        report = CoverageReport(
            symbol=symbol,
            requested_start=start,
            requested_end=end,
            source=self.source_name,
        )
        try:
            boot = self.find_bootstrap(symbol, start, end)
            events = list(
                load_events(
                    self.db,
                    symbol=symbol,
                    snapshot_ts=boot.exchange_ts,
                    snapshot_u=boot.update_id,
                    snapshot_seq=boot.cross_sequence,
                    end=end,
                )
            )
        except ReplayError as exc:
            report.valid = False
            report.reason = str(exc)
            return report
        except Exception as exc:  # noqa: BLE001 — surface as coverage invalid
            report.valid = False
            report.reason = f"clickhouse_error: {exc}"
            return report

        if not events:
            report.valid = False
            report.reason = "no_events"
            return report

        report.events_emitted = len(events)
        report.actual_first_ts = events[0].exchange_ts
        report.actual_last_ts = events[-1].exchange_ts
        # message counts approximate via unique (type,u,seq,ts)
        keys: set[tuple] = set()
        snaps = deltas = 0
        for e in events:
            key = (e.message_type, e.update_id, e.cross_sequence, e.exchange_ts)
            if key in keys:
                continue
            keys.add(key)
            if e.message_type == "snapshot":
                snaps += 1
            else:
                deltas += 1
        report.messages_read = len(keys)
        report.snapshots = snaps
        report.deltas = deltas
        report.valid = True
        report.reason = "ok"
        return report
