"""Research-only EDC sync tolerance (M0–M5). Does not alter production defaults."""

from .detect_bar_gap import detect_bar_gap_sync, detect_strict_sync_baseline
from .detect_extended import (
    apply_cohesion_filter,
    detect_compressed_rebound_only,
    detect_price_distance_sync,
    detect_touch_and_expand,
)
from .horizon_30m_runner import run_xrp_30m_horizon_research, shortlist_30m_modes
from .mfe_runner import run_xrp_all_tolerance_mfe_mae
from .research_policy import apply_available_source_research, research_policy_document
from .runner import run_sync_tolerance_pilot
from .shortlist_runner import run_xrp_shortlist_with_sources, shortlist_modes
from .xrp_30d_real_tpsl_pnl_runner import run_xrp_30d_real_tpsl_pnl
from .xrp_30d_core_sources_comparison_runner import run_xrp_30d_core_sources_comparison
from .xrp_30d_1h_4h_signal_timeframes_runner import run_xrp_30d_1h_4h_signal_timeframes

__all__ = [
    "detect_bar_gap_sync",
    "detect_strict_sync_baseline",
    "detect_price_distance_sync",
    "detect_touch_and_expand",
    "detect_compressed_rebound_only",
    "apply_cohesion_filter",
    "run_sync_tolerance_pilot",
    "run_xrp_all_tolerance_mfe_mae",
    "run_xrp_shortlist_with_sources",
    "run_xrp_30m_horizon_research",
    "run_xrp_30d_core_sources_comparison",
    "run_xrp_30d_real_tpsl_pnl",
    "run_xrp_30d_1h_4h_signal_timeframes",
    "shortlist_modes",
    "shortlist_30m_modes",
    "apply_available_source_research",
    "research_policy_document",
]
