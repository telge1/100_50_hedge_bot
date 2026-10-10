"""One in-memory snapshot, one session. Does not reread the parquet file."""

from __future__ import annotations

from ..active_snapshot.canonical_candle_session import CanonicalCandleSession
from ..immutable_candle_snapshot_v1 import CandleInputRow
from ..immutable_snapshot_candle_session import (
    ImmutableInputCoverageError,
    ImmutableSnapshotCandleSession,
    _ClickHouseFallbackRefused,
)


class ReusedSnapshotSession(ImmutableSnapshotCandleSession):
    """Same incremental session as Phase 11L, fed by rows already verified in this process."""

    def __init__(
        self,
        rows: list[CandleInputRow],
        *,
        snapshot_id: str,
        ohlcv_hash: str,
        symbol: str = "DOGEUSDT",
        timeframe: str = "5m",
    ) -> None:
        CanonicalCandleSession.__init__(
            self,
            symbol,
            timeframe,
            max_1m_bars=300_000,
            use_incremental_5m=True,
        )
        self._source = _ClickHouseFallbackRefused()
        self.snapshot_id = snapshot_id
        self.ohlcv_hash = ohlcv_hash
        if not rows or rows[0].symbol != symbol:
            raise ImmutableInputCoverageError("snapshot symbol mismatch")
        self._snap_rows = {int(r.open_time.timestamp()): r for r in rows}
        self._cover_lo = min(self._snap_rows)
        self._cover_hi = max(self._snap_rows)
