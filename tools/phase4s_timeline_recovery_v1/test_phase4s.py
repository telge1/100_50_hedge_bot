"""Phase 4S adapter tests."""

from __future__ import annotations

import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

from ssot_adapter import apply_ssot_pythonpath, published_inventory  # noqa: E402


def test_adapter_loads_viewer_scope():
    apply_ssot_pythonpath()
    from research_charts.historical_lmv2_pd_contract_v1 import ALLOWED_VIEWER_SCOPES  # noqa: E402

    assert ("5m", 1) in ALLOWED_VIEWER_SCOPES


def test_published_inventory_nonempty():
    inv = published_inventory()
    assert int(inv["published_decision_closes"]) > 0
