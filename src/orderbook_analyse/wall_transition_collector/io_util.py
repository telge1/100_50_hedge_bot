"""Append-only transition CSV with dedupe keys."""

from __future__ import annotations

import csv
import hashlib
from pathlib import Path
from typing import Any, Iterable

FIELDS = [
    "wall_sequence_id",
    "transition_ts",
    "side",
    "transition_type",
    "price",
    "qty",
    "details",
]


def transition_key(row: dict[str, Any]) -> str:
    return "|".join(
        [
            str(row.get("wall_sequence_id") or ""),
            str(row.get("transition_ts") or ""),
            str(row.get("side") or ""),
            str(row.get("transition_type") or ""),
            str(row.get("price") or ""),
        ]
    )


def load_existing_keys(path: Path, *, max_keys: int = 2_000_000) -> set[str]:
    keys: set[str] = set()
    if not path.exists() or path.stat().st_size == 0:
        return keys
    with path.open(encoding="utf-8") as fh:
        for i, row in enumerate(csv.DictReader(fh)):
            keys.add(transition_key(row))
            if i + 1 >= max_keys:
                break
    return keys


def keys_hash(keys: Iterable[str]) -> str:
    h = hashlib.sha256()
    for k in sorted(keys):
        h.update(k.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def append_transitions(
    path: Path,
    rows: list[dict[str, Any]],
    *,
    existing_keys: set[str] | None = None,
) -> tuple[int, set[str]]:
    """Append new rows only. Returns (n_written, updated_keys)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    keys = existing_keys if existing_keys is not None else load_existing_keys(path)
    new_rows = []
    for r in rows:
        k = transition_key(r)
        if k in keys:
            continue
        keys.add(k)
        new_rows.append({f: r.get(f) for f in FIELDS})
    if not new_rows:
        return 0, keys
    write_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        if write_header:
            w.writeheader()
        for r in new_rows:
            w.writerow(r)
    return len(new_rows), keys
