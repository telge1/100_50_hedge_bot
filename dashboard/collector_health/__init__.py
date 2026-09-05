"""Evidence-based collector health + gated OI 5m backfill (CH SoT).

Does not modify live collectors. Public-trades backfill UI stays fail-closed.
"""

from __future__ import annotations

HEALTH_CONTRACT_VERSION = "collector_health_v1"
OI_SOT_TABLE = "open_interest_5m_history"
OI_SOT_DATABASE = "orderbook_analysis"
OI_SOURCE = "BYBIT_REST_5M_HISTORY"
OI_GRANULARITY = "5m"
PUBLIC_TRADES_BACKFILL_GATE = "PUBLIC_TRADES_BACKFILL_PARTIALLY_READY"
PUBLIC_TRADES_UI_BANNER = "DEGRADED — LIVE CURRENT BUT DATA LOSS POSSIBLE"
