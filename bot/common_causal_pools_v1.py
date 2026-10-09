"""Shared causal pool universe for Short, Long, and (future) Live Chart.

Uses only ``frozen_lld_config_v1`` + ``scanner_pools_for_index`` — no workspace LLD,
no OA as-of, no full-pane TRP selection for signal-relevant ``available`` pools.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from bot.e1r_live_scanner.config import ensure_runtime_paths

SCANNER_ENTRY_TIMEFRAME = "15m"


class CommonCausalPoolsError(ValueError):
    """Invalid pane, decision time, or preconditions for common pool resolution."""


def _ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


@dataclass(frozen=True)
class CommonCausalPoolsResultV1:
    """Signal-relevant pools at a 15m (or other TF) bar close."""

    symbol: str
    timeframe: str
    decision_close: datetime
    candle_open: datetime
    decision_index: int
    available: list[dict]
    ui_selected: list[dict]
    proof: dict[str, Any]
    rows_hash: str
    config_version: int
    available_pool_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "decision_close": self.decision_close.isoformat(),
            "candle_open": self.candle_open.isoformat(),
            "decision_index": self.decision_index,
            "available_count": len(self.available),
            "available_pool_ids": list(self.available_pool_ids),
            "rows_hash": self.rows_hash,
            "config_version": self.config_version,
            "proof": dict(self.proof),
        }


def common_causal_pools_from_loaded_pane(
    symbol: str,
    candles: list,
    decision_close: datetime,
    *,
    timeframe: str = SCANNER_ENTRY_TIMEFRAME,
    cache: dict[str, Any] | None = None,
    strict_complete_buckets: bool = True,
) -> CommonCausalPoolsResultV1:
    """Causal ``available`` pools on an already-loaded pane (scanner path)."""
    ensure_runtime_paths()
    from dashboard.research_charts.frozen_lld_config_v1 import (
        FROZEN_LLD_CONFIG_VERSION,
        PANE_FROM_UTC,
        frozen_lld_config_for_timeframe_v1,
    )
    from dashboard.research_charts.frozen_pool_kernel_v1 import resolve_decision_bar_index
    from dashboard.research_charts.lld_research_kernel import (
        candle_stamps,
        scanner_pools_for_index,
    )
    from dashboard.research_charts.short_pool_oracle_v1 import rows_hash

    sym = str(symbol or "").strip().upper()
    if not sym:
        raise CommonCausalPoolsError("symbol is required")
    tf = str(timeframe or "").strip()
    if not tf:
        raise CommonCausalPoolsError("timeframe is required")
    if not strict_complete_buckets:
        raise CommonCausalPoolsError(f"strict_complete_buckets=False for {sym} {tf} loaded pane")
    if not candles:
        raise CommonCausalPoolsError(f"empty candle pane for {sym} {tf}")

    decision = _ensure_utc(decision_close)
    stamps = candle_stamps(candles)
    if _ensure_utc(stamps[0]) != _ensure_utc(PANE_FROM_UTC):
        raise CommonCausalPoolsError(
            f"pane first open {stamps[0].isoformat()} != PANE_FROM {PANE_FROM_UTC.isoformat()}"
        )

    idx = resolve_decision_bar_index(stamps, decision, tf)
    lld_cfg = frozen_lld_config_for_timeframe_v1(tf)
    if not lld_cfg.enabled:
        raise CommonCausalPoolsError("frozen LLD engine is disabled")

    lld_cache = cache if cache is not None else {}
    available, ui_selected, proof = scanner_pools_for_index(
        candles, tf, idx, lld_cfg, cache=lld_cache
    )
    pool_ids = sorted(r["pool_id"] for r in available)

    return CommonCausalPoolsResultV1(
        symbol=sym,
        timeframe=tf,
        decision_close=decision,
        candle_open=_ensure_utc(stamps[idx]),
        decision_index=idx,
        available=available,
        ui_selected=ui_selected,
        proof=proof.as_dict(),
        rows_hash=rows_hash(available),
        config_version=FROZEN_LLD_CONFIG_VERSION,
        available_pool_ids=pool_ids,
    )


def common_causal_pool_universe(
    symbol: str,
    decision_close: datetime,
    *,
    timeframe: str = SCANNER_ENTRY_TIMEFRAME,
    cache: dict[str, Any] | None = None,
) -> CommonCausalPoolsResultV1:
    """Load pane through ``decision_close`` and return causal ``available`` pools."""
    ensure_runtime_paths()
    from dashboard.research_charts.frozen_lld_config_v1 import HISTORY_WEEKS, PANE_FROM_UTC
    from dashboard.research_charts.lld_research_kernel import load_pane_candles

    sym = str(symbol or "").strip().upper()
    tf = str(timeframe or "").strip()
    decision = _ensure_utc(decision_close)
    pane_from_unix = int(PANE_FROM_UTC.timestamp())
    decision_unix = int(decision.timestamp())

    packed, candles = load_pane_candles(
        sym,
        tf,
        from_unix=pane_from_unix,
        to_unix=decision_unix,
        history_weeks=HISTORY_WEEKS,
    )
    strict = bool(packed.get("strict_complete_buckets"))
    if not strict:
        raise CommonCausalPoolsError(
            f"strict_complete_buckets=False for {sym} {tf} pane "
            f"(from={PANE_FROM_UTC.isoformat()}, to={decision.isoformat()})"
        )
    return common_causal_pools_from_loaded_pane(
        sym,
        candles,
        decision,
        timeframe=tf,
        cache=cache,
        strict_complete_buckets=strict,
    )
