"""Dashboard wrapper for orderbook_analyse canonical LLD provider."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .oa_import import ensure_oa_on_path as ensure_oa_src


def parse_liquidity_location_as_of(raw: str) -> datetime:
    """Parse dashboard as-of ISO after OA path bootstrap (no early OA import)."""
    ensure_oa_src()
    from orderbook_analyse.liquidity_pool_signal.canonical import parse_as_of_iso

    return parse_as_of_iso(raw)


def build_causal_lld_payload(
    *,
    symbol: str,
    timeframe: str,
    as_of: datetime,
    liquidity: dict[str, Any] | None,
    render_end: datetime | None = None,
) -> dict[str, Any]:
    ensure_oa_src()
    from orderbook_analyse.liquidity_pool_signal.canonical import causal_pane_lld_bundle

    return causal_pane_lld_bundle(
        symbol=str(symbol).strip().upper(),
        timeframe=str(timeframe).strip(),
        as_of=as_of,
        liquidity=liquidity,
        render_end=render_end,
    )
