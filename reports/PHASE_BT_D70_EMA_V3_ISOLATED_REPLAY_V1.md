# BT-D70 — EMA V3 Isolated Replay V1

**VERDICT:** `EMA_V3_ISOLATED_REPLAY_PASS`

**Baseline:** BT-D69 `PURE_EMA_FALSE_ALLOW_AUDIT_PASS` (d1bc4eaf)  
**Scope:** research-only `classifier_version` on isolated strategy; production unchanged.

## Classifier selection
- `v1`: frozen BT-D65 (`classification_v1.py`)
- `v3`: BT-D69 `v3_bear_stack_falling_59_200` — full bear stack + falling EMA59/200; **no** `PRICE_BELOW_FALLING_EMA59_200`
- Config default: `pure_ema_confirmation.enabled: false`, `classifier_version: v1`

## Reference V3 regressions
- Mandatory parity: **True** (`reference_case_parity.csv`)
- Blocks (not SHORT_OK): 2026-08-06 00:00, 2026-08-12 08:30, 2026-09-11 12:05
- SHORT_OK controls: 2026-09-09 20:55, 2026-08-05 03:30/07:30, 2026-08-13 10:30
- 2026-09-09 16:30 EMA-only: not SHORT_OK (`FALLING_EMAS_PRICE_RECLAIM`; not P2 SHORT_OK)

## BT-D65 chart20
- V1 recompute match: **True** (20/20)
- V1 vs V3 differ on **5** chart timestamps (expected: fallback-only V1 `SHORT_OK` rows: Aug-5 00:00, 06:00; Aug-6 00:00; Aug-12 08:30)

## Chronological pilots (D67 windows, research READY pause)
- Sep-9 evening: 12 pool candidates; 8 pending created; 1 EMA cancel; 0 fills — **identical V1/V3 counts**
- Sep-11 midday: 1 pool candidate (12:00); 0 pending either classifier
- Sep-11 12:05 EMA SHORT_OK under V3 but **no pool signal** (D68 parity preserved)

See `chronological_replay_comparison.csv`, `blocked_pool_candidates.csv`, `pending_lifecycle_comparison.csv`, `independent_window_audit.csv`.

## Data
- BT-D60 TRP SMA-seed EMA maps; OHLC load spans chart + pilot + independent windows with 720h+ warmup (fixes pilot-only warmup bug on August reference bars).

## Safety
- No BT-D46 / density SSOT / dashboard / live-order changes.
- No production activation.

Artifacts: `results/phase_bt_d70_ema_v3_v1/`
