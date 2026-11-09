"""RAM ↔ drawings.json health (research charts)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from drawings.manager import DrawingManager
from drawings.models import Drawing
from drawings.persistence import load_drawings, serialize_drawing
from overlays.serialization import to_unix_seconds


def _canonical_drawings_blob(drawings: list[Drawing]) -> str:
    ordered = sorted(drawings, key=lambda d: d.drawing_id)
    payload = {
        "version": 1,
        "drawings": [serialize_drawing(d) for d in ordered],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _compare_fingerprint(d: Drawing) -> dict[str, Any]:
    meta = dict(d.metadata or {})
    return {
        "drawing_id": d.drawing_id,
        "drawing_type": d.drawing_type,
        "symbol": d.symbol,
        "visible": bool(d.visible),
        "locked": bool(d.locked),
        "extend_right": bool(meta.get("extend_right")),
        "start_timestamp": to_unix_seconds(d.start_timestamp),
        "end_timestamp": to_unix_seconds(d.end_timestamp),
        "start_price": d.start_price,
        "end_price": d.end_price,
        "price": d.price,
        "timestamp": to_unix_seconds(d.timestamp),
        "style_color": d.style.color if d.style else None,
        "style_width": float(d.style.width) if d.style else None,
    }


def _list_ram_drawings(manager: DrawingManager) -> list[Drawing]:
    return list(manager.get_drawings(include_hidden=True))


def compute_drawing_health(manager: DrawingManager, path: Path) -> dict[str, Any]:
    """Compare live DrawingManager (RAM) with on-disk drawings.json."""
    ram_items = _list_ram_drawings(manager)
    file_items = load_drawings(path) if path.is_file() else []

    ram_ids = sorted(d.drawing_id for d in ram_items)
    file_ids = sorted(d.drawing_id for d in file_items)

    ram_by_id = {d.drawing_id: d for d in ram_items}
    file_by_id = {d.drawing_id: d for d in file_items}

    ram_only = sorted(set(ram_ids) - set(file_ids))
    file_only = sorted(set(file_ids) - set(ram_ids))

    content_diff: list[dict[str, Any]] = []
    for did in sorted(set(ram_ids) & set(file_ids)):
        rf = _compare_fingerprint(ram_by_id[did])
        ff = _compare_fingerprint(file_by_id[did])
        if rf != ff:
            content_diff.append({"drawing_id": did, "ram": rf, "file": ff})

    ram_blob = _canonical_drawings_blob(ram_items)
    file_blob = _canonical_drawings_blob(file_items)

    drift = bool(ram_only or file_only or content_diff or _sha256(ram_blob) != _sha256(file_blob))

    return {
        "drawings_path": str(path),
        "ram_count": len(ram_items),
        "file_count": len(file_items),
        "ram_drawing_ids": ram_ids,
        "file_drawing_ids": file_ids,
        "ram_sha256": _sha256(ram_blob),
        "file_sha256": _sha256(file_blob),
        "ids_only_in_ram": ram_only,
        "ids_only_in_file": file_only,
        "ids_with_content_diff": content_diff,
        "drift": drift,
    }
