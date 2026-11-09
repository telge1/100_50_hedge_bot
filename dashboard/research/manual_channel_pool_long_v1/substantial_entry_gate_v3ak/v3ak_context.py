"""V3AK context — frozen V3AB snapshot, variant B only for runs."""

from __future__ import annotations

import sys
from pathlib import Path

V3AK = Path(__file__).resolve().parent
PROBE = V3AK.parent
V3AB = PROBE / "support_break_exit_v3ab"
V3AH = PROBE / "realistic_pool_tp_v3ah"
V3W = PROBE / "tp_research_v3w"

for p in (V3AK, V3AH, V3W, V3AB, PROBE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from config import L1_V3E_VARIANT_B  # noqa: E402
from v3ab_context import build_context_from_snapshot  # noqa: E402

VARIANT_B = L1_V3E_VARIANT_B
FROZEN_SHA256 = "b0718d82a94e8ad12d27d570c0cd7ce97cf8088a2db21c4c92830cd80fc600cb"
DEFAULT_SNAPSHOT = V3AB / "l1_v3ab_drawings_snapshot_b0718d82a94e8ad1.json"
TP_SCENARIO = "CAP6_D20_BLOCK"

__all__ = [
    "VARIANT_B",
    "FROZEN_SHA256",
    "DEFAULT_SNAPSHOT",
    "TP_SCENARIO",
    "build_context_from_snapshot",
]
