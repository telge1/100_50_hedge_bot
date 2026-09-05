"""Contracts for LARGE_MOVE_SEPARABILITY_DISCOVERY_V1."""

from __future__ import annotations

NO_FIT_LM = {
    "outcome_used_for_matching": False,
    "outcome_used_for_thresholds": False,
    "outcome_used_for_state_definition": False,
    "outcome_used_for_sample_selection": False,
    "outcome_used_for_checkpoint_contract": False,
    "outcome_used_for_episode_contract": False,
    "outcome_used_for_entry_timestamp": False,
    "outcome_used_for_feature_timestamp": False,
    "holdout_used_for_feature_selection": False,
    "holdout_used_for_threshold_selection": False,
    "holdout_used_for_model_selection": False,
}

EXPECTED_V2_SHA_PREFIX = "6ca0718e4c0420d51ff1"

LABEL_CONTRACT = {
    **NO_FIT_LM,
    "version": "LARGE_MOVE_LABEL_CONTRACT_V1",
    "primary_label": "LARGE_MOVE_25BPS_15M",
    "primary_threshold_bps": 25.0,
    "primary_horizon_s": 900,
    "primary_rationale": "11bps taker RT + spread/slippage buffer; fixed ex ante",
    "clean_label": "CLEAN_LARGE_MOVE_25_15",
    "adverse_barrier_bps": 15.0,
    "path_prices": "LONG uses bid path vs long entry; SHORT uses ask path vs short entry",
    "secondary_labels": [
        "LARGE_MOVE_20BPS_15M",
        "LARGE_MOVE_30BPS_15M",
        "LARGE_MOVE_25BPS_30M",
    ],
    "secondary_not_used_for_selection": True,
}

FEATURE_CONTRACT = {
    **NO_FIT_LM,
    "version": "LARGE_MOVE_FEATURE_CONTRACT_V1",
    "feature_cutoff": "executable_entry_ts (= entry_book_ts from ENTRY_TIMING_V1)",
    "max_features_in_candidate": 8,
    "max_features_per_family": 2,
    "windows_s": [5, 15, 30, 60],
    "no_centered_windows": True,
    "no_forward_windows": True,
    "families": [
        "pool_edge_geometry",
        "acceptance_quality",
        "public_trade_flow",
        "orderbook",
        "open_interest",
        "market_context",
    ],
    "liquidations": "excluded_if_source_unavailable",
}

MODEL_CONTRACT = {
    **NO_FIT_LM,
    "model_type": "sklearn.linear_model.LogisticRegression",
    "penalty": "l2",
    "C": 1.0,
    "solver": "lbfgs",
    "max_iter": 1000,
    "class_weight": None,
    "no_grid_search": True,
    "score_threshold_rule": "development_top_20pct_quantile",
    "standardize_on_development_only": True,
    "impute_median_on_development_only": True,
    "target_primary": "CLEAN_LARGE_MOVE_25_15",
}
