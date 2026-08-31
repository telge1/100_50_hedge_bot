"""Read-only loaders for pools, 1s walls, and 1s trades."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from orderbook_analyse.canonical_pool_wall_trade_reaction_v1.contracts import (
    EXPECTED_STRUCTURAL_BUNDLE,
    EXPECTED_STRUCTURAL_SPEC,
    FEATURES_TABLE,
    STRUCT_ROOT,
    SYMBOL,
    TRADES_TABLE,
)
from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client, load_clickhouse_settings

QSET = {"max_execution_time": 300, "receive_timeout": 320}


def _utc(s: str | datetime | pd.Timestamp) -> datetime:
    if isinstance(s, pd.Timestamp):
        s = s.to_pydatetime()
    if isinstance(s, str):
        s = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if s.tzinfo is None:
        return s.replace(tzinfo=timezone.utc)
    return s.astimezone(timezone.utc)


def verify_structural_freeze() -> dict[str, Any]:
    freeze = json.loads((STRUCT_ROOT / "freeze_manifest.json").read_text())
    ok = (
        freeze.get("structural_analysis_spec_sha256") == EXPECTED_STRUCTURAL_SPEC
        and freeze.get("structural_class_bundle_sha256") == EXPECTED_STRUCTURAL_BUNDLE
    )
    if not ok:
        raise RuntimeError(f"structural_freeze_mismatch:{freeze}")
    return freeze


def load_pool_episodes() -> pd.DataFrame:
    ep = pd.read_parquet(STRUCT_ROOT / "raw_pool_episodes.parquet")
    ep["first_seen"] = pd.to_datetime(ep["first_seen"], utc=True)
    ep["last_seen"] = pd.to_datetime(ep["last_seen"], utc=True)
    ep["lower"] = ep["lower"].astype(float)
    ep["upper"] = ep["upper"].astype(float)
    # episode_id == pool_id in this freeze
    ep = ep.rename(columns={"episode_id": "pool_id"})
    return ep


def load_first_seen_tags() -> pd.DataFrame:
    """One tag row per pool at first_seen snapshot (if present)."""
    tags = pd.read_parquet(STRUCT_ROOT / "pool_class_tags.parquet")
    tags["snapshot_ts"] = pd.to_datetime(tags["snapshot_ts"], utc=True)
    # keep first snapshot per pool_id
    tags = tags.sort_values("snapshot_ts").groupby("pool_id", as_index=False).first()
    tags["class_tags_str"] = tags["class_tags"].apply(
        lambda x: "|".join(sorted(x)) if hasattr(x, "__iter__") and not isinstance(x, str) else str(x)
    )
    return tags[["pool_id", "class_tags_str", "snapshot_ts"]].rename(
        columns={"snapshot_ts": "tag_snapshot_ts"}
    )


def _client():
    load_clickhouse_settings()
    return get_clickhouse_client()


def fetch_features_day(client, day: datetime) -> pd.DataFrame:
    start = _utc(day).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    rows = client.query(
        f"""
        SELECT
          bucket_start,
          toFloat64(mid_price) AS mid_price,
          toFloat64(bid_wall_price) AS bid_wall_price,
          toFloat64(bid_wall_qty) AS bid_wall_qty,
          toFloat64(bid_wall_notional) AS bid_wall_notional,
          toFloat64(ask_wall_price) AS ask_wall_price,
          toFloat64(ask_wall_qty) AS ask_wall_qty,
          toFloat64(ask_wall_notional) AS ask_wall_notional
        FROM {FEATURES_TABLE} FINAL
        WHERE symbol = {{s:String}}
          AND depth = 200
          AND parser_version = 'ob200_v3'
          AND is_valid = 1
          AND bucket_start >= {{a:DateTime64(3,'UTC')}}
          AND bucket_start < {{b:DateTime64(3,'UTC')}}
        ORDER BY bucket_start
        """,
        parameters={"s": SYMBOL, "a": start, "b": end},
        settings=QSET,
    ).result_rows
    df = pd.DataFrame(
        rows,
        columns=[
            "bucket_start",
            "mid_price",
            "bid_wall_price",
            "bid_wall_qty",
            "bid_wall_notional",
            "ask_wall_price",
            "ask_wall_qty",
            "ask_wall_notional",
        ],
    )
    if not df.empty:
        df["bucket_start"] = pd.to_datetime(df["bucket_start"], utc=True)
        for c in df.columns[1:]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def fetch_trades_1s_day(client, day: datetime) -> pd.DataFrame:
    start = _utc(day).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    rows = client.query(
        f"""
        SELECT
          toStartOfSecond(trade_ts) AS second,
          sum(if(side = 'Buy', toFloat64(size) * toFloat64(price), 0.)) AS buy_notional,
          sum(if(side = 'Sell', toFloat64(size) * toFloat64(price), 0.)) AS sell_notional,
          sum(if(side = 'Buy', toFloat64(size), 0.)) AS buy_qty,
          sum(if(side = 'Sell', toFloat64(size), 0.)) AS sell_qty,
          count() AS trade_count
        FROM {TRADES_TABLE}
        WHERE symbol = {{s:String}}
          AND trade_ts >= {{a:DateTime64(3,'UTC')}}
          AND trade_ts < {{b:DateTime64(3,'UTC')}}
        GROUP BY second
        ORDER BY second
        """,
        parameters={"s": SYMBOL, "a": start, "b": end},
        settings=QSET,
    ).result_rows
    df = pd.DataFrame(
        rows,
        columns=["second", "buy_notional", "sell_notional", "buy_qty", "sell_qty", "trade_count"],
    )
    if not df.empty:
        df["second"] = pd.to_datetime(df["second"], utc=True)
        for c in df.columns[1:]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def load_market_window(start: datetime, end: datetime) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Load features + 1s trades day-by-day for [start, end]."""
    client = _client()
    start = _utc(start)
    end = _utc(end)
    days = pd.date_range(start.date(), end.date(), freq="D", tz="UTC")
    feat_parts: list[pd.DataFrame] = []
    trade_parts: list[pd.DataFrame] = []
    day_stats: list[dict[str, Any]] = []
    for d in days:
        f = fetch_features_day(client, d.to_pydatetime())
        t = fetch_trades_1s_day(client, d.to_pydatetime())
        feat_parts.append(f)
        trade_parts.append(t)
        day_stats.append(
            {
                "day": d.strftime("%Y-%m-%d"),
                "feature_rows": int(len(f)),
                "trade_seconds": int(len(t)),
                "feature_coverage_pct": round(100.0 * len(f) / 86400.0, 2),
            }
        )
        print(f"  loaded {d.date()} features={len(f)} trade_secs={len(t)}", flush=True)
    feat = pd.concat(feat_parts, ignore_index=True) if feat_parts else pd.DataFrame()
    trades = pd.concat(trade_parts, ignore_index=True) if trade_parts else pd.DataFrame()
    if not feat.empty:
        feat = feat[(feat["bucket_start"] >= start) & (feat["bucket_start"] <= end)]
        feat = feat.sort_values("bucket_start").drop_duplicates("bucket_start")
        feat = feat.set_index("bucket_start", drop=False)
    if not trades.empty:
        trades = trades[(trades["second"] >= start) & (trades["second"] <= end)]
        trades = trades.sort_values("second").drop_duplicates("second")
        trades = trades.set_index("second", drop=False)
    meta = {
        "symbol": SYMBOL,
        "features_table": FEATURES_TABLE,
        "trades_table": TRADES_TABLE,
        "start": start.isoformat().replace("+00:00", "Z"),
        "end": end.isoformat().replace("+00:00", "Z"),
        "feature_rows": int(len(feat)),
        "trade_seconds": int(len(trades)),
        "days": day_stats,
    }
    return feat, trades, meta
