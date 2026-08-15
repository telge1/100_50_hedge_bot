"""In-memory store for dry-run / unit tests (no MySQL writes)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from research.regime_scanner.derivatives.aggregate_5m import BucketRecord
from research.regime_scanner.derivatives.schema import SCHEMA_STATEMENTS


@dataclass
class UpsertStats:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0


@dataclass
class InMemoryDerivativeStore:
    """Holds buckets + import-run metadata in process memory."""

    oi: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)
    liq: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)
    oflow: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)
    runs: dict[str, dict[str, Any]] = field(default_factory=dict)
    schema_initialized: bool = False
    write_log: list[str] = field(default_factory=list)

    def init_schema(self) -> None:
        self.schema_initialized = True
        self.write_log.append("init_schema")

    def close(self) -> None:
        return None

    @staticmethod
    def _key(b: BucketRecord) -> tuple[str, str, str]:
        return (
            b.symbol,
            b.bucket_start.isoformat().replace("+00:00", "Z"),
            b.import_version,
        )

    def upsert_buckets(self, buckets: list[BucketRecord]) -> UpsertStats:
        stats = UpsertStats()
        for b in buckets:
            key = self._key(b)
            payload = b.to_dict()
            prior = self.oi.get(key)
            if prior is None:
                stats.inserted += 1
            elif prior.get("source_hash") == b.source_hash:
                stats.unchanged += 1
            else:
                stats.updated += 1
            self.oi[key] = dict(payload)
            self.liq[key] = dict(payload)
            self.oflow[key] = dict(payload)
            self.write_log.append(f"upsert:{key[0]}:{key[1]}")
        return stats

    def get_buckets(
        self,
        *,
        symbols: list[str],
        import_version: str,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for (sym, _bs, ver), row in sorted(self.oi.items()):
            if sym in symbols and ver == import_version:
                out.append(row)
        return out

    def record_import_run(self, label: str, payload: dict[str, Any]) -> None:
        self.runs[label] = dict(payload)
        self.write_log.append(f"run:{label}")

    def schema_sql(self) -> tuple[str, ...]:
        return SCHEMA_STATEMENTS
