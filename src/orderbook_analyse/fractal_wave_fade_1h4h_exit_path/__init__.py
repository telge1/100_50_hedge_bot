"""1h/4h Tier-A wave-fade exit/path research (frozen signal, fixed exit variants)."""

from __future__ import annotations

from pathlib import Path

AUDIT_VERSION = "fractal_wave_fade_1h4h_exit_path_v1"
FEE_PCT = 0.11  # full-position roundtrip equivalent
MIN_SAMPLE = 30
VERY_SMALL = 15

EVENTS_PATH = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "fractal_wave_fade_trend_filter_generalization/events_with_trend.csv"
)

SIGNAL_TFS = ("1h", "4h")
SYMBOLS = ("DOGEUSDT", "BTCUSDT")

MAX_HOLD_MIN = {
    "1h": 72 * 60,  # 72h
    "4h": 10 * 24 * 60,  # 10d
}

FAV_LEVELS = (0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0)
ADV_LEVELS = (0.5, 1.0, 1.5, 2.0, 3.0, 4.0)

SINGLE_GRID = {
    "1h": {
        "tp": (1.0, 1.5, 2.0, 3.0, 4.0),
        "sl": (1.0, 1.5, 2.0, 3.0),
    },
    "4h": {
        "tp": (2.0, 3.0, 4.0, 5.0, 6.0, 8.0),
        "sl": (1.5, 2.0, 3.0, 4.0),
    },
}

REFERENCE_SINGLE = {
    "1h": (
        (1.5, 1.0),
        (2.0, 1.0),
        (2.0, 1.5),
        (3.0, 1.5),
        (3.0, 2.0),
        (4.0, 2.0),
    ),
    "4h": (
        (2.0, 1.5),
        (3.0, 1.5),
        (4.0, 2.0),
        (5.0, 2.0),
        (6.0, 2.0),
        (6.0, 3.0),
        (8.0, 3.0),
    ),
}

# Fixed scale-out / runner specs (no optimization)
# legs: list of (weight, tp_pct|None) ; None = no fixed TP (runner to timeout)
# be_after_first_tp: move remaining SL to 0 (entry) after first partial TP fills
SCALEOUT_SPECS = {
    "S1": {
        "1h": {"legs": ((0.50, 1.0), (0.50, 3.0)), "sl": 1.5, "be_after_first_tp": False},
        "4h": {"legs": ((0.50, 2.0), (0.50, 5.0)), "sl": 2.5, "be_after_first_tp": False},
    },
    "S2": {
        "1h": {"legs": ((0.50, 1.0), (0.50, 3.0)), "sl": 1.5, "be_after_first_tp": True},
        "4h": {"legs": ((0.50, 2.0), (0.50, 5.0)), "sl": 2.5, "be_after_first_tp": True},
    },
    "S3": {
        "1h": {
            "legs": ((0.33, 1.0), (0.33, 2.0), (0.34, 4.0)),
            "sl": 1.5,
            "be_after_first_tp": False,
        },
        "4h": {
            "legs": ((0.33, 2.0), (0.33, 4.0), (0.34, 6.0)),
            "sl": 3.0,
            "be_after_first_tp": False,
        },
    },
    "S4": {
        "1h": {
            "legs": ((0.33, 1.0), (0.33, 2.0), (0.34, 4.0)),
            "sl": 1.5,
            "be_after_first_tp": True,
        },
        "4h": {
            "legs": ((0.33, 2.0), (0.33, 4.0), (0.34, 6.0)),
            "sl": 3.0,
            "be_after_first_tp": True,
        },
    },
    "RUNNER": {
        "1h": {"legs": ((0.50, 1.5), (0.50, None)), "sl": 1.5, "be_after_first_tp": True},
        "4h": {"legs": ((0.50, 3.0), (0.50, None)), "sl": 3.0, "be_after_first_tp": True},
    },
}

FEE_SEMANTICS = """
Fee semantics (partial exits):
  Full 100% position roundtrip fee equivalent = 0.11%.
  Each closed weight w pays fee contribution w * 0.11%.
  Trade net = sum_i w_i * (gross_i - 0.11%), with sum w_i = 1 when fully closed.
  No double-counting: entry+exit fees are bundled into the single 0.11% per unit size.
  SL / TP / TIMEOUT / BE exits all use the same per-weight fee debit.
"""

METHOD_DOC = """
Frozen Tier-A wave-end fade (UP->SHORT, DOWN->LONG).
Entry: first tradeable 1m open strictly after confirmation (frozen entry_time).
TFs: 1h (max hold 72h), 4h (max hold 10d).
Symbols: DOGEUSDT, BTCUSDT.
SL_FIRST on same-bar TP/SL ambiguity.
Fixed single-TP grid + fixed scale-out S1-S4 + one RUNNER. No threshold search.
""" + FEE_SEMANTICS

__all__ = [
    "AUDIT_VERSION",
    "FEE_PCT",
    "SIGNAL_TFS",
    "SCALEOUT_SPECS",
    "MAX_HOLD_MIN",
]
