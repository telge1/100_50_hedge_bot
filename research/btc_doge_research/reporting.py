"""Small deterministic report writers; no raw event dumps."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterable, Sequence

from .contracts import sanitize_json


def ensure_output(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "evidence").mkdir(exist_ok=True)


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(
            sanitize_json(value),
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def write_text(path: Path, value: str) -> None:
    path.write_text(
        value.rstrip() + "\n", encoding="utf-8", newline="\n"
    )


def write_csv(
    path: Path, columns: Sequence[str], records: Iterable[Sequence[Any]]
) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(columns)
        writer.writerows(
            [sanitize_json(value) for value in record] for record in records
        )
