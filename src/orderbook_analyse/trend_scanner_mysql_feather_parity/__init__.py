"""APT MySQL vs Feather trend-scanner parity smoke (read-only).

Does not change scanner rules. Uses existing aggregate_ohlcv_from_5m,
run_c34b_structure / run_structure_for_timeframe, enumerate_structure_breaks.
"""

from __future__ import annotations

AUDIT_VERSION = "trend_scanner_mysql_feather_parity_apt_v1"

__all__ = ["AUDIT_VERSION"]
