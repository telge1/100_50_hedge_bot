"""Frozen LLD pool engine + selection policy v1 (Short-scanner reference snapshot).

This module does NOT replace ``ui_lld_config`` / workspace settings in production yet.
It exists for tests and future migration (Phase 2+).

POOL ENGINE / SELECTION (frozen, affects ``scanner_pools_for_index`` → ``available``):
  - Pane anchor: PANE_FROM_UTC, HISTORY_WEEKS
  - LLD engine: enabled, amount, highest_len, lowest_len
  - TF amount scaling + ``select_lld_pools_for_chart`` policy constants (imported from
    ``service`` today so behavior stays byte-identical to the live Short path)

DISPLAY ONLY (not in this module — remain UI / indicator_settings.json):
  - pool/support/resistance colors, border widths, transparency
  - cluster_gap_pct, minimum_cluster_pools, show_cluster_labels, etc. (overlay composition)
  - ema_fast/slow styling on LLD pane

Pool compute window (future contract, documented in frozen_pool_kernel_spec_v1.md):
  candles loaded from PANE_FROM through decision bar close — independent of chart zoom.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .service import (
    LLD_AMOUNT_MULTIPLIER_BY_TF,
    LLD_DENSE_LOOKBACK_DAYS,
    LLD_DENSE_MIN_LIFE_HOURS,
    LLD_DENSE_SHORT_KEEP_FRAC,
    LLD_OLDER_MIN_LIFE_HOURS,
    MAX_LLD_AMOUNT,
    MAX_LLD_CHART_POOLS,
    lld_config_for_timeframe,
    scaled_lld_amount,
)
from .trp_import import load_trp

FROZEN_LLD_CONFIG_VERSION = 1

# Matches bot/e1r_live_scanner/config.PANE_FROM_DEFAULT and find_short_entry_15m_v1.
PANE_FROM_UTC = datetime(2026, 2, 10, tzinfo=timezone.utc)
HISTORY_WEEKS = 12
SCANNER_ENTRY_TIMEFRAME = "15m"

# Engine fields from indicator_settings.json liquidity_location (pools+ob workspace SoT at freeze).
LLD_ENGINE_V1: dict[str, Any] = {
    "enabled": True,
    "amount": 2000,
    "highest_len": 4,
    "lowest_len": 4,
}

# Selection policy (service.py) — re-exported for spec/tests; logic still in select_lld_pools_for_chart.
LLD_SELECTION_POLICY_V1 = {
    "amount_multiplier_by_tf": dict(LLD_AMOUNT_MULTIPLIER_BY_TF),
    "dense_lookback_days": LLD_DENSE_LOOKBACK_DAYS,
    "dense_min_life_hours": LLD_DENSE_MIN_LIFE_HOURS,
    "dense_short_keep_frac": LLD_DENSE_SHORT_KEEP_FRAC,
    "older_min_life_hours": LLD_OLDER_MIN_LIFE_HOURS,
    "max_chart_pools": MAX_LLD_CHART_POOLS,
    "max_lld_amount": MAX_LLD_AMOUNT,
}


def frozen_lld_engine_config_v1():
    """TRP LiquidityLocationConfig from frozen engine fields (pre-TF scaling)."""
    trp = load_trp()
    return trp["LiquidityLocationConfig"].from_dict(dict(LLD_ENGINE_V1))


def frozen_lld_config_for_timeframe_v1(timeframe: str):
    """Same shape as ``ui_lld_config(timeframe)`` when workspace matches ``LLD_ENGINE_V1``."""
    return lld_config_for_timeframe(frozen_lld_engine_config_v1(), str(timeframe))


def frozen_scaled_amount_v1(timeframe: str) -> int:
    return scaled_lld_amount(int(LLD_ENGINE_V1["amount"]), str(timeframe))


def frozen_lld_config_snapshot_v1(timeframe: str) -> dict[str, Any]:
    """Serializable config for oracle fixtures (engine + effective amount)."""
    cfg = frozen_lld_config_for_timeframe_v1(timeframe)
    return {
        "version": FROZEN_LLD_CONFIG_VERSION,
        "timeframe": str(timeframe),
        "enabled": bool(cfg.enabled),
        "amount_base": int(LLD_ENGINE_V1["amount"]),
        "amount_effective": int(cfg.amount),
        "highest_len": int(cfg.highest_len),
        "lowest_len": int(cfg.lowest_len),
        "selection_policy": dict(LLD_SELECTION_POLICY_V1),
    }
