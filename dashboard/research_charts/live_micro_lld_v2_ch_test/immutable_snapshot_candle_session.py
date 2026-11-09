"""Serve CanonicalCandleSession from one immutable 1m snapshot. No ClickHouse fallback."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from data.models import Candle

from .active_snapshot.canonical_candle_session import CanonicalCandleSession
from .immutable_candle_snapshot_v1 import (
    CandleInputRow,
    SnapshotError,
    require_snapshot,
)

LEGACY_ENGINE_INPUT_HASH_NOTE = (
    "engine_input_hash covers config, window, candle count, and the first/last close only. "
    "It does not identify every OHLCV value. Phase 11R does not replace that legacy hash. "
    "content_based_input_id is the versioned full-content identity."
)


class ImmutableInputCoverageError(SnapshotError):
    """Requested 1m range is outside the frozen snapshot or a minute is missing."""


class _ClickHouseFallbackRefused:
    def get_1m_candles(self, *args, **kwargs):
        raise ImmutableInputCoverageError("clickhouse fallback refused")


class ImmutableSnapshotCandleSession(CanonicalCandleSession):
    def __init__(
        self,
        directory: Path,
        snapshot_id: str,
        *,
        ohlcv_hash: str,
        symbol: str = "DOGEUSDT",
        timeframe: str = "5m",
        max_1m_bars: int = 300_000,
    ) -> None:
        super().__init__(
            symbol,
            timeframe,
            max_1m_bars=max_1m_bars,
            use_incremental_5m=True,
        )
        self._source = _ClickHouseFallbackRefused()
        self.snapshot_id = snapshot_id
        self.ohlcv_hash = ohlcv_hash
        rows = require_snapshot(directory, snapshot_id, ohlcv_hash=ohlcv_hash)
        if rows[0].symbol != self.symbol:
            raise ImmutableInputCoverageError("snapshot symbol mismatch")
        self._snap_rows = {int(r.open_time.timestamp()): r for r in rows}
        self._cover_lo = min(self._snap_rows)
        self._cover_hi = max(self._snap_rows)

    def _fetch_1m_range(self, fetch_lo: int, fetch_hi: int) -> None:
        if fetch_lo > fetch_hi:
            return
        if int(fetch_lo) < self._cover_lo or int(fetch_hi) > self._cover_hi:
            raise ImmutableInputCoverageError(
                f"IMMUTABLE_INPUT_COVERAGE_ERROR [{fetch_lo}, {fetch_hi}] "
                f"outside [{self._cover_lo}, {self._cover_hi}]"
            )
        candles: list[Candle] = []
        u = int(fetch_lo)
        while u <= int(fetch_hi):
            row = self._snap_rows.get(u)
            if row is None:
                raise ImmutableInputCoverageError(f"IMMUTABLE_INPUT_COVERAGE_ERROR missing {u}")
            candles.append(_row_to_candle(row))
            u += 60
        new_u = self._merge_1m(candles)
        if self._1m_by_unix:
            self._min_unix = min(self._1m_by_unix)
            self._max_unix = max(self._1m_by_unix)
        if self.use_incremental_5m:
            self._incremental_update_5m(new_u)


def _row_to_candle(row: CandleInputRow) -> Candle:
    return Candle(
        timestamp=row.open_time,
        open=float(row.open),
        high=float(row.high),
        low=float(row.low),
        close=float(row.close),
        volume=float(row.volume),
        symbol=row.symbol,
        timeframe="1m",
    )


def content_based_input_id(
    *,
    snapshot_id: str,
    price_content_hash: str,
    revision_metadata_hash: str,
    config_hash: str,
    history_policy: str,
    decision_close: datetime,
) -> str:
    payload = {
        "identity_version": "content_based_input_v1",
        "snapshot_id": snapshot_id,
        "price_content_hash": price_content_hash,
        "revision_metadata_hash": revision_metadata_hash,
        "config_hash": config_hash,
        "history_policy": history_policy,
        "decision_close": decision_close.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def remaining_projection_fingerprint(projections) -> str:
    lines = []
    for p in sorted(projections, key=lambda x: x.pool_id):
        lines.append(
            "|".join(
                (
                    p.pool_id,
                    f"{p.original_bottom:.8f}",
                    f"{p.original_top:.8f}",
                    f"{p.remaining_bottom:.8f}",
                    f"{p.remaining_top:.8f}",
                    f"{p.remaining_fraction:.8f}",
                    str(int(p.fully_traversed)),
                    str(int(p.engine_active)),
                    p.strength_class,
                )
            )
        )
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
