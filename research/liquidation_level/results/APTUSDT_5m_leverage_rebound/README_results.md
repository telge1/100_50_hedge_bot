# Leverage Rebound Audit Results

## Disclaimer

These liquidation levels are **estimated** by a causal LuxAlgo-style model.
They are **not** real exchange liquidation feeds.

Symbol: `APTUSDT`  
Feather: `/home/telgenbuescher/projects/Signal_Generator_Ralf/data/bybit/futures/APT_USDT_USDT-5m-futures.feather`  
Candles: `52569`  
Period: `2025-12-27 00:00:00+00:00` → `2026-06-27 12:40:00+00:00`  
Split: first 70% in-sample / last 30% out-of-sample by candle index.

Measurement starts at the **open of the next candle** after a strict through-level sweep
(`high > level` and `low < level`). The sweep candle itself is never a trade entry.

## 1–3. Small rebound after 100x / 50x / 25x sweeps?

### 100x
- LONG rebound after lower sweep: h3/0.25% → hit=61.1% | mfe=0.444% | mae=0.535% | n=4493
  vs control MFE diff≈-0.0005% (bootstrap CI [-0.01888487124436803, 0.01929026229217838]; empirical only, not a significance claim)
- SHORT rebound after upper sweep: h3/0.25% → hit=60.5% | mfe=0.436% | mae=0.452% | n=4279
  vs control MFE diff≈-0.0304% (bootstrap CI [-0.05027570264320576, -0.01123574543821838]; empirical only, not a significance claim)

### 50x
- LONG rebound after lower sweep: h3/0.25% → hit=66.6% | mfe=0.528% | mae=0.688% | n=4662
  vs control MFE diff≈0.0573% (bootstrap CI [0.0368880624126518, 0.07689796436976956]; empirical only, not a significance claim)
- SHORT rebound after upper sweep: h3/0.25% → hit=66.4% | mfe=0.512% | mae=0.484% | n=4147
  vs control MFE diff≈0.0009% (bootstrap CI [-0.022107521160339032, 0.02306598302415923]; empirical only, not a significance claim)

### 25x
- LONG rebound after lower sweep: h3/0.25% → hit=71.3% | mfe=0.605% | mae=0.843% | n=4302
  vs control MFE diff≈0.0918% (bootstrap CI [0.06723323116807027, 0.1173363133650182]; empirical only, not a significance claim)
- SHORT rebound after upper sweep: h3/0.25% → hit=68.3% | mfe=0.516% | mae=0.584% | n=3487
  vs control MFE diff≈0.0116% (bootstrap CI [-0.011683686030582563, 0.03327372129603455]; empirical only, not a significance claim)

## 4. Stronger after deeper multi-leverage sweeps?

See `rebound_threshold_summary.csv` groups `combo_*` and `cascade_*`.
Compare mean MFE / hit rates for `100x_only` vs `100x_50x_25x` and cascades
`100x->50x->25x`.

Combination counts (full): `{
  "100x_only": 2324,
  "50x_only": 631,
  "25x_only": 206,
  "100x_50x": 1119,
  "50x_25x": 367,
  "100x_25x": 159,
  "100x_50x_25x": 948
}`

## 5. Sweep + reclaim better than sweep alone?

See `reclaim_summary.csv`. Compare `immediate_reclaim` / `next_candle_reclaim`
vs `no_reclaim` for horizon-3 / 0.25% hit rates and mean MFE.
Also see `rejection_vs_breakthrough.csv`.

## 6. Hit rates for 0.10% / 0.25% / 0.50% at 1/3/6/12 bars

Examples (full sample):

| Group | 0.10%@1 | 0.25%@3 | 0.50%@6 | 0.50%@12 |
|---|---|---|---|---|
| lower_100x | hit=69.2% | mfe=0.268% | mae=0.304% | n=4493 | hit=61.1% | mfe=0.444% | mae=0.535% | n=4493 | hit=46.9% | mfe=0.604% | mae=0.708% | n=4493 | hit=59.4% | mfe=0.801% | mae=0.956% | n=4493 |
| lower_50x | hit=75.1% | mfe=0.331% | mae=0.379% | n=4662 | hit=66.6% | mfe=0.528% | mae=0.688% | n=4662 | hit=52.9% | mfe=0.703% | mae=0.853% | n=4662 | hit=63.8% | mfe=0.902% | mae=1.098% | n=4662 |
| lower_25x | hit=79.5% | mfe=0.400% | mae=0.522% | n=4302 | hit=71.3% | mfe=0.605% | mae=0.843% | n=4302 | hit=57.6% | mfe=0.766% | mae=1.037% | n=4302 | hit=66.1% | mfe=0.949% | mae=1.272% | n=4302 |
| upper_100x | hit=69.6% | mfe=0.251% | mae=0.266% | n=4279 | hit=60.5% | mfe=0.436% | mae=0.452% | n=4279 | hit=47.8% | mfe=0.612% | mae=0.624% | n=4279 | hit=61.3% | mfe=0.880% | mae=0.892% | n=4279 |
| upper_50x | hit=75.3% | mfe=0.301% | mae=0.288% | n=4147 | hit=66.4% | mfe=0.512% | mae=0.484% | n=4147 | hit=53.7% | mfe=0.681% | mae=0.694% | n=4147 | hit=66.7% | mfe=0.974% | mae=0.994% | n=4147 |
| upper_25x | hit=75.4% | mfe=0.310% | mae=0.328% | n=3487 | hit=68.3% | mfe=0.516% | mae=0.584% | n=3487 | hit=56.2% | mfe=0.699% | mae=0.836% | n=3487 | hit=69.6% | mfe=1.031% | mae=1.193% | n=3487 |

## 7. Adverse move size

Mean/median MAE are in `rebound_threshold_summary.csv` alongside MFE.
Also check `rebound_before_adverse_*` columns (rebound touched before adverse).

## 8. Out-of-sample confirmation?

Compare the same keys in `summary_out_of_sample.json`.
Example lower_100x h3/0.25: full=`hit=61.1% | mfe=0.444% | mae=0.535% | n=4493` OOS=`hit=62.6% | mfe=0.436% | mae=0.578% | n=1426`

## 9. Larger than matched controls?

See `control_comparison.csv` (month/hour/range-matched, non-sweep candles, fixed seed).
Bootstrap CIs are descriptive only — **not** formal statistical significance.

## 10. Enough after 0.12% round-trip costs?

Peak MFE is not a realized trade return. Even if mean MFE > 0.12%, MAE and timing
usually erase a naive threshold scalp.

- lower_100x: mean MFE h3=0.444070383542535 vs cost 0.12 → exceeds_cost=True (MFE is peak favorable excursion, not realized net after costs/slippage)
- lower_50x: mean MFE h3=0.5279862271705089 vs cost 0.12 → exceeds_cost=True (MFE is peak favorable excursion, not realized net after costs/slippage)
- lower_25x: mean MFE h3=0.6051086382238421 vs cost 0.12 → exceeds_cost=True (MFE is peak favorable excursion, not realized net after costs/slippage)
- upper_100x: mean MFE h3=0.4363662427598763 vs cost 0.12 → exceeds_cost=True (MFE is peak favorable excursion, not realized net after costs/slippage)
- upper_50x: mean MFE h3=0.5116888103002589 vs cost 0.12 → exceeds_cost=True (MFE is peak favorable excursion, not realized net after costs/slippage)
- upper_25x: mean MFE h3=0.5162936165547981 vs cost 0.12 → exceeds_cost=True (MFE is peak favorable excursion, not realized net after costs/slippage)

## Integration

No scanner / bot / strategy integration from this audit.
