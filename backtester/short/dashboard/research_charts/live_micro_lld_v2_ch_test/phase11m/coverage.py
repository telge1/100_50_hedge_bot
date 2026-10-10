"""Warmup coverage against the frozen Phase 11R window. No candle download."""

from __future__ import annotations

from datetime import datetime, timezone

from ..identity import build_live_micro_lld_v2_config
from ..phase11l.frozen import required_source_start_unix

SNAPSHOT_START = datetime(2026, 5, 23, 5, 25, tzinfo=timezone.utc)
SNAPSHOT_END_EXCLUSIVE = datetime(2026, 9, 30, 12, 1, tzinfo=timezone.utc)
PROBE_CLOSE = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def warmup_covers(timeframe: str, pool_detail: int, decision_close: datetime = PROBE_CLOSE) -> dict:
    src = datetime.fromtimestamp(
        required_source_start_unix(decision_close, pool_detail, timeframe),
        tz=timezone.utc,
    )
    cfg = build_live_micro_lld_v2_config(int(pool_detail))
    fits = SNAPSHOT_START <= src and decision_close < SNAPSHOT_END_EXCLUSIVE
    return {
        "timeframe": timeframe,
        "pool_detail": int(pool_detail),
        "decision_close": decision_close.isoformat(),
        "required_source_start": src.isoformat(),
        "warmup_bars": int(cfg.volume_lookback) + int(cfg.swing_len) + 64,
        "volume_lookback": int(cfg.volume_lookback),
        "fits_frozen_snapshot": fits,
    }
