# PHASE BT-D90 — July Upper Entry Pool Feature Synthesis

**Baseline:** BT-D85 July 25 executed trades · BT-D87 `031ea67e` · BT-D88 `8b80eed3` · BT-D89 `5c9b59fd`  
**Mode:** Read-only synthesis · no strategy, filters, CH writes, cache rebuild, or new walks  
**Date:** 2026-10-28  

## Verdict

**`UPPER_POOL_SYNTHESIS_INCONCLUSIVE`**

All **25** trades join exactly once on `fill_time_utc` with aligned `signal_close_utc`. Measured features show **partial group separation** (shorter overlap tracks, more SL among user picks) but **heavy distribution overlap** and **strong counterexamples**—not enough to propose a candidate filter or claim a single causal SL signature.

---

## 1. Method

- **Inputs only:** D87 pool geometry at signal, D88 price-tracked persistence (July-bundle window), D89 168h overlap + CH `known_at`.
- **Join keys:** `fill_time_utc`; validated `signal_close_utc` across phases.
- **Tool:** `tools/phase_bt_d90_july_upper_pool_synthesis_v1/run_bt_d90.py` (CSV merge + summaries; no CH/D8 recompute).

**Artifacts**

| File | Role |
|------|------|
| `results/phase_bt_d90_july_upper_pool_synthesis_v1/unified_25_trades.csv` | One row per trade, raw units + provenance columns |
| `results/phase_bt_d90_july_upper_pool_synthesis_v1/grouped_feature_summary.json` | Medians/ranges by group |
| `results/phase_bt_d90_july_upper_pool_synthesis_v1/counterexample_cases.json` | Conflicting TP/SL vs feature profiles |

---

## 2. Data quality (D88 vs D89 before interpreting overlap)

| Topic | Rule in this synthesis |
|-------|-------------------------|
| **D88 persistence bars** | Price region tracked from window start (often **2026-07-01** floor) → left-censored vs full 168h. |
| **D89 overlap bars / fraction** | **168h** from signal (May floor); **July bars = exact D8 depth2**; **pre-July = CH upper-region proxy** (not exact depth2). |
| **Comparable fraction** | Use `overlap_fraction_d89_168h_mixed` only with `evidence_quality_coverage`; trades with `window_bars_pre_july_d89 > 0` mix proxy + exact (e.g. **2026-07-01** fill: 1929 pre-July bars). |
| **Origin age** | `ch_known_at_min_hours_d89` is SSOT at signal; **sub-hour for all 25**. Do **not** treat `segment_epoch_min_hours_d89` as full pool lifetime. |
| **D88 ≈ D89 bars (midsummer)** | For July-only windows, D88 qualifying bars match D89 ge2 bar counts (same underlying walk alignment in prior phases). |

Column `missing_or_proxy_notes` on the unified CSV repeats these limits per row.

---

## 3. Unified feature comparison (medians [min – max])

| Feature | User-selected 13 | Other 12 | Overlap note |
|---------|------------------|----------|--------------|
| Exit | **TP 2 / SL 11** | TP 8 / SL 4 | User picks enrich **SL** label, not a pure pool metric |
| Band width % of entry (D87) | 0.027 [0.006 – 0.069] | 0.023 [0.003 – 0.085] | Nearly same medians; user **max** wider |
| Width rank among July bands | 56 [12 – 96] | 48 [4 – 100] | Full rank overlap |
| Unique origins @ depth2 (D87) | 2 [2 – 3] | 2 [2 – 4] | Same |
| Gap to next upper abs (D87) | 0.00027 [0 – 0.0017] | 0.000047 [0 – 0.00045] | User **larger** gaps on average—not “isolated by gap” |
| Overlap bars ge2 (D89 168h mixed) | **10** [1 – 453] | **28** [1 – 682] | Median **2.8×** shorter for user group; ranges touch |
| Overlap fraction (D89 mixed) | **0.50%** [0.05% – 22.5%] | **1.39%** [0.05% – 33.8%] | Same low floor; user lower median |
| Persistence bars price-tracked (D88) | **10** [1 – 453] | **28** [1 – 503] | Mirrors D89 bar story |
| Hours signal − first tracked (D88) | **47.1** [0 – 166.8] | **103.5** [0 – 168] | User tracks start **later** in window (shorter visible history) |
| CH `known_at` min age (h) | 0 [0 – 0.17] | 0 [0 – 0.17] | No group split |
| Fresh/unstable heuristic (D89) | **4** | 3 | Weak tilt to user |
| Sudden appearance (D88) | **4** | 2 | Weak tilt to user |
| Adjacent upper neighbors @50bp (D89) | 2 [1 – 6] | 2 [1 – 4] | Same median |

**Qualitative combinations** that loosely match many (not all) user picks:

1. **Short visible overlap** (few ge2 bars + low fraction) **and/or** **sudden/fresh** heuristics at signal.  
2. **`isolated` structure label** (D87) on many user SLs—but comparison also uses isolated.  
3. **Outcome alignment:** user set is SL-heavy; visual “weak” correlates with **loss label** more than with a unique geometry scalar.

**Not** consistent with user selection: thin band only, large gap to next upper, low origin count, or old `known_at`.

---

## 4. Counterexamples (see JSON for full list)

| Pattern | Example fill (UTC) | Point |
|---------|-------------------|--------|
| **TP + user-flagged + long overlap** | 2026-07-31T03:50:00 | 453 bars, 22.5% fraction, TP |
| **TP + user-flagged + wide band** | 2026-07-24T12:05:00 | width rank 96, TP |
| **TP + not flagged + sudden/1 bar** | 2026-07-23T14:35 / 16:35 | Same “fresh” profile as several user SLs |
| **SL + not flagged + thin band + long overlap** | 2026-07-12T23:05 / 2026-07-15T11:50 | Would look “strong” on overlap, still SL |
| **SL + flagged + long overlap** | 2026-07-09T03:05 / 2026-07-28T05:00 | User weak label despite 43–337 bars |
| **SL + long overlap, not flagged** | 2026-07-07T09:10 | 682 bars, worst SL overlap in cohort |

---

## 5. Answers (A–D)

### A. Do the 13 weak-looking pools share a consistent measurable signature?

**Partially, not consistently.** The strongest common tilt is **shorter in-window depth2 overlap** (median 10 vs 28 bars; lower overlap fraction) and **somewhat more** sudden/fresh heuristics. Band width, origin count, neighbor count, and CH `known_at` age **do not** separate the groups cleanly. Several user picks are **wide bands** or **long overlap** (Jul 28, Jul 31 TP).

### B. Which 2–3 features appear useful for investigating the visual signature?

1. **`overlap_bars_ge2_d89_168h_mixed` / `overlap_fraction_d89_168h_mixed`** (with proxy caveat)—best cross-trade persistence density.  
2. **`hours_signal_minus_first_tracked_d88` + `sudden_appearance_d88`**—how “new” the band looks in the July-centric walk.  
3. **`structure_label_d87` + band context** (isolated vs stacked)—qualitative overlay; numeric width rank alone is weak.

Secondary: D89 **fresh/unstable** flag for case studies, not a standalone rule.

### C. Which successful trades have the same supposedly weak features?

| Fill UTC | TP | Weak-like signals |
|----------|----|-------------------|
| 2026-07-08T04:30:00 | Yes | 3 overlap bars, fresh/unstable, sudden |
| 2026-07-23T14:35:00 | Yes | 1 bar, fresh, sudden |
| 2026-07-23T16:35:00 | Yes | 1 bar, fresh, sudden |
| 2026-07-24T12:05:00 | Yes | User-flagged; wide band; moderate overlap |
| 2026-07-31T03:50:00 | Yes | User-flagged; **longest** overlap in user set |

Also **2026-07-13T02:35** (TP, not user-flagged): only 4 overlap bars, wide band rank 100.

### D. Is evidence sufficient to justify proposing a candidate filter?

**No.** Outcome imbalance in the user subset (11/13 SL), overlapping feature distributions, proxy/pre-July mixing on early-July trades, and minute-scale `known_at` (no 24h persistence signal) leave **high false-positive/false-negative risk** if any single threshold were promoted. Further work would need **blind labeling**, **exact-depth2 windows without proxy**, and **outcome-independent** validation—not filter optimization on these 25 trades.

---

## 6. Hard limits respected

No strategy/filter/TP/SL/EMA changes · no new backtest · no CH writes · no cache rebuild · no dashboard · no push.

---

## 7. Suggested next research (not in scope)

- Restrict overlap metrics to **July-only D8-exact** subsample (23 trades with `window_bars_pre_july_d89 = 0`) and recompare.  
- Export **landscape viewer stills** keyed to unified CSV rows for qualitative ↔ numeric pairing.  
- Separate **visual weakness** from **realized SL** in labeling before any filter discussion.
