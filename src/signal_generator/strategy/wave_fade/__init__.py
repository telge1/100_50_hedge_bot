"""Frozen Fractal Wave Fade + BE50 strategy core (logic-preserving port)."""

from __future__ import annotations

from signal_generator.strategy.wave_fade.adapter import (
    bars_to_ohlcv_df,
    ensure_confirmation_before_entry,
    one_minute_books,
    records_to_ohlcv_df,
)
from signal_generator.strategy.wave_fade.annotation import annotate_waves_df
from signal_generator.strategy.wave_fade.be50 import (
    prepare_book,
    simulate_be50_trade,
    simulate_be50_trade_fast,
    trade_levels,
)
from signal_generator.strategy.wave_fade.clusters import build_same_side_clusters, pair_window
from signal_generator.strategy.wave_fade.edges import (
    compute_frozen_eff_edges_from_waves,
    frozen_eff_edges_all_signal_tfs,
    load_frozen_eff_edges,
)
from signal_generator.strategy.wave_fade.exits import (
    dir_ret,
    hold_end_i,
    scan_exit_sl_first,
)
from signal_generator.strategy.wave_fade.indicators import (
    attach_indicators,
    stochastic_rsi,
    wilder_rsi,
)
from signal_generator.strategy.wave_fade.parameters import (
    APT_IS_END,
    BASELINE_LABEL,
    BE_FRAC,
    SIGNAL_TFS,
    SOURCE_COMMIT,
    TPSL_BY_TF,
)
from signal_generator.strategy.wave_fade.selection import (
    event_sort_key,
    first_cluster_entry_signal_ids,
    prepare_signal_events,
)
from signal_generator.strategy.wave_fade.signals import (
    build_symbol_signals,
    build_waves_from_ohlcv,
    resolve_entries,
)
from signal_generator.strategy.wave_fade.tpsl import apply_upgrade_plan, tpsl_for_tf
from signal_generator.strategy.wave_fade.trend import assign_trend_bucket
from signal_generator.strategy.wave_fade.waves import segment_stoch_waves

__all__ = [
    "APT_IS_END",
    "BASELINE_LABEL",
    "BE_FRAC",
    "SIGNAL_TFS",
    "SOURCE_COMMIT",
    "TPSL_BY_TF",
    "annotate_waves_df",
    "apply_upgrade_plan",
    "assign_trend_bucket",
    "attach_indicators",
    "bars_to_ohlcv_df",
    "build_same_side_clusters",
    "build_symbol_signals",
    "build_waves_from_ohlcv",
    "compute_frozen_eff_edges_from_waves",
    "dir_ret",
    "ensure_confirmation_before_entry",
    "event_sort_key",
    "first_cluster_entry_signal_ids",
    "frozen_eff_edges_all_signal_tfs",
    "hold_end_i",
    "load_frozen_eff_edges",
    "one_minute_books",
    "pair_window",
    "prepare_book",
    "prepare_signal_events",
    "records_to_ohlcv_df",
    "resolve_entries",
    "scan_exit_sl_first",
    "segment_stoch_waves",
    "simulate_be50_trade",
    "simulate_be50_trade_fast",
    "stochastic_rsi",
    "tpsl_for_tf",
    "trade_levels",
    "wilder_rsi",
]
