"""Extended manifest for 51-coin × 7-day public-trade backfill."""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

STATUSES = (
    "PENDING",
    "AVAILABLE",
    "DOWNLOADED",
    "VERIFIED",
    "IMPORTED",
    "AUDITED",
    "FAILED",
    "MISSING",
    "ARCHIVE_UNAVAILABLE",
)

ALLOWED_TRANSITIONS = {
    "PENDING": frozenset(
        {
            "AVAILABLE",
            "MISSING",
            "ARCHIVE_UNAVAILABLE",
            "FAILED",
            "PENDING",
            "AUDITED",
            "DOWNLOADED",
        }
    ),
    "AVAILABLE": frozenset(
        {"DOWNLOADED", "FAILED", "AVAILABLE", "AUDITED", "ARCHIVE_UNAVAILABLE", "MISSING"}
    ),
    "DOWNLOADED": frozenset({"VERIFIED", "FAILED", "DOWNLOADED"}),
    "VERIFIED": frozenset({"IMPORTED", "FAILED", "VERIFIED"}),
    "IMPORTED": frozenset({"AUDITED", "FAILED", "IMPORTED", "VERIFIED"}),
    "AUDITED": frozenset({"AUDITED"}),
    "FAILED": frozenset(
        {"PENDING", "AVAILABLE", "DOWNLOADED", "VERIFIED", "IMPORTED", "FAILED"}
    ),
    "MISSING": frozenset({"PENDING", "AVAILABLE", "MISSING", "FAILED", "DOWNLOADED"}),
    "ARCHIVE_UNAVAILABLE": frozenset(
        {"PENDING", "AVAILABLE", "ARCHIVE_UNAVAILABLE", "FAILED"}
    ),
}

BACKFILL_MANIFEST_FIELDS = [
    "symbol",
    "utc_date",
    "url",
    "http_status",
    "content_length",
    "compressed_bytes",
    "source_rows",
    "parsed_rows",
    "invalid_rows",
    "duplicate_trade_ids_in_file",
    "duplicate_trade_ids_against_target",
    "out_of_day_rows",
    "inserted_rows",
    "skipped_existing_rows",
    "min_event_time",
    "max_event_time",
    "status",
    "error",
    "retry_count",
    "download_started_at",
    "import_finished_at",
    "audit_finished_at",
    "sha256",
    "updated_at",
]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def file_key(symbol: str, utc_date: str) -> str:
    return f"{symbol.upper()}|{utc_date}"


@dataclass
class BackfillManifestRow:
    symbol: str
    utc_date: str
    url: str
    http_status: int | None = None
    content_length: int | None = None
    compressed_bytes: int = 0
    source_rows: int = 0
    parsed_rows: int = 0
    invalid_rows: int = 0
    duplicate_trade_ids_in_file: int = 0
    duplicate_trade_ids_against_target: int = 0
    out_of_day_rows: int = 0
    inserted_rows: int = 0
    skipped_existing_rows: int = 0
    min_event_time: str = ""
    max_event_time: str = ""
    status: str = "PENDING"
    error: str = ""
    retry_count: int = 0
    download_started_at: str = ""
    import_finished_at: str = ""
    audit_finished_at: str = ""
    sha256: str = ""
    updated_at: str = field(default_factory=_utcnow)

    @property
    def key(self) -> str:
        return file_key(self.symbol, self.utc_date)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BackfillManifestStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.rows: dict[str, BackfillManifestRow] = {}
        if path.is_file():
            self._load()

    def _load(self) -> None:
        with self.path.open(encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for raw in reader:
                row = BackfillManifestRow(
                    symbol=raw["symbol"],
                    utc_date=raw["utc_date"],
                    url=raw["url"],
                    http_status=int(raw["http_status"]) if raw.get("http_status") else None,
                    content_length=int(raw["content_length"]) if raw.get("content_length") else None,
                    compressed_bytes=int(raw.get("compressed_bytes") or 0),
                    source_rows=int(raw.get("source_rows") or 0),
                    parsed_rows=int(raw.get("parsed_rows") or 0),
                    invalid_rows=int(raw.get("invalid_rows") or 0),
                    duplicate_trade_ids_in_file=int(raw.get("duplicate_trade_ids_in_file") or 0),
                    duplicate_trade_ids_against_target=int(
                        raw.get("duplicate_trade_ids_against_target") or 0
                    ),
                    out_of_day_rows=int(raw.get("out_of_day_rows") or 0),
                    inserted_rows=int(raw.get("inserted_rows") or 0),
                    skipped_existing_rows=int(raw.get("skipped_existing_rows") or 0),
                    min_event_time=raw.get("min_event_time") or "",
                    max_event_time=raw.get("max_event_time") or "",
                    status=raw.get("status") or "PENDING",
                    error=raw.get("error") or "",
                    retry_count=int(raw.get("retry_count") or 0),
                    download_started_at=raw.get("download_started_at") or "",
                    import_finished_at=raw.get("import_finished_at") or "",
                    audit_finished_at=raw.get("audit_finished_at") or "",
                    sha256=raw.get("sha256") or "",
                    updated_at=raw.get("updated_at") or _utcnow(),
                )
                self.rows[row.key] = row

    def ensure(self, symbol: str, utc_date: str, url: str) -> BackfillManifestRow:
        key = file_key(symbol, utc_date)
        if key not in self.rows:
            self.rows[key] = BackfillManifestRow(
                symbol=symbol.upper(), utc_date=utc_date, url=url
            )
            self.save()
        return self.rows[key]

    def get(self, symbol: str, utc_date: str) -> BackfillManifestRow | None:
        return self.rows.get(file_key(symbol, utc_date))

    def set_status(self, row: BackfillManifestRow, status: str, **updates: Any) -> BackfillManifestRow:
        allowed = ALLOWED_TRANSITIONS.get(row.status, frozenset())
        if status not in allowed:
            raise ValueError(f"illegal status {row.status} -> {status} for {row.key}")
        for key, value in updates.items():
            setattr(row, key, value)
        row.status = status
        row.updated_at = _utcnow()
        self.rows[row.key] = row
        self.save()
        return row

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=BACKFILL_MANIFEST_FIELDS)
            writer.writeheader()
            for row in sorted(self.rows.values(), key=lambda r: (r.symbol, r.utc_date)):
                writer.writerow(row.to_dict())
        tmp.replace(self.path)

    def as_rows(self) -> list[BackfillManifestRow]:
        return [self.rows[k] for k in sorted(self.rows)]

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.rows.values():
            counts[row.status] = counts.get(row.status, 0) + 1
        return counts


def iter_backfill_jobs(symbols: Iterable[str], days: Iterable[str]) -> list[tuple[str, str]]:
    return [(s.upper(), d) for s in symbols for d in days]
