# PHASE BT-D40 — Causal Upper Pool Landscape Quality Review

**Project:** `pools+ob+delta_bot`  
**Prior:** BT-D39 `DYNAMIC_POOL_FORENSICS_PASS`  
**Run:** `bt_d26_dynamic_referenz_sep11_24h_v1`  
**Date:** 2026-10-26  

---

## VERDICT

**`UPPER_POOL_QUALITY_REVIEW_PASS`**

Read-only causal landscape review at decision closes. Canonical **5m PD1 Exact Unique-Origin Density**, **Depth2**, **validated_cache** (168h warmup contract). No strategy, filter, density-math, dashboard, PnL, or outcome-based selection changes.

---

## 1. Method

| Input | Role |
|-------|------|
| BT-D39 `manual_review_10.csv` | Ten control timestamps |
| BT-D39 episodes / transitions | Cross-check emits & bound drift |
| `_structural_eligible_upper` | Same gates as BT-D26 (gap, age, TP, SL, price below pool) |
| `tools/phase_bt_d40_upper_pool_quality_v1/run_quality_review.py` | Single forward density replay |

**Outputs:** `results/phase_bt_d40_upper_pool_quality_v1/`  
(`reference_cases.csv`, `eligible_upper_landscapes.csv`, `reference_1535_all_uppers.csv`, `bound_drift_12.csv`, `structural_observations.md`).

For each reference close: **every** upper band in the snapshot is listed (eligible + ineligible with exclusion reason), not a top-N subset.

---

## 2. Ten reference cases — landscape summary

| Ep | UTC | Close | BT-D26 lower–upper | Eligible / snapshot | BT-D26 rank | Selection reason |
|---:|-----|------:|--------------------|--------------------:|------------:|------------------|
| 1 | 12:00 | 0.08356 | 0.08365–0.08366 | 86 / 100 | **1** | FIRST_DYNAMIC |
| 2 | 12:05 | 0.08338 | 0.08384–0.083844 | 84 / 98 | **1** | RESELECT_INVALIDATION |
| 4 | 12:35 | 0.08488 | 0.08542–0.08545 | 53 / 73 | **1** | RESELECT_INVALIDATION |
| 11 | 15:25 | 0.08663 | 0.08855–0.08856 | 36 / 53 | **1** | LOWER_ELIGIBLE_CHANGED |
| **12** | **15:35** | **0.08668** | **0.08681–0.086838** | **37 / 51** | **1** | LOWER_ELIGIBLE_CHANGED |
| 13 | 15:40 | 0.08639 | 0.08681–0.08682 | 39 / 53 | **1** | LOWER_ELIGIBLE (bound drift) |
| 20 | 19:45 | 0.08438 | 0.08444–0.08447 | 45 / 59 | **1** | LOWER_ELIGIBLE |
| 35 | 23:20 | 0.08398 | 0.08408–0.08412 | 53 / 67 | **1** | LOWER_ELIGIBLE |
| 50 | 01:45+1d | 0.08448 | 0.084482–0.084488 | 48 / 66 | **1** | LOWER_ELIGIBLE |
| 65 | 06:20+1d | 0.08434 | 0.08442–0.08445 | 61 / 75 | **1** | LOWER_ELIGIBLE |

**Observation:** In all ten controls, BT-D26’s pick is **eligible rank 1** by `pool_lower` — consistent with the documented lowest-edge rule only. No secondary structural tie-break is applied.

Full per-band rows: `eligible_upper_landscapes.csv` (~695 rows = all snapshot uppers across ten closes).

---

## 3. Reference 15:35 UTC — all 37 eligible uppers

**Selected (BT-D26):** 0.08681–0.086838 · 2 origins · **pool_age_bars = 1** · gap pass · Entry/SL/TP **0.08681 / 0.087011676 / 0.08586**  
**Previous tracked:** 0.08855–0.08856 (3 origins, age 19 at this close) — still eligible but **rank 2**.

### Why 0.08681 is the lowest eligible zone

Among 37 eligible bands, **minimum `pool_lower` is 0.08681** (rank 1). The next eligible step in lower-edge space is **0.08855** (Δlower ≈ **0.00174**, ~2.0% above the selected lower edge). No eligible band exists between close **0.08668** and 0.08681 with a lower edge below 0.08681.

### Ineligible bands at this close (14 of 51)

| Exclusion reason | Count |
|------------------|------:|
| `UPPER_GAP_FAIL` | 14 |

No other exclusion code appeared on non-eligible snapshot bands at this close (others are structurally eligible or absent).

### Persistence vs selection (causal descriptors only)

| Role | Example lower–upper | pool_age_bars | origins | eligible rank |
|------|---------------------|---------------|---------|---------------|
| **BT-D26 pick** | 0.08681–0.086838 | **1** | 2 | 1 |
| Older persistent eligible | 0.08856–0.08859 | **44** | 3 | 3 |
| Older persistent eligible | 0.08904–0.08907 | **44** | 3 | 5 |
| More origins (not selected) | 0.08933–0.08934 | 44 | **6** | 13 |

BT-D26 **does not** use age, origin count, or cluster overlap — only **lowest eligible lower edge**. The 15:35 pick is the **youngest** among eligible bands at rank 1, not the most persistent or origin-dense.

### Cluster / overlap (15:35)

Price-interval overlap clustering on the full snapshot places the selected band in a **local cluster** with neighbouring micro-bands near ~0.0868–0.0891 (several eligible steps with small Δlower). The **0.08855+** ladder shares overlapping origin families with unresolved multi-band episodes logged in BT-D26 `unresolved_events` (same day) — identity reconciliation issue, not a second selection rule.

**Complete 37-row table:** `reference_1535_all_uppers.csv` (includes ineligible bands, overlap counts, `price_cluster_id`, distance to close, gap %, hypothetical entry/SL per band).

---

## 4. Twelve bound-drift episodes

**Definition (BT-D39):** same `pool_lower`, changed `pool_upper` and/or `origin_set_key` → new `pool_key`, emit with `LOWER_ELIGIBLE_UPPER_POOL_CHANGED`.

| Finding | Detail |
|---------|--------|
| Count | **12** (matches BT-D39) |
| Origin set changed | **12 / 12** on emit |
| Same structural zone? | **Same lower anchor**; upper bound is a **sub-band** of a multi-band origin ladder (density split), not a move to a different lower price |
| SL | `planned_sl` moves with `short_sl(upper_top)`; see `sl_delta_abs` in `bound_drift_12.csv` (e.g. ep 13: 0.086838 → 0.08682 top, SL **0.087011676 → 0.08699364**) |

**Diagnosis:** These are **membership/bound reconcile** events within one lower-price neighbourhood, not independent “new strategic zones” in the sense of BT-D39 zone-step emits. No signals removed; documentation only.

---

## 5. Does selection use anything beyond lowest lower edge?

| Structural signal | In eligibility? | Used for BT-D26 pick among eligible? |
|-------------------|-----------------|--------------------------------------|
| Price below pool | Yes | Yes (filter) |
| Min pool age | Yes | Yes (filter) |
| Upper gap test | Yes | Yes (filter) |
| Depth2 TP exists | Yes | Yes (filter) |
| Entry below SL | Yes | Yes (filter) |
| **Lowest `pool_lower`** | — | **Yes (sole ranking rule)** |
| Higher pool_age_bars | — | **No** |
| More `active_origin_count` | — | **No** |
| Cluster density / overlap | — | **No** |
| Distance from close beyond filters | — | **No** |

We **do not** propose an alternative “winner” pool — quality dimensions are **exposed** for human review; economic relevance rules remain out of scope.

---

## 6. Outcomes / lookahead

No future price paths, fills, or PnL were used in this phase.

---

## 7. Artifacts

| File | Content |
|------|---------|
| `reference_cases.csv` | Ten-case summary |
| `eligible_upper_landscapes.csv` | All snapshot uppers × 10 cases |
| `reference_1535_all_uppers.csv` | Full 51-band landscape at 15:35 |
| `bound_drift_12.csv` | Bound-drift diagnosis |
| `structural_observations.md` | Machine summary |

**Re-run:** `python3 tools/phase_bt_d40_upper_pool_quality_v1/run_quality_review.py`

---

**End of BT-D40.**
