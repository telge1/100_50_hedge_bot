# Historical Post-Break Acceptance vs Reclaim Audit

**Primary decision:** `POST_BREAK_SIGNAL_IS_PRICE_ONLY`

## Scope

- Events: existing `selected_deep_dive_events.csv` (15 important 1h/4h protected breaks).
- No new event definition, no new days, no live gate, no ML.
- Features causal at each cutoff; outcomes may use future path.

## Sample

- n=15 BREAK_ACCEPTED=9 RECLAIM=6 AMBIGUOUS=0

## Cutoff results (Accepted vs Reclaim)

- **+5s:** best_price_auc=0.8518518518518519 best_ob_auc=0.8645833333333334 best_flow_auc=0.6666666666666666 dist_only=0.8518518518518519 dist+ob+flow=1.0
- **+10s:** best_price_auc=0.8518518518518519 best_ob_auc=0.8425925925925926 best_flow_auc=0.8333333333333334 dist_only=0.8518518518518519 dist+ob+flow=0.8981481481481481
- **+20s:** best_price_auc=0.9166666666666666 best_ob_auc=0.8981481481481481 best_flow_auc=0.9166666666666666 dist_only=0.8703703703703703 dist+ob+flow=0.9351851851851852
- **+30s:** best_price_auc=0.9166666666666666 best_ob_auc=0.8148148148148148 best_flow_auc=0.9166666666666666 dist_only=0.7777777777777778 dist+ob+flow=0.9074074074074074
- +60/+120 confirmation: see combined_results.csv

## Strongest features (at earliest primary cutoff of interest)

- Price: `distance_beyond_level_bps` AUC=0.8518518518518519
- OB: `near_depth_imbalance` AUC=0.8425925925925926
- Flow: `fraction_volume_beyond_level` AUC=0.8333333333333334
- Distance control @ focus: {'cutoff': 5, 'auc_distance_only': 0.8518518518518519, 'n_distance_only': 15, 'auc_ob_only': 0.9166666666666666, 'n_ob_only': 14, 'auc_flow_only': 0.4375, 'n_flow_only': 14, 'auc_distance_plus_ob': 0.90625, 'n_distance_plus_ob': 14, 'auc_distance_plus_flow': 0.8854166666666666, 'n_distance_plus_flow': 14, 'auc_distance_plus_ob_flow': 1.0, 'n_distance_plus_ob_flow': 13, 'auc_ob_plus_flow': 0.9761904761904762, 'n_ob_plus_flow': 13}

## Timing

- EARLIEST_USEFUL_TIME: **BREAK_PLUS_5S**
- Practical for later Block/Allow: **EARLY (≤30s) — potentially actionable later**

**Caveat:** n=15 (Accepted/Reclaim). Combo AUCs near 1.0 on this sample are not treated as robust OB+flow lift; primary decision prefers price-only when distance already separates strongly.

## Robustness

- Jackknife: {'feature': 'distance_beyond_level_bps', 'cutoff': 10, 'auc_full': 0.8518518518518519, 'auc_min': 0.8222222222222222, 'auc_max': 0.9333333333333333, 'fragile': False, 'n_events': 15}
- Bootstrap: {'feature': 'distance_beyond_level_bps', 'cutoff': 10, 'auc_full': 0.8518518518518519, 'ci_low': 0.58, 'ci_high': 1.0, 'n_boot': 400}
- Strongest subgroup: bullish
- Weakest/instable subgroup: APTUSDT

## Counterexamples / deep dives

- `ACCEPTED_STRONG_DIST`: DOGEUSDT_PROTECTED_LOW_BREAK_bearish_20260106_0p14909_1h outcome=BREAK_ACCEPTED dist5=225.36722784895042 flip5=None flow5=-4192504.5219500978
- `ACCEPTED_WEAK_DIST`: APTUSDT_PROTECTED_HIGH_BREAK_bullish_20260118_1p8316_1h outcome=BREAK_ACCEPTED dist5=3.5488097837970165 flip5=7.266764169957907 flow5=0.0
- `RECLAIM_STRONG_DIST_FALSE`: APTUSDT_PROTECTED_LOW_BREAK_bearish_20260512_1p0801_4h outcome=RECLAIM dist5=16.202203499677253 flip5=99.0 flow5=-5501.287044999994
- `RECLAIM_WEAK_DIST`: APTUSDT_PROTECTED_LOW_BREAK_bearish_20251229_1p589_4h outcome=RECLAIM dist5=-582.1271239773445 flip5=0.0 flow5=376.0952
- `DOGE_FEB28_LOW`: DOGEUSDT_PROTECTED_LOW_BREAK_bearish_20260228_0p09133_4h outcome=RECLAIM dist5=4.9271871236165 flip5=17.60903295150034 flow5=54739.57077000011
- `DOGE_JAN06_LOW`: DOGEUSDT_PROTECTED_LOW_BREAK_bearish_20260106_0p14909_1h outcome=BREAK_ACCEPTED dist5=225.36722784895042 flip5=None flow5=-4192504.5219500978
- `DOGE_FEB28_HIGH`: DOGEUSDT_PROTECTED_HIGH_BREAK_bullish_20260228_0p09259_1h outcome=RECLAIM dist5=5.940166324657036 flip5=71.42667101047945 flow5=56072.54232000001
- `DOGE_FEB20_LOW`: DOGEUSDT_PROTECTED_LOW_BREAK_bearish_20260220_0p09777_1h outcome=BREAK_ACCEPTED dist5=14.830725171321085 flip5=99.0 flow5=13582.427220000012
- `APT_MAY12`: APTUSDT_PROTECTED_LOW_BREAK_bearish_20260512_1p0801_4h outcome=RECLAIM dist5=16.202203499677253 flip5=99.0 flow5=-5501.287044999994
- `APT_DEC30`: APTUSDT_PROTECTED_HIGH_BREAK_bullish_20251230_1p72_1h outcome=BREAK_ACCEPTED dist5=8.72093023255847 flip5=99.0 flow5=509.07411

## Required answers

1. Analyzable Accepted/Reclaim: 9 / 6 (ambiguous=0).
2. +5s: best_price_auc=0.8518518518518519 best_ob_auc=0.8645833333333334 best_flow_auc=0.6666666666666666 dist_only=0.8518518518518519 dist+ob+flow=1.0
3. +10s: best_price_auc=0.8518518518518519 best_ob_auc=0.8425925925925926 best_flow_auc=0.8333333333333334 dist_only=0.8518518518518519 dist+ob+flow=0.8981481481481481
4. +20s: best_price_auc=0.9166666666666666 best_ob_auc=0.8981481481481481 best_flow_auc=0.9166666666666666 dist_only=0.8703703703703703 dist+ob+flow=0.9351851851851852
5. +30s: best_price_auc=0.9166666666666666 best_ob_auc=0.8148148148148148 best_flow_auc=0.9166666666666666 dist_only=0.7777777777777778 dist+ob+flow=0.9074074074074074
6. Strongest price feature: distance_beyond_level_bps (AUC=0.8518518518518519).
7. Strongest OB feature: near_depth_imbalance (AUC=0.8425925925925926).
8. Strongest flow feature: fraction_volume_beyond_level (AUC=0.8333333333333334).
9. OB/Flow value beyond distance: see Distance control Δ (dist+ob+flow − dist_only).
10. Refill / S/R-flip: see OB features `gross_refill`, `flip_depth_ratio`, `near_depth_imbalance`.
11. EARLIEST_USEFUL_TIME: BREAK_PLUS_5S.
12. Practical earliness: EARLY (≤30s) — potentially actionable later.
13. Counterexamples: see list above + deep_dive_timelines.csv.

## Boundary

STOP — no gate, bot, scanner, threshold, ML, or new downloads.

