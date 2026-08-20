"""Atomic JSON state for wall-transition collector."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import SCHEMA_VERSION


def atomic_write_json(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"corrupt state file {path}: {exc}") from exc
    if not isinstance(obj, dict):
        raise RuntimeError(f"corrupt state file {path}: not an object")
    return obj


def default_state(symbol: str) -> dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "schema_version": SCHEMA_VERSION,
        "symbol": symbol,
        "last_processed_ts": None,
        "last_processed_update_id": None,
        "last_written_transition_ts": None,
        "known_transition_ids_hash": None,
        "last_success_utc": None,
        "last_error": None,
        "restart_count": 0,
        "transitions_written_total": 0,
    }


def touch_success(state: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    out = dict(state)
    out.update(kwargs)
    out["last_success_utc"] = datetime.now(timezone.utc).isoformat()
    out["last_error"] = None
    return out
