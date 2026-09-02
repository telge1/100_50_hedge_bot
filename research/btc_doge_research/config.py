"""Bounded Phase-1 pilot configuration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .contracts import parse_utc, validate_symbol, validate_window

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULT_ROOT = REPO_ROOT / "results" / "research_db_phase_1_pilot_v1"
OB200_ROOT = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/data/"
    "orderbook_raw_shadow/ob200_v3"
)


@dataclass(frozen=True)
class PilotWindow:
    pilot_id: str
    symbol: str
    start: datetime
    end: datetime
    reference: str

    def __post_init__(self) -> None:
        validate_symbol(self.symbol)
        validate_window(self.start, self.end)


BTC_WINDOW = PilotWindow(
    pilot_id="btc_run_018",
    symbol="BTCUSDT",
    start=parse_utc("2026-08-31T18:30:00Z"),
    end=parse_utc("2026-08-31T19:30:00Z"),
    reference="results/btc_ob_fight_cases/20260831T190000Z/run_018",
)

DOGE_WINDOW = PilotWindow(
    pilot_id="doge_20260829_1145_1230",
    symbol="DOGEUSDT",
    start=parse_utc("2026-08-29T11:45:00Z"),
    end=parse_utc("2026-08-29T12:30:00Z"),
    reference="results/aggressor_efficiency_data_audit_v1/ABSCHLUSSBERICHT.md",
)

SEAM_WINDOWS = {
    "BTCUSDT": (
        ("2026-08-25T12:00:00Z", "2026-08-25T12:05:00Z"),
        ("2026-08-26T06:30:00Z", "2026-08-26T06:35:00Z"),
        ("2026-08-28T15:00:00Z", "2026-08-28T15:05:00Z"),
    ),
    "DOGEUSDT": (
        ("2026-08-25T12:00:00Z", "2026-08-25T12:05:00Z"),
        ("2026-08-26T06:30:00Z", "2026-08-26T06:35:00Z"),
        ("2026-08-28T15:00:00Z", "2026-08-28T15:05:00Z"),
    ),
}
