"""Long V1 live scanner configuration (strategy params only from frozen package)."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTEXT_STUDY_V1 = REPO_ROOT / "results/pool_scan/context_study_v1"
LONG_FROZEN_ROOT = REPO_ROOT / "frozen_strategies/long_geometry_ladder24_be100_v1"
FROZEN_TAG_COMMIT = "0a4e44836fb28d02b697fbd0d9a83dff05f7d223"

RUNTIME_DIR = REPO_ROOT / "bot/long_v1_live_scanner/runtime"
LOG_DIR = REPO_ROOT / "bot/long_v1_live_scanner/logs"

POLL_SECONDS_DEFAULT = 45
MAX_CLOSED_15M_LAG_MINUTES_DEFAULT = 25
MAX_CLOSED_1M_LAG_MINUTES_DEFAULT = 5

PARITY_COINS = ("DOGEUSDT", "XRPUSDT", "APTUSDT")


def ensure_runtime_paths() -> None:
    for p in (str(REPO_ROOT), str(CONTEXT_STUDY_V1)):
        if p not in sys.path:
            sys.path.insert(0, p)
    from pool_pattern.market import ensure_paths

    ensure_paths()


_CONTEXT_ENTRY15_NAME = "long_v1_context_find_short_entry_15m_v1"


def context_entry15_module():
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


def frozen_config():
    from frozen_strategies.long_geometry_ladder24_be100_v1 import config as FC

    return FC


@dataclass
class ScannerConfig:
    poll_seconds: int = POLL_SECONDS_DEFAULT
    live: bool = True
    max_closed_15m_lag_minutes: int = MAX_CLOSED_15M_LAG_MINUTES_DEFAULT
    max_closed_1m_lag_minutes: int = MAX_CLOSED_1M_LAG_MINUTES_DEFAULT
    log_path: Path = field(default_factory=lambda: LOG_DIR / "signals.jsonl")
    state_path: Path = field(default_factory=lambda: RUNTIME_DIR / "state.json")
    dry_run_marker_path: Path = field(default_factory=lambda: RUNTIME_DIR / "dry_run_start.txt")

    # Research / parity windows
    pane_from: datetime = field(default_factory=lambda: frozen_config().PANE_FROM)
    pane_to: datetime = field(default_factory=lambda: datetime(2099, 12, 31, 23, 59, 59, tzinfo=timezone.utc))
    report_from: datetime = field(default_factory=lambda: datetime(2026, 6, 1, tzinfo=timezone.utc))
    report_to: datetime = field(default_factory=lambda: datetime(2026, 7, 31, 23, 59, 59, tzinfo=timezone.utc))

    def utc(self, ts: datetime) -> datetime:
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)

    def effective_data_end(self, now: datetime | None = None) -> datetime:
        if not self.live:
            return self.utc(self.report_to)
        return self.utc(now or datetime.now(timezone.utc))
