"""Read-only coverage probes and market data loaders (aggregate OB proxy)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client

QSET = {"max_execution_time": 180, "receive_timeout": 200}


def _utc(dt: datetime | pd.Timestamp) -> datetime:
    if isinstance(dt, pd.Timestamp):
        dt = dt.to_pydatetime()
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _q(client, sql: str, params: dict | None = None):
    return client.query(sql, parameters=params or {}, settings=QSET).result_rows


def fetch_ob_agg_1s(client, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    """Aggregate OB200 proxy — NOT genuine per-level L2."""
    rows = _q(
        client,
        """
        SELECT
          bucket_start, is_valid,
          toFloat64(best_bid_price) AS best_bid_price,
          toFloat64(best_ask_price) AS best_ask_price,
          toFloat64(mid_price) AS mid_price,
          toFloat64(spread_bps) AS spread_bps,
          toFloat64(bid_qty_l50) AS bid_qty_l50,
          toFloat64(ask_qty_l50) AS ask_qty_l50,
          toFloat64(imbalance_l50) AS imbalance_l50,
          toFloat64(bid_qty_bps25) AS bid_qty_bps25,
          toFloat64(ask_qty_bps25) AS ask_qty_bps25,
          toFloat64(bid_wall_price) AS bid_wall_price,
          toFloat64(ask_wall_price) AS ask_wall_price,
          toFloat64(bid_wall_qty) AS bid_wall_qty,
          toFloat64(ask_wall_qty) AS ask_wall_qty,
          toFloat64(bid_wall_bps_dist) AS bid_wall_bps_dist,
          toFloat64(ask_wall_bps_dist) AS ask_wall_bps_dist,
          toFloat64(bid_qty_added) AS bid_qty_added,
          toFloat64(bid_qty_removed) AS bid_qty_removed,
          toFloat64(ask_qty_added) AS ask_qty_added,
          toFloat64(ask_qty_removed) AS ask_qty_removed,
          toFloat64(ofi) AS ofi,
          toFloat64(mid_price_change) AS mid_price_change
        FROM orderbook_analysis.orderbook_features_1s_v2 FINAL
        WHERE symbol={s:String} AND depth=200 AND parser_version='ob200_v3'
          AND bucket_start>={a:DateTime64(3,'UTC')} AND bucket_start<{b:DateTime64(3,'UTC')}
        ORDER BY bucket_start
        """,
        {"s": symbol, "a": _utc(start), "b": _utc(end)},
    )
    cols = [
        "bucket_start",
        "is_valid",
        "best_bid_price",
        "best_ask_price",
        "mid_price",
        "spread_bps",
        "bid_qty_l50",
        "ask_qty_l50",
        "imbalance_l50",
        "bid_qty_bps25",
        "ask_qty_bps25",
        "bid_wall_price",
        "ask_wall_price",
        "bid_wall_qty",
        "ask_wall_qty",
        "bid_wall_bps_dist",
        "ask_wall_bps_dist",
        "bid_qty_added",
        "bid_qty_removed",
        "ask_qty_added",
        "ask_qty_removed",
        "ofi",
        "mid_price_change",
    ]
    df = pd.DataFrame(rows, columns=cols)
    if not df.empty:
        df["bucket_start"] = pd.to_datetime(df["bucket_start"])
        for c in cols[2:]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df.attrs["source_kind"] = "AGGREGATE_PROXY"
    df.attrs["source_table"] = "orderbook_analysis.orderbook_features_1s_v2"
    return df


def fetch_trades_1s(client, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    rows = _q(
        client,
        """
        SELECT
          toStartOfSecond(trade_ts) AS second,
          count() AS trade_count,
          sum(if(side='Buy', toFloat64(size)*toFloat64(price), 0.)) AS buy_notional,
          sum(if(side='Sell', toFloat64(size)*toFloat64(price), 0.)) AS sell_notional,
          sum(if(side='Buy', toFloat64(size), 0.)) AS buy_qty,
          sum(if(side='Sell', toFloat64(size), 0.)) AS sell_qty,
          avg(toFloat64(size)*toFloat64(price)) AS avg_trade_notional,
          quantile(0.95)(toFloat64(size)*toFloat64(price)) AS p95_trade_notional
        FROM orderbook_analysis.public_trades_canonical
        WHERE symbol={s:String}
          AND trade_ts>={a:DateTime64(3,'UTC')} AND trade_ts<{b:DateTime64(3,'UTC')}
        GROUP BY second
        ORDER BY second
        """,
        {"s": symbol, "a": _utc(start), "b": _utc(end)},
    )
    df = pd.DataFrame(
        rows,
        columns=[
            "second",
            "trade_count",
            "buy_notional",
            "sell_notional",
            "buy_qty",
            "sell_qty",
            "avg_trade_notional",
            "p95_trade_notional",
        ],
    )
    if not df.empty:
        df["second"] = pd.to_datetime(df["second"])
        for c in df.columns[1:]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["delta_notional"] = df["buy_notional"] - df["sell_notional"]
    return df


def fetch_oi_5s(client, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    rows = _q(
        client,
        """
        SELECT bucket_time, toFloat64(open_interest) AS open_interest,
               toFloat64(open_interest_value) AS open_interest_value
        FROM orderbook_analysis.open_interest_5s
        WHERE symbol={s:String}
          AND bucket_time>={a:DateTime64(3,'UTC')} AND bucket_time<{b:DateTime64(3,'UTC')}
        ORDER BY bucket_time
        """,
        {"s": symbol, "a": _utc(start), "b": _utc(end)},
    )
    df = pd.DataFrame(rows, columns=["bucket_time", "open_interest", "open_interest_value"])
    if not df.empty:
        df["bucket_time"] = pd.to_datetime(df["bucket_time"])
    return df


def fetch_liquidations(client, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    rows = _q(
        client,
        """
        SELECT event_time, liquidated_position_side AS side,
               toFloat64(notional_estimate) AS notional
        FROM orderbook_analysis.all_liquidations
        WHERE symbol={s:String}
          AND event_time>={a:DateTime64(3,'UTC')} AND event_time<{b:DateTime64(3,'UTC')}
        ORDER BY event_time
        """,
        {"s": symbol, "a": _utc(start), "b": _utc(end)},
    )
    df = pd.DataFrame(rows, columns=["event_time", "side", "notional"])
    if not df.empty:
        df["event_time"] = pd.to_datetime(df["event_time"])
        df["notional"] = pd.to_numeric(df["notional"], errors="coerce")
    return df


def fetch_candles_1m(client, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    rows = _q(
        client,
        """
        SELECT open_time, open, high, low, close, volume
        FROM signal_generator.candles_1m FINAL
        WHERE symbol={s:String} AND interval='1m'
          AND open_time>={a:DateTime64(3,'UTC')} AND open_time<{b:DateTime64(3,'UTC')}
        ORDER BY open_time
        """,
        {"s": symbol, "a": _utc(start), "b": _utc(end)},
    )
    df = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume"])
    if not df.empty:
        df["open_time"] = pd.to_datetime(df["open_time"])
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def probe_raw_ob200_available(client) -> dict[str, Any]:
    """Detect whether genuine per-level OB200 is usable (expect blocked)."""
    out = {
        "per_level_raw_available": False,
        "reason": None,
        "aggregate_proxy_table": "orderbook_analysis.orderbook_features_1s_v2",
        "aggregate_proxy_available": False,
    }
    try:
        n = _q(
            client,
            """
            SELECT count() FROM orderbook_analysis.orderbook_features_1s_v2
            WHERE symbol='BTCUSDT' AND bucket_start>=now()-INTERVAL 1 DAY
            LIMIT 1
            """,
        )[0][0]
        out["aggregate_proxy_available"] = int(n) >= 0
    except Exception as exc:  # noqa: BLE001
        out["aggregate_proxy_available"] = False
        out["reason"] = f"aggregate_probe:{exc}"
    # raw deltas known broken on this host
    try:
        _q(client, "SELECT 1 FROM orderbook_analysis.orderbook_deltas LIMIT 1")
        out["per_level_raw_available"] = True
    except Exception as exc:  # noqa: BLE001
        out["per_level_raw_available"] = False
        out["reason"] = f"orderbook_deltas_unavailable:{type(exc).__name__}"
    return out


def coverage_for_episode(
    *,
    symbol: str,
    t0: pd.Timestamp,
    t1: pd.Timestamp | None,
    t2: pd.Timestamp | None,
    ob: pd.DataFrame,
    trades: pd.DataFrame,
    oi: pd.DataFrame,
    liq: pd.DataFrame,
    candles: pd.DataFrame,
) -> dict[str, Any]:
    """Coverage summary around checkpoints (missing ≠ zero)."""

    def _win_cov(df: pd.DataFrame, tcol: str, a: pd.Timestamp, b: pd.Timestamp, expected_sec: float | None = None):
        if df is None or df.empty or pd.isna(a) or pd.isna(b) or b <= a:
            return {"status": "MISSING", "n": 0, "gap_frac": None}
        sl = df[(df[tcol] >= a) & (df[tcol] < b)]
        n = len(sl)
        if n == 0:
            return {"status": "MISSING", "n": 0, "gap_frac": 1.0}
        gap = None
        if expected_sec is not None and expected_sec > 0:
            gap = max(0.0, 1.0 - n / expected_sec)
        status = "VALID" if (gap is None or gap <= 0.25) else ("PARTIAL" if gap <= 0.6 else "SPARSE")
        return {"status": status, "n": n, "gap_frac": gap}

    pre_a = t0 - pd.Timedelta(minutes=5)
    pre_b = t1 if pd.notna(t1) else t0
    touch_a = t2 - pd.Timedelta(seconds=5) if pd.notna(t2) else t0
    touch_b = t2 + pd.Timedelta(seconds=5) if pd.notna(t2) else t0 + pd.Timedelta(seconds=5)
    post_a = t2 if pd.notna(t2) else t0
    post_b = post_a + pd.Timedelta(seconds=60)

    cov = {
        "ob_source_kind": "AGGREGATE_PROXY",
        "ob_per_level_raw": False,
        "pre_approach": {
            "ob": _win_cov(ob, "bucket_start", pre_a, pre_b, (pre_b - pre_a).total_seconds()),
            "trades": _win_cov(trades, "second", pre_a, pre_b, (pre_b - pre_a).total_seconds()),
            "oi": _win_cov(oi, "bucket_time", pre_a, pre_b, None),
            "liq": _win_cov(liq, "event_time", pre_a, pre_b, None),
            "candles_1m": _win_cov(candles, "open_time", pre_a, pre_b, None),
        },
        "at_first_touch": {
            "ob": _win_cov(ob, "bucket_start", touch_a, touch_b, 10),
            "trades": _win_cov(trades, "second", touch_a, touch_b, 10),
            "oi": _win_cov(oi, "bucket_time", touch_a, touch_b, None),
            "liq": _win_cov(liq, "event_time", touch_a, touch_b, None),
        },
        "post_touch_60s": {
            "ob": _win_cov(ob, "bucket_start", post_a, post_b, 60),
            "trades": _win_cov(trades, "second", post_a, post_b, 60),
            "oi": _win_cov(oi, "bucket_time", post_a, post_b, None),
            "liq": _win_cov(liq, "event_time", post_a, post_b, None),
            "candles_1m": _win_cov(candles, "open_time", post_a, post_b + pd.Timedelta(minutes=3), None),
        },
    }
    # analyzability: need trades+ob+candles around touch; OI/liq optional
    need = [
        cov["at_first_touch"]["ob"]["status"] in {"VALID", "PARTIAL"},
        cov["at_first_touch"]["trades"]["status"] in {"VALID", "PARTIAL"},
        cov["post_touch_60s"]["ob"]["status"] in {"VALID", "PARTIAL", "SPARSE"},
        cov["post_touch_60s"]["trades"]["status"] in {"VALID", "PARTIAL", "SPARSE"},
    ]
    cov["analyzable_core"] = all(need)
    cov["oi_available"] = cov["at_first_touch"]["oi"]["status"] != "MISSING" or cov["pre_approach"]["oi"]["status"] != "MISSING"
    cov["liq_available"] = cov["at_first_touch"]["liq"]["n"] > 0 or cov["post_touch_60s"]["liq"]["n"] > 0
    # liq empty in window is EMPTY_SLICE not zero market
    if cov["at_first_touch"]["liq"]["n"] == 0 and cov["post_touch_60s"]["liq"]["n"] == 0:
        cov["liq_note"] = "EMPTY_TABLE_SLICE_IN_WINDOW"
    return cov
