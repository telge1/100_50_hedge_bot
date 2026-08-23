"""Market data loaders for --enrich only. Must not be imported by dry-run/analyze paths."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from . import constants as C
from .causality import as_utc


def fetch_ob_1s_causal(client: Any, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    """1s OB buckets for enrichment; only documented schema columns."""
    from .....cluster_sweep_research.clickhouse_source import _as_utc, _q

    rows = _q(
        client,
        """
        SELECT
          bucket_start,
          last_source_ts,
          is_valid,
          imbalance_l10,
          imbalance_l50,
          spread_bps,
          bid_qty_l50,
          ask_qty_l50
        FROM orderbook_analysis.orderbook_features_1s_v2 FINAL
        WHERE symbol={s:String}
          AND parser_version={p:String} AND depth={d:UInt16}
          AND bucket_start>={a:DateTime64(3,'UTC')} AND bucket_start<={b:DateTime64(3,'UTC')}
        ORDER BY bucket_start
        """,
        {
            "s": symbol,
            "p": C.OB_PARSER,
            "d": C.OB_DEPTH,
            "a": _as_utc(start),
            "b": _as_utc(end),
        },
    )
    cols = [
        "bucket_start",
        "last_source_ts",
        "is_valid",
        "imbalance_l10",
        "imbalance_l50",
        "spread_bps",
        "bid_qty_l50",
        "ask_qty_l50",
    ]
    df = pd.DataFrame(rows, columns=cols)
    if not df.empty:
        df["bucket_start"] = pd.to_datetime(df["bucket_start"], utc=True)
        df["last_source_ts"] = pd.to_datetime(df["last_source_ts"], utc=True)
    return df


def load_enrichment_market_data(client: Any, symbol: str, start: datetime, end: datetime) -> dict[str, Any]:
    """Load candles/trades/OB/OI/liq for enrichment. Call only from --enrich."""
    from .....cluster_sweep_research.clickhouse_source import (
        aggregate_timeframe,
        fetch_candles_1m,
        fetch_liquidations,
        fetch_oi_1m,
        fetch_trades_1m,
    )

    start_u, end_u = as_utc(start), as_utc(end)
    warm = timedelta(days=7)
    pad = timedelta(hours=2)
    c1m = fetch_candles_1m(client, symbol, start_u - warm, end_u + pad)
    c5 = aggregate_timeframe(c1m, "5m") if c1m is not None and not c1m.empty else pd.DataFrame()
    trades = fetch_trades_1m(client, symbol, start_u - pad, end_u + pad)
    ob = fetch_ob_1s_causal(client, symbol, start_u - pad, end_u + pad)
    try:
        oi = fetch_oi_1m(client, symbol, start_u - pad, end_u + pad)
    except Exception:
        oi = pd.DataFrame()
    try:
        liq = fetch_liquidations(client, symbol, start_u - pad, end_u + pad)
    except Exception:
        liq = pd.DataFrame()
    return {
        "candles_1m": c1m,
        "candles_5m": c5,
        "trades": trades,
        "ob_1s": ob,
        "oi_1m": oi,
        "liq": liq,
    }



def open_clickhouse_client() -> Any:
    from .....cluster_sweep_research.clickhouse_source import default_client

    return default_client()
