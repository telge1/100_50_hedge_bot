"""Episode-1 touch/detection: independent event-time derivation from raw market data.

Frozen sibling packages must not be modified:
  - level_first_episode1_wall_flow_qdh_base_v1

This package must not use FIRST_TOUCH_ISO / known touch/detection timestamps
as calculation inputs. Reference ISOs are comparison-only.
"""

from __future__ import annotations

from datetime import datetime, timezone

AUDIT_ID = "LEVEL_FIRST_EPISODE1_TOUCH_DETECTION_INDEPENDENT_V1"
SCHEMA_VERSION = "level_first_episode1_touch_detection_independent_v1"
RUN_PREFIX = "tdi1_"
SYMBOL = "BTCUSDT"

# Fixed research objects known before T0 (zone), not touch timestamps.
PERSISTENT_CLUSTER_ID = "pc_7775a856ab22f006"
LEVEL_CLUSTER_ID = "cl_7949623c5c9e9f0e"
LEVEL_ID = "lvl:CLOSED:30m:TPO_VAL:1788723000"
# Ordinal visit binding for Episode 1 within this zone (not an ISO timestamp).
RESEARCH_VISIT_COUNT = 3

WALL_SIDE = "ask"
WALL_PRICE = 79780.0
TICK_SIZE = 0.1
BAND_TICKS = 5

# Comparison-only references (FORBIDDEN as calc inputs).
REFERENCE_ZONE_FIRST_TOUCH_ISO = "2026-09-06T20:19:02.229Z"
REFERENCE_WALL_FIRST_TOUCH_ISO = "2026-09-06T20:19:02.232Z"
REFERENCE_DETECTION_ISO = "2026-09-06T20:21:00Z"
REFERENCE_EPISODE_ID = "ep:pc_7775a856ab22f006:1788725942"

VERDICT_OK = "EPISODE1_TOUCH_DETECTION_EVENT_TIME_INDEPENDENTLY_DERIVED"
VERDICT_BLOCKED = "EPISODE1_TOUCH_DETECTION_INDEPENDENT_DERIVATION_BLOCKED"

ALLOW_CLICKHOUSE_WRITES = False
BUCKET_MS = 100
EVIDENCE_PRE_TOUCH_S = 300

FORBIDDEN_CALC_KEYS = (
    "first_touch",
    "FIRST_TOUCH",
    "FIRST_TOUCH_ISO",
    "detection",
    "DETECTION",
    "DETECTION_ISO",
    "detection_ts",
    "trigger_ts",
    "episode_start",
)


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
