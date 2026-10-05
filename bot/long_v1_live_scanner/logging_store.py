"""JSONL signal log (long scanner paths only)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class JsonlSignalLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, row: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")

    def write_wave_summary(self, summary: dict[str, Any]) -> None:
        row = dict(summary)
        row.setdefault("event", "wave_summary")
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
