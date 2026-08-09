"""Multi-TF wave-end fade confluence research (MySQL SoT)."""

from __future__ import annotations

from pathlib import Path

AUDIT_VERSION = "fractal_signal_confluence_db_v1"
FEE_PCT = 0.11
MIN_SAMPLE = 30
VERY_SMALL = 15

ENV_FILE = Path(
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/"
    "research/regime_scanner/.env.regime_db"
)

SYMBOLS = ("DOGEUSDT", "BTCUSDT")
SIGNAL_TFS = ("15m", "30m", "1h", "4h")
TF_RANK = {"15m": 0, "30m": 1, "1h": 2, "4h": 3}
TF_BAR_MIN = {"15m": 15, "30m": 30, "1h": 60, "4h": 240}

APT_IS_END = "2026-08-08T10:21:00+00:00"

# Fixed pair windows (minutes) — a priori, not optimized
PAIR_WINDOW_MIN: dict[tuple[str, str], int] = {
    ("15m", "30m"): 30,
    ("30m", "1h"): 60,
    ("1h", "4h"): 240,
    ("15m", "1h"): 60,
    ("15m", "4h"): 240,
    ("30m", "4h"): 240,
}

OUTCOME_HORIZONS = (30, 60, 120, 240, 480, 720, 1440)

# Exit targets keyed by TF orientation
TPSL_BY_TF = {
    "15m": (1.0, 1.0),
    "30m": (2.0, 1.5),
    "1h": (2.0, 1.5),
    "4h": (4.0, 2.0),
}
TPSL_EXTRA_4H = (6.0, 3.0)

MAX_HOLD_BY_TF = {
    "15m": 12 * 60,
    "30m": 24 * 60,
    "1h": 72 * 60,
    "4h": 10 * 24 * 60,
}

PRIMARY_HORIZON_BY_HIGHEST = {
    "15m": 60,
    "30m": 120,
    "1h": 240,
    "4h": 720,
}

DEFINITIONS_DOC = """
Multi-TF confluence (a priori, frozen defs)

Signal (each TF): UP wave end → SHORT; DOWN → LONG.
confirmation = end_available_at; T0 = first 1m open strictly after.
Tier A / Q4 / EMA trend: same frozen H4 + APT-IS efficiency quartiles
(recomputed from APTUSDT market_candles, no CSV inputs).

Confluence pair windows (max |Δt| between confirmation times):
  15m↔30m: 30m
  30m↔1h / 15m↔1h: 60m
  1h↔4h / 15m↔4h / 30m↔4h: 240m
Plus sensitivity: ±1 bar of the larger TF (max of pair window and that).

Same-side signals linked if any pair within window → cluster (union-find).
Opposite-side signals within window → CONFLICT (not merged into same-side cluster).

Entry interpretations (fixed): FIRST_SIGNAL / HIGHEST_TF / LAST_SIGNAL.
Exit variants for clusters: highest-TF TPSL vs first-signal-TF TPSL.
Fees 0.11%; SL_FIRST.
"""
