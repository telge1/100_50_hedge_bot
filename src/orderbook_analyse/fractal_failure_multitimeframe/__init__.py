"""Multi-timeframe wave-failure signal audit (APTUSDT)."""

from __future__ import annotations

AUDIT_VERSION = "fractal_failure_multitimeframe_v1"
SYMBOL = "APTUSDT"
MIN_SAMPLE = 30
VERY_SMALL = 10
ROUNDTRIP_FEE_PCT = 0.11

WAVE_DIR = (
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "fractal_cycle_wave_analysis_apt"
)

# Trading TFs + 1m diagnostic-only
TRADING_TFS = ("5m", "15m", "30m", "1h", "4h")
DIAGNOSTIC_TFS = ("1m",)
ALL_TFS = TRADING_TFS + DIAGNOSTIC_TFS

HORIZONS_BY_TF: dict[str, tuple[int, ...]] = {
    "5m": (5, 15, 30, 60, 120),
    "15m": (15, 30, 60, 120, 240),
    "30m": (30, 60, 120, 240, 480),
    "1h": (60, 120, 240, 480, 720),
    "4h": (240, 480, 720, 1440),
    "1m": (5, 15, 30, 60),  # diagnostic only
}

# Fixed ranking horizons — NOT chosen by max in-sample result
MAIN_HORIZON_BY_TF: dict[str, int] = {
    "5m": 30,
    "15m": 60,
    "30m": 120,
    "1h": 240,
    "4h": 720,
    "1m": 15,
}

EDGE_DELAYS_BY_TF: dict[str, tuple[int, ...]] = {
    "5m": (0, 1, 3, 5, 10),
    "15m": (0, 1, 3, 5, 10, 15),
    "30m": (0, 5, 10, 15, 30),
    "1h": (0, 5, 15, 30, 60),
    "4h": (0, 15, 30, 60, 120),
    "1m": (0, 1, 3),
}

FIRST_TOUCH_LEVELS_BASE = (0.10, 0.20, 0.30, 0.50, 0.75, 1.00)
FIRST_TOUCH_EXTRA_HTF = (1.50, 2.00)

FAILURE_DOC = """
Same local failure mask for every TF (no TF-specific thresholds):
  FAILED_UP_WAVE:   direction=UP   and (signed_price_move_pct<=0
                    or directional_efficiency<=0 or inefficient_flag)
  FAILED_DOWN_WAVE: direction=DOWN and (signed<=0 or eff<=0 or inefficient_flag)
Confirmation = end_available_at.
Entry = first 1m open STRICTLY AFTER end_available_at.
FAILED_UP => SHORT (expect DOWN); FAILED_DOWN => LONG (expect UP).
"""

METHOD_DOC = """
Strictly causal; reuse fractal_cycle_phase_failure.events.local_failure_mask.
No CCI/RSI/EMA filters; no protected-level; no threshold optimization.
Forward path on 1m OHLC. Ranking uses fixed main horizons per TF.
APT in-sample research only.
"""

__all__ = ["AUDIT_VERSION", "SYMBOL", "TRADING_TFS", "MAIN_HORIZON_BY_TF"]
