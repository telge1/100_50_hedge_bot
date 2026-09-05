"""Liquidity Pool Case Sequence Freeze V1 — outcome-blind sequence lock."""

SCHEMA_VERSION = "frozen_case_sequence_v1"
FREEZE_SCOPE = "SEQUENCE_AND_REFERENCE_SEMANTICS_ONLY"
SOURCE_REL = (
    "results/liquidity_pool_six_case_wall_trade_reaction_sample_v1/selection_manifest.json"
)
REFERENCE_SOURCE_FIELD = "cluster_start_ts"
FORBIDDEN_FIELD_SUBSTR = (
    "outcome",
    "verdict",
    "pnl",
    "mfe",
    "mae",
    "return",
    "evidence",
    "accepted",
    "rejected",
    "winning",
    "losing",
    "no_trade",
    "trade_no",
    "reaction_class",
)

# Comparable deep-audit roots (not the shared six-case short sample alone).
CASE_02_DEEP_AUDITS = (
    "results/case_02_pool_edge_aggressor_efficiency_timeline_v1",
    "results/case_02_control_shift_timestamp_review_v1",
    "results/post_case_02_next_pool_causal_reaction_audit_v1",
)
CASE_06_DEEP_AUDITS = (
    "results/ask_pool_022736_wall_public_trade_reaction_audit_v1",
)
# Shared short sample documents early exposure for CASE_01 (and all six briefly).
SIX_CASE_SAMPLE = "results/liquidity_pool_six_case_wall_trade_reaction_sample_v1"
