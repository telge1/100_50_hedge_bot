"""Phase 11K — required 1m span estimates for trim/capacity checks (research only)."""

from __future__ import annotations

from datetime import datetime, timezone

from research_charts.service import _timeframe_seconds
from research_charts.trp_import import load_trp

from .identity import build_live_micro_lld_v2_config


def required_1m_unix_span(
    history_from_close: datetime,
    decision_close: datetime,
    pool_detail: int,
    timeframe: str = "5m",
) -> tuple[int, int]:
    """Inclusive unix range of 1m bars needed for one decision close (src_start .. to)."""
    cfg = build_live_micro_lld_v2_config(pool_detail)
    warmup_bars = int(cfg.volume_lookback) + int(cfg.swing_len) + 64
    tf_sec = _timeframe_seconds(timeframe)
    from_unix = int(history_from_close.astimezone(timezone.utc).timestamp())
    to_unix = int(decision_close.astimezone(timezone.utc).timestamp())
    load_start = max(0, from_unix - warmup_bars * tf_sec)
    need = int(load_trp()["expected_source_bars"]("1m", timeframe))
    src_start = int(load_start) - need * 60
    return src_start, to_unix


def required_1m_bar_count(src_start_unix: int, to_unix: int) -> int:
    return (int(to_unix) - int(src_start_unix)) // 60 + 1


def max_1m_bars_sufficient(
    max_1m_bars: int,
    src_start_unix: int,
    to_unix: int,
) -> bool:
    return max_1m_bars >= required_1m_bar_count(src_start_unix, to_unix)
