"""Oracle fixture tiers for FAST / MEDIUM / SLOW pytest selection (no production code).

Phase 3B commands (from repo root):

  FAST (~30–60s):
    pytest bot/e1r_live_scanner/tests backtester/short/dashboard/research_charts/tests \\
      -m "not slow and not medium" \\
      --ignore=backtester/short/dashboard/research_charts/tests/test_lld_research_kernel.py \\
      --ignore=backtester/short/dashboard/research_charts/tests/test_lld_overlay_payload.py

  MEDIUM (~1–3 min):
    pytest bot/e1r_live_scanner/tests \\
      backtester/short/dashboard/research_charts/tests/test_short_pool_oracle_v1.py \\
      backtester/short/dashboard/research_charts/tests/test_frozen_pool_kernel_v1.py \\
      backtester/short/dashboard/research_charts/tests/test_frozen_lld_config_parity_v1.py \\
      backtester/short/dashboard/research_charts/tests/test_scanner_prefix_causality.py \\
      -m medium

  FULL (hours — run explicitly):
    pytest bot/e1r_live_scanner/tests \\
      backtester/short/dashboard/research_charts/tests/test_short_pool_oracle_v1.py \\
      backtester/short/dashboard/research_charts/tests/test_frozen_pool_kernel_v1.py \\
      backtester/short/dashboard/research_charts/tests/test_frozen_lld_config_parity_v1.py \\
      backtester/short/dashboard/research_charts/tests/test_scanner_prefix_causality.py \\
      -m slow
"""

from __future__ import annotations

# One real decision per coin — MEDIUM gate (loaded pane + golden parity).
ORACLE_MEDIUM_FIXTURES: tuple[tuple[str, str], ...] = (
    ("XRPUSDT", "2026-06-19T02:00:00+00:00"),
    ("ADAUSDT", "2026-06-24T01:45:00+00:00"),
    ("DOGEUSDT", "2026-06-11T12:00:00+00:00"),
)

ORACLE_MEDIUM_KEYS = frozenset(ORACLE_MEDIUM_FIXTURES)

# Parametrized tests that use (symbol, decision_iso) and should be tiered.
ORACLE_PARAMETRIZED_TEST_NAMES = frozenset(
    {
        "test_short_pool_oracle_deterministic_repeat",
        "test_short_pool_oracle_matches_golden",
        "test_kernel_matches_oracle_golden",
        "test_no_lookahead_suffix_pane",
        "test_frozen_config_available_pools_match_current_short",
        "test_loaded_pane_api_matches_at_decision_api",
        "test_scanner_pools_prefix_invariant",
    }
)
