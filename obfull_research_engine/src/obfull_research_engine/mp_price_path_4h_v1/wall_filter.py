"""Frozen WALL_PERSISTENCE application (thresholds unchanged)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_frozen_wall_filter(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def event_passes_wall_persistence(
    feature_row: dict[str, Any],
    frozen: dict[str, Any],
) -> bool:
    thr = frozen["thresholds"]
    directions = frozen["directions"]
    for name, threshold in thr.items():
        raw = feature_row.get(name)
        if raw is None or raw == "" or raw == "None":
            return False
        if feature_row.get(f"{name}__available") is False:
            return False
        try:
            xv = float(raw)
        except (TypeError, ValueError):
            return False
        direction = int(directions[name])
        if direction > 0 and xv < float(threshold):
            return False
        if direction < 0 and xv > float(threshold):
            return False
    return True
