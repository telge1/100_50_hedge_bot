"""V3W TP research — frozen parameter grids (no post-hoc tuning)."""

from __future__ import annotations

from dataclasses import dataclass

DRAWINGS_SNAPSHOT = "../l1_v3u_drawings_snapshot_d05d218646aced84.json"
DRAWINGS_SHA256 = "d05d218646aced84ffc0adbb8ca66a6a8f92048190e72dc9cea3140270fb594c"

ZONE_MERGE_GAP_PCT = 0.15  # matches config.ZONE_MERGE_GAP_PCT

# TP2 — first substantial zone (pre-declared combos)
@dataclass(frozen=True)
class TP2Params:
    code: str
    min_width_pct: float
    min_depth: int
    min_origins: int


TP2_GRID: tuple[TP2Params, ...] = (
    TP2Params("TP2a_w008_d3_o3", 0.08, 3, 3),
    TP2Params("TP2b_w012_d4_o4", 0.12, 4, 4),
    TP2Params("TP2c_w015_d5_o4", 0.15, 5, 4),
)

# TP3 — density pick (pre-declared)
TP3_VARIANTS: tuple[str, ...] = (
    "TP3a_max_depth",
    "TP3b_max_origins",
    "TP3c_depth_x_width",
)

# Control trades (V3V reproduction)
CONTROL_AUG26 = "2026-08-26 15:10:00 UTC"
CONTROL_SEP24 = "2026-09-24 09:40:00 UTC"

OOS_HOLDOUT_START_UTC = "2026-09-16 00:00:00 UTC"  # exploratory split
