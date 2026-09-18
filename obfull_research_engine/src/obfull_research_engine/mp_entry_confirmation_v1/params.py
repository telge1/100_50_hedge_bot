"""Frozen entry-confirmation parameters (frozen before outcomes)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

BATCH_RUN_REL = Path("obfull_research_engine/runs/mp_edge_event_batch_v1_20260916")
ENRICH_RUN_REL = Path("obfull_research_engine/runs/mp_ob_feature_enrichment_v1_20260916")
PRICE_PATH_RUN_REL = Path("obfull_research_engine/runs/mp_price_path_4h_v1_20260916")
DEFAULT_RUN_REL = Path("obfull_research_engine/runs/mp_entry_confirmation_v1_20260916")
WALL_FILTER_REL = PRICE_PATH_RUN_REL / "frozen_wall_persistence_filter_v1.json"

CANDLE_DATABASE = "signal_generator"
CANDLE_TABLE = "candles_1m"
SYMBOL = "BTCUSDT"
EXCHANGE = "bybit"
NS = 1_000_000_000

# Frozen primary parameters (must match contract)
STRUCTURE_LOOKBACK_BARS = 3
CONFIRMATION_TIMEOUT_MINUTES = 120
RETEST_TOLERANCE_PCT = 0.05
TARGET_PCT = 0.41
STOP_PCT = 0.15
MAX_HOLDING_MINUTES = 240
CONFIRMATION_TF_MINUTES = 5
REQUIRED_RECLAIM_CLOSES = 2

# Diagnostic only — never used to retune primary rules
DIAGNOSTIC_PAIRS = (
    (0.41, 0.10),
    (0.41, 0.20),
    (0.41, 0.25),
    (0.50, 0.15),
    (0.50, 0.20),
)
COST_PCT = (0.08, 0.12)
PATH_HORIZONS_MIN = (15, 30, 60, 120, 240)
CLOSE_RETURN_HORIZONS_MIN = (30, 60, 120, 240)

MANUAL_CASE_TRIGGERS_UTC = (
    "2026-09-10T16:44:17",
    "2026-09-06T21:48:26",
    "2026-09-11T07:33:36",
)
PILOT_EXTRA_MAX = 17  # + 3 manual = <=20


@dataclass(frozen=True)
class ConfirmParams:
    batch_run_dir: Path
    enrich_run_dir: Path
    price_path_run_dir: Path
    out_dir: Path
    symbol: str = SYMBOL
    exchange: str = EXCHANGE
    structure_lookback_bars: int = STRUCTURE_LOOKBACK_BARS
    confirmation_timeout_minutes: int = CONFIRMATION_TIMEOUT_MINUTES
    retest_tolerance_pct: float = RETEST_TOLERANCE_PCT
    target_pct: float = TARGET_PCT
    stop_pct: float = STOP_PCT
    max_holding_minutes: int = MAX_HOLDING_MINUTES
    confirmation_tf_minutes: int = CONFIRMATION_TF_MINUTES
    required_reclaim_closes: int = REQUIRED_RECLAIM_CLOSES

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("batch_run_dir", "enrich_run_dir", "price_path_run_dir", "out_dir"):
            d[k] = str(getattr(self, k))
        d["units"] = "percent"
        d["diagnostic_pairs"] = [list(p) for p in DIAGNOSTIC_PAIRS]
        d["cost_pct"] = list(COST_PCT)
        return d
