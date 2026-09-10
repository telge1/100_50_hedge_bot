"""Episode-1 integration: independent touch/detection → wall-flow/QDH.

Frozen packages (semantically unmodified):
  - level_first_episode1_touch_detection_independent_v1
  - level_first_episode1_wall_flow_qdh_base_v1

research_visit_count=3 is ONLY a detector-stage Episode-1 research binding,
not a live candidate rule and not used inside the wall-flow stage.
"""

from __future__ import annotations

AUDIT_ID = "LEVEL_FIRST_EPISODE1_DETECTION_TO_WALL_FLOW_INTEGRATION_V1"
SCHEMA_VERSION = "level_first_episode1_detection_to_wall_flow_integration_v1"
RUN_PREFIX = "d2w1_"

# Read-only book tables from proven wall-flow E2E (timing files MUST NOT be used as calc inputs).
FROZEN_WALL_FLOW_BOOK_DIR = (
    "results/level_first_episode1_wall_flow_qdh_base_v1/BTCUSDT/wfq1_58db1918314881b6a"
)
FROZEN_WALL_FLOW_SEMANTIC_HASH = "f10b6398f2aa5f49f8a8834ac1a992e9e3bbe92d1a5975411938cfdeaecd5c36"
FROZEN_EXACT_HITS = 207
FROZEN_BAND_HITS = 214
FROZEN_TIMELINE_ROWS = 4178
FROZEN_TRADE_DEDUP = (29532, 29532, 0)

VERDICT_OK = "EPISODE1_INDEPENDENT_DETECTION_TO_WALL_FLOW_E2E_PROVEN"
VERDICT_BLOCKED = "EPISODE1_DETECTION_TO_WALL_FLOW_INTEGRATION_BLOCKED"

ALLOW_CLICKHOUSE_WRITES = False

# Files in book dir that must never be used as timing inputs by this adapter.
FORBIDDEN_TIMING_ARTIFACTS = (
    "independent_zone_first_touch.json",
    "independent_wall_first_touch.json",
    "independent_detection.json",
    "independent_wall_observation_at_zone_touch.json",
    "independent_derivation.json",
    "FIRST_TOUCH_ISO",
)
