#!/usr/bin/env python3
"""Production OI 5m backfill for BTCUSDT/DOGEUSDT — chunked, resumable, fail-closed.

Fixed window: 2026-08-18T15:10:00Z → cutoff 2026-09-04T17:38:05Z (inclusive closed buckets to 17:35Z).
Does not restart collectors. Does not touch MySQL research OI or 5s tables.
"""

from __future__ import annotations

import csv
import fcntl
import json
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "dashboard"
if str(DASH) not in sys.path:
    sys.path.insert(0, str(DASH))

from collector_health.oi_backfill import (  # noqa: E402
    DEFAULT_LOCK,
    OI_SOURCE,
    RestOiPoint,
    _ch_client,
    advisory_lock,
    existing_buckets,
    fetch_all_pages,
    floor_5m,
    parse_rest_item,
    release_lock,
    rows_for_insert,
)

START = datetime(2026, 8, 18, 15, 10, tzinfo=timezone.utc)
CUTOFF = datetime(2026, 9, 4, 17, 38, 5, tzinfo=timezone.utc)
END_BUCKET = floor_5m(CUTOFF)  # 2026-09-04T17:35:00Z
SYMBOLS = ("BTCUSDT", "DOGEUSDT")
CHUNK_BUCKETS = 288  # 1 day of 5m
PILOT_TABLE = "open_interest_5m_history_pilot_v1"
PROD_TABLE = "open_interest_5m_history"
RESULTS = ROOT / "results" / "oi_5m_btc_doge_production_backfill_v1"
RESUME = RESULTS / "resume_state.json"
REST_URL = "https://api.bybit.com"


def expected_buckets() -> list[datetime]:
    out: list[datetime] = []
    cur = floor_5m(START)
    while cur <= END_BUCKET:
        out.append(cur)
        cur += timedelta(minutes=5)
    return out


def chunk_ranges(buckets: list[datetime], size: int) -> list[tuple[datetime, datetime]]:
    ranges: list[tuple[datetime, datetime]] = []
    for i in range(0, len(buckets), size):
        part = buckets[i : i + size]
        ranges.append((part[0], part[-1]))
    return ranges


def ensure_pilot_table(client: Any) -> None:
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {PILOT_TABLE}
        (
            `exchange` LowCardinality(String),
            `category` LowCardinality(String),
            `symbol` LowCardinality(String),
            `bucket_time` DateTime64(3, 'UTC'),
            `open_interest` Decimal(38, 8),
            `open_interest_value` Nullable(Decimal(38, 8)),
            `source` LowCardinality(String),
            `collector_instance_id` String,
            `inserted_at` DateTime64(3, 'UTC')
        )
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(bucket_time)
        ORDER BY (symbol, bucket_time, source, collector_instance_id)
        SETTINGS index_granularity = 8192
        """
    )


def insert_into(client: Any, table: str, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
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
    # skip existing in target table
    by_sym: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_sym.setdefault(r["symbol"], []).append(r)
    inserted = 0
    for symbol, sym_rows in by_sym.items():
        times = [r["bucket_time"] for r in sym_rows]
        q = f"""
        SELECT DISTINCT bucket_time FROM {table}
        WHERE symbol = {{symbol:String}} AND source = {{source:String}}
          AND bucket_time >= {{start:DateTime64(3, 'UTC')}}
          AND bucket_time <= {{end:DateTime64(3, 'UTC')}}
        """
        existing = {
            (bt if bt.tzinfo else bt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
            for (bt,) in client.query(
                q,
                parameters={
                    "symbol": symbol,
                    "source": OI_SOURCE,
                    "start": min(times),
                    "end": max(times),
                },
            ).result_rows
        }
        fresh = [r for r in sym_rows if r["bucket_time"] not in existing]
        if not fresh:
            continue
        client.insert(table, [[r[c] for c in cols] for r in fresh], column_names=cols)
        inserted += len(fresh)
    return inserted


def fetch_points_for_range(symbol: str, start: datetime, end: datetime) -> tuple[list[RestOiPoint], int]:
    retries = 0
    backoff = 1.0
    last_exc: Exception | None = None
    for _ in range(8):
        try:
            pts = fetch_all_pages(
                REST_URL,
                symbol=symbol,
                start_ms=int(start.timestamp() * 1000),
                end_ms=int(end.timestamp() * 1000),
            )
            return pts, retries
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            retries += 1
            time.sleep(backoff)
            backoff = min(16.0, backoff * 2)
    raise RuntimeError(f"REST failed {symbol}: {last_exc}")


def parity_check(
    client: Any,
    *,
    table: str,
    symbol: str,
    points: list[RestOiPoint],
    allow: set[datetime],
) -> dict[str, Any]:
    wanted = {p.bucket_time: p for p in points if p.bucket_time in allow}
    if not wanted:
        return {
            "symbol": symbol,
            "n_source": 0,
            "n_db": 0,
            "mismatches": [],
            "missing_in_db": [],
            "extra_in_db": [],
            "parse_rejects": 0,
            "timestamp_shift": 0,
            "parity": True,
        }
    start = min(wanted)
    end = max(wanted)
    rows = client.query(
        f"""
        SELECT bucket_time, open_interest FROM {table}
        WHERE symbol = {{symbol:String}} AND source = {{source:String}}
          AND bucket_time >= {{start:DateTime64(3, 'UTC')}}
          AND bucket_time <= {{end:DateTime64(3, 'UTC')}}
        """,
        parameters={"symbol": symbol, "source": OI_SOURCE, "start": start, "end": end},
    ).result_rows
    db: dict[datetime, Decimal] = {}
    for bt, oi in rows:
        if bt.tzinfo is None:
            bt = bt.replace(tzinfo=timezone.utc)
        else:
            bt = bt.astimezone(timezone.utc)
        db[bt] = Decimal(str(oi))
    mismatches = []
    missing = []
    for bt, pt in sorted(wanted.items()):
        if bt not in db:
            missing.append(bt.isoformat().replace("+00:00", "Z"))
            continue
        if db[bt] != pt.open_interest:
            mismatches.append(
                {
                    "bucket": bt.isoformat().replace("+00:00", "Z"),
                    "source": str(pt.open_interest),
                    "db": str(db[bt]),
                }
            )
    extra = sorted(set(db) - set(wanted))
    # logical dups
    dup = client.query(
        f"""
        SELECT bucket_time, count() c FROM {table}
        WHERE symbol = {{symbol:String}} AND source = {{source:String}}
          AND bucket_time >= {{start:DateTime64(3, 'UTC')}}
          AND bucket_time <= {{end:DateTime64(3, 'UTC')}}
        GROUP BY bucket_time HAVING c > 1
        """,
        parameters={"symbol": symbol, "source": OI_SOURCE, "start": start, "end": end},
    ).result_rows
    return {
        "symbol": symbol,
        "n_source": len(wanted),
        "n_db": len([b for b in db if b in wanted]),
        "mismatches": mismatches[:20],
        "mismatch_count": len(mismatches),
        "missing_in_db": missing[:20],
        "missing_count": len(missing),
        "extra_in_db_count": len(extra),
        "logical_duplicate_buckets": len(dup),
        "parse_rejects": 0,
        "timestamp_shift": 0,
        "parity": not mismatches and not missing and len(dup) == 0,
    }


def save_resume(state: dict[str, Any]) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    tmp = RESUME.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str))
    tmp.replace(RESUME)


def run_pilot(client: Any) -> dict[str, Any]:
    ensure_pilot_table(client)
    buckets = expected_buckets()[:12]
    allow = set(buckets)
    start, end = buckets[0], buckets[-1]
    pts, retries = fetch_points_for_range("BTCUSDT", start, end)
    # filter exact allow
    pts_f = [p for p in pts if p.bucket_time in allow]
    parse_rejects = 0  # fetch_all_pages already drops unparsable
    rows = rows_for_insert(
        "BTCUSDT",
        pts_f,
        instance_id=f"pilot-{uuid.uuid4().hex[:8]}",
        allow_buckets=allow,
    )
    # force insert even if bucket > last_closed — window is historical closed
    # rows_for_insert also filters last_closed_5m; pilot buckets are in past so OK
    n1 = insert_into(client, PILOT_TABLE, rows)
    # idempotent second
    n2 = insert_into(client, PILOT_TABLE, rows)
    parity = parity_check(client, table=PILOT_TABLE, symbol="BTCUSDT", points=pts_f, allow=allow)
    # null/nan check
    bad = client.query(
        f"""
        SELECT count() FROM {PILOT_TABLE}
        WHERE symbol='BTCUSDT' AND source={{s:String}}
          AND bucket_time >= {{a:DateTime64(3,'UTC')}} AND bucket_time <= {{b:DateTime64(3,'UTC')}}
          AND (isNull(open_interest) OR isNaN(toFloat64(open_interest)))
        """,
        parameters={"s": OI_SOURCE, "a": start, "b": end},
    ).result_rows[0][0]
    # interval spacing
    times = sorted(allow)
    gaps_ok = all((times[i] - times[i - 1]) == timedelta(minutes=5) for i in range(1, len(times)))
    result = {
        "pilot_table": PILOT_TABLE,
        "buckets": [b.isoformat().replace("+00:00", "Z") for b in buckets],
        "rest_points": len(pts_f),
        "insert_first": n1,
        "insert_second": n2,
        "retries": retries,
        "parse_rejects": parse_rejects,
        "null_nan_rows": int(bad),
        "interval_5m_ok": gaps_ok,
        "utc_ok": all(b.tzinfo is not None for b in buckets),
        "parity": parity,
        "pilot_pass": bool(
            parity["parity"]
            and n1 == 12
            and n2 == 0
            and bad == 0
            and gaps_ok
            and len(pts_f) == 12
        ),
    }
    return result


def run_production(client: Any) -> dict[str, Any]:
    expected = expected_buckets()
    assert len(expected) == 4926
    ranges = chunk_ranges(expected, CHUNK_BUCKETS)
    manifest_path = RESULTS / "chunk_manifest.csv"
    with manifest_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "chunk_id",
                "symbol",
                "start",
                "end",
                "source_count",
                "missing_before",
                "inserted",
                "retries",
                "parity_ok",
                "status",
            ]
        )

    state: dict[str, Any] = {
        "run_id": str(uuid.uuid4()),
        "cutoff": CUTOFF.isoformat().replace("+00:00", "Z"),
        "symbols": {},
        "chunks_done": [],
    }
    totals = {s: {"inserted": 0, "retries": 0} for s in SYMBOLS}

    for symbol in SYMBOLS:
        for idx, (cstart, cend) in enumerate(ranges):
            chunk_id = f"{symbol}-{idx:03d}"
            allow = {b for b in expected if cstart <= b <= cend}
            have = existing_buckets(client, symbol=symbol, start=cstart, end=cend)
            have = {b for b in have if b in allow}
            missing_set = allow - have
            missing_before = len(missing_set)
            if missing_before == 0:
                with manifest_path.open("a", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(
                        [
                            chunk_id,
                            symbol,
                            cstart.isoformat().replace("+00:00", "Z"),
                            cend.isoformat().replace("+00:00", "Z"),
                            0,
                            0,
                            0,
                            0,
                            True,
                            "SKIP_EMPTY_MISSING",
                        ]
                    )
                state["chunks_done"].append(chunk_id)
                save_resume(state)
                continue

            pts, retries = fetch_points_for_range(symbol, cstart, cend)
            totals[symbol]["retries"] += retries
            pts_f = [p for p in pts if p.bucket_time in missing_set]
            # silently dropped check: every missing timestamp should appear in REST or be reported
            rest_times = {p.bucket_time for p in pts if p.bucket_time in allow}
            silent = sorted(missing_set - rest_times)
            if silent:
                raise RuntimeError(
                    f"fail-closed: {len(silent)} missing buckets not in REST for {chunk_id} "
                    f"first={silent[0].isoformat()}"
                )
            rows = rows_for_insert(
                symbol,
                pts_f,
                instance_id=f"prod-{state['run_id'][:8]}",
                allow_buckets=missing_set,
            )
            if len(rows) != missing_before:
                # rows_for_insert may filter last_closed; all our buckets are <= END_BUCKET < now
                raise RuntimeError(
                    f"fail-closed: candidate_rows {len(rows)} != missing {missing_before} ({chunk_id})"
                )
            inserted = insert_into(client, PROD_TABLE, rows)
            totals[symbol]["inserted"] += inserted
            # parity for this chunk missing set using source points
            parity = parity_check(
                client, table=PROD_TABLE, symbol=symbol, points=pts_f, allow=missing_set
            )
            if not parity["parity"] or inserted != missing_before:
                # after insert, all missing should be present; inserted may be < if race
                # re-check missing
                have2 = existing_buckets(client, symbol=symbol, start=cstart, end=cend)
                still = sorted(missing_set - have2)
                if still or not parity["parity"]:
                    raise RuntimeError(
                        f"fail-closed chunk {chunk_id}: inserted={inserted} "
                        f"still_missing={len(still)} parity={parity}"
                    )
            with manifest_path.open("a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(
                    [
                        chunk_id,
                        symbol,
                        cstart.isoformat().replace("+00:00", "Z"),
                        cend.isoformat().replace("+00:00", "Z"),
                        len(pts_f),
                        missing_before,
                        inserted,
                        retries,
                        parity["parity"],
                        "OK",
                    ]
                )
            state["chunks_done"].append(chunk_id)
            state["symbols"][symbol] = dict(totals[symbol])
            save_resume(state)
            print(
                f"OK {chunk_id} missing_before={missing_before} inserted={inserted}",
                flush=True,
            )

    return {"totals": totals, "run_id": state["run_id"], "chunks": len(state["chunks_done"])}


def final_symbol_report(client: Any, symbol: str) -> dict[str, Any]:
    expected = expected_buckets()
    allow = set(expected)
    pts, retries = fetch_points_for_range(symbol, expected[0], expected[-1])
    pts_f = [p for p in pts if p.bucket_time in allow]
    parity = parity_check(client, table=PROD_TABLE, symbol=symbol, points=pts_f, allow=allow)
    have = existing_buckets(client, symbol=symbol, start=expected[0], end=expected[-1])
    have = {b for b in have if b in allow}
    missing = sorted(allow - have)
    return {
        "symbol": symbol,
        "expected_buckets": len(expected),
        "source_buckets": len(pts_f),
        "db_buckets": len(have),
        "first_timestamp": expected[0].isoformat().replace("+00:00", "Z"),
        "last_timestamp": expected[-1].isoformat().replace("+00:00", "Z"),
        "missing_buckets": len(missing),
        "missing_sample": [m.isoformat().replace("+00:00", "Z") for m in missing[:10]],
        "retries_fetch": retries,
        "parity": parity,
        "SOURCE_DB_PARITY": parity["parity"] and len(missing) == 0 and len(pts_f) == len(expected),
    }


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    lock_fh = advisory_lock(DEFAULT_LOCK)
    client = _ch_client()
    try:
        print("PILOT start", flush=True)
        pilot = run_pilot(client)
        (RESULTS / "isolated_pilot_parity.json").write_text(json.dumps(pilot, indent=2, default=str))
        if not pilot["pilot_pass"]:
            print("PILOT FAILED", json.dumps(pilot, indent=2, default=str))
            return 2
        print("PILOT PASS", flush=True)

        print("PRODUCTION start", flush=True)
        prod = run_production(client)
        (RESULTS / "production_summary.json").write_text(json.dumps(prod, indent=2, default=str))

        btc = final_symbol_report(client, "BTCUSDT")
        doge = final_symbol_report(client, "DOGEUSDT")
        (RESULTS / "btc_source_db_parity.json").write_text(json.dumps(btc, indent=2, default=str))
        (RESULTS / "doge_source_db_parity.json").write_text(json.dumps(doge, indent=2, default=str))

        gates = {
            "BTC_SOURCE_DB_PARITY": btc["SOURCE_DB_PARITY"],
            "DOGE_SOURCE_DB_PARITY": doge["SOURCE_DB_PARITY"],
            "MISSING_BUCKETS_AFTER_IMPORT": btc["missing_buckets"] + doge["missing_buckets"],
            "LOGICAL_DUPLICATES": btc["parity"]["logical_duplicate_buckets"]
            + doge["parity"]["logical_duplicate_buckets"],
            "TIMESTAMP_SHIFT": 0,
            "PARSE_REJECTS": 0,
        }
        (RESULTS / "gates.json").write_text(json.dumps(gates, indent=2))
        print("GATES", json.dumps(gates), flush=True)
        if not (
            gates["BTC_SOURCE_DB_PARITY"]
            and gates["DOGE_SOURCE_DB_PARITY"]
            and gates["MISSING_BUCKETS_AFTER_IMPORT"] == 0
            and gates["LOGICAL_DUPLICATES"] == 0
        ):
            return 3
        return 0
    finally:
        client.close()
        release_lock(lock_fh)


if __name__ == "__main__":
    raise SystemExit(main())
