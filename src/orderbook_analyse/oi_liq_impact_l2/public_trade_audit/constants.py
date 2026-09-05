"""Constants for public trade impact compression audit."""

from __future__ import annotations

FORMAT_VERSION = "oi_liq_impact_l2_public_trade_audit/v1"

VERDICT_COMPLETE = "BTC_PUBLIC_TRADE_IMPACT_AUDIT_COMPLETE"
VERDICT_BLOCKED = "BTC_PUBLIC_TRADE_IMPACT_AUDIT_BLOCKED"

CATEGORY_SUSTAINED_FLOW_COMPRESSION = "SUSTAINED_FLOW_COMPRESSION"
CATEGORY_FALLING_FLOW_LOW_IMPACT = "FALLING_FLOW_LOW_IMPACT"
CATEGORY_SUSTAINED_FLOW_NO_COMPRESSION = "SUSTAINED_FLOW_NO_COMPRESSION"
CATEGORY_FALLING_FLOW_NO_COMPRESSION = "FALLING_FLOW_NO_COMPRESSION"
CATEGORY_INVALID_OR_ZERO_FLOW = "INVALID_OR_ZERO_FLOW"

ALL_CATEGORIES = (
    CATEGORY_SUSTAINED_FLOW_COMPRESSION,
    CATEGORY_FALLING_FLOW_LOW_IMPACT,
    CATEGORY_SUSTAINED_FLOW_NO_COMPRESSION,
    CATEGORY_FALLING_FLOW_NO_COMPRESSION,
    CATEGORY_INVALID_OR_ZERO_FLOW,
)

DEFAULT_INPUT_DIR = "results/oi_liq_impact_l2/aggregate_wall_proxy_btc_f3"
DEFAULT_OUTPUT_DIR = "results/oi_liq_impact_l2/public_trade_impact_audit_btc"
DEFAULT_WINDOWS = (5, 10)
DEFAULT_HORIZONS = (1, 3, 5, 10, 15, 30, 60)

REQUIRED_INPUT_FILES = (
    "impact_compression_metrics.csv",
    "proxy_events.csv",
    "proxy_reclaims.csv",
    "matched_controls.csv",
    "proxy_manifest.json",
)

OPTIONAL_INPUT_FILES = ("incremental_feature_groups.csv", "proxy_timeline_1s.csv")

# Direction-specific aggressive notional semantics (read-only audit labels).
AGGRESSIVE_NOTIONAL_SIDE = {"LONG": "sell_notional", "SHORT": "buy_notional"}

WINDOW_COMPARISONS: tuple[tuple[str, str, str], ...] = (
    ("first5_last5", "first5", "last5"),
    ("first10_last10", "first10", "last10"),
    ("first_half_second_half", "first_half", "second_half"),
)

IMPACT_CLASSIFICATION_FIELDS = (
    "cluster_id",
    "direction",
    "comparison_pair",
    "category",
    "data_abort",
    "trades_present",
    "first_aggressive_notional",
    "last_aggressive_notional",
    "notional_ratio_last_over_first",
    "first_impact_per_notional",
    "last_impact_per_notional",
    "impact_ratio_last_over_first",
    "adverse_extension_at_anchor",
    "aggregate_depth_recovery_observed",
    "flip_tradeflow_second",
    "flip_ofi_second",
    "flip_microprice_second",
    "classification_window_end_second",
)

IMPACT_CATEGORY_SUMMARY_FIELDS = (
    "scope",
    "comparison_pair",
    "category",
    "cluster_count",
    "cluster_fraction",
    "median_first_aggressive_notional",
    "median_last_aggressive_notional",
    "median_notional_ratio_last_over_first",
    "median_first_impact_per_notional",
    "median_last_impact_per_notional",
    "median_impact_ratio_last_over_first",
)

POST_COMPRESSION_OUTCOME_FIELDS = (
    "cluster_id",
    "direction",
    "comparison_pair",
    "category",
    "horizon_minutes",
    "outcome_start_second",
    "pre_flush_close_reclaim",
    "minutes_to_pre_flush_close_reclaim",
    "max_further_adverse_extension",
    "forward_return_episode_direction",
    "mfe_episode_direction",
    "mae_episode_direction",
)

MATCHED_CONTROL_COMPARISON_FIELDS = (
    "comparison_pair",
    "metric",
    "flush_value",
    "control_value",
    "flush_n",
    "control_n",
    "note",
)

NON_OVERLAPPING_ROBUSTNESS_FIELDS = (
    "subset",
    "scope",
    "comparison_pair",
    "category",
    "cluster_count",
    "cluster_fraction",
)
