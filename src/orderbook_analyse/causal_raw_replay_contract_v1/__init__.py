"""CAUSAL_RAW_REPLAY_CONTRACT_V1 — kausaler Raw-OB-Replay für Forschung."""

from __future__ import annotations

FORMAT_VERSION = "causal_raw_replay_contract/v1"
CONTRACT_VERSION = "RAW_REPLAY_FEATURE_CONTRACT_V1"
DEFAULT_OUT = "results/causal_raw_replay_contract_v1"
SYMBOLS = ("BTCUSDT", "DOGEUSDT")
RAW_ROOT = "/home/telgenbuescher/projects/orderbook_analyse/data/orderbook_raw_shadow/ob200_v3"
COLLECTOR_PID = 3946369
SEED = "causal_v1"

# Numeric tolerance for prefix-invariance (float feature fields).
FP_TOL = 1e-9

__all__ = [
    "COLLECTOR_PID",
    "CONTRACT_VERSION",
    "DEFAULT_OUT",
    "FORMAT_VERSION",
    "FP_TOL",
    "RAW_ROOT",
    "SEED",
    "SYMBOLS",
]
