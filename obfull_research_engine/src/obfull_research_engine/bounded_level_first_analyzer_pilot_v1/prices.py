"""Causal public trades, 1s mid, and 1m candles. No outcome metrics here."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..outcomes.prices import CausalPriceIndex
from ..outcomes.public_trade_index import PublicTradeIndex, load_public_trades_window
from ..timeparse import format_utc_z
from .episodes import parse_utc


def load_pilot_trades(*, symbol: str, start: datetime, end: datetime) -> tuple[PublicTradeIndex, dict[str, Any]]:
    return load_public_trades_window(symbol=symbol, start=start, end=end)


def try_load_1s_mid(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
) -> tuple[CausalPriceIndex | None, dict[str, Any]]:
    """Load available UTC hour partitions. Missing hours are gaps, not a hard abort.

    Pre-roll into the previous hour (e.g. 18:55 when the pilot starts 19:00) must
    not fail the entire 1s-mid load when only that pre-roll partition is absent.
    """
    import pandas as pd

    from ..partition_io import partition_dir

    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    h = start.replace(minute=0, second=0, microsecond=0)
    frames: list[Any] = []
    loaded: list[str] = []
    missing: list[str] = []
    requested: list[str] = []
    while h < end:
        pdir = partition_dir(symbol, h)
        pq = pdir / "state_1s.parquet"
        label = h.strftime("%Y-%m-%dT%H:%M:%SZ")
        requested.append(label)
        if pq.exists():
            frames.append(pd.read_parquet(pq))
            loaded.append(label)
        else:
            missing.append(label)
        h += timedelta(hours=1)
    meta = {
        "ok": bool(frames),
        "price_source": "mb_state_1s_v1.mid_price",
        "requested_hours": requested,
        "loaded_hours": loaded,
        "missing_hours": missing,
        "fallback_price_source": None if frames else "public_trades",
        "public_trade_fallback_transparent": not bool(frames) or bool(missing),
        "pre_roll_hour_requested": requested[0] if requested else None,
        "query_uses_utc_hour_partitions": True,
        "inclusive_start_exclusive_end": True,
    }
    if not frames:
        meta["error"] = "no_state_partitions_loaded"
        return None, meta
    full = pd.concat(frames, ignore_index=True)
    full["state_ts"] = pd.to_datetime(full["state_ts"], utc=True)
    full = full.sort_values("state_ts").drop_duplicates(subset=["state_ts"], keep="last").reset_index(drop=True)
    keep_from = start - timedelta(seconds=2)
    mask = (full["state_ts"] >= keep_from) & (full["state_ts"] < end)
    sliced = full.loc[mask].reset_index(drop=True)
    meta["n_rows"] = int(len(sliced))
    if len(sliced):
        meta["state_ts_min"] = sliced["state_ts"].min().isoformat().replace("+00:00", "Z")
        meta["state_ts_max"] = sliced["state_ts"].max().isoformat().replace("+00:00", "Z")
    idx = CausalPriceIndex.from_state_df(sliced)
    return idx, meta


def asof_price(marks: list[tuple[datetime, float]], ts: datetime) -> float | None:
    ts = parse_utc(ts)
    best: float | None = None
    for mt, price in marks:
        if parse_utc(mt) <= ts:
            best = float(price)
        else:
            break
    return best


def load_candles_1m(*, symbol: str, start: datetime, end: datetime) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from orderbook_analyse.research.general_market_behavior_v1.coverage import load_clickhouse_env
    from ..interval_coverage import _q

    load_clickhouse_env()
    hs = start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    he = end.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sql = (
        "SELECT open_time, open, high, low, close FROM signal_generator.candles_1m "
        f"WHERE symbol='{symbol.upper()}' AND interval='1m' "
        f"AND open_time>='{hs}' AND open_time<'{he}' ORDER BY open_time FORMAT JSONEachRow"
    )
    import json

    rows: list[dict[str, Any]] = []
    for line in _q(sql).splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        open_ts = parse_utc(str(obj["open_time"]).replace(" ", "T") + "Z")
        rows.append(
            {
                "open_time": format_utc_z(open_ts),
                "open": float(obj["open"]),
                "high": float(obj["high"]),
                "low": float(obj["low"]),
                "close": float(obj["close"]),
            }
        )
    return rows, {"ok": bool(rows), "n": len(rows), "table": "signal_generator.candles_1m"}


def trade_events(index: PublicTradeIndex) -> list[dict[str, Any]]:
    return [{"ts": t.trade_ts.to_pydatetime(), "price": float(t.price), "kind": "trade"} for t in index.trades]


def mid_events(index: CausalPriceIndex | None) -> list[dict[str, Any]]:
    if index is None:
        return []
    out = []
    for p in index.points:
        if p.price_valid != 1:
            continue
        out.append({"ts": p.available_at.to_pydatetime(), "price": float(p.mid), "kind": "mid"})
    return out


def price_marks(
    trades: PublicTradeIndex,
    mid: CausalPriceIndex | None,
) -> list[tuple[datetime, float]]:
    marks: list[tuple[datetime, float]] = []
    for t in trades.trades:
        marks.append((t.trade_ts.to_pydatetime().astimezone(timezone.utc), float(t.price)))
    if mid is not None:
        for p in mid.points:
            if p.price_valid != 1:
                continue
            marks.append((p.available_at.to_pydatetime().astimezone(timezone.utc), float(p.mid)))
    marks.sort(key=lambda x: x[0])
    return marks
