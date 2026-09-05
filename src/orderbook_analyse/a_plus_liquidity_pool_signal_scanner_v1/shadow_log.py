"""Append-only shadow event log."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ShadowEventLog:
    def __init__(self, out_dir: Path) -> None:
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._paths = {
            "candidates": self.out_dir / "candidates.jsonl",
            "confirmed_signals": self.out_dir / "confirmed_signals.jsonl",
            "invalidated_candidates": self.out_dir / "invalidated_candidates.jsonl",
            "marker_payloads": self.out_dir / "marker_payloads.jsonl",
            "gate_audit": self.out_dir / "gate_audit.jsonl",
            "data_quality": self.out_dir / "data_quality.jsonl",
        }
        for p in self._paths.values():
            if p.exists():
                raise FileExistsError(f"refusing to overwrite existing log: {p}")

    def append(self, kind: str, row: dict[str, Any]) -> None:
        path = self._paths[kind]
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")

    def write_manifest(self, manifest: dict[str, Any]) -> None:
        path = self.out_dir / "manifest.json"
        if path.exists():
            raise FileExistsError(path)
        path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
