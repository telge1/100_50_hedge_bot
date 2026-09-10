"""Documented availability — not a silent copy of state_ts."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from orderbook_analyse.market_profile.anchor import as_utc

from .time_bounds import last_included_1m_close


def market_profile_available_at(
    *,
    effective_profile_end: datetime,
    use_final: bool,
    uses_1m_ohlc: bool = True,
) -> dict[str, Any]:
    """Earliest instant the chart MP payload for [start, effective_end) is complete.

    Pipeline facts (dashboard dual_profile / loader, Phase-1 lineage):

    * Trades: ``trade_ts < effective_profile_end`` on
      ``orderbook_analysis.public_trades_canonical``. Bound is event time.
      Dashboard default ``use_final=False`` — no ReplacingMergeTree FINAL wait.
    * 1m OHLC: ``open_time < effective_profile_end`` on
      ``signal_generator.candles_1m``. A 1m bar is complete at its close
      (open + 60s). Loader uses FINAL only as query-time dedupe, not a delay.
    * Aggregation is a SELECT over those bounds; no extra finalize watermark.

    Therefore ``available_at = max(trade_bound, last_1m_close, final_bound)``.
    For minute-aligned exclusive ends this equals ``effective_profile_end``.
    ``state_ts`` remains ``effective_profile_end`` and is a different field.
    """
    end = as_utc(effective_profile_end)
    trade_ready = end
    candle_ready = last_included_1m_close(end) if uses_1m_ohlc else end
    # FINAL on trades is opt-in and off for the live chart path.
    final_ready = end
    _ = use_final
    available = max(trade_ready, candle_ready, final_ready)
    return {
        "available_at": available,
        "components": {
            "trade_event_time_exclusive_bound": trade_ready.isoformat().replace("+00:00", "Z"),
            "canonical_trade_final_delay_s": 0,
            "use_final_trades": bool(use_final),
            "last_included_1m_candle_close": candle_ready.isoformat().replace("+00:00", "Z"),
            "aggregation_finalize_delay_s": 0,
            "rule": "max(trade_exclusive_end, last_1m_close, no_final_wait)",
        },
    }


def later_than_decision(available_at: datetime, decision_time: datetime) -> bool:
    return as_utc(available_at) > as_utc(decision_time)
