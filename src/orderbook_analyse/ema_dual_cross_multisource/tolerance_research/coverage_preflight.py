"""Coverage preflight for 30d XRP core-sources comparison."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from ...cluster_sweep_research.clickhouse_source import _as_utc, _q, coverage_report, default_client

OB_PARSER = "ob200_v3"
OB_DEPTH = 200
EXPECTED_CANDLE_MINUTES_PER_DAY = 1440
OB_COMPLETE_DAY_THRESHOLD = 0.90  # 90% of minutes with OB buckets


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.to_pydatetime()


def _daily_ob_minutes(client, symbol: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    rows = _q(
        client,
        """
        SELECT
          toDate(bucket_start) AS day,
          countDistinct(toStartOfMinute(bucket_start)) AS ob_minutes,
          min(bucket_start) AS first_ts,
          max(bucket_start) AS last_ts
        FROM orderbook_analysis.orderbook_features_1s_v2 FINAL
        WHERE symbol={s:String}
          AND parser_version={pv:String} AND depth={d:UInt16}
          AND bucket_start>={a:DateTime64(3,'UTC')} AND bucket_start<{b:DateTime64(3,'UTC')}
        GROUP BY day ORDER BY day
        """,
        {
            "s": symbol,
            "pv": OB_PARSER,
            "d": OB_DEPTH,
            "a": _as_utc(start),
            "b": _as_utc(end),
        },
    )
    out = []
    for day, ob_min, first_ts, last_ts in rows:
        ratio = float(ob_min) / EXPECTED_CANDLE_MINUTES_PER_DAY
        out.append(
            {
                "day": str(day),
                "ob_minutes": int(ob_min),
                "expected_minutes": EXPECTED_CANDLE_MINUTES_PER_DAY,
                "coverage_ratio": round(ratio, 6),
                "status": "COMPLETE" if ratio >= OB_COMPLETE_DAY_THRESHOLD else "PARTIAL",
                "first_ts": str(first_ts),
                "last_ts": str(last_ts),
                "parser_version": OB_PARSER,
                "depth": OB_DEPTH,
            }
        )
    return out


def _daily_candle_minutes(client, symbol: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    rows = _q(
        client,
        """
        SELECT toDate(open_time) AS day, count() AS n
        FROM signal_generator.candles_1m FINAL
        WHERE symbol={s:String} AND interval='1m'
          AND open_time>={a:DateTime64(3,'UTC')} AND open_time<{b:DateTime64(3,'UTC')}
        GROUP BY day ORDER BY day
        """,
        {"s": symbol, "a": _as_utc(start), "b": _as_utc(end)},
    )
    return [
        {
            "day": str(day),
            "candle_minutes": int(n),
            "expected_minutes": EXPECTED_CANDLE_MINUTES_PER_DAY,
            "coverage_ratio": round(int(n) / EXPECTED_CANDLE_MINUTES_PER_DAY, 6),
            "status": "COMPLETE" if int(n) >= EXPECTED_CANDLE_MINUTES_PER_DAY * 0.99 else "PARTIAL",
        }
        for day, n in rows
    ]


def _find_ob_complete_subwindow(daily_ob: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not daily_ob:
        return None
    complete_days = [d["day"] for d in daily_ob if d["status"] == "COMPLETE"]
    if not complete_days:
        return None
    # longest contiguous run
    best_start = best_end = complete_days[0]
    cur_start = cur_end = complete_days[0]
    for day in complete_days[1:]:
        prev = pd.Timestamp(cur_end)
        nxt = pd.Timestamp(day)
        if (nxt - prev).days == 1:
            cur_end = day
        else:
            if pd.Timestamp(cur_end) - pd.Timestamp(cur_start) >= pd.Timestamp(best_end) - pd.Timestamp(best_start):
                best_start, best_end = cur_start, cur_end
            cur_start = cur_end = day
    if pd.Timestamp(cur_end) - pd.Timestamp(cur_start) >= pd.Timestamp(best_end) - pd.Timestamp(best_start):
        best_start, best_end = cur_start, cur_end
    return {
        "start_at": f"{best_start}T00:00:00+00:00",
        "end_at": f"{(pd.Timestamp(best_end) + pd.Timedelta(days=1)).strftime('%Y-%m-%d')}T00:00:00+00:00",
        "n_days": (pd.Timestamp(best_end) - pd.Timestamp(best_start)).days + 1,
    }


def determine_30d_window(client, symbol: str) -> tuple[datetime, datetime, dict[str, Any]]:
    """Find end_at (exclusive) and start_at = end_at - 30 days from core feed availability."""
    probe_end = datetime.now(timezone.utc).replace(microsecond=0)
    probe_start = probe_end - timedelta(days=45)
    report = coverage_report(client, symbol, probe_start, probe_end)

    core_keys = ("candles_1m", "public_trades", "ob200_v3")
    last_ts: list[datetime] = []
    for key in core_keys:
        ts = _parse_ts((report.get(key) or {}).get("last_ts"))
        if ts:
            last_ts.append(ts)
    if not last_ts:
        raise RuntimeError("No core feed timestamps found for window determination")

    # Exclusive end = UTC midnight after min last core timestamp date
    min_last = min(last_ts)
    end_at = datetime(min_last.year, min_last.month, min_last.day, tzinfo=timezone.utc) + timedelta(days=1)
    start_at = end_at - timedelta(days=30)

    span_days = (end_at - start_at).total_seconds() / 86400.0
    if abs(span_days - 30.0) > 1e-6:
        raise RuntimeError(f"Window not exactly 30 days: {span_days}")

    daily_ob = _daily_ob_minutes(client, symbol, start_at, end_at)
    daily_candles = _daily_candle_minutes(client, symbol, start_at, end_at)
    ob_sub = _find_ob_complete_subwindow(daily_ob)

    feeds: dict[str, Any] = {}
    for name, key in [
        ("candles", "candles_1m"),
        ("public_trades", "public_trades"),
        ("orderbook_ob200_v3", "ob200_v3"),
        ("open_interest", "open_interest_5s"),
        ("liquidations", "liquidations"),
    ]:
        rec = report.get(key) or {}
        first = rec.get("first_ts")
        last = rec.get("last_ts")
        n = int(rec.get("row_count") or 0)
        st = rec.get("status", "MISSING")
        if name == "orderbook_ob200_v3":
            st = "COMPLETE" if daily_ob and all(d["status"] == "COMPLETE" for d in daily_ob) else (
                "PARTIAL" if daily_ob else "MISSING"
            )
        feeds[name] = {
            "first_ts": first,
            "last_ts": last,
            "row_count": n,
            "expected_window_start": start_at.isoformat(),
            "expected_window_end": end_at.isoformat(),
            "status": st,
            "parser_version": OB_PARSER if name == "orderbook_ob200_v3" else None,
        }

    preflight = {
        "symbol": symbol,
        "start_at": start_at.isoformat(),
        "end_at": end_at.isoformat(),
        "span_days": span_days,
        "feeds": feeds,
        "daily_ob_coverage": daily_ob,
        "daily_candle_coverage": daily_candles,
        "ob_complete_subwindow": ob_sub,
        "ob_full_30d": bool(daily_ob) and all(d["status"] == "COMPLETE" for d in daily_ob),
        "liquidity_location_note": "LLD evaluated per candidate via feature_builder liquidity_confluence",
        "volatility_note": "Volatility from EMA/ATR features at decision_at",
        "fake_impulse_note": "Frozen gate features at decision_at",
    }
    return start_at, end_at, preflight
