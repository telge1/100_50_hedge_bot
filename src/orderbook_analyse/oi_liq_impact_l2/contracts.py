"""Single-source discovery data and direction contracts."""

from __future__ import annotations

ORDERBOOK_TABLE = "orderbook_analysis.orderbook_features_1s_v2"
ORDERBOOK_PARSER_VERSION = "ob200_v3"
ORDERBOOK_DEPTH = 200
ORDERBOOK_EXPECTED_SECONDS_PER_MINUTE = 60
ORDERBOOK_GENUINE_SQL = (
    "is_valid = 1 "
    "AND NOT has(splitByChar(',', quality_flags), 'carried_forward')"
)
ORDERBOOK_GENUINE_DESCRIPTION = (
    "is_valid=1 and quality_flags does not contain carried_forward"
)
ORDERBOOK_CARRIED_FORWARD_POLICY = (
    "carried_forward seconds never contribute L2 dynamics or recovery"
)

L2_SIDE_BY_DIRECTION = {
    "LONG": "bid/support",
    "SHORT": "ask/resistance",
}
LIQUIDATION_SIDE_BY_DIRECTION = {
    "LONG": "LIQUIDATED_LONG",
    "SHORT": "LIQUIDATED_SHORT",
}
AGGRESSOR_SIDE_BY_DIRECTION = {
    "LONG": "Sell",
    "SHORT": "Buy",
}
AGGRESSIVE_NOTIONAL_COLUMN_BY_DIRECTION = {
    "LONG": "sell_notional",
    "SHORT": "buy_notional",
}
OPPOSITE_NOTIONAL_COLUMN_BY_DIRECTION = {
    "LONG": "buy_notional",
    "SHORT": "sell_notional",
}
LIQUIDATION_COUNT_COLUMN_BY_DIRECTION = {
    "LONG": "liquidated_long_count",
    "SHORT": "liquidated_short_count",
}
LIQUIDATION_NOTIONAL_COLUMN_BY_DIRECTION = {
    "LONG": "liquidated_long_notional",
    "SHORT": "liquidated_short_notional",
}
L2_DEPTH_COLUMN_BY_DIRECTION = {
    "LONG": "genuine_bid_depth_l50_mean",
    "SHORT": "genuine_ask_depth_l50_mean",
}
L2_OPPOSING_DEPTH_COLUMN_BY_DIRECTION = {
    "LONG": "genuine_ask_depth_l50_mean",
    "SHORT": "genuine_bid_depth_l50_mean",
}
L2_ADDED_COLUMN_BY_DIRECTION = {
    "LONG": "genuine_bid_qty_added",
    "SHORT": "genuine_ask_qty_added",
}
L2_REMOVED_COLUMN_BY_DIRECTION = {
    "LONG": "genuine_bid_qty_removed",
    "SHORT": "genuine_ask_qty_removed",
}
L2_RECOVERY_RELATION = (
    "current directional depth > previous directional depth "
    "OR current directional imbalance > previous directional imbalance "
    "OR current directional net-add flow > previous directional net-add flow; "
    "both minutes must be consecutive valid minutes with genuine L2 seconds"
)
