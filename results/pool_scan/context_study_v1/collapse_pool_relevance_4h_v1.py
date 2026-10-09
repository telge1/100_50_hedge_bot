"""Keep the older 4h pool when a later one only overlaps it.

Reads pool_relevance_4h_v1.csv. A newer pool is dropped while an older pool of
the same symbol and side is still open and their price boxes intersect.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
IN_ROWS = ROOT / "pool_relevance_4h_v1.csv"
OUT_ROWS = ROOT / "pool_relevance_4h_collapsed_v1.csv"
OUT_METHOD = ROOT / "pool_relevance_4h_collapsed_method_v1.json"


def overlaps(older: dict, newer: dict) -> bool:
    """True when the newer box sits inside the older one and adds no new edge."""
    older_bottom = float(older["bottom_price"])
    older_top = float(older["top_price"])
    newer_bottom = float(newer["bottom_price"])
    newer_top = float(newer["top_price"])
    inside = newer_bottom >= older_bottom and newer_top <= older_top
    return inside and newer_bottom < older_top and older_bottom < newer_top


def collapse(rows: list[dict]) -> list[dict]:
    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        grouped.setdefault((row["symbol"], row["side"]), []).append(row)
    kept: list[dict] = []
    for key in grouped:
        ordered = sorted(grouped[key], key=lambda row: (row["available_at"], row["pool_id"]))
        open_rows: list[dict] = []
        for row in ordered:
            born = datetime.fromisoformat(row["available_at"])
            open_rows = [old for old in open_rows if datetime.fromisoformat(old["held_until"]) > born]
            if any(overlaps(old, row) for old in open_rows):
                continue
            open_rows.append(row)
            kept.append(row)
    kept.sort(key=lambda row: (row["symbol"], row["side"], row["available_at"], row["pool_id"]))
    return kept


def main() -> None:
    for path in (OUT_ROWS, OUT_METHOD):
        if path.exists():
            raise SystemExit(f"refuse overwrite {path}")
    with IN_ROWS.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    kept = collapse(rows)
    with OUT_ROWS.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(kept)
    OUT_METHOD.write_text(json.dumps({
        "script": Path(__file__).name,
        "source": IN_ROWS.name,
        "rule": "Same symbol and side: drop a later pool while an older pool is still open and the newer box sits inside the older box. A step that extends past the older edge stays.",
        "input_rows": len(rows),
        "kept_rows": len(kept),
        "dropped_rows": len(rows) - len(kept),
    }, indent=2) + "\n")
    print("input", len(rows), "kept", len(kept), "dropped", len(rows) - len(kept))


if __name__ == "__main__":
    main()
