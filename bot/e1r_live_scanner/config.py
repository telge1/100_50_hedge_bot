"""Scanner configuration (no strategy parameters beyond frozen V1 windows)."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTEXT_STUDY_V1 = REPO_ROOT / "results/pool_scan/context_study_v1"
FROZEN_ROOT = REPO_ROOT / "frozen_strategies/e1r_cluster3_ema200_v1"
FROZEN_CODE = FROZEN_ROOT / "code"
FROZEN_FREEZE_COMMIT = "adf7dac448e5fcb4202c22a0e4955ee4d43b06a4"

PANE_FROM_DEFAULT = datetime(2026, 2, 10, tzinfo=timezone.utc)
PANE_TO_DEFAULT = datetime(2099, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
E1R_SIM_FROM_DEFAULT = datetime(2026, 4, 1, tzinfo=timezone.utc)
REPORT_FROM_DEFAULT = datetime(2026, 6, 1, tzinfo=timezone.utc)
REPORT_TO_DEFAULT = datetime(2026, 7, 31, 23, 59, 59, tzinfo=timezone.utc)
LIVE_REPORT_TO = datetime(2099, 12, 31, 23, 59, 59, tzinfo=timezone.utc)

PARITY_COINS = ("XRPUSDT", "ADAUSDT", "DOGEUSDT")
POLL_SECONDS_DEFAULT = 45
MAX_CLOSED_15M_LAG_MINUTES_DEFAULT = 25


def ensure_runtime_paths() -> None:
    """Repo + context_study_v1 on sys.path (not frozen code)."""
    for p in (str(REPO_ROOT), str(CONTEXT_STUDY_V1)):
        if p not in sys.path:
            sys.path.insert(0, p)
    from pool_pattern.market import ensure_paths

    ensure_paths()


_CONTEXT_ENTRY15_NAME = "e1r_context_find_short_entry_15m_v1"


def context_entry15_module():
    """Context-study entry scanner (supports live ``load_end``; never frozen copy)."""
    import importlib.util

    if _CONTEXT_ENTRY15_NAME in sys.modules:
        return sys.modules[_CONTEXT_ENTRY15_NAME]
    path = CONTEXT_STUDY_V1 / "find_short_entry_15m_v1.py"
    spec = importlib.util.spec_from_file_location(_CONTEXT_ENTRY15_NAME, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_CONTEXT_ENTRY15_NAME] = mod
    spec.loader.exec_module(mod)
    return mod


@dataclass
class ScannerConfig:
    pane_from: datetime = PANE_FROM_DEFAULT
    pane_to: datetime = PANE_TO_DEFAULT
    e1r_sim_from: datetime = E1R_SIM_FROM_DEFAULT
    report_from: datetime = REPORT_FROM_DEFAULT
    report_to: datetime = REPORT_TO_DEFAULT
    history_weeks: int = 12
    poll_seconds: int = POLL_SECONDS_DEFAULT
    live: bool = False
    max_closed_15m_lag_minutes: int = MAX_CLOSED_15M_LAG_MINUTES_DEFAULT
    log_path: Path = field(
        default_factory=lambda: REPO_ROOT / "bot/e1r_live_scanner/logs/signals.jsonl"
    )

    def utc(self, ts: datetime) -> datetime:
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)

    def effective_data_end(self, now: datetime | None = None) -> datetime:
        if not self.live:
            return self.utc(self.pane_to)
        return self.utc(now or datetime.now(timezone.utc))

    def effective_report_to(self) -> datetime:
        return LIVE_REPORT_TO if self.live else self.utc(self.report_to)

    def research_load_end(self) -> datetime | None:
        """None → ``find_short_entry_15m_v1.LOAD_END`` (reproducible research)."""
        return None if self.live else None
