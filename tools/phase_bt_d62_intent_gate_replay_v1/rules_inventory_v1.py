"""R1–R6 definitions copied from BT-D61 (do not redefine thresholds here)."""

from __future__ import annotations

RULES_INVENTORY_MD = """# R1–R6 inventory (from BT-D61, UNVALIDATED)

Source: `tools/phase_bt_d61_regime_gegenprobe_audit_v1/ablation_v1.py`,
`regime_labels_v1.py`, `results/phase_bt_d61_regime_audit_v1/proposed_rules.md`.

## R1 — EMA structure (Variant A)
- `ema9_lt_20_lt_59_lt_200` at decision close.

## R2 — Slopes (Variant B)
- R1 AND `ema59_slope_12b_pct < 0` AND `ema200_slope_12b_pct < 0`.

## R3 — Direction + chop (Variant C)
- R2 AND `net_move_24b_pct < 0` AND `chop_ratio_24b < 8.0`.

## R4 — Upper isolation (Variant D)
- R3 AND `gap_to_next_upper_pct >= 0.5`
- AND `upper_bands_between_entry_and_next == 0`
- AND `local_stack_density_0p35pct <= 2`.
- **Requires verified pool snapshot** (missing pool → UNCERTAIN for R4/R5).

## R5 — No stack growth (Variant E)
- R4 AND NOT (`local_stack_density_0p35pct >= 3` OR `origin_ids_added`).

## R6 — Sideways composite
- `research_regime == SIDEWAYS_CANDIDATE` when score >= 2 among:
  - chop_ratio_24b >= 6.0
  - |net_move_24b_pct| < 0.35
  - EMA59 flattening (slope 6b > 12b) AND ema200_slope_12b < 0
  - local_stack_density >= 3 (pool required)
  - origin_ids_added (pool required)

## EMA data contract (BT-D60)
- TRP SMA-seed EMA9/20/59/200; `ema_validity` must be `VALIDATED_TRP_D60`.

## Research uncertainty
- Thresholds are not OOS-validated.
- Sep-4/5 pool density may be partial (D59 stride cache only).
- R6 can veto R1–R5 in precedence experiments (BT-D62 only).
"""
