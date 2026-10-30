"""Canonical lower atomic pool keys — shared by streak cache and qualification lookup."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
D7 = ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1"
if str(D7) not in sys.path:
    sys.path.insert(0, str(D7))

from signal_types_v1 import pool_key  # noqa: E402


def canonical_lower_atomic_pool_key(pool_lo: float, pool_hi: float) -> str:
    """Exact atomic band identity (matches D7 short pool_key / float band matching)."""
    return pool_key(pool_lo, pool_hi)
