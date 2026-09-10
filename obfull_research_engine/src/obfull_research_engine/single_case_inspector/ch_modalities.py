"""ClickHouse helpers for OI and liquidations (read-only, causal)."""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from orderbook_analyse.research.general_market_behavior_v1.coverage import load_clickhouse_env

from ..timeparse import format_utc_z
from . import OI_MAX_AGE_SECONDS, OI_WINDOWS_S


def _q(sql: str) -> str:
    host = os.environ.get("CLICKHOUSE_HOST", "127.0.0.1")
    port = os.environ.get("CLICKHOUSE_HTTP_PORT") or os.environ.get("CLICKHOUSE_PORT") or "8123"
    user = os.environ.get("CLICKHOUSE_USER", "default")
    password = os.environ.get("CLICKHOUSE_PASSWORD", "")
    params = {"user": user}
    if password:
        params["password"] = password
    url = f"http://{host}:{port}/?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, data=sql.encode(), method="POST")
    with urllib.request.urlopen(req, timeout=180) as resp:
        return resp.read().decode()


def _parse_ch(v: str) -> datetime:
    s = v.strip()
    if s.endswith("Z"):
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    if "." in s:
        try:
            return datetime.strptime(s[:26], "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def load_oi_samples(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load OI buckets with bucket_time in [start, end)."""
    load_clickhouse_env()
    symbol = symbol.upper()
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    hs = start.strftime("%Y-%m-%d %H:%M:%S")
    he = end.strftime("%Y-%m-%d %H:%M:%S")
    sql = (
        "SELECT bucket_time, source_event_time, open_interest "
        "FROM orderbook_analysis.open_interest_5s "
        f"WHERE symbol='{symbol}' AND bucket_time>='{hs}' AND bucket_time<'{he}' "
        "ORDER BY bucket_time FORMAT JSONEachRow"
    )
    rows: list[dict[str, Any]] = []
    for line in _q(sql).splitlines():
        if not line.strip():
            continue
        o = json.loads(line)
        bt = _parse_ch(str(o["bucket_time"]))
        et = _parse_ch(str(o.get("source_event_time") or o["bucket_time"]))
        rows.append(
            {
                "bucket_time": bt,
                "bucket_unix": int(bt.timestamp()),
                "source_event_time": et,
                "open_interest": float(o["open_interest"]),
            }
        )
    meta = {
        "table": "orderbook_analysis.open_interest_5s",
        "n": len(rows),
        "load_start": format_utc_z(start),
        "load_end_exclusive": format_utc_z(end),
        "max_age_contract_s": OI_MAX_AGE_SECONDS,
    }
    return rows, meta


def asof_oi(
    samples: list[dict[str, Any]],
    *,
    asof: datetime,
    max_age_s: int = OI_MAX_AGE_SECONDS,
) -> dict[str, Any] | None:
    """Latest OI with source_event_time < asof and age <= max_age."""
    asof = asof.astimezone(timezone.utc)
    best = None
    for s in samples:
        et = s["source_event_time"]
        if et >= asof:
            continue
        age = (asof - et).total_seconds()
        if age > float(max_age_s):
            continue
        if best is None or et > best["source_event_time"]:
            best = {**s, "age_seconds": age}
    return best


def oi_window_metrics(
    samples: list[dict[str, Any]],
    *,
    focus_ts: datetime,
    window_s: int,
    price_start: float | None,
    price_end: float | None,
) -> dict[str, Any]:
    """Causal OI change over [focus-W, focus) — no future fill."""
    focus_ts = focus_ts.astimezone(timezone.utc)
    w_start = datetime.fromtimestamp(focus_ts.timestamp() - window_s, tz=timezone.utc)
    start_oi = asof_oi(samples, asof=w_start)
    # end: last sample strictly before focus
    end_oi = asof_oi(samples, asof=focus_ts)
    base: dict[str, Any] = {
        "window_s": window_s,
        "window_start": format_utc_z(w_start),
        "window_end_exclusive": format_utc_z(focus_ts),
        "oi_start": None,
        "oi_end": None,
        "oi_delta_abs": None,
        "oi_delta_pct": None,
        "oi_start_age_seconds": None,
        "oi_end_age_seconds": None,
        "oi_direction": "UNAVAILABLE",
        "price_direction": "UNAVAILABLE",
        "quadrant": "MIXED_OR_FLAT",
        "coverage": "UNAVAILABLE",
    }
    if start_oi is None or end_oi is None:
        return base
    # ensure start sample is not after window start by more than max age already handled;
    # also require start sample event < focus and end < focus (asof guarantees)
    s_v = float(start_oi["open_interest"])
    e_v = float(end_oi["open_interest"])
    delta = e_v - s_v
    pct = (delta / s_v * 100.0) if s_v else None
    if abs(delta) < 1e-12:
        direction = "FLAT"
    elif delta > 0:
        direction = "RISING"
    else:
        direction = "FALLING"

    price_dir = "UNAVAILABLE"
    if price_start is not None and price_end is not None and price_start > 0:
        pr = (price_end - price_start) / price_start
        if abs(pr) < 1e-8:
            price_dir = "FLAT"
        elif pr > 0:
            price_dir = "UP"
        else:
            price_dir = "DOWN"

    if price_dir == "UP" and direction == "RISING":
        quad = "PRICE_UP_OI_UP"
    elif price_dir == "UP" and direction == "FALLING":
        quad = "PRICE_UP_OI_DOWN"
    elif price_dir == "DOWN" and direction == "RISING":
        quad = "PRICE_DOWN_OI_UP"
    elif price_dir == "DOWN" and direction == "FALLING":
        quad = "PRICE_DOWN_OI_DOWN"
    else:
        quad = "MIXED_OR_FLAT"

    base.update(
        {
            "oi_start": s_v,
            "oi_end": e_v,
            "oi_delta_abs": delta,
            "oi_delta_pct": pct,
            "oi_start_age_seconds": start_oi.get("age_seconds"),
            "oi_end_age_seconds": end_oi.get("age_seconds"),
            "oi_direction": direction,
            "price_direction": price_dir,
            "quadrant": quad,
            "coverage": "OK",
            "oi_start_event_time": format_utc_z(start_oi["source_event_time"]),
            "oi_end_event_time": format_utc_z(end_oi["source_event_time"]),
        }
    )
    return base


def build_oi_context(
    *,
    symbol: str,
    focus_ts: datetime,
    pre_seconds: int,
    price_at: dict[int, float | None],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    focus_ts = focus_ts.astimezone(timezone.utc)
    load_start = datetime.fromtimestamp(focus_ts.timestamp() - pre_seconds - OI_MAX_AGE_SECONDS - 60, tz=timezone.utc)
    samples, meta = load_oi_samples(symbol=symbol, start=load_start, end=focus_ts)
    rows = []
    for w in OI_WINDOWS_S:
        ps = price_at.get(int(focus_ts.timestamp()) - w)
        pe = price_at.get(int(focus_ts.timestamp()) - 1)  # last completed second before focus
        # fallback: asof prices via keys near window
        rows.append(
            oi_window_metrics(
                samples,
                focus_ts=focus_ts,
                window_s=w,
                price_start=ps,
                price_end=pe,
            )
        )
    source_ok = len(samples) > 0
    meta["source_complete"] = source_ok
    meta["status"] = "COMPLETE" if source_ok else "MISSING"
    return rows, meta


def load_liquidations(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    load_clickhouse_env()
    symbol = symbol.upper()
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    hs = start.strftime("%Y-%m-%d %H:%M:%S")
    he = end.strftime("%Y-%m-%d %H:%M:%S")
    sql = (
        "SELECT event_time, system_generated_at, received_at, position_side_raw, "
        "liquidated_position_side, size, bankruptcy_price, notional_estimate, event_key "
        "FROM orderbook_analysis.all_liquidations "
        f"WHERE symbol='{symbol}' AND event_time>='{hs}' AND event_time<'{he}' "
        "ORDER BY event_time FORMAT JSONEachRow"
    )
    # Probe source existence outside window (completeness of table access)
    tip = _q(
        "SELECT count() FROM orderbook_analysis.all_liquidations "
        f"WHERE symbol='{symbol}' FORMAT TSV"
    ).strip()
    rows: list[dict[str, Any]] = []
    for line in _q(sql).splitlines():
        if not line.strip():
            continue
        o = json.loads(line)
        et = _parse_ch(str(o["event_time"]))
        side = str(o.get("liquidated_position_side") or "").upper()
        notional = float(o.get("notional_estimate") or 0.0)
        recv = o.get("received_at")
        sys_at = o.get("system_generated_at")
        rows.append(
            {
                "event_time": et,
                "event_unix": int(et.timestamp()),
                "side": side,
                "liquidated_position_side": side,
                "position_side_raw": str(o.get("position_side_raw") or ""),
                "notional": notional,
                "size": float(o.get("size") or 0.0),
                "bankruptcy_price": None if o.get("bankruptcy_price") in (None, "") else float(o["bankruptcy_price"]),
                "event_key": str(o.get("event_key") or ""),
                "received_at": _parse_ch(str(recv)) if recv else None,
                "system_generated_at": _parse_ch(str(sys_at)) if sys_at else None,
            }
        )
    meta = {
        "table": "orderbook_analysis.all_liquidations",
        "n_in_window": len(rows),
        "source_row_count_symbol": int(tip or 0),
        "source_complete": True,  # table reachable; zero events = real zeros
        "load_start": format_utc_z(start),
        "load_end_exclusive": format_utc_z(end),
        "empty_event_policy": "ZERO_IS_VALID_WHEN_SOURCE_COMPLETE",
    }
    return rows, meta


def liq_window_metrics(
    events: list[dict[str, Any]],
    *,
    focus_ts: datetime,
    window_s: int,
    source_complete: bool,
) -> dict[str, Any]:
    focus_ts = focus_ts.astimezone(timezone.utc)
    lo = focus_ts.timestamp() - window_s
    hi = focus_ts.timestamp()
    long_n = short_n = 0.0
    long_c = short_c = 0
    max_evt = 0.0
    max_ts = None
    max_side = None
    for e in events:
        u = e["event_unix"]
        if not (lo <= u < hi):
            continue
        side = e["side"]
        n = float(e["notional"])
        is_long = "LONG" in side
        is_short = "SHORT" in side
        if is_long:
            long_n += n
            long_c += 1
        elif is_short:
            short_n += n
            short_c += 1
        else:
            # unknown side — count as neither long/short separately
            pass
        if n > max_evt:
            max_evt = n
            max_ts = format_utc_z(e["event_time"])
            max_side = side
    return {
        "window_s": window_s,
        "window_start": format_utc_z(datetime.fromtimestamp(lo, tz=timezone.utc)),
        "window_end_exclusive": format_utc_z(focus_ts),
        "long_count": long_c,
        "short_count": short_c,
        "long_notional": long_n,
        "short_notional": short_n,
        "net_notional": long_n - short_n,
        "max_event_notional": max_evt if (long_c + short_c) else 0.0,
        "max_event_time": max_ts,
        "max_event_side": max_side,
        "source_complete": source_complete,
        "coverage": "OK" if source_complete else "SOURCE_INCOMPLETE",
    }
