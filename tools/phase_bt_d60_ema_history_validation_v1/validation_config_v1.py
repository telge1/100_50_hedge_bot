"""BT-D60 thresholds and probe windows (read-only research)."""

from __future__ import annotations

# A) Pool landscape context (documented; not validated in this OHLC phase)
POOL_CONTEXT_WARMUP_HOURS = 168

# B) OHLC history before first evaluated decision close
WARMUP_HOURS_CANDIDATES = (72, 168, 336, 720)
REFERENCE_WARMUP_HOURS = 720

SYMBOL = "DOGEUSDT"

# Convergence: compare candidate warmups vs REFERENCE_WARMUP_HOURS at probe closes.
# abs_tol: float noise floor; rel_tol: fraction of |reference EMA| (price-scale aware).
EMA200_CONVERGENCE_ABS_TOL = 1e-8
EMA200_CONVERGENCE_REL_TOL = 1e-5
EMA59_CONVERGENCE_ABS_TOL = 1e-8
EMA59_CONVERGENCE_REL_TOL = 1e-5

# Slopes (EMA59/200) need period-1 valid samples + lookback bars (TRP SMA seed).
SLOPE_LOOKBACKS = (6, 12, 24)
MIN_BARS_BEFORE_SLOPE = {p: (p - 1) + max(SLOPE_LOOKBACKS) for p in (59, 200)}

# First decision closes per BT-D59 reference windows (5m bar timestamps = close time).
PROBE_DECISION_CLOSES = [
    "2026-09-04T12:00:00Z",
    "2026-09-05T12:00:00Z",
    "2026-09-09T16:30:00Z",
    "2026-09-09T17:25:00Z",
    "2026-09-09T20:55:00Z",
    "2026-09-09T21:25:00Z",
    "2026-09-11T12:00:00Z",
    "2026-09-11T12:25:00Z",
    "2026-09-11T12:35:00Z",
    "2026-09-11T15:30:00Z",
    "2026-09-11T17:00:00Z",
]

FIRST_DECISION_CLOSE = min(PROBE_DECISION_CLOSES)
