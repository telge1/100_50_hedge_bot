# BT-D71 — Extended EMA V1 vs V3 Real Pool Replay

**VERDICT:** `EXTENDED_EMA_REPLAY_PASS`

**Baseline:** BT-D70 `EMA_V3_ISOLATED_REPLAY_PASS` (5c18c0cd)

## Question
Does EMA V3 change historical short **orders** and modeled outcomes vs frozen V1,
with identical canonical pool inputs and unchanged Entry/SL/TP/pending rules?

## Windows (bounded)
| Window | Bars | Pool candidates (V1/V3 same) |
|--------|------|------------------------------|
| 2026-08-05 00:00 – 2026-08-06 02:00 | 315 | 36 |
| 2026-08-12 06:00 – 11:00 | 60 | 5 |
| 2026-08-13 08:00 – 13:00 | 60 | 4 |
| Sep-9 evening (D67) | 72 | 12 |
| Sep-11 midday (D67) | 26 | 1 |

OHLC load: 2026-07-04 → 2026-09-11 with BT-D60 TRP warmup. Density: canonical (Aug) + D54/cache (Sep).

## Summary (RESEARCH_READY track)
- **533** verified 5m closes
- **58** closes with valid original pool candidate
- **30** EMA-only classification diffs (V1 vs V3)
- **9** pool-conditional authorize diffs (candidate present, V1 would allow, V3 blocks or vice versa)
- **37** end-of-bar pending open state diffs (chronological divergence)
- **Pending created differs** on Aug pilots (V1 > V3); **Sep pilots identical** (8 vs 8, 0 vs 0)
- **Modeled fills:** V1 **2** (Aug-5 late + Aug-12 morning); V3 **0**

## Mandatory case audit

| Timestamp | V1 EMA | V3 EMA | Pool candidate? | V1 pending impact |
|-----------|--------|--------|-----------------|-------------------|
| 2026-08-06 00:00 | SHORT_OK | BLOCK (UNCERTAIN) | **No** | None — classification-only |
| 2026-08-12 08:30 | SHORT_OK | BLOCK (UNCERTAIN) | **Yes** | V1 creates pending → modeled fill 08:35; V3 blocks |
| 2026-09-11 12:05 | SHORT_OK | BLOCK (UNCERTAIN) | **No** | None — EMA-only (D68: no pool at 12:05) |

## Interpretation
- V3 **does** change real order chronology where V1-only fallback `PRICE_BELOW_FALLING_EMA59_200` coincides with pool signals (August investigations).
- V3 **does not** change Sep-9/Sep-11 pilot pending/fills in this sample (EMA paths align on pool-timed bars).
- V3 is **not** “classification only” overall: fewer pendings and zero modeled fills vs V1 on verified Aug windows.
- **Not** a statistical proof of better trade selection or PnL.

## Pause (fail-closed default)
`DEFAULT_SHORT_PAUSED`: zero pending created on all pilots (pool candidates still observed).
`RESEARCH_READY` runs isolate EMA V1/V3; Aug-12 V1 track ends `SHORT_PAUSED` after modeled SL.

## Coverage
Preflight: start/mid/end sample per window (`canonical_coverage.csv`). Full bar-by-bar replay uses in-memory density cache per pilot. No fabricated candidates on missing snapshots.

Artifacts: `results/phase_bt_d71_ema_comparison_v1/`
