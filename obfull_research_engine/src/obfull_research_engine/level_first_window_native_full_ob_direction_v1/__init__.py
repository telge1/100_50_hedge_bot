"""LEVEL_FIRST_WINDOW_NATIVE_FULL_OB_DIRECTION_V1 — outcome-blind book evidence."""

from __future__ import annotations

CONTRACT_NAME = "LEVEL_FIRST_WINDOW_NATIVE_FULL_OB_DIRECTION_V1"
CONTRACT_VERSION = "1.0.0"
CONFIG_REL = "config/level_first_window_native_full_ob_direction_v1.json"
SCHEMA_REL = "contracts/level_first_window_native_full_ob_direction_v1.schema.json"
DRILLDOWN_CONFIG_REL = "config/event_drilldown_v1.json"
EXPECTED_DRILLDOWN_SHA256 = "8589bb8affd760ce2ffe19e371d73a4e7a4bf29152a783b46a543af73b4a0d05"

REPAIRED_PILOT_DIR = (
    "results/bounded_level_first_analyzer_pilot_v1/BTCUSDT/lf1_69e21d12d280596e"
)
CAPABILITY_AUDIT_DIR = (
    "results/level_first_existing_full_ob_directional_adapter_v1/BTCUSDT/foa1_da2bd17fbc9b98d0"
)
FROZEN_AUDIT = (
    "results/level_first_analyzer_functional_integrity_audit_v1/BTCUSDT/fia1_265e28bad7503190"
)
FROZEN_ORIGINAL_PILOT = (
    "results/bounded_level_first_analyzer_pilot_v1/BTCUSDT/lf1_7a49869694acb931"
)

SUPPORT_FAMILIES = (
    "FRONT_LIQUIDITY_REMOVAL_SUPPORT",
    "BACK_LIQUIDITY_DEFENSE_SUPPORT",
    "DIRECTIONAL_IMBALANCE_SUPPORT",
    "NON_BLOCKING_FRONT_BOOK_SUPPORT",
)
CONTRADICTION_FAMILIES = (
    "FRONT_WALL_CONTRADICTION",
    "BACK_LIQUIDITY_PULL_CONTRADICTION",
    "DIRECTIONAL_IMBALANCE_CONTRADICTION",
    "OPPOSING_REFILL_CONTRADICTION",
)
FAMILY_STATUS = ("SUPPORTED", "CONTRADICTED", "NOT_OBSERVED", "NOT_EVALUATED")
OVERALL_CLASSES = (
    "FULL_OB_STRONGLY_SUPPORTS_REACTION",
    "FULL_OB_PARTIALLY_SUPPORTS_REACTION",
    "FULL_OB_CONTRADICTS_REACTION",
    "FULL_OB_MIXED",
    "FULL_OB_UNCLEAR",
    "FULL_OB_NOT_EVALUATED",
    "FULL_OB_DIRECTION_NOT_EVALUATED",
)
REACTION_CLASSES = (
    "ACCEPTED_ABOVE",
    "ACCEPTED_BELOW",
    "RECLAIMED_UP",
    "RECLAIMED_DOWN",
    "REJECTED_UP_FROM_SUPPORT_CONTEXT",
    "REJECTED_DOWN_FROM_RESISTANCE_CONTEXT",
)

def __getattr__(name: str):
    if name == "run_window_native":
        from .runner import run_window_native

        return run_window_native
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["CONTRACT_NAME", "run_window_native"]
