"""
Suggested resource limits for multi-batch historical import (documentation + optional env).

Values derived from Phase 4J measurements (~15s / 30 snapshots) and current host baseline.
Not enforced unless corresponding ``HISTORICAL_IMPORT_*`` env vars are set.
"""

from __future__ import annotations

from typing import Any

# Observed Oct 2026 on import host (catalog mount + CH local)
OBSERVED_DISK_FREE_BYTES = 282_190_442_496  # ~263 GiB
OBSERVED_BATCH_30_SECONDS = 15.0
OBSERVED_READBACK_30_SECONDS = 3.0

# Conservative proposals (not auto-applied)
PROPOSED_MIN_FREE_BYTES = 50 * (1024**3)  # 50 GiB headroom below observed free
PROPOSED_MAX_ACTIVE_MERGES = 2
PROPOSED_MAX_ORPHAN_MEMBER_SNAPSHOTS = 0
PROPOSED_MAX_LMV2_ACTIVE_PARTS = 40  # sum across LMV2 tables; optional future env
PROPOSED_MAX_BATCH_WALL_SECONDS = 120  # per 50-close batch
PROPOSED_MAX_READBACK_WALL_SECONDS = 60


def proposal_document() -> dict[str, Any]:
    return {
        "disk": {
            "observed_free_bytes": OBSERVED_DISK_FREE_BYTES,
            "proposed_min_free_bytes": PROPOSED_MIN_FREE_BYTES,
            "env": "HISTORICAL_IMPORT_MIN_FREE_BYTES",
        },
        "clickhouse_merges": {
            "proposed_max_active_merges": PROPOSED_MAX_ACTIVE_MERGES,
            "env": "HISTORICAL_IMPORT_MAX_ACTIVE_MERGES",
        },
        "orphans": {
            "proposed_max_orphan_member_snapshots": PROPOSED_MAX_ORPHAN_MEMBER_SNAPSHOTS,
            "env": "HISTORICAL_IMPORT_MAX_ORPHAN_MEMBER_SNAPSHOTS",
        },
        "parts": {
            "proposed_max_lmv2_active_parts": PROPOSED_MAX_LMV2_ACTIVE_PARTS,
            "env": "HISTORICAL_IMPORT_MAX_LMV2_ACTIVE_PARTS",
            "note": "optional; wire in ch_import_resource_monitor when env set",
        },
        "timing": {
            "observed_batch_30s": OBSERVED_BATCH_30_SECONDS,
            "observed_readback_30s": OBSERVED_READBACK_30_SECONDS,
            "proposed_max_batch_wall_seconds": PROPOSED_MAX_BATCH_WALL_SECONDS,
            "proposed_max_readback_wall_seconds": PROPOSED_MAX_READBACK_WALL_SECONDS,
            "env_batch": "HISTORICAL_IMPORT_MAX_BATCH_WALL_SECONDS",
            "env_readback": "HISTORICAL_IMPORT_MAX_READBACK_WALL_SECONDS",
        },
        "parallelism": "forbidden — single writer lock only",
    }


def proposed_500_batch_limit_environ() -> dict[str, str]:
    """
    Env key/values for the planned 500×50 April ``15m_PD1`` run.

    Not applied automatically — set explicitly before a freigegebener import run.
    Does **not** include write/sidecar gates.
    """
    return {
        "HISTORICAL_IMPORT_MIN_FREE_BYTES": str(PROPOSED_MIN_FREE_BYTES),
        "HISTORICAL_IMPORT_MAX_ORPHAN_MEMBER_SNAPSHOTS": str(
            PROPOSED_MAX_ORPHAN_MEMBER_SNAPSHOTS
        ),
        "HISTORICAL_IMPORT_MAX_BATCH_WALL_SECONDS": str(PROPOSED_MAX_BATCH_WALL_SECONDS),
        "HISTORICAL_IMPORT_MAX_READBACK_WALL_SECONDS": str(
            PROPOSED_MAX_READBACK_WALL_SECONDS
        ),
    }
