# PHASE BT-D87 — User-Selected Weak Upper Pool Forensic Analysis

**Baseline:** BT-D85 `JULY_MONTH_RESEARCH_PASS` · commit `434fbbd8`  
**Mode:** Read-only analysis (no strategy, cache, or filter changes)  
**Date:** 2026-10-28  

## Verdict

**`WEAK_UPPER_POOL_ANALYSIS_PASS`**

All 13 user fill timestamps resolved to ledger rows; signal decision closes loaded from the validated July D8 bundle with **zero** resolution issues. Pool bands matched at causal `signal_close_utc` via ledger `pool_key`.

---

## Scope and data sources

| Item | Path |
|------|------|
| Ledger (25 trades) | `results/phase_bt_d85_july_2026_no_pause_research_v1/trade_ledger_no_pause.json` |
| Density cache | `results/phase_bt_d85_july_2026_no_pause_research_v1/cache_bundle/` |
| Contract | DOGEUSDT · 5m · PD1 · depth2 · `exact_unique_origin` |
| Analysis tool | `tools/phase_bt_d87_weak_upper_pool_analysis_v1/run_bt_d87.py` |
| Outputs | `results/phase_bt_d87_weak_upper_pool_analysis_v1/` |

**Signal resolution:** For all 25 trades, `fill_time_utc − signal_close_utc = 5.0` minutes. Ledger `signal_close_utc` equals the bundle `decision_close_utc` used for landscape reads (verified; no alternate close required).

**Unavailable at decision close (documented in `unresolved_source_fields.json`):** pool age, persistence duration, origin first-seen time, lifetime notional, SSOT depth2 overlap score, chart pixel width. Only band fields present in BT-D8 snapshots were used.

---

## Groups

| Group | N | TP | SL | Note |
|-------|---|----|----|------|
| User-selected (visual “weak/small”) | 13 | 2 | 11 | Labels independent of this study |
| Other July executed trades | 12 | 8 | 4 | Not pre-filtered for “strong” or wins |

User selection correlates with **SL-heavy** outcomes (11/13) but the comparison set includes **four SL** trades and **two of the thinnest bands in the whole month** (not user-selected).

---

## Selected 13 — pool state at signal close (summary)

Full rows: `selected_13_pool_properties.csv`.

| Fill UTC | Signal UTC | Entry | Band [lower, upper] | Width % entry | Origins | Exit | Width rank (July 25) |
|----------|------------|-------|---------------------|---------------|---------|------|----------------------|
| 2026-07-08T07:20:00Z | 07:15:00Z | 0.07220 | [0.07220, 0.072222] | 0.0305 | 2 | SL | 64% |
| 2026-07-09T03:05:00Z | 03:00:00Z | 0.07218 | [0.07218, 0.072208] | 0.0388 | 2 | SL | 80% |
| 2026-07-12T01:15:00Z | 01:10:00Z | 0.07279 | [0.07279, 0.072834] | 0.0604 | 2 | SL | 88% |
| 2026-07-12T07:00:00Z | 06:55:00Z | 0.07285 | [0.07285, 0.072870] | 0.0275 | 2 | SL | 56% |
| 2026-07-13T04:25:00Z | 04:20:00Z | 0.07216 | [0.07216, 0.072168] | 0.0111 | 2 | SL | 20% |
| 2026-07-17T09:20:00Z | 09:15:00Z | 0.071578 | [0.071578, 0.071582] | **0.0056** | 2 | SL | **12%** |
| 2026-07-24T00:55:00Z | 00:50:00Z | 0.06916 | [0.06916, 0.069166] | 0.0087 | 2 | SL | 16% |
| 2026-07-24T12:05:00Z | 12:00:00Z | 0.06946 | [0.06946, 0.069508] | 0.0691 | 2 | **TP** | 96% |
| 2026-07-24T17:15:00Z | 17:10:00Z | 0.06888 | [0.06888, 0.068902] | 0.0319 | 3 | SL | 68% |
| 2026-07-27T15:10:00Z | 15:05:00Z | 0.07148 | [0.07148, 0.071492] | 0.0168 | 2 | SL | 40% |
| 2026-07-28T05:00:00Z | 04:55:00Z | 0.07008 | [0.07008, 0.070096] | 0.0228 | 2 | SL | 48% |
| 2026-07-31T03:50:00Z | 03:45:00Z | 0.07014 | [0.07014, 0.07016] | 0.0285 | 2 | **TP** | 60% |
| 2026-07-31T11:00:00Z | 10:55:00Z | 0.06951 | [0.06951, 0.06952] | 0.0144 | 3 | SL | 36% |

Width rank = percentile among all 25 trade entry bands (higher = wider vs other July entries).

---

## Group comparison (same metrics)

From `pool_property_comparison.json` (medians / means):

| Metric | Selected 13 (median) | Other 12 (median) | Observation |
|--------|----------------------|---------------------|-------------|
| Band width % of entry | 0.0275 | 0.0232 | Selected **not** systematically narrower; mean slightly lower on selected but overlapping ranges |
| Band width abs | 2.0e-5 | 1.7e-5 | Similar tick-scale widths (~0.002–0.006% typical) |
| Unique origin count | 2 | 2 | Almost all at **depth2 floor (2)**; selected max 3 vs comparison max **4** |
| Active upper bands @ signal | 313 | 317 | Same landscape density |
| Gap to next upper (abs) | 0.00027 | 0.000047 | Selected somewhat **larger gaps** above (more “lonely” on chart) |
| Stacked/near neighbors | 0 | 0 | Median 0 both groups; selected mean slightly higher |
| Locals within 50 bp of entry | 1 | 1 | Same local crowding |
| Shared origins w/ next upper | 0 | 0 | Median 0; occasional high shared counts in both groups |

**Not user-selected but objectively thinner:** e.g. 2026-07-15 fill (width **0.00271%**, rank **4%**) and 2026-07-12T23:05 (width **0.00275%**, rank **8%**) — both in the comparison set.

**User-selected but wide:** 2026-07-24T12:05 (**TP**, width rank **96%**); 2026-07-12T01:15 (width rank **88%**).

---

## Visual mini-pools — technical drivers (descriptive)

What the cache supports:

1. **Price band thickness:** Many selected pools sit in the **lower half** of July width percentiles (12–48% ranks for seven fills), consistent with **short vertical extent** on a price chart. Not universal (two selected fills are wide-band ranks 60–96%).

2. **Minimum origin depth:** **12/13** selected bands have exactly **2** unique origins (the PD1 depth2 minimum). Visually, depth2 bands cannot appear “origin-heavy”; thin appearance often equals **tight price span** rather than low origin count relative to other July trades (comparison also mostly 2 origins).

3. **Isolation / spacing:** Several selected bands show **large `gap_to_next_upper_lower_abs`** (e.g. 0.00074–0.00170 on 2026-07-12T01:15, 2026-07-27T15:10) vs many comparison trades with **zero gap** (touching next upper). That can read as a **small lone strip** with empty space above on the chart.

4. **Stacking / overlap:** Four selected trades are `stacked_cluster` (touching or overlapping neighbors, e.g. 2026-07-24T00:55, 2026-07-24T17:15, 2026-07-31 fills). Stacking does **not** exclude visual “small” appearance — local clusters of **narrow** bands can still look like mini-zones.

5. **Outcome overlap:** User picks skew **SL (11/13)**; comparison **TP (8/12)**. That is a **post-hoc correlation** with labels, not a pool metric in the cache. Two user-selected **TP** trades prove weak appearance ≠ SL.

**Not supported from this snapshot:** persistence/age, notional strength, or a standalone depth2 overlap score.

---

## Conclusions (factual)

1. **Common among the 13 selected:** Predominantly **depth2-minimum (2 origins)**, **sub-0.04% entry-relative widths** for most (not all), and **SL-heavy realized outcomes** in July replay. Several show **wider spacing to the next upper band** than the median comparison trade.

2. **Difference from the other 12:** **No clean separation** on width % or origin count alone — distributions overlap. Comparison includes **narrower** non-selected bands. Selected set has **more SL outcomes** by user choice pattern, not by construction of the comparison set.

3. **Most informative measurable features (from available fields):** **Band width as % of entry** (and absolute tick width), **width rank among July entry bands**, and **gap to the next active upper band**. Origin count is mostly flat at 2 and rarely discriminates.

4. **Inconclusive without extra data:** Whether **visual** weakness matches **objective** narrowness (counterexamples both ways); any **age/persistence** story; whether **intrabar/chart rendering** amplifies certain bands; causal link between pool shape and SL rate.

5. **Additional historical data that would help (read-only follow-up, not requested here):** Origin **lifetime segments** before decision close, time series of band merge/split, and chart-scale render metadata — none are in the BT-D8 snapshot used here.

**No performance or filter recommendation** — observation only.

---

## Safety

- No strategy, EMA, entry, SL, TP, pool filter, or cache rebuild changes  
- No ClickHouse writes  
- No new monthly backtest  
- No git push  

---

## Commits

| Artifact | Note |
|----------|------|
| `tools/phase_bt_d87_weak_upper_pool_analysis_v1/run_bt_d87.py` | Read-only analyzer |
| `reports/PHASE_BT_D87_USER_SELECTED_WEAK_UPPER_POOL_ANALYSIS_V1.md` | This report |
| `results/phase_bt_d87_weak_upper_pool_analysis_v1/*` | CSV + JSON deliverables |
