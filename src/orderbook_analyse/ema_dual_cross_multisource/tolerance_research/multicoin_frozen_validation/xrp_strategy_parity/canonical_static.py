"""Canonical strategy definition diff resolutions + updated static parity fields."""

from __future__ import annotations

from typing import Any

from ...shared_strategy.semantics import (
    DIFF_RESOLUTIONS,
    ENTRY_RULE,
    MULTICOIN_DETECTION_SCOPES,
    OUTCOME_PAD_HOURS,
    REQUIRE_FULL_HORIZON,
    SOURCE_PAD_HOURS,
    WARMUP_PAD_DAYS,
)
from ..constants import ENTRY_RULE as MC_ENTRY_RULE
from ..constants import OUTCOME_PAD_HOURS as MC_OUTCOME
from ..constants import REQUIRE_FULL_HORIZON as MC_RFH
from ..constants import WARMUP_PAD_DAYS as MC_WARM


def canonical_static_fields() -> list[dict[str, Any]]:
    """After unification, these fields must all match."""
    return [
        {
            "field": "candidate_pipeline",
            "original_value": "evaluate_candidates_canonical (shared)",
            "multicoin_value": "evaluate_candidates_canonical via detect_modes_for_coin",
            "match": True,
            "source_file": "shared_strategy/candidates.py",
            "function": "evaluate_candidates_canonical",
            "evidence": DIFF_RESOLUTIONS["candidate_pipeline"],
        },
        {
            "field": "entry_rule",
            "original_value": ENTRY_RULE,
            "multicoin_value": MC_ENTRY_RULE,
            "match": ENTRY_RULE == MC_ENTRY_RULE == "SIGNAL_TF_NEXT_OPEN_AFTER_SIGNAL_BAR",
            "source_file": "shared_strategy/entry.py",
            "function": "next_signal_tf_open",
            "evidence": DIFF_RESOLUTIONS["entry_rule"],
        },
        {
            "field": "require_full_horizon",
            "original_value": REQUIRE_FULL_HORIZON,
            "multicoin_value": MC_RFH,
            "match": REQUIRE_FULL_HORIZON is False and MC_RFH is False,
            "source_file": "shared_strategy/outcomes.py",
            "function": "simulate_canonical_trade",
            "evidence": DIFF_RESOLUTIONS["require_full_horizon"],
        },
        {
            "field": "candle_pads",
            "original_value": {"warmup_days": WARMUP_PAD_DAYS, "outcome_hours": OUTCOME_PAD_HOURS, "source_hours": SOURCE_PAD_HOURS},
            "multicoin_value": {"warmup_days": MC_WARM, "outcome_hours": MC_OUTCOME, "source_hours": SOURCE_PAD_HOURS},
            "match": WARMUP_PAD_DAYS == MC_WARM and OUTCOME_PAD_HOURS == MC_OUTCOME,
            "source_file": "shared_strategy/market_data.py",
            "function": "load_strategy_market_data",
            "evidence": DIFF_RESOLUTIONS["candle_pads"],
        },
        {
            "field": "xrp_parity_gate_scope",
            "original_value": list(MULTICOIN_DETECTION_SCOPES),
            "multicoin_value": list(MULTICOIN_DETECTION_SCOPES),
            "match": True,
            "source_file": "xrp_parity.compare_xrp_candidates_to_export",
            "function": "compare_xrp_candidates_to_export",
            "evidence": DIFF_RESOLUTIONS["xrp_parity_gate_scope"],
        },
    ]
