"""Full historical import scope (Apr–Sep 2026, four strategy combos)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

CATALOG_ID = "doge_apr_sep_2026_v1"
PIPELINE_LINE_PREFIX = "historical_mixed_frozen_doge_apr_sep_2026_v1"

# Pilot week already imported under a separate ledger line (Phase 3D).
PILOT_PIPELINE_LINE_ID = "historical_mixed_frozen_doge_15m_pd10_aug2026_pilot_v1"
PILOT_COMBO_FOLDER = "15m_PD10"
PILOT_MONTH = "2026-08"
PILOT_DECISION_CLOSE_STEMS: tuple[str, ...] = (
    "20260801T000000Z",
    "20260801T164500Z",
    "20260802T093000Z",
    "20260803T021500Z",
    "20260803T190000Z",
    "20260804T114500Z",
    "20260805T043000Z",
    "20260805T211500Z",
    "20260806T140000Z",
    "20260807T064500Z",
)

AUTHORIZED_FROZEN_INPUT_IDS: frozenset[str] = frozenset(
    {
        "84c22453fc6e2b5cc2791ab2301bdc600a9804632088a8e144b5007941e52c93",
        "cfae2571806c823407767e4d81da82fe984a928ce29843c9f061fc66d79c115b",
        "fe549309ef047ea092bd1fa3eba5c16fae647db7f3fad95dc567b47c66ebcb66",
    }
)


@dataclass(frozen=True)
class StrategyCombo:
    timeframe: str
    pool_detail: int

    @property
    def folder(self) -> str:
        return f"{self.timeframe}_PD{self.pool_detail}"

    def pipeline_line_id(self) -> str:
        slug = self.folder.lower().replace("_", "")
        return f"{PIPELINE_LINE_PREFIX}_{slug}_v1"


STRATEGY_COMBOS: tuple[StrategyCombo, ...] = (
    StrategyCombo("15m", 1),
    StrategyCombo("15m", 5),
    StrategyCombo("15m", 10),
    StrategyCombo("4h", 10),
)

# Derived from pilot write (~3s for 10 closes) and catalog size; conservative for CH + readback.
DEFAULT_BATCH_SIZE_CLOSES = 50
MAX_BATCH_SIZE_CLOSES = 200

MONTHS_APR_SEP: tuple[str, ...] = (
    "2026-04",
    "2026-05",
    "2026-06",
    "2026-07",
    "2026-08",
    "2026-09",
)


def combo_for_folder(folder: str) -> StrategyCombo | None:
    for c in STRATEGY_COMBOS:
        if c.folder == folder:
            return c
    return None


def all_combo_folders() -> Sequence[str]:
    return [c.folder for c in STRATEGY_COMBOS]
