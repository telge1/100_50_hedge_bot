"""Frozen contracts for FROZEN_HIGH_ACCEPTED_ENTRY_TIMING_V1.

Ex ante only — no outcome-based selection of latency/exit/cost.
"""

from __future__ import annotations

NO_FIT_ENTRY = {
    "outcome_used_for_matching": False,
    "outcome_used_for_thresholds": False,
    "outcome_used_for_state_definition": False,
    "outcome_used_for_sample_selection": False,
    "outcome_used_for_checkpoint_contract": False,
    "outcome_used_for_episode_contract": False,
    "outcome_used_for_entry_timestamp": False,
    "outcome_used_for_execution_rule": False,
    "outcome_used_for_exit_selection": False,
    "outcome_used_for_cost_selection": False,
}

EXPECTED_V2_FREEZE_PREFIX = "6ca0718e4c0420d51ff1"

# Direction from acceptance state only (not AEF compression direction).
ACCEPTANCE_TO_TRADE_SIDE = {
    "ACCEPTED_ABOVE": "LONG",
    "ACCEPTED_BELOW": "SHORT",
}

EXECUTION_CONTRACT = {
    **NO_FIT_ENTRY,
    "version": "FROZEN_HIGH_ACCEPTED_ENTRY_TIMING_EXECUTION_V1",
    "signal_available_ts": "earliest_causal_entry_ts_v2",
    "primary_latency_seconds": 1,
    "diagnostic_latency_seconds": [0, 2],
    "max_entry_lookup_seconds": 2,
    "max_exit_lookup_seconds": 2,
    "book_source": "raw_ob200_sample_best_bid_ask",
    "sample_ms": 250,
    "LONG_entry": "first_best_ask_at_or_after_legal_ts",
    "SHORT_entry": "first_best_bid_at_or_after_legal_ts",
    "LONG_exit": "first_best_bid_at_or_after_exit_ts",
    "SHORT_exit": "first_best_ask_at_or_after_exit_ts",
    "no_interpolation": True,
    "no_backward_snapshot": True,
    "acceptance_to_side": dict(ACCEPTANCE_TO_TRADE_SIDE),
}

COST_CONTRACT = {
    **NO_FIT_ENTRY,
    "version": "FROZEN_HIGH_ACCEPTED_ENTRY_TIMING_COST_V1",
    "primary": "taker_taker_plus_1bp_slippage_per_side",
    "entry_fee_rate": 0.00055,
    "exit_fee_rate": 0.00055,
    "roundtrip_fee_bps": 11.0,
    "primary_extra_slippage_bps_per_side": 1.0,
    "diagnostic_extra_slippage_bps_per_side": [0.0, 1.0, 2.0],
    "notional_usdt": 1000.0,
    "maker_taker_hypothetical_only": True,
    "zero_fee_decomposition_only": True,
}

EXIT_CONTRACT = {
    **NO_FIT_ENTRY,
    "version": "FROZEN_HIGH_ACCEPTED_ENTRY_TIMING_EXIT_V1",
    "primary_horizon_s": 300,
    "secondary_horizon_s": 900,
    "horizons_s": [300, 900],
    "no_tp_sl_trailing": True,
    "no_grid_search": True,
    "exit_selection_outcome_based": False,
}
