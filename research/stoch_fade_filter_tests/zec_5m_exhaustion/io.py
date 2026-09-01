"""Read-only 1m load and causal last-closed 5m Stoch K."""

from __future__ import annotations

import json
import sys
from typing import Any

import numpy as np
import pandas as pd

from .config import (
    CANDLE_LOAD_END_EXCLUSIVE,
    CANDLE_LOAD_START,
    DASHBOARD_ROOT,
    GOLD_ROOT,
    LIVE_BOT_ENV,
    PIN_CANDLE_DATA_TO,
)
from .rule import stoch_exhausted_in_trade_direction


def _ensure_gold() -> None:
    src = str((GOLD_ROOT / "src").resolve())
    if src not in sys.path:
        sys.path.insert(0, src)


def to_utc(value: object) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def iso_z(value: object) -> str | None:
    if value is None:
        return None
    ts = pd.Timestamp(value)
    if pd.isna(ts):
        return None
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def load_jsonl(path) -> list[dict[str, Any]]:
    rows = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def connect_readonly() -> tuple[Any, dict[str, Any]]:
    _ensure_gold()
    from dotenv import load_dotenv
    from signal_generator.db.candles import CandleRepository
    from signal_generator.db.client import get_client

    if not LIVE_BOT_ENV.is_file():
        raise RuntimeError(f"CLICKHOUSE_ENV_MISSING:{LIVE_BOT_ENV}")
    load_dotenv(LIVE_BOT_ENV, override=False)
    inner = get_client()
    ping = inner.query("SELECT 1")
    repo = CandleRepository(inner)
    meta = {
        "connect_ok": True,
        "select_1_ok": bool(ping.result_rows and ping.result_rows[0][0] == 1),
        "writes": 0,
        "read_only": True,
        "query_types": ["SELECT"],
        "tables": ["signal_generator.candles_1m"],
    }
    return repo, meta


def load_symbol_1m(repo: Any, symbol: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    start = to_utc(CANDLE_LOAD_START).to_pydatetime()
    end = to_utc(CANDLE_LOAD_END_EXCLUSIVE).to_pydatetime()
    rows = repo.get_candles(symbol, start, end)
    if not rows:
        raise RuntimeError(f"CANDLES_EMPTY:{symbol}")
    df = pd.DataFrame(
        [
            {
                "open_time": to_utc(r["open_time"]),
                "close_time": to_utc(r["close_time"])
                if r.get("close_time") is not None
                else to_utc(r["open_time"]) + pd.Timedelta(minutes=1),
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r.get("volume") or 0.0),
            }
            for r in rows
        ]
    )
    df = df.sort_values("open_time").drop_duplicates("open_time").reset_index(drop=True)
    pin = to_utc(PIN_CANDLE_DATA_TO)
    df = df.loc[df["open_time"] <= pin].reset_index(drop=True)
    df["available_at"] = pd.to_datetime(df["close_time"], utc=True)
    df["timestamp"] = pd.to_datetime(df["open_time"], utc=True)
    meta = {
        "symbol": symbol,
        "n_1m": int(len(df)),
        "first_open": iso_z(df["open_time"].iloc[0]),
        "last_open": iso_z(df["open_time"].iloc[-1]),
        "gap_count": int((df["open_time"].diff() > pd.Timedelta(minutes=1)).sum()),
        "writes": 0,
    }
    return df, meta


def last_closed_index(available_at: np.ndarray, entry: np.datetime64) -> int | None:
    i = int(np.searchsorted(available_at, entry, side="right")) - 1
    if i < 0:
        return None
    return i


def build_5m_stoch(candles_1m: pd.DataFrame) -> pd.DataFrame:
    dash = str(DASHBOARD_ROOT)
    if dash not in sys.path:
        sys.path.insert(0, dash)
    from research.stoch_fade_trade_context_analysis.pipeline import aggregate_complete

    as_of = to_utc(candles_1m["close_time"].iloc[-1])
    agg, _audit = aggregate_complete(candles_1m, "5m", as_of=as_of)
    _ensure_gold()
    from signal_generator.strategy.wave_fade.indicators import attach_indicators

    return attach_indicators(agg)


def five_minute_flag_at_entry(
    frame_5m: pd.DataFrame,
    avail: np.ndarray,
    *,
    entry: pd.Timestamp,
    direction: str,
) -> dict[str, Any]:
    entry_ns = np.datetime64(to_utc(entry).to_datetime64())
    idx = last_closed_index(avail, entry_ns)
    if idx is None:
        return {
            "tf_5m_stoch_k": None,
            "tf_5m_stoch_d": None,
            "tf_5m_source_bar_open": None,
            "tf_5m_source_bar_close": None,
            "tf_5m_available_at": None,
            "available_at_le_entry": None,
            "stoch_exhausted_in_trade_direction": False,
            "snapshot_missing": True,
        }
    row = frame_5m.iloc[idx]
    avail_ts = to_utc(row["available_at"])
    if avail_ts > to_utc(entry):
        raise RuntimeError(f"LOOKAHEAD:5m:{iso_z(avail_ts)}>{iso_z(entry)}")
    k = row.get("stoch_k")
    flag = stoch_exhausted_in_trade_direction(direction, k)
    return {
        "tf_5m_stoch_k": None if pd.isna(k) else float(k),
        "tf_5m_stoch_d": None if pd.isna(row.get("stoch_d")) else float(row.get("stoch_d")),
        "tf_5m_source_bar_open": iso_z(row["timestamp"]),
        "tf_5m_source_bar_close": iso_z(avail_ts),
        "tf_5m_available_at": iso_z(avail_ts),
        "available_at_le_entry": True,
        "stoch_exhausted_in_trade_direction": bool(flag),
        "snapshot_missing": False,
    }


def load_outcomes(eval_dir, symbol: str) -> pd.DataFrame:
    from .config import FEE_PP

    path = eval_dir / "coin_runs" / symbol / "outcomes.jsonl"
    rows = load_jsonl(path)
    frame = pd.DataFrame(rows)
    frame["entry_time"] = pd.to_datetime(frame["entry_time"], utc=True)
    frame["exit_time"] = pd.to_datetime(frame["exit_time"], utc=True)
    frame["direction"] = frame["direction"].str.upper()
    frame["outcome"] = frame["outcome"].str.upper()
    frame["pnl_pct_gross"] = pd.to_numeric(frame["pnl_pct_gross"], errors="coerce")
    frame["pnl_pct_net"] = frame["pnl_pct_gross"] - FEE_PP
    frame.loc[frame["outcome"] == "OPEN", "pnl_pct_net"] = np.nan
    frame.loc[frame["is_open"] == True, "pnl_pct_net"] = np.nan
    return frame
