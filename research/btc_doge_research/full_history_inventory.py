"""Coverage inventory for resumable BTC/DOGE full-history backfill."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .clickhouse import connect, rows
from .config import OB200_ROOT
from .contracts import ALLOWED_SYMBOLS, sanitize_json, stable_hash
from .full_history_contracts import (
    DAY_ZIP_END,
    DAY_ZIP_PRODUCER_ID,
    EXPECTED_CANDLES,
    EXPECTED_OB_FILES,
    EXPECTED_OB_SECONDS,
    EXPECTED_OI,
    FULL_HISTORY_CONTRACT_VERSION,
    LIVE_PRODUCER_ID,
    LIVE_RAW_FROM,
    LIVE_TERMINAL,
    LIVE_TERMINAL_REASON,
    OB_SEMANTICS,
    ordering_ambiguous_for_day,
    pilot_batch_id,
)
from .ob200_parser import iter_json_records
from .source_file_registry import load_source_file

RESULT_ROOT = Path(__file__).resolve().parents[2] / "results" / "btc_doge_research_db_full_history_v1"


def _day_range(start: datetime, end: datetime):
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day < end:
        yield day
        day += timedelta(days=1)


def _audit_ob_day(symbol: str, day: datetime) -> dict[str, Any]:
    gaps = 0
    dup = 0
    files = 0
    fingerprints: list[str] = []
    for hour in range(24):
        hour_start = day + timedelta(hours=hour)
        hour_end = hour_start + timedelta(hours=1)
        name = (
            f"{symbol}_{hour_start:%Y%m%dT%H%M%SZ}_{hour_end:%Y%m%dT%H%M%SZ}_ob200_v3.zst"
        )
        path = (
            OB200_ROOT / symbol / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}" / name
        )
        if not path.exists():
            return {
                "files": files,
                "expected_files": EXPECTED_OB_FILES,
                "gaps": gaps,
                "duplicate_u": dup,
                "complete": False,
                "source_fingerprint": stable_hash({"missing": name}),
                "queue_overflow": None,
                "writer_errors": None,
            }
        source = load_source_file(path, OB200_ROOT)
        fingerprints.append(source.fingerprint)
        files += 1
        last_u = None
        valid = False
        for _, obj in iter_json_records(path):
            typ = obj.get("type")
            if typ not in ("snapshot", "rotation_checkpoint", "delta"):
                continue
            data = obj["data"]
            u = int(data.get("u") or 0)
            if typ in ("snapshot", "rotation_checkpoint"):
                valid = True
                last_u = u
            else:
                if not valid:
                    gaps += 1
                elif u == last_u:
                    dup += 1
                elif u != last_u + 1:
                    gaps += 1
                last_u = u
    manifest = source.manifest if files else {}
    return {
        "files": files,
        "expected_files": EXPECTED_OB_FILES,
        "gaps": gaps,
        "duplicate_u": dup,
        "complete": files == EXPECTED_OB_FILES and gaps == 0,
        "source_fingerprint": stable_hash(sorted(fingerprints)),
        "queue_overflow": manifest.get("queue_overflow"),
        "writer_errors": manifest.get("writer_errors"),
    }


def _ch_day_metrics(client: Any, symbol: str, day: datetime) -> dict[str, Any]:
    start = day
    end = day + timedelta(days=1)
    trade = rows(
        client,
        """SELECT count(), uniqExact(trade_id), min(trade_ts), max(trade_ts)
        FROM orderbook_analysis.public_trades_canonical
        WHERE symbol=%(symbol)s AND trade_ts>=%(start)s AND trade_ts<%(end)s""",
        {"symbol": symbol, "start": start, "end": end},
    )[0]
    oi = rows(
        client,
        """SELECT count(), uniqExact(bucket_time), min(bucket_time), max(bucket_time)
        FROM orderbook_analysis.open_interest_5s
        WHERE symbol=%(symbol)s AND bucket_time>=%(start)s AND bucket_time<%(end)s""",
        {"symbol": symbol, "start": start, "end": end},
    )[0]
    candles = rows(
        client,
        """SELECT count(), uniqExact(open_time), min(open_time), max(open_time)
        FROM signal_generator.candles_1m FINAL
        WHERE symbol=%(symbol)s AND interval='1m'
          AND open_time>=%(start)s AND open_time<%(end)s""",
        {"symbol": symbol, "start": start, "end": end},
    )[0]
    liq = rows(
        client,
        """SELECT count(), uniqExact(event_key), min(event_time), max(event_time)
        FROM orderbook_analysis.all_liquidations
        WHERE symbol=%(symbol)s AND event_time>=%(start)s AND event_time<%(end)s""",
        {"symbol": symbol, "start": start, "end": end},
    )[0]
    return {
        "trade_count": int(trade[0]),
        "trade_unique": int(trade[1]),
        "trade_min": trade[2],
        "trade_max": trade[3],
        "oi_count": int(oi[0]),
        "oi_unique": int(oi[1]),
        "oi_min": oi[2],
        "oi_max": oi[3],
        "candle_count": int(candles[0]),
        "candle_unique": int(candles[1]),
        "liq_count": int(liq[0]),
        "liq_unique": int(liq[1]),
    }


def _producer_for_day(day: datetime) -> tuple[str, str, bool, str | None]:
    day_end = day + timedelta(days=1)
    if day < LIVE_RAW_FROM.replace(hour=0, minute=0, second=0, microsecond=0):
        if day <= DAY_ZIP_END:
            return DAY_ZIP_PRODUCER_ID, "EVENT_TIME_END_OF_SECOND", True, "BOUNDED_IMPORT_WINDOW_END"
        return "NONE", "NOT_AVAILABLE", False, "NO_PRODUCER"
    if day_end <= LIVE_RAW_FROM:
        return "NONE", "NOT_AVAILABLE", False, "BEFORE_LIVE_RAW"
    if day <= LIVE_TERMINAL.replace(hour=0, minute=0, second=0, microsecond=0):
        if day_end > LIVE_TERMINAL:
            return LIVE_PRODUCER_ID, OB_SEMANTICS, False, LIVE_TERMINAL_REASON
        return LIVE_PRODUCER_ID, OB_SEMANTICS, True, None
    return LIVE_PRODUCER_ID, OB_SEMANTICS, False, "AFTER_QUEUE_FULL"


def build_inventory() -> dict[str, Any]:
    client = connect()
    inventory_rows: list[dict[str, Any]] = []
    excluded_rows: list[dict[str, Any]] = []
    segment_rows: list[dict[str, Any]] = []
    gap_rows: list[dict[str, Any]] = []
    try:
        start = datetime(2026, 7, 19, tzinfo=timezone.utc)
        end = datetime(2026, 9, 1, tzinfo=timezone.utc)
        for symbol in sorted(ALLOWED_SYMBOLS):
            for day in _day_range(start, end):
                day_str = day.strftime("%Y-%m-%d")
                producer_id, semantics, producer_complete, terminal = _producer_for_day(day)
                ch = _ch_day_metrics(client, symbol, day)
                ob = (
                    _audit_ob_day(symbol, day)
                    if producer_id == LIVE_PRODUCER_ID and day >= LIVE_RAW_FROM.replace(hour=0, minute=0, second=0, microsecond=0)
                    else {
                        "files": 0,
                        "expected_files": EXPECTED_OB_FILES,
                        "gaps": 0,
                        "duplicate_u": 0,
                        "complete": False,
                        "source_fingerprint": "NOT_APPLICABLE",
                        "queue_overflow": None,
                        "writer_errors": None,
                    }
                )
                reasons: list[str] = []
                if producer_id == "NONE":
                    reasons.append("NO_PRODUCER")
                if day < LIVE_RAW_FROM.replace(hour=0, minute=0, second=0, microsecond=0):
                    reasons.append("NO_OB200_RAW_FILES")
                if terminal == LIVE_TERMINAL_REASON:
                    reasons.append("QUEUE_FULL_PARTIAL_DAY")
                if terminal == "AFTER_QUEUE_FULL":
                    reasons.append("AFTER_QUEUE_FULL")
                if ch["trade_count"] == 0:
                    reasons.append("NO_TRADES")
                if ch["candle_count"] != EXPECTED_CANDLES:
                    reasons.append("INCOMPLETE_CANDLES")
                if ch["oi_count"] != EXPECTED_OI or ch["oi_unique"] != EXPECTED_OI:
                    reasons.append("INCOMPLETE_OI")
                if producer_id == LIVE_PRODUCER_ID and not ob["complete"]:
                    reasons.append("INCOMPLETE_OB200")
                if ob.get("gaps"):
                    reasons.append("OB200_U_GAP")
                if ordering_ambiguous_for_day(symbol, day):
                    reasons.append("ORDERING_AMBIGUOUS_PRESENT")
                eligible = not reasons or reasons == ["ORDERING_AMBIGUOUS_PRESENT"]
                row = {
                    "symbol": symbol,
                    "utc_day": day_str,
                    "producer_id": producer_id,
                    "source_semantics": semantics,
                    "coverage_start": day.isoformat().replace("+00:00", "Z"),
                    "coverage_end": (day + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
                    "terminal_reason": terminal or "",
                    "coverage_complete": producer_complete and eligible,
                    "trade_count": ch["trade_count"],
                    "oi_count": ch["oi_count"],
                    "candle_count": ch["candle_count"],
                    "liq_count": ch["liq_count"],
                    "ob200_files": ob["files"],
                    "ob200_u_gaps": ob["gaps"],
                    "source_fingerprint": ob["source_fingerprint"],
                    "eligible": eligible,
                    "exclusion_reasons": ";".join(reasons),
                    "ordering_ambiguous_count": len(ordering_ambiguous_for_day(symbol, day)),
                }
                inventory_rows.append(row)
                if not eligible:
                    excluded_rows.append(row)
                if ob["gaps"]:
                    gap_rows.append(
                        {
                            "symbol": symbol,
                            "utc_day": day_str,
                            "gap_type": "OB200_U_GAP",
                            "count": ob["gaps"],
                        }
                    )
                segment_rows.append(
                    {
                        "symbol": symbol,
                        "utc_day": day_str,
                        "producer_id": producer_id,
                        "source_semantics": semantics,
                        "source_fingerprint": ob["source_fingerprint"],
                        "terminal_reason": terminal or "",
                    }
                )
        pilot_ready = rows(
            client,
            f"""SELECT count() FROM btc_doge_research.research_batch_runs
            WHERE batch_id='{pilot_batch_id()}' AND status='READY'""",
        )[0][0]
        summary = {
            "contract_version": FULL_HISTORY_CONTRACT_VERSION,
            "eligible_symbol_days": sum(1 for r in inventory_rows if r["eligible"]),
            "excluded_symbol_days": len(excluded_rows),
            "eligible_days_btc": sorted({r["utc_day"] for r in inventory_rows if r["eligible"] and r["symbol"] == "BTCUSDT"}),
            "eligible_days_doge": sorted({r["utc_day"] for r in inventory_rows if r["eligible"] and r["symbol"] == "DOGEUSDT"}),
            "pilot_ready": int(pilot_ready),
            "pilot_day": "2026-08-26",
        }
        return {
            "inventory_rows": inventory_rows,
            "excluded_rows": excluded_rows,
            "segment_rows": segment_rows,
            "gap_rows": gap_rows,
            "summary": summary,
        }
    finally:
        client.close()


def write_inventory(result: dict[str, Any]) -> None:
    RESULT_ROOT.mkdir(parents=True, exist_ok=True)
    with (RESULT_ROOT / "full_history_coverage_inventory.csv").open("w", encoding="utf-8", newline="") as handle:
        fields = sorted({k for row in result["inventory_rows"] for k in row})
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(result["inventory_rows"])
    with (RESULT_ROOT / "excluded_days.csv").open("w", encoding="utf-8", newline="") as handle:
        fields = sorted({k for row in result["excluded_rows"] for k in row}) or ["symbol"]
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(result["excluded_rows"])
    with (RESULT_ROOT / "producer_segments.csv").open("w", encoding="utf-8", newline="") as handle:
        fields = sorted({k for row in result["segment_rows"] for k in row})
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(result["segment_rows"])
    with (RESULT_ROOT / "source_gaps.csv").open("w", encoding="utf-8", newline="") as handle:
        fields = sorted({k for row in result["gap_rows"] for k in row}) or ["symbol"]
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(result["gap_rows"])
    (RESULT_ROOT / "full_history_coverage_summary.json").write_text(
        json.dumps(sanitize_json(result["summary"]), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def run() -> dict[str, Any]:
    result = build_inventory()
    write_inventory(result)
    return sanitize_json(result["summary"])


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, sort_keys=True))
