"""Hot-reload helper for long-lived dashboard (1h on-demand deploy)."""

from __future__ import annotations

import importlib

from pd1_landscape_viewer_v1 import module_hot_reload_v1 as mhr
from pd1_landscape_viewer_v1.historical_window_v1 import landscape_capabilities


def test_capabilities_includes_1h_on_demand_scope():
    mhr._LAST_MTIMES.clear()
    mhr.ensure_pd1_landscape_modules_current()
    caps = landscape_capabilities()
    scopes = [s for s in caps.get("supported_viewer_scopes", []) if s.get("timeframe") == "1h"]
    assert len(scopes) == 1
    assert scopes[0]["pipeline_line_id"] == "on_demand_ch1m_1hpd1_v1"
    assert scopes[0]["membership_slot_minutes"] == 60


def test_reload_is_idempotent():
    mhr._LAST_MTIMES.clear()
    mhr.ensure_pd1_landscape_modules_current()
    before = dict(mhr._LAST_MTIMES)
    mhr.ensure_pd1_landscape_modules_current()
    assert mhr._LAST_MTIMES == before
    importlib.reload(mhr)
