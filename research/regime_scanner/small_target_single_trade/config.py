"""Frozen small-target exit matrix (no free search)."""

from __future__ import annotations

from typing import Any

STRATEGY_A6 = "a6_short"
STRATEGY_STP = "stp_b2e1"

TP_VALUES = (0.25, 0.50, 0.75, 1.00)
SL_VALUES = (0.50, 0.75, 1.00)  # magnitudes; applied as negative for short adverse
HORIZONS = (24, 48, 96, 192)
PRIMARY_HORIZON = 192
EFFECTIVE_COSTS = (0.20, 0.25, 0.30)
BASE_COST = 0.20

SAME_BAR_POLICY = "conservative_sl"
A6_PARENT_LABEL = "multicoin_a6_signal_store_20260722"
A6_OUTCOME_VERSION = "tp3_sl2_h192_cost020_v1"

EXIT_COMBOS: tuple[tuple[float, float], ...] = tuple(
    (tp, sl) for tp in TP_VALUES for sl in SL_VALUES
)


def exit_combo_id(tp: float, sl: float) -> str:
    return f"tp{tp:.2f}_sl{sl:.2f}".replace(".", "p")


def is_micro_target(tp: float) -> bool:
    return abs(float(tp) - 0.25) < 1e-12


def matrix_rows() -> list[dict[str, Any]]:
    rows = []
    for tp, sl in EXIT_COMBOS:
        rows.append(
            {
                "tp_pct": tp,
                "sl_pct": -float(sl),  # signed adverse for short path helpers
                "sl_magnitude_pct": float(sl),
                "combo_id": exit_combo_id(tp, sl),
                "micro_target_diagnostic": is_micro_target(tp),
            }
        )
    return rows
