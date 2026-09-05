"""Reporting / CSV writers for F0 outputs."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    # union keys
    keys: list[str] = []
    seen = set()
    for r in rows:
        for k in r.keys():
            if k not in seen:
                seen.add(k)
                keys.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def ensure_empty_outdir(path: Path) -> None:
    if path.exists():
        if any(path.iterdir()):
            raise FileExistsError(
                f"output dir {path} is not empty — refuse overwrite (pass a new path)"
            )
    else:
        path.mkdir(parents=True, exist_ok=True)
