"""Crash-tail inventory for orphaned open segments."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import SCHEMA_VERSION


def inventory_orphan_tmp(path: Path) -> dict[str, Any]:
    stat = path.stat()
    result = {
        "schema_version": SCHEMA_VERSION,
        "completion_status": "PARTIAL",
        "reason": "CRASH_ORPHAN_TMP",
        "tmp_path": str(path),
        "size_bytes": stat.st_size,
        "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "inventoried_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "renamed_to_complete": False,
    }
    sidecar = Path(str(path) + ".partial.json")
    if not sidecar.exists():
        sidecar.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def scan_orphan_tmps(root: Path) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    return [inventory_orphan_tmp(path) for path in sorted(root.rglob("*.zst.tmp"))]
