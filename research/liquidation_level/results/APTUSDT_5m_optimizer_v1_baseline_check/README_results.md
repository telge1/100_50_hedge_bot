# Liquidation Level Optimizer Results

Estimated LuxAlgo-style levels — **not** real exchange liquidations.
This run ranks **path context** (further squeeze, then drop), not a trading edge.

## How many combinations were checked?

- Completed configurations: **1**
- Eligible for ranking: **1**
- Failed: **0**
- Cache hits / misses: **0 / 0**

## Baseline vs winners

Baseline (`b77cc1b59653fe68`):
- full events (50x immediate reclaim h50): **2106**
- IS adverse median: **1.4312001635657268**
- IS peak-drop median: **2.3728483905474995**
- IS peak-before-trough: **53.558844256518675**

Best robust (IS-ranked, OOS-confirmed if possible): `b77cc1b59653fe68`
- OOS status: **confirmed**
- full_n: **2106**
- IS adverse / peak-drop / PBT: **1.4312001635657268** / **2.3728483905474995** / **53.558844256518675**

Best sensitive-but-stable: `b77cc1b59653fe68` · OOS **confirmed** · full_n **2106**

## Did more sensitive settings help?

Compare `baseline_comparison.csv` and the three ranking CSVs.
Often more sensitive volume/volatility settings **raise event counts** but also raise noise
(sideways / breakout). Prefer configs that stay eligible, keep OOS confirmation, and do not
inflate p95 adverse without a matching peak-drop structure.

## Recommendation

Use `recommended_configs/best_robust_path.json` for further research path context.
Keep `recommended_configs/baseline.json` as the reproducibility anchor.
See also `refinement_grid.json` for Stage-B (not auto-run).

No scanner/bot/live integration.
