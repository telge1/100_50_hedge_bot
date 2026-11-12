"""Persistent drawing_id ↔ exchange order mapping."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class BoxSyncState:
    drawing_id: str
    symbol: str
    exchange_order_id: str
    order_link_id: str
    entry_price: float
    stop_price: float
    target_price: float
    order_status: str
    content_hash: str
    last_action: str
    updated_at: float = field(default_factory=time.time)
    desired_sl: float | None = None
    desired_tp: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._boxes: dict[str, BoxSyncState] = {}
        self.load()

    def load(self) -> None:
        if not self.path.is_file():
            self._boxes = {}
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        rows = raw.get("boxes") or {}
        self._boxes = {}
        for did, row in rows.items():
            if isinstance(row, dict):
                self._boxes[str(did)] = BoxSyncState(**row)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"boxes": {k: v.to_dict() for k, v in self._boxes.items()}}
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def get(self, drawing_id: str) -> BoxSyncState | None:
        return self._boxes.get(drawing_id)

    def all_boxes(self) -> list[BoxSyncState]:
        return list(self._boxes.values())

    def put(self, state: BoxSyncState) -> None:
        state.updated_at = time.time()
        self._boxes[state.drawing_id] = state
        self.save()


class EventLog:
    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {**record, "ts": time.time()}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
