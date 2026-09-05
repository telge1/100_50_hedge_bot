"""Comparative post-audit synthesis for CASE_03–CASE_05 (read-only artefacts)."""

from __future__ import annotations

FORMAT_VERSION = "case_03_05_entry_contract_comparative_synthesis/v1"
VERDICT = "CASE_03_05_COMPARATIVE_SYNTHESIS_COMPLETE_SMALL_N"

CASE_SEQUENCE_FREEZE_SHA256 = (
    "5ec44b95273af34508c327c841d5734e4ff1193caacb332d1f9d1e2cf79140d8"
)
ENTRY_CONTRACT_FREEZE_SHA256 = (
    "76b79cce5ceac816feade974521f0b4f876adb5ab6960e54d6e9498b93e97494"
)
CONFIG_SHA256 = "905c8f6cd3b642cb356fe80baab64a80be231a905645a16b2e21a7b79a870050"
MIN_ROOM_BPS = 50.0
COST_BPS = (11.0, 15.0, 20.0)

EXPOSURE = {
    "CASE_03": "PRE_ENTRY_CONTRACT_EXPOSED",
    "CASE_04": "PRE_ENTRY_CONTRACT_EXPOSED",
    "CASE_05": "PROSPECTIVE_ENTRY_CONTRACT_TEST",
}

CASE_DIRS = {
    "CASE_03": "case_03_frozen_bid_pool_causal_reaction_audit_v1",
    "CASE_04": "case_04_frozen_bid_pool_causal_reaction_audit_v1",
    "CASE_05": "case_05_frozen_bid_pool_entry_contract_v1_audit",
}
