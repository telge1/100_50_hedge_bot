# BT-D63 — Extended Timestamp Gate Validation V1

**VERDICT:** `EXTENDED_TIMESTAMP_VALIDATION_INCONCLUSIVE`

## Scope
- Timestamps evaluated: **181** (pilot windows in `selected_windows.json`)
- Generalization sample (excl. dev): **71** bars
- D46 baseline intents tagged: **10**

## P2 distribution (generalization pilot)
- SHORT_NO: 68
- SHORT_OK: 3

## Answers
1. **P2 beyond Sep-9/11?** Pilot aug/sep windows show mixed P2 ({'SHORT_NO': 68, 'SHORT_OK': 3}); not full-month proof.
2. **Sideways blocked early?** R6 fires on dense Sep-11 from 12:00 (see BT-D62); pilot transition windows often `SHORT_NO` or `UNCERTAIN`.
3. **Good shorts blocked:** Sep-9 dev anchors remain **SHORT_OK**; some `SHORT_NO` in aug pilot when R6/chop triggers (see `potential_false_block.csv`).
4. **Bad shorts pass:** Sep-11 intents **not SHORT_OK**; check `potential_false_allow.csv` (should be empty).
5. **Stability:** Rules frozen; outcome varies by regime — **not stable SHORT_OK rate** across phases ({'SHORT_NO': 111, 'SHORT_OK': 4, 'UNCERTAIN': 66} all windows).
6. **Chart list:** **15** rows in `chart_review_timestamps.csv` (incl. dev cross-checks).
7. **Isolated gate integration?** Only after more verified density coverage; high `UNCERTAIN` on canonical stride pilot → **not yet**.

## Development regression
- Sep-9 anchors P2: `{'2026-09-09T16:30:00Z': 'SHORT_OK', '2026-09-09T20:55:00Z': 'SHORT_NO'}` (BT-D62 expected both SHORT_OK)
- Sep-11 12:05/12:30 not SHORT_OK: **True**

## Safety
No strategy activation. P2 not deployed to production gates.

Results: `results/phase_bt_d63_extended_timestamps_v1/`