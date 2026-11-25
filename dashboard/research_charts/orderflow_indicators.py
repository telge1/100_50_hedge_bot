"""Read-only Delta, trade density and CVD from public_trades_canonical.

Prints are executions, not market orders. Bubble buckets are not an input.
A candle is never marked complete: the collector does not persist a per-candle
sequence or health row. Empty candles stay GAP or UNKNOWN and carry no zero.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import clickhouse_connect
from clickhouse_connect.driver.exceptions import DatabaseError, OperationalError

from .clickhouse_config import load_clickhouse_config

CANONICAL_FQN = "orderbook_analysis.public_trades_canonical"
TF_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}
MAX_RANGE_S = 7 * 24 * 3600
QUERY_TIMEOUT_S = 30
QUERY_MEMORY = 800_000_000
CONFIRM_GAP_S = 15 * 60
PROBE_S = 2 * 24 * 3600
_Q = Decimal("0.00000001")
_PCT = Decimal("0.0001")


def _client():
    return clickhouse_connect.get_client(**load_clickhouse_config().connect_kwargs())


def _settings() -> dict[str, int]:
    return {
        "max_execution_time": QUERY_TIMEOUT_S,
        "max_memory_usage": QUERY_MEMORY,
        "max_threads": 2,
    }


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return _utc(dt).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _dec(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _money(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.quantize(_Q, rounding=ROUND_HALF_UP), "f")


def _pct(delta: Decimal, total: Decimal) -> str | None:
    if total == 0:
        return None
    return format((delta / total * Decimal("100")).quantize(_PCT, rounding=ROUND_HALF_UP), "f")


def floor_open(epoch: int, step: int) -> int:
    return int(epoch) - (int(epoch) % int(step))


def classify_empty(
    open_s: int,
    step: int,
    as_of_s: int,
    left_ts: int | None,
    right_ts: int | None,
) -> str:
    """Closed empty bucket. Never COMPLETE, never a numeric zero."""
    close_s = int(open_s) + int(step)
    if int(open_s) <= int(as_of_s) < close_s or close_s > int(as_of_s):
        return "FORMING"
    left_far = left_ts is None or (int(open_s) - int(left_ts)) >= CONFIRM_GAP_S
    right_far = right_ts is None or (int(right_ts) - close_s) >= CONFIRM_GAP_S
    if left_far and right_far:
        return "GAP"
    return "UNKNOWN"


def apply_cvd(bars: list[dict[str, Any]], *, reset: str) -> None:
    """In-place. Gaps and unknown buckets break the line. No synthetic zero."""
    segment = 0
    running: Decimal | None = None
    prev_day: str | None = None
    for bar in bars:
        status = bar["status"]
        day = datetime.fromtimestamp(int(bar["time"]), tz=timezone.utc).strftime("%Y-%m-%d")
        day_break = reset == "utc_day" and prev_day is not None and day != prev_day
        if status != "MEASURED" and status != "FORMING":
            bar["cvd_usdt"] = None
            bar["segment_start"] = False
            bar["segment_id"] = None
            running = None
            prev_day = day
            continue
        if bar.get("delta_usdt") is None:
            bar["cvd_usdt"] = None
            bar["segment_start"] = False
            bar["segment_id"] = None
            running = None
            prev_day = day
            continue
        delta = Decimal(bar["delta_usdt"])
        start = running is None or day_break
        if start:
            segment += 1
            running = delta
        else:
            running = running + delta
        bar["cvd_usdt"] = _money(running)
        bar["segment_start"] = start
        bar["segment_id"] = segment
        prev_day = day


def _measured_bar(open_s: int, row: dict[str, Any], *, forming: bool) -> dict[str, Any]:
    buy = _dec(row["buy_usdt"])
    sell = _dec(row["sell_usdt"])
    total = buy + sell
    delta = buy - sell
    prints = int(row["prints"])
    avg = (total / Decimal(prints)) if prints else None
    return {
        "time": open_s,
        "status": "FORMING" if forming else "MEASURED",
        "coverage": "UNPROVEN",
        "coverage_note": (
            "Laufende Kerze, nur bis zur letzten bekannten Ausführung. "
            "Vollständigkeit ist nicht belegt."
            if forming
            else "Gemessene Ausführungen. Keine gespeicherte Collector-Sequenz, Vollständigkeit nicht belegt."
        ),
        "buy_usdt": _money(buy),
        "sell_usdt": _money(sell),
        "total_usdt": _money(total),
        "delta_usdt": _money(delta),
        "delta_pct": _pct(delta, total),
        "prints": prints,
        "avg_usdt_per_print": _money(avg) if avg is not None else None,
        "max_prints_1s": int(row["max_1s"]),
        "max_prints_10s": int(row["max_10s"]),
        "first_trade_ts": _iso(row["first_ts"]),
        "last_trade_ts": _iso(row["last_ts"]),
        "highlight": False,
    }


def _empty_bar(open_s: int, status: str) -> dict[str, Any]:
    if status == "GAP":
        note = "Bestätigte Lücke in public_trades_canonical. Kein Delta und keine Nulllinie."
        coverage = "GAP"
    elif status == "FORMING":
        note = "Laufende Kerze ohne bisher bekannte Ausführung."
        coverage = "FORMING"
    else:
        note = "Keine Ausführung in der Kerze. Ohne Collector-Beleg weder Lücke noch Null."
        coverage = "UNKNOWN"
        status = "UNKNOWN"
    return {
        "time": open_s,
        "status": status,
        "coverage": coverage,
        "coverage_note": note,
        "buy_usdt": None,
        "sell_usdt": None,
        "total_usdt": None,
        "delta_usdt": None,
        "delta_pct": None,
        "prints": None,
        "avg_usdt_per_print": None,
        "max_prints_1s": None,
        "max_prints_10s": None,
        "first_trade_ts": None,
        "last_trade_ts": None,
        "highlight": False,
    }


def _query(sql: str, params: dict[str, Any]) -> list[tuple]:
    client = _client()
    try:
        return client.query(sql, parameters=params, settings=_settings()).result_rows
    except (DatabaseError, OperationalError) as exc:
        raise RuntimeError(f"query_failed:{exc}") from exc
    finally:
        client.close()


def _load_buckets(symbol: str, step: int, start: datetime, end: datetime) -> list[dict[str, Any]]:
    sql = f"""
    WITH sec AS (
      SELECT
        toUnixTimestamp(toStartOfInterval(trade_ts, toIntervalSecond({int(step)}))) AS candle_open,
        toUnixTimestamp(toStartOfInterval(trade_ts, toIntervalSecond(10))) AS w10,
        toUnixTimestamp(toStartOfSecond(trade_ts)) AS sec,
        count() AS n1,
        sumIf(notional, side = 'Buy') AS buy_usdt,
        sumIf(notional, side = 'Sell') AS sell_usdt,
        min(trade_ts) AS first_ts,
        max(trade_ts) AS last_ts
      FROM {CANONICAL_FQN} FINAL
      PREWHERE symbol = {{s:String}}
      WHERE trade_ts >= {{a:DateTime64(3, 'UTC')}}
        AND trade_ts < {{b:DateTime64(3, 'UTC')}}
      GROUP BY candle_open, w10, sec
    ),
    win AS (
      SELECT
        candle_open,
        w10,
        sum(n1) AS n10,
        max(n1) AS max_1s,
        sum(buy_usdt) AS buy_usdt,
        sum(sell_usdt) AS sell_usdt,
        min(first_ts) AS first_ts,
        max(last_ts) AS last_ts
      FROM sec
      GROUP BY candle_open, w10
    )
    SELECT
      candle_open,
      sum(n10) AS prints,
      max(max_1s) AS max_1s,
      max(n10) AS max_10s,
      sum(buy_usdt) AS buy_usdt,
      sum(sell_usdt) AS sell_usdt,
      min(first_ts) AS first_ts,
      max(last_ts) AS last_ts
    FROM win
    GROUP BY candle_open
    ORDER BY candle_open
    """
    rows = _query(sql, {"s": symbol, "a": start, "b": end})
    out = []
    for open_s, prints, max_1s, max_10s, buy, sell, first_ts, last_ts in rows:
        out.append(
            {
                "time": int(open_s),
                "prints": int(prints),
                "max_1s": int(max_1s),
                "max_10s": int(max_10s),
                "buy_usdt": _dec(buy),
                "sell_usdt": _dec(sell),
                "first_ts": first_ts,
                "last_ts": last_ts,
            }
        )
    return out


def _probe(symbol: str, start: datetime, end: datetime) -> tuple[datetime | None, datetime | None]:
    left_from = start - timedelta(seconds=PROBE_S)
    right_to = end + timedelta(seconds=PROBE_S)
    sql = f"""
    SELECT
      (
        SELECT max(trade_ts)
        FROM {CANONICAL_FQN} FINAL
        PREWHERE symbol = {{s:String}}
        WHERE trade_ts >= {{lf:DateTime64(3, 'UTC')}}
          AND trade_ts < {{a:DateTime64(3, 'UTC')}}
      ),
      (
        SELECT min(trade_ts)
        FROM {CANONICAL_FQN} FINAL
        PREWHERE symbol = {{s:String}}
        WHERE trade_ts >= {{b:DateTime64(3, 'UTC')}}
          AND trade_ts < {{rt:DateTime64(3, 'UTC')}}
      )
    """
    rows = _query(sql, {"s": symbol, "lf": left_from, "a": start, "b": end, "rt": right_to})
    if not rows:
        return None, None
    return rows[0][0], rows[0][1]


def _epoch(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    return int(_utc(dt).timestamp())


def load_orderflow(
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    *,
    as_of: datetime | None = None,
    cvd_reset: str = "utc_day",
) -> dict[str, Any]:
    sym = str(symbol or "").strip().upper()
    tf = str(timeframe or "").strip().lower()
    if tf not in TF_SECONDS:
        raise ValueError("invalid_timeframe")
    reset = str(cvd_reset or "utc_day").strip().lower()
    if reset not in {"utc_day", "range_start"}:
        raise ValueError("invalid_cvd_reset")
    start, end = _utc(start), _utc(end)
    if end <= start:
        raise ValueError("invalid_time_range")
    as_of = _utc(as_of or datetime.now(timezone.utc))
    step = TF_SECONDS[tf]
    if (end - start).total_seconds() > MAX_RANGE_S:
        raise ValueError("time_range_too_large")
    read_start = start
    if reset == "utc_day":
        midnight = start.replace(hour=0, minute=0, second=0, microsecond=0)
        read_start = midnight
    query_end = min(end, as_of)
    if query_end <= read_start:
        buckets: list[dict[str, Any]] = []
    else:
        buckets = _load_buckets(sym, step, read_start, query_end)
    by_time = {b["time"]: b for b in buckets}
    grid_from = floor_open(int(read_start.timestamp()), step)
    grid_to = floor_open(int((min(end, as_of) - timedelta(milliseconds=1)).timestamp()), step)
    opens = list(range(grid_from, grid_to + step, step)) if grid_to >= grid_from else []
    needs_probe = any(t not in by_time and not (t <= int(as_of.timestamp()) < t + step) for t in opens)
    probe_left = probe_right = None
    if needs_probe and query_end > read_start:
        probe_left, probe_right = _probe(sym, read_start, query_end)
    left_hint = _epoch(probe_left)
    right_hint = _epoch(probe_right)
    measured_times = sorted(by_time)
    bars: list[dict[str, Any]] = []
    for open_s in opens:
        forming = open_s <= int(as_of.timestamp()) < open_s + step
        row = by_time.get(open_s)
        if row is not None:
            bars.append(_measured_bar(open_s, row, forming=forming))
            continue
        if forming:
            bars.append(_empty_bar(open_s, "FORMING"))
            continue
        left_candidates = [left_hint] if left_hint is not None and left_hint < open_s else []
        for t in measured_times:
            if t < open_s:
                last = by_time[t]["last_ts"]
                ep = _epoch(last)
                if ep is not None:
                    left_candidates.append(ep)
            else:
                break
        right_candidates = [right_hint] if right_hint is not None and right_hint >= open_s + step else []
        for t in measured_times:
            if t > open_s:
                first = by_time[t]["first_ts"]
                ep = _epoch(first)
                if ep is not None:
                    right_candidates.append(ep)
                break
        left_ts = max(left_candidates) if left_candidates else None
        right_ts = min(right_candidates) if right_candidates else None
        status = classify_empty(open_s, step, int(as_of.timestamp()), left_ts, right_ts)
        bars.append(_empty_bar(open_s, status))
    apply_cvd(bars, reset=reset)
    visible_from = int(start.timestamp())
    visible = [b for b in bars if int(b["time"]) >= visible_from]
    return {
        "symbol": sym,
        "timeframe": tf,
        "source": CANONICAL_FQN,
        "dedup": "FINAL",
        "cvd_reset": reset,
        "as_of": _iso(as_of),
        "coverage_proof": {
            "collector_sequence_persisted": False,
            "per_candle_health_persisted": False,
            "reason": (
                "public_trades_canonical speichert keine Stream-Sequenz. "
                "Die Spool-Sequenz und gap_detected bleiben im Collector-Prozess "
                "und werden nicht je Kerze abgelegt."
            ),
        },
        "baseline": {
            "available": False,
            "sample_count": 0,
            "p95_prints": None,
            "p95_max_10s": None,
            "reason": "Keine Kerze ist als vollständig belegt. Lücken gehen nicht als Null in eine Referenz ein.",
        },
        "bars": visible,
    }
