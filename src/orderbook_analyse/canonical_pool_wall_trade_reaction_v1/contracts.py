"""Constants and paths for Pool × Wall × Trade reaction V1."""

from __future__ import annotations

from pathlib import Path

OA_ROOT = Path("/home/telgenbuescher/projects/orderbook_analyse")
STRUCT_ROOT = OA_ROOT / "results/canonical_pool_structural_class_analysis_v1"
OUT_ROOT = OA_ROOT / "results/canonical_pool_wall_trade_reaction_v1"

SYMBOL = "BTCUSDT"
FEATURES_TABLE = "orderbook_analysis.orderbook_features_1s_v2"
TRADES_TABLE = "orderbook_analysis.public_trades_canonical"

# Structural freeze hashes (inputs must match)
EXPECTED_STRUCTURAL_SPEC = "cbe69a4da27e18596246bfa997758c5f81173962572b1350793e93f6b36b0e02"
EXPECTED_STRUCTURAL_BUNDLE = "81b03ad86f4345937fcedd33304e4fd8fbb923f16269c62ae6c02d40f18fb6e4"

# Analysis window clipped to features availability
ANALYSIS_START = "2026-08-01T00:00:00Z"
ANALYSIS_END = "2026-08-28T16:26:23Z"

# Mechanical thresholds (documented, not fitted)
WALL_IN_ZONE_MIN_FRAC = 0.05  # ≥5% of episode seconds with wall inside zone
TOUCH_TOLERANCE_BPS = 2.0  # mid within 2 bps of front edge counts as touch
FORWARD_SECONDS = 1800  # 30m after first touch
WALL_LOOKAROUND_SECONDS = 60  # ±60s around touch for eaten/pulled
EATEN_TRADE_FRAC = 0.5  # trade notional into wall ≥ 50% of wall-notional drop → EATEN
PULLED_TRADE_FRAC = 0.15  # drop with little trade → PULLED
REJECT_REVERSAL_ZONE_FRAC = 0.5  # reverse ≥50% of zone height without back-edge cross
PASS_BACK_EDGE = True  # mid beyond back edge → PASSED_THROUGH
