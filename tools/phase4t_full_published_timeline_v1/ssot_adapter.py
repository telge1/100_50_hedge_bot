"""Re-export Phase 4S SSOT adapter (read-only)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "phase4s_timeline_recovery_v1" / "ssot_adapter.py"
_spec = importlib.util.spec_from_file_location("phase4s_ssot_adapter", _PATH)
_mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_mod)

apply_ssot_pythonpath = _mod.apply_ssot_pythonpath
build_landscape_window = _mod.build_landscape_window
published_inventory = _mod.published_inventory
MAX_EXPORT_HOURS = _mod.MAX_EXPORT_HOURS

apply_ssot_pythonpath()
