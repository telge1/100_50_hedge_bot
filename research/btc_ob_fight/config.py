"""Configuration resolution for BTC OB Fight fact CLI."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import HEURISTIC_CONTRACT_VERSION

DEFAULT_SYMBOL = "BTCUSDT"
DEFAULT_BEFORE_MINUTES = 30
DEFAULT_AFTER_MINUTES = 30
DEFAULT_VA_PCT = 0.70
DEFAULT_TARGET_BINS = 160

# Documented UNFROZEN heuristics (from legacy case study).
WALL_MAX_BPS = 800
WALL_QTY_MEDIAN_MULT = 3.0
WALL_TRADE_MATCH_FRAC = 0.30
WALL_SAMPLE_INTERVAL_SECONDS = 1
LEVEL_TOUCH_BAND_BPS = 5.0
BTCUSDT_TICK_SIZE = 0.1
REPORT_MICRO_EPISODE_SECONDS = 1.0

LEGACY_OB_FALLBACK = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/data/orderbook_raw_shadow/ob200_v3"
)
OA_SRC = Path("/home/telgenbuescher/projects/orderbook_analyse/src")


@dataclass(frozen=True)
class RunConfig:
    symbol: str
    anchor: datetime
    before_minutes: int
    after_minutes: int
    ob_root: Path
    out_root: Path
    read_only: bool = True
    no_overwrite: bool = True

    @property
    def window_start(self) -> datetime:
        return self.anchor - timedelta(minutes=self.before_minutes)

    @property
    def window_end(self) -> datetime:
        return self.anchor + timedelta(minutes=self.after_minutes)

    def heuristic_manifest(self) -> dict:
        return {
            "heuristic_contract_version": HEURISTIC_CONTRACT_VERSION,
            "status": "UNFROZEN_HEURISTIC",
            "wall_max_bps": WALL_MAX_BPS,
            "wall_qty_median_mult": WALL_QTY_MEDIAN_MULT,
            "wall_trade_match_frac": WALL_TRADE_MATCH_FRAC,
            "wall_sample_interval_seconds": WALL_SAMPLE_INTERVAL_SECONDS,
            "level_touch_band_bps": LEVEL_TOUCH_BAND_BPS,
            "btcusdt_tick_size": BTCUSDT_TICK_SIZE,
            "report_micro_episode_seconds": REPORT_MICRO_EPISODE_SECONDS,
        }


def resolve_ob_root(explicit: Path | None = None) -> Path | None:
    if explicit is not None:
        p = explicit.expanduser().resolve()
        return p if p.is_dir() else None
    for key in ("BTC_OB200_SHADOW_ROOT", "OB200_SHADOW_ROOT"):
        val = os.environ.get(key)
        if val:
            p = Path(val).expanduser().resolve()
            if p.is_dir():
                return p
    if LEGACY_OB_FALLBACK.is_dir():
        return LEGACY_OB_FALLBACK
    return None


def utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso_z(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return utc(dt).strftime("%Y-%m-%dT%H:%M:%S.%f").rstrip("0").rstrip(".") + "Z"


def anchor_folder_name(anchor: datetime) -> str:
    a = utc(anchor)
    return a.strftime("%Y%m%dT%H%M%SZ")


def allocate_run_dir(out_root: Path, anchor: datetime) -> Path:
    base = out_root / "btc_ob_fight_cases" / anchor_folder_name(anchor)
    base.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        candidate = base / f"run_{n:03d}"
        if not candidate.exists():
            candidate.mkdir(parents=True, exist_ok=False)
            return candidate
        n += 1
