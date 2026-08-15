"""Read-only join wrapper for OI compression breakout audit."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from research.regime_scanner.liquidation_exhaustion.loader import (
    load_joined_5m,
    mark_known_outage,
    validate_symbols,
)
from research.regime_scanner.oi_compression_breakout.config import IMPORT_VERSION_DEFAULT

__all__ = [
    "coverage_report",
    "load_joined_5m",
    "mark_known_outage",
    "validate_symbols",
    "IMPORT_VERSION_DEFAULT",
    "load_frames",
]


def coverage_report(joined: pd.DataFrame) -> dict[str, Any]:
    by_symbol = []
    if not joined.empty:
        for sym, g in joined.groupby("symbol", sort=True):
            by_symbol.append(
                {
                    "symbol": str(sym),
                    "joined_rows": int(len(g)),
                    "min_bucket": str(g["bucket_start"].min()),
                    "max_bucket": str(g["bucket_start"].max()),
                    "n_sequences": int(g["sequence_id"].nunique()),
                }
            )
    return {
        "joined_rows": int(len(joined)),
        "symbols": sorted(joined["symbol"].unique().tolist()) if len(joined) else [],
        "by_symbol": by_symbol,
    }


def load_frames(
    *,
    symbols: list[str],
    start: datetime,
    end: datetime,
    import_version: str = IMPORT_VERSION_DEFAULT,
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame], dict[str, Any]]:
    """Load joined frame, split by symbol, return coverage dict."""
    joined = load_joined_5m(
        symbols=symbols,
        start=start,
        end=end,
        import_version=import_version,
    )
    cov = coverage_report(joined)
    frames: dict[str, pd.DataFrame] = {}
    if not joined.empty:
        for sym, g in joined.groupby("symbol", sort=True):
            frames[str(sym)] = g.sort_values("bucket_start").reset_index(drop=True)
    return joined, frames, cov
