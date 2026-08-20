"""1-minute market activity export from ClickHouse (read-only)."""

from __future__ import annotations

import csv
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from orderbook_analyse.dynamic_wall_detector import connect_readonly
from orderbook_analyse.wall_toxicity_audit.data_access import ensure_utc

LOG = logging.getLogger(__name__)

# Bybit public trade `side`: taker/aggressor side (Buy = aggressive buy).
# Liquidation `side`: Buy = LIQUIDATED_LONG, Sell = LIQUIDATED_SHORT (bybit_recorder docstring).


def export_market_activity_1m(
    *,
    symbols: list[str],
    start: datetime,
    end: datetime,
    output_dir: Path,
    overwrite: bool = False,
) -> dict[str, Any]:
    start, end = ensure_utc(start), ensure_utc(end)
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()) and not overwrite:
        raise FileExistsError(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    db = connect_readonly()

    trade_rows: list[dict[str, Any]] = []
    oi_rows: list[dict[str, Any]] = []
    liq_rows: list[dict[str, Any]] = []
    market_rows: list[dict[str, Any]] = []

    for sym in symbols:
        # trades 1m
        q = db.query(
            """
            SELECT
              toStartOfMinute(trade_ts) AS m,
              count() AS trade_count_1m,
              countIf(side = 'Buy') AS buy_trade_count_1m,
              countIf(side = 'Sell') AS sell_trade_count_1m,
              sum(notional) AS trade_volume_1m,
              sumIf(notional, side = 'Buy') AS aggressive_buy_notional_1m,
              sumIf(notional, side = 'Sell') AS aggressive_sell_notional_1m,
              avg(notional) AS average_trade_size_1m,
              quantileExact(0.5)(notional) AS median_trade_size_1m,
              max(notional) AS max_trade_size_1m,
              min(trade_ts) AS first_trade_ts,
              max(trade_ts) AS last_trade_ts
            FROM orderbook_analysis.public_trades
            WHERE symbol = {s:String}
              AND trade_ts >= {a:DateTime64(3,'UTC')}
              AND trade_ts <  {b:DateTime64(3,'UTC')}
            GROUP BY m
            ORDER BY m
            """,
            parameters={"s": sym, "a": start, "b": end},
        )
        trades_by_m: dict[Any, dict] = {}
        for row in q.result_rows:
            (
                m,
                tc,
                bc,
                sc,
                vol,
                buy_n,
                sell_n,
                avg_sz,
                med_sz,
                max_sz,
                first_ts,
                last_ts,
            ) = row
            rec = {
                "symbol": sym,
                "minute_utc": m.isoformat(),
                "trade_count_1m": int(tc),
                "buy_trade_count_1m": int(bc),
                "sell_trade_count_1m": int(sc),
                "total_trade_volume_1m": float(vol or 0),
                "trade_volume_1m": float(vol or 0),
                "buy_volume_1m": float(buy_n or 0),
                "sell_volume_1m": float(sell_n or 0),
                "aggressive_buy_notional_1m": float(buy_n or 0),
                "aggressive_sell_notional_1m": float(sell_n or 0),
                "aggressive_flow_delta_1m": float(buy_n or 0) - float(sell_n or 0),
                "average_trade_size_1m": float(avg_sz or 0),
                "median_trade_size_1m": float(med_sz or 0),
                "max_trade_size_1m": float(max_sz or 0),
                "large_trade_count_1m": None,  # needs causal baseline; filled later optional
                "first_trade_ts": first_ts.isoformat() if first_ts else None,
                "last_trade_ts": last_ts.isoformat() if last_ts else None,
            }
            trade_rows.append(rec)
            trades_by_m[m] = rec

        # OI 1m from ticker
        q = db.query(
            """
            SELECT
              toStartOfMinute(exchange_ts) AS m,
              argMin(open_interest, exchange_ts) AS oi_open,
              max(open_interest) AS oi_high,
              min(open_interest) AS oi_low,
              argMax(open_interest, exchange_ts) AS oi_close,
              count() AS oi_sample_count_1m,
              countIf(open_interest IS NOT NULL) AS oi_nonnull_1m,
              max(exchange_ts) AS last_oi_ts
            FROM orderbook_analysis.ticker_samples
            WHERE symbol = {s:String}
              AND exchange_ts >= {a:DateTime64(3,'UTC')}
              AND exchange_ts <  {b:DateTime64(3,'UTC')}
            GROUP BY m
            ORDER BY m
            """,
            parameters={"s": sym, "a": start, "b": end},
        )
        oi_by_m: dict[Any, dict] = {}
        for row in q.result_rows:
            m, oi_o, oi_h, oi_l, oi_c, n, nn, last_ts = row
            oi_o_f = float(oi_o) if oi_o is not None else None
            oi_c_f = float(oi_c) if oi_c is not None else None
            ch = (oi_c_f - oi_o_f) if oi_o_f is not None and oi_c_f is not None else None
            pct = (ch / oi_o_f * 100.0) if ch is not None and oi_o_f not in (None, 0) else None
            rec = {
                "symbol": sym,
                "minute_utc": m.isoformat(),
                "oi_open": oi_o_f,
                "oi_high": float(oi_h) if oi_h is not None else None,
                "oi_low": float(oi_l) if oi_l is not None else None,
                "oi_close": oi_c_f,
                "oi_change_abs_1m": ch,
                "oi_change_pct_1m": pct,
                "oi_sample_count_1m": int(n),
                "oi_available": int(nn) > 0,
                "oi_stale": int(nn) == 0,
                "oi_age_seconds": None,
            }
            oi_rows.append(rec)
            oi_by_m[m] = rec

        # liquidations 1m
        q = db.query(
            """
            SELECT
              toStartOfMinute(liquidation_ts) AS m,
              count() AS liquidation_count_1m,
              countIf(side = 'Buy') AS long_liquidation_count_1m,
              countIf(side = 'Sell') AS short_liquidation_count_1m,
              sum(notional) AS liquidation_notional_1m,
              sumIf(notional, side = 'Buy') AS long_liquidation_notional_1m,
              sumIf(notional, side = 'Sell') AS short_liquidation_notional_1m,
              max(notional) AS max_liquidation_notional_1m,
              min(liquidation_ts) AS first_liquidation_ts,
              max(liquidation_ts) AS last_liquidation_ts
            FROM orderbook_analysis.liquidations
            WHERE symbol = {s:String}
              AND liquidation_ts >= {a:DateTime64(3,'UTC')}
              AND liquidation_ts <  {b:DateTime64(3,'UTC')}
            GROUP BY m
            ORDER BY m
            """,
            parameters={"s": sym, "a": start, "b": end},
        )
        liq_by_m: dict[Any, dict] = {}
        for row in q.result_rows:
            (
                m,
                lc,
                long_c,
                short_c,
                tot,
                long_n,
                short_n,
                mx,
                first_ts,
                last_ts,
            ) = row
            dom = "NONE"
            if float(long_n or 0) > float(short_n or 0):
                dom = "LONG"
            elif float(short_n or 0) > float(long_n or 0):
                dom = "SHORT"
            elif int(lc) > 0:
                dom = "MIXED"
            rec = {
                "symbol": sym,
                "minute_utc": m.isoformat(),
                "liquidation_count_1m": int(lc),
                "long_liquidation_count_1m": int(long_c),
                "short_liquidation_count_1m": int(short_c),
                "liquidation_notional_1m": float(tot or 0),
                "long_liquidation_notional_1m": float(long_n or 0),
                "short_liquidation_notional_1m": float(short_n or 0),
                "dominant_liquidation_side_1m": dom,
                "max_liquidation_notional_1m": float(mx or 0),
                "first_liquidation_ts": first_ts.isoformat() if first_ts else None,
                "last_liquidation_ts": last_ts.isoformat() if last_ts else None,
            }
            liq_rows.append(rec)
            liq_by_m[m] = rec

        # orderbook updates 1m
        q = db.query(
            """
            SELECT
              toStartOfMinute(exchange_ts) AS m,
              count() AS orderbook_update_count_1m,
              countIf(message_type = 'snapshot') AS snapshot_count_1m
            FROM orderbook_analysis.orderbook_deltas
            WHERE symbol = {s:String}
              AND exchange_ts >= {a:DateTime64(3,'UTC')}
              AND exchange_ts <  {b:DateTime64(3,'UTC')}
            GROUP BY m
            ORDER BY m
            """,
            parameters={"s": sym, "a": start, "b": end},
        )
        ob_by_m = {r[0]: {"orderbook_update_count_1m": int(r[1]), "snapshot_count_1m": int(r[2])} for r in q.result_rows}

        # price OHLC from trades
        q = db.query(
            """
            SELECT
              toStartOfMinute(trade_ts) AS m,
              argMin(price, trade_ts), max(price), min(price), argMax(price, trade_ts)
            FROM orderbook_analysis.public_trades
            WHERE symbol = {s:String}
              AND trade_ts >= {a:DateTime64(3,'UTC')}
              AND trade_ts <  {b:DateTime64(3,'UTC')}
            GROUP BY m
            """,
            parameters={"s": sym, "a": start, "b": end},
        )
        px_by_m = {}
        for m, o, h, l, c in q.result_rows:
            o_f, h_f, l_f, c_f = float(o), float(h), float(l), float(c)
            ret = (c_f - o_f) / o_f * 10_000 if o_f else None
            rng = (h_f - l_f) / o_f * 10_000 if o_f else None
            px_by_m[m] = {
                "open": o_f,
                "high": h_f,
                "low": l_f,
                "close": c_f,
                "return_1m_bps": ret,
                "range_1m_bps": rng,
            }

        minutes = sorted(set(trades_by_m) | set(oi_by_m) | set(liq_by_m) | set(ob_by_m) | set(px_by_m))
        for m in minutes:
            t = trades_by_m.get(m, {})
            oi = oi_by_m.get(m, {})
            li = liq_by_m.get(m, {})
            ob = ob_by_m.get(m, {})
            px = px_by_m.get(m, {})
            vol = float(t.get("trade_volume_1m") or 0)
            liq_n = float(li.get("liquidation_notional_1m") or 0)
            # If trades exist in window overall, missing minute liq = true zero when OB+trades present
            trades_ok = m in trades_by_m or m in ob_by_m
            liq_count = int(li.get("liquidation_count_1m") or 0) if m in liq_by_m else (0 if trades_ok else None)
            row = {
                "symbol": sym,
                "minute_utc": m.isoformat(),
                **px,
                **{k: t.get(k) for k in (
                    "trade_count_1m", "buy_trade_count_1m", "sell_trade_count_1m", "trade_volume_1m",
                    "aggressive_buy_notional_1m", "aggressive_sell_notional_1m", "aggressive_flow_delta_1m",
                    "average_trade_size_1m", "max_trade_size_1m", "large_trade_count_1m",
                )},
                **{k: oi.get(k) for k in (
                    "oi_open", "oi_close", "oi_change_abs_1m", "oi_change_pct_1m", "oi_sample_count_1m",
                    "oi_available", "oi_stale",
                )},
                "liquidation_count_1m": liq_count if liq_count is not None else None,
                "long_liquidation_count_1m": int(li.get("long_liquidation_count_1m") or 0) if m in liq_by_m else (0 if trades_ok else None),
                "short_liquidation_count_1m": int(li.get("short_liquidation_count_1m") or 0) if m in liq_by_m else (0 if trades_ok else None),
                "long_liquidation_notional_1m": float(li.get("long_liquidation_notional_1m") or 0) if m in liq_by_m else (0.0 if trades_ok else None),
                "short_liquidation_notional_1m": float(li.get("short_liquidation_notional_1m") or 0) if m in liq_by_m else (0.0 if trades_ok else None),
                "liquidation_notional_1m": liq_n if m in liq_by_m else (0.0 if trades_ok else None),
                "liquidation_to_trade_volume_ratio_1m": (liq_n / vol) if vol > 0 and m in liq_by_m else (0.0 if trades_ok and vol > 0 else None),
                **ob,
                "wall_transition_count_1m": None,
                "wall_created_count_1m": None,
                "wall_disappeared_count_1m": None,
                "wall_grew_count_1m": None,
                "wall_shrank_count_1m": None,
                "wall_migration_count_1m": None,
                "wall_refill_count_1m": None,
                "bid_wall_transition_count_1m": None,
                "ask_wall_transition_count_1m": None,
                "trades_source_available": m in trades_by_m,
                "oi_source_available": m in oi_by_m,
                "liquidation_source_available": trades_ok,  # recorder-linked; events may be zero
                "orderbook_source_available": m in ob_by_m,
                "wall_source_available": False,
                "minute_complete": (m in trades_by_m) and (m in ob_by_m) and (m in oi_by_m),
                "missing_sources": ",".join(
                    [
                        x
                        for x, ok in (
                            ("trades", m in trades_by_m),
                            ("orderbook", m in ob_by_m),
                            ("oi", m in oi_by_m),
                        )
                        if not ok
                    ]
                ),
                "quality_status": "OK" if (m in trades_by_m and m in ob_by_m) else "PARTIAL",
            }
            # fill liq ratios on trade file too
            if m in liq_by_m:
                liq_rows_idx = next(i for i, r in enumerate(liq_rows) if r["symbol"] == sym and r["minute_utc"] == m.isoformat())
                liq_rows[liq_rows_idx]["liquidation_to_trade_volume_ratio_1m"] = (
                    liq_n / vol if vol > 0 else None
                )
                liq_rows[liq_rows_idx]["liquidation_to_trade_count_ratio_1m"] = (
                    float(liq_count or 0) / float(t.get("trade_count_1m") or 0)
                    if t.get("trade_count_1m")
                    else None
                )
            market_rows.append(row)

    def _write(name: str, rows: list[dict[str, Any]]) -> None:
        path = output_dir / name
        if not rows:
            path.write_text("", encoding="utf-8")
            return
        keys: list[str] = []
        seen: set[str] = set()
        for r in rows:
            for k in r:
                if k not in seen:
                    seen.add(k)
                    keys.append(k)
        with path.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)

    _write("trade_activity_1m.csv", trade_rows)
    _write("oi_coverage_1m.csv", oi_rows)
    _write("liquidation_activity_1m.csv", liq_rows)
    _write("market_activity_1m.csv", market_rows)
    # also copy into collector audit if requested by caller
    return {
        "n_trade_minutes": len(trade_rows),
        "n_market_rows": len(market_rows),
        "symbols": symbols,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "semantics": {
            "public_trades.side": "Bybit taker/aggressor (Buy=aggressive buy)",
            "liquidations.side": "Buy=LIQUIDATED_LONG, Sell=LIQUIDATED_SHORT",
        },
    }
