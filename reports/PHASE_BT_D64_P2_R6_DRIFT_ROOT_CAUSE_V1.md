# BT-D64 — P2/R6 Drift Root Cause V1

**VERDICT:** `P2_DRIFT_ROOT_CAUSE_CONFIRMED`

## A. Sep-9 20:55 UTC
D63 pool_evolution prior_entry chained across 16:00-22:00 window; D61/D62 use per-reference-window prior (NO_PRIOR at 20:55)

- **BT-D62:** P2 SHORT_OK — `origin_ids_added=False`, R6 false (regime UNCERTAIN).
- **BT-D63:** P2 SHORT_NO — `origin_ids_added=True`, R6 true (SIDEWAYS).
- **First diverging input:** `origin_ids_added` (pool prior chain), not EMA.

## B. Parity references
See `reference_decision_diff.csv` for 16:30, 20:55, 12:00, 12:05, 12:30.

## C. UNCERTAIN breakdown (D63)
- missing_upper_candidate_proxy: 66

## D. SHORT_OK generalization (3 bars)
Distinct timestamps: ['2026-08-12T14:00:00Z', '2026-08-12T14:30:00Z', '2026-08-12T15:00:00Z'] — not a win rate.

## F. Repair
Documented in `recommended_repair.md` (prior_entry handling only).

Artifacts: `results/phase_bt_d64_p2_drift_v1/`