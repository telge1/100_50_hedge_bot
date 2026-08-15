#!/usr/bin/env python3
"""Smoke: download APT+DOGE 2026-01-06 historical trades; validate; coverage vs deep-dive events."""

from __future__ import annotations

import argparse
import csv
import json
import logging
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
    SIDE_SEMANTICS,
    count_trades_in_window,
    process_trade_day,
)

DEFAULT_OUT = (
    PROJECT_ROOT / "results" / "historical_trade_download_smoke_20260808"
)
DEFAULT_DATA = PROJECT_ROOT / "data" / "bybit_historical_trades"
EVENTS_CSV = (
    PROJECT_ROOT
    / "results"
    / "historical_structure_break_ob_deep_dive_20260808"
    / "selected_deep_dive_events.csv"
)
SMOKE_PAIRS = (("APTUSDT", "2026-01-06"), ("DOGEUSDT", "2026-01-06"))


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


def _parse_iso_ms(value: str) -> int:
    ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return int(ts.timestamp() * 1000)


def decide(results: list[dict[str, Any]], coverage: list[dict[str, Any]]) -> str:
    if not results:
        return "HISTORICAL_TRADE_SMOKE_FAILED"
    statuses = [r.get("status") for r in results]
    cov_ok = all(int(c.get("trades_in_window") or 0) > 0 for c in coverage) if coverage else False
    if all(s == "OK" for s in statuses) and cov_ok:
        return "HISTORICAL_TRADE_SMOKE_OK"
    if any(s == "OK" for s in statuses):
        return "HISTORICAL_TRADE_SMOKE_PARTIAL"
    return "HISTORICAL_TRADE_SMOKE_FAILED"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--data-root", type=Path, default=DEFAULT_DATA)
    p.add_argument("--events-csv", type=Path, default=EVENTS_CSV)
    p.add_argument("--connect-timeout", type=float, default=15.0)
    p.add_argument("--read-timeout", type=float, default=180.0)
    p.add_argument("--max-retries", type=int, default=5)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    out_dir: Path = args.out_dir
    if not out_dir.is_absolute():
        out_dir = (PROJECT_ROOT / out_dir).resolve()
    data_root: Path = args.data_root
    if not data_root.is_absolute():
        data_root = (PROJECT_ROOT / data_root).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    data_root.mkdir(parents=True, exist_ok=True)

    session = build_session()
    warmup_session(session, connect=args.connect_timeout, read=args.read_timeout)

    inventory: list[dict[str, Any]] = []
    format_samples: dict[str, Any] = {
        "endpoint": LIST_FILES_URL,
        "params": {
            "bizType": BIZ_TYPE,
            "productId": PRODUCT_ID,
            "interval": "daily",
            "periods": "",
        },
        "side_semantics": SIDE_SEMANTICS,
        "by_symbol_date": {},
    }

    for symbol, day in SMOKE_PAIRS:
        logging.info("smoke download %s %s", symbol, day)
        result, inspect = process_trade_day(
            session,
            symbol=symbol,
            day=day,
            out_root=data_root,
            connect=args.connect_timeout,
            read=args.read_timeout,
            max_retries=args.max_retries,
            do_inspect=True,
        )
        inventory.append(result.to_row())
        format_samples["by_symbol_date"][f"{symbol}_{day}"] = {
            "download": {
                k: getattr(result, k)
                for k in (
                    "status",
                    "api_http_status",
                    "api_ret_code",
                    "api_ret_msg",
                    "list_file_count",
                    "filename",
                    "download_url",
                    "reported_size",
                    "downloaded_size",
                    "archive_kind",
                    "extracted_filename",
                    "uncompressed_size",
                    "detected_format",
                    "columns",
                    "trade_count",
                    "buy_count",
                    "sell_count",
                    "first_trade_ts_raw",
                    "first_trade_ts_utc",
                    "last_trade_ts_raw",
                    "last_trade_ts_utc",
                    "min_price",
                    "max_price",
                    "error",
                )
            },
            "inspect": inspect,
        }

    # Event coverage for Jan-06 deep-dive events
    coverage_rows: list[dict[str, Any]] = []
    events_path: Path = args.events_csv
    if events_path.is_file():
        with events_path.open(newline="", encoding="utf-8") as fh:
            events = [
                r
                for r in csv.DictReader(fh)
                if r.get("date") == "2026-01-06"
            ]
        for ev in events:
            symbol = ev["symbol"]
            day = ev["date"]
            break_ts = ev.get("first_break_ts") or ev.get("available_at")
            if not break_ts:
                coverage_rows.append(
                    {
                        "event_id": ev.get("event_id"),
                        "symbol": symbol,
                        "date": day,
                        "status": "NO_BREAK_TS",
                        "trades_in_window": 0,
                    }
                )
                continue
            start_ms = _parse_iso_ms(break_ts) - 5 * 60 * 1000
            end_ms = _parse_iso_ms(break_ts) + 5 * 60 * 1000
            day_info = format_samples["by_symbol_date"].get(f"{symbol}_{day}", {})
            extracted = (day_info.get("download") or {}).get("extracted_filename")
            data_path = data_root / symbol / day / extracted if extracted else None
            if data_path is None or not data_path.is_file():
                coverage_rows.append(
                    {
                        "event_id": ev.get("event_id"),
                        "symbol": symbol,
                        "date": day,
                        "first_break_ts": break_ts,
                        "window_start_utc": datetime.fromtimestamp(
                            start_ms / 1000, tz=timezone.utc
                        ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
                        + "Z",
                        "window_end_utc": datetime.fromtimestamp(
                            end_ms / 1000, tz=timezone.utc
                        ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
                        + "Z",
                        "status": "NO_TRADE_FILE",
                        "trades_in_window": 0,
                    }
                )
                continue
            stats = count_trades_in_window(data_path, start_ms=start_ms, end_ms=end_ms)
            coverage_rows.append(
                {
                    "event_id": ev.get("event_id"),
                    "symbol": symbol,
                    "date": day,
                    "first_break_ts": break_ts,
                    "window_start_utc": datetime.fromtimestamp(
                        start_ms / 1000, tz=timezone.utc
                    ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
                    + "Z",
                    "window_end_utc": datetime.fromtimestamp(
                        end_ms / 1000, tz=timezone.utc
                    ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
                    + "Z",
                    "trades_in_window": stats.get("n"),
                    "buy_in_window": stats.get("buy"),
                    "sell_in_window": stats.get("sell"),
                    "status": "COVERED" if int(stats.get("n") or 0) > 0 else "EMPTY_WINDOW",
                }
            )
    else:
        logging.warning("events csv missing: %s", events_path)

    primary = decide(inventory, coverage_rows)
    summary = {
        "primary_decision": primary,
        "endpoint": LIST_FILES_URL,
        "params": format_samples["params"],
        "smoke_pairs": [f"{s}/{d}" for s, d in SMOKE_PAIRS],
        "inventory": inventory,
        "coverage": coverage_rows,
        "side_semantics": SIDE_SEMANTICS,
        "data_root": str(data_root),
        "artifact_dir": str(out_dir),
    }

    _write_csv(out_dir / "download_inventory.csv", inventory)
    _write_csv(out_dir / "event_trade_coverage.csv", coverage_rows)
    (out_dir / "format_sample.json").write_text(
        json.dumps(format_samples, indent=2, default=str) + "\n", encoding="utf-8"
    )
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8"
    )

    lines = [
        "# Historical Trade Download Smoke",
        "",
        f"**Primary Decision:** `{primary}`",
        "",
        "## Endpoint / params",
        "",
        f"- URL: `{LIST_FILES_URL}`",
        f"- bizType=`{BIZ_TYPE}` productId=`{PRODUCT_ID}` interval=`daily` periods=``",
        "- No browser cookies/tokens hardcoded; Akamai warmup via `/data-download`.",
        "",
        "## Code changes",
        "",
        "- `research/orderbook/bybit_historical_download_common.py` (shared session/warmup/retry/.part/ZIP/gzip)",
        "- `research/orderbook/bybit_historical_trades_download.py`",
        "- `scripts/download_bybit_historical_trades.py`",
        "- `scripts/run_historical_trade_download_smoke.py`",
        "- OB downloader left intact (no behavior change required for this smoke).",
        "",
        "## Downloads",
        "",
    ]
    for row in inventory:
        lines.append(
            f"- {row.get('symbol')} {row.get('date')}: status={row.get('status')} "
            f"file={row.get('filename')} size={row.get('downloaded_size')} "
            f"trades={row.get('trade_count')} buy={row.get('buy_count')} sell={row.get('sell_count')}"
        )
    apt = format_samples["by_symbol_date"].get("APTUSDT_2026-01-06", {}).get("inspect") or {}
    lines += [
        "",
        "## Format (APTUSDT 2026-01-06)",
        "",
        f"- detected_format: `{apt.get('detected_format')}`",
        f"- columns: `{apt.get('columns')}`",
        f"- timestamp_unit: `{apt.get('timestamp_unit')}` (UTC)",
        f"- side values: `{apt.get('side_values_seen')}`",
        "",
        "## Buy/Sell semantics",
        "",
        f"- {SIDE_SEMANTICS}",
        "",
        "## Event coverage (±5m around first_break_ts)",
        "",
    ]
    for c in coverage_rows:
        lines.append(
            f"- `{c.get('event_id')}` → {c.get('status')} n={c.get('trades_in_window')} "
            f"(buy={c.get('buy_in_window')} sell={c.get('sell_in_window')})"
        )
    lines += [
        "",
        f"Artifacts: `{out_dir}`",
        "",
    ]
    (out_dir / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("PRIMARY_DECISION", primary)
    print("OUT", out_dir)
    for row in inventory:
        print(
            row.get("symbol"),
            row.get("date"),
            row.get("status"),
            "trades=",
            row.get("trade_count"),
        )
    return 0 if primary == "HISTORICAL_TRADE_SMOKE_OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
