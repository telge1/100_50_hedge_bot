"""Explicit symbol + pipeline_line for landscape viewer (no silent DOGE fallback)."""

from __future__ import annotations

from research_charts.historical_lmv2_pd_contract_v1 import (
    ALLOWED_VIEWER_SCOPES,
    HISTORICAL_SYMBOL,
    HistoricalPdConfig,
    resolve_historical_pd_config,
    resolve_viewer_historical_scope,
)
from research_charts.historical_pool_state_v1 import HistoricalPoolStateError
from research_charts.multicoin_pd1_scope_v1 import (
    assert_symbol_allowed,
    resolve_multicoin_pd1_scope,
)


def resolve_landscape_viewer_config(
    *,
    symbol: str,
    timeframe: str = "5m",
    pool_detail: int = 1,
    pipeline_line_id: str | None = None,
) -> HistoricalPdConfig:
    sym = assert_symbol_allowed(symbol) if symbol.upper() != HISTORICAL_SYMBOL else HISTORICAL_SYMBOL
    tf = str(timeframe).strip()
    pd = int(pool_detail)
    if (tf, pd) not in ALLOWED_VIEWER_SCOPES:
        raise HistoricalPoolStateError(
            "INVALID_SCOPE",
            "viewer scope not in release matrix",
            {"timeframe": tf, "pool_detail": pd},
        )
    if pipeline_line_id is None:
        if sym == HISTORICAL_SYMBOL:
            return resolve_viewer_historical_scope(timeframe=tf, pool_detail=pd)
        mc = resolve_multicoin_pd1_scope(sym)
        if tf != "5m" or pd != 1:
            raise HistoricalPoolStateError(
                "INVALID_SCOPE",
                "multicoin viewer prep supports 5m PD1 only",
                {"symbol": sym, "timeframe": tf, "pool_detail": pd},
            )
        base = resolve_historical_pd_config(1, timeframe="5m")
        return HistoricalPdConfig(
            pool_detail=pd,
            symbol=sym,
            timeframe=tf,
            pipeline_line_id=mc.pipeline_line_id,
            config_hash=base.config_hash,
        )
    line = str(pipeline_line_id).strip()
    base = resolve_historical_pd_config(pd, timeframe=tf)
    return HistoricalPdConfig(
        pool_detail=pd,
        symbol=sym.upper(),
        timeframe=tf,
        pipeline_line_id=line,
        config_hash=base.config_hash,
    )
