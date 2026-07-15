"""Abstract and in-memory candle store backends."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

import pandas as pd

from research.regime_scanner.mysql_candle_store.schema import ALLOWED_SOURCES
from research.regime_scanner.mysql_candle_store.source_policy import resolve_candle_upsert
from research.regime_scanner.timeframes import ensure_utc_timestamp


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _naive_utc(ts: object) -> datetime:
    t = ensure_utc_timestamp(ts).to_pydatetime()
    return t.replace(tzinfo=None)


@dataclass
class UpsertStats:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped_protected: int = 0
    conflicts: int = 0
    conflict_details: list[dict[str, Any]] = field(default_factory=list)

    def merge(self, other: "UpsertStats") -> "UpsertStats":
        self.inserted += other.inserted
        self.updated += other.updated
        self.unchanged += other.unchanged
        self.skipped_protected += other.skipped_protected
        self.conflicts += other.conflicts
        self.conflict_details.extend(other.conflict_details)
        return self


class CandleStore(Protocol):
    def init_schema(self) -> None: ...

    def upsert_candles(self, rows: list[dict[str, Any]]) -> UpsertStats: ...

    def fetch_candles(
        self,
        *,
        exchange: str,
        symbol: str,
        timeframe: str,
        start_time: object | None = None,
        end_time: object | None = None,
        decision_time: object | None = None,
        closed_only: bool = True,
        source: str | None = None,
    ) -> pd.DataFrame: ...

    def count_candles(self, *, exchange: str, symbol: str, timeframe: str) -> int: ...

    def insert_validation_run(self, row: dict[str, Any]) -> int: ...

    def wipe_timeframe(self, *, exchange: str, symbol: str, timeframe: str) -> int: ...


@dataclass
class InMemoryCandleStore:
    """Deterministic in-memory backend for unit tests (no MySQL)."""

    candles: dict[tuple[str, str, str, datetime], dict[str, Any]] = field(default_factory=dict)
    validation_runs: list[dict[str, Any]] = field(default_factory=list)
    _schema_ready: bool = False
    _next_id: int = 1
    _next_validation_id: int = 1

    def init_schema(self) -> None:
        self._schema_ready = True

    def upsert_candles(self, rows: list[dict[str, Any]]) -> UpsertStats:
        if not self._schema_ready:
            raise RuntimeError("schema not initialized; call init_schema()")
        stats = UpsertStats()
        now = _utc_now()
        for raw in rows:
            source = str(raw["source"])
            if source not in ALLOWED_SOURCES:
                raise ValueError(f"unsupported source: {source}")
            key = (
                str(raw["exchange"]),
                str(raw["symbol"]),
                str(raw["timeframe"]),
                _naive_utc(raw["open_time"]),
            )
            payload = {
                "exchange": key[0],
                "symbol": key[1],
                "timeframe": key[2],
                "open_time": key[3],
                "close_time": _naive_utc(raw["close_time"]),
                "open": float(raw["open"]),
                "high": float(raw["high"]),
                "low": float(raw["low"]),
                "close": float(raw["close"]),
                "volume": float(raw["volume"]),
                "is_closed": bool(raw["is_closed"]),
                "source": source,
                "source_timeframe": raw.get("source_timeframe"),
                "source_hash": raw.get("source_hash"),
                "updated_at": now,
            }
            existing = self.candles.get(key)
            decision = resolve_candle_upsert(existing, payload)
            if decision.action == "insert":
                payload["id"] = self._next_id
                self._next_id += 1
                payload["created_at"] = now
                self.candles[key] = payload
                stats.inserted += 1
            elif decision.action == "update":
                assert existing is not None
                payload["id"] = existing["id"]
                payload["created_at"] = existing["created_at"]
                self.candles[key] = payload
                stats.updated += 1
            elif decision.action == "unchanged":
                stats.unchanged += 1
            elif decision.action == "skip_protected":
                stats.skipped_protected += 1
            else:
                stats.conflicts += 1
                stats.conflict_details.append(
                    {
                        "exchange": key[0],
                        "symbol": key[1],
                        "timeframe": key[2],
                        "open_time": ensure_utc_timestamp(key[3]).isoformat(),
                        "reason": decision.reason,
                        "existing_source": existing.get("source") if existing else None,
                        "incoming_source": source,
                    }
                )
        return stats

    def fetch_candles(
        self,
        *,
        exchange: str,
        symbol: str,
        timeframe: str,
        start_time: object | None = None,
        end_time: object | None = None,
        decision_time: object | None = None,
        closed_only: bool = True,
        source: str | None = None,
    ) -> pd.DataFrame:
        rows = [
            r
            for (ex, sym, tf, _ot), r in self.candles.items()
            if ex == exchange and sym == symbol and tf == timeframe
        ]
        empty_cols = [
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "open_time",
            "close_time",
            "is_closed",
            "source",
            "source_timeframe",
        ]
        if not rows:
            return pd.DataFrame(columns=empty_cols)
        start_n = _naive_utc(start_time) if start_time is not None else None
        end_n = _naive_utc(end_time) if end_time is not None else None
        decision_n = _naive_utc(decision_time) if decision_time is not None else None
        out: list[dict[str, Any]] = []
        for r in rows:
            if closed_only and not r["is_closed"]:
                continue
            if source is not None and str(r.get("source")) != str(source):
                continue
            if start_n is not None and r["open_time"] < start_n:
                continue
            if end_n is not None and r["open_time"] > end_n:
                continue
            if decision_n is not None and r["close_time"] > decision_n:
                continue
            out.append(r)
        frame = pd.DataFrame(out)
        if frame.empty:
            return pd.DataFrame(columns=empty_cols)
        frame = frame.sort_values("open_time").reset_index(drop=True)
        frame["timestamp"] = pd.to_datetime(frame["open_time"], utc=True)
        frame["open_time"] = pd.to_datetime(frame["open_time"], utc=True)
        frame["close_time"] = pd.to_datetime(frame["close_time"], utc=True)
        return frame

    def count_candles(self, *, exchange: str, symbol: str, timeframe: str) -> int:
        return sum(
            1
            for (ex, sym, tf, _) in self.candles
            if ex == exchange and sym == symbol and tf == timeframe
        )

    def insert_validation_run(self, row: dict[str, Any]) -> int:
        payload = dict(row)
        payload["id"] = self._next_validation_id
        self._next_validation_id += 1
        payload.setdefault("created_at", _utc_now())
        self.validation_runs.append(payload)
        return int(payload["id"])

    def wipe_timeframe(self, *, exchange: str, symbol: str, timeframe: str) -> int:
        keys = [
            k
            for k in list(self.candles)
            if k[0] == exchange and k[1] == symbol and k[2] == timeframe
        ]
        for k in keys:
            del self.candles[k]
        return len(keys)
