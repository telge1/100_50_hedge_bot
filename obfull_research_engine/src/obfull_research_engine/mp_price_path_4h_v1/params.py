"""Frozen parameters for 4h price-path study."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

BATCH_RUN_REL = Path("obfull_research_engine/runs/mp_edge_event_batch_v1_20260916")
ENRICH_RUN_REL = Path("obfull_research_engine/runs/mp_ob_feature_enrichment_v1_20260916")
DEFAULT_RUN_REL = Path("obfull_research_engine/runs/mp_price_path_4h_v1_20260916")

CANDLE_DATABASE = "signal_generator"
CANDLE_TABLE = "candles_1m"
CANDLE_TIMEFRAME = "1m"
SYMBOL = "BTCUSDT"
EXCHANGE = "bybit"

NS = 1_000_000_000
HORIZONS_MIN = (5, 15, 30, 60, 120, 180, 240)
MFE_TARGETS_PCT = (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 0.75, 1.00)
MAE_STOPS_PCT = (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 0.75, 1.00)
FIRST_HIT_HORIZONS_MIN = (30, 60, 120, 240)
COST_PCT = (0.08, 0.12)
NET_TARGETS_PCT = (0.05, 0.10, 0.20, 0.30)
PILOT_MAX_EVENTS = 20
INITIAL_ADVERSE_BEFORE_MFE = (0.0, 0.05, 0.10, 0.20, 0.30)


@dataclass(frozen=True)
class PathParams:
    batch_run_dir: Path
    enrich_run_dir: Path
    out_dir: Path
    symbol: str = SYMBOL
    exchange: str = EXCHANGE
    candle_database: str = CANDLE_DATABASE
    candle_table: str = CANDLE_TABLE
    candle_timeframe: str = CANDLE_TIMEFRAME
    pilot_max_events: int = PILOT_MAX_EVENTS

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["batch_run_dir"] = str(self.batch_run_dir)
        d["enrich_run_dir"] = str(self.enrich_run_dir)
        d["out_dir"] = str(self.out_dir)
        d["horizons_min"] = list(HORIZONS_MIN)
        d["mfe_targets_pct"] = list(MFE_TARGETS_PCT)
        d["mae_stops_pct"] = list(MAE_STOPS_PCT)
        d["cost_pct"] = list(COST_PCT)
        d["units"] = "percent"
        d["bps_note"] = "100 bps = 1.00 percent; user outputs are percent only"
        return d
