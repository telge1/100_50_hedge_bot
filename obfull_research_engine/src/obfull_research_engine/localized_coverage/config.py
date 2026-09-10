"""Policy config hash."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from . import (
    MAX_LOCAL_EXCLUSION_DURATION_SECONDS,
    OI_HARD_AGE_MS,
    OI_STAGE_POLICY,
    POLICY_ID,
    POLICY_VERSION,
    RECOVERY_CHECKPOINT_REASONS,
)


def policy_config_payload() -> dict[str, Any]:
    return {
        "policy_id": POLICY_ID,
        "policy_version": POLICY_VERSION,
        "max_local_exclusion_duration_seconds": MAX_LOCAL_EXCLUSION_DURATION_SECONDS,
        "oi_hard_age_ms": OI_HARD_AGE_MS,
        "recovery_checkpoint_reasons": sorted(RECOVERY_CHECKPOINT_REASONS),
        "oi_stage_policy": OI_STAGE_POLICY,
        "notes": {
            "missing_u_ids_are_not_duration": True,
            "no_interpolation": True,
            "tainted_until_validated_full_book_checkpoint": True,
            "periodic_5m_does_not_heal_gap": True,
        },
    }


def policy_config_hash() -> str:
    blob = json.dumps(policy_config_payload(), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()
