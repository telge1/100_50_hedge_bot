# PHASE BT-D26 — Dynamic Upper Pool Selector (Reference)

**Project:** `pools+ob+delta_bot`  
**Date:** 2026-10-25  
**Verdict:** **`DYNAMIC_SELECTOR_REFERENCE_READY`**

Read-only w.r.t. BT-D23 pilot, baseline trading engine, LMV2 Short V2, and live dashboard (no restart).

---

## 1. Goal

Reuse **existing** structural rules and **BT-D21** selection (`detect_short_signal_pool_first` → lowest `pool_lower_edge`) while tracking **one** dynamic upper candidate per 5m close. Emit **PLANNED** setups only on candidate events (not every eligible band). No bearish/proximity, no fills, no PnL.

---

## 2. Implementation (isolated)

| Artifact | Path |
|----------|------|
| Identity reconciliation | `tools/phase_bt_d26_dynamic_upper_pool_selector_v1/candidate_identity_v1.py` |
| State machine | `tools/phase_bt_d26_dynamic_upper_pool_selector_v1/candidate_state_v1.py` |
| Scanner | `tools/phase_bt_d26_dynamic_upper_pool_selector_v1/dynamic_selector_v1.py` |
| CLI | `tools/phase_bt_d26_dynamic_upper_pool_selector_v1/run_dynamic_selector.py` |
| Chart overlay | `tools/phase_bt_d26_dynamic_upper_pool_selector_v1/chart_adapter_v1.py` |
| Tests | `tools/phase_bt_d26_dynamic_upper_pool_selector_v1/tests/test_dynamic_selector_v1.py` |

**Contract:** `bt_d26_dynamic_upper_pool_selector_v1`  
**Selection note (reference):** lowest eligible upper — **not** final economic relevance approval.  
**TP:** `select_short_tp_depth2` / `next_eligible_band` — marked **experimental** in JSON (`tp_selection_note`).

### States (logged per close in `candidate_transitions`)

`NO_CANDIDATE` · `CANDIDATE_SELECTED` · `CANDIDATE_STILL_VALID` · `CANDIDATE_INVALIDATED` · `NEXT_CANDIDATE_SELECTED`

### Identity (`pool_identity_v1` + `origin_set_v1`)

- Pending-style active check: `pool_still_active_for_pending`
- Bound drift / merge: `resolve_tracked_upper_band`
- **UNRESOLVED:** `ORIGIN_SET_AMBIGUOUS_MULTIPLE_BANDS` → appended to `unresolved_events` (no synthetic merge)

---

## 3. Pilot run (24h validated cache)

| Run | Path | Setups |
|-----|------|--------|
| **A — BT-D23** (unchanged) | `results/setup_scanner_runs/bt_d23_pilot_sep11_24h_v1/` | **261** |
| **B — BT-D26** | `results/setup_scanner_runs/bt_d26_dynamic_referenz_sep11_24h_v1/` | **102** |

Config: `configs/pilot_bt_d21_pool_first_24h.yaml`  
Comparison embedded in B’s `setups.json` → `comparison_bt_d23`.

### B — event mix

| `selection_reason` | Count |
|--------------------|------:|
| `LOWER_ELIGIBLE_UPPER_POOL_CHANGED` | 71 |
| `RESELECT_AFTER_INVALIDATION_BT_D21_LOWEST` | 30 |
| `FIRST_DYNAMIC_CANDIDATE_BT_D21_LOWEST` | 1 |

`unresolved_events`: **deduped per origin-set episode** (ambiguous origin-set ↔ multiple upper bands; tracked pool retained when origins still active — see JSON for first occurrence + match list).

**Not optimized** toward any target count — reflects causal lowest-pool tracking + invalidation/reselect.

---

## 4. Reference closes (2026-09-11)

| UTC | BT-D26 tracked `pool_lower` / `pool_upper` | Emit at bar? | Notes |
|-----|---------------------------------------------|--------------|-------|
| **12:00** | 0.08365 / 0.08366 | **Yes** — `FIRST_DYNAMIC_CANDIDATE_BT_D21_LOWEST` | BT-D23 lists **62** setups on same close (all eligible bands). |
| **15:35** | 0.08681 / 0.086838 | **Yes** — `RESELECT_AFTER_INVALIDATION_BT_D21_LOWEST` | Aligns with BT-D23/D24 reference band at 15:35 (single D23 setup that bar). |
| **17:00** | 0.08593 / 0.085982 | **Yes** — `RESELECT_AFTER_INVALIDATION_BT_D21_LOWEST` | After invalidation/reselect; not the 15:35 origin pair — lowest **currently** eligible upper at 17:00 close. BT-D23: 1 setup that bar (different origin set at same structural window). |

**Why not identical to BT-D21 trading (7 signals)?** BT-D26 does **not** apply `pool_signal_hold`, single pending slot, or position limits — it only tracks **landscape-driven** candidate changes. Switches when a **lower** eligible upper appears or origins go inactive.

---

## 5. Chart (BT-D24-ready)

- `chart_overlay_bundle.json` beside `setups.json` (same field shape as BT-D23 overlay; `metadata.scanner_contract` = BT-D26).
- Dashboard wiring for BT-D26 is **out of scope** (no server restart).

---

## 6. Tests

```bash
python3 -m pytest tools/phase_bt_d26_dynamic_upper_pool_selector_v1/tests/test_dynamic_selector_v1.py -q
```

Covers: ≤1 emit per bar, STILL_VALID suppression, gap helper, origin-set merge resolution, chart overlay, state switch, entry/SL path via integration fixture.

**BT-D23:** no files modified under `phase_bt_d23_pool_first_setup_scanner_v1/`.

---

## 7. Reuse vs open decisions

| Reused 1:1 | Still open (unchanged) |
|------------|-------------------------|
| `_structural_eligible_upper` / `detect_short_signal_pool_first` | Final “relevant” pool vs lowest-edge reference |
| `gap_detection_v1`, entry/SL/TP math | Dense lower TP cluster |
| `pool_identity_v1` cancel semantics | Reducing `unresolved_events` noise vs strict ambiguity policy |
| YAML pilot structural params | Economic approval of dynamic switch rules |

---

## VERDICT

**`DYNAMIC_SELECTOR_REFERENCE_READY`**

Prototype, tests, 24h A/B comparison, reference timestamps, and chart artifacts are in place. Identity ambiguity is **logged**, not papered over (`DYNAMIC_SELECTOR_PARTIAL` would apply only if ambiguity blocked tracking — it does not after reconcile fix).

**STOP** — no strategy freeze, no trading simulation, no PnL, no LMV2 Short V2 changes.
