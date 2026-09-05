"""Read-only discovery of BTC/DOGE research source roots and manifests."""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .clickhouse import connect, rows
from .config import OB200_ROOT, REPO_ROOT
from .contracts import ALLOWED_SYMBOLS, sanitize_json, stable_hash
from .full_history_contracts import (
    DAY_ZIP_PRODUCER_ID,
    LIVE_PRODUCER_ID,
    LIVE_RAW_FROM,
    LIVE_TERMINAL,
    LIVE_TERMINAL_REASON,
    RESULT_ROOT_SOURCE_RECOVERY,
    SHADOW_ARCHIVE_PRODUCER_ID,
)
from .source_file_registry import load_source_file

OB200_NAME_RE = re.compile(
    r"^(?P<symbol>BTCUSDT|DOGEUSDT)_"
    r"(?P<start>\d{8}T\d{6}Z)_"
    r"(?P<end>\d{8}T\d{6}Z)_ob200_v3\.zst$"
)

KNOWN_ROOTS = [
    {
        "source_id": "filesystem_ob200_shadow",
        "source_type": "SHADOW_ARCHIVE",
        "producer_id": SHADOW_ARCHIVE_PRODUCER_ID,
        "path": str(OB200_ROOT),
        "semantics": "raw_ob200_event_time_eos_v1",
    },
    {
        "source_id": "clickhouse_public_trades",
        "source_type": "CLICKHOUSE",
        "producer_id": "CLICKHOUSE_CANONICAL",
        "path": "orderbook_analysis.public_trades_canonical",
        "semantics": "public_trade_taker_aggressor_v1",
    },
    {
        "source_id": "clickhouse_open_interest",
        "source_type": "CLICKHOUSE",
        "producer_id": "CLICKHOUSE_OI_5S",
        "path": "orderbook_analysis.open_interest_5s",
        "semantics": "open_interest_5s_v1",
    },
    {
        "source_id": "clickhouse_liquidations",
        "source_type": "CLICKHOUSE",
        "producer_id": "CLICKHOUSE_LIQUIDATIONS",
        "path": "orderbook_analysis.all_liquidations",
        "semantics": "liquidation_flow_facts_v1",
    },
    {
        "source_id": "clickhouse_candles",
        "source_type": "CLICKHOUSE",
        "producer_id": "CLICKHOUSE_CANDLES_1M",
        "path": "signal_generator.candles_1m",
        "semantics": "candles_1m_v1",
    },
    {
        "source_id": "clickhouse_ob_features",
        "source_type": "CLICKHOUSE_DERIVED",
        "producer_id": "CLICKHOUSE_OB_FEATURES_1S",
        "path": "orderbook_analysis.orderbook_features_1s_v2",
        "semantics": "derived_ob200_v3",
        "import_eligible": False,
    },
]

MANIFEST_GLOBS = [
    REPO_ROOT / "results/research_db_phase_1_pilot_v1/*manifest*.json",
    REPO_ROOT / "results/research_db_phase_0_inventory_contract_v1/preflight_manifest.json",
    REPO_ROOT / "results/btc_ob_fight_cases/**/manifest*.json",
]


def _parse_ob200_filename(name: str) -> dict[str, str] | None:
    match = OB200_NAME_RE.match(name)
    if not match:
        return None
    return match.groupdict()


def _discover_ob200_files() -> list[dict[str, Any]]:
    discovered: list[dict[str, Any]] = []
    for symbol in sorted(ALLOWED_SYMBOLS):
        symbol_root = OB200_ROOT / symbol
        if not symbol_root.is_dir():
            continue
        for path in sorted(symbol_root.rglob("*_ob200_v3.zst")):
            parsed = _parse_ob200_filename(path.name)
            if not parsed:
                continue
            start = datetime.strptime(parsed["start"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            end = datetime.strptime(parsed["end"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            zero_duration = end <= start
            hour_in_live = LIVE_RAW_FROM <= start < LIVE_TERMINAL
            hour_after_terminal = start >= LIVE_TERMINAL
            if hour_in_live:
                producer = LIVE_PRODUCER_ID
            else:
                producer = SHADOW_ARCHIVE_PRODUCER_ID
            try:
                source = load_source_file(path, OB200_ROOT)
                fingerprint = source.fingerprint
                manifest = source.manifest
                manifest_start = source.segment_start.isoformat().replace("+00:00", "Z")
                manifest_end = source.segment_end.isoformat().replace("+00:00", "Z")
            except Exception as exc:
                fingerprint = stable_hash({"error": str(exc), "path": str(path)})
                manifest = {}
                manifest_start = start.isoformat().replace("+00:00", "Z")
                manifest_end = end.isoformat().replace("+00:00", "Z")
            discovered.append(
                {
                    "symbol": symbol,
                    "source_id": "filesystem_ob200_shadow",
                    "source_type": "SHADOW_ARCHIVE",
                    "producer_id": producer,
                    "relative_path": str(path.relative_to(OB200_ROOT)),
                    "filename_start": start.isoformat().replace("+00:00", "Z"),
                    "filename_end": end.isoformat().replace("+00:00", "Z"),
                    "manifest_start": manifest_start,
                    "manifest_end": manifest_end,
                    "segment_start": start.isoformat().replace("+00:00", "Z"),
                    "segment_end": end.isoformat().replace("+00:00", "Z"),
                    "utc_day": start.strftime("%Y-%m-%d"),
                    "source_fingerprint": fingerprint,
                    "zero_duration": zero_duration,
                    "boundary_stub": zero_duration,
                    "boundary_role": "ZERO_DURATION_AUXILIARY" if zero_duration else "",
                    "format_version": manifest.get("format_version", ""),
                    "parser_version": manifest.get("parser_version", ""),
                    "queue_overflow": manifest.get("queue_overflow"),
                    "writer_errors": manifest.get("writer_errors"),
                    "bytes": path.stat().st_size,
                    "hour_after_queue_full": int(hour_after_terminal),
                    "hour_in_live_before_terminal": int(hour_in_live),
                }
            )
    return discovered


def _ch_table_coverage(client: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    queries = {
        "PUBLIC_TRADES": (
            "orderbook_analysis.public_trades_canonical",
            "trade_ts",
        ),
        "OPEN_INTEREST": (
            "orderbook_analysis.open_interest_5s",
            "bucket_time",
        ),
        "LIQUIDATIONS": (
            "orderbook_analysis.all_liquidations",
            "event_time",
        ),
        "CANDLES": (
            "signal_generator.candles_1m",
            "open_time",
        ),
    }
    for symbol in sorted(ALLOWED_SYMBOLS):
        for modality, (table, ts_col) in queries.items():
            extra = " AND interval='1m'" if modality == "CANDLES" else ""
            result = rows(
                client,
                f"""SELECT count(), min({ts_col}), max({ts_col}),
                           uniqExact(toDate({ts_col}))
                    FROM {table}
                    WHERE symbol=%(symbol)s{extra}""",
                {"symbol": symbol},
            )[0]
            out.append(
                {
                    "symbol": symbol,
                    "modality": modality,
                    "table": table,
                    "row_count": int(result[0]),
                    "min_ts": str(result[1]) if result[1] else "",
                    "max_ts": str(result[2]) if result[2] else "",
                    "distinct_days": int(result[3]),
                }
            )
    ob_features = rows(
        client,
        """SELECT symbol, count(), min(bucket_start), max(bucket_start)
           FROM orderbook_analysis.orderbook_features_1s_v2
           WHERE depth=200 AND parser_version='ob200_v3'
             AND symbol IN ('BTCUSDT','DOGEUSDT')
           GROUP BY symbol ORDER BY symbol""",
    )
    for symbol, count, min_ts, max_ts in ob_features:
        out.append(
            {
                "symbol": symbol,
                "modality": "OB200_DERIVED_CH",
                "table": "orderbook_analysis.orderbook_features_1s_v2",
                "row_count": int(count),
                "min_ts": str(min_ts),
                "max_ts": str(max_ts),
                "distinct_days": 0,
                "import_eligible": False,
            }
        )
    return out


def _manifest_references() -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for pattern in MANIFEST_GLOBS:
        for path in sorted(REPO_ROOT.glob(str(pattern.relative_to(REPO_ROOT)))):
            if not path.is_file():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            refs.append(
                {
                    "manifest_path": str(path.relative_to(REPO_ROOT)),
                    "symbol": payload.get("symbol", ""),
                    "window_start": payload.get("window_start", ""),
                    "window_end": payload.get("window_end", ""),
                    "source_paths": [
                        item.get("source_relative_path", "")
                        for item in payload.get("source_files", [])
                        if isinstance(item, dict)
                    ],
                }
            )
    return refs


def _previously_missed(old_inventory_path: Path, ob_files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    missed: list[dict[str, Any]] = []
    if not old_inventory_path.is_file():
        return missed
    excluded_days: set[tuple[str, str]] = set()
    with old_inventory_path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("eligible", "").lower() != "true":
                excluded_days.add((row["symbol"], row["utc_day"]))
    ob_by_day: dict[tuple[str, str], int] = {}
    for item in ob_files:
        key = (item["symbol"], item["utc_day"])
        ob_by_day[key] = ob_by_day.get(key, 0) + 1
    for (symbol, day), file_count in sorted(ob_by_day.items()):
        if (symbol, day) not in excluded_days:
            continue
        if file_count == 0:
            continue
        reason = "AFTER_QUEUE_FULL" if day >= LIVE_TERMINAL.strftime("%Y-%m-%d") else "NO_OB200_RAW_FILES"
        missed.append(
            {
                "symbol": symbol,
                "utc_day": day,
                "ob200_files_found": file_count,
                "previous_exclusion": reason,
                "reconciliation": (
                    "SHADOW_ARCHIVE files exist but old gate treated day as "
                    f"{reason} without modality-scoped OB import"
                ),
            }
        )
    return missed


def build_source_discovery() -> dict[str, Any]:
    client = connect()
    try:
        ob_files = _discover_ob200_files()
        table_cov = _ch_table_coverage(client)
        manifests = _manifest_references()
        old_inv = REPO_ROOT / "results/btc_doge_research_db_full_history_v1/excluded_days.csv"
        missed = _previously_missed(old_inv, ob_files)
        by_symbol_day: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in ob_files:
            key = (item["symbol"], item["utc_day"])
            by_symbol_day.setdefault(key, []).append(item)
        reconciliation = {
            "golden_case_btc_20260831": {
                "reference": "results/btc_ob_fight_cases/20260831T190000Z/run_018",
                "ob200_root": str(OB200_ROOT),
                "files_on_disk": len(by_symbol_day.get(("BTCUSDT", "2026-08-31"), [])),
                "previous_exclusion_reason": "AFTER_QUEUE_FULL",
                "actual_cause": (
                    "full_history_inventory._producer_for_day() invalidated entire UTC days "
                    f"after {LIVE_TERMINAL.isoformat()} for LIVE_PRODUCER_ID even when "
                    f"{SHADOW_ARCHIVE_PRODUCER_ID} hourly zst files exist under {OB200_ROOT}. "
                    "The raw-archive-only collector (PID 3946369) continued writing shadow "
                    "files after queue_full; queue_full must only invalidate the live "
                    "producer stream, not shadow archive segments."
                ),
            },
            "day_zip_period_no_ob200_raw_files": {
                "previous_exclusion_reason": "NO_OB200_RAW_FILES",
                "actual_cause": (
                    "Old inventory skipped filesystem scan for days before LIVE_RAW_FROM "
                    "and assumed Day-ZIP producer without verifying FS paths. "
                    f"Shadow archive under {OB200_ROOT} begins {LIVE_RAW_FROM.isoformat()}; "
                    "earlier days have no FS OB200 but may still have trades/OI/candles in ClickHouse."
                ),
            },
            "queue_full_scope": {
                "terminal": LIVE_TERMINAL.isoformat().replace("+00:00", "Z"),
                "terminal_reason": LIVE_TERMINAL_REASON,
                "rule": "queue_full invalidates LIVE_COLLECTOR only from terminal onward",
            },
            "previously_missed_symbol_days": len(missed),
        }
        return sanitize_json(
            {
                "known_roots": KNOWN_ROOTS,
                "ob200_files": ob_files,
                "table_coverage": table_cov,
                "manifest_references": manifests,
                "previously_missed": missed,
                "reconciliation": reconciliation,
                "summary": {
                    "ob200_file_count": len(ob_files),
                    "symbols": sorted(ALLOWED_SYMBOLS),
                    "ob200_days_btc": sorted({f["utc_day"] for f in ob_files if f["symbol"] == "BTCUSDT"}),
                    "ob200_days_doge": sorted({f["utc_day"] for f in ob_files if f["symbol"] == "DOGEUSDT"}),
                },
            }
        )
    finally:
        client.close()


def write_source_discovery(result: dict[str, Any]) -> None:
    RESULT_ROOT_SOURCE_RECOVERY.mkdir(parents=True, exist_ok=True)
    discovered = [
        {
            "source_id": root["source_id"],
            "source_type": root["source_type"],
            "producer_id": root["producer_id"],
            "path": root["path"],
            "semantics": root["semantics"],
            "import_eligible": root.get("import_eligible", True),
        }
        for root in KNOWN_ROOTS
    ]
    with (RESULT_ROOT_SOURCE_RECOVERY / "discovered_sources.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(discovered[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(discovered)
    ob_fields = sorted({k for row in result["ob200_files"] for k in row})
    with (RESULT_ROOT_SOURCE_RECOVERY / "source_files_by_symbol_day.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ob_fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(result["ob200_files"])
    table_fields = sorted({k for row in result["table_coverage"] for k in row})
    with (RESULT_ROOT_SOURCE_RECOVERY / "source_table_coverage.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=table_fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(result["table_coverage"])
    missed_fields = sorted({k for row in result["previously_missed"] for k in row}) or ["symbol"]
    with (RESULT_ROOT_SOURCE_RECOVERY / "previously_missed_sources.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=missed_fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(result["previously_missed"])
    (RESULT_ROOT_SOURCE_RECOVERY / "source_lineage_reconciliation.json").write_text(
        json.dumps(sanitize_json(result["reconciliation"]), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def run() -> dict[str, Any]:
    result = build_source_discovery()
    write_source_discovery(result)
    return result["summary"]


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, sort_keys=True))
