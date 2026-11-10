"""Reload PD1 landscape modules when source files change (long-lived dashboard process)."""

from __future__ import annotations

import importlib
import os
from types import ModuleType

_LAST_MTIMES: dict[str, float] = {}

# Order: dependents after dependencies.
_LANDSCAPE_MODULE_NAMES: tuple[str, ...] = (
    "pd1_landscape_viewer_v1.on_demand_1hpd1_v1",
    "pd1_landscape_viewer_v1.historical_window_v1",
    "pd1_landscape_viewer_v1.window_service_v1",
)


def _module_mtime(mod: ModuleType) -> float:
    path = getattr(mod, "__file__", None)
    if not path:
        return 0.0
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def ensure_pd1_landscape_modules_current() -> None:
    """Import or reload landscape builder modules if their .py files were updated."""
    for name in _LANDSCAPE_MODULE_NAMES:
        mod = importlib.import_module(name)
        mtime = _module_mtime(mod)
        prev = _LAST_MTIMES.get(name)
        if prev is None or (mtime > 0 and mtime != prev):
            mod = importlib.reload(mod)
            _LAST_MTIMES[name] = _module_mtime(mod)
