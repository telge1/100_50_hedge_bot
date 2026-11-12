"""Poll drawings.json mtime + content hash (post-`persist_drawings`)."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class DrawingFileObservation:
    path: Path
    mtime: float
    content_hash: str
    payload: dict[str, Any]
    observed_at: float


def hash_payload(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def observe_drawings(path: Path) -> DrawingFileObservation | None:
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    payload = json.loads(text)
    return DrawingFileObservation(
        path=path,
        mtime=path.stat().st_mtime,
        content_hash=hash_payload(payload),
        payload=payload,
        observed_at=time.time(),
    )


class DrawingChangeDetector:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._last_hash: str | None = None
        self._last_mtime: float | None = None

    def poll(self) -> tuple[bool, DrawingFileObservation | None]:
        obs = observe_drawings(self.path)
        if obs is None:
            return False, None
        changed = obs.content_hash != self._last_hash
        if changed:
            self._last_hash = obs.content_hash
            self._last_mtime = obs.mtime
        return changed, obs
