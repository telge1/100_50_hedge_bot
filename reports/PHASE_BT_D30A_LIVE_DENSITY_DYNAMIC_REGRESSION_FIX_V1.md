# PHASE BT-D30A — Live Density + Dynamic Selector Regression Fix

**Date:** 2026-10-26  
**Dashboard:** `spread_recovery_hedge_short_dev/dashboard`  
**Live URL:** `http://dash.immotel.de:8080/live-charts/research` (:3000 backend)

---

## VERDICT

**`LIVE_DENSITY_DYNAMIC_SERVER_READY_BROWSER_NOT_VERIFIED`**

Server-side root causes addressed; dashboard **restarted** with correct `PYTHONPATH` (PID **2936957** on `:3000`). Research page still **401** without session — browser matrix not re-run here.

---

## A. Live stand (investigated)

| Check | Finding |
|--------|---------|
| Process | Was PID **2832562** (`python app.py`, cwd `dashboard/`) — **pre-BT-D30 Python** if not restarted after workspace/API changes |
| Template `research_charts.js` | **Before fix:** `?v=bt-d29-dynamic-selector-live-v1` |
| In-file `ASSET_V` (iframe) | **BT-D30:** `phase6xt-bt-d30-dynamic-display-v1` → **D30A:** `phase6xt-bt-d30a-live-v1` |
| Mismatch | Host JS cache key **bt-d29** while disk had **BT-D30** (`btD26DisplayMode`, `display_mode` on API) → inconsistent client behavior / stale bundle |
| Python `sync_bt_d26` | Source includes `display_mode`, `as_of_unix`, `make_vline` markers — **requires process reload** |
| `pool_pattern` import | `app.py` needs `PYTHONPATH` including `pools+ob+delta_bot` (restart without it **fails to bind**) |

---

## B. Root causes (confirmed)

### 1. Static asset cache incoherence (primary client regression)

`templates/research_charts.html` still loaded:

```html
research_charts.js?v=bt-d29-dynamic-selector-live-v1
```

while BT-D30 changed `research_charts.js` (display mode, as-of sync, density repaint). Browsers could keep an **old** `research_charts.js` under the **bt-d29** query string while rendering **new** HTML (`researchBtD26DisplayMode`), breaking initialization and overlay/density paths.

**Fix:** Unified cache bust **`phase6xt-bt-d30a-live-v1`** on all research scripts + `ASSET_V`.

### 2. BT-D26 loader UX / hang

`applyBtD26DynamicSelectorBacktester` awaited `zoomChartToIsoRange` **before** `setStatus(...geladen)`. `zoomChartToIsoRange` can run `reloadVisibleHistory` and leave the UI on **"Dynamic Selector: lade PLANNED setups …"** for a long time or indefinitely on failure.

**Fix:** Set success **status immediately after** load + overlay sync + schedule viewport; run zoom in **try/catch** afterward.

### 3. Density cleared after BT overlay sync

`refreshOverlaysVisible()` after BT-D26 viewport sync did not always restore canvas density (B-layer) on every path.

**Fix:** `repaintExactOriginDensityAllVisiblePanes()` after BT-D26 load and after viewport sync; existing `repaintExactOriginDensityFromCache` on `on_chart_ready` / `applyPaneBundle` / `loadOverlays` retained.

### 4. Stale Python (secondary)

If `:3000` was not restarted after BT-D30, `load`/`sync-viewport` with `display_mode` could **500** (`unexpected keyword argument`). New JS + old server → failed or silent viewport sync (`catch` swallowed errors).

**Fix:** Restart dashboard with:

`PYTHONPATH=spread_recovery_hedge_short_dev:dashboard:pools+ob+delta_bot`

---

## C. Density (server sanity, unchanged math)

- Window API / `build_landscape_window` unchanged.
- Repaint path: `PD1ExactOriginResearch.repaintPaneFromCache` → `setExactOriginDensity`.
- No SSOT/density math changes.

---

## D. BT-D30 dynamic selector (server simulation)

With current code + referenz run:

- **as-of 2026-09-11 15:35 UTC** → `planned_entry` **0.08681**, **1** box + **11** markers in ±8h viewport (`current_candidate`).

---

## E. Minimal code changes (D30A)

| File | Change |
|------|--------|
| `templates/research_charts.html` | All research script `?v=phase6xt-bt-d30a-live-v1` |
| `research_charts.js` | `ASSET_V` aligned; `repaintExactOriginDensityAllVisiblePanes`; BT-D26 status before zoom; density repaint after BT sync/load; viewport sync warns on error |
| `test_bt_d30a_live_regression_v1.py` | New regression tests |
| `test_bt_d30_clean_dynamic_setup_chart_v1.py` | Asset version assertion updated |

No strategy, entry/SL/TP, scanner, or density math changes.

---

## F. Verification matrix

| # | Case | Server | Browser |
|---|------|--------|---------|
| 1 | B ON, BT OFF → pools | repaint paths OK | NOT_VERIFIED |
| 2 | B OFF | — | NOT_VERIFIED |
| 3 | B ON + Dynamic Selector | load+sync OK | NOT_VERIFIED |
| 4 | Aktueller Kandidat ≤1 box | as-of logic OK | NOT_VERIFIED |
| 5 | Alle Setups | unchanged branch | NOT_VERIFIED |
| 6 | Mode switch | — | NOT_VERIFIED |
| 7 | No stuck "lade …" | status before zoom | NOT_VERIFIED |
| 8 | Pan/zoom | schedule + repaint | NOT_VERIFIED |
| 9 | 15:35 entries | 0.08681 / offset run separate file | NOT_VERIFIED |
| 10 | BT-D23 / BT-D12 | untouched | NOT_VERIFIED |
| 11 | No PnL on PLANNED | unchanged | NOT_VERIFIED |

**Post-deploy:** hard refresh or new `?v=phase6xt-bt-d30a-live-v1` (normal navigation after template deploy).

---

## G. Tests

```
test_bt_d30a_live_regression_v1.py — 3 passed
test_bt_d30_clean_dynamic_setup_chart_v1.py — 4 passed
```

---

## Stop

Diagnosis, minimal fix, server restart, and targeted tests complete.
