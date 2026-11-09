"""EMA59 BT-P5 July pilot trades → TRP long_position drawings (read-only)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .bt_d12_latest_backtester import resolve_pools_ob_repo

SOURCE = "ema59_bt_p5_pilot_overlay_v1"
STRATEGY_ID = "ema59_long_rebound_pilot_v1"
PREFIX = "ema59p5-"
OVERLAY_REL = Path("results/phase_ema59_bt_p5_pilot_v1/overlay_variant_a.json")


def overlay_path() -> Path:
    return resolve_pools_ob_repo() / OVERLAY_REL


def load_overlay_bundle() -> dict[str, Any] | None:
    path = overlay_path()
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)


def build_drawing_specs(bundle: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    positions: list[dict[str, Any]] = []
    for spec in bundle.get("position_specs") or []:
        row = dict(spec)
        row["start"] = _parse_dt(row.get("start"))
        row["end"] = _parse_dt(row.get("end"))
        positions.append(row)
    markers = list(bundle.get("marker_specs") or [])
    for pos in positions:
        pos.setdefault("metadata", {})
        pos["metadata"]["origin"] = SOURCE
        pos["metadata"]["strategy_id"] = STRATEGY_ID
    return positions, [], markers
