"""LOCALIZED_EXCLUSION_V1 — isolate Full-OB sequence gaps / OI stale without discarding whole windows."""

from __future__ import annotations

POLICY_ID = "LOCALIZED_EXCLUSION_V1"
POLICY_VERSION = "1.0.0"
STRICT_POLICY_ID = "STRICT_WHOLE_WINDOW"

VERDICT_COMPLETE = "DATA_COMPLETE"
VERDICT_USABLE = "DATA_USABLE_WITH_EXCLUSIONS"
VERDICT_NOT_COMPLETE = "DATA_NOT_COMPLETE"

# Contaminated interval may be locally excluded up to this duration (seconds).
# Longer contamination is a hard local gap; other independent usable spans may remain.
MAX_LOCAL_EXCLUSION_DURATION_SECONDS = 300

# OI hard age (ms) — existing contract
OI_HARD_AGE_MS = 120_000

# Checkpoint / snapshot reasons that may end TAINTED (must still pass book validation)
RECOVERY_CHECKPOINT_REASONS = frozenset(
    {"segment_start", "exchange_snapshot", "reconnect_resync"}
)

INTERVAL_VALID = "VALID"
INTERVAL_TAINTED_SEQUENCE_GAP = "TAINTED_SEQUENCE_GAP"
INTERVAL_TAINTED_RESYNC = "TAINTED_RESYNC"
INTERVAL_MISSING_WALL_CLOCK = "MISSING_WALL_CLOCK"
INTERVAL_VALID_AFTER_CHECKPOINT = "VALID_AFTER_CHECKPOINT"
INTERVAL_OI_STALE = "OI_STALE"
INTERVAL_HARD_LOCAL_GAP = "HARD_LOCAL_GAP"

# Stage OI policy documentation
OI_STAGE_POLICY = {
    "CANDIDATE_DETECT": "OI optional context — emit OI_CONTEXT_UNAVAILABLE; do not drop candidate",
    "EPISODE_GROUP": "OI optional — keep episode; flag OI_CONTEXT_UNAVAILABLE",
    "AVR_CONTEXT": "OI optional context fields only",
    "AVR_MULTISCALE_CONTEXT": "OI optional for w60s/w300s; mark oi_coverage_status when missing",
    "PUBLIC_TRADE_OUTCOMES": "OI not required",
}
