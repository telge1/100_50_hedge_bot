"""Integrity helpers: JSON-safe, prefix parity."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from orderbook_analyse.aggressor_efficiency_flip.timeutil import parse_utc


def json_safe(obj: Any) -> Any:
    if isinstance(obj, float):
        if not math.isfinite(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [json_safe(v) for v in obj]
    return obj


def strip_trades(d: dict[str, Any]) -> dict[str, Any]:
    """Remove non-serializable trade objects from feature dicts."""
    out = {}
    for k, v in d.items():
        if k == "aggressor_trades":
            continue
        if isinstance(v, dict):
            out[k] = strip_trades(v)
        else:
            out[k] = v
    return out


def prefix_snapshot(feature_row: dict[str, Any], checkpoint_s: int) -> dict[str, Any]:
    return {
        "event_id": feature_row.get("event_id"),
        "compression_flag": feature_row.get("compression_flag"),
        "favorable_progress_bps": feature_row.get("favorable_progress_bps"),
        "trap_cp": (feature_row.get("trap_checkpoints") or {}).get(f"cp_{checkpoint_s}s"),
        "accept_cp": (feature_row.get("acceptance_checkpoints") or {}).get(f"cp_{checkpoint_s}s"),
        "decision_state": feature_row.get(f"decision_state_{checkpoint_s}s"),
    }
