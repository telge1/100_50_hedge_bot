"""Read-only research-db loaders for BTC/DOGE OB Fight CLI."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from research.btc_doge_research.clickhouse import connect, rows
from research.btc_doge_research.contracts import TARGET_DATABASE
from research.btc_doge_research.phase2_contracts import TICK_SIZE

from .config import iso_z, utc
from .eligibility_contract import (
    DATA_SOURCE_RESEARCH_DB,
    OI_EXPECTED_FREQUENCY_MS,
    RESEARCH_DATABASE,
)

FORBIDDEN_WRITE = (
    "INSERT",
    "ALTER",
    "DELETE",
    "UPDATE",
    "TRUNCATE",
    "DROP",
    "OPTIMIZE",
)


class ResearchDbLoaderError(RuntimeError):
    pass


def _assert_read_only_sql(sql: str) -> None:
    upper = sql.upper()
    for token in FORBIDDEN_WRITE:
        if token in upper.split():
            raise ResearchDbLoaderError(f"forbidden write token in research loader SQL: {token}")


def research_client():
    return connect()


def _dt_sql(dt: datetime) -> str:
    return utc(dt).strftime("%Y-%m-%d %H:%M:%S")


def _dec_fs(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return value.decode().rstrip("\x00")
    return str(value).rstrip("\x00")


def tick_size(symbol: str) -> Decimal:
    if symbol not in TICK_SIZE:
        raise ResearchDbLoaderError(f"unsupported symbol tick size: {symbol}")
    return TICK_SIZE[symbol]


def ticks_to_price(symbol: str, ticks: int) -> float:
    return float(Decimal(ticks) * tick_size(symbol))


class TimedQuery:
    def __init__(self) -> None:
        self.timings: list[dict[str, Any]] = []

    def run(self, client: Any, name: str, sql: str, parameters: dict[str, Any] | None = None) -> list[tuple]:
        _assert_read_only_sql(sql)
        t0 = time.perf_counter()
        result = rows(client, sql, parameters or {})
        elapsed = time.perf_counter() - t0
        self.timings.append(
            {
                "source_name": name,
                "elapsed_s": round(elapsed, 6),
                "row_count": len(result),
                "database": RESEARCH_DATABASE,
            }
        )
        return result


def load_ob200_snapshots(
    client: Any,
    timer: TimedQuery,
    symbol: str,
    start: datetime,
    end: datetime,
    *,
    inclusive_end: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load unique 1s OB200 snapshots; keep first build_id by computed_at for safety."""
    start = utc(start)
    end = utc(end)
    op = "<=" if inclusive_end else "<"
    sql = f"""
        SELECT
            snapshot_ts, producer_id, bid_price_ticks, bid_quantities,
            ask_price_ticks, ask_quantities, best_bid, best_ask, mid, spread,
            bid_level_count, ask_level_count, genuine_depth, source_fingerprint,
            build_id, coverage_status, computed_at
        FROM {TARGET_DATABASE}.research_ob200_snapshots_1s
        WHERE symbol = %(symbol)s
          AND snapshot_ts >= %(start)s
          AND snapshot_ts {op} %(end)s
        ORDER BY snapshot_ts, computed_at
    """
    raw = timer.run(
        client,
        "OB200",
        sql,
        {"symbol": symbol, "start": start, "end": end},
    )
    by_ts: dict[datetime, dict[str, Any]] = {}
    dup = 0
    for r in raw:
        ts = utc(r[0])
        if ts in by_ts:
            dup += 1
            continue
        bid_ticks = list(r[2] or [])
        bid_qty = list(r[3] or [])
        ask_ticks = list(r[4] or [])
        ask_qty = list(r[5] or [])
        bids = [(ticks_to_price(symbol, int(t)), float(q)) for t, q in zip(bid_ticks, bid_qty)]
        asks = [(ticks_to_price(symbol, int(t)), float(q)) for t, q in zip(ask_ticks, ask_qty)]
        by_ts[ts] = {
            "ts": ts,
            "producer_id": str(r[1]),
            "bids": bids,
            "asks": asks,
            "best_bid": float(r[6]),
            "best_ask": float(r[7]),
            "mid": float(r[8]),
            "spread": float(r[9]),
            "bid_levels": int(r[10]),
            "ask_levels": int(r[11]),
            "genuine_200": bool(r[12]) and int(r[10]) == 200 and int(r[11]) == 200,
            "source_fingerprint": _dec_fs(r[13]),
            "build_id": _dec_fs(r[14]),
            "coverage_status": str(r[15]),
            "ok": True,
        }
    out = [by_ts[k] for k in sorted(by_ts)]
    meta = {
        "table": f"{TARGET_DATABASE}.research_ob200_snapshots_1s",
        "raw_rows": len(raw),
        "unique_seconds": len(out),
        "duplicate_seconds_dropped": dup,
        "min_ts": iso_z(out[0]["ts"]) if out else None,
        "max_ts": iso_z(out[-1]["ts"]) if out else None,
        "levels_200x200": sum(1 for x in out if x["genuine_200"]),
    }
    return out, meta


def _extract_walls_float(snap: dict[str, Any], *, max_walls: int = 10) -> list[dict[str, Any]]:
    """Float-path wall extraction (same heuristics as ``ob_replay.extract_walls``)."""
    import statistics

    from .config import WALL_MAX_BPS, WALL_QTY_MEDIAN_MULT

    mid = float(snap["mid"])
    if mid <= 0:
        return []
    thr = mid * float(WALL_MAX_BPS) / 10000.0
    out: list[dict[str, Any]] = []
    for side, levels in (("BID", snap.get("bids") or []), ("ASK", snap.get("asks") or [])):
        in_range = [(float(p), float(q)) for p, q in levels if abs(float(p) - mid) <= thr] or [
            (float(p), float(q)) for p, q in levels
        ]
        qtys = [q for _, q in in_range]
        med = statistics.median(qtys) if qtys else 0.0
        if med <= 0:
            continue
        walls = []
        for price, qty in in_range:
            ratio = qty / med
            if ratio < WALL_QTY_MEDIAN_MULT:
                continue
            walls.append(
                {
                    "side": side,
                    "price": price,
                    "qty": qty,
                    "notional": price * qty,
                    "bps_from_mid": abs(price - mid) / mid * 10000.0,
                    "distance_bps": abs(price - mid) / mid * 10000.0,
                    "ratio": ratio,
                }
            )
        walls.sort(key=lambda w: w["notional"], reverse=True)
        out.extend(walls[:max_walls])
    return out


def ob_snapshots_to_wall_rows(snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Adapt research OB snapshots to wall_events.sample_ob_snapshots row shape."""
    rows_out: list[dict[str, Any]] = []
    for i, snap in enumerate(snapshots):
        if not snap.get("ok"):
            rows_out.append({"sample_index": i, "ts": iso_z(snap.get("ts")), "ok": False})
            continue
        mid = float(snap["mid"])
        best_bid = float(snap["best_bid"])
        best_ask = float(snap["best_ask"])
        spread_bps = (best_ask - best_bid) / mid * 10000.0 if mid > 0 else 0.0
        bids = [(float(p), float(q)) for p, q in snap["bids"]]
        asks = [(float(p), float(q)) for p, q in snap["asks"]]
        walls = _extract_walls_float(
            {"mid": mid, "bids": bids, "asks": asks},
            max_walls=10,
        )
        top_bid = sorted([w for w in walls if w["side"] == "BID"], key=lambda w: -float(w["notional"]))[:5]
        top_ask = sorted([w for w in walls if w["side"] == "ASK"], key=lambda w: -float(w["notional"]))[:5]

        def _wall_dict(w: dict[str, Any]) -> dict[str, Any]:
            return {
                "side": w["side"],
                "price": float(w["price"]),
                "qty": float(w["qty"]),
                "notional": float(w["notional"]),
                "bps_from_mid": float(w.get("bps_from_mid") or 0),
                "distance_bps": float(w.get("distance_bps") or 0),
                "ratio": float(w.get("ratio") or 0),
            }

        rows_out.append(
            {
                "sample_index": i,
                "ts": iso_z(snap["ts"]),
                "as_of": iso_z(snap["ts"]),
                "mid": mid,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread_bps": spread_bps,
                "bid_levels": snap["bid_levels"],
                "ask_levels": snap["ask_levels"],
                "genuine_200": snap["genuine_200"],
                "top_bid_walls": [_wall_dict(w) for w in top_bid],
                "top_ask_walls": [_wall_dict(w) for w in top_ask],
                "bids": bids,
                "asks": asks,
                "ok": True,
            }
        )
    return rows_out


def _count_research_event_trades(
    client: Any, timer: TimedQuery, symbol: str, start: datetime, end: datetime
) -> int:
    # No FINAL: same MinMax-pruning rationale as load_public_trades (physical==canonical).
    sql = f"""
        SELECT count()
        FROM {TARGET_DATABASE}.research_public_trades
        WHERE symbol=%(symbol)s
          AND event_time >= %(start)s AND event_time < %(end)s
    """
    return int(
        timer.run(client, "PUBLIC_TRADES_EVENT_COUNT", sql, {"symbol": symbol, "start": start, "end": end})[0][0]
    )


def _public_trades_days_ready(client: Any, timer: TimedQuery, symbol: str, start: datetime, end: datetime) -> bool:
    """True when each UTC day intersecting [start,end) has a terminal PUBLIC_TRADES batch."""
    day = utc(start).replace(hour=0, minute=0, second=0, microsecond=0)
    end_u = utc(end)
    while day < end_u:
        day_end = day + timedelta(days=1)
        batch_prefix = f"fh:{symbol}:PUBLIC_TRADES:{day:%Y%m%dT000000Z}:{day_end:%Y%m%dT000000Z}:"
        sql = f"""
            SELECT status FROM (
              SELECT status,
                     row_number() OVER (
                       PARTITION BY batch_id
                       ORDER BY multiIf(status IN ('READY','PARTIAL','FAILED'), 2, 0) DESC,
                                started_at DESC
                     ) rn
              FROM {TARGET_DATABASE}.research_batch_runs
              WHERE startsWith(batch_id, %(prefix)s)
            ) WHERE rn=1
        """
        found = timer.run(client, "PUBLIC_TRADES_BATCH_STATUS", sql, {"prefix": batch_prefix})
        if not found or found[0][0] not in {"READY", "PARTIAL"}:
            return False
        day = day_end
    return True


def load_public_trades(
    client: Any,
    timer: TimedQuery,
    symbol: str,
    start: datetime,
    end: datetime,
    *,
    allow_legacy_trade_companion: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load deduplicated public trades from ``btc_doge_research.research_public_trades`` only.

    Legacy OA companion is disabled by default (source purity). Opt-in only via
    ``allow_legacy_trade_companion=True`` for explicit diagnostics — never silent.

    Performance note: queries omit ``FINAL``. Rematerialization audit proved
    physical == canonical and zero multi-version keys for the active build, and
    ``FINAL`` on ``ORDER BY (symbol, trade_id)`` disables event_time MinMax
    pruning (measured ~5× slower on the BTC golden window). Python still
    dedupes by ``trade_id`` for safety.
    """
    start = utc(start)
    end = utc(end)
    table = f"{TARGET_DATABASE}.research_public_trades"

    # Single pruned scan (no FINAL): derive span from payload; avoid second pass.
    sql = f"""
        SELECT event_time, trade_id, taker_side, toFloat64(price), toFloat64(base_size), toFloat64(quote_notional)
        FROM {TARGET_DATABASE}.research_public_trades
        WHERE symbol=%(symbol)s
          AND event_time >= %(start)s AND event_time < %(end)s
        ORDER BY event_time, trade_id
    """
    raw = timer.run(client, "PUBLIC_TRADES", sql, {"symbol": symbol, "start": start, "end": end})
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for r in raw:
        tid = str(r[1])
        if tid in seen:
            continue
        seen.add(tid)
        out.append(
            {
                "ts": utc(r[0]),
                "trade_id": tid,
                "side": str(r[2]),
                "price": float(r[3]),
                "size": float(r[4]),
                "notional": float(r[5]),
            }
        )
    event_count = len(out)
    spans_window = False
    if out:
        spans_window = out[0]["ts"] <= start + timedelta(seconds=5) and out[-1]["ts"] >= end - timedelta(seconds=5)

    if not spans_window:
        days_ready = _public_trades_days_ready(client, timer, symbol, start, end)
        if allow_legacy_trade_companion and days_ready:
            print(
                "WARNING: LEGACY_TRADE_COMPANION_OA_CANONICAL — not source-pure; "
                "research_public_trades lacks correct window events",
                flush=True,
            )
            table = "orderbook_analysis.public_trades_canonical"
            sql = f"""
                SELECT trade_ts, trade_id, side, toFloat64(price), toFloat64(size), toFloat64(notional)
                FROM orderbook_analysis.public_trades_canonical FINAL
                WHERE symbol=%(symbol)s
                  AND trade_ts >= %(start)s AND trade_ts < %(end)s
                ORDER BY trade_ts, trade_id
            """
            raw = timer.run(client, "PUBLIC_TRADES", sql, {"symbol": symbol, "start": start, "end": end})
            seen = set()
            out = []
            for r in raw:
                tid = str(r[1])
                if tid in seen:
                    continue
                seen.add(tid)
                out.append(
                    {
                        "ts": utc(r[0]),
                        "trade_id": tid,
                        "side": str(r[2]),
                        "price": float(r[3]),
                        "size": float(r[4]),
                        "notional": float(r[5]),
                    }
                )
            return out, {
                "table": table,
                "raw_count": len(raw),
                "deduped_count": len(out),
                "dedup_removed": len(raw) - len(out),
                "aggressor_semantics": "side/taker_side Buy/Sell is Bybit taker/aggressor",
                "sort": "ts, trade_id",
                "source_mode": "LINEAGE_COMPANION_OA_CANONICAL",
                "lineage_companion_used": True,
                "raw_archive_replay_used": False,
                "data_source": DATA_SOURCE_RESEARCH_DB,
                "mixed_sources_used": True,
                "research_event_count": event_count,
                "research_trade_events_missing": True,
                "min_ts": iso_z(out[0]["ts"]) if out else None,
                "max_ts": iso_z(out[-1]["ts"]) if out else None,
                "query_mode": "FINAL_COMPANION",
            }
        return [], {
            "table": table,
            "raw_count": 0,
            "deduped_count": 0,
            "source_mode": "RESEARCH_TRADE_EVENTS_MISSING",
            "lineage_companion_used": False,
            "research_event_count": event_count,
            "research_trade_events_missing": True,
            "public_trades_days_ready": days_ready,
            "mixed_sources_used": False,
            "raw_archive_replay_used": False,
            "query_mode": "NO_FINAL_PRUNED",
        }

    meta = {
        "table": table,
        "raw_count": len(raw),
        "deduped_count": len(out),
        "dedup_removed": len(raw) - len(out),
        "aggressor_semantics": "side/taker_side Buy/Sell is Bybit taker/aggressor",
        "sort": "ts, trade_id",
        "source_mode": "RESEARCH_PUBLIC_TRADES",
        "lineage_companion_used": False,
        "raw_archive_replay_used": False,
        "data_source": DATA_SOURCE_RESEARCH_DB,
        "mixed_sources_used": False,
        "research_event_count": event_count,
        "research_trade_events_missing": False,
        "min_ts": iso_z(out[0]["ts"]) if out else None,
        "max_ts": iso_z(out[-1]["ts"]) if out else None,
        "query_mode": "NO_FINAL_PRUNED",
    }
    return out, meta


def load_open_interest(
    client: Any, timer: TimedQuery, symbol: str, start: datetime, end: datetime
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    sql = f"""
        SELECT observation_time, toFloat64(open_interest), toFloat64(open_interest_value),
               coverage_status, build_id
        FROM {TARGET_DATABASE}.research_open_interest_observations
        WHERE symbol=%(symbol)s
          AND observation_time >= %(start)s AND observation_time < %(end)s
        ORDER BY observation_time, computed_at
    """
    raw = timer.run(client, "OPEN_INTEREST", sql, {"symbol": symbol, "start": utc(start), "end": utc(end)})
    by_ts: dict[datetime, dict[str, Any]] = {}
    for r in raw:
        ts = utc(r[0])
        if ts in by_ts:
            continue
        by_ts[ts] = {
            "ts": ts,
            "oi": float(r[1]),
            "oi_value": float(r[2]),
            "coverage_status": str(r[3]),
            "build_id": _dec_fs(r[4]),
        }
    out = [by_ts[k] for k in sorted(by_ts)]
    expected = max(0, int((utc(end) - utc(start)).total_seconds() * 1000 // OI_EXPECTED_FREQUENCY_MS))
    meta = {
        "table": f"{TARGET_DATABASE}.research_open_interest_observations",
        "count": len(out),
        "expected_samples": expected,
        "resolution_ms": OI_EXPECTED_FREQUENCY_MS,
        "min_ts": iso_z(out[0]["ts"]) if out else None,
        "max_ts": iso_z(out[-1]["ts"]) if out else None,
        "coverage_statuses": sorted({x["coverage_status"] for x in out}),
    }
    return out, meta


def load_liquidations(
    client: Any, timer: TimedQuery, symbol: str, start: datetime, end: datetime
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    sql = f"""
        SELECT event_time, liquidated_position_side, position_side_raw,
               toFloat64(executed_base_size), toFloat64(bankruptcy_reference_quote),
               toFloat64(bankruptcy_price), event_key, coverage_status, forced_flow
        FROM {TARGET_DATABASE}.research_liquidation_events
        WHERE symbol=%(symbol)s
          AND event_time >= %(start)s AND event_time < %(end)s
        ORDER BY event_time, event_key
    """
    raw = timer.run(client, "LIQUIDATIONS", sql, {"symbol": symbol, "start": utc(start), "end": utc(end)})
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for r in raw:
        key = str(r[6])
        if key in seen:
            continue
        seen.add(key)
        side = str(r[1])
        base = float(r[3] or 0)
        bp = float(r[5]) if r[5] is not None else None
        out.append(
            {
                "ts": utc(r[0]),
                "event_time": utc(r[0]),
                "side": side,
                "liquidated_side": side,
                "position_side_raw": str(r[2]),
                "forced_trade_direction": str(r[8]) if r[8] is not None else (
                    "FORCED_BUY" if side == "LIQUIDATED_SHORT" else "FORCED_SELL"
                ),
                "executed_base_size": base,
                "notional": float(r[4] or 0),
                "notional_estimate": float(r[4] or 0),
                "bankruptcy_price": bp,
                "bankruptcy_reference_quote": float(r[4] or 0),
                "event_key": key,
                "coverage_status": str(r[7]),
            }
        )
    meta = {
        "table": f"{TARGET_DATABASE}.research_liquidation_events",
        "count": len(out),
        "raw_count": len(raw),
        "deduped_count": len(out),
        "dedup_key": "event_key",
        "null_events_are_valid": True,
        "min_ts": iso_z(out[0]["ts"]) if out else None,
        "max_ts": iso_z(out[-1]["ts"]) if out else None,
    }
    return out, meta


def load_candles_coverage(
    client: Any, timer: TimedQuery, symbol: str, start: datetime, end: datetime
) -> dict[str, Any]:
    """Documented external COVERAGE_ONLY candle source (not a research import)."""
    sql = """
        SELECT count(), min(open_time), max(open_time)
        FROM signal_generator.candles_1m FINAL
        WHERE exchange='bybit' AND symbol=%(symbol)s AND interval='1m' AND is_closed=1
          AND open_time >= %(start)s AND open_time < %(end)s
    """
    row = timer.run(client, "CANDLES_1M", sql, {"symbol": symbol, "start": utc(start), "end": utc(end)})[0]
    expected = int((utc(end) - utc(start)).total_seconds() // 60)
    return {
        "table": "signal_generator.candles_1m",
        "classification": "COVERAGE_ONLY",
        "count": int(row[0]),
        "expected_minutes": expected,
        "min_ts": iso_z(utc(row[1])) if row[1] else None,
        "max_ts": iso_z(utc(row[2])) if row[2] else None,
        "complete": int(row[0]) >= max(0, expected - 1),
    }


def terminal_batch_status(
    client: Any, timer: TimedQuery, *, symbol: str, modality: str, segment_start: datetime, segment_end: datetime
) -> str | None:
    from research.btc_doge_research.full_history_contracts import segment_batch_id

    # producer prefix varies; match by startsWith of identity without producer suffix ambiguity
    start = utc(segment_start)
    end = utc(segment_end)
    prefix = f"fh:{symbol}:{modality}:{start:%Y%m%dT%H%M%SZ}:{end:%Y%m%dT%H%M%SZ}:"
    sql = f"""
        SELECT status FROM (
          SELECT status, batch_id,
                 row_number() OVER (
                   PARTITION BY batch_id
                   ORDER BY multiIf(status IN ('READY','PARTIAL','FAILED'), 2, status='RUNNING', 1, 0) DESC,
                            started_at DESC, completed_at DESC NULLS LAST
                 ) rn
          FROM {TARGET_DATABASE}.research_batch_runs
          WHERE startsWith(batch_id, %(prefix)s)
        ) WHERE rn=1
        ORDER BY multiIf(status='READY', 3, status='PARTIAL', 2, status='FAILED', 1, 0) DESC
        LIMIT 1
    """
    found = timer.run(client, f"BATCH_{modality}", sql, {"prefix": prefix})
    if not found:
        return None
    return str(found[0][0])


def probe_ob200_coverage_meta(
    client: Any,
    timer: TimedQuery,
    symbol: str,
    start: datetime,
    end: datetime,
    *,
    inclusive_end: bool = True,
) -> dict[str, Any]:
    """Lightweight OB200 coverage without loading bid/ask arrays."""
    start = utc(start)
    end = utc(end)
    expected = int((end - start).total_seconds()) + (1 if inclusive_end else 0)
    op = "<=" if inclusive_end else "<"
    sql = f"""
        SELECT countDistinct(snapshot_ts), min(snapshot_ts), max(snapshot_ts),
               countIf(bid_level_count=200 AND ask_level_count=200),
               count() - countDistinct(snapshot_ts)
        FROM {TARGET_DATABASE}.research_ob200_snapshots_1s
        WHERE symbol=%(symbol)s
          AND snapshot_ts >= %(start)s AND snapshot_ts {op} %(end)s
    """
    row = timer.run(client, "OB200_COVERAGE_META", sql, {"symbol": symbol, "start": start, "end": end})[0]
    observed = int(row[0] or 0)
    levels_ok = int(row[3] or 0) == observed and observed > 0
    dup = int(row[4] or 0)
    missing_count = max(0, expected - observed)
    if observed == 0:
        status = "NOT_AVAILABLE"
        missing_intervals = [{"start": iso_z(start) or "", "end": iso_z(end) or ""}]
        missing_seconds: list[str] = []
    elif missing_count or not levels_ok or dup > 0:
        status = "PARTIAL"
        # Only enumerate missing seconds when few; else interval summary
        missing_seconds = []
        missing_intervals = []
        if 0 < missing_count <= 64:
            miss_sql = f"""
                WITH expected AS (
                  SELECT toDateTime(toUnixTimestamp(%(start)s) + number) AS ts
                  FROM numbers(%(n)s)
                ),
                have AS (
                  SELECT DISTINCT snapshot_ts AS ts
                  FROM {TARGET_DATABASE}.research_ob200_snapshots_1s
                  WHERE symbol=%(symbol)s
                    AND snapshot_ts >= %(start)s AND snapshot_ts {op} %(end)s
                )
                SELECT ts FROM expected WHERE ts NOT IN have ORDER BY ts
            """
            n = expected if inclusive_end else expected
            miss_rows = timer.run(
                client,
                "OB200_MISSING_SECONDS",
                miss_sql,
                {"symbol": symbol, "start": start, "end": end, "n": n},
            )
            missing_seconds = [iso_z(utc(r[0])) or "" for r in miss_rows]
            dts = [utc(r[0]) for r in miss_rows]
            missing_intervals = []
            if dts:
                a = b = dts[0]
                for ts in dts[1:]:
                    if (ts - b).total_seconds() == 1:
                        b = ts
                        continue
                    missing_intervals.append({"start": iso_z(a) or "", "end": iso_z(b) or ""})
                    a = b = ts
                missing_intervals.append({"start": iso_z(a) or "", "end": iso_z(b) or ""})
        else:
            missing_intervals = [{"start": iso_z(start) or "", "end": iso_z(end) or "", "missing_count": missing_count}]
    else:
        status = "COMPLETE"
        missing_intervals = []
        missing_seconds = []
    return {
        "source_name": "OB200",
        "symbol": symbol,
        "requested_start": iso_z(start),
        "requested_end": iso_z(end),
        "available_start": iso_z(utc(row[1])) if row[1] else None,
        "available_end": iso_z(utc(row[2])) if row[2] else None,
        "expected_units": expected,
        "observed_units": observed,
        "missing_count": missing_count,
        "missing_intervals": missing_intervals,
        "missing_seconds": missing_seconds,
        "duplicate_seconds": dup,
        "levels_200x200_ok": levels_ok,
        "source_segment_status": status,
        "effective_coverage_status": status,
        "mandatory_for_facts": True,
        "mandatory_for_interpretation": True,
        "probe_mode": "COVERAGE_META_NO_ARRAYS",
        "table": f"{TARGET_DATABASE}.research_ob200_snapshots_1s",
    }


def probe_public_trade_events_meta(
    client: Any,
    timer: TimedQuery,
    symbol: str,
    start: datetime,
    end: datetime,
    *,
    source_name: str = "PUBLIC_TRADES",
) -> dict[str, Any]:
    """Event-table coverage probe only — no companion, no full event download.

    Omits ``FINAL`` so event_time MinMax indexes prune (FINAL on
    ``ORDER BY (symbol, trade_id)`` forces a full merge-read). Safe while
    rematerialization keeps physical == canonical.
    """
    start = utc(start)
    end = utc(end)
    sql = f"""
        SELECT min(event_time), max(event_time), count()
        FROM {TARGET_DATABASE}.research_public_trades
        WHERE symbol=%(symbol)s
          AND event_time >= %(start)s AND event_time < %(end)s
    """
    span = timer.run(client, f"{source_name}_EVENT_SPAN", sql, {"symbol": symbol, "start": start, "end": end})[0]
    count = int(span[2] or 0)
    days_ready = _public_trades_days_ready(client, timer, symbol, start, end)
    if count == 0:
        status = "NOT_AVAILABLE"
    else:
        tmin, tmax = utc(span[0]), utc(span[1])
        covers = tmin <= start + timedelta(seconds=5) and tmax >= end - timedelta(seconds=5)
        status = "COMPLETE" if covers else "PARTIAL"
    return {
        "source_name": source_name,
        "symbol": symbol,
        "requested_start": iso_z(start),
        "requested_end": iso_z(end),
        "available_start": iso_z(utc(span[0])) if span[0] else None,
        "available_end": iso_z(utc(span[1])) if span[1] else None,
        "expected_units": None,
        "observed_units": count,
        "missing_count": 0 if count else 1,
        "missing_intervals": []
        if count
        else [{"start": iso_z(start) or "", "end": iso_z(end) or "", "reason": "RESEARCH_TRADE_EVENTS_MISSING"}],
        "source_segment_status": "RESEARCH_TRADE_EVENTS_MISSING" if count == 0 else "RESEARCH_PUBLIC_TRADES",
        "effective_coverage_status": status,
        "mandatory_for_facts": True,
        "mandatory_for_interpretation": True,
        "lineage": {
            "table": f"{TARGET_DATABASE}.research_public_trades",
            "lineage_companion_used": False,
            "raw_archive_replay_used": False,
            "public_trades_days_ready": days_ready,
            "research_trade_events_missing": count == 0,
            "query_mode": "NO_FINAL_PRUNED",
        },
        "probe_mode": "EVENT_SPAN_ONLY",
    }
