"""F3 aggregate wall proxy discovery constants."""

from __future__ import annotations

from decimal import Decimal

FORMAT_VERSION = "oi_liq_impact_l2_aggregate_proxy/v1"
SYMBOL = "BTCUSDT"
WINDOW_START = "2026-08-20T12:33:00Z"
WINDOW_END = "2026-08-24T06:35:00Z"
BTCUSDT_TICK = Decimal("0.1")
PRIMARY_CLUSTER_GAP = 1
CLUSTER_GAP_SENSITIVITIES = (1, 2, 3, 5)
BASELINE_MINUTES = 5
HORIZON_MINUTES = 60
TIME_MARKS_MINUTES = (1, 3, 5, 10, 15, 30, 60)
TICK_NEAR_SENSITIVITIES = (1, 2, 3)
COMPRESSION_BUCKETS_MINUTES = ((1, 3), (4, 5), (6, 10), (11, 15), (16, 30), (31, 60))

WALL_STATUS_EXACT = "DOMINANT_WALL_STABLE_EXACT"
WALL_STATUS_NEAR = "DOMINANT_WALL_STABLE_NEAR"
WALL_STATUS_CHANGED = "DOMINANT_WALL_CHANGED"
WALL_STATUS_MISSING = "DOMINANT_WALL_MISSING"
WALL_STATUS_INVALID = "WALL_PROXY_DATA_INVALID"

RECLAIM_ANCHORS = (
    "PRE_FLUSH_CLOSE",
    "DOMINANT_WALL_ANCHOR_PRICE",
    "FLUSH_CLUSTER_VWAP",
    "ADVERSE_EXTREME_PRICE",
)

DEFAULT_F1_DIR = "results/oi_liq_impact_l2/discovery_smoke_btc_60m_v2"
DEFAULT_F2_DIR = "results/oi_liq_impact_l2/event_chain_btc_60m_f2"
DEFAULT_OUTPUT_DIR = "results/oi_liq_impact_l2/aggregate_wall_proxy_btc_f3"

# Schema mapping: direction -> dominant wall columns in orderbook_features_1s_v2
WALL_PRICE_COLUMN = {"LONG": "bid_wall_price", "SHORT": "ask_wall_price"}
WALL_QTY_COLUMN = {"LONG": "bid_wall_qty", "SHORT": "ask_wall_qty"}
WALL_BPS_DIST_COLUMN = {"LONG": "bid_wall_bps_dist", "SHORT": "ask_wall_bps_dist"}

DEPTH_L_COLUMNS = {
    "L5": ("bid_qty_l5", "ask_qty_l5", "imbalance_l5"),
    "L10": ("bid_qty_l10", "ask_qty_l10", "imbalance_l10"),
    "L25": ("bid_qty_l25", "ask_qty_l25", "imbalance_l25"),
    "L50": ("bid_qty_l50", "ask_qty_l50", "imbalance_l50"),
}
DEPTH_BPS_COLUMNS = {
    "bps5": ("bid_qty_bps5", "ask_qty_bps5", "imbalance_bps5"),
    "bps10": ("bid_qty_bps10", "ask_qty_bps10", "imbalance_bps10"),
    "bps25": ("bid_qty_bps25", "ask_qty_bps25", "imbalance_bps25"),
    "bps50": ("bid_qty_bps50", "ask_qty_bps50", "imbalance_bps50"),
}

DIRECTIONAL_DEPTH_COL = {"LONG": "bid_qty_l50", "SHORT": "ask_qty_l50"}
DIRECTIONAL_OPPOSING_DEPTH_COL = {"LONG": "ask_qty_l50", "SHORT": "bid_qty_l50"}
DIRECTIONAL_IMBALANCE_COL = "imbalance_l50"
DIRECTIONAL_ADD_COL = {"LONG": "bid_qty_added", "SHORT": "ask_qty_added"}
DIRECTIONAL_REMOVE_COL = {"LONG": "bid_qty_removed", "SHORT": "ask_qty_removed"}
AGGRESSIVE_NOTIONAL_COL = {"LONG": "sell_notional", "SHORT": "buy_notional"}

PROXY_SEMANTICS = (
    "Aggregate proxy only; no per-level wall lifecycle claims.",
    "DOMINANT_WALL_CHANGED does not imply removal of prior wall level.",
    "DOMINANT_WALL_REAPPEARED is not a refill claim.",
    "carried_forward describes known state, not dynamics.",
    "Side aggregate add/remove is side dynamics, not dominant wall refill.",
)
