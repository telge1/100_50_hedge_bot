"""sys.path bootstrap so dry-run can reuse backtester/short modules."""

from __future__ import annotations

import sys
from pathlib import Path

_WORKSPACE = Path(__file__).resolve().parents[2]
_SHORT = _WORKSPACE / "backtester" / "short"

_EXTRA = [
    "/home/telgenbuescher/projects/Signal_Generator_Ralf/signal_generator_stoch_waves/src",
    "/home/telgenbuescher/projects/orderbook_analyse/src",
    str(_SHORT),
    str(_SHORT / "dashboard"),
    str(_WORKSPACE),
]


def ensure_import_paths() -> Path:
    """Insert research/data paths once; return short package root."""
    for path in reversed(_EXTRA):
        if path not in sys.path:
            sys.path.insert(0, path)
    return _SHORT
