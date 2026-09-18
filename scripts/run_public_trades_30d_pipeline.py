#!/usr/bin/env python3
"""30-day Gold-51 public-trade backfill orchestrator (reuses 7d CLI).

Does not start/stop the live collector. workers=1. Writes only
orderbook_analysis.public_trades_canonical via the existing runner.

  python scripts/run_public_trades_30d_pipeline.py --gate A
  python scripts/run_public_trades_30d_pipeline.py --gate BCD --run-dir ...
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signal_generator.bybit.public_trades.manifest import write_json  # noqa: E402
from signal_generator.bybit.public_trades.urls import utc_day_bounds  # noqa: E402
from signal_generator.bybit.public_trades.window import (  # noqa: E402
    classify_symbol_day,
    inclusive_utc_days,
    load_universe_symbols,
)
from signal_generator.config import get_clickhouse_settings  # noqa: E402
from signal_generator.db.client import ClickHouseClient  # noqa: E402
from signal_generator.db.public_trades import CanonicalPublicTradeRepository  # noqa: E402

logger = logging.getLogger("public_trades_30d")

GOLD_UNIVERSE = Path(
    "/home/telgenbuescher/projects/wave_fade_gold_f16ae32/config/universe_tradeable_51.json"
)
REPO_UNIVERSE = ROOT / "config" / "universe_tradeable_51.json"
CLI = ROOT / "scripts" / "run_public_trades_7d_backfill.py"
WINDOW_START = date(2026, 7, 19)
WINDOW_END_EXCL = date(2026, 8, 18)
OI_MODULE = "orderbook_analyse.oi_liquidation_collector"


def _iso(ts: datetime | None = None) -> str:
    ts = ts or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat()


def _run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pgrep(pattern: str) -> list[str]:
    proc = subprocess.run(
        ["pgrep", "-af", pattern], capture_output=True, text=True, check=False
    )
    return [
        ln
        for ln in proc.stdout.splitlines()
        if pattern in ln and "pgrep" not in ln
    ]


def rss_kb(pid: int) -> int | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except (OSError, ValueError):
        return None
    return None


def append_resource(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.is_file() and path.stat().st_size > 0
    with path.open("a", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(row.keys()))
        if not exists:
            w.writeheader()
        w.writerow(row)


def oi_health() -> dict[str, Any]:
    lines = pgrep(OI_MODULE)
    live = pgrep("run_live_collector")
    return {
        "oi_liquidation_running": bool(lines),
        "oi_pgrep": lines[:5],
        "live_collector_running": bool(live),
        "live_pgrep": live[:5],
    }


def load_cli():
    import importlib.util

    spec = importlib.util.spec_from_file_location("pt7d_cli", CLI)
    if spec is None or spec.loader is None:
        raise SystemExit("cannot load backfill CLI")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def invoke_cli(args: list[str]) -> int:
    logger.info("CLI %s", " ".join(args))
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.run(
        [sys.executable, str(CLI), *args],
        cwd=str(ROOT),
        env=env,
        check=False,
    )
    return int(proc.returncode)


def coverage_matrix(
    client: ClickHouseClient,
    symbols: list[str],
    days: list[date],
) -> list[dict[str, Any]]:
    cli = load_cli()
    start, _ = utc_day_bounds(days[0])
    _, end = utc_day_bounds(days[-1])
    have = {
        (r["symbol"], r["utc_day"]): r
        for r in cli.query_coverage(client, symbols=symbols, start=start, end=end)
    }
    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        for day in days:
            meta = have.get((symbol, day.isoformat()))
            if meta is None:
                klass = "MISSING"
                need = "NEEDS_DOWNLOAD"
                logical = 0
                sources = ""
                min_ts = ""
                max_ts = ""
            else:
                klass = classify_symbol_day(
                    logical_unique=int(meta["logical_unique"]),
                    min_ts=meta["min_ts"],
                    max_ts=meta["max_ts"],
                    sources=list(meta["sources"] or []),
                    day=day,
                )
                logical = int(meta["logical_unique"])
                sources = "|".join(str(s) for s in meta["sources"])
                min_ts = str(meta["min_ts"])
                max_ts = str(meta["max_ts"])
                if klass == "ALREADY_AUDITED":
                    need = "ALREADY_AUDITED"
                elif klass == "PARTIAL_IN_CLICKHOUSE":
                    need = "NEEDS_DOWNLOAD"
                else:
                    need = "NEEDS_DOWNLOAD"
            rows.append(
                {
                    "symbol": symbol,
                    "utc_day": day.isoformat(),
                    "day_class": klass,
                    "coverage_need": need,
                    "logical_unique": logical,
                    "min_event_ts": min_ts,
                    "max_event_ts": max_ts,
                    "sources": sources,
                }
            )
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def symbol_day_counts(
    repo: CanonicalPublicTradeRepository, symbol: str, day: date
) -> dict[str, Any]:
    start, end = utc_day_bounds(day)
    return repo.physical_and_logical_counts(symbols=(symbol,), start=start, end=end)


def fail(run_dir: Path, reason: str) -> None:
    write_json(run_dir / "HARD_FAIL.json", {"reason": reason, "at": _iso()})
    raise SystemExit(f"PUBLIC_TRADES_30D_HARD_FAIL: {reason}")


def gate_a(run_dir: Path, universe: Path) -> None:
    health0 = oi_health()
    if health0["live_collector_running"]:
        fail(run_dir, "live collector is running; will not start backfill")
    if not health0["oi_liquidation_running"]:
        logger.warning("OI collector not running (will not start it)")

    client = ClickHouseClient.from_settings(get_clickhouse_settings())
    repo = CanonicalPublicTradeRepository(client)
    before = symbol_day_counts(repo, "BTCUSDT", date(2026, 7, 19))
    write_json(run_dir / "gate_a_ch_before.json", before)
    rss_before = rss_kb(os.getpid())
    t0 = time.perf_counter()
    rc = invoke_cli(
        [
            "--mode",
            "backfill",
            "--smoke",
            "--workers",
            "1",
            "--universe-file",
            str(universe),
            "--run-dir",
            str(run_dir),
            "--resume",
        ]
    )
    if rc != 0:
        fail(run_dir, f"gate A backfill exit {rc}")
    elapsed = time.perf_counter() - t0
    after = symbol_day_counts(repo, "BTCUSDT", date(2026, 7, 19))
    write_json(run_dir / "gate_a_ch_after.json", after)
    if int(after["logical_unique_rows"] or 0) <= 0:
        fail(run_dir, "gate A produced 0 logical trades")
    if int(after["physical_rows"]) != int(after["logical_unique_rows"]):
        fail(run_dir, "gate A physical != logical")

    rc2 = invoke_cli(
        [
            "--mode",
            "backfill",
            "--smoke",
            "--workers",
            "1",
            "--universe-file",
            str(universe),
            "--run-dir",
            str(run_dir),
            "--resume",
        ]
    )
    if rc2 != 0:
        fail(run_dir, f"gate A idempotent rerun exit {rc2}")
    after2 = symbol_day_counts(repo, "BTCUSDT", date(2026, 7, 19))
    if int(after2["logical_unique_rows"]) != int(after["logical_unique_rows"]):
        fail(run_dir, "gate A not idempotent")

    health1 = oi_health()
    if health0["oi_liquidation_running"] and not health1["oi_liquidation_running"]:
        fail(run_dir, "OI collector died during gate A")
    if health1["live_collector_running"]:
        fail(run_dir, "live collector appeared during gate A")

    rss_after = rss_kb(os.getpid())
    write_json(
        run_dir / "gate_a.json",
        {
            "ok": True,
            "elapsed_s": round(elapsed, 3),
            "logical_unique": after["logical_unique_rows"],
            "physical": after["physical_rows"],
            "before": before,
            "after": after,
            "after_rerun": after2,
            "rss_kb_before": rss_before,
            "rss_kb_after": rss_after,
            "oi": health1,
        },
    )
    client.close()


def gate_symbol(
    run_dir: Path,
    universe: Path,
    symbol: str,
    *,
    name: str,
) -> None:
    health0 = oi_health()
    rc = invoke_cli(
        [
            "--mode",
            "backfill",
            "--start-date",
            WINDOW_START.isoformat(),
            "--end-date-exclusive",
            WINDOW_END_EXCL.isoformat(),
            "--symbols",
            symbol,
            "--workers",
            "1",
            "--universe-file",
            str(universe),
            "--run-dir",
            str(run_dir),
            "--resume",
        ]
    )
    if rc != 0:
        fail(run_dir, f"{name} backfill exit {rc}")
    health1 = oi_health()
    if health0["oi_liquidation_running"] and not health1["oi_liquidation_running"]:
        fail(run_dir, f"OI collector died during {name}")
    write_json(run_dir / f"{name}.json", {"ok": True, "symbol": symbol, "oi": health1})


def remaining_symbols(universe: list[str]) -> list[str]:
    skip = {"BTCUSDT", "ETHUSDT"}
    return [s for s in universe if s not in skip]


def write_manifest_exports(run_dir: Path) -> None:
    path = run_dir / "backfill_manifest.csv"
    if not path.is_file():
        return
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    write_csv(run_dir / "files.csv", rows)
    insert_rows = [
        {
            "symbol": r["symbol"],
            "utc_date": r["utc_date"],
            "status": r["status"],
            "inserted_rows": r.get("inserted_rows"),
            "skipped_existing_rows": r.get("skipped_existing_rows"),
            "sha256": r.get("sha256"),
            "min_event_time": r.get("min_event_time"),
            "max_event_time": r.get("max_event_time"),
            "error": r.get("error"),
        }
        for r in rows
    ]
    write_csv(run_dir / "insert_audit.csv", insert_rows)
    from collections import Counter

    write_json(
        run_dir / "checkpoint.json",
        {
            "at": _iso(),
            "status_counts": dict(Counter(r["status"] for r in rows)),
            "n": len(rows),
        },
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--gate", default="A", help="A, B, C, D, BCD, or ALL")
    p.add_argument("--run-dir", type=Path, default=None)
    p.add_argument("--run-id", default=None)
    p.add_argument(
        "--universe-file",
        type=Path,
        default=GOLD_UNIVERSE if GOLD_UNIVERSE.is_file() else REPO_UNIVERSE,
    )
    p.add_argument("--continue-after-smoke", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    run_id = args.run_id or _run_id()
    run_dir = args.run_dir or (
        ROOT / "results" / "public_trades_backfill_30d" / run_id
    )
    run_dir = run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "run.log"
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(fh)

    universe_path: Path = args.universe_file
    symbols = load_universe_symbols(universe_path)
    if len(symbols) != 51 or "BTCUSDT" not in symbols or "ETHUSDT" not in symbols:
        raise SystemExit("PUBLIC_TRADES_30D_BLOCKED: universe must be gold-51 with BTC and ETH")
    if "XAUUSDT" in symbols:
        raise SystemExit("PUBLIC_TRADES_30D_BLOCKED: XAUUSDT in universe")
    days = inclusive_utc_days(WINDOW_START, WINDOW_END_EXCL)

    write_json(
        run_dir / "request.json",
        {
            "run_id": run_id,
            "gate": args.gate,
            "window_start": WINDOW_START.isoformat(),
            "window_end_exclusive": WINDOW_END_EXCL.isoformat(),
            "universe_file": str(universe_path),
            "universe_sha256": sha256_path(universe_path),
            "workers": 1,
            "pid": os.getpid(),
            "started_at": _iso(),
        },
    )
    write_json(
        run_dir / "universe.json",
        {
            "path": str(universe_path),
            "sha256": sha256_path(universe_path),
            "count": len(symbols),
            "unique": len(set(symbols)),
            "duplicates": [],
            "symbols": symbols,
            "contains_BTCUSDT": True,
            "contains_ETHUSDT": True,
            "contains_XAUTUSDT": "XAUTUSDT" in symbols,
            "contains_PAXGUSDT": "PAXGUSDT" in symbols,
            "contains_XAUUSDT": False,
            "class_default": "FULL_TARGET",
        },
    )
    (run_dir / "pid").write_text(str(os.getpid()), encoding="utf-8")

    settings = get_clickhouse_settings()
    client = ClickHouseClient.from_settings(settings)
    cli = load_cli()
    pre = cli.run_preflight(run_dir, client)
    mem = Path("/proc/meminfo").read_text()
    mem_map = {}
    for line in mem.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            mem_map[parts[0].rstrip(":")] = parts[1]
    pre["meminfo_kb"] = {
        k: int(mem_map[k])
        for k in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree")
        if k in mem_map
    }
    pre["oi"] = oi_health()
    write_json(run_dir / "preflight.json", pre)
    if pre["oi"]["live_collector_running"]:
        fail(run_dir, "live collector running")

    cov = coverage_matrix(client, symbols, days)
    write_csv(run_dir / "coverage_before.csv", cov)
    n_full = sum(1 for r in cov if r["day_class"] == "ALREADY_AUDITED")
    n_part = sum(1 for r in cov if r["day_class"] == "PARTIAL_IN_CLICKHOUSE")
    n_miss = sum(1 for r in cov if r["day_class"] == "MISSING")
    write_json(
        run_dir / "capacity.json",
        {
            "disk_free_bytes": pre["disk_free_bytes"],
            "canonical_bytes": pre.get("canonical_bytes"),
            "coverage_before": {
                "ALREADY_AUDITED": n_full,
                "PARTIAL_IN_CLICKHOUSE": n_part,
                "MISSING": n_miss,
                "needs_download": sum(1 for r in cov if r["coverage_need"] == "NEEDS_DOWNLOAD"),
            },
        },
    )
    append_resource(
        run_dir / "resource_metrics.csv",
        {
            "at": _iso(),
            "gate": "preflight",
            "rss_kb": rss_kb(os.getpid()) or "",
            "disk_free_bytes": pre["disk_free_bytes"],
            "oi_running": pre["oi"]["oi_liquidation_running"],
        },
    )

    gate = args.gate.upper()
    try:
        if gate in {"A", "ALL"}:
            gate_a(run_dir, universe_path)
            write_manifest_exports(run_dir)
            append_resource(
                run_dir / "resource_metrics.csv",
                {
                    "at": _iso(),
                    "gate": "A",
                    "rss_kb": rss_kb(os.getpid()) or "",
                    "disk_free_bytes": pre["disk_free_bytes"],
                    "oi_running": oi_health()["oi_liquidation_running"],
                },
            )
            if gate == "A" and not args.continue_after_smoke:
                write_json(run_dir / "run_manifest.json", {"run_id": run_id, "stopped_after": "A", "ok": True})
                logger.info("Gate A passed. Stopped (no --continue-after-smoke).")
                return 0
        if gate in {"B", "BCD", "ALL"} or (gate == "A" and args.continue_after_smoke):
            gate_symbol(run_dir, universe_path, "BTCUSDT", name="gate_b")
            write_manifest_exports(run_dir)
        if gate in {"C", "BCD", "ALL"} or (gate == "A" and args.continue_after_smoke):
            gate_symbol(run_dir, universe_path, "ETHUSDT", name="gate_c")
            write_manifest_exports(run_dir)
        if gate in {"D", "BCD", "ALL"} or (gate == "A" and args.continue_after_smoke):
            rest = ",".join(remaining_symbols(symbols))
            rc = invoke_cli(
                [
                    "--mode",
                    "backfill",
                    "--start-date",
                    WINDOW_START.isoformat(),
                    "--end-date-exclusive",
                    WINDOW_END_EXCL.isoformat(),
                    "--symbols",
                    rest,
                    "--workers",
                    "1",
                    "--universe-file",
                    str(universe_path),
                    "--run-dir",
                    str(run_dir),
                    "--resume",
                ]
            )
            if rc != 0:
                fail(run_dir, f"gate D exit {rc}")
            write_json(run_dir / "gate_d.json", {"ok": True, "symbols": remaining_symbols(symbols)})
            write_manifest_exports(run_dir)

        cov_after = coverage_matrix(client, symbols, days)
        write_csv(run_dir / "coverage_after.csv", cov_after)
        repo = CanonicalPublicTradeRepository(client)
        start, _ = utc_day_bounds(days[0])
        _, end = utc_day_bounds(days[-1])
        counts = repo.physical_and_logical_counts(start=start, end=end)
        write_json(
            run_dir / "duplicate_audit.json",
            {
                "physical_rows": counts["physical_rows"],
                "logical_unique_rows": counts["logical_unique_rows"],
                "physical_duplicates": int(counts["physical_rows"])
                - int(counts["logical_unique_rows"]),
                "archive_unique": cli.archive_unique_count(client),
            },
        )
        write_json(
            run_dir / "run_manifest.json",
            {
                "run_id": run_id,
                "gate": gate,
                "ok": True,
                "finished_at": _iso(),
                "pid": os.getpid(),
            },
        )
        report = run_dir / "REPORT.md"
        existing = report.read_text(encoding="utf-8") if report.is_file() else ""
        report.write_text(
            existing
            + f"\n\n## Pipeline gate `{gate}` finished\n\n"
            + f"- finished_at: {_iso()}\n"
            + f"- pid: {os.getpid()}\n"
            + f"- see checkpoint.json / coverage_after.csv / duplicate_audit.json\n",
            encoding="utf-8",
        )
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
