"""AGGRESSOR_EFFICIENCY_TRAPPED_VWAP_ACCEPTANCE_V1 — research stage 1.

Reuses aggressor_efficiency_flip dual-impact / VWAP / trade loading.
Adds trade-level trapped VWAP, pool-edge acceptance/reclaim, combined states.
Not a live signal. Not a second efficiency engine.
"""

from __future__ import annotations

PACKAGE_ID = "AGGRESSOR_EFFICIENCY_TRAPPED_VWAP_ACCEPTANCE_V1"
FEATURE_VERSION = "aef_trap_accept_features/v1"
CAUSAL_CONTRACT_VERSION = "aef_trap_accept_contract/v1"
