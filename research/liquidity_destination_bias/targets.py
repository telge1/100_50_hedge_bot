"""Causal LLD pool target selection.

The canonical pool provider is imported from orderbook_analyse. No pool
geometry formula is duplicated here.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from .contract import (
    MIN_TARGET_PERSISTENCE_SECONDS,
    TARGET_CONTRACT_VERSION,
    TARGET_SOURCE,
    TARGET_TIMEFRAME,
)
from .models import FrozenTarget, utc

OA_SRC = Path("/home/telgenbuescher/projects/orderbook_analyse/src")
SR_DASHBOARD = Path(__file__).resolve().parents[2] / "dashboard"


class PoolSnapshotProvider(Protocol):
    def snapshot(self, symbol: str, t0: datetime) -> dict[str, Any]: ...


class CanonicalLldPoolProvider:
    """Thin adapter over the existing canonical chart LLD provider."""

    def __init__(self, timeframe: str = TARGET_TIMEFRAME) -> None:
        self.timeframe = timeframe

    def snapshot(self, symbol: str, t0: datetime) -> dict[str, Any]:
        for path in (OA_SRC, SR_DASHBOARD):
            text = str(path)
            if text not in sys.path:
                sys.path.insert(0, text)
        from orderbook_analyse.liquidity_pool_signal.chart_pool_adapter import (
            export_snapshot,
        )

        t0_u = utc(t0)
        return export_snapshot(
            symbol=symbol,
            timeframe=self.timeframe,
            window_start=t0_u,
            as_of=t0_u,
        )


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def select_frozen_targets(
    snapshot: dict[str, Any],
    *,
    price_t0: float,
    t0: datetime,
    min_persistence_seconds: int = MIN_TARGET_PERSISTENCE_SECONDS,
) -> tuple[FrozenTarget | None, FrozenTarget | None]:
    """Select nearest eligible ASK above and BID below using T0-known pools."""
    t0_u = utc(t0)
    cutoff = t0_u - timedelta(seconds=min_persistence_seconds)
    rows = snapshot.get("active_canonical_pools") or snapshot.get("active_pools") or []
    known: list[dict[str, Any]] = []
    for row in rows:
        available_raw = row.get("available_at")
        if not available_raw:
            continue
        available = utc(_parse_time(str(available_raw)))
        if available > cutoff:
            continue
        if not bool(row.get("active_as_of", True)):
            continue
        lower = float(row.get("lower", row.get("lower_edge")))
        upper = float(row.get("upper", row.get("upper_edge")))
        if lower > upper:
            continue
        known.append({**row, "_available": available, "_lower": lower, "_upper": upper})

    asks = [r for r in known if str(r.get("side")).upper() == "ASK" and r["_lower"] > price_t0]
    bids = [r for r in known if str(r.get("side")).upper() == "BID" and r["_upper"] < price_t0]
    ask = min(asks, key=lambda r: (r["_lower"], str(r.get("pool_id")))) if asks else None
    bid = max(bids, key=lambda r: (r["_upper"], str(r.get("pool_id")))) if bids else None

    def freeze(row: dict[str, Any] | None, side: str) -> FrozenTarget | None:
        if row is None:
            return None
        lower = row["_lower"]
        upper = row["_upper"]
        return FrozenTarget(
            target_id=str(row.get("pool_id")),
            side=side,
            lower_price=lower,
            upper_price=upper,
            touch_price=lower if side == "ASK" else upper,
            available_at=row["_available"],
            source=TARGET_SOURCE,
            contract_version=str(
                snapshot.get("canonical_provider_version") or TARGET_CONTRACT_VERSION
            ),
        )

    return freeze(ask, "ASK"), freeze(bid, "BID")
