"""Load F1/F2 artifacts and ClickHouse 1s aggregate rows."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from orderbook_analyse.oi_liq_impact_l2.contracts import (
    ORDERBOOK_DEPTH,
    ORDERBOOK_GENUINE_SQL,
    ORDERBOOK_PARSER_VERSION,
    ORDERBOOK_TABLE,
)
from orderbook_analyse.oi_liq_impact_l2.wall_absorption.clusters import FlushCluster

_SETTINGS = {"max_execution_time": 600, "receive_timeout": 620}

OB_1S_COLUMNS = (
    "bucket_start",
    "quality_flags",
    "is_valid",
    "best_bid_price",
    "best_bid_qty",
    "best_ask_price",
    "best_ask_qty",
    "mid_price",
    "microprice",
    "spread_bps",
    "bid_wall_price",
    "bid_wall_qty",
    "bid_wall_bps_dist",
    "ask_wall_price",
    "ask_wall_qty",
    "ask_wall_bps_dist",
    "bid_qty_l5",
    "ask_qty_l5",
    "imbalance_l5",
    "bid_qty_l10",
    "ask_qty_l10",
    "imbalance_l10",
    "bid_qty_l25",
    "ask_qty_l25",
    "imbalance_l25",
    "bid_qty_l50",
    "ask_qty_l50",
    "imbalance_l50",
    "bid_qty_bps5",
    "ask_qty_bps5",
    "imbalance_bps5",
    "bid_qty_bps10",
    "ask_qty_bps10",
    "imbalance_bps10",
    "bid_qty_bps25",
    "ask_qty_bps25",
    "imbalance_bps25",
    "bid_qty_bps50",
    "ask_qty_bps50",
    "imbalance_bps50",
    "ofi",
    "bid_qty_added",
    "bid_qty_removed",
    "ask_qty_added",
    "ask_qty_removed",
    "processed_updates",
    "last_update_seq",
)


class AggregateProxyError(RuntimeError):
    pass


@dataclass(frozen=True)
class LoadedInputs:
    f1_dir: Path
    f2_dir: Path | None
    manifest: dict[str, Any]
    minute_features: pd.DataFrame
    flush_candidates: pd.DataFrame
    clusters: list[FlushCluster]
    input_hashes: dict[str, str]
    ob_1s: pd.DataFrame
    trades_1s: pd.DataFrame
    candles_1m: pd.DataFrame


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_window() -> tuple[datetime, datetime]:
    start = datetime.fromisoformat("2026-08-20T12:33:00+00:00")
    end = datetime.fromisoformat("2026-08-24T06:35:00+00:00")
    return start, end


def is_genuine_row(row: pd.Series) -> bool:
    if int(row.get("is_valid", 0)) != 1:
        return False
    flags = str(row.get("quality_flags") or "")
    return "carried_forward" not in flags.split(",") if flags else True


def load_f1_bundle(f1_dir: Path) -> tuple[dict[str, Any], pd.DataFrame, pd.DataFrame, dict[str, str]]:
    f1_dir = f1_dir.resolve()
    manifest_path = f1_dir / "discovery_manifest.json"
    features_path = f1_dir / "minute_features.csv"
    candidates_path = f1_dir / "flush_candidates.csv"
    for path in (manifest_path, features_path, candidates_path):
        if not path.is_file():
            raise AggregateProxyError(f"missing F1 artifact: {path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    minute_features = pd.read_csv(features_path)
    flush_candidates = pd.read_csv(candidates_path)
    hashes = {
        "discovery_manifest.json": sha256(manifest_path),
        "minute_features.csv": sha256(features_path),
        "flush_candidates.csv": sha256(candidates_path),
    }
    return manifest, minute_features, flush_candidates, hashes


def load_ob_1s(client: Any, *, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    cols = ", ".join(OB_1S_COLUMNS)
    rows = client.query(
        f"""
        SELECT {cols}
        FROM {ORDERBOOK_TABLE} FINAL
        WHERE symbol = {{s:String}}
          AND parser_version = '{ORDERBOOK_PARSER_VERSION}'
          AND depth = {ORDERBOOK_DEPTH}
          AND bucket_start >= {{a:DateTime64(3,'UTC')}}
          AND bucket_start < {{b:DateTime64(3,'UTC')}}
        ORDER BY bucket_start
        """,
        parameters={"s": symbol, "a": start, "b": end},
        settings=_SETTINGS,
    ).result_rows
    frame = pd.DataFrame(rows, columns=list(OB_1S_COLUMNS))
    if not frame.empty:
        frame["bucket_start"] = pd.to_datetime(frame["bucket_start"], utc=True)
        frame["is_genuine"] = frame.apply(is_genuine_row, axis=1)
    return frame


def load_trades_1s(client: Any, *, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    rows = client.query(
        """
        SELECT
          toStartOfSecond(trade_ts) AS second,
          count() AS trade_count,
          sumIf(toFloat64(size) * toFloat64(price), side = 'Buy') AS buy_notional,
          sumIf(toFloat64(size) * toFloat64(price), side = 'Sell') AS sell_notional
        FROM orderbook_analysis.public_trades_canonical
        WHERE symbol = {s:String}
          AND trade_ts >= {a:DateTime64(3,'UTC')}
          AND trade_ts < {b:DateTime64(3,'UTC')}
        GROUP BY second
        ORDER BY second
        """,
        parameters={"s": symbol, "a": start, "b": end},
        settings=_SETTINGS,
    ).result_rows
    frame = pd.DataFrame(
        rows, columns=["second", "trade_count", "buy_notional", "sell_notional"]
    )
    if not frame.empty:
        frame["second"] = pd.to_datetime(frame["second"], utc=True)
    return frame


def load_candles_1m(client: Any, *, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    rows = client.query(
        """
        SELECT open_time, open, high, low, close
        FROM signal_generator.candles_1m FINAL
        WHERE symbol = {s:String} AND interval = '1m'
          AND open_time >= {a:DateTime64(3,'UTC')}
          AND open_time < {b:DateTime64(3,'UTC')}
        ORDER BY open_time
        """,
        parameters={"s": symbol, "a": start, "b": end},
        settings=_SETTINGS,
    ).result_rows
    frame = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close"])
    if not frame.empty:
        frame["open_time"] = pd.to_datetime(frame["open_time"], utc=True)
    return frame


def minute_has_trade_feed(minute_features: pd.DataFrame, minute: str, direction: str) -> bool:
    subset = minute_features[
        (minute_features["minute"] == minute) & (minute_features["direction"] == direction)
    ]
    if subset.empty:
        return False
    row = subset.iloc[0]
    if bool(row.get("technical_gap", False)):
        return False
    return bool(row.get("trades_present", False))


def trade_feed_gap_in_range(
    minute_features: pd.DataFrame,
    direction: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> bool:
    minutes = pd.date_range(start.floor("min"), end.floor("min"), freq="1min", tz="UTC")
    for minute in minutes:
        minute_str = minute.isoformat().replace("+00:00", "Z")
        subset = minute_features[
            (minute_features["minute"] == minute_str) & (minute_features["direction"] == direction)
        ]
        if subset.empty:
            continue
        row = subset.iloc[0]
        if bool(row.get("technical_gap", False)):
            return True
        if not bool(row.get("trades_present", False)):
            return True
    return False
