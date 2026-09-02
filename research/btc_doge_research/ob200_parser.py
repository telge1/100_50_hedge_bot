"""Strict streaming parser that reconstructs every full OB200 book event."""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from .contracts import (
    OB200_KEY_VERSION,
    parse_utc,
    stable_hash,
    utc,
    validate_symbol,
)
from .source_file_registry import SourceFile

ZERO = Decimal("0")
SUPPORTED_TYPES = {"snapshot", "rotation_checkpoint", "delta"}


@dataclass(frozen=True)
class FullBookEvent:
    event_time: datetime
    receive_time: datetime | None
    exchange_sequence: int
    update_id: int
    raw_event_type: str
    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]
    source_record: int
    event_key: str
    content_fingerprint: str
    quality_flags: tuple[str, ...]


@dataclass
class ParseAudit:
    records_read: int = 0
    replayable_records: int = 0
    emitted_events: int = 0
    duplicate_u: int = 0
    u_gaps: list[tuple[int, int]] = field(default_factory=list)
    first_event_time: datetime | None = None
    last_event_time: datetime | None = None
    min_bid_levels: int | None = None
    min_ask_levels: int | None = None
    max_bid_levels: int = 0
    max_ask_levels: int = 0
    short_book_events: int = 0
    identical_timestamp_groups: int = 0
    manifest_replayable: bool | None = None
    effective_replayable: bool = False
    full_file_consumed: bool = False


def iter_json_records(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    if path.suffix == ".zst":
        import zstandard as zstd

        with path.open("rb") as handle:
            with zstd.ZstdDecompressor().stream_reader(handle) as stream:
                text = io.TextIOWrapper(stream, encoding="utf-8")
                for record, line in enumerate(text):
                    if line.strip():
                        value = json.loads(line)
                        if not isinstance(value, dict):
                            raise ValueError(f"record {record} is not an object")
                        yield record, value
        return
    with path.open("rt", encoding="utf-8") as handle:
        for record, line in enumerate(handle):
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError(f"record {record} is not an object")
                yield record, value


class OB200SegmentReader:
    def __init__(self, source: SourceFile, symbol: str) -> None:
        self.source = source
        self.symbol = validate_symbol(symbol)
        self.audit = ParseAudit(
            manifest_replayable=bool(source.manifest.get("replayable"))
        )

    def iter_full_books(
        self, start: datetime, end: datetime
    ) -> Iterator[FullBookEvent]:
        start, end = utc(start), utc(end)
        bids: dict[Decimal, Decimal] = {}
        asks: dict[Decimal, Decimal] = {}
        last_u: int | None = None
        valid = False
        previous_ts: datetime | None = None

        for record, obj in iter_json_records(self.source.path):
            self.audit.records_read += 1
            event_type = str(obj.get("type", ""))
            if event_type not in SUPPORTED_TYPES:
                continue
            self.audit.replayable_records += 1
            if obj.get("format_version") not in {
                None,
                "ob200_v3_live_archive/v1",
            }:
                raise ValueError(f"unexpected record format at {record}")
            data = obj.get("data")
            if not isinstance(data, dict) or str(data.get("s")) != self.symbol:
                raise ValueError(f"invalid symbol/data at record {record}")
            event_ms = obj.get("ts")
            if not isinstance(event_ms, int):
                raise ValueError(f"missing integer ts at record {record}")
            event_time = datetime.fromtimestamp(
                event_ms / 1000.0, tz=timezone.utc
            )
            self.audit.first_event_time = self.audit.first_event_time or event_time
            self.audit.last_event_time = event_time
            if previous_ts == event_time:
                self.audit.identical_timestamp_groups += 1
            previous_ts = event_time

            update_id = int(data.get("u") or 0)
            sequence = int(data.get("seq") or 0)
            if event_type in {"snapshot", "rotation_checkpoint"}:
                bids.clear()
                asks.clear()
                self._apply_levels(bids, data.get("b"), record)
                self._apply_levels(asks, data.get("a"), record)
                last_u = update_id
                valid = True
            else:
                if not valid or last_u is None:
                    raise ValueError(f"delta before checkpoint at record {record}")
                if update_id == last_u:
                    self.audit.duplicate_u += 1
                elif update_id != last_u + 1:
                    self.audit.u_gaps.append((last_u, update_id))
                    raise ValueError(
                        f"u continuity gap at record {record}: {last_u}->{update_id}"
                    )
                self._apply_levels(bids, data.get("b"), record)
                self._apply_levels(asks, data.get("a"), record)
                last_u = update_id

            if not (start <= event_time < end):
                continue
            sorted_bids = tuple(sorted(bids.items(), reverse=True))
            sorted_asks = tuple(sorted(asks.items()))
            flags = self._validate_book(sorted_bids, sorted_asks, record)
            receive_time = self._receive_time(obj.get("local_receive_ts"))
            key_payload = {
                "version": OB200_KEY_VERSION,
                "symbol": self.symbol,
                "event_ms": event_ms,
                "update_id": update_id,
                "event_type": event_type,
                "source_fingerprint": self.source.fingerprint,
                "source_record": record,
            }
            content_payload = {
                "bids": [(str(p), str(q)) for p, q in sorted_bids],
                "asks": [(str(p), str(q)) for p, q in sorted_asks],
            }
            self.audit.emitted_events += 1
            yield FullBookEvent(
                event_time=event_time,
                receive_time=receive_time,
                exchange_sequence=sequence,
                update_id=update_id,
                raw_event_type=event_type,
                bids=sorted_bids,
                asks=sorted_asks,
                source_record=record,
                event_key=stable_hash(key_payload),
                content_fingerprint=stable_hash(content_payload),
                quality_flags=tuple(flags),
            )

        self.audit.full_file_consumed = True
        self.audit.effective_replayable = not self.audit.u_gaps and valid

    @staticmethod
    def _apply_levels(
        book: dict[Decimal, Decimal], values: Any, record: int
    ) -> None:
        if values is None:
            return
        if not isinstance(values, list):
            raise ValueError(f"levels not a list at record {record}")
        for level in values:
            if not isinstance(level, list) or len(level) != 2:
                raise ValueError(f"invalid level at record {record}")
            price, size = Decimal(str(level[0])), Decimal(str(level[1]))
            if not price.is_finite() or not size.is_finite():
                raise ValueError(f"non-finite level at record {record}")
            if price <= ZERO or size < ZERO:
                raise ValueError(f"nonpositive price/negative size at record {record}")
            if size == ZERO:
                book.pop(price, None)
            else:
                book[price] = size

    def _validate_book(
        self,
        bids: tuple[tuple[Decimal, Decimal], ...],
        asks: tuple[tuple[Decimal, Decimal], ...],
        record: int,
    ) -> list[str]:
        if not bids or not asks:
            raise ValueError(f"empty book side at record {record}")
        if len(bids) > 200 or len(asks) > 200:
            raise ValueError(f"more than 200 levels at record {record}")
        if bids[0][0] >= asks[0][0]:
            raise ValueError(f"crossed book at record {record}")
        self.audit.min_bid_levels = (
            len(bids)
            if self.audit.min_bid_levels is None
            else min(self.audit.min_bid_levels, len(bids))
        )
        self.audit.min_ask_levels = (
            len(asks)
            if self.audit.min_ask_levels is None
            else min(self.audit.min_ask_levels, len(asks))
        )
        self.audit.max_bid_levels = max(self.audit.max_bid_levels, len(bids))
        self.audit.max_ask_levels = max(self.audit.max_ask_levels, len(asks))
        flags: list[str] = []
        if len(bids) < 200 or len(asks) < 200:
            flags.append("SHORT_BOOK")
            self.audit.short_book_events += 1
        if self.source.manifest.get("replayable") is False:
            flags.append("LEGACY_MANIFEST_REPLAYABLE_FALSE_REASSESSED_BY_U")
        return flags

    @staticmethod
    def _receive_time(value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            scale = 1000.0 if value > 10_000_000_000 else 1.0
            return datetime.fromtimestamp(value / scale, tz=timezone.utc)
        return parse_utc(str(value))
