"""Frozen pool kernel v1 — deterministic ``available`` at a decision bar close.

Not wired into live Short/Long/Chart consumers (Phase 2). Uses only
``frozen_lld_config_v1`` + existing ``load_pane_candles`` / ``scanner_pools_for_index``.

Public ``decision_timestamp`` semantics (matches Short oracle / ``bar["close_time"]``):

  **A) Decision-bar CLOSE** — not candle open.

  Index ``i`` is the unique bar where ``bar_close(candle_open[i], timeframe) == decision_timestamp``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .frozen_lld_config_v1 import (
    FROZEN_LLD_CONFIG_VERSION,
    HISTORY_WEEKS,
    PANE_FROM_UTC,
    SCANNER_ENTRY_TIMEFRAME,
    frozen_lld_config_for_timeframe_v1,
)
from .lld_research_kernel import (
    candle_stamps,
    load_pane_candles,
    scanner_pools_for_index,
)
from .short_pool_oracle_v1 import rows_hash
from pool_scan.clock import bar_close


class FrozenPoolKernelError(ValueError):
    """Invalid inputs or data preconditions for the frozen pool kernel."""


def _ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def resolve_decision_bar_index(
    stamps: list[datetime],
    decision_timestamp: datetime,
    timeframe: str,
) -> int:
    """Map decision **bar close** to candle index; raise if no exact match."""
    decision = _ensure_utc(decision_timestamp)
    matches = [
        i for i, open_ts in enumerate(stamps) if bar_close(open_ts, timeframe) == decision
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise FrozenPoolKernelError(
            f"decision_timestamp {decision.isoformat()} is not a {timeframe} bar close "
            f"on this pane (first={stamps[0].isoformat() if stamps else None}, "
            f"last_open={stamps[-1].isoformat() if stamps else None})"
        )
    raise FrozenPoolKernelError(
        f"decision_timestamp {decision.isoformat()} matches multiple {timeframe} bars"
    )


@dataclass(frozen=True)
class FrozenPoolKernelResultV1:
    symbol: str
    timeframe: str
    decision_timestamp: datetime
    candle_open_timestamp: datetime
    candle_index: int
    pane_first_timestamp: datetime
    pane_last_timestamp: datetime
    available: list[dict]
    available_pool_ids: list[str]
    frozen_config_version: int
    available_rows_hash: str
    strict_complete_buckets: bool
    proof: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "decision_timestamp": self.decision_timestamp.isoformat(),
            "candle_open_timestamp": self.candle_open_timestamp.isoformat(),
            "candle_index": self.candle_index,
            "pane_first_timestamp": self.pane_first_timestamp.isoformat(),
            "pane_last_timestamp": self.pane_last_timestamp.isoformat(),
            "available_count": len(self.available_pool_ids),
            "available_pool_ids": list(self.available_pool_ids),
            "available_rows_hash": self.available_rows_hash,
            "frozen_config_version": self.frozen_config_version,
            "strict_complete_buckets": self.strict_complete_buckets,
            "proof": dict(self.proof),
        }


def available_from_loaded_pane_v1(
    symbol: str,
    candles: list,
    decision_timestamp: datetime,
    *,
    timeframe: str = SCANNER_ENTRY_TIMEFRAME,
    cache: dict[str, Any] | None = None,
    strict_complete_buckets: bool = True,
) -> FrozenPoolKernelResultV1:
    """``available`` at decision using an already-loaded pane (no ClickHouse reload)."""
    from bot.common_causal_pools_v1 import (
        CommonCausalPoolsError,
        common_causal_pools_from_loaded_pane,
    )

    sym = str(symbol or "").strip().upper()
    tf = str(timeframe or "").strip()
    try:
        common = common_causal_pools_from_loaded_pane(
            sym,
            candles,
            decision_timestamp,
            timeframe=tf,
            cache=cache,
            strict_complete_buckets=strict_complete_buckets,
        )
    except CommonCausalPoolsError as exc:
        raise FrozenPoolKernelError(str(exc)) from exc

    stamps = candle_stamps(candles)
    decision = _ensure_utc(decision_timestamp)
    return FrozenPoolKernelResultV1(
        symbol=common.symbol,
        timeframe=common.timeframe,
        decision_timestamp=decision,
        candle_open_timestamp=common.candle_open,
        candle_index=common.decision_index,
        pane_first_timestamp=_ensure_utc(stamps[0]),
        pane_last_timestamp=_ensure_utc(stamps[-1]),
        available=common.available,
        available_pool_ids=list(common.available_pool_ids),
        frozen_config_version=common.config_version,
        available_rows_hash=common.rows_hash,
        strict_complete_buckets=True,
        proof=common.proof,
    )


def available_at_decision_v1(
    symbol: str,
    decision_timestamp: datetime,
    *,
    timeframe: str = SCANNER_ENTRY_TIMEFRAME,
    cache: dict[str, Any] | None = None,
) -> FrozenPoolKernelResultV1:
    """Signal-relevant pools at ``decision_timestamp`` (15m bar **close** by default)."""
    sym = str(symbol or "").strip().upper()
    tf = str(timeframe or "").strip()
    decision = _ensure_utc(decision_timestamp)
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
        raise FrozenPoolKernelError(
            f"strict_complete_buckets=False for {sym} {tf} pane "
            f"(from={PANE_FROM_UTC.isoformat()}, to={decision.isoformat()})"
        )
    return available_from_loaded_pane_v1(
        sym,
        candles,
        decision,
        timeframe=tf,
        cache=cache,
        strict_complete_buckets=strict,
    )
