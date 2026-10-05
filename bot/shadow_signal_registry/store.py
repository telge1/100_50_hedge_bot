"""Append-only event store and paths per side."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

Side = Literal["long", "short"]

RUNTIME = Path(__file__).resolve().parent / "runtime"


class RegistryStore:
    def __init__(self, side: Side) -> None:
        if side not in ("long", "short"):
            raise ValueError(side)
        self.side = side
        RUNTIME.mkdir(parents=True, exist_ok=True)
        self.events_path = RUNTIME / f"{side}_signals.jsonl"
        self.snapshot_csv = RUNTIME / f"{side}_snapshot.csv"
        self.snapshot_json = RUNTIME / f"{side}_snapshot.json"
        self.summary_json = RUNTIME / f"{side}_summary.json"
        self.state_path = RUNTIME / f"{side}_registry_state.json"

    def append_event(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, default=str)
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def load_events(self) -> list[dict[str, Any]]:
        if not self.events_path.is_file():
            return []
        out: list[dict[str, Any]] = []
        for line in self.events_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out

    def load_state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return {"signal_ids": [], "snapshots": {}}
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def save_state(self, state: dict[str, Any]) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)
