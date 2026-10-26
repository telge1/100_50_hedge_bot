# PHASE BT-D41 — Causal Pending Short Limit Order Lifecycle V1

**Project:** `pools+ob+delta_bot`  
**Upstream:** BT-D39 `DYNAMIC_POOL_FORENSICS_PASS`, BT-D40 `UPPER_POOL_QUALITY_REVIEW_PASS`  
**Pilot:** `bt_d26_dynamic_referenz_sep11_24h_v1` (Sep-11–12 2026, 5m PD1, validated density cache)  
**Date:** 2026-10-26  

---

## VERDICT

**`PENDING_LIFECYCLE_RESEARCH_PASS`**

Isolated research state machine over the existing **102** BT-D26 `PLANNED` episodes. **No** live orders, **no** fills, **no** PnL, **no** changes to BT-D26, dashboard, or density SSOT.

---

## 1. Problem statement (user rule)

From the current price upward, only the **next structurally eligible upper pool** (lowest eligible `pool_lower`) may be selected. A planned short limit can **miss by a tick**; price may then fall several percent and the pool landscape shifts. A pending order must **not** persist blindly or auto-execute on a later rally without a causal validity review at each **5m decision close**.

BT-D41 models **scanner selection** vs **active pending** vs **cancel/replace** explicitly.

---

## 2. Time model (causal)

| Field | Definition |
|-------|------------|
| `decision_close_utc` | Completed 5m bar close **T** (landscape + review) |
| `intent_created_utc` | **T** when scanner emits / lifecycle `CREATE` |
| `earliest_eligible_execution_utc` | **T+5m** (next bar open; no retroactive fill in signal bar) |
| `next_review_utc` | Next 5m close after **T** |

Only information available at **T** is used for landscape and eligibility (`_structural_eligible_upper`, Depth2 TP, gap, `origin_set_v1` active checks).

---

## 3. State machine

| State | Meaning |
|-------|---------|
| `NO_CANDIDATE` | No active pending; no scanner emit at close |
| `PLANNED` | Scanner signal recorded (`PLANNED_SCANNER` event) |
| `PENDING` | Active pending short limit (research) |
| `CANCELLED` | Structurally invalidated or superseded |
| `REPLACED` | Scanner emit while pending existed |
| `EXPIRED_INVALID` | Cancel after structural gate failure (e.g. no TP) |
| `FILLED` | **Reserved** — not simulated in BT-D41 |

**At most one** `PENDING` order at any time (enforced in replay).

**Distinction:**

- **Scanner** — BT-D26 dynamic candidate emit (`PLANNED_SCANNER`)
- **Planned levels** — entry / SL / TP from unchanged rules
- **Pending** — order active from `earliest_eligible_execution_utc`
- **Cancel** — review failure or `NOT_NEXT_ELIGIBLE_UPPER` before a new create

---

## 4. Review at each decision close (pending exists)

Order of operations per close **T**:

1. **Intrabar flag** (no fill): if `high >= planned_entry` on bar **T** and `T >= earliest_eligible_execution_utc` → `POTENTIAL_FILL_UNRESOLVED` (does **not** assert cancel or fill).
2. **Review** pending against snapshot at **T**:
   - Pool still **active** (`pool_still_active_for_pending`, `origin_set_v1`)
   - `_structural_eligible_upper` (gap, age, entry &lt; SL, Depth2 TP)
   - Still **next** eligible upper (`detect_short_signal_pool_first` lowest `pool_lower`)
   - Reconcile bounds via `resolve_tracked_upper_band` (bound/origin drift → document; refresh levels in-place on `KEEP` when reconciled)
3. **Scanner step** (unchanged BT-D26): if emit → `PLANNED_SCANNER` + `CREATE`; if pending still present → `REPLACE` (`SCANNER_EMIT_SUPERSEDES_PENDING`).

**Not a cancel reason:** price falling 2–5% alone. Observed cancels use **structure** (`POOL_NOT_ABOVE_PRICE` when close ≥ pool lower, `POOL_NOT_ACTIVE`, `NOT_NEXT_ELIGIBLE_UPPER`, etc.).

---

## 5. Intrabar boundary (E)

Between decision closes, limit price may be touched. BT-D41 **does not** simulate fills and **does not** retroactively cancel because a touch might have occurred.

- Event: `POTENTIAL_FILL_UNRESOLVED`
- Pilot count: **82** touch intervals (`unresolved_fill_intervals.csv`)
- **Future:** deterministic fill-vs-cancel ordering documented in `summary.md`; not activated in BT-D41.

---

## 6. Pilot results (Sep-11 24h)

| Metric | Value |
|--------|------:|
| BT-D26 `PLANNED` setups | 102 |
| `PLANNED_SCANNER` | 102 |
| `CREATE` | 102 |
| `KEEP` | 198 |
| `CANCEL` | 89 |
| `REPLACE` | 12 |
| `POTENTIAL_FILL_UNRESOLVED` | 82 |

### Cancel reasons (`cancel_reasons.csv`)

| Reason | Count | Role |
|--------|------:|------|
| `NOT_NEXT_ELIGIBLE_UPPER` | 36 | Closer eligible upper appeared (review before scanner create) |
| `POOL_NOT_ACTIVE` | 30 | Origin-set / band inactive |
| `POOL_NOT_ABOVE_PRICE` | 23 | Close no longer below pool lower (structural) |
| `SCANNER_EMIT_SUPERSEDES_PENDING` | 12 | Bound-drift / scanner emit with pending still present |

`REPLACE` (**12**) aligns with BT-D39 **bound-drift** episodes (same lower, upper/origin shift).

---

## 7. Reference case — 2026-09-11 15:35 UTC (G)

| Field | Value |
|-------|-------|
| Close | 0.08668 |
| Pool | 0.08681–0.086838 |
| Entry / SL / TP | 0.08681 / 0.087011676 / 0.08586 |
| Scanner reason | `LOWER_ELIGIBLE_UPPER_POOL_CHANGED` |

**At 15:35 (episode 12):**

1. **CANCEL** prior pending (0.08855–0.08856) — `NOT_NEXT_ELIGIBLE_UPPER` (0.08681 zone became next eligible).
2. **PLANNED_SCANNER** + **CREATE** new pending; active from **15:40** UTC.

**15:40 (episode 13, bound drift):**

1. **KEEP** pending with reconcile note (`ORIGIN_SET_AMBIGUOUS_MULTIPLE_BANDS`; scanner lowest key differs).
2. **REPLACE** on scanner emit → upper **0.08682**, SL **0.08699364** (same lower anchor).

Full causal chain: `results/phase_bt_d41_pending_lifecycle_v1/reference_1535_timeline.json`.

---

## 8. Test matrix (H)

| # | Requirement | Status |
|---|-------------|--------|
| 1 | Pool unchanged → KEEP | Pilot `KEEP` events; pytest smoke |
| 2 | Depth2 loss → CANCEL | Via `_structural_eligible_upper` / `NO_TP_DEPTH2` |
| 3 | Not ACTIVE → CANCEL | `test_not_active_cancels` |
| 4 | Closer eligible upper → CANCEL (then CREATE) | `NOT_NEXT_ELIGIBLE_UPPER` (36×) |
| 5 | Distant upper only → KEEP | When still lowest eligible |
| 6 | Origin-set drift | Reconcile + detail on KEEP; REPLACE on scanner emit |
| 7 | Upper bound change → SL | REPLACE rows; bound-drift 12 |
| 8 | Large fall, still eligible → no price-only cancel | No `% drop` reason in cancel table |
| 9 | Loses relevance → CANCEL | `POOL_NOT_ACTIVE` / structural |
| 10 | Intrabar touch | `test_intrabar_unresolved_high_touch` + 82 pilot flags |
| 11 | No candidate | `NO_CANDIDATE` after pending cleared without emit |
| 12 | Single pending | `test_no_parallel_pending_in_pilot` |
| 13 | No lookahead | Close-only density replay |
| 14 | Reproducible order | Fixed `seq` in `lifecycle_events.csv` |

Run: `python3 -m pytest tools/phase_bt_d41_pending_lifecycle_v1/tests/test_pending_lifecycle_v1.py`

---

## 9. Artifacts

| Path | Content |
|------|---------|
| `tools/phase_bt_d41_pending_lifecycle_v1/pending_lifecycle_v1.py` | Review + time helpers |
| `tools/phase_bt_d41_pending_lifecycle_v1/run_lifecycle.py` | Pilot replay |
| `results/phase_bt_d41_pending_lifecycle_v1/lifecycle_events.csv` | All events (`seq`) |
| `results/phase_bt_d41_pending_lifecycle_v1/pending_orders.csv` | 102 created pending rows |
| `results/phase_bt_d41_pending_lifecycle_v1/cancel_reasons.csv` | Aggregated cancel reasons |
| `results/phase_bt_d41_pending_lifecycle_v1/reference_1535_timeline.json` | 15:35+ timeline |
| `results/phase_bt_d41_pending_lifecycle_v1/unresolved_fill_intervals.csv` | Intrabar touch flags |
| `results/phase_bt_d41_pending_lifecycle_v1/summary.md` | Machine summary |

**Re-run:** `python3 tools/phase_bt_d41_pending_lifecycle_v1/run_lifecycle.py`

---

## 10. Scope limits (unchanged production)

- No new ranking scores, entry/SL/TP rules, or filters
- BT-D26 run artifacts not overwritten
- `FILLED` and fill-vs-cancel ordering left for a later phase

---

**End of BT-D41.**
