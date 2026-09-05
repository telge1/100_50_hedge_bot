"""Read-only BTCUSDT/DOGEUSDT current multi-source recheck (V1)."""

from __future__ import annotations

FORMAT_VERSION = "btc_doge_current_recheck/v1"
DEFAULT_OUT = "results/btc_doge_current_multisource_recheck_v1"
SYMBOLS = ("BTCUSDT", "DOGEUSDT")
RAW_ROOT = "/home/telgenbuescher/projects/orderbook_analyse/data/orderbook_raw_shadow/ob200_v3"
COLLECTOR_PID = 3946369

__all__ = ["FORMAT_VERSION", "DEFAULT_OUT", "SYMBOLS", "RAW_ROOT", "COLLECTOR_PID"]
