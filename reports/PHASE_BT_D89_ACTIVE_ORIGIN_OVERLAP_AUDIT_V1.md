# PHASE BT-D89 — Exact ACTIVE Origin Overlap Forensics

**Baseline:** BT-D87 `031ea67e` · BT-D88 `8b80eed3`  
**Mode:** Read-only · no strategy, filter, CH write, or cache rebuild  
**Date:** 2026-10-28  

## Verdict

**`ACTIVE_ORIGIN_OVERLAP_AUDIT_PARTIAL`**

ClickHouse publish + catalog **`known_at`** is available and was joined to trades after resolving a **D8 vs CH pool_id naming gap**. Full **168h exact depth2 overlap** is only available for **July** closes (D8 bundle); earlier window bars use a documented **CH upper-region proxy**. Origin lifetimes at signal are **minutes—not days**—so persistence heuristics tied to 24h+ `known_at` never fire. User-selected pools still show **shorter in-window overlap** (consistent with BT-D88), but **true lifetime separation between groups is weak**.

---

## 1. Source verification

| Layer | Table / artifact | Role |
|--------|------------------|------|
| Publish ledger | `signal_generator.research_lmv2_active_snapshot_publish_v1` | Causal snapshot_id per 5m `decision_close` |
| ACTIVE catalog | `signal_generator.research_lmv2_snapshot_active_pool_v1` | **`known_at`**, bounds, `pool_id`, side |
| Exact depth2 bands | `results/phase_bt_d85_july_2026_no_pause_research_v1/cache_bundle/` | `origin_ids` in matched upper band (SSOT for backtest) |
| Candles | `signal_generator.candles_1m` | Not used in this phase |

**Pipeline (BT-D85-aligned):** `historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1` · `config_hash` `4aeb36fe…641c9` · **44 064** published 5m PD1 closes (**2026-05-01 → 2026-09-30**).

### Concepts (must not conflate)

| Concept | Meaning in this audit |
|---------|------------------------|
| **CH `known_at`** | First cataloged knowledge time for ACTIVE `pool_id` at published snapshot (true SSOT lifetime field) |
| **Segment epoch in `origin_ids`** | Last `:` field of lldv2 id ≈ **5m candle open** embedded in density output |
| **D8 `origin_ids`** | Exact **depth2 unique-origin** members of the matched upper band at `decision_close` |
| **CH region proxy (pre-July / non-D8 bars)** | Count of distinct upper `pool_id` epochs overlapping entry band prices in catalog—not depth2 clustering |

**Naming gap:** D8 lists `lldv2:DOGEUSDT:5m:upper:<epoch>` while CH catalog stores `lldv2:DOGEUSDT:5m:micro:upper:<epoch>`. Join uses epoch suffix + `micro` variant lookup.

Provenance JSON: `results/phase_bt_d89_active_origin_overlap_audit_v1/source_provenance.json`

---

## 2. Method (past-only)

- Cohort: same **13 + 12** fills as BT-D87/D88; signal = ledger `signal_close_utc`.
- Window: **`signal − 168h`**, floored at **2026-05-01** publish start (not truncated at July 1).
- For each 5m close in window (chronological):
  - **If close ≥ 2026-07-01:** D8 band match → exact contributing `origin_ids`; bar counts toward depth2 if **≥2 origins** in band.
  - **Else:** CH catalog at published close → upper pools overlapping reference band prices; **≥2 distinct epoch keys** = proxy depth2 bar.
- At signal: CH `known_at` for each contributing origin (via micro id mapping).
- Descriptive heuristics only (no thresholds for trading):
  - **Persistent overlap:** ≥50% window bars with ≥2 contributors **and** `known_at` ≥24h before signal.
  - **Fresh/unstable:** ≤3 such bars and segment age &lt;1h (or equivalent short track).

Tool: `tools/phase_bt_d89_active_origin_overlap_audit_v1/run_bt_d89.py` (~77s, read-only CH + D8 LRU).

---

## 3. Group comparison (medians)

| Metric | User-selected 13 | Other 12 |
|--------|------------------|----------|
| Bars with ≥2 contributors in region | **10** | 28 |
| Fraction of 168h window bars (≥2) | **0.50%** | 1.39% |
| CH `known_at` min age at signal (h) | **0.0** | 0.0 |
| Segment epoch min age at signal (h) | **0.083** | 0.083 |
| Origin Jaccard (signal vs window start) | **0.0** | 0.0 |
| D8 origin membership change bars (July) | 3 | 3.5 |
| Fresh/unstable heuristic | **4** | 3 |
| Persistent overlap heuristic (24h `known_at`) | **0** | 0 |
| Window bars pre-July (168h context) | **0** | 0* |
| Adjacent upper neighbors @ 50bp | 2 | 2 |

\*Comparison **max** pre-July bars **1929** (e.g. 2026-07-01 trade); most user-selected midsummer trades have **entire 168h window inside July** → **D8-exact only**.

---

## 4. Findings

### True lifetime vs segment epoch

For all 25 trades, verified CH `known_at` ages at signal are **sub-hour** (typically **0–20 minutes**; max observed **~50 min** in window aggregates). Segment-epoch ages are **~5–15 minutes** before signal. **`known_at` is not “months-old pool birth”** for these micro upper contributors—it is **recent ACTIVE publication** aligned with nearby segment opens.

Example (user SL, 2026-07-08T07:15 signal): two band origins; CH `known_at` for epoch `1783493400` at **06:55Z** (20 min before signal); segment epoch **07:10Z** (5 min before signal).

### Overlap evolution

- **User-selected:** shorter tracked overlap chains (median **10** bars vs **28**), matching BT-D88 price-track persistence.
- **Jaccard 0** signal vs window-start origins: contributing **epoch set at 168h boundary disjoint** from signal band—overlap in price region **re-formed** during the window, not a single continuous identity.
- **Counterexamples:** user **TP** trades with **453** / long proxy+exact bars (Jul 31, Jul 28); comparison **TP** with long region overlap (Jul 7, **682** bars).

### Cluster coherence

Adjacent upper neighbors within **50bp** of entry: **~2–3** at signal for both groups (no large gap treated as “bad”; strategy already requires gap to next upper).

### Missing / uncertain

- **Exact depth2** history for **May–June** window segments: only CH **upper-region proxy**, not analyzer-derived bands.
- **Merge/split event log** not exported from SSOT; inferred via origin membership changes on D8 July bars only.
- **Persistent strong band** heuristic never triggered because **`known_at` ≪ 24h** for these entries.

---

## 5. Answers

1. **Are user-marked weak bands younger or less persistent?**  
   **Less persistent in-window overlap** (fewer 5m bars with ≥2 contributors). **Not younger by CH `known_at` in hours**—both groups show **minute-scale** lifetimes at signal.

2. **Do good trades use short-lived bands?**  
   **Yes.** Comparison and user-selected both include **TP** trades with **1-bar** or **short** overlap; also **long** overlap **TP** exists (Jul 31 user-selected).

3. **Does origin evolution separate beyond BT-D87 width/count?**  
   **Partially:** **overlap duration** and **window-start vs signal origin disjointness** add signal; **`known_at` does not** separate groups at hour scale. Width/origin-count from BT-D87 remains relevant.

4. **What remains unproven?**  
   Causal link from overlap metrics to SL; full **exact depth2** timeline before July without extra analyzer runs; whether **older ACTIVE origins** outside the matched band should have been required.

---

## 6. Further investigation warranted?

**Optional, read-only follow-up (not requested here):**

- Run **bounded** depth2 analyzer snapshots on CH for **pre-July window starts** for early-July trades only (replace region proxy).
- Export **episode records** (`build_active_episode_records`) for contributing **micro pool_ids** only at selected fills (25× bounded CH reads).

**Not recommended without scope change:** filter design, threshold search, or PnL replay.

---

## 7. Artifacts

| File | Content |
|------|---------|
| `per_trade_origin_overlap.csv` | Per-trade features + coverage note |
| `group_comparison.json` | Group medians / heuristic counts |
| `source_provenance.json` | Tables, pipeline, concept map |

---

## 8. Safety

No trading rule, EMA, TP/SL, density SSOT, filter, dashboard, CH import, cache rebuild, or backtest changes · **no git push**.

---

## Commits

| Artifact | Role |
|----------|------|
| `tools/phase_bt_d89_active_origin_overlap_audit_v1/run_bt_d89.py` | Analyzer |
| `results/phase_bt_d89_active_origin_overlap_audit_v1/*` | CSV/JSON |
| `reports/PHASE_BT_D89_ACTIVE_ORIGIN_OVERLAP_AUDIT_V1.md` | This report |
