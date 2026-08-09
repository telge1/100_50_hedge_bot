"""Fixed TP/SL grid on T0 15m-failure confirmation entries (APTUSDT)."""

from __future__ import annotations

AUDIT_VERSION = "fractal_15m_failure_tpsl_v1"
SYMBOL = "APTUSDT"
MIN_SAMPLE = 30
ROUNDTRIP_FEE_PCT = 0.11
MAX_HOLD_MIN = 240

ENTRY_DETAIL = (
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "fractal_15m_failure_confirmation_entry_apt/entry_delay_detail.csv"
)
CONFIRMATION_EVENTS = (
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "fractal_15m_failure_confirmation_entry_apt/confirmation_events.csv"
)

TP_GRID = (0.15, 0.20, 0.25, 0.30, 0.40, 0.50)
SL_GRID = (0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 0.75, 1.00)

FOCUS_COMBOS = (
    (0.15, 0.25),
    (0.20, 0.30),
    (0.25, 0.40),
    (0.30, 0.50),
    (0.40, 0.75),
    (0.50, 1.00),
)

FIRST_TOUCH_LEVELS = (0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50)

ENTRY_DOC = """
T0 entry unchanged from confirmation_entry run:
  entry_bar = first 1m open_time STRICTLY AFTER confirmation_available_at
  entry_price = that bar OPEN
Signal unchanged: FAILED_UP=>SHORT, FAILED_DOWN=>LONG.
"""

METHOD_DOC = """
Fixed TP x SL grid only; no search outside matrix.
Same-bar TP+SL: primary SL_FIRST (conservative); TP_FIRST sensitivity counted.
Fees: 0.11% roundtrip always subtracted from gross exit return.
Max horizon 240m then TIME_EXIT.
APT in-sample research only — not strategy confirmation.
"""

__all__ = ["AUDIT_VERSION", "SYMBOL", "TP_GRID", "SL_GRID"]
