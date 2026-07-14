# Phase B Results — Sweep Analysis Windows

## What Phase B does

For each validated upper 50x immediate-reclaim sweep, Phase B opens causal
analysis windows of the next **3 / 6 / 12** closed 5m candles.

## Sweep is still not an entry

No reversal/breakout classification, momentum entry, TP/SL, fees, or PnL.

## Frozen vs dynamic

- Frozen: Phase-A 5m/15m/30m snapshot at sweep close (never overwritten)
- Dynamic: features as each follow candle closes
- HTF: last fully closed 15m/30m bucket as-of that follow candle's close

## Overlaps

Overlapping windows are kept separately. Diagnostics report concurrent coverage.

## Completeness

Incomplete windows near end-of-data are kept with
`status=INCOMPLETE_END_OF_DATA` and `complete=false`.

## Readiness

phase_b_ready_for_phase_c = **True**

Hash: `b4d4437cba11d26d3772f814793c8f2425f5a5a2634f874b672171fc68cbf7cd`

