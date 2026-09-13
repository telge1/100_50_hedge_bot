"""Frozen Phase-2 evaluation constants.

These values must match evaluation_contract_frozen_v1.yaml. They define the
evaluation protocol only and do not change the Phase-1 episode contract.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Final

EVALUATION_CONTRACT_VERSION: Final = "liquidity_destination_baseline_eval_v1"
RESEARCH_BANNER: Final = "RESEARCH_ONLY_NO_TRADE"

PRIMARY_CLASSES: Final = (
    "UPPER_FIRST",
    "LOWER_FIRST",
    "NEITHER_WITHIN_HORIZON",
)
DIRECTIONAL_CLASSES: Final = ("UPPER_FIRST", "LOWER_FIRST")
AMBIGUOUS_OUTCOME: Final = "SIMULTANEOUS_OR_AMBIGUOUS"
ALLOWED_OUTCOMES: Final = PRIMARY_CLASSES + (AMBIGUOUS_OUTCOME,)

MAJORITY_TIE_BREAK: Final = (
    "NEITHER_WITHIN_HORIZON",
    "UPPER_FIRST",
    "LOWER_FIRST",
)

REQUIRED_COLUMNS: Final = (
    "episode_id",
    "symbol",
    "t0_utc",
    "distance_upper_bps",
    "distance_lower_bps",
    "outcome",
)

SPLIT_BOUNDS: Final = {
    "TRAIN": (
        datetime(2026, 8, 25, tzinfo=timezone.utc),
        datetime(2026, 8, 29, tzinfo=timezone.utc),
    ),
    "VALIDATION": (
        datetime(2026, 8, 29, tzinfo=timezone.utc),
        datetime(2026, 8, 30, tzinfo=timezone.utc),
    ),
    "TEST": (
        datetime(2026, 8, 30, tzinfo=timezone.utc),
        None,
    ),
}

EXPECTED_DATASET_FINGERPRINT: Final = (
    "9f7d2bd2fd1aff9315b268c70f818020ba91f3c2f5992f3dcd15aec550ad77e8"
)
EXPECTED_EPISODE_HASHES: Final = {
    "BTCUSDT": "ca3ba554e59653fbdbe03124d1558aa3f4f767c274c532c9c611d92abf4cf1a4",
    "DOGEUSDT": "2d1217b46d490bed10377add4318563e461eefb6a509a943631e9d5e8d0b08af",
}

BASELINE_IDS: Final = (
    "B0_TRAIN_MAJORITY",
    "B1_NEAREST_TARGET",
    "B2_INVERSE_NEAREST",
    "B3_DETERMINISTIC_HASH_CONTROL",
    "B4_SYMBOL_TRAIN_MAJORITY",
)
