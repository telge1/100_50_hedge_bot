# Multi-Blocker Start-Depth Audit

**Decision: `START_DEPTH_AUDIT_PASS_WITH_WARNINGS`**

## Phase-1 mechanics (baseline)

1. Start trigger: T1 6% projected short-avg distance after neutralization, confirmed on completed 5m close, fill next 5m open (`select_start_by_timing_mode`).
2. Baseline start price: that next-open fill.
3. Pre-refill book: long/short qty+avg from fill-replay pre-signal state.
4. Refill: `refill_short_qty = max(long_qty - short_qty, 0)` then `short_qty == long_qty` via `neutralize_at_price` / `compute_neutralization`.
5. Shared-BE + legacy full-exit: existing CoberturaEngine (`shared_be`, baseline fill flags).
6. Reused: `load_case_universe`, T1 start, neutralize, `run_cobertura`, pnl/capital/same-candle helpers from `run_multi_blocker_forensic_audit`.

Deeper fill model: `open_if_gapped_else_target_on_low_touch` (never fills at candle low).
STRUCTURE_CONFIRMED: **not implemented** (no causal post-baseline reclaim start helper to reuse).

## Answers

1. Baseline reproduced?: **True** (`BASELINE_PARITY_PASS`)
2. Median remaining downside after baseline start: **0.5054162797895387**
3. Baseline start above later low by >2/5/10/15%: **25/25/25/25** of 25
4. Extra recoveries by depth: `{'B2': 1, 'B4': 0, 'B6': 0, 'B8': 1, 'B10': 2, 'B12': 0, 'B15': 0, 'NO_COBERTURA': 0}` (DOT@B10, ETC@B2/B8, OP@B10)
5. Lost baseline recoveries: `{'B2': 2, 'B4': 2, 'B6': 0, 'B8': 2, 'B10': 2, 'B12': 1, 'B15': 2, 'NO_COBERTURA': 2}` (APT/TIA often lost when delayed)
6. Combined-PnL delta sums vs B0: `{'B2': '-152.5579501271742', 'B4': '-333.07537393634664', 'B6': '-355.60289519316007', 'B8': '-509.59325157650153', 'B10': '-569.0354637114217', 'B12': '-757.8745577769029', 'B15': '-984.8137058085998', 'NO_COBERTURA': '-1846.6067606660392'}` — deeper starts worsen aggregate Combined
7. Median max drawdown by variant: `{'B0': '17.28860273940013', 'B2': '7.559301128980064', 'B4': '7.48772670073496', 'B6': '8.904639856470098', 'B8': '8.542497913984981', 'B10': '6.271574183556446', 'B12': '8.316371331237896', 'B15': '6.169951536100086', 'NO_COBERTURA': '92.28169299999999'}` — deeper starts generally lower median DD
8. Deeper targets reached: **25/25** for all B2–B15 within 120d (0 unreached).
9. NO_COBERTURA better than all: **0** trades
10. Classifications: `{'DEEPER_START_IMPROVES_ONLY_DRAWDOWN': 22, 'START_LIKELY_TOO_EARLY': 3}`
11. Robust depth?: best_combined counts `{'B0': 22, 'B10': 2, 'B2': 1}` — **B0 dominates**; no robust fixed deeper depth.
12. Structure follow-up?: weakly justified — fixed deeper starts cut drawdown and unlock a few recoveries, but hurt Combined for most and lose APT/TIA often; any delay rule needs causal OOS validation.

Best combined-pnl variant counts: `{'B0': 22, 'B10': 2, 'B2': 1}`

B0 recovered_120d: **2** open: **23**

| variant | reached | rec120 | open120 | comb_sum | med_dd | extra_rec | lost_rec |
|---|---:|---:|---:|---:|---:|---:|---:|
| B0 | 25 | 2 | 23 | -855.1652490533094 | 17.28860273940013 |  |  |
| B2 | 25 | 1 | 24 | -1007.7231991804836 | 7.559301128980064 | 1 | 2 |
| B4 | 25 | 0 | 25 | -1188.240622989656 | 7.48772670073496 | 0 | 2 |
| B6 | 25 | 2 | 23 | -1210.7681442464695 | 8.904639856470098 | 0 | 0 |
| B8 | 25 | 1 | 24 | -1364.758500629811 | 8.542497913984981 | 1 | 2 |
| B10 | 25 | 2 | 23 | -1424.2007127647312 | 6.271574183556446 | 2 | 2 |
| B12 | 25 | 1 | 24 | -1613.0398068302122 | 8.316371331237896 | 0 | 1 |
| B15 | 25 | 0 | 25 | -1839.9789548619092 | 6.169951536100086 | 0 | 2 |
| NO_COBERTURA | 0 | 0 | 25 | -2701.7720097193487 | 92.28169299999999 | 0 | 2 |

Decision: `START_DEPTH_AUDIT_PASS_WITH_WARNINGS`
