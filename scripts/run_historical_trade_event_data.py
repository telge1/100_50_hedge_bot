#!/usr/bin/env python3
"""Download missing OB-day historical trades + coverage for 15 deep-dive events.

Does not re-download existing Jan-06 days (skip via downloader). No OB/trade join.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from research.orderbook.bybit_historical_download_common import (  # noqa: E402
    LIST_FILES_URL,
    build_session,
    warmup_session,
)
from research.orderbook.bybit_historical_trades_download import (  # noqa: E402
    BIZ_TYPE,
    PRODUCT_ID,
    count_trades_in_window,
    process_trade_day,
)

DEFAULT_DATA = PROJECT_ROOT / "data" / "bybit_historical_trades"
DEFAULT_OUT = PROJECT_ROOT / "results" / "historical_trade_event_data_20260808"
EVENTS_CSV = (
    PROJECT_ROOT
    / "results"
    / "historical_structure_break_ob_deep_dive_20260808"
    / "selected_deep_dive_events.csv"
)

# All OB event days (inventory includes already-present Jan-06 as skip)
ALL_DAYS: dict[str, tuple[str, ...]] = {
    "APTUSDT": (
        "2025-12-29",
        "2025-12-30",
        "2026-01-06",
        "2026-01-18",
        "2026-05-12",
        "2026-05-23",
    ),
    "DOGEUSDT": (
        "2026-01-06",
        "2026-01-15",
        "2026-02-20",
        "2026-02-28",
    ),
}

ALREADY_PRESENT = {("APTUSDT", "2026-01-06"), ("DOGEUSDT", "2026-01-06")}


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for k in row:
            if k not in seen:
                seen.add(k)
                fields.append(k)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)


def _iso_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"


def _parse_iso_ms(value: str) -> int:
    ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return int(ts.timestamp() * 1000)


def resolve_csv_path(data_root: Path, symbol: str, day: str, extracted_name: str | None) -> Path | None:
    day_dir = data_root / symbol / day
    if extracted_name:
        p = day_dir / extracted_name
        if p.is_file():
            return p
    # fallback: any decompressed csv for the day
    cands = sorted(day_dir.glob(f"{symbol}{day}.csv")) + sorted(day_dir.glob("*.csv"))
    cands = [c for c in cands if c.is_file() and not c.name.endswith(".part")]
    return cands[0] if cands else None


def coverage_status(*, day_ok: bool, n: int, window_ms: int = 600_000) -> str:
    if not day_ok:
        return "TRADE_COVERAGE_MISSING"
    if n <= 0:
        return "TRADE_COVERAGE_MISSING"
    # FULL if any trades in ±5m (day file covers calendar day; window inside day)
    # PARTIAL reserved if future: sparse / truncated — for now day file + n>0 = FULL
    return "TRADE_COVERAGE_FULL"


def decide(inventory: list[dict[str, Any]], coverage: list[dict[str, Any]]) -> str:
    day_ok = all(r.get("status") == "OK" for r in inventory)
    cov_statuses = [c.get("coverage_status") for c in coverage]
    if not inventory or not coverage:
        return "HISTORICAL_TRADE_EVENT_DATA_FAILED"
    if day_ok and all(s == "TRADE_COVERAGE_FULL" for s in cov_statuses):
        return "HISTORICAL_TRADE_EVENT_DATA_READY"
    if any(r.get("status") == "OK" for r in inventory) or any(
        s == "TRADE_COVERAGE_FULL" for s in cov_statuses
    ):
        return "HISTORICAL_TRADE_EVENT_DATA_PARTIAL"
    return "HISTORICAL_TRADE_EVENT_DATA_FAILED"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-root", type=Path, default=DEFAULT_DATA)
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--events-csv", type=Path, default=EVENTS_CSV)
    p.add_argument("--connect-timeout", type=float, default=15.0)
    p.add_argument("--read-timeout", type=float, default=300.0)
    p.add_argument("--max-retries", type=int, default=5)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    data_root = args.data_root if args.data_root.is_absolute() else (PROJECT_ROOT / args.data_root)
    out_dir = args.out_dir if args.out_dir.is_absolute() else (PROJECT_ROOT / args.out_dir)
    data_root = data_root.resolve()
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    data_root.mkdir(parents=True, exist_ok=True)

    disk = shutil.disk_usage(PROJECT_ROOT)
    logging.info(
        "disk free=%.1fGB total=%.1fGB; estimate remaining trades ~0.5–1.5GB",
        disk.free / (1024**3),
        disk.total / (1024**3),
    )
    if disk.free < 5 * (1024**3):
        logging.error("insufficient free disk (<5GB); STOP")
        return 2

    session = build_session()
    warmup_session(session, connect=args.connect_timeout, read=args.read_timeout)

    inventory: list[dict[str, Any]] = []
    daily_stats: list[dict[str, Any]] = []

    for symbol, days in ALL_DAYS.items():
        for day in days:
            logging.info("process trades %s %s", symbol, day)
            result, _inspect = process_trade_day(
                session,
                symbol=symbol,
                day=day,
                out_root=data_root,
                connect=args.connect_timeout,
                read=args.read_timeout,
                max_retries=args.max_retries,
                do_inspect=True,
            )
            row = result.to_row()
            row["already_present_target"] = (symbol, day) in ALREADY_PRESENT
            row["download_action"] = (
                "SKIPPED_EXISTING"
                if result.skipped_download
                else ("DOWNLOADED" if result.status == "OK" else result.status)
            )
            inventory.append(row)

            csv_path = resolve_csv_path(
                data_root, symbol, day, result.extracted_filename
            )
            gz_path = None
            if result.filename:
                cand = data_root / symbol / day / Path(result.filename).name
                if cand.is_file():
                    gz_path = cand
            daily_stats.append(
                {
                    "symbol": symbol,
                    "date": day,
                    "status": result.status,
                    "download_action": row["download_action"],
                    "filename": result.filename,
                    "extracted_filename": result.extracted_filename,
                    "gz_bytes": gz_path.stat().st_size if gz_path else result.downloaded_size,
                    "csv_bytes": (
                        csv_path.stat().st_size
                        if csv_path
                        else result.uncompressed_size
                    ),
                    "trade_count": result.trade_count,
                    "buy_count": result.buy_count,
                    "sell_count": result.sell_count,
                    "first_trade_ts_utc": result.first_trade_ts_utc,
                    "last_trade_ts_utc": result.last_trade_ts_utc,
                    "min_price": result.min_price,
                    "max_price": result.max_price,
                    "error": result.error,
                }
            )
            logging.info(
                "%s %s → %s trades=%s action=%s",
                symbol,
                day,
                result.status,
                result.trade_count,
                row["download_action"],
            )

    # Event coverage
    coverage: list[dict[str, Any]] = []
    events_path: Path = args.events_csv
    if not events_path.is_file():
        logging.error("missing events csv: %s", events_path)
        return 1
    with events_path.open(newline="", encoding="utf-8") as fh:
        events = list(csv.DictReader(fh))
    if len(events) != 15:
        logging.warning("expected 15 deep-dive events, got %s", len(events))

    inv_by = {(r["symbol"], r["date"]): r for r in inventory}
    for ev in events:
        symbol = ev["symbol"]
        day = ev["date"]
        eid = ev.get("event_id")
        break_ts = ev.get("first_break_ts") or ""
        inv = inv_by.get((symbol, day))
        day_ok = bool(inv and inv.get("status") == "OK")
        base = {
            "event_id": eid,
            "symbol": symbol,
            "date": day,
            "direction": ev.get("direction"),
            "structure_type": ev.get("structure_type"),
            "first_break_ts": break_ts,
            "day_trade_status": inv.get("status") if inv else "NO_INVENTORY",
        }
        if not break_ts:
            coverage.append(
                {
                    **base,
                    "coverage_status": "TRADE_COVERAGE_MISSING",
                    "trades_in_window": 0,
                    "reason": "missing_first_break_ts",
                }
            )
            continue
        start_ms = _parse_iso_ms(break_ts) - 5 * 60 * 1000
        end_ms = _parse_iso_ms(break_ts) + 5 * 60 * 1000
        base["window_start_utc"] = _iso_ms(start_ms)
        base["window_end_utc"] = _iso_ms(end_ms)
        base["trade_ts_to_ob_note"] = "trade_ts_ms = trade_timestamp_seconds * 1000; OB ts is ms"
        csv_path = resolve_csv_path(
            data_root,
            symbol,
            day,
            inv.get("extracted_filename") if inv else None,
        )
        if not day_ok or csv_path is None:
            coverage.append(
                {
                    **base,
                    "coverage_status": "TRADE_COVERAGE_MISSING",
                    "trades_in_window": 0,
                    "reason": "missing_day_file" if csv_path is None else "day_not_ok",
                }
            )
            continue
        stats = count_trades_in_window(csv_path, start_ms=start_ms, end_ms=end_ms)
        n = int(stats.get("n") or 0)
        status = coverage_status(day_ok=True, n=n)
        # If window spills outside calendar day file span, mark PARTIAL
        first_day = inv.get("first_trade_ts_utc") if inv else None
        last_day = inv.get("last_trade_ts_utc") if inv else None
        if first_day and last_day:
            day_start = _parse_iso_ms(first_day)
            day_end = _parse_iso_ms(last_day)
            if start_ms < day_start or end_ms > day_end + 60_000:
                if n > 0:
                    status = "TRADE_COVERAGE_PARTIAL"
                else:
                    status = "TRADE_COVERAGE_MISSING"
        coverage.append(
            {
                **base,
                "coverage_status": status,
                "trades_in_window": n,
                "buy_in_window": stats.get("buy"),
                "sell_in_window": stats.get("sell"),
                "first_trade_in_window_utc": stats.get("first_trade_ts_utc"),
                "last_trade_in_window_utc": stats.get("last_trade_ts_utc"),
                "csv_path": str(csv_path),
            }
        )

    primary = decide(inventory, coverage)
    from collections import Counter

    cov_counts = Counter(c.get("coverage_status") for c in coverage)
    actions = Counter(r.get("download_action") for r in inventory)
    total_gz = sum(int(r.get("gz_bytes") or 0) for r in daily_stats)
    total_csv = sum(int(r.get("csv_bytes") or 0) for r in daily_stats)
    disk_after = shutil.disk_usage(PROJECT_ROOT)

    summary = {
        "primary_decision": primary,
        "endpoint": LIST_FILES_URL,
        "params": {"bizType": BIZ_TYPE, "productId": PRODUCT_ID, "interval": "daily"},
        "n_inventory_days": len(inventory),
        "download_actions": dict(actions),
        "coverage_counts": dict(cov_counts),
        "total_gz_bytes": total_gz,
        "total_csv_bytes": total_csv,
        "total_bytes_approx": total_gz + total_csv,
        "disk_free_bytes_before_approx": disk.free,
        "disk_free_bytes_after": disk_after.free,
        "data_root": str(data_root),
        "artifact_dir": str(out_dir),
        "n_events": len(coverage),
    }

    _write_csv(out_dir / "download_inventory.csv", inventory)
    _write_csv(out_dir / "daily_trade_statistics.csv", daily_stats)
    _write_csv(out_dir / "event_trade_coverage.csv", coverage)
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8"
    )

    lines = [
        "# Historical Trade Event Data",
        "",
        f"**Primary Decision:** `{primary}`",
        "",
        f"Endpoint: `{LIST_FILES_URL}` · bizType=`{BIZ_TYPE}` productId=`{PRODUCT_ID}`",
        "",
        "## Download actions",
        "",
        f"- {dict(actions)}",
        "",
        "## Daily statistics",
        "",
    ]
    for r in daily_stats:
        gz_mb = (int(r.get("gz_bytes") or 0) / (1024 * 1024))
        csv_mb = (int(r.get("csv_bytes") or 0) / (1024 * 1024))
        lines.append(
            f"- {r['symbol']} {r['date']}: {r['status']} action={r['download_action']} "
            f"trades={r['trade_count']} buy={r['buy_count']} sell={r['sell_count']} "
            f"gz={gz_mb:.1f}MB csv={csv_mb:.1f}MB "
            f"span={r['first_trade_ts_utc']}→{r['last_trade_ts_utc']}"
        )
    lines += [
        "",
        f"Totals: gz={total_gz/(1024**2):.1f}MB csv={total_csv/(1024**2):.1f}MB "
        f"combined≈{(total_gz+total_csv)/(1024**2):.1f}MB",
        "",
        "## Event coverage (15 deep-dive events, ±5m around first_break_ts)",
        "",
        f"- counts: {dict(cov_counts)}",
        "",
    ]
    for c in coverage:
        lines.append(
            f"- `{c.get('event_id')}` → {c.get('coverage_status')} "
            f"n={c.get('trades_in_window')} "
            f"({c.get('first_trade_in_window_utc')}→{c.get('last_trade_in_window_utc')})"
        )
    lines += [
        "",
        f"Disk free after: {disk_after.free/(1024**3):.1f}GB",
        f"Artifacts: `{out_dir}`",
        "",
    ]
    (out_dir / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("PRIMARY_DECISION", primary)
    print("ACTIONS", dict(actions))
    print("COVERAGE", dict(cov_counts))
    print("TOTAL_MB", round((total_gz + total_csv) / (1024**2), 1))
    print("OUT", out_dir)
    return 0 if primary == "HISTORICAL_TRADE_EVENT_DATA_READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
