"""Phase-2 shared MP/LLD event materialization — serialize only, no new math."""

from __future__ import annotations

CONTRACT_NAME = "MARKET_PROFILE_LLD_SHARED_EVENT_MATERIALIZATION_V1"
CONTRACT_VERSION = "1.0.0"
MP_SOURCE_NAME = "dashboard/market_profile_v1"
MP_SOURCE_VERSION = "market_profile_v1_dual_tpo_volume_v1"
LLD_SOURCE_NAME = "indicators.liquidity_location.engine/run_liquidity_location"
LLD_SOURCE_VERSION = "liquidity_pool_signal/canonical_v1"

VISIBLE_LEVEL_TYPES = ("TPO_POC", "TPO_VAH", "TPO_VAL")
CONTEXT_ONLY_LEVEL_TYPES = (
    "PROFILE_HIGH",
    "PROFILE_LOW",
    "VOLUME_VPOC",
    "VOLUME_VAH",
    "VOLUME_VAL",
)
PROFILE_STATES = ("CLOSED", "DEVELOPING", "INVALID")
ZONE_TYPES = ("LLD_RESISTANCE_ZONE", "LLD_SUPPORT_ZONE")
ZONE_STATUSES = ("ACTIVE", "INVALIDATED")
FORBIDDEN_ZONE_LABELS = (
    "SHORT_LIQUIDATION_POOL",
    "LONG_LIQUIDATION_POOL",
    "LIQUIDATED",
    "CONSUMED",
    "PARTIALLY_CONSUMED",
)
FORBIDDEN_EVENT_FIELDS = (
    "mfe",
    "mae",
    "pnl",
    "entry_price",
    "exit_price",
    "trade_result",
    "notional_usdt",
    "liquidation_notional",
)

PILOT_CASES = (
    {
        "case_id": "ex1_30m_developing",
        "symbol": "BTCUSDT",
        "request_as_of": "2026-09-06T22:25:00Z",
        "timeframe": "30m",
        "profile_state": "DEVELOPING",
        "expected_tpo": {"poc": 79759.0, "vah": 79914.0, "val": 79694.0},
    },
    {
        "case_id": "ex2_1h_closed",
        "symbol": "BTCUSDT",
        "request_as_of": "2026-09-06T22:00:00Z",
        "timeframe": "1h",
        "profile_state": "CLOSED",
        "expected_tpo": {"poc": 79911.0, "vah": 79972.0, "val": 79866.0},
    },
    {
        "case_id": "ex3_4h_developing",
        "symbol": "BTCUSDT",
        "request_as_of": "2026-09-06T22:25:00Z",
        "timeframe": "4h",
        "profile_state": "DEVELOPING",
        "expected_tpo": {"poc": 79856.25, "vah": 79972.5, "val": 79785.0},
    },
)

from .runner import run_pilot  # noqa: E402

__all__ = [
    "CONTRACT_NAME",
    "CONTRACT_VERSION",
    "PILOT_CASES",
    "run_pilot",
]
