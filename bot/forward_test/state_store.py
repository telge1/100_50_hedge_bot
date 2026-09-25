"""Persist already-emitted event / signal keys so dry-run does not spam."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class SeenStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._keys: set[str] = set()
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        keys = raw.get("keys") if isinstance(raw, dict) else None
        if isinstance(keys, list):
            self._keys = {str(k) for k in keys}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {"keys": sorted(self._keys)}
        self.path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def has(self, key: str) -> bool:
        return key in self._keys

    def add(self, key: str) -> bool:
        """Return True if newly added."""
        if key in self._keys:
            return False
        self._keys.add(key)
        return True
