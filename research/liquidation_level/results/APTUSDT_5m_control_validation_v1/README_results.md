# Control Validation — Winner `2eab613f172d928e`

Estimated LuxAlgo-style levels — **not** real exchange liquidations.

## Why this test?

The optimizer’s top10 control comparison only used 100 events vs 100 controls and looked weak.
This audit re-tests the **frozen** winner with many matched control runs on the **full** event set.

## How controls were matched

Same sample (IS/OOS), same calendar month, same short direction, similar UTC hour
(cyclic), same ATR%% and volume-ratio quantile buckets, ≥96 candles away from the event,
and enough forward candles. If needed, matching loosens: ±4h → neighbor ATR → neighbor volume.
Matching never uses future returns.

## Event reproduction

- Full / IS / OOS: **2696 / 1824 / 872**
- Expected: 2696 / 1824 / 872
- Mean match rate: **100.0**

## OOS vs matched controls

See `horizon_comparison.csv` and `oos_decision.json`.

Decision status: **confirmed_better_than_matched_control**  
Reasons: []  
Integration recommended: **True**

## Leverage / side

Primary events are **upper / short / 50x immediate reclaim** (optimizer primary metric).
`leverage_comparison.csv` tags co-swept 25x/100x on the same candle (`includes_*`, `mixed_*`).
Lower/long sweeps are not part of this winner universe.

## Seed / matching sensitivity

See `seed_sensitivity.csv` and `matching_sensitivity.csv`.
Unstable flags feed the conservative decision rules.

## Important

No trading-edge claim if results are unstable, weak, or worse than controls.
No scanner/bot integration from this audit.
