"""Idempotent Bybit REST 5m OI backfill targeting CH open_interest_5m_history.

Default mode is dry-run. Never writes MySQL research_open_interest_5m.
Never synthesizes 5s history from 5m rows.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import random
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import OI_GRANULARITY, OI_SOT_DATABASE, OI_SOT_TABLE, OI_SOURCE
from .ch_config import load_orderbook_ch_config

logger = logging.getLogger(__name__)

BUCKET_S = 300
REST_URL_DEFAULT = "https://api.bybit.com"
DEFAULT_LOCK = Path("/tmp/oi_5m_history_backfill.lock")
MAX_UI_SPAN_DAYS = 14
ALLOWED_SYMBOL_RE = __import__("re").compile(r"^[A-Z0-9]{3,20}$")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware UTC")
        return value.astimezone(timezone.utc)
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        raise ValueError(f"timestamp must be timezone-aware UTC: {value!r}")
    return dt.astimezone(timezone.utc)


def floor_5m(ts: datetime) -> datetime:
    ts = ts.astimezone(timezone.utc)
    epoch = int(ts.timestamp())
    floored = epoch - (epoch % BUCKET_S)
    return datetime.fromtimestamp(floored, tz=timezone.utc)


def last_closed_5m(*, now: datetime | None = None) -> datetime:
    """Latest fully closed 5m bucket start (exclusive of current forming bucket)."""
    n = now or utc_now()
    floored = floor_5m(n)
    # If exactly on boundary, previous bucket is last closed; else floor is open → subtract 5m
    if int(n.timestamp()) % BUCKET_S == 0:
        return floored - timedelta(seconds=BUCKET_S)
    return floored - timedelta(seconds=BUCKET_S)


def expected_closed_buckets(start: datetime, end: datetime) -> list[datetime]:
    """Inclusive start/end as bucket starts; only closed buckets ≤ last_closed_5m()."""
    start_b = floor_5m(start)
    end_b = floor_5m(end)
    closed = last_closed_5m()
    if end_b > closed:
        end_b = closed
    if start_b > end_b:
        return []
    out: list[datetime] = []
    cur = start_b
    while cur <= end_b:
        out.append(cur)
        cur += timedelta(seconds=BUCKET_S)
    return out


def normalize_symbol(symbol: str) -> str:
    s = str(symbol).strip().upper()
    if not ALLOWED_SYMBOL_RE.match(s):
        raise ValueError(f"invalid symbol: {symbol!r}")
    return s


@dataclass(frozen=True)
class RestOiPoint:
    timestamp_ms: int
    open_interest: Decimal
    single_open_interest: Decimal | None
    bucket_time: datetime


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def parse_rest_item(item: dict[str, Any]) -> RestOiPoint | None:
    ts = item.get("timestamp")
    oi = _to_decimal(item.get("openInterest"))
    if ts is None or oi is None:
        return None
    ts_i = int(ts)
    bucket = datetime.fromtimestamp(ts_i / 1000.0, tz=timezone.utc)
    single = _to_decimal(item.get("singleOpenInterest"))
    return RestOiPoint(
        timestamp_ms=ts_i,
        open_interest=oi,
        single_open_interest=single,
        bucket_time=bucket,
    )


def fetch_open_interest_page(
    rest_url: str,
    *,
    symbol: str,
    start_ms: int,
    end_ms: int,
    cursor: str = "",
    timeout_s: float = 30.0,
) -> tuple[list[dict[str, Any]], str]:
    params = {
        "category": "linear",
        "symbol": symbol,
        "intervalTime": "5min",
        "startTime": str(start_ms),
        "endTime": str(end_ms),
        "limit": "200",
    }
    if cursor:
        params["cursor"] = cursor
    url = rest_url.rstrip("/") + "/v5/market/open-interest?" + urlencode(params)
    req = Request(url, headers={"User-Agent": "oi-5m-backfill/collector-health-v1"})
    with urlopen(req, timeout=timeout_s) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if payload.get("retCode") not in (0, None):
        raise RuntimeError(payload.get("retMsg") or "open-interest failed")
    result = payload.get("result") or {}
    return list(result.get("list") or []), str(result.get("nextPageCursor") or "").strip()


def fetch_all_pages(
    rest_url: str,
    *,
    symbol: str,
    start_ms: int,
    end_ms: int,
    min_interval_sec: float = 0.25,
    max_retries: int = 8,
) -> list[RestOiPoint]:
    cursor = ""
    seen_cursors: set[str] = set()
    points: dict[int, RestOiPoint] = {}
    while True:
        backoff = 1.0
        items: list[dict[str, Any]] = []
        next_cursor = ""
        for attempt in range(max_retries):
            try:
                items, next_cursor = fetch_open_interest_page(
                    rest_url,
                    symbol=symbol,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    cursor=cursor,
                )
                break
            except HTTPError as exc:
                if exc.code == 429 or exc.code >= 500:
                    time.sleep(backoff + random.random() * 0.3)
                    backoff = min(16.0, backoff * 2)
                    continue
                raise
            except (URLError, TimeoutError, OSError, RuntimeError):
                time.sleep(backoff + random.random() * 0.3)
                backoff = min(16.0, backoff * 2)
        else:
            raise RuntimeError(f"REST pagination failed for {symbol}")
        for item in items:
            if not isinstance(item, dict):
                continue
            pt = parse_rest_item(item)
            if pt is None:
                continue
            points[pt.timestamp_ms] = pt
        if not next_cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor
        time.sleep(min_interval_sec + random.random() * 0.05)
    return [points[k] for k in sorted(points)]


def advisory_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        fh.close()
        raise RuntimeError(f"another OI backfill holds {path}") from exc
    fh.seek(0)
    fh.truncate()
    fh.write(f"{os_getpid()}\n")
    fh.flush()
    return fh


def os_getpid() -> int:
    import os

    return os.getpid()


def release_lock(fh: Any) -> None:
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


def _ch_client(database: str = OI_SOT_DATABASE):
    import clickhouse_connect

    cfg = load_orderbook_ch_config()
    return clickhouse_connect.get_client(
        host=cfg.host,
        port=cfg.port,
        username=cfg.user,
        password=cfg.password,
        database=database,
    )


def existing_buckets(
    client: Any,
    *,
    symbol: str,
    start: datetime,
    end: datetime,
) -> set[datetime]:
    q = """
    SELECT DISTINCT bucket_time
    FROM open_interest_5m_history
    WHERE symbol = {symbol:String}
      AND source = {source:String}
      AND bucket_time >= {start:DateTime64(3, 'UTC')}
      AND bucket_time <= {end:DateTime64(3, 'UTC')}
    """
    rows = client.query(
        q,
        parameters={
            "symbol": symbol,
            "source": OI_SOURCE,
            "start": start,
            "end": end,
        },
    ).result_rows
    out: set[datetime] = set()
    for (bt,) in rows:
        if bt.tzinfo is None:
            bt = bt.replace(tzinfo=timezone.utc)
        else:
            bt = bt.astimezone(timezone.utc)
        out.add(bt)
    return out


def detect_gaps(
    client: Any,
    *,
    symbols: Iterable[str],
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    expected = expected_closed_buckets(start, end)
    expected_set = set(expected)
    per_symbol: dict[str, Any] = {}
    total_missing = 0
    for symbol in symbols:
        have = existing_buckets(client, symbol=symbol, start=start, end=end)
        # Only compare within expected closed set
        have_closed = {b for b in have if b in expected_set}
        missing = sorted(expected_set - have_closed)
        total_missing += len(missing)
        per_symbol[symbol] = {
            "expected_buckets": len(expected),
            "present_buckets": len(have_closed),
            "missing_buckets": len(missing),
            "missing_starts": [m.isoformat().replace("+00:00", "Z") for m in missing[:50]],
            "missing_starts_truncated": len(missing) > 50,
            "coverage_status": (
                "COMPLETE"
                if not missing
                else ("PARTIAL" if have_closed else "EMPTY")
            ),
            "source": OI_SOURCE,
            "granularity": OI_GRANULARITY,
        }
    return {
        "table": f"{OI_SOT_DATABASE}.{OI_SOT_TABLE}",
        "source": OI_SOURCE,
        "granularity": OI_GRANULARITY,
        "start": start.isoformat().replace("+00:00", "Z"),
        "end": end.isoformat().replace("+00:00", "Z"),
        "last_closed_5m": last_closed_5m().isoformat().replace("+00:00", "Z"),
        "expected_buckets_per_symbol": len(expected),
        "total_missing_buckets": total_missing,
        "symbols": per_symbol,
    }


def rows_for_insert(
    symbol: str,
    points: Iterable[RestOiPoint],
    *,
    instance_id: str,
    allow_buckets: set[datetime] | None,
) -> list[dict[str, Any]]:
    now = utc_now()
    rows: list[dict[str, Any]] = []
    for pt in points:
        if allow_buckets is not None and pt.bucket_time not in allow_buckets:
            continue
        # Only closed buckets
        if pt.bucket_time > last_closed_5m():
            continue
        rows.append(
            {
                "exchange": "BYBIT",
                "category": "linear",
                "symbol": symbol,
                "bucket_time": pt.bucket_time,
                "open_interest": pt.open_interest,
                "open_interest_value": None,
                "source": OI_SOURCE,
                "collector_instance_id": instance_id,
                "inserted_at": now,
            }
        )
    return rows


def insert_rows(client: Any, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    # Idempotent: skip buckets already present (per symbol batch)
    by_sym: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_sym.setdefault(r["symbol"], []).append(r)
    inserted = 0
    cols = [
        "exchange",
        "category",
        "symbol",
        "bucket_time",
        "open_interest",
        "open_interest_value",
        "source",
        "collector_instance_id",
        "inserted_at",
    ]
    for symbol, sym_rows in by_sym.items():
        times = [r["bucket_time"] for r in sym_rows]
        existing = existing_buckets(
            client,
            symbol=symbol,
            start=min(times),
            end=max(times),
        )
        fresh = [r for r in sym_rows if r["bucket_time"] not in existing]
        if not fresh:
            continue
        client.insert(
            OI_SOT_TABLE,
            [[r[c] for c in cols] for r in fresh],
            column_names=cols,
        )
        inserted += len(fresh)
    return inserted


def run_backfill(
    *,
    symbols: list[str],
    start: datetime,
    end: datetime,
    dry_run: bool = True,
    detect_only: bool = False,
    verify_only: bool = False,
    rest_url: str = REST_URL_DEFAULT,
    lock_path: Path = DEFAULT_LOCK,
    run_id: str | None = None,
) -> dict[str, Any]:
    run_id = run_id or str(uuid.uuid4())
    symbols_n = [normalize_symbol(s) for s in symbols]
    if not symbols_n:
        raise ValueError("symbols required")
    span_days = (end - start).total_seconds() / 86400.0
    if span_days < 0:
        raise ValueError("end before start")
    if span_days > MAX_UI_SPAN_DAYS * 4:
        # CLI may be longer; hard cap 60d for safety in this module
        if span_days > 60:
            raise ValueError("span exceeds 60 days hard cap")

    lock_fh = advisory_lock(lock_path)
    try:
        client = _ch_client()
        try:
            gap_report = detect_gaps(client, symbols=symbols_n, start=start, end=end)
            summary: dict[str, Any] = {
                "run_id": run_id,
                "dry_run": dry_run,
                "detect_only": detect_only,
                "verify_only": verify_only,
                "table": f"{OI_SOT_DATABASE}.{OI_SOT_TABLE}",
                "source": OI_SOURCE,
                "granularity": OI_GRANULARITY,
                "gap_report": gap_report,
                "symbols": {},
                "inserted_total": 0,
                "would_insert_total": 0,
            }
            if verify_only or detect_only:
                summary["status"] = "OK"
                return summary

            instance_id = f"oi5m-backfill-{run_id[:8]}"
            start_ms = int(start.timestamp() * 1000)
            end_ms = int(end.timestamp() * 1000)
            for symbol in symbols_n:
                missing = gap_report["symbols"][symbol]["missing_buckets"]
                missing_starts = [
                    parse_utc(x)
                    for x in gap_report["symbols"][symbol]["missing_starts"]
                ]
                # If truncated list, recompute full missing set
                if gap_report["symbols"][symbol].get("missing_starts_truncated"):
                    expected = set(expected_closed_buckets(start, end))
                    have = existing_buckets(client, symbol=symbol, start=start, end=end)
                    missing_starts = sorted(expected - have)
                    missing = len(missing_starts)
                allow = set(missing_starts)
                points = fetch_all_pages(
                    rest_url, symbol=symbol, start_ms=start_ms, end_ms=end_ms
                )
                rows = rows_for_insert(
                    symbol, points, instance_id=instance_id, allow_buckets=allow
                )
                sym_out: dict[str, Any] = {
                    "rest_points": len(points),
                    "missing_buckets": missing,
                    "candidate_rows": len(rows),
                    "inserted": 0,
                }
                if dry_run:
                    summary["would_insert_total"] += len(rows)
                else:
                    n = insert_rows(client, rows)
                    sym_out["inserted"] = n
                    summary["inserted_total"] += n
                # verify coverage after
                after = detect_gaps(client, symbols=[symbol], start=start, end=end)
                sym_out["coverage_after"] = after["symbols"][symbol]["coverage_status"]
                sym_out["missing_after"] = after["symbols"][symbol]["missing_buckets"]
                summary["symbols"][symbol] = sym_out
            summary["status"] = "OK"
            summary["live_seam_note"] = (
                "Only closed 5m buckets ≤ last_closed_5m; live 5s/events untouched; "
                "MySQL research_open_interest_5m untouched"
            )
            return summary
        finally:
            client.close()
    finally:
        release_lock(lock_fh)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Bybit REST 5m OI → ClickHouse open_interest_5m_history (dry-run default)"
    )
    p.add_argument("--symbols", required=True, help="Comma-separated symbols")
    p.add_argument("--start", required=True, help="UTC start ISO-8601")
    p.add_argument("--end", required=True, help="UTC end ISO-8601")
    p.add_argument("--dry-run", action="store_true", default=True)
    p.add_argument("--execute", action="store_true", help="Actually insert (disables dry-run)")
    p.add_argument("--detect-gaps", action="store_true")
    p.add_argument("--backfill-missing", action="store_true")
    p.add_argument("--verify-only", action="store_true")
    p.add_argument("--rest-url", default=REST_URL_DEFAULT)
    p.add_argument("--lock-path", type=Path, default=DEFAULT_LOCK)
    p.add_argument("--log-level", default="INFO")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    start = parse_utc(args.start)
    end = parse_utc(args.end)
    dry_run = not args.execute
    if args.backfill_missing and not args.execute:
        dry_run = True
    try:
        summary = run_backfill(
            symbols=symbols,
            start=start,
            end=end,
            dry_run=dry_run,
            detect_only=args.detect_gaps and not args.backfill_missing and not args.execute,
            verify_only=args.verify_only,
            rest_url=args.rest_url,
            lock_path=args.lock_path,
        )
    except Exception as exc:
        logger.error("OI backfill failed: %s", exc)
        print(json.dumps({"status": "ERROR", "error": str(exc)}, indent=2))
        return 1
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
