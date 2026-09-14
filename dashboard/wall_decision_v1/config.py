"""Central provisional thresholds — do not scatter magic numbers in JS callers."""

from __future__ import annotations

RULE_VERSION = "wall_decision_v1_provisional"

# Explicitly provisional — not calibrated for expectancy.
V1_PROVISIONAL = {
    "wall_consume_pct": 0.65,
    "trade_explained_pct": 0.60,
    "aggressor_share": 0.65,
    "acceptance_sec": 15,
    "replenish_max_pct": 0.25,
    "hysteresis_ticks": 2,
    "major_wall_median_mult": 3.0,
    "mark": "V1_PROVISIONAL",
}

SHADOW_RETENTION_DAYS = 14
SHADOW_DIR_NAME = "wall_decision_shadow"
# Retention: daily JSONL files under dashboard/logs/wall_decision_shadow/;
# files older than SHADOW_RETENTION_DAYS (mtime) are pruned on append.
# Format is append-only JSONL (not a single growing JSON blob).
