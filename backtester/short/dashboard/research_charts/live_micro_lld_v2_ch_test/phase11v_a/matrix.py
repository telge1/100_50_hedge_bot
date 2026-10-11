"""Coverage matrix for five timeframes and PD1–PD10. Reads manifests only."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ..phase11u_c.coverage import (
    BASE_PRICE_HASH,
    BASE_RANGE_END_EXCLUSIVE,
    BASE_RANGE_START,
    BASE_REVISION_HASH,
    BASE_SNAPSHOT_ID,
    base_file_status,
    preset_warmup_bars,
    warmup_source_start,
)
from ..phase11u_d.multi_file import FilePart, _ordered_records, composite_input_id

REFERENCE_CLOSE = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
TIMEFRAMES = ("5m", "15m", "30m", "1h", "4h")
EXPECTED_COMPOSITE_ID = "4b509b6565b06c1e37a09f195f001b55e7517de29355a0482871798472de7d31"
TAIL_SNAPSHOT_ID = "79a5e932749c8539d9caf0ff24911741b4492f6c08de9ba1ae187492233f8394"
TAIL_PRICE_HASH = "deeabd3fb8228e51b73cddd5da71cd847bc3197ab60e6f1c09106ee988c25c1f"
TAIL_REVISION_HASH = "a139f4c7c14e8c7b52cd79d7e02c5448a81b05278ce8b08bc4e3bd50f4f9ecea"
SYMBOL = "DOGEUSDT"


def _first_manifest(root: Path, snapshot_id: str, price_hash: str, revision_hash: str) -> Path:
    matches = sorted(root.glob(f"**/{snapshot_id}.manifest.json"))
    for path in matches:
        text = path.read_text(encoding="utf-8")
        if price_hash in text and revision_hash in text and f'"snapshot_id": "{snapshot_id}"' in text:
            return path.parent
    raise FileNotFoundError(snapshot_id)


def verify_composite(root: Path) -> dict:
    base_dir = _first_manifest(root, BASE_SNAPSHOT_ID, BASE_PRICE_HASH, BASE_REVISION_HASH)
    tail_dir = _first_manifest(root, TAIL_SNAPSHOT_ID, TAIL_PRICE_HASH, TAIL_REVISION_HASH)
    parts = [
        FilePart(base_dir, BASE_SNAPSHOT_ID, BASE_PRICE_HASH, BASE_REVISION_HASH),
        FilePart(tail_dir, TAIL_SNAPSHOT_ID, TAIL_PRICE_HASH, TAIL_REVISION_HASH),
    ]
    records = _ordered_records(parts)
    composite_id = composite_input_id(records)
    return {
        "composite_id": composite_id,
        "matches_expected": composite_id == EXPECTED_COMPOSITE_ID,
        "range_start": records[0]["range_start"],
        "range_end_exclusive": records[-1]["range_end_exclusive"],
        "base_directory": str(base_dir),
        "tail_directory": str(tail_dir),
        "row_counts": [int(record["row_count"]) for record in records],
    }


def coverage_matrix(decision_close: datetime = REFERENCE_CLOSE) -> list[dict]:
    rows = []
    for timeframe in TIMEFRAMES:
        for pool_detail in range(1, 11):
            candle = base_file_status(decision_close, timeframe, pool_detail)
            candle_ready = candle == "READY"
            source = warmup_source_start(decision_close, timeframe, pool_detail)
            rows.append(
                {
                    "symbol": SYMBOL,
                    "timeframe": timeframe,
                    "pool_detail": pool_detail,
                    "warmup_bars": preset_warmup_bars(pool_detail),
                    "source_start": source.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "immutable_range_start": BASE_RANGE_START.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "immutable_decision_bar_end": BASE_RANGE_END_EXCLUSIVE.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "CANDLE_INPUT_READY": candle_ready,
                    "ENGINE_INPUT_READY": candle_ready,
                    "candle_status": candle,
                    "ORIGINAL_PUBLICATION_AVAILABLE": False,
                    "REMAINING_PROJECTION_READY": False,
                    "HISTORICAL_HEATMAP_READY": False,
                }
            )
    return rows


def apply_publications(rows: list[dict], publications: dict[tuple[str, int], dict]) -> None:
    for row in rows:
        key = (row["timeframe"], int(row["pool_detail"]))
        found = publications.get(key)
        if not found:
            continue
        available = int(found.get("snapshot_count") or 0) == 1 and int(found.get("catalog_rows") or 0) > 0
        row["ORIGINAL_PUBLICATION_AVAILABLE"] = available
        row["publication_snapshot_id"] = found.get("snapshot_id")
        row["snapshot_count"] = int(found.get("snapshot_count") or 0)
        row["catalog_rows"] = int(found.get("catalog_rows") or 0)
        row["REMAINING_PROJECTION_READY"] = bool(row["CANDLE_INPUT_READY"] and available)
