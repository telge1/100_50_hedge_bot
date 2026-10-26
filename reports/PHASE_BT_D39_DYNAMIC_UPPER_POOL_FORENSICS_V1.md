# PHASE BT-D39 — Dynamic Upper Pool Candidate Forensics

**Project:** `pools+ob+delta_bot`  
**Live chart (context only, no changes):** `spread_recovery_hedge_short_dev/dashboard`  
**Reference run:** `bt_d26_dynamic_referenz_sep11_24h_v1`  
**Date:** 2026-10-26  

---

## VERDICT

**`DYNAMIC_POOL_FORENSICS_PASS`**

Read-only replay of all **102** PLANNED setups using stored BT-D26 JSON plus canonical **5m PD1 Depth2** density (`validated_cache`, 168h warmup contract). No new signals, no rule changes, no PnL/fill simulation.

---

## 1. Scope and method

| Item | Source |
|------|--------|
| Episodes | `results/setup_scanner_runs/bt_d26_dynamic_referenz_sep11_24h_v1/setups.json` |
| Transitions log | Same file `candidate_transitions` (cross-check) |
| Config | `configs/pilot_bt_d21_pool_first_24h.yaml` via `load_yaml_and_validate` |
| Landscape at each close | `create_density_session` + `_structural_eligible_upper` (same as BT-D21/BT-D26) |
| Pool age | Replayed `pool_age_bars` counter (bars band present in snapshot) |
| Forensics tool | `tools/phase_bt_d39_dynamic_upper_pool_forensics_v1/run_forensics.py` |

**Outputs:** `results/phase_bt_d39_dynamic_upper_pool_forensics_v1/` (`episodes.csv`, `transitions.csv`, `reference_1535.json`, `manual_review_10.csv`, `summary.md`).

---

## 2. Event mix (stored run)

| `selection_reason` | Count |
|--------------------|------:|
| `LOWER_ELIGIBLE_UPPER_POOL_CHANGED` | 71 |
| `RESELECT_AFTER_INVALIDATION_BT_D21_LOWEST` | 30 |
| `FIRST_DYNAMIC_CANDIDATE_BT_D21_LOWEST` | 1 |

These are **state-machine labels** from `candidate_state_v1.py`, not post-hoc inventions.

---

## 3. Forensic classification (102 emits)

Classification uses **only** previous/current `pool_key`, bounds, `origin_set_key`, and stored reasons (no invented price/time thresholds).

| Class | Count | Meaning |
|-------|------:|---------|
| `5_SWITCH_TO_LOWER_ELIGIBLE_ZONE` | 59 | `pool_lower` moved to a **lower** eligible band (`LOWER_ELIGIBLE…`, different zone) |
| `3_POOL_BOUND_DRIFT_OR_SPLIT_MERGE` | 12 | **Same** `pool_lower`, **upper** bound changed (reconcile / multi-band origin set) |
| `4_DEACTIVATION_RESELECT` | 30 | `RESELECT_AFTER_INVALIDATION…` (typically `POOL_ORIGIN_INACTIVE` on prior track) |
| `1_FIRST_SELECT` | 1 | First dynamic candidate at run start |

**Note:** 71 `LOWER_ELIGIBLE…` events = 59 true zone-step + 12 same-lower bound drift (still logged as LOWER_ELIGIBLE because `pool_key` changed).

**Not observed as primary emit driver in this run:** standalone “origin-only” change with identical `pool_key` (would not emit per BT-D26 `TRACKED_CANDIDATE_UNCHANGED`).

`unresolved_events` in the run (e.g. `ORIGIN_SET_AMBIGUOUS_MULTIPLE_BANDS` at 15:35–15:45) document **identity reconciliation**; they do not replace the lowest-eligible selection rule.

---

## 4. Reference close — 2026-09-11 15:35 UTC

| Field | Value |
|-------|--------|
| **5m close** | 0.08668 |
| **Selected upper** | lower **0.08681**, upper **0.086838** |
| **Origins (2)** | `lldv2:…:1789137900`, `lldv2:…:1789140600` |
| **Entry / SL / TP** | 0.08681 / 0.087011676 / 0.08586 |
| **`selection_reason`** | `LOWER_ELIGIBLE_UPPER_POOL_CHANGED` |
| **`invalidation_reason`** | `SUPERSEDED_BY_LOWER_ELIGIBLE_POOL` |
| **Previous tracked** | 0.08855–0.08856 (3 origins, higher zone) |
| **Pool age at select** | **1 bar** (band newly counted in replay) |
| **Eligible uppers** | **37** structurally eligible; **lowest** by `pool_lower` is the selected band |

### Why this pool (causal, no future price)

1. At 15:35 close, BT-D21 **pool_first** picks the **minimum `pool_lower`** among bands passing structural checks (price below pool, min age, upper gap, entry below SL, Depth2 TP).
2. The selected band is **rank 1** in `eligible_uppers_ordered_by_pool_lower` in `reference_1535.json`.
3. The prior tracked candidate (0.08855) remains **eligible** but is **not** lowest; the state machine records a **zone step** (`LOWER_ELIGIBLE…`), not an invalidation-only reselect.
4. This is a **new price zone** (~0.0868 vs ~0.0886), not a cosmetic origin tweak within one stable `pool_key`.

### Alternatives (same close, excerpt)

Higher eligible bands (not chosen) include the previous 0.08855–0.08856 stack and many intermediate uppers — full ordered list in `reference_1535.json`. **Strength/persistence:** previous zone had **higher** `pool_age_bars` (~19+) but **higher** `pool_lower`; selector is explicitly **lowest-edge**, not max-age.

---

## 5. Manual control cases (10)

See `manual_review_10.csv`. Episodes **1, 2, 4, 11–13, 20, 35, 50, 65** cover:

- first select  
- invalidation reselect  
- approach to 15:35 (ep 11)  
- **reference 15:35 (ep 12)**  
- **15:40 bound drift** same lower 0.08681, upper 0.086838→0.08682 (ep 13, class `3_…`)  
- mid-run zone switches and bound drift  

Each row includes a **Research chart GO TO** question for visual confirmation (Exact Origin density, Depth2, min origins 2).

---

## 6. Implications for interpretation

| Question | Finding |
|----------|---------|
| Are all 102 emits “new strategic uppers”? | **No.** 12/102 are **same lower edge**, upper bound drift; 30/102 are **reselect after inactive origins** without necessarily the lowest global zone change label. |
| Does selector use future price? | **No** in this analysis; eligibility uses close at decision bar only. |
| Does lowest eligible = strongest pool? | **No** — documented `selection_note`; age/persistence can favor higher bands that are **not** chosen. |

---

## 7. Constraints observed

- No trading / Entry / SL / TP / density SSOT changes  
- No new scanner run (replay only)  
- No dashboard code changes  
- No PnL or fill simulation  

---

**End of BT-D39.**
