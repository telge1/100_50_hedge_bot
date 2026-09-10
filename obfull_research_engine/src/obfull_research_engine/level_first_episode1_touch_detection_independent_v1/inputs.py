"""Load zone (pre-T0 research object) and frozen raw trade/candle inputs."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..bounded_level_first_analyzer_pilot_v1.episodes import parse_utc
from ..paths import ENGINE_ROOT
from . import LEVEL_ID, LEVEL_CLUSTER_ID, PERSISTENT_CLUSTER_ID

LF1_MP_EVENTS = (
    ENGINE_ROOT
    / "results/bounded_level_first_analyzer_pilot_v1/BTCUSDT/lf1_69e21d12d280596e/mp_level_events.csv"
)
INPUT_FREEZE_DIR = (
    ENGINE_ROOT
    / "results/level_first_episode1_corrected_sms1_persist_v1/BTCUSDT"
    / "episode1_independent_derivation_inputs_v1"
)


def load_zone_from_mp_events(path: Path = LF1_MP_EVENTS, *, level_id: str = LEVEL_ID) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("level_id") != level_id:
                continue
            available_at = parse_utc(row["available_at"])
            return {
                "event_type": "ZONE_AVAILABLE",
                "zone_id": PERSISTENT_CLUSTER_ID,
                "level_cluster_id": LEVEL_CLUSTER_ID,
                "persistent_cluster_id": PERSISTENT_CLUSTER_ID,
                "level_id": row["level_id"],
                "level_type": row.get("tpo_type"),
                "zone_low": float(row["level_zone_low"]),
                "zone_high": float(row["level_zone_high"]),
                "zone_available_at": available_at.strftime("%Y-%m-%dT%H:%M:%SZ")
                if available_at.microsecond == 0
                else available_at.isoformat().replace("+00:00", "Z"),
                "zone_available_at_dt": available_at,
                "source_file": str(path),
            }
    raise RuntimeError(f"level_id not found: {level_id}")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def load_trades(path: Path | None = None) -> list[dict[str, Any]]:
    return load_jsonl(path or (INPUT_FREEZE_DIR / "public_trades_zone_window.jsonl"))


def load_candles(path: Path | None = None) -> list[dict[str, Any]]:
    return load_jsonl(path or (INPUT_FREEZE_DIR / "candles_1m_zone_window.jsonl"))


def assert_no_forbidden_calc_inputs(cfg: dict[str, Any] | None, *, forbidden: tuple[str, ...]) -> list[str]:
    cfg = dict(cfg or {})
    stripped = []
    for key in forbidden:
        if key in cfg:
            stripped.append(key)
            cfg.pop(key, None)
    return stripped
