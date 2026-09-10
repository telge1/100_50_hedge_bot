from __future__ import annotations
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ENGINE_ROOT / "contracts"
RESULTS = ENGINE_ROOT / "results" / "btc_multi_hour_state_v1"
DATA = RESULTS / "data"
SCHEMA_JSON = CONTRACTS / "mb_state_1s_v1.schema.json"
SCHEMA_SHA = CONTRACTS / "mb_state_1s_v1.sha256"
PILOT_PARQUET = Path(
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/results/"
    "general_market_behavior_research_engine_v1_btc_1h_pilot/btc_state_1s_v1.parquet"
)
COVERAGE_CHECKS = ENGINE_ROOT / "results" / "coverage_checks"
