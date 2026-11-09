"""BT-P11 July SL diagnose (archived) — Legacy / Archiv only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .bt_d12_latest_backtester import resolve_pools_ob_repo

SOURCE = "ema59_bt_p11_6m_legacy_archive_v1"
STRATEGY_ID = "ema59_bt_p11_6m_legacy_archive_v1"
BACKTEST_ID = "ema59_6m_juli_sl_diagnose"
DISPLAY_NAME = "EMA59 6M LONG — Juli SL-Diagnose (Archiv P11)"
PREFIX = "ema59p11-legacy-"
OVERLAY_REL = Path(
    "results/phase_ema59_bt_p11_6m_long_sl_visual_v1/archive_pre_p12/overlay_6m_july_sl_diagnose.json"
)


def overlay_path() -> Path:
    return resolve_pools_ob_repo() / OVERLAY_REL


def load_overlay_bundle() -> dict[str, Any] | None:
    path = overlay_path()
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
