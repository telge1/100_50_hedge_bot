# Historical Protected-Level Pull Break-Risk Audit

**Primary decision:** `PULL_IS_DISTANCE_PROXY`

## Definitions (fixed before evaluation)

- Episode entry: distance to active C3.4B protected level ≤ 50 bps (safe side).
- Approach anchors: first times ≤ 50 / 25 / 10 / 5 bps.
- Primary feature anchor: `approach_10bps_ts` (fallback 25→50).
- LEVEL_BREAK: 1m close beyond protected level (structure-aligned).
- LEVEL_HOLD_REJECT: reached ≤25 bps, then away ≥80 bps for ≥30 min without break.
- AMBIGUOUS: no clear outcome within 120 min / day end / level change.
- Wall zone: level ± 8 bps on defensive book side.
- Trade match tolerance: ±750 ms (same as prior audit).
- Primary pull measure: PASSIVE_REMOVAL_EXCESS (% of initial zone not explained by matching aggressor flow).

## Sample counts

- Total approaches: **136**
- LEVEL_BREAK: **106**
- LEVEL_HOLD_REJECT: **21**
- AMBIGUOUS: **9**
- By symbol: `{'APTUSDT': {'LEVEL_HOLD_REJECT': 9, 'LEVEL_BREAK': 60, 'AMBIGUOUS': 8}, 'DOGEUSDT': {'LEVEL_HOLD_REJECT': 12, 'LEVEL_BREAK': 46, 'AMBIGUOUS': 1}}`
- By direction: `{'bearish': {'LEVEL_HOLD_REJECT': 11, 'LEVEL_BREAK': 64, 'AMBIGUOUS': 4}, 'bullish': {'LEVEL_BREAK': 42, 'LEVEL_HOLD_REJECT': 10, 'AMBIGUOUS': 5}}`
- By timeframe: `{'1h': {'LEVEL_HOLD_REJECT': 11, 'LEVEL_BREAK': 65, 'AMBIGUOUS': 5}, '4h': {'LEVEL_BREAK': 41, 'LEVEL_HOLD_REJECT': 10, 'AMBIGUOUS': 4}}`

## Central comparison (BREAK vs HOLD_REJECT)

- Best pull feature (PASSIVE_REMOVAL_EXCESS family): `passive_removal_excess_pct_60s`
- Pull-only AUC: **0.6156783468104223**
- Distance-only AUC: **0.7704402515723271**
- Distance+Pull AUC: **0.7803234501347709** (Δ vs distance-only: 0.009883198562443796)
- Matched controls (nearest hold same symbol/direction/tf + distance/speed): {'n_breaks': 106, 'n_matched': 13, 'median_pull_diff_break_minus_hold': 0.0}
- Robustness: {'bootstrap': {'feature': 'passive_removal_excess_pct_60s', 'ci_low': -0.11905468390579685, 'ci_high': 0.2476915399859204, 'n_boot': 400}, 'jackknife': {'feature': 'passive_removal_excess_pct_60s', 'auc_full': 0.6156783468104223, 'auc_min': 0.6070754716981132, 'auc_max': 0.6304245283018868, 'fragile': False}}
- Pull-start rate: breaks 0.7924528301886793 (84/106); holds 0.6190476190476191 (13/21)

## Timing

- Earliest useful pull separation beyond proximity: **NO_EARLY_SEPARATION**
- Median seconds from approach-anchor to break (breaks only): **120.0**

## Subgroups

- Strongest subgroup (by pull AUC): **bearish**

## Counterexamples (selected)

- `BREAK_STRONG_PULL`: APTUSDT_1h_low_20260512_192700_1p0801 pull=0.9816088395670782 dist=3.240440699933806 (APTUSDT bearish 1h)
- `BREAK_WEAK_PULL`: DOGEUSDT_4h_high_20260220_160200_0p10117 pull=0.0 dist=-9.390135415636836 (DOGEUSDT bullish 4h)
- `HOLD_STRONG_PULL`: DOGEUSDT_4h_high_20260220_191500_0p10117 pull=0.4624378737864146 dist=10.378570722546986 (DOGEUSDT bullish 4h)
- `HOLD_WEAK_PULL`: DOGEUSDT_1h_high_20260228_061800_0p09164 pull=0.0 dist=7.092972501092263 (DOGEUSDT bullish 1h)
- `BREAK_EXTRA`: APTUSDT_4h_low_20260512_192700_1p0801 pull=0.9816088395670782 dist=3.240440699933806 (APTUSDT bearish 4h)
- `BREAK_EXTRA`: APTUSDT_1h_low_20251230_085900_1p712 pull=0.9623959630101027 dist=8.761682242990988 (APTUSDT bearish 1h)
- `BREAK_EXTRA`: APTUSDT_4h_low_20260118_234600_1p7639 pull=0.928489943468387 dist=-1.9842394693580843 (APTUSDT bearish 4h)
- `HOLD_EXTRA`: APTUSDT_1h_high_20251230_193700_1p72 pull=0.4409666836271238 dist=8.720930232557178 (APTUSDT bullish 1h)

## Answers to required questions

1. Approaches total: **136** (1h+4h protected high/low on the 10 OB days).
2. LEVEL_BREAK: **106**.
3. LEVEL_HOLD_REJECT: **21** (AMBIGUOUS=9, excluded from main comparison).
4. Passive wall-removal is modestly higher before breaks (pull AUC≈0.6156783468104223), but holds also show pull frequently — not a clean separator.
5. Under distance control / matched holds: median pull diff ≈ 0.0; Distance+Pull does not beat Distance-only (Δ=0.009883198562443796).
6. Incremental value vs distance-only: **no** (Distance-only 0.7704402515723271 ≥ Distance+Pull 0.7803234501347709).
7. Earliest pull-beyond-proximity signal: **NO_EARLY_SEPARATION**.
8. Strongest subgroup by pull AUC: **bearish** (see subgroup_statistics.csv for bearish/bullish, APT/DOGE, 1h/4h).
9. Counterexamples: strong-pull holds and weak-pull breaks both exist (see list above).
10. The ~54s pre-break pull from the prior deep-dive is **not** a standalone early break warning: it is largely shared approach behavior / proximity confounded → **PULL_IS_DISTANCE_PROXY**.

## Boundary

No live gate, no production threshold, no bot/scanner/ML changes, no new day downloads.

