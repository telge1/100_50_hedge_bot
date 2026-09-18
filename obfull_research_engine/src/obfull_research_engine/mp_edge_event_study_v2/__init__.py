"""MP edge event study V2 — directional approach, delayed labels, trade_side outcomes."""

from __future__ import annotations

PACKAGE_ID = "mp_edge_event_study_v2"
CONTRACT_VERSION = "2.0.0"
V1_OUTCOME_INVALIDATION = (
    "V1 TP/SL/MFE/MAE for TRUE_BREAK used fade_side instead of break/trade_side; "
    "all V1 TRUE_BREAK outcome metrics are FACHLICH_UNGUELTIG."
)

__all__ = ["PACKAGE_ID", "CONTRACT_VERSION", "V1_OUTCOME_INVALIDATION"]
