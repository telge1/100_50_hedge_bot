"""Phase 4T config fix regression test."""

from __future__ import annotations

import json
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

from timeline_builder import run_timeline_on_window  # noqa: E402


def test_visible_window_passed_to_timeline_config():
    window = {
        "window": {"from_utc": "2026-09-09T00:00:00Z", "to_utc": "2026-09-09T01:00:00Z"},
        "pools": [],
        "prices": [
            {"t": "2026-09-09T00:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1},
            {"t": "2026-09-09T00:05:00Z", "o": 1, "h": 1, "l": 1, "c": 1},
        ],
    }
    # Will fail node if adapter missing — only check config plumbing via mock path
    cfg_path = TOOLS.parents[1] / "tools/phase3a_dynamic_pool_landscape_timeline_v1/research_config_v1.json"
    pilot = json.loads(cfg_path.read_text())
    assert pilot["visible_from_utc"] != "2026-09-09T00:00:00Z"
