"""Import paths for frozen find_short_entry_15m_v1 only (never context_study drift)."""

from __future__ import annotations

import hashlib
import importlib
import sys
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[4]
_DASH = _REPO / "backtester" / "short"
_FROZEN_CODE = (
    _REPO / "frozen_strategies" / "e1r_cluster3_ema200_v1" / "code"
)
_FROZEN_ENTRY = _FROZEN_CODE / "find_short_entry_15m_v1.py"
_EXPECTED_SHA256 = (
    "62a3b454eab8905a619eb38ee06c39eebcc0aecc01c7457798149ab44b299421"
)
_FREEZE_TAG = "e1r_cluster3_ema200_shadow_freeze_v1"
_FREEZE_COMMIT = "adf7dac448e5fcb4202c22a0e4955ee4d43b06a4"


def frozen_entry_sha256() -> str:
    data = _FROZEN_ENTRY.read_bytes()
    return hashlib.sha256(data).hexdigest()


def verify_frozen_entry_hash() -> dict[str, Any]:
    actual = frozen_entry_sha256()
    ok = actual == _EXPECTED_SHA256
    return {
        "path": str(_FROZEN_ENTRY),
        "expected_sha256": _EXPECTED_SHA256,
        "actual_sha256": actual,
        "match": ok,
        "freeze_tag": _FREEZE_TAG,
        "freeze_commit": _FREEZE_COMMIT,
    }


def ensure_frozen_short_paths() -> Path:
    """Prepend frozen code dir so ``import find_short_entry_15m_v1`` loads freeze copy."""
    verify = verify_frozen_entry_hash()
    if not verify["match"]:
        raise RuntimeError(f"FROZEN_HASH_MISMATCH: {verify}")

    for p in (str(_REPO), str(_DASH), str(_FROZEN_CODE)):
        if p not in sys.path:
            sys.path.insert(0, p)

    # Drop any already-imported drifted module from context_study.
    if "find_short_entry_15m_v1" in sys.modules:
        mod_file = getattr(sys.modules["find_short_entry_15m_v1"], "__file__", "") or ""
        if str(_FROZEN_CODE) not in mod_file:
            del sys.modules["find_short_entry_15m_v1"]

    from pool_pattern.market import ensure_paths

    ensure_paths()
    return _FROZEN_CODE


def load_frozen_entry_module():
    ensure_frozen_short_paths()
    return importlib.import_module("find_short_entry_15m_v1")
