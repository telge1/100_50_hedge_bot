"""Load canonical density snapshots from BT-D54 forensic artifacts (read-only)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DENSITY_NOTE_D54 = (
    "BT-D54 run_forensics canonical_provider snapshots "
    "(ResearchDensityRunV1 / historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1)"
)


def load_d54_window_snapshots(json_path: Path) -> dict[str, dict[str, Any]]:
    doc = json.loads(json_path.read_text(encoding="utf-8"))
    out: dict[str, dict[str, Any]] = {}
    for item in doc.get("snapshots") or []:
        ba = item.get("bar_analysis") or {}
        bar = ba.get("decision_close_utc")
        if not bar:
            continue
        uppers = []
        for u in item.get("upper_bands_above_close") or []:
            uppers.append(
                {
                    "lower_price": str(u["pool_lower"]),
                    "upper_price": str(u["pool_upper"]),
                    "origin_ids": u.get("origin_ids") or "",
                    "unique_origin_count": int(u.get("origin_count") or 2),
                    "side": "upper",
                }
            )
        lowers = []
        for lo in item.get("lower_bands") or []:
            lowers.append(
                {
                    "lower_price": str(lo["pool_lower"]),
                    "upper_price": str(lo["pool_upper"]),
                    "side": "lower",
                }
            )
        out[bar] = {
            "decision_close_utc": bar,
            "upper_bands": uppers,
            "lower_bands": lowers,
            "pipeline_line_id": DENSITY_NOTE_D54,
            "data_provenance": "phase_bt_d54_perfect_downtrend_short_entry_v1",
        }
    return out
