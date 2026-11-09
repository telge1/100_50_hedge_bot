"""EMA59 BT-P12 6M July WATCH backtest → TRP long_position (Aktuelle Backtests)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .bt_d12_latest_backtester import resolve_pools_ob_repo

SOURCE = "ema59_bt_p12_6m_current_backtest_v1"
BACKTEST_ID = "ema59_6m_juli_watch_p12"
DISPLAY_NAME = "EMA59 6M LONG — Juli WATCH P12"
PREFIX = "ema59p12-"
OVERLAY_REL = Path("results/phase_ema59_bt_p12_6m_long_watch_v1/overlay_6m_july_watch_p12.json")


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
        meta = dict(pos.get("metadata") or {})
        meta["origin"] = SOURCE
        meta["backtest_id"] = BACKTEST_ID
        meta.pop("strategy_id", None)
        pos["metadata"] = meta
    return positions, [], markers
