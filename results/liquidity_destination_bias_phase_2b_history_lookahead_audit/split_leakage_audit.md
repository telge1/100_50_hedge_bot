# Split Leakage Audit — Phase 2 Baselines

**Generated (UTC):** 2026-09-05T13:59:26Z  
**Dataset fingerprint:** `9f7d2bd2fd1aff9315b268c70f818020ba91f3c2f5992f3dcd15aec550ad77e8`  
**Eval contract hash:** `185691763f9b11f0602c832c99a6b5210642684d10a3f61360236dab42f85a0c`

## Verdict: PASS

No episode appears in multiple splits. No random mixing. No whole-dataset scaling. Train-only majority. Test outcomes unused for rule selection.

## Frozen temporal splits (by `t0_utc`)

| Split | Start inclusive | End exclusive | Episodes | Notes |
|---|---|---|---:|---|
| TRAIN | 2026-08-25T00:00Z | 2026-08-29T00:00Z | 1074 | BTC 553 / DOGE 521 |
| VALIDATION | 2026-08-29T00:00Z | 2026-08-30T00:00Z | 209 | not used to pick live baseline |
| TEST | 2026-08-30T00:00Z | end of Phase-1D | 436 | BTC 220 / DOGE 216 |

Evidence: `evaluation_contract_frozen_v1.yaml`, `baselines/splits.py` `assert_no_split_leak`, Phase-2 ABSCHLUSSBERICHT.

## Confirmed controls

| Control | Status | Evidence |
|---|---|---|
| Unique `episode_id` across splits | PASS | `assert_no_split_leak` |
| No split overlap / boundary violations | PASS | exclusive end bounds |
| No random split | PASS | calendar UTC only |
| Train-only global majority (B0) | PASS | `majority_label(train_rows)` |
| Symbol-specific majority (B4) from TRAIN only | PASS | `by_symbol` from train |
| Hash control uses fingerprint + episode_id only | PASS | `hash_control` |
| Nearest target uses T0 distances only | PASS | `distance_*_bps` frozen at T0 |
| No global normalization / scaler fit on all rows | PASS | no scaler in baselines package |
| Validation not used for live baseline selection | PASS | Phase-2 report + contract forbidden list |
| Builder/contract/dataset unchanged by Phase 2 | PASS | Phase-2 safety block |

## Consumed benchmark status (mandatory)

```text
2026-08-30 / 2026-08-31 = consumed_benchmark_test
```

These days were inspected via Phase-2 TEST metrics (Nearest Acc ~0.791, directional ~0.854).  
They remain valid for **reproducible baseline comparisons** against frozen Phase-1D bytes.  
They are **not** an untouched final holdout for later orderflow/bias models. A new forward reservation is required after feature/model freeze (see `history_sufficiency_policy.yaml`).

## Leakage status summary

| Risk | Status |
|---|---|
| Split episode leak | CLEAR |
| Test-label training | CLEAR |
| Threshold tuning on TEST | CLEAR (no threshold optimization in Phase 2) |
| Consumed holdout for future models | **FLAGGED** — reserve new forward days |
