"""File-level manifest for resumable public-trade ingest."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

STATUSES = (
    "PENDING",
    "DOWNLOADED",
    "VERIFIED",
    "IMPORTED",
    "AUDITED",
    "FAILED",
)

ALLOWED_TRANSITIONS = {
    "PENDING": frozenset({"DOWNLOADED", "FAILED", "PENDING"}),
    "DOWNLOADED": frozenset({"VERIFIED", "FAILED", "DOWNLOADED"}),
    "VERIFIED": frozenset({"IMPORTED", "FAILED", "VERIFIED"}),
    "IMPORTED": frozenset({"AUDITED", "IMPORTED", "FAILED", "VERIFIED"}),
    "AUDITED": frozenset({"AUDITED", "VERIFIED", "IMPORTED"}),
    "FAILED": frozenset({"PENDING", "DOWNLOADED", "VERIFIED", "IMPORTED", "FAILED"}),
}

MANIFEST_FIELDS = [
    "symbol",
    "day",
    "url",
    "status",
    "local_path",
    "compressed_bytes",
    "etag",
    "http_status",
    "rowcount_source",
    "rowcount_parsed",
    "invalid_rows",
    "duplicate_rows",
    "out_of_day_rows",
    "inserted_rows",
    "min_trade_ts",
    "max_trade_ts",
    "unique_trade_ids",
    "error",
    "updated_at",
]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def file_key(symbol: str, day: str) -> str:
    return f"{symbol.upper()}|{day}"


@dataclass
class ManifestRow:
    symbol: str
    day: str
    url: str
    status: str = "PENDING"
    local_path: str = ""
    compressed_bytes: int = 0
    etag: str = ""
    http_status: int | None = None
    rowcount_source: int = 0
    rowcount_parsed: int = 0
    invalid_rows: int = 0
    duplicate_rows: int = 0
    out_of_day_rows: int = 0
    inserted_rows: int = 0
    min_trade_ts: str = ""
    max_trade_ts: str = ""
    unique_trade_ids: int = 0
    error: str = ""
    updated_at: str = field(default_factory=_utcnow)

    @property
    def key(self) -> str:
        return file_key(self.symbol, self.day)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ManifestStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.rows: dict[str, ManifestRow] = {}
        if path.is_file():
            self._load()

    def _load(self) -> None:
        with self.path.open(encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for raw in reader:
                row = ManifestRow(
                    symbol=raw["symbol"],
                    day=raw["day"],
                    url=raw["url"],
                    status=raw.get("status") or "PENDING",
                    local_path=raw.get("local_path") or "",
                    compressed_bytes=int(raw.get("compressed_bytes") or 0),
                    etag=raw.get("etag") or "",
                    http_status=int(raw["http_status"]) if raw.get("http_status") else None,
                    rowcount_source=int(raw.get("rowcount_source") or 0),
                    rowcount_parsed=int(raw.get("rowcount_parsed") or 0),
                    invalid_rows=int(raw.get("invalid_rows") or 0),
                    duplicate_rows=int(raw.get("duplicate_rows") or 0),
                    out_of_day_rows=int(raw.get("out_of_day_rows") or 0),
                    inserted_rows=int(raw.get("inserted_rows") or 0),
                    min_trade_ts=raw.get("min_trade_ts") or "",
                    max_trade_ts=raw.get("max_trade_ts") or "",
                    unique_trade_ids=int(raw.get("unique_trade_ids") or 0),
                    error=raw.get("error") or "",
                    updated_at=raw.get("updated_at") or _utcnow(),
                )
                self.rows[row.key] = row

    def ensure(self, symbol: str, day: str, url: str) -> ManifestRow:
        key = file_key(symbol, day)
        if key not in self.rows:
            self.rows[key] = ManifestRow(symbol=symbol.upper(), day=day, url=url)
            self.save()
        return self.rows[key]

    def get(self, symbol: str, day: str) -> ManifestRow | None:
        return self.rows.get(file_key(symbol, day))

    def set_status(self, row: ManifestRow, status: str, **updates: Any) -> ManifestRow:
        current = row.status
        allowed = ALLOWED_TRANSITIONS.get(current, frozenset())
        if status not in allowed:
            raise ValueError(f"illegal status {current} -> {status} for {row.key}")
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
            writer = csv.DictWriter(fh, fieldnames=MANIFEST_FIELDS)
            writer.writeheader()
            for row in sorted(self.rows.values(), key=lambda r: (r.symbol, r.day)):
                writer.writerow(row.to_dict())
        tmp.replace(self.path)

    def as_rows(self) -> list[ManifestRow]:
        return [self.rows[k] for k in sorted(self.rows)]


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)


def iter_jobs(symbols: Iterable[str], days: Iterable[str]) -> list[tuple[str, str]]:
    return [(s.upper(), d) for s in symbols for d in days]
