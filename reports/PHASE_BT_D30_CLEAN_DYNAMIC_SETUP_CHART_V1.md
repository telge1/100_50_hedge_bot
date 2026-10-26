# PHASE BT-D30 — Clean Dynamic Short Setup Chart View

**Date:** 2026-10-26  
**Dashboard:** `spread_recovery_hedge_short_dev/dashboard`  
**Scanner catalog:** `pools+ob+delta_bot/results/setup_scanner_runs/bt_d26_dynamic_referenz_sep11_24h_v1/setups.json`  
**Strategy ID (unchanged):** `pool_dynamic_selector_bt_d26`

---

## VERDICT

**`CLEAN_DYNAMIC_CHART_SERVER_READY_BROWSER_NOT_VERIFIED`**

- Server logic + unit tests: **PASS**
- Research UI requires auth (`401` on `/live-charts/research` without session): **BROWSER NOT_VERIFIED**
- No scanner re-run, no entry/SL/TP rule changes, no density/SSOT changes

---

## A. Two display modes (BT-D26 only)

| Mode | Value | Behavior |
|------|--------|----------|
| **Aktueller Kandidat** (default) | `current_candidate` | ≤1 PLANNED short position box + prior candidates as **vline markers** |
| **Alle Setups** | `all_setups` | Previous viewport behavior (all setups intersecting view) |

UI: `researchBtD26DisplayMode` in `templates/research_charts.html`  
Persistence: `localStorage` key `researchBtD26DisplayMode`  
Cache bust: `ASSET_V = phase6xt-bt-d30-dynamic-display-v1`

BT-D23, BT-D12, and other strategies: **unchanged**.

---

## B. Causal “current candidate” (replay as-of)

Resolution uses embedded **`candidate_transitions`** (288 bars) — not “last setup in catalog”.

Algorithm (`resolve_active_candidate_at_asof`):

1. Walk transitions with `decision_utc` ≤ `as_of_unix`.
2. On `CANDIDATE_INVALIDATED` / `NO_CANDIDATE` → clear active candidate.
3. On `emit_setup` with `current` → bind setup from `setups[]` at same `decision_utc`.
4. On `CANDIDATE_STILL_VALID` → keep prior emitted setup.
5. Missing setup row for `emit_setup` → **`UNRESOLVED`** (no guess).

**Replay as-of:** `replayTargetTs` when historical replay active; else viewport end (causal, no lookahead).

**Reference checks (referenz run):**

| as-of UTC | Status | planned_entry |
|-----------|--------|----------------|
| 2026-09-11 11:55 | NONE | — |
| 2026-09-11 12:00 | ACTIVE | 0.08365 |
| 2026-09-11 15:35 | ACTIVE | 0.08681 |
| 2026-09-11 17:00 | ACTIVE | 0.08593 |

---

## C. Historical markers

- Prior emitted setups (`decision_utc` ≤ as-of, ≠ active) → **`btd26-marker-*`** vlines in viewport.
- Tooltip: planned entry/SL/TP + pool bounds (`setup_to_marker_spec`).
- Active candidate → single **`btd26-planned-*`** short position box (4h preview window unchanged).

---

## D. Density independence

BT-D26 sync still ends with `refreshOverlaysVisible()` + existing exact-origin **repaint from cache** (Phase 6XT). No changes to `build_landscape_window` or B-layer JS logic.

---

## E. Candidate detail panel

`#researchBtD26CandidateDetail` shows for **current_candidate** mode:

- PLANNED SHORT — NOT EXECUTED
- as-of UTC, status (ACTIVE / NONE / UNRESOLVED)
- pool lower/upper, origins, gap, entry mode/offset, entry/SL/TP, selection_reason, dynamic_state, invalidation

No PnL, fills, or USDT.

---

## F. Read-only episode analysis (102 setups)

`analyze_setup_episode_transitions` on referenz catalog:

| selection_reason | Count | Interpretation |
|------------------|------:|----------------|
| `LOWER_ELIGIBLE_UPPER_POOL_CHANGED` | 71 | Zone change — lower eligible upper moved |
| `RESELECT_AFTER_INVALIDATION_BT_D21_LOWEST` | 30 | Re-select after invalidation |
| `FIRST_DYNAMIC_CANDIDATE_BT_D21_LOWEST` | 1 | First select in window |

Per-episode flags: `pool_changed_vs_previous`, `origin_set_changed_vs_previous` (no new filters).

---

## G. Tests

`dashboard/test_bt_d30_clean_dynamic_setup_chart_v1.py` — **4 passed**

- UI wiring + asset version
- as-of resolution + clean view (1 box + markers)
- episode counts
- BT-D23 module contract untouched

**Manual matrix (post-login):**

| Case | Expected |
|------|----------|
| Replay before first setup | NONE, no box |
| Replay at 15:35 | one box @ 0.08681 |
| Invalidation window | NONE until re-select |
| Next candidate | new box, old → marker |
| Pan/zoom | as-of from replay or view end |
| Alle Setups | 102-style multi-box viewport |
| B ON/OFF | density unchanged |
| BT-D23 / BT-D12 | unchanged |

---

## H. Files touched (display only)

| File | Change |
|------|--------|
| `research_charts/bt_d26_dynamic_selector_backtester.py` | as-of resolve, markers, clean view, episode analysis |
| `research_charts/workspace_session.py` | display modes in sync/load |
| `research_charts/api.py` | `display_mode`, `as_of_unix` on load/sync |
| `templates/research_charts.html` | mode select + detail panel |
| `static/js/research/research_charts.js` | wiring, GO TO refresh, ASSET_V bump |
| `test_bt_d30_clean_dynamic_setup_chart_v1.py` | new |

---

## Stop

UI integration + automated tests complete. No strategy, scanner recompute, or density changes.
