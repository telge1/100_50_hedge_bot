# BT-D61 — Regime Gegenprobe Rule Audit V1

**VERDICT:** `REGIME_RULE_RESEARCH_PASS`

## A. Provenance
- Gegenprobe rows: **340** (SHA verified vs BT-D60A)
- EMA path: **TRP SMA-seed** (`ema_validity=VALIDATED_TRP_D60` on all rows: 340/340)`
- Pool: D54 (Sep-9), D9 cache (Sep-11); Sep-4/5 pool from D59 stride cache where available else EMA-only (`UNCERTAIN_NO_DENSITY_AT_CLOSE`)

## B. Regime distribution
- DOWNTREND_CANDIDATE: 1
- SIDEWAYS_CANDIDATE: 213
- UNCERTAIN: 126

## Answers
1. **Sep-9 vs Sep-11:** Bearish EMA stack overlaps; Sep-11 separates on **stack density / origin growth / chop** (dense window SIDEWAYS labels: 10/13 bars). Sep-9 DOWNTREND labels: 0/19.
2. **Early sideways prevention:** Variant E blocks Sep-11 intent bars only if stack/chop warnings fire **before** 12:05/12:30 — see `false_block_audit.csv` (not full prevention on EMA-only).
3. **Pool evolution:** 78 bars pass C but fail D — isolation/gap filters add signal beyond EMA.
4. **Independent episodes:** 75 grouped segments; ~67 non-development evidence classes. **No OOS holdout.**
5. **Rule candidates:** `results/phase_bt_d61_regime_audit_v1/proposed_rules.md` (R1–R6, UNVALIDATED).

## Reference coverage
- Sep-9 16:30 / 20:55: **in dataset**
- Sep-11 12:25 / 12:35: **in dataset**
- Sep-4–5 transition: **289 bars** (full 5m in Gegenprobe, not D59 stride)

## Safety
No strategy activation. `regime_classifier_v1` unchanged.

## Outputs
- `results/phase_bt_d61_regime_audit_v1/`