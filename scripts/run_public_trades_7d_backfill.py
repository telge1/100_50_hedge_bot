#!/usr/bin/env python3
"""51-coin × 7 UTC-day Bybit public-trade backfill (no collector restart).

Examples:
  python scripts/run_public_trades_7d_backfill.py --mode preflight
  python scripts/run_public_trades_7d_backfill.py --mode check-sources
  python scripts/run_public_trades_7d_backfill.py --mode storage-gate
  python scripts/run_public_trades_7d_backfill.py --mode backfill
  python scripts/run_public_trades_7d_backfill.py --mode audit
  python scripts/run_public_trades_7d_backfill.py --mode candle-reconcile
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signal_generator.bybit.live.candle_universe import load_candle_universe  # noqa: E402
from signal_generator.bybit.public_trades.audit import (  # noqa: E402
    VOLUME_REL_TOL,
    reconcile_symbol_day,
    summarize_classes,
)
from signal_generator.bybit.public_trades.backfill_manifest import (  # noqa: E402
    BackfillManifestStore,
    iter_backfill_jobs,
)
from signal_generator.bybit.public_trades.backfill_runner import (  # noqa: E402
    BackfillRunner,
    check_sources,
    project_7d_storage,
    seed_pilot_audited,
)
from signal_generator.bybit.public_trades.downloader import (  # noqa: E402
    DISK_FREE_MIN_BYTES,
    HttpxTransport,
    PublicTradeDownloadError,
    assert_disk_free,
    disk_free_bytes,
)
from signal_generator.bybit.public_trades.manifest import write_json  # noqa: E402
from signal_generator.bybit.public_trades.urls import daily_url, utc_day_bounds  # noqa: E402
from signal_generator.config import get_clickhouse_settings  # noqa: E402
from signal_generator.db.client import ClickHouseClient  # noqa: E402
from signal_generator.db.public_trades import CanonicalPublicTradeRepository  # noqa: E402

logger = logging.getLogger("public_trades_7d")

UNIVERSE_PATH = ROOT / "config" / "universe_tradeable_51.json"
DEFAULT_RESULTS = ROOT / "results" / "public_trades_51_coin_7d"
BACKFILL_DAYS = (
    date(2026, 8, 10),
    date(2026, 8, 11),
    date(2026, 8, 12),
    date(2026, 8, 13),
    date(2026, 8, 14),
    date(2026, 8, 15),
    date(2026, 8, 16),
)
EXPECTED_FILES = 357
PILOT_PAIRS = [
    ("DOGEUSDT", "2026-08-15"),
    ("DOGEUSDT", "2026-08-16"),
    ("LITUSDT", "2026-08-15"),
    ("LITUSDT", "2026-08-16"),
]
COLLECTOR_PID = 1738842


def _iso(ts: datetime | None) -> str:
    if ts is None:
        return ""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat()


def load_universe() -> list[str]:
    symbols = load_candle_universe(UNIVERSE_PATH)
    if len(symbols) != 51:
        raise SystemExit(f"PUBLIC_TRADES_7D_BACKFILL_BLOCKED: universe size {len(symbols)} != 51")
    if "XAUUSDT" in symbols:
        raise SystemExit("PUBLIC_TRADES_7D_BACKFILL_BLOCKED: XAUUSDT in universe")
    return symbols


def expected_jobs(symbols: list[str]) -> list[tuple[str, str]]:
    days = [d.isoformat() for d in BACKFILL_DAYS]
    jobs = iter_backfill_jobs(symbols, days)
    if len(jobs) != EXPECTED_FILES:
        raise SystemExit(
            f"PUBLIC_TRADES_7D_BACKFILL_BLOCKED: expected {EXPECTED_FILES} files, got {len(jobs)}"
        )
    return jobs


def collector_snapshot() -> dict[str, Any]:
    import json as json_lib
    import urllib.request

    pid_path = Path(f"/proc/{COLLECTOR_PID}")
    alive = pid_path.exists()
    cmd = ""
    start = ""
    if alive:
        cmd = Path(f"/proc/{COLLECTOR_PID}/cmdline").read_bytes().replace(b"\0", b" ").decode()
        try:
            start = Path(f"/proc/{COLLECTOR_PID}/stat").read_text().split()[21]
        except OSError:
            start = ""
    status: dict[str, Any] = {
        "pid": COLLECTOR_PID,
        "alive": alive,
        "cmd": cmd.strip(),
        "start_ticks": start,
    }
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:8787/api/collector/status", timeout=5
        ) as resp:
            payload = json_lib.loads(resp.read().decode())
        status["collector_state"] = payload.get("state") or payload.get("collector_state")
        status["desired_state"] = payload.get("desired_state")
        status["last_error"] = payload.get("last_error")
    except Exception as exc:  # noqa: BLE001
        status["collector_api_error"] = str(exc)
    return status


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


def canonical_bytes(client: ClickHouseClient) -> dict[str, Any]:
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
            return {"bytes_on_disk": size, "source": "docker_du"}
    except Exception as exc:  # noqa: BLE001
        return {"bytes_on_disk": None, "error": str(exc)[:300], "source": "unavailable"}
    return {"bytes_on_disk": None, "source": "unavailable"}


def archive_unique_count(client: ClickHouseClient) -> int:
    q = client.query(
        "SELECT uniqExact(symbol, trade_id) FROM orderbook_analysis.public_trades_archive"
    )
    return int(q.result_rows[0][0])


def ensure_manifest(results: Path, symbols: list[str]) -> BackfillManifestStore:
    store = BackfillManifestStore(results / "backfill_manifest.csv")
    for symbol, utc_date in expected_jobs(symbols):
        store.ensure(symbol, utc_date, daily_url(symbol, date.fromisoformat(utc_date)))
    if len(store.rows) != EXPECTED_FILES:
        raise SystemExit(
            f"PUBLIC_TRADES_7D_BACKFILL_BLOCKED: manifest size {len(store.rows)} != {EXPECTED_FILES}"
        )
    store.save()
    return store


def run_preflight(results: Path, client: ClickHouseClient) -> dict[str, Any]:
    import shutil
    import subprocess

    git_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False
    ).stdout.strip()
    git_branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    git_status = subprocess.run(
        ["git", "status", "--short"], cwd=ROOT, capture_output=True, text=True, check=False
    ).stdout
    disk = shutil.disk_usage("/")
    repo = CanonicalPublicTradeRepository(client)
    counts = repo.physical_and_logical_counts()
    archive_n = archive_unique_count(client)
    out = {
        "pwd": str(ROOT),
        "branch": git_branch,
        "head": git_head,
        "git_status_short": git_status.strip().splitlines(),
        "collector": collector_snapshot(),
        "disk_free_bytes": disk.free,
        "disk_total_bytes": disk.total,
        "canonical_counts": counts,
        "archive_unique": archive_n,
        "canonical_bytes": canonical_bytes(client),
        "hard_stop_free_bytes": DISK_FREE_MIN_BYTES,
    }
    write_json(results / "preflight.json", out)
    return out


def run_check_sources(results: Path, manifest: BackfillManifestStore) -> dict[str, Any]:
    result = check_sources(manifest, transport=HttpxTransport(), sleep_s=0.03)
    write_csv(results / "source_availability.csv", result.rows)
    write_json(
        results / "source_check.json",
        {
            "total": result.total,
            "ok": result.ok,
            "missing": result.missing,
            "total_content_length": result.total_content_length,
        },
    )
    if result.ok != EXPECTED_FILES or result.missing:
        raise SystemExit(
            "PUBLIC_TRADES_7D_BACKFILL_BLOCKED: "
            f"ok={result.ok} missing={result.missing} expected={EXPECTED_FILES}"
        )
    return {
        "ok": result.ok,
        "missing": result.missing,
        "total_content_length": result.total_content_length,
    }


def run_storage_gate(
    results: Path,
    manifest: BackfillManifestStore,
    *,
    free_bytes: int,
) -> dict[str, Any]:
    total_gz = sum(int(r.content_length or r.compressed_bytes or 0) for r in manifest.as_rows())
    proj = project_7d_storage(
        total_gz_bytes=total_gz, estimated_rows=None, free_bytes=free_bytes
    )
    write_json(results / "storage_projection.json", proj)
    if proj["blocked"]:
        raise SystemExit("PUBLIC_TRADES_7D_BACKFILL_BLOCKED_STORAGE")
    return proj


def run_backfill(
    results: Path,
    manifest: BackfillManifestStore,
    repo: CanonicalPublicTradeRepository,
    *,
    max_files: int | None = None,
) -> dict[str, Any]:
    runner = BackfillRunner(
        cache_dir=results / "cache",
        manifest=manifest,
        repo=repo,
        transport=HttpxTransport(),
        batch_size=3000,
        pause_ms=50,
    )
    seed_pilot_audited(manifest, repo, pilot_pairs=PILOT_PAIRS)
    rows = [r for r in manifest.as_rows() if r.status != "AUDITED"]
    if max_files is not None:
        rows = rows[:max_files]
    t0 = time.perf_counter()
    completed = 0
    errors: list[dict[str, str]] = []
    imported_trades = 0
    skipped = 0
    total = EXPECTED_FILES
    already = sum(1 for r in manifest.as_rows() if r.status == "AUDITED")
    for i, row in enumerate(rows, start=1):
        assert_disk_free(results)
        started = time.perf_counter()
        try:
            row = runner.process_file(row)
            runner.audit_file(
                row,
                cache_path=results / "cache" / f"{row.symbol}{row.utc_date}.csv.gz",
            )
            imported_trades += row.inserted_rows
            skipped += row.skipped_existing_rows
            completed += 1
        except (PublicTradeDownloadError, Exception) as exc:  # noqa: BLE001
            logger.exception("file failed %s %s", row.symbol, row.utc_date)
            try:
                if row.status not in {"FAILED", "MISSING", "AUDITED"}:
                    manifest.set_status(row, "FAILED", error=str(exc)[:400])
            except Exception:  # noqa: BLE001
                pass
            errors.append(
                {"symbol": row.symbol, "utc_date": row.utc_date, "error": str(exc)[:400]}
            )
            if "DISK_HARD_STOP" in str(exc) or "empty trdMatchID" in str(exc):
                raise
        elapsed = time.perf_counter() - t0
        done = already + completed
        remaining_files = max(0, total - done)
        file_s = time.perf_counter() - started
        eta = (elapsed / max(completed, 1)) * remaining_files if completed else None
        progress = {
            "files_completed": done,
            "files_total": total,
            "current_file": f"{row.symbol}{row.utc_date}",
            "inserted_rows": imported_trades,
            "skipped_existing_rows": skipped,
            "errors": len(errors),
            "last_file_s": round(file_s, 3),
            "elapsed_s": round(elapsed, 3),
            "eta_s": round(eta, 1) if eta is not None else None,
            "free_bytes": disk_free_bytes(results),
            "throughput_files_per_h": round(3600 * completed / max(elapsed, 1e-6), 2),
        }
        write_json(results / "import_progress.json", progress)
        logger.info(
            "progress %s/%s %s inserted=%s skipped=%s eta_s=%s",
            done,
            total,
            progress["current_file"],
            imported_trades,
            skipped,
            progress["eta_s"],
        )
    return {
        "completed": already + completed,
        "errors": errors,
        "inserted_rows": imported_trades,
        "skipped_existing_rows": skipped,
        "elapsed_s": round(time.perf_counter() - t0, 3),
        "manifest_summary": manifest.summary(),
    }


def run_global_audit(
    results: Path,
    manifest: BackfillManifestStore,
    repo: CanonicalPublicTradeRepository,
    client: ClickHouseClient,
) -> dict[str, Any]:
    summary = manifest.summary()
    failed = summary.get("FAILED", 0) + summary.get("MISSING", 0)
    audited = summary.get("AUDITED", 0)
    if audited != EXPECTED_FILES or failed:
        raise SystemExit(
            "PUBLIC_TRADES_7D_BACKFILL_BLOCKED_AUDIT: "
            f"AUDITED={audited} FAILED/MISSING={failed} expected={EXPECTED_FILES}"
        )
    start, _ = utc_day_bounds(BACKFILL_DAYS[0])
    _, end = utc_day_bounds(BACKFILL_DAYS[-1])
    counts = repo.physical_and_logical_counts(start=start, end=end)
    by_day = repo.count_by_symbol_day(start=start, end=end)
    coverage_rows = [
        {"symbol": s, "utc_date": d, "logical_unique": n} for s, d, n in by_day
    ]
    write_csv(results / "coverage_by_symbol_day.csv", coverage_rows)
    symbols = sorted({r.symbol for r in manifest.as_rows()})
    days = {d.isoformat() for d in BACKFILL_DAYS}
    by_sym_days: dict[str, set[str]] = {}
    for s, d, _n in by_day:
        by_sym_days.setdefault(s, set()).add(d)
    missing_cov = []
    for s in symbols:
        have = by_sym_days.get(s, set())
        for d in sorted(days):
            if d not in have:
                missing_cov.append({"symbol": s, "utc_date": d})
    archive_n = archive_unique_count(client)
    physical_dups = counts["physical_rows"] - counts["logical_unique_rows"]
    audit = {
        "audited_files": audited,
        "symbols": len(symbols),
        "missing_symbol_days": missing_cov,
        "logical_unique_rows": counts["logical_unique_rows"],
        "physical_rows": counts["physical_rows"],
        "final_rows": counts["final_rows"],
        "physical_duplicates": physical_dups,
        "logical_size_sum": str(counts["logical_size_sum"]),
        "archive_unique": archive_n,
        "archive_unique_expected": 4_251_001,
        "archive_unchanged": archive_n == 4_251_001,
        "collector": collector_snapshot(),
    }
    write_csv(
        results / "dedup_audit.csv",
        [{"metric": k, "value": v} for k, v in audit.items() if k != "missing_symbol_days"],
    )
    write_json(results / "global_audit.json", audit)
    if missing_cov:
        raise SystemExit(
            f"PUBLIC_TRADES_7D_BACKFILL_BLOCKED_AUDIT: missing {len(missing_cov)} symbol-days"
        )
    if archive_n != 4_251_001:
        raise SystemExit(
            f"PUBLIC_TRADES_7D_BACKFILL_BLOCKED_AUDIT: archive unique {archive_n} != 4251001"
        )
    return audit


def run_candle_reconcile(
    results: Path, client: ClickHouseClient, symbols: list[str]
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        for day in BACKFILL_DAYS:
            logger.info("reconcile %s %s", symbol, day.isoformat())
            rows.extend(reconcile_symbol_day(client, symbol=symbol, day=day))
    write_csv(results / "candle_reconciliation.csv", rows)
    counts = summarize_classes(rows)
    quote_diffs = []
    base_mismatch = 0
    ohlc_only = 0
    for r in rows:
        if r["class"] != "CANDLE_MISMATCH":
            continue
        base_eq = r["candle_volume"] == r["trade_base_volume"]
        if not base_eq:
            base_mismatch += 1
        else:
            ohlc_only += 1
        if r["candle_turnover"] and r["trade_quote_volume"]:
            try:
                from decimal import Decimal

                q = abs(Decimal(r["candle_turnover"]) - Decimal(r["trade_quote_volume"]))
                quote_diffs.append(q)
            except Exception:  # noqa: BLE001
                pass
    missing = counts.get("PUBLIC_TRADES_MISSING", 0)
    summary = {
        "n_minutes": len(rows),
        "counts": counts,
        "volume_rel_tol": str(VOLUME_REL_TOL),
        "base_volume_mismatch_minutes": base_mismatch,
        "ohlc_only_mismatch_minutes": ohlc_only,
        "quote_abs_diff_max": str(max(quote_diffs) if quote_diffs else 0),
        "quote_abs_diff_p99": str(
            sorted(quote_diffs)[int(0.99 * (len(quote_diffs) - 1))] if quote_diffs else 0
        ),
    }
    write_json(results / "candle_reconciliation_summary.json", summary)
    if missing:
        raise SystemExit(
            f"PUBLIC_TRADES_7D_BACKFILL_BLOCKED_AUDIT: PUBLIC_TRADES_MISSING={missing}"
        )
    if base_mismatch:
        raise SystemExit(
            f"PUBLIC_TRADES_7D_BACKFILL_BLOCKED_AUDIT: unexplained base volume mismatches={base_mismatch}"
        )
    return summary


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--mode",
        required=True,
        choices=(
            "preflight",
            "check-sources",
            "storage-gate",
            "backfill",
            "audit",
            "candle-reconcile",
            "all",
        ),
    )
    p.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS)
    p.add_argument("--max-files", type=int, default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    results: Path = args.results_dir
    results.mkdir(parents=True, exist_ok=True)
    symbols = load_universe()
    settings = get_clickhouse_settings()
    client = ClickHouseClient.from_settings(settings)
    repo = CanonicalPublicTradeRepository(client)
    repo.ensure_table()
    manifest = ensure_manifest(results, symbols)
    summary: dict[str, Any] = {
        "mode": args.mode,
        "started_at": _iso(datetime.now(timezone.utc)),
        "symbols": len(symbols),
        "expected_files": EXPECTED_FILES,
        "days": [d.isoformat() for d in BACKFILL_DAYS],
    }
    try:
        if args.mode in {"preflight", "all"}:
            summary["preflight"] = run_preflight(results, client)
        if args.mode in {"check-sources", "all"}:
            summary["sources"] = run_check_sources(results, manifest)
        if args.mode in {"storage-gate", "all"}:
            free = disk_free_bytes(results)
            summary["storage"] = run_storage_gate(results, manifest, free_bytes=free)
        if args.mode in {"backfill", "all"}:
            summary["backfill"] = run_backfill(
                results, manifest, repo, max_files=args.max_files
            )
        if args.mode in {"audit", "all"}:
            summary["audit"] = run_global_audit(results, manifest, repo, client)
        if args.mode in {"candle-reconcile", "all"}:
            summary["candle"] = run_candle_reconcile(results, client, symbols)
        summary["collector_after"] = collector_snapshot()
        summary["manifest_summary"] = manifest.summary()
        summary["finished_at"] = _iso(datetime.now(timezone.utc))
        write_json(results / f"run_{args.mode.replace('-', '_')}.json", summary)
        print(json.dumps({"mode": args.mode, "ok": True, "manifest": manifest.summary()}, default=str))
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
