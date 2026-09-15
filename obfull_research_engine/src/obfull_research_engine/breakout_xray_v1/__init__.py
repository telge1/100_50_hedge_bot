"""Breakout X-Ray V1 — dual-mode read-only analysis (strategy-edge / manual-window).

Execution of live Bronze replay / full CH runs is held while the Silver full
builder lock is active. Offline unit and fixture tests remain allowed.
"""

from __future__ import annotations

FORMULA_ID = "dashboard_dual_tpo_vah_val_v1"
IMPLEMENTATION_SOURCE = "DIRECT_IMPORT_DASHBOARD_PURE_FUNCTIONS"
PACKAGE_VERSION = "breakout_xray_v1"
FULL_RUN_BLOCK_REASON = "FULL_RUN_BLOCKED_ACTIVE_SILVER_BUILDER"
EXPLICIT_EXECUTION_REQUIRED = "FULL_RUN_BLOCKED_EXPLICIT_EXECUTION_REQUIRED"

__all__ = [
    "FORMULA_ID",
    "IMPLEMENTATION_SOURCE",
    "PACKAGE_VERSION",
    "FULL_RUN_BLOCK_REASON",
    "EXPLICIT_EXECUTION_REQUIRED",
]
