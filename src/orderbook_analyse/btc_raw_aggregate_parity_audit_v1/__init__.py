"""Read-only BTC raw OB vs aggregate parity root-cause audit (V1)."""

from __future__ import annotations

FORMAT_VERSION = "btc_raw_aggregate_parity_root_cause/v1"
DEFAULT_OUT = "results/btc_raw_aggregate_parity_root_cause_v1"
RAW_ROOT = "/home/telgenbuescher/projects/orderbook_analyse/data/orderbook_raw_shadow/ob200_v3"
COLLECTOR_PID = 3946369
SEED = 2026082512

__all__ = ["FORMAT_VERSION", "DEFAULT_OUT", "RAW_ROOT", "COLLECTOR_PID", "SEED"]
