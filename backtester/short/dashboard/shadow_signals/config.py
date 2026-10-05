"""ClickHouse connection for shadow signal reads (credentials from collector env)."""

from __future__ import annotations

from research_charts.clickhouse_config import ClickHouseConfig, load_clickhouse_config

SHADOW_CH_DATABASE = "live_forward"

LONG_VIEW = f"{SHADOW_CH_DATABASE}.shadow_signals_long_latest"
SHORT_VIEW = f"{SHADOW_CH_DATABASE}.shadow_signals_short_latest"


def load_shadow_clickhouse_config() -> ClickHouseConfig:
    return load_clickhouse_config()
