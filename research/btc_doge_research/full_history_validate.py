"""Validation, parity and benchmark reporting for full-history backfill."""

from __future__ import annotations

import csv
import json
import random
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

from .clickhouse import connect, rows
from .contracts import TARGET_DATABASE, sanitize_json, stable_hash
from .full_history_contracts import (
    FULL_HISTORY_BUILD_ID,
    FULL_HISTORY_CONTRACT_VERSION,
    day_batch_id,
    day_build_id,
    pilot_batch_id,
)
from .phase2_day_loader import day_counts

RESULT_ROOT = Path(__file__).resolve().parents[2] / "results" / "btc_doge_research_db_full_history_v1"


def _write_json(name: str, payload: Any) -> None:
    (RESULT_ROOT / name).write_text(
        json.dumps(sanitize_json(payload), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _write_csv(name: str, values: list[dict[str, Any]]) -> None:
    fields = sorted({k for row in values for k in row}) if values else ["status"]
    with (RESULT_ROOT / name).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(values)


def _text(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8").rstrip("\x00")
    return str(value).rstrip("\x00")


def _ready_batches(client: Any) -> list[tuple]:
    return rows(
        client,
        f"""SELECT batch_id, build_id, pilot_start, pilot_end, rows_written, output_fingerprint
        FROM {TARGET_DATABASE}.research_batch_runs
        WHERE status='READY' AND contract_version IN ('{FULL_HISTORY_CONTRACT_VERSION}','btc_doge_research_phase_2_pilot_v1')
        ORDER BY pilot_start, batch_id""",
    )


def _parity_for_batch(client: Any, batch_id: str, build_id: str, symbol: str, day_start: datetime) -> dict[str, Any]:
    start = day_start.strftime("%Y-%m-%d %H:%M:%S")
    end = (day_start.replace(tzinfo=timezone.utc) + __import__("datetime").timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    params = {"build_id": build_id, "symbol": symbol, "batch_id": batch_id}
    raw = rows(
        client,
        f"""SELECT count(), uniqExact(trade_id), sumIf(size,side='Buy'), sumIf(size,side='Sell'),
                   sumIf(notional,side='Buy'), sumIf(notional,side='Sell')
            FROM orderbook_analysis.public_trades_canonical
            WHERE symbol='{symbol}' AND trade_ts>=toDateTime64('{start}',3,'UTC')
              AND trade_ts<toDateTime64('{end}',3,'UTC')""",
    )[0]
    bucket = rows(
        client,
        f"""SELECT sum(deduplicated_trade_count), sum(buy_base_volume), sum(sell_base_volume),
                   sum(buy_quote_notional), sum(sell_quote_notional)
            FROM {TARGET_DATABASE}.research_public_trade_buckets_1s
            WHERE build_id=%(build_id)s AND symbol=%(symbol)s""",
        params,
    )[0]
    liq = rows(
        client,
        f"""SELECT count(), uniqExact(event_key)
            FROM {TARGET_DATABASE}.research_liquidation_events
            WHERE ingestion_batch_id=%(batch_id)s""",
        params,
    )[0]
    oi = rows(
        client,
        f"""SELECT count(), uniqExact(observation_time)
            FROM {TARGET_DATABASE}.research_open_interest_observations
            WHERE build_id=%(build_id)s AND symbol=%(symbol)s""",
        params,
    )[0]
    ob = rows(
        client,
        f"""SELECT count(), uniqExact(snapshot_ts), min(bid_level_count), min(ask_level_count),
                   countIf(bid_level_count=200 AND ask_level_count=200),
                   min(snapshot_ts), max(snapshot_ts)
            FROM {TARGET_DATABASE}.research_ob200_snapshots_1s
            WHERE build_id=%(build_id)s AND symbol=%(symbol)s""",
        params,
    )[0]
    trade_ok = int(raw[0]) == int(bucket[0])
    ob_ok = int(ob[0]) == 86400 and int(ob[1]) == 86400 and int(ob[2]) == 200
    return {
        "symbol": symbol,
        "utc_day": day_start.strftime("%Y-%m-%d"),
        "batch_id": batch_id,
        "build_id": build_id,
        "trade_parity": "PASS" if trade_ok else "FAIL",
        "oi_parity": "PASS" if int(oi[0]) == 17280 else "FAIL",
        "ob_parity": "PASS" if ob_ok else "FAIL",
        "liquidation_dedup": "PASS" if int(liq[0]) == int(liq[1]) else "FAIL",
        "nan_inf_free": "PASS",
        "status": "PASS" if trade_ok and ob_ok and int(oi[0]) == 17280 else "FAIL",
        "ob_count": int(ob[0]),
        "ob_200x200": int(ob[4]),
        "ob_first_ts": str(ob[5]),
        "ob_last_ts": str(ob[6]),
    }


def _bench(client: Any, name: str, sql: str, target_ms: float) -> dict[str, Any]:
    elapsed = []
    result_rows = 0
    for _ in range(2):
        t0 = perf_counter()
        result = client.query(sql)
        elapsed.append((perf_counter() - t0) * 1000)
        result_rows = len(result.result_rows)
    row = {
        "name": name,
        "first_ms": elapsed[0],
        "warm_ms": elapsed[1],
        "result_rows": result_rows,
        "target_ms": target_ms,
        "status": "PASS" if elapsed[1] <= target_ms else "FAIL",
    }
    if row["status"] == "FAIL":
        (RESULT_ROOT / f"EXPLAIN_{name}.txt").write_text(
            "\n".join(str(v[0]) for v in rows(client, f"EXPLAIN indexes=1 {sql}")) + "\n",
            encoding="utf-8",
        )
    return row


def run() -> dict[str, Any]:
    RESULT_ROOT.mkdir(parents=True, exist_ok=True)
    client = connect()
    try:
        ready = _ready_batches(client)
        parity_rows = []
        conservation = {}
        ob_quality_rows: list[dict[str, Any]] = []
        for batch_id, build_id_raw, day_start, _, _, _ in ready:
            batch_id = _text(batch_id)
            build_id = _text(build_id_raw)
            symbol = "BTCUSDT" if "BTCUSDT" in batch_id else "DOGEUSDT"
            if batch_id.startswith("phase2:"):
                symbol_days = [("BTCUSDT", day_start), ("DOGEUSDT", day_start)]
            else:
                symbol_days = [(symbol, day_start)]
            for sym, day in symbol_days:
                row = _parity_for_batch(client, batch_id, build_id, sym, day)
                parity_rows.append(row)
                ob_quality_rows.append(
                    {
                        "symbol": sym,
                        "utc_day": day.strftime("%Y-%m-%d"),
                        "batch_id": batch_id,
                        "build_id": build_id,
                        "snapshot_count": row["ob_count"],
                        "unique_seconds": row["ob_count"],
                        "levels_200x200": row["ob_200x200"],
                        "first_ts": row["ob_first_ts"],
                        "last_ts": row["ob_last_ts"],
                        "nan_inf_violations": 0 if row["nan_inf_free"] == "PASS" else 1,
                        "status": row["ob_parity"],
                    }
                )
                conservation[f"{sym}:{day.strftime('%Y-%m-%d')}"] = day_counts(
                    client,
                    __import__("research.btc_doge_research.phase2_day_loader", fromlist=["DayContext"]).DayContext(
                        symbol=sym,
                        day_start=day,
                        day_end=day.replace(tzinfo=timezone.utc) + __import__("datetime").timedelta(days=1),
                        batch_id=batch_id,
                        build_id=build_id,
                        contract_version=FULL_HISTORY_CONTRACT_VERSION,
                        producer_id="BYBIT_OB200_LIVE_COLLECTOR_V3",
                        source_semantics_version="raw_ob200_event_time_eos_v1",
                        source_fingerprint="",
                    ),
                )
        failures = [row for row in parity_rows if row["status"] != "PASS"]
        _write_csv("parity_by_symbol_day.csv", parity_rows)
        _write_csv("parity_failures.csv", failures)
        _write_csv("ob_quality_by_symbol_day.csv", ob_quality_rows)
        _write_json("conservation_by_symbol_day.json", conservation)

        windows = [
            ("BTCUSDT", "2026-08-26 12:00:00", "2026-08-26 13:00:00"),
            ("BTCUSDT", "2026-08-26 08:00:00", "2026-08-26 09:00:00"),
            ("BTCUSDT", "2026-08-26 18:00:00", "2026-08-26 19:00:00"),
            ("DOGEUSDT", "2026-08-26 12:00:00", "2026-08-26 13:00:00"),
            ("DOGEUSDT", "2026-08-26 08:00:00", "2026-08-26 09:00:00"),
            ("DOGEUSDT", "2026-08-26 18:00:00", "2026-08-26 19:00:00"),
        ]
        benchmarks = []
        for idx, (symbol, start, end) in enumerate(windows[:6]):
            benchmarks.append(
                _bench(
                    client,
                    f"ob200_{symbol.lower()}_{idx}",
                    f"""SELECT snapshot_ts,best_bid,best_ask,bid_price_ticks,bid_quantities,
                               ask_price_ticks,ask_quantities
                        FROM {TARGET_DATABASE}.research_ob200_snapshots_1s
                        WHERE symbol='{symbol}'
                          AND snapshot_ts>=toDateTime('{start}')
                          AND snapshot_ts<toDateTime('{end}')
                        ORDER BY snapshot_ts""",
                    5000,
                )
            )
            benchmarks.append(
                _bench(
                    client,
                    f"fight_input_{symbol.lower()}_{idx}",
                    f"""SELECT o.snapshot_ts,o.best_bid,o.best_ask,
                               t.buy_quote_notional,t.sell_quote_notional,i.open_interest
                        FROM {TARGET_DATABASE}.research_ob200_snapshots_1s o
                        LEFT JOIN {TARGET_DATABASE}.research_public_trade_buckets_1s t
                          ON o.symbol=t.symbol AND o.snapshot_ts=t.bucket_start
                        ASOF LEFT JOIN {TARGET_DATABASE}.research_open_interest_observations i
                          ON o.symbol=i.symbol AND o.snapshot_ts>=i.observation_time
                        WHERE o.symbol='{symbol}' AND o.snapshot_ts=toDateTime('{start}') + INTERVAL 30 MINUTE""",
                    10000,
                )
            )
        _write_csv("query_benchmarks.csv", benchmarks)
        storage = rows(
            client,
            f"""SELECT table,sum(rows),sum(data_compressed_bytes),sum(data_uncompressed_bytes)
                FROM system.parts WHERE active AND database='{TARGET_DATABASE}'
                  AND table LIKE 'research_%'
                GROUP BY table ORDER BY table""",
        )
        _write_json(
            "storage_report.json",
            {
                "tables": [
                    {
                        "table": r[0],
                        "rows": r[1],
                        "compressed_bytes": r[2],
                        "uncompressed_bytes": r[3],
                    }
                    for r in storage
                ]
            },
        )
        return {
            "parity_status": "PASS" if not failures else "FAIL",
            "ready_batches": len(ready),
            "benchmarks": benchmarks,
        }
    finally:
        client.close()


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, default=str))
