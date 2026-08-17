#!/usr/bin/env python3
"""DOGEUSDT + LITUSDT 2-day public-trade pilot (no collector changes).

Examples:
  python scripts/run_public_trades_pilot.py --mode dry-run
  python scripts/run_public_trades_pilot.py --mode import
  python scripts/run_public_trades_pilot.py --mode reimport
  python scripts/run_public_trades_pilot.py --mode resume-abort --abort-after-rows 5000
  python scripts/run_public_trades_pilot.py --mode resume-finish
  python scripts/run_public_trades_pilot.py --mode audit
  python scripts/run_public_trades_pilot.py --mode all
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import resource
import sys
import time
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signal_generator.bybit.public_trades.audit import (  # noqa: E402
    VOLUME_REL_TOL,
    reconcile_symbol_day,
    summarize_classes,
)
from signal_generator.bybit.public_trades.downloader import (  # noqa: E402
    DISK_FREE_MIN_BYTES,
    HttpxTransport,
    assert_disk_free,
)
from signal_generator.bybit.public_trades.importer import (  # noqa: E402
    AbortAfterRows,
    PublicTradeImporter,
    stream_parse_file,
)
from signal_generator.bybit.public_trades.manifest import ManifestStore, write_json  # noqa: E402
from signal_generator.bybit.public_trades.storage import project_storage  # noqa: E402
from signal_generator.bybit.public_trades.urls import daily_url, utc_day_bounds  # noqa: E402
from signal_generator.config import get_clickhouse_settings  # noqa: E402
from signal_generator.db.client import ClickHouseClient  # noqa: E402
from signal_generator.db.public_trades import (  # noqa: E402
    CANONICAL_FQN,
    CanonicalPublicTradeRepository,
)

logger = logging.getLogger("public_trades_pilot")

PILOT_SYMBOLS = ("DOGEUSDT", "LITUSDT")
PILOT_DAYS = (date(2026, 8, 15), date(2026, 8, 16))
DEFAULT_RESULTS = ROOT / "results" / "public_trades_51_coin_pilot"


def _iso(ts: datetime | None) -> str:
    if ts is None:
        return ""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat()


def collector_snapshot() -> dict[str, Any]:
    import json as json_lib
    import urllib.request

    pid_path = Path("/proc/1738842")
    alive = pid_path.exists()
    cmd = ""
    if alive:
        cmd = Path("/proc/1738842/cmdline").read_bytes().replace(b"\0", b" ").decode()
    status: dict[str, Any] = {"pid_1738842_alive": alive, "cmd": cmd}
    try:
        with urllib.request.urlopen("http://127.0.0.1:8787/api/collector/status", timeout=5) as resp:
            payload = json_lib.loads(resp.read().decode())
        status["collector_state"] = payload.get("state") or payload.get("collector_state")
        status["desired_state"] = payload.get("desired_state")
        status["last_error"] = payload.get("last_error")
    except Exception as exc:  # noqa: BLE001
        status["collector_api_error"] = str(exc)
    return status


def rss_kb() -> int:
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def query_safe(client: ClickHouseClient, sql: str, params: dict[str, Any] | None = None) -> Any:
    return client.query(sql, parameters=params)


def canonical_bytes(client: ClickHouseClient) -> dict[str, Any]:
    try:
        q = client.query(
            """
            SELECT
                sum(bytes_on_disk),
                sum(rows),
                uniqExact(partition)
            FROM system.parts
            WHERE active = 1
              AND table = {table:String}
              AND database = {db:String}
            """,
            parameters={"table": "public_trades_canonical", "db": "orderbook_analysis"},
        )
        row = q.result_rows[0]
        return {
            "bytes_on_disk": int(row[0] or 0),
            "parts_rows": int(row[1] or 0),
            "partitions": int(row[2] or 0),
            "source": "system.parts",
        }
    except Exception as exc:  # noqa: BLE001
        err = str(exc)[:300]
    # Fallback: table data dir inside the ClickHouse container (no orderbook_deltas).
    import subprocess

    try:
        proc = subprocess.run(
            [
                "docker",
                "exec",
                "orderbook-clickhouse",
                "bash",
                "-c",
                "readlink -f /var/lib/clickhouse/data/orderbook_analysis/public_trades_canonical | xargs du -sb",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            size = int(proc.stdout.strip().split()[0])
            return {
                "bytes_on_disk": size,
                "parts_rows": None,
                "partitions": None,
                "source": "docker_du",
                "system_parts_error": err,
            }
    except Exception as exc2:  # noqa: BLE001
        err = f"{err}; docker_du={exc2}"
    return {"bytes_on_disk": None, "error": err, "source": "unavailable"}


def counts_for(
    repo: CanonicalPublicTradeRepository,
    *,
    symbols: tuple[str, ...] | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict[str, Any]:
    return repo.physical_and_logical_counts(symbols=symbols, start=start, end=end)


def build_importer(results: Path, client: ClickHouseClient | None, *, dry_repo: bool) -> PublicTradeImporter:
    manifest = ManifestStore(results / "pilot_manifest.csv")
    repo = None if dry_repo else CanonicalPublicTradeRepository(client)  # type: ignore[arg-type]
    return PublicTradeImporter(
        cache_dir=results / "cache",
        manifest=manifest,
        repo=repo,
        transport=HttpxTransport(),
        batch_size=3000,
        pause_ms=200,
        source="archive",
    )


def run_dry_run(results: Path, importer: PublicTradeImporter) -> dict[str, Any]:
    report_rows: list[dict[str, Any]] = []
    ids_by_symbol: dict[str, dict[str, str]] = {s: {} for s in PILOT_SYMBOLS}
    collisions: list[dict[str, Any]] = []
    for symbol in PILOT_SYMBOLS:
        for day in PILOT_DAYS:
            t0 = time.perf_counter()
            row = importer.process_file(symbol, day, dry_run=True)
            elapsed = time.perf_counter() - t0
            path = Path(row.local_path)
            last_stats = None
            for trade, stats, dup in stream_parse_file(path, symbol=symbol, day=day):
                last_stats = stats
                if trade is None or dup:
                    continue
                prev = ids_by_symbol[symbol].get(trade.trade_id)
                if prev and prev != day.isoformat():
                    collisions.append(
                        {
                            "symbol": symbol,
                            "trade_id": trade.trade_id,
                            "day_a": prev,
                            "day_b": day.isoformat(),
                        }
                    )
                else:
                    ids_by_symbol[symbol][trade.trade_id] = day.isoformat()
            stats = last_stats
            report_rows.append(
                {
                    "symbol": symbol,
                    "day": day.isoformat(),
                    "url": row.url,
                    "status": row.status,
                    "header": "|".join(stats.header) if stats else "",
                    "compressed_bytes": row.compressed_bytes,
                    "rowcount_source": row.rowcount_source,
                    "rowcount_parsed": row.rowcount_parsed,
                    "invalid_rows": row.invalid_rows,
                    "duplicate_rows_in_file": row.duplicate_rows,
                    "out_of_day_rows": row.out_of_day_rows,
                    "unique_trade_ids": row.unique_trade_ids,
                    "empty_trade_id": stats.empty_trade_id if stats else 0,
                    "min_trade_ts": row.min_trade_ts,
                    "max_trade_ts": row.max_trade_ts,
                    "elapsed_s": round(elapsed, 3),
                    "error": row.error,
                }
            )
    write_csv(results / "dry_run_report.csv", report_rows)
    write_json(
        results / "dry_run_cross_day_collisions.json",
        {"count": len(collisions), "sample": collisions[:50]},
    )
    return {
        "files": report_rows,
        "cross_day_collisions": len(collisions),
        "ids_by_symbol_counts": {k: len(v) for k, v in ids_by_symbol.items()},
    }


def run_import(
    results: Path,
    importer: PublicTradeImporter,
    *,
    force_reimport: bool = False,
    abort_after_rows: int | None = None,
    abort_symbol: str = "LITUSDT",
    abort_day: date = date(2026, 8, 16),
) -> list[dict[str, Any]]:
    out = []
    aborted = False
    for symbol in PILOT_SYMBOLS:
        for day in PILOT_DAYS:
            t0 = time.perf_counter()
            rss0 = rss_kb()
            try:
                abort = None
                if (
                    abort_after_rows is not None
                    and not aborted
                    and symbol == abort_symbol
                    and day == abort_day
                ):
                    abort = abort_after_rows
                row = importer.process_file(
                    symbol,
                    day,
                    force_reimport=force_reimport,
                    abort_after_rows=abort,
                )
            except AbortAfterRows as exc:
                elapsed = time.perf_counter() - t0
                out.append(
                    {
                        "symbol": symbol,
                        "day": day.isoformat(),
                        "status": "FAILED",
                        "error": str(exc),
                        "elapsed_s": round(elapsed, 3),
                        "rss_kb": rss_kb(),
                        "rss_delta_kb": rss_kb() - rss0,
                    }
                )
                aborted = True
                continue
            elapsed = time.perf_counter() - t0
            out.append(
                {
                    "symbol": symbol,
                    "day": day.isoformat(),
                    "status": row.status,
                    "inserted_rows": row.inserted_rows,
                    "unique_trade_ids": row.unique_trade_ids,
                    "elapsed_s": round(elapsed, 3),
                    "rss_kb": rss_kb(),
                    "rss_delta_kb": rss_kb() - rss0,
                    "error": row.error,
                }
            )
    write_csv(results / "import_timing.csv", out)
    return out


def run_candle_audit(client: ClickHouseClient, results: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for symbol in PILOT_SYMBOLS:
        for day in PILOT_DAYS:
            rows.extend(reconcile_symbol_day(client, symbol=symbol, day=day))
    write_csv(results / "candle_reconciliation.csv", rows)
    summary = summarize_classes(rows)
    write_json(
        results / "candle_reconciliation_summary.json",
        {"volume_rel_tol": str(VOLUME_REL_TOL), "price_abs_tol": "0", "counts": summary},
    )
    return {"n": len(rows), "counts": summary}


def run_live_id_audit(client: ClickHouseClient, results: Path) -> dict[str, Any]:
    start, _ = utc_day_bounds(PILOT_DAYS[0])
    _, end = utc_day_bounds(PILOT_DAYS[-1])
    q = client.query(
        """
        SELECT symbol, count(), min(trade_ts), max(trade_ts)
        FROM orderbook_analysis.public_trades
        WHERE symbol IN {symbols:Array(String)}
          AND trade_ts >= {start:DateTime64(3, 'UTC')}
          AND trade_ts < {end:DateTime64(3, 'UTC')}
        GROUP BY symbol
        """,
        parameters={"symbols": list(PILOT_SYMBOLS), "start": start, "end": end},
    )
    rows = []
    if not q.result_rows:
        rows.append(
            {
                "symbol": "DOGEUSDT+LITUSDT",
                "status": "NOT_TESTABLE",
                "reason": (
                    "No orderbook_analysis.public_trades rows in "
                    f"[{_iso(start)}, {_iso(end)}). Live recorder max for DOGEUSDT "
                    "is 2026-08-11. Archive vs WS field i is not proven."
                ),
                "live_rows": 0,
                "matched_ids": "",
                "archive_only": "",
                "live_only": "",
            }
        )
    else:
        for symbol, n, mn, mx in q.result_rows:
            rows.append(
                {
                    "symbol": symbol,
                    "status": "HAS_OVERLAP",
                    "live_rows": int(n),
                    "min_ts": _iso(mn),
                    "max_ts": _iso(mx),
                }
            )
    write_csv(results / "archive_live_id_audit.csv", rows)
    return {"rows": rows, "testable": any(r.get("status") == "HAS_OVERLAP" for r in rows)}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--mode",
        required=True,
        choices=("dry-run", "import", "reimport", "resume-abort", "resume-finish", "audit", "all"),
    )
    p.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS)
    p.add_argument("--abort-after-rows", type=int, default=5000)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    results: Path = args.results_dir
    results.mkdir(parents=True, exist_ok=True)
    assert_disk_free(results, minimum=DISK_FREE_MIN_BYTES)

    settings = get_clickhouse_settings()
    client = ClickHouseClient.from_settings(settings)
    before = collector_snapshot()
    write_json(results / "collector_before.json", before)

    repo = CanonicalPublicTradeRepository(client)
    if args.mode != "dry-run":
        repo.ensure_table()

    importer = build_importer(results, client, dry_repo=(args.mode == "dry-run"))
    # dry-run still needs a store with cache; recreate with repo for other modes
    if args.mode != "dry-run":
        importer = build_importer(results, client, dry_repo=False)

    summary: dict[str, Any] = {"mode": args.mode, "started_at": _iso(datetime.now(timezone.utc))}

    if args.mode in {"dry-run", "all"}:
        importer = build_importer(results, client, dry_repo=True)
        summary["dry_run"] = run_dry_run(results, importer)

    if args.mode in {"import", "all"}:
        importer = build_importer(results, client, dry_repo=False)
        importer.repo.ensure_table()  # type: ignore[union-attr]
        before_counts = counts_for(repo)
        t0 = time.perf_counter()
        summary["import"] = run_import(results, importer)
        summary["import_elapsed_s"] = round(time.perf_counter() - t0, 3)
        summary["counts_after_import"] = counts_for(
            repo, symbols=PILOT_SYMBOLS, start=utc_day_bounds(PILOT_DAYS[0])[0], end=utc_day_bounds(PILOT_DAYS[-1])[1]
        )
        summary["counts_table_after_import"] = before_counts | {"after": counts_for(repo)}
        summary["bytes_after_import"] = canonical_bytes(client)

    if args.mode in {"reimport", "all"}:
        importer = build_importer(results, client, dry_repo=False)
        before_re = counts_for(
            repo, symbols=PILOT_SYMBOLS, start=utc_day_bounds(PILOT_DAYS[0])[0], end=utc_day_bounds(PILOT_DAYS[-1])[1]
        )
        summary["reimport"] = run_import(results, importer, force_reimport=True)
        after_re = counts_for(
            repo, symbols=PILOT_SYMBOLS, start=utc_day_bounds(PILOT_DAYS[0])[0], end=utc_day_bounds(PILOT_DAYS[-1])[1]
        )
        summary["dedup_audit"] = {
            "logical_unique_before": before_re["logical_unique_rows"],
            "logical_unique_after": after_re["logical_unique_rows"],
            "final_before": before_re["final_rows"],
            "final_after": after_re["final_rows"],
            "physical_before": before_re["physical_rows"],
            "physical_after": after_re["physical_rows"],
            "logical_size_sum_before": str(before_re["logical_size_sum"]),
            "logical_size_sum_after": str(after_re["logical_size_sum"]),
            "logical_unchanged": before_re["logical_unique_rows"] == after_re["logical_unique_rows"],
            "volume_unchanged": str(before_re["logical_size_sum"]) == str(after_re["logical_size_sum"]),
        }
        write_csv(
            results / "dedup_audit.csv",
            [
                {
                    "metric": k,
                    "value": v,
                }
                for k, v in summary["dedup_audit"].items()
            ],
        )

    if args.mode == "resume-abort":
        importer = build_importer(results, client, dry_repo=False)
        summary["resume_abort"] = run_import(
            results,
            importer,
            force_reimport=True,
            abort_after_rows=args.abort_after_rows,
        )

    if args.mode == "resume-finish":
        importer = build_importer(results, client, dry_repo=False)
        summary["resume_finish"] = run_import(results, importer, force_reimport=False)

    if args.mode in {"audit", "all"}:
        summary["candle"] = run_candle_audit(client, results)
        summary["live_id"] = run_live_id_audit(client, results)
        start, _ = utc_day_bounds(PILOT_DAYS[0])
        _, end = utc_day_bounds(PILOT_DAYS[-1])
        by_sym = {}
        for symbol in PILOT_SYMBOLS:
            by_sym[symbol] = counts_for(repo, symbols=(symbol,), start=start, end=end)
        bytes_info = canonical_bytes(client)
        gz = 0
        for symbol in PILOT_SYMBOLS:
            for day in PILOT_DAYS:
                p = results / "cache" / f"{symbol}{day.isoformat()}.csv.gz"
                if p.is_file():
                    gz += p.stat().st_size
        ch_bytes = bytes_info.get("bytes_on_disk")
        free = assert_disk_free(results)
        window = counts_for(repo, symbols=PILOT_SYMBOLS, start=start, end=end)
        summary["storage"] = project_storage(
            doge_rows=by_sym["DOGEUSDT"]["logical_unique_rows"],
            lit_rows=by_sym["LITUSDT"]["logical_unique_rows"],
            combined_rows=window["logical_unique_rows"],
            combined_ch_bytes=ch_bytes,
            combined_physical_rows=window["physical_rows"],
            combined_gz_bytes=gz,
            free_bytes_now=free,
            first_import_ch_bytes=34_312_872,
        )
        # Per-symbol bytes cannot be split from one table without parts; record combined.
        write_json(results / "storage_projection.json", summary["storage"] | {"bytes_info": bytes_info, "by_symbol": by_sym, "pilot_gz_bytes": gz})

    after = collector_snapshot()
    write_json(results / "collector_after.json", after)
    summary["collector_before"] = before
    summary["collector_after"] = after
    summary["finished_at"] = _iso(datetime.now(timezone.utc))
    write_json(results / f"run_{args.mode.replace('-', '_')}.json", summary)
    print(json.dumps({"mode": args.mode, "collector": after.get("collector_state"), "ok": True}, default=str))
    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
