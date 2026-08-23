"""Canonical frozen EDC strategy semantics (XRP-successful run as source of truth).

This module documents the frozen rules. Runtime lives in sibling modules.
Do not change rules based on multi-coin PnL.
"""

from __future__ import annotations

CANONICAL_STRATEGY_ID = "EDC_FROZEN_XRP_REFERENCE_V1"

# Candidate / entry (from xrp_30d_core_sources_comparison / evaluate_candidates_core_30d)
ENTRY_RULE = "SIGNAL_TF_NEXT_OPEN_AFTER_SIGNAL_BAR"
ENTRY_RULE_DESCRIPTION = (
    "After the completed signal bar (decision_at = bar close), enter at the open of the "
    "next signal-timeframe bar (mfe_runner._next_open / shared next_signal_tf_open). "
    "For contiguous 5m sessions this open_time equals decision_at."
)

# Outcome (from run_edc_xrp_horizon_tp_sl_matrix)
REQUIRE_FULL_HORIZON = False
INCOMPLETE_OUTCOME_REASON = "INCOMPLETE_OUTCOME_HORIZON"
INCOMPLETE_POLICY = (
    "If the 1m path ends before entry_at+horizon, classify as INCOMPLETE_OUTCOME_HORIZON "
    "with include_in_primary_pnl=False. Never treat truncated data as TP/SL/TIME win/loss."
)

# Market load pads (core_sources: warm 5d; horizon matrix outcome pad to +12h)
WARMUP_PAD_DAYS = 5
OUTCOME_PAD_HOURS = 12
SOURCE_PAD_HOURS = 2

# Reference cell
REF_TIMEFRAME = "5m"
REF_MODE = "M0_STRICT_SYNC"
REF_GROUP = "CORE_RESEARCH_SUPPORTIVE"
REF_TP_PCT = 0.75
REF_SL_PCT = 0.50
REF_HORIZON = "8h"
REF_HORIZON_MIN = 480
REF_COST_PCT = 0.15
REF_NOTIONAL = 1000.0
SAME_BAR_RULE = "SL_FIRST"

# Multicoin detection scope (parity gate must use the same scope)
MULTICOIN_DETECTION_SCOPES = (
    ("5m", "M0_STRICT_SYNC"),
    ("5m", "M5_COMPRESSED_REBOUND"),
    ("15m", "M4_TOUCH_05_EXP_1"),
)

DIFF_RESOLUTIONS = {
    "candidate_pipeline": (
        "Canonical = shared re-detect via evaluate_candidates_canonical "
        "(same logic as original evaluate_candidates_core_30d). "
        "Frozen CSV is a reproducibility artifact, not the strategy definition."
    ),
    "entry_rule": (
        "Canonical = SIGNAL_TF_NEXT_OPEN_AFTER_SIGNAL_BAR (original XRP). "
        "Rejected alternate FIRST_1M_OPEN_AT_OR_AFTER_DECISION_AT as multicoin-only drift."
    ),
    "require_full_horizon": (
        "Canonical flag False (original matrix). Premature path end → INCOMPLETE, not TIME."
    ),
    "candle_pads": (
        "Canonical warm_pad=5d, outcome_pad=12h, source_pad=2h (aligned to original XRP loaders)."
    ),
    "xrp_parity_gate_scope": (
        "Gate compares only MULTICOIN_DETECTION_SCOPES, not all M0/M4/M5×5m/15m."
    ),
}
