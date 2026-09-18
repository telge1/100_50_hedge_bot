"""Unified OB Forschungsengine analysis facade (OB-Forschungsengine.md).

Composes existing proven modules into one analysis contract:

* MP zone / edge events → Silver L2 + public trades
* Wall-flow attribution → QDH_base / QDH_toxic (M_OI=M_LIQ=1)
* Optional MP HIT/PULL enrichment features
* Gated stubs for Footprint / OI / Liq / WallState / Signal V2

Does **not** invent Signal thresholds. Optional modules stay OFF until
out-of-sample proof (plan steps 10–15).
"""

from __future__ import annotations

from .contract import (
    ANALYSIS_CONTRACT_VERSION,
    AUDIT_ID,
    FeatureGates,
    PACKAGE_NAME,
    RUN_PREFIX,
    default_gates,
)

__all__ = [
    "PACKAGE_NAME",
    "AUDIT_ID",
    "ANALYSIS_CONTRACT_VERSION",
    "RUN_PREFIX",
    "FeatureGates",
    "default_gates",
]
