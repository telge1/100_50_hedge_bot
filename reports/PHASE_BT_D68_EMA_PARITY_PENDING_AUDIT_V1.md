# BT-D68 — EMA Parity & Pending Audit V1

**VERDICT:** `EMA_REPLAY_PARITY_RESOLVED`

## A) Sep-9 16:30
- Pure EMA: **UNCERTAIN** (D65 = D67 = D68 reload).
- **SHORT_OK** in older tables is **P2 / R5–R6**, not BT-D65 EMA.

## B) Sep-11 zero Pending
- One pool candidate at **12:00** only; EMA **UNCERTAIN** → block.
- **12:05** EMA **SHORT_OK** but **no** baseline pool signal → no Pending.

See `results/phase_bt_d68_ema_parity_v1/sep11_zero_pending_explanation.md`.

## Timing
- BT-D67 `unresolved_timing_cases.csv` empty for pilots; runner order: **fill → EMA cancel → D41 review**.

## Artifacts
`results/phase_bt_d68_ema_parity_v1/`