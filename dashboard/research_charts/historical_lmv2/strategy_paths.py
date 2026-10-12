"""Ensure repo + context_study_v1 imports for frozen short baseline (read-only)."""

from __future__ import annotations

import sys
from pathlib import Path

import os

_HERE = Path(__file__).resolve()
_POBOB = Path(
    os.environ.get(
        "POOLS_OB_DELTA_BOT_ROOT",
        "/home/telgenbuescher/projects/pools+ob+delta_bot",
    )
)
if (_POBOB / "pool_pattern").is_dir():
    _REPO = _POBOB
else:
    _REPO = _HERE.parents[5]
_CONTEXT = _REPO / "results" / "pool_scan" / "context_study_v1"
_DASH = _REPO / "backtester" / "short"


def ensure_strategy_paths() -> Path:
    for p in (str(_REPO), str(_DASH), str(_CONTEXT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    from pool_pattern.market import ensure_paths

    ensure_paths()
    return _CONTEXT
