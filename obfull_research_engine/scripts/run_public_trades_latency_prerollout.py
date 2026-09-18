#!/usr/bin/env python3
"""Read-only public_trades_canonical latency measurement (>=120s). No writes."""

from __future__ import annotations

import json
import math
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RUN = Path(
    "/home/telgenbuescher/projects/orderbook_analyse_ch_research_mp_qdh_trigger_v1/"
    "obfull_research_engine/runs/ema_trend_live_prerollout_qualification_v1_20260918"
)
DURATION_S = 120.0
POLL_INTERVAL_S = 0.5
SYMBOLS = ("BTCUSDT", "ETHUSDT")


def _load_ch():
    from pathlib import Path as P
    import os
    import clickhouse_connect

    env_path = P("/home/telgenbuescher/projects/orderbook_analyse/.env")
    file_env: dict[str, str] = {}
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        file_env[k.strip()] = v.strip().strip("'").strip('"')
    host = os.environ.get("CLICKHOUSE_HOST") or file_env.get("CLICKHOUSE_HOST", "127.0.0.1")
    port = int(os.environ.get("CLICKHOUSE_HTTP_PORT") or file_env.get("CLICKHOUSE_HTTP_PORT") or "8123")
    user = os.environ.get("CLICKHOUSE_USER") or file_env.get("CLICKHOUSE_USER", "default")
    password = os.environ.get("CLICKHOUSE_PASSWORD") or file_env.get("CLICKHOUSE_PASSWORD", "")
    return clickhouse_connect.get_client(host=host, port=port, username=user, password=password)


def _pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    arr = sorted(xs)
    idx = min(len(arr) - 1, max(0, int(round((p / 100.0) * (len(arr) - 1)))))
    return float(arr[idx])


def main() -> int:
    client = _load_ch()
    # SELECT-only probe
    assert client.query("SELECT 1").result_rows == [(1,)]

    seen: set[tuple[str, str]] = set()
    trades: list[dict[str, Any]] = []
    polls: list[dict[str, Any]] = []
    watermark: dict[str, datetime | None] = {s: None for s in SYMBOLS}
    late_arrivals = 0
    ooo = 0
    last_ts_by_sym: dict[str, datetime | None] = {s: None for s in SYMBOLS}
    gaps_s: list[float] = []

    t0 = time.time()
    host_start = datetime.now(timezone.utc)
    # Clock skew probe: CH now() vs host
    ch_now = client.query("SELECT now64(3)").result_rows[0][0]
    if getattr(ch_now, "tzinfo", None) is None:
        ch_now = ch_now.replace(tzinfo=timezone.utc)
    clock_skew_host_minus_ch_ms = (host_start - ch_now.astimezone(timezone.utc)).total_seconds() * 1000.0

    sql = """
    SELECT
      symbol,
      trade_id,
      trade_ts,
      ingest_timestamp,
      price,
      size,
      side
    FROM orderbook_analysis.public_trades_canonical FINAL
    PREWHERE symbol IN {syms:Array(String)}
      AND trade_ts >= parseDateTime64BestEffort({since:String}, 3)
      AND ingest_timestamp >= parseDateTime64BestEffort({since:String}, 3) - INTERVAL 60 SECOND
    ORDER BY trade_ts ASC, trade_id ASC
    LIMIT 5000
    """

    poll_n = 0
    # Use CH server clock for watermarks to avoid host TZ ambiguity
    ch_now0 = client.query("SELECT toString(now64(3))").result_rows[0][0]
    # start 2s before CH now
    from datetime import timedelta
    import datetime as _dt

    # Parse CH now string
    def _parse_ch(s: str) -> datetime:
        # '2026-09-18 13:30:20.251'
        s = str(s).replace("T", " ")
        if "." in s:
            base, frac = s.split(".", 1)
            frac = (frac + "000000")[:6]
            dt = _dt.datetime.strptime(base + "." + frac, "%Y-%m-%d %H:%M:%S.%f")
        else:
            dt = _dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=timezone.utc)

    since_dt = _parse_ch(ch_now0) - timedelta(seconds=2)
    since = since_dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    while time.time() - t0 < DURATION_S:
        poll_time = datetime.now(timezone.utc)
        # analyzer-visible time aligned to CH clock + measured skew
        ch_now_str = client.query("SELECT toString(now64(3))").result_rows[0][0]
        ch_now_dt = _parse_ch(ch_now_str)
        poll_n += 1
        rows = client.query(
            sql,
            parameters={"syms": list(SYMBOLS), "since": since},
        ).result_rows
        observed_at = ch_now_dt  # CH-aligned "visible to analyzer" reference
        host_observed = datetime.now(timezone.utc)
        new_this_poll = 0
        max_ts_poll = since_dt
        for symbol, trade_id, trade_ts, ingest_ts, price, size, side in rows:
            if getattr(trade_ts, "tzinfo", None) is None:
                trade_ts = trade_ts.replace(tzinfo=timezone.utc)
            else:
                trade_ts = trade_ts.astimezone(timezone.utc)
            if ingest_ts is not None:
                if getattr(ingest_ts, "tzinfo", None) is None:
                    ingest_ts = ingest_ts.replace(tzinfo=timezone.utc)
                else:
                    ingest_ts = ingest_ts.astimezone(timezone.utc)
            key = (str(symbol), str(trade_id))
            if trade_ts > max_ts_poll:
                max_ts_poll = trade_ts
            if key in seen:
                continue
            seen.add(key)
            new_this_poll += 1
            wm = watermark.get(symbol)
            if wm is not None and trade_ts < wm:
                late_arrivals += 1
            last = last_ts_by_sym.get(symbol)
            if last is not None and trade_ts < last:
                ooo += 1
            else:
                last_ts_by_sym[symbol] = trade_ts
            if wm is None or trade_ts > wm:
                if wm is not None:
                    gaps_s.append((trade_ts - wm).total_seconds())
                watermark[symbol] = trade_ts

            exch_to_ingest_ms = None
            ingest_to_obs_ms = None
            exch_to_obs_ms = None
            if ingest_ts is not None:
                exch_to_ingest_ms = (ingest_ts - trade_ts).total_seconds() * 1000.0
                ingest_to_obs_ms = (observed_at - ingest_ts).total_seconds() * 1000.0
            exch_to_obs_ms = (observed_at - trade_ts).total_seconds() * 1000.0
            # Guard: discard pathological samples (>5 min) as query-window artifacts
            if exch_to_obs_ms is not None and exch_to_obs_ms > 300_000:
                continue
            trades.append(
                {
                    "symbol": symbol,
                    "trade_id": str(trade_id),
                    "exchange_event_time": trade_ts.isoformat(),
                    "receive_time": None,
                    "clickhouse_ingest_timestamp": ingest_ts.isoformat() if ingest_ts else None,
                    "query_observed_at_ch": observed_at.isoformat(),
                    "query_observed_at_host": host_observed.isoformat(),
                    "poll_time": poll_time.isoformat(),
                    "exch_to_ingest_ms": exch_to_ingest_ms,
                    "ingest_to_observed_ms": ingest_to_obs_ms,
                    "exch_to_analyzer_visible_ms": exch_to_obs_ms,
                }
            )
        if max_ts_poll and max_ts_poll != since_dt:
            since_dt = max_ts_poll - timedelta(milliseconds=200)
            since = since_dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        freshness = None
        if watermark["BTCUSDT"]:
            freshness = (observed_at - watermark["BTCUSDT"]).total_seconds()
        polls.append(
            {
                "poll": poll_n,
                "poll_time": poll_time.isoformat(),
                "rows_returned": len(rows),
                "new_trades": new_this_poll,
                "source_freshness_s_btc": freshness,
                "since": since,
            }
        )
        time.sleep(POLL_INTERVAL_S)

    exch_ingest = [t["exch_to_ingest_ms"] for t in trades if t["exch_to_ingest_ms"] is not None]
    exch_obs = [t["exch_to_analyzer_visible_ms"] for t in trades if t["exch_to_analyzer_visible_ms"] is not None]
    ingest_obs = [t["ingest_to_observed_ms"] for t in trades if t["ingest_to_observed_ms"] is not None]

    def stats(xs: list[float]) -> dict[str, Any]:
        if not xs:
            return {"n": 0, "p50": None, "p95": None, "p99": None, "max": None, "mean": None}
        return {
            "n": len(xs),
            "p50": _pct(xs, 50),
            "p95": _pct(xs, 95),
            "p99": _pct(xs, 99),
            "max": max(xs),
            "mean": statistics.fmean(xs),
        }

    # Suitability — factual; primary signal is exch→ingest (CH visibility).
    # receive_time absent from schema → Exchange→Receive cannot be measured.
    p95_ingest = _pct(exch_ingest, 95) if exch_ingest else None
    p95_vis = _pct(exch_obs, 95) if exch_obs else None
    if p95_ingest is None:
        suitability = "NOT_SUITABLE_FOR_FAST_FLOW"
        suit_reason = "no_trades_observed"
    elif p95_ingest <= 1500:
        suitability = "SUITABLE_FOR_EARLY_EVIDENCE"
        suit_reason = f"exch→ingest p95={p95_ingest:.1f}ms <=1500ms (early evidence windows start at 1s)"
    elif p95_ingest <= 5000:
        suitability = "SUITABLE_WITH_DELAY_WARNING"
        suit_reason = (
            f"exch→ingest p95={p95_ingest:.1f}ms; usable for early evidence with delay warning. "
            f"exch→analyzer_visible(p95)={p95_vis}"
        )
    else:
        suitability = "NOT_SUITABLE_FOR_FAST_FLOW"
        suit_reason = f"exch→ingest p95={p95_ingest:.1f}ms exceeds early-evidence comfort band"

    longest_gap = max(gaps_s) if gaps_s else None
    report = {
        "PUBLIC_TRADE_LATENCY_MEASURED": True,
        "duration_s": DURATION_S,
        "symbols": list(SYMBOLS),
        "poll_interval_s": POLL_INTERVAL_S,
        "poll_count": poll_n,
        "unique_trades": len(trades),
        "duplicates_skipped": "watermark_dedupe_by_trade_id",
        "late_arrivals": late_arrivals,
        "out_of_order_arrivals": ooo,
        "clock_skew_host_minus_ch_ms": clock_skew_host_minus_ch_ms,
        "fields_available": {
            "exchange_event_time": "trade_ts",
            "receive_time": "NOT_IN_SCHEMA",
            "clickhouse_visible_at": "ingest_timestamp",
            "query_observed_at": "host_datetime_at_query_return",
            "poll_time": "host_datetime_at_poll_start",
        },
        "exch_to_ingest_ms": stats(exch_ingest),
        "ingest_to_observed_ms": stats(ingest_obs),
        "exch_to_analyzer_visible_ms": stats(exch_obs),
        "longest_observed_gap_s": longest_gap,
        "source_freshness_last_poll_s_btc": polls[-1]["source_freshness_s_btc"] if polls else None,
        "suitability": suitability,
        "suitability_reason": suit_reason,
        "writes": False,
        "sample_trades_head": trades[:5],
        "polls_tail": polls[-3:],
    }
    RUN.mkdir(parents=True, exist_ok=True)
    (RUN / "PUBLIC_TRADES_LATENCY_REPORT.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n"
    )
    md = [
        "# PUBLIC_TRADES_LATENCY_REPORT",
        "",
        f"**PUBLIC_TRADE_LATENCY_MEASURED:** `true`",
        f"**Symbols:** `{', '.join(SYMBOLS)}`",
        f"**Duration:** `{DURATION_S}s`",
        f"**Polls:** `{poll_n}`",
        f"**Unique trades:** `{len(trades)}`",
        f"**Late arrivals:** `{late_arrivals}`",
        f"**Out-of-order:** `{ooo}`",
        f"**Clock skew host−CH:** `{clock_skew_host_minus_ch_ms:.1f} ms`",
        "",
        "## Field availability",
        "- exchange_event_time = `trade_ts`",
        "- receive_time = **NOT IN SCHEMA** (cannot measure Exchange→Receive lag)",
        "- clickhouse_visible_at ≈ `ingest_timestamp`",
        "- analyzer-visible = query return time on host",
        "",
        "## Lags (ms)",
        f"- exch→ingest: `{json.dumps(stats(exch_ingest))}`",
        f"- ingest→observed: `{json.dumps(stats(ingest_obs))}`",
        f"- exch→analyzer_visible: `{json.dumps(stats(exch_obs))}`",
        "",
        f"**Suitability:** `{suitability}`",
        f"**Reason:** {suit_reason}",
        "",
        f"**Longest data gap (watermark jumps):** `{longest_gap}` s",
    ]
    (RUN / "PUBLIC_TRADES_LATENCY_REPORT.md").write_text("\n".join(md) + "\n")
    print(
        json.dumps(
            {
                "polls": poll_n,
                "trades": len(trades),
                "late": late_arrivals,
                "ooo": ooo,
                "p50": _pct(exch_obs, 50),
                "p95": _pct(exch_obs, 95),
                "p99": _pct(exch_obs, 99),
                "max": max(exch_obs) if exch_obs else None,
                "suitability": suitability,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
