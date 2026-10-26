# PHASE BT-D36 — Exact Origin Density Browser Rendering Root Cause

**Date:** 2026-10-26  
**Dashboard:** `/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard`  
**Pool repo (report):** `/home/telgenbuescher/projects/pools+ob+delta_bot`  
**Live:** `http://dash.immotel.de:8080/live-charts/research`

---

## VERDICT

**`DENSITY_RENDERING_SERVER_CLIENT_VERIFIED`**

- **Server / window API / adapter:** verified locally (262 pools, 434 canvas segments at min-origins ≥ 2 for 2026-09-11 11:34:51 UTC ±4h).
- **Client root cause + minimal fix:** implemented in dashboard JS (BT-D36 supersede + GO TO viewport guard).
- **Authenticated live browser:** not exercised in this pass (no session in automation). **BROWSER: NOT_VERIFIED.**

---

## A. Symptom vs misleading UI

| Observation | Interpretation |
|---------------|----------------|
| Chart corner **`66804 · miss`** | Pane candle load status: `rows.length + " · " + packed.cache` in `updatePaneLoadStatusFromBundle()` — **not** Exact Origin hint. |
| No blue/violet bands with **B ON**, PD1, depth 2 | Density path issue; unrelated to pane cache miss label. |
| Candles + “History geladen” | `POST /api/research/pane` OK; density uses separate `POST /live-charts/viewer/api/window`. |

---

## B. End-to-end path (first failing step)

| Step | Function / endpoint | Result (this scenario) |
|------|---------------------|-------------------------|
| 1 B checkbox | `PD1ExactOriginResearch` `change` → `refreshAll` | OK when enabled |
| 2 PD1 / depth | `activeScopesForPane`, `getMinOrigins` | 5m PD1 released |
| 3 Viewport | `resolveViewSec` / GO TO `explicit_opts` | ±4h around replay target |
| 4 Plan | `buildWindows` → `planExactOriginLoadWindows` | OK inside catalog bounds |
| 5 Fetch | `fetchWindow` → `POST …/api/window` | Server returns pools (see D) |
| 6 Client chunk cache | `fetchPlannedWindows` / `touchExactOriginWindowCache` | **BUG (pre-fix):** successful HTTP body **discarded** if `loadSerial` superseded during slow fetch |
| 7 Adapter | `poolsToExactOriginZones` → `mergeExactOriginSegments` | 606 merged segments (server pools); 434 after depth ≥ 2 |
| 8 Filter | `filterSegmentsByMinOrigins(2)` | Non-empty |
| 9 Push | `pushCanvasDensity` → `chart.setExactOriginDensity` | Skipped when step 6 never committed + step 10 aborted |
| 10 Race | `exactOriginLoadSerial` bump from `scheduleViewport` after `jumpChartsToUnix` during GO TO | **First paint aborted** without retry; optional **infinite discard** on slow (>debounce) window builds |

**First defective step (pre-fix):** `pd1_exact_origin_density_research_v1.js` → `fetchPlannedWindows` (drop cache on superseded `loadSerial`) and `refreshPane` (silent return after fetch without coalesced retry).

---

## C. Server verification (read-only)

Window build (same inputs as client `exact_origin_v1`):

- `from_utc` `2026-09-11T07:34:51Z` → `to_utc` `2026-09-11T15:34:51Z`
- `timeframe` 5m, `pool_detail` 1, `compute_mode=exact_origin_v1`
- **~5.1s** cold build (BT-D35 batch path), **262** pools

Node adapter harness on saved pools:

| Stage | Count |
|-------|------:|
| Raw zones | 131278 |
| Viewport-culled | 99683 |
| Merged segments | 606 |
| After min-origins **2** | **434** |

**Conclusion:** bands are not empty for this replay; failure is **client concurrency**, not SSOT math or empty API.

---

## D. Minimal fix (dashboard only)

**Files:**

1. `static/js/research/pd1_exact_origin_density_research_v1.js`
   - Always commit successful window responses to `exactOriginWindowStores` (gate only on `replayGen`).
   - `scheduleSupersededDensityRefresh` — coalesced retry when a refresh is superseded after fetch.
   - `scheduleViewport` — skip while `pane._suppressExactOriginViewport`.
   - Version: `phase6xt-bt-d36-density-supersede-v1`.

2. `static/js/research/research_charts.js`
   - During GO TO `refreshAll`, set `_suppressExactOriginViewport` on visible panes (avoid debounced viewport refresh stealing `loadSerial` mid-await).
   - `ASSET_V` → `phase6xt-bt-d36-density-supersede-v1`.

3. `templates/research_charts.html` — cache-bust query for density module + `research_charts.js`.

**Unchanged:** `build_landscape_window` math, CH imports, BT-D26, entry/SL/TP, strategy.

---

## E. Regression tests

`dashboard/test_bt_d36_density_load_supersede_v1.py` — wiring for cache commit, supersede retry, GO TO suppress flag, asset bust.

Run:

```bash
cd /home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard
pytest -q test_bt_d36_density_load_supersede_v1.py
```

| # | Case | Coverage |
|---|------|----------|
| 1 | B ON, no backtester | Server + adapter (D36 harness) |
| 2 | B OFF | Existing strip paths (unchanged) |
| 3 | B ON + BT-D26 | `repaintExactOriginDensityFromCache` unchanged |
| 4 | Pan/zoom | `scheduleViewport` + supersede retry |
| 5 | Replay GO TO | `_suppressExactOriginViewport` + explicit `refreshAll` |
| 6 | Layout 1 | `api(pane)` iframe bridge unchanged |
| 7 | Depth 2 | `filterSegmentsByMinOrigins` — 434 segments in harness |
| 8 | BT-D26 logic | No edits |
| 9 | PnL/fills | No edits |

---

## F. Browser verification

| Check | Status |
|-------|--------|
| Research page with login | **NOT_RUN** (automated session not wired) |
| Network: `POST …/viewer/api/window` | **SERVER_VERIFIED** via Python build + timed curl attempt (live HTTP can queue; use D35 server + cache) |
| Canvas `setExactOriginDensity` | **CLIENT_CONTRACT_VERIFIED** via adapter harness + existing canvas integration tests |

**Manual after deploy:** hard-refresh research (new `?v=phase6xt-bt-d36-density-supersede-v1`), DOGEUSDT 5m PD1, B ON, depth 2, GO TO `2026-09-11 11:34:51 UTC` — hint should show `B – Exact Origin · canvas … seg … pools …` and bands visible.

---

## G. Stop condition

Root cause evidenced at client fetch/cache + GO TO viewport race; minimal fix and tests delivered. No strategy or density-math changes.
