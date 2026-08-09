"""Early causal detection of later-confirmed 15m wave failures (APTUSDT)."""

from __future__ import annotations

AUDIT_VERSION = "fractal_15m_failure_early_detection_v1"
SYMBOL = "APTUSDT"
MIN_SAMPLE = 30
VERY_SMALL = 10
ROUNDTRIP_FEE_PCT = 0.11
WEAK_PRICE_ABS = 0.02  # same fixed "kaum" as prior work; not optimized
MIN_ABS_STOCH = 1e-9

WAVE_DIR = (
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "fractal_cycle_wave_analysis_apt"
)
FAILURE_EVENTS = (
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "fractal_cycle_phase_failure_apt/failure_events.csv"
)

# Snapshot offsets after wave start_available_at (minutes)
SNAPSHOT_OFFSETS_MIN = (3, 5, 8, 10, 12, 15)
FORWARD_HORIZONS_MIN = (5, 15, 30, 60, 120)
LEAD_BUCKETS = (
    ("0_3", 0.0, 3.0),
    ("3_5", 3.0, 5.0),
    ("5_8", 5.0, 8.0),
    ("8_10", 8.0, 10.0),
    ("gt10", 10.0, float("inf")),
)
PERSIST_BUCKETS = (
    ("1", 1, 1),
    ("2_3", 2, 3),
    ("4_5", 4, 5),
    ("ge6", 6, 10_000),
)

METHOD_DOC = """
Ground truth: exact failure_events.csv labels (FAILED_UP/DOWN) — labels only.
t0 = 15m wave start_available_at (causal wave start known).
Snapshots at t0+{3,5,8,10,12,15}m using last 1m bar with available_at<=t.
15m Stoch/RSI/EMA estimated on completed 15m history + forming bar from 1m.
partial_directional_efficiency = signed_partial_price / |partial_stoch_delta|
  (same semantics as completed-wave directional_efficiency).
EARLY_*_FAILURE_CANDIDATE: stoch still moves with wave direction AND
  (price fails to follow OR partial_eff<=0). RSI/1m/5m are overlays only.
No threshold search.
"""

__all__ = ["AUDIT_VERSION", "SYMBOL"]
