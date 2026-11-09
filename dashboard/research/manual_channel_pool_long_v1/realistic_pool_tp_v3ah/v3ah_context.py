"""V3AH — frozen V3AB snapshot (unchanged SHA)."""

from __future__ import annotations

import sys
from pathlib import Path

V3AH = Path(__file__).resolve().parent
PROBE = V3AH.parent
V3AB = PROBE / "support_break_exit_v3ab"

for p in (V3AH, PROBE, V3AB):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from config import L1_V3E_VARIANT_A, L1_V3E_VARIANT_B, L1_V3E_VARIANT_C  # noqa: E402
from v3ab_context import build_context_from_snapshot  # noqa: E402

VARIANTS = {"A": L1_V3E_VARIANT_A, "B": L1_V3E_VARIANT_B, "C": L1_V3E_VARIANT_C}

FROZEN_SHA256 = "b0718d82a94e8ad12d27d570c0cd7ce97cf8088a2db21c4c92830cd80fc600cb"
DEFAULT_SNAPSHOT = V3AB / "l1_v3ab_drawings_snapshot_b0718d82a94e8ad1.json"

__all__ = ["VARIANTS", "FROZEN_SHA256", "DEFAULT_SNAPSHOT", "build_context_from_snapshot"]
