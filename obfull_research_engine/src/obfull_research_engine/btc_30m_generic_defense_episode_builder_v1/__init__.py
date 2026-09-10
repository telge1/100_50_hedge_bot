"""Generic BTC 30m defense episode builder (no Episode-1 special branch).

Discovers CLOSED 30m MP zones, chronological visits, attack clusters, past-only
walls, touches, features, and outcomes under a strict feature/outcome separation.

Verdict candidate: BTC_30M_GENERIC_DEFENSE_EPISODE_BUILDER_V1
"""

from __future__ import annotations

from ..wall_defense_outcome_contract_v1 import CONTRACT_HASH as _PINNED_CONTRACT_HASH
from ..wall_defense_outcome_contract_v1 import ATTACK_CLUSTER_GAP_MS_DEFAULT, outcome_contract_hash

AUDIT_ID = "BTC_30M_GENERIC_DEFENSE_EPISODE_BUILDER_V1"
SCHEMA_VERSION = "btc_30m_generic_defense_episode_builder_v1"
RUN_PREFIX = "gdeb1_"
BOOK_SOURCE = "ORDERBOOK_FULL"

# Pin must match frozen wall_defense_outcome_contract_v1.
CONTRACT_HASH = _PINNED_CONTRACT_HASH
EXPECTED_CONTRACT_HASH = "efd620a305e8252e61f03c516c69de364049153e6fb02c091caf6137e4e7e46a"

SNAPSHOT_OFFSETS_S = (0, 1, 3, 5, 10, 15, 30, 60, 120)
ANCHORS = (
    "ZONE_FIRST_TOUCH",
    "WALL_FIRST_TOUCH",
    "FIRST_JOINT_BREACH",
    "DETECTION",
)

ATTACK_CLUSTER_GAP_MS = ATTACK_CLUSTER_GAP_MS_DEFAULT  # 60_000 from contract

# Oracle regression target — NOT a selection / calc input.
EPISODE1_REDISCOVERY_EPISODE_ID = "ep:pc_7775a856ab22f006:1788725942"
EPISODE1_REDISCOVERY_PERSISTENT_CLUSTER_ID = "pc_7775a856ab22f006"

WORKTREE_ROOT = "/home/telgenbuescher/projects/orderbook_analyse_btc30m_v1"

assert CONTRACT_HASH == EXPECTED_CONTRACT_HASH, (
    f"contract hash drift: {CONTRACT_HASH} != {EXPECTED_CONTRACT_HASH}"
)
assert outcome_contract_hash() == EXPECTED_CONTRACT_HASH

__all__ = [
    "AUDIT_ID",
    "SCHEMA_VERSION",
    "RUN_PREFIX",
    "BOOK_SOURCE",
    "CONTRACT_HASH",
    "EXPECTED_CONTRACT_HASH",
    "SNAPSHOT_OFFSETS_S",
    "ANCHORS",
    "ATTACK_CLUSTER_GAP_MS",
    "WORKTREE_ROOT",
]
