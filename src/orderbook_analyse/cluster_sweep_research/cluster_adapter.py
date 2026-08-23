"""Thin adapter to trading_research_platform Liquidity Location (no reimplementation)."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from .models import ClusterSnapshot

DEFAULT_TRP = Path("/home/telgenbuescher/projects/trading_research_platform")

# Pre-implementation audit answers (frozen documentation for this module)
LLD_AUDIT: dict[str, Any] = {
    "source_repo": str(DEFAULT_TRP),
    "engine_file": "indicators/liquidity_location/engine.py",
    "cluster_file": "indicators/liquidity_location/clusters.py",
    "orderbook_analyse_wrapper": "src/orderbook_analyse/market_event_report/lld_context.py",
    "inputs": "OHLCV candles only (volume for strength); no OB/trades required for pools",
    "bullish_zones": "side=lower: swing low confirmed; box [low-half, low]",
    "bearish_zones": "side=upper: swing high confirmed; box [high, high+half]",
    "chart_number": "cluster.pool_count (pools merged); display min usually 3",
    "strength": "norm_vol of source candle (Pine volume_ = norm_vol[1]), capped 0–10",
    "zone_begin": "created_timestamp = confirmation candle i (source = i-1)",
    "zone_end_invalidation": "upper: high > top; lower: low < bottom (strict)",
    "overlap_merge": "pools not merged; clusters aggregate overlapping/near pools via union-find",
    "causal": True,
    "repaint": False,
    "notes": (
        "Engine documented as causal Pine parity. Clusters support as_of snapshots so "
        "later pools do not inflate earlier clusters. amount=300 is display prune only."
    ),
}


class CausalVerdict(str, Enum):
    CAUSAL_REUSABLE = "CAUSAL_REUSABLE"
    BLOCKED_NON_CAUSAL = "BLOCKED_NON_CAUSAL"
    UNAVAILABLE = "UNAVAILABLE"


def ensure_trp_path(trp_root: Path | None = None) -> Path:
    root = Path(trp_root) if trp_root is not None else DEFAULT_TRP
    if not root.exists():
        raise FileNotFoundError(f"TRP root missing: {root}")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return root


def dataframe_to_trp_candles(df: pd.DataFrame, *, symbol: str, timeframe: str):
    """Convert OHLCV frame (open_time, open, high, low, close, volume) to TRP Candle list."""
    ensure_trp_path()
    from data.models import Candle

    out = []
    for r in df.itertuples(index=False):
        ts = pd.Timestamp(r.open_time)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        out.append(
            Candle(
                timestamp=ts.to_pydatetime(),
                open=float(r.open),
                high=float(r.high),
                low=float(r.low),
                close=float(r.close),
                volume=float(getattr(r, "volume", 0.0) or 0.0),
                symbol=symbol,
                timeframe=timeframe,
            )
        )
    return out


@dataclass
class LldRunResult:
    verdict: CausalVerdict
    pools: list[Any]
    reason: str | None = None
    metadata: dict[str, Any] | None = None


def run_lld_pools(
    candles_df: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    amount: int = 300,
    highest_len: int = 2,
    lowest_len: int = 2,
    trp_root: Path | None = None,
) -> LldRunResult:
    """Run existing LLD engine; do not invent alternate pool geometry."""
    try:
        ensure_trp_path(trp_root)
        from indicators.liquidity_location import LiquidityLocationConfig
        from indicators.liquidity_location.engine import run_liquidity_location
    except Exception as exc:  # noqa: BLE001
        return LldRunResult(CausalVerdict.UNAVAILABLE, [], reason=f"import:{exc}")

    if candles_df.empty:
        return LldRunResult(CausalVerdict.UNAVAILABLE, [], reason="empty_candles")

    try:
        candles = dataframe_to_trp_candles(candles_df, symbol=symbol, timeframe=timeframe)
        result = run_liquidity_location(
            candles,
            LiquidityLocationConfig(
                enabled=True,
                amount=amount,
                highest_len=highest_len,
                lowest_len=lowest_len,
                clusters_enabled=True,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return LldRunResult(CausalVerdict.UNAVAILABLE, [], reason=f"engine:{exc}")

    return LldRunResult(
        CausalVerdict.CAUSAL_REUSABLE,
        list(result.pools_all),
        reason=None,
        metadata=dict(result.metadata or {}),
    )


def active_clusters_as_of(
    pools: Sequence[Any],
    as_of: datetime,
    *,
    gap_pct: float = 0.10,
    minimum_pools: int = 3,
    trp_root: Path | None = None,
) -> list[ClusterSnapshot]:
    """Causal cluster snapshot at as_of using TRP cluster_pools(..., as_of=T)."""
    ensure_trp_path(trp_root)
    from indicators.liquidity_location.clusters import cluster_pools, filter_clusters

    t = as_of
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    raw = cluster_pools(pools, gap_pct=gap_pct, as_of=t, active_only=False)
    filtered = filter_clusters(raw, minimum_pools=minimum_pools)
    out: list[ClusterSnapshot] = []
    for c in filtered:
        out.append(
            ClusterSnapshot(
                cluster_id=c.cluster_id,
                side=c.side,
                low=float(c.cluster_low),
                high=float(c.cluster_high),
                mid=float(c.cluster_mid),
                width_abs=float(c.cluster_width_abs),
                width_pct=None if c.cluster_width_pct is None else float(c.cluster_width_pct),
                pool_count=int(c.pool_count),
                strength_sum=None if c.strength_sum is None else float(c.strength_sum),
                strength_mean=None if c.strength_mean is None else float(c.strength_mean),
                strength_max=None if c.strength_max is None else float(c.strength_max),
                oldest_created=c.oldest_created_timestamp,
                newest_created=c.newest_created_timestamp,
                pool_ids=tuple(c.pool_ids),
            )
        )
    return out
