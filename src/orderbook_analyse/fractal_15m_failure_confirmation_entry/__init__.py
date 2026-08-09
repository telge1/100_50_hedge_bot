"""Post-confirmation entry timing after 15m wave failure (APTUSDT)."""

from __future__ import annotations

AUDIT_VERSION = "fractal_15m_failure_confirmation_entry_v1"
SYMBOL = "APTUSDT"
MIN_SAMPLE = 30
VERY_SMALL = 10
ROUNDTRIP_FEE_PCT = 0.11

FAILURE_EVENTS = (
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "fractal_cycle_phase_failure_apt/failure_events.csv"
)
WAVE_15M = (
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "fractal_cycle_wave_analysis_apt/waves_15m.csv"
)
EARLY_SNAPSHOTS = (
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "fractal_15m_failure_early_detection_apt/intra_wave_snapshots.csv"
)
WAVE_DIR = (
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "fractal_cycle_wave_analysis_apt"
)

ENTRY_DELAYS_MIN = (0, 1, 2, 3, 5, 10, 15, 30)
FORWARD_HORIZONS_MIN = (5, 15, 30, 60, 120, 240)
PULLBACK_BUCKETS = (
    ("0_05", 0.00, 0.05),
    ("05_10", 0.05, 0.10),
    ("10_20", 0.10, 0.20),
    ("20_30", 0.20, 0.30),
    ("gt30", 0.30, float("inf")),
)
FIRST_TOUCH_LEVELS = (0.10, 0.20)

ENTRY_PRICE_DOC = """
ENTRY PRICE SEMANTICS (fixed, causal, primary):
  decision_time = confirmation_available_at + delay_minutes
  confirmation_available_at = 15m wave end_available_at
    (= first time the completed failure is known)
  entry_bar = first 1m candle with open_time (timestamp) STRICTLY GREATER
    than decision_time
  entry_price = that bar's OPEN
  Rationale: do not use the confirmation bar close as executable;
  trade only the next open that appears after the decision clock.
"""

METHOD_DOC = """
Ground truth: exact failure_events.csv episodes (one 15m wave = one episode).
No entry before confirmation_available_at.
FAILED_UP_WAVE => SHORT; FAILED_DOWN_WAVE => LONG.
Directional returns signed so + = profit in expected reversal direction.
No threshold optimization; fixed delays/buckets only.
"""

__all__ = ["AUDIT_VERSION", "SYMBOL", "ENTRY_PRICE_DOC"]
