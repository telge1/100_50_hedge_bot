"""Short-lived prepare tokens binding drawing revision to confirm dialog."""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import Any


class PrepareTokenStore:
    def __init__(self, path: Path, *, ttl_sec: float = 120.0) -> None:
        self.path = path
        self.ttl_sec = ttl_sec

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def purge(self) -> None:
        now = time.time()
        data = self._read()
        keep = {k: v for k, v in data.items() if isinstance(v, dict) and now - float(v.get("ts", 0)) < self.ttl_sec}
        self._write(keep)

    def issue(self, payload: dict[str, Any]) -> str:
        self.purge()
        token = secrets.token_urlsafe(24)
        data = self._read()
        data[token] = {**payload, "ts": time.time()}
        self._write(data)
        return token

    def consume(self, token: str) -> dict[str, Any] | None:
        self.purge()
        data = self._read()
        row = data.pop(token, None)
        self._write(data)
        if not isinstance(row, dict):
            return None
        if time.time() - float(row.get("ts", 0)) > self.ttl_sec:
            return None
        return row
