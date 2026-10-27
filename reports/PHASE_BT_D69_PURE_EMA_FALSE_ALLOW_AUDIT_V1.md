# BT-D69 — Pure EMA False-Allow Audit V1

**VERDICT:** `PURE_EMA_FALSE_ALLOW_AUDIT_PASS`

## Research question
Does `PRICE_BELOW_FALLING_EMA59_200` permit `SHORT_OK` without a complete bear EMA stack?

**Answer:** Yes — all mandatory suspect cases are `SHORT_OK` on V1 via that fallback; **V3 removes them** without blocking positive controls that already use `BEAR_STACK_FALLING_59_200`.

- Mandatory V1 `SHORT_OK`: **3/3**
- Mandatory V3 `SHORT_OK`: **0/3**
- Control false-blocks V3 only: **0** (V4 blocks Sep-9 20:55 — see audit CSV).

Artifacts: `results/phase_bt_d69_pure_ema_audit_v1/`

User visual judgments are noted in cohort labels only; no outcome-based tuning.