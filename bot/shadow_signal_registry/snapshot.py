"""Atomic snapshot writers."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from bot.shadow_signal_registry.models import LONG_SNAPSHOT_FIELDS, SHORT_SNAPSHOT_FIELDS, Side


def write_snapshot(
    side: Side,
    rows: list[dict[str, Any]],
    csv_path: Path,
    json_path: Path,
) -> None:
    fields = LONG_SNAPSHOT_FIELDS if side == "long" else SHORT_SNAPSHOT_FIELDS
    sorted_rows = sorted(
        rows,
        key=lambda r: (r.get("decision_time") or "", r.get("signal_id") or ""),
        reverse=True,
    )
    for p, writer in ((csv_path, "csv"), (json_path, "json")):
        tmp = p.with_suffix(p.suffix + ".tmp")
        if writer == "csv":
            with tmp.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                w.writeheader()
                for row in sorted_rows:
                    w.writerow({k: row.get(k) for k in fields})
        else:
            tmp.write_text(json.dumps(sorted_rows, indent=2, default=str), encoding="utf-8")
        tmp.replace(p)
