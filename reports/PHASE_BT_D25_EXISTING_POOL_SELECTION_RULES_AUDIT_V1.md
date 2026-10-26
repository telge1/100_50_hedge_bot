# PHASE BT-D25 — Existing Pool Landscape Research Rules Audit

**Project:** `pools+ob+delta_bot`  
**Mode:** Read-only — no scanner/config/code changes  
**Date:** 2026-10-25  
**Verdict:** **`EXISTING_POOL_RULES_PARTIAL`**

---

## Executive summary

Research and freeze documents describe **selective** upper-pool choice (“relevant” touch pool, proximity, bearish candle, nearest candidate, cooldown, opportunity dedup, optional age). **BT-D21 pool-first** already narrows to **one** structurally eligible upper band per bar (lowest `pool_lo`) plus `pool_signal_hold`. **BT-D23** reuses the **same per-band structural gates** (`_structural_eligible_upper`) but emits **every** eligible upper band across the 24h window (deduped only by **origin-set episode**), with **no** proximity, bearish, nearest, cooldown, or trading hold.

Hence **261 setups / 24h** is consistent with code: not a gap-formula bug, but **missing reuse of older selection layers** that were never unified into one “production” selector.

---

## A. Sources reviewed

| Source | Role |
|--------|------|
| `Signal_definierung.md` | User narrative: 0.5% gaps, dense TP, SL 0.2%, relevance, S4 selectivity |
| `strategy_freeze/pool_landscape_v1_candidate/` | `USER_CONFIRMED_RULES.md`, `IMPLEMENTATION_DEVIATIONS.md`, `OPEN_STRATEGY_DECISIONS.md`, `STRATEGY_SPEC_CANDIDATE_4D.md` |
| Phase **2A** | `tools/phase2a_5m_pd1_depth2_short_candidates_v1/timeline_scanner.js` — proximity + bearish + **nearest** upper + cooldown |
| Phase **4D** | `strategy_rules.py` — SL/TP math, `select_short_tp_depth2` |
| **BT-D5** / registry | `parameter_registry_v1.json` — experimental vs frozen flags |
| **BT-D20** | `reports/PHASE_BT_D20_CURRENT_SHORT_STRATEGY_A_TO_Z_AUDIT_V1.md` — filter catalogue |
| **BT-D21** | `pool_first_signal_v1.py`, `pool_signal_hold`, `pool_identity_v1` |
| **BT-D23** | `setup_scanner_v1.py`, `setup_dedup_v1.py` |
| Modules | `signal_engine_v1.py`, `gap_detection_v1.py`, `entry_price_v1.py`, `pool_identity_v1.py` |

---

## B. Rule table (mandatory)

| REGEL | QUELLE | FUNKTION / FORMEL | CONFIG (Pilot / Baseline) | BT-D23 STATUS | ABWEICHUNG |
|-------|--------|-------------------|---------------------------|---------------|------------|
| Depth-2 exact unique origin | DATA-03, Signal_definierung | Bands with ≥2 origins in PD1 snapshot | `min_unique_origins: 2`, `exact_unique_origin` | **JA** | — |
| 168h + older ACTIVE origins | DATA-04 | Density provider / validated cache bundle | cache SSOT | **JA** | Scanner reads cache only; same as BT-D21 |
| Upper entry at pool **lower edge** | SHORT-ENTRY-LEVEL-01, 4D | `entry = pool_lo` or `pool_lo * (1 - offset/100)` | `pool_lower_edge`, offset 0 | **JA** | `compute_short_entry_level` |
| SL 0.2% above upper **top** | SHORT-04, 4D | `sl = pool_hi * 1.002` | `sl_above_upper_top_pct: 0.2` | **JA** | `short_sl` |
| TP dense lower **cluster** (user) | SHORT-05, O-01 | Qualitative “large/dense” cluster | — | **NEIN** | BT-D23 uses `select_short_tp_depth2` only |
| TP nearest lower top ≥0.5% below entry | SHORT-06, 4D SHORT-TP-01 | Sort lowers by **desc top**, first with `(entry-top)/entry*100 ≥ min_tp` | `min_distance_pct: 0.5`, `next_eligible_band` | **JA** | Documented as **not** dense-cluster (BT-D23 note) |
| Min entry→TP 0.5% | USER_CONFIRMED | Same as TP row | `take_profit.min_distance_pct` | **JA** | At planned entry, not fill |
| **Relevant** upper pool (not any band) | SHORT-01, O-06, IMPLEMENTATION_DEVIATIONS #1 | User: touch/relevance; 2A: proximity + nearest | — | **NEIN** | BT-D23: any band passing structural gates |
| Bearish / rejection candle | S3, SHORT-02, 2A | `close < open` (+ optional v2 touch) | baseline: required | **NEIN** | Pool-first / BT-D23 explicitly skip |
| Proximity to upper pool | 2A, BT-D20 filter G | `inside \| touch \| near_below` within `proximity_pct` | baseline `0.05%` | **NEIN** | Structural gate uses **close < pool_lo** only (`POOL_NOT_ABOVE_PRICE`) |
| **Nearest** upper among candidates | 2A `pickUpperPool`, baseline engine | Min distance `abs(high - pool_lo)/pool_lo` among proximity hits | baseline mode | **NEIN** | BT-D21: **lowest pool_lo** among eligible; BT-D23: **all** eligible |
| Upper **large gap** ≥0.5% | SHORT-03, O-05, Phase 2A profile | Union upper intervals → gaps; `gap_pct = (gap_hi-gap_lo)/gap_lo*100`; large if ≥0.5% | `upper_gap_min_pct: 0.5` | **JA** | `has_upper_large_gap_above_pool(pool_hi)` |
| Gap measured vs **union adjacency** | BT-D20 F | `compute_side_gaps` on unioned uppers | `adjacent_union_interval_gap_pct_v1` | **JA** | Not “next physical band only”; any gap with `gap_bottom ≥ pool_upper` |
| Pool **min age** bars | O-04, FILTER-AGE-01 | `pool_age_bars[pk] >= min_age` | research 5; pilot **0** | **TEILWEISE** | Gate exists in `_structural_eligible_upper`; pilot **off** |
| Small vs large upper structure | S4, Signal_definierung | Manual “micro pool” preference | — | **NEIN** | No band-width / micro filter in code |
| Min distance **between** upper pools | User research / gaps | Implicit in gap union | — | **TEILWEISE** | Only via gap-above test, not inter-pool spacing rule |
| Origin-set **relevance** | DATA-03, freeze | ≥2 unique origins per band | — | **JA** | Per band, not “which” origin set wins |
| Pool **deactivation** / cancel | EXEC-POOL-DEACTIVATED-01 | Loss of depth-2 zone / origins in snapshot | `origin_set_v1` (BT-D21) | **NEIN** (scanner) | BT-D23 dedup clears episode when origins absent; no cancel simulation |
| **One signal per bar** (lowest upper) | BT-D21 pool-first | `candidates.sort(pool_lo); best = [0]` | `pool_first_structural_v1` | **NEIN** | BT-D23 loops **all** `upper_bands` |
| **pool_signal_hold** (no re-signal same pool) | BT-D21 engine | Block `pool_key` until `origin_set_v1` inactive | `pool_signal_hold` | **NEIN** | BT-D23 uses **origin-set episode** dedup only |
| Opportunity / cooldown dedup | 2A, IMPLEMENTATION_DEVIATIONS #14 | 6-candle cooldown per pool key; 2G research | 2A defaults | **NEIN** | BT-D23: one emit per origin episode, no cooldown |
| Max 1 position / pending slot | POS-01, BT-D21 engine | Single `pending`, `position_open_until` | `max_concurrent_positions: 1` | **NEIN** | Scanner by design emits all episodes |
| **Next pool** when upper inactive | EXEC-POOL-DEACTIVATED-01, `pool_identity_v1` | Pending cancel; hold released; new signal when eligible | `pool_still_active_for_pending` | **NEIN** (scanner) | **Implemented in BT-D21 engine only** — see §D |
| Pending limit at pool edge | EXEC-USER-PENDING-LIMIT-V1 | Limit at entry_level; causal fill BT-D18 | pilot YAML | **NEIN** | Scanner = planned levels only |
| Fees / PnL | SHORT-07 | Round-trip 0.11% | pnl block | **NEIN** | Scanner has no PnL |

---

## C. Why 261 setups in 24h? (pilot cache `bt_d23_pilot_sep11_24h_v1`)

| Factor | Evidence |
|--------|----------|
| **All eligible uppers, not one** | `setup_scanner_v1` iterates every `upper_bands` entry; BT-D21 `detect_short_signal_pool_first` picks **one** (lowest `pool_lo`). |
| **261 = 261 unique `origin_set_key`** | `setup_distribution.json`: `unique_origin_sets: 261` — dedup is **per origin episode**, not per price zone. |
| **59 entry price levels** with **multiple** origin sets | Same price zone, different density merges → separate setups. |
| **Peak 12:00 UTC (94 setups)** | Rapid landscape churn + many bands first becoming structurally eligible as price trades through upper structure (`POOL_NOT_ABOVE_PRICE` requires `close < pool_lo`). |
| **Gap rule applied** | `has_upper_large_gap_above_pool` uses union gaps ≥0.5% with `gap_bottom ≥ pool_upper` — **correct per BT-D20/4D implementation**; not the main driver of 261 vs 7 signals. |
| **No proximity / bearish** | Many bands pass while price is below pool lower edge without touch/rejection. |
| **pool_min_age_bars = 0** | Bands eligible from first bar they appear in snapshot. |
| **Older selection BT-D23 does not use** | Baseline **nearest_upper_proximity_v1** (7 signals in BT-D21 compare window) + pending/hold; 2A proximity+nearest+cooldown; freeze “relevant pool” narrative. |

**Conclusion:** 261 is **expected** for “all structural episodes” semantics; it is **not** what baseline backtester or 2A scanner emit.

---

## D. Dynamic “next pool” when upper becomes inactive

| Layer | Mechanism | Identity |
|-------|-----------|----------|
| **Pending order cancel (BT-D21)** | Each bar: if pending and `!pool_still_active_for_pending(...)` → `CANCELLED_POOL_INACTIVE` | `origin_set_v1` or `exact_band_v1` (`pool_identity_v1.py`) |
| **pool_signal_hold (BT-D21)** | Same `pool_still_active_for_pending` on held metadata → release `pool_key` for new signals | Origin set + last pool bounds in hold |
| **New signal after cancel** | Engine may signal **another** eligible upper (lowest among candidates) when pending cleared | **Lowest `pool_lo`**, not “next higher” |
| **BT-D23 dedup** | `SetupDedupTrackerV1.end_bar_origin_inactive`: drop episode when origin set absent from snapshot; re-emit on return = new `FIRST_STRUCTURAL_ELIGIBILITY` | Sorted origin IDs string |
| **User MS / WATCH** | Blocks shorts in 3B/4A scenarios | **UNRESOLVED** (O-08); **not** in BT-D7/BT-D23 path |

**No new implementation proposed** — reuse candidates: `pool_identity_v1.pool_still_active_for_pending`, BT-D21 hold/cancel loop, 2A cooldown map.

---

## E. Take-profit: documented vs implemented

| Approach | Where defined | Code | BT-D23 |
|----------|---------------|------|--------|
| **Dense lower cluster** (user SHORT-05) | Signal_definierung, O-01 OPEN | Not in `select_short_tp_depth2` | **NEIN** |
| **next_eligible_band / nearest lower top** | 4D SHORT-TP-01, freeze amendment | `select_short_tp_depth2(entry, intervals, min_tp_pct)` — highest eligible lower **top** | **JA** |
| TP quality gate | TP-QUALITY-01 OPEN | `tp_pool_quality_gate_enabled: false` | **NEIN** |
| PD10 for TP landscape | O-13 OPEN | Not wired | **NEIN** |

---

## F. Reuse recommendation (no new rules)

### 1:1 reusable without new strategy design

| Component | Path | Use for |
|-----------|------|---------|
| Per-band structural gates | `pool_first_signal_v1._structural_eligible_upper` | Same eligibility as BT-D21 |
| Gap detection | `gap_detection_v1` | 0.5% union-gap semantics |
| Entry / SL / TP math | `entry_price_v1`, `strategy_rules.short_sl`, `select_short_tp_depth2` | Planned levels |
| **Single pool per bar** | `detect_short_signal_pool_first` | Matches BT-D21 “one candidate” |
| Baseline touch path | `signal_engine_v1` + `nearest_upper_proximity_v1` | Historical 3-signal / proximity research |
| Pool lifecycle | `pool_identity_v1`, `short_research_engine_v1` pool_signal_hold | Cancel / hold / next signal |
| 2A research scanner | `timeline_scanner.js` | Proximity + bearish + nearest + cooldown reference |
| Freeze truth table | `IMPLEMENTATION_DEVIATIONS.md`, `OPEN_STRATEGY_DECISIONS.md` | What is still OPEN vs MATCH |

### User decisions still open (from freeze, not resolved by BT-D23)

- **O-01** Dense lower TP cluster vs nearest-top TP  
- **O-05** Whether `min_pool_gap_pct` is a hard gate everywhere (vs profile-only)  
- **O-04** `pool_min_age_bars` hard gate  
- **O-06** “Relevant” upper beyond density/proximity  
- **O-15** S4 “only clearest” quality filters  
- **O-08** WATCH / when shorts resume  
- **O-10** Re-entry same opportunity  

---

## G. BT-D23 vs BT-D21 vs baseline (selection only)

```
Baseline (nearest_upper_proximity_v1):
  ALL uppers → proximity filter → bearish → nearest by |high−pool_lo| → gap → ONE signal/bar

BT-D21 (pool_first_structural_v1):
  ALL uppers → structural gates → LOWEST pool_lo → ONE signal/bar + pool_signal_hold + single pending

BT-D23 (setup scanner):
  ALL uppers → structural gates → EMIT per origin_set (episode dedup) → NO limit on bands per bar
```

---

## VERDICT

**`EXISTING_POOL_RULES_PARTIAL`**

- **Reusable:** Structural gates, gap/SL/TP modules, pool-first **single-pool** selector, baseline proximity path, identity/cancel/hold in trading engine.  
- **Gap found:** BT-D23 intentionally omits documented **selection** layers (relevance, proximity, bearish, nearest/lowest-per-bar policy, cooldown, opportunity dedup, trading hold) while applying **all** structural passes → 261 episodes.  
- **Not a defect** in 0.5% gap measurement relative to BT-D20 catalogue.

**STOP** — read-only analysis complete. No scanner, config, or strategy changes.
