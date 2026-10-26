# PHASE 6XT — Independent 5m PD1 Exact Origin-Density Display Fix

**Date:** 2026-10-26  
**Dashboard:** `/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard`  
**Density SSOT path:** `pd1_landscape_viewer_v1.historical_window_v1.build_landscape_window` → `POST /live-charts/viewer/api/window`  
**Live:** `http://dash.immotel.de:8080/live-charts/research` (→ app `:3000`)

---

## VERDICT

**`INDEPENDENT_DENSITY_SERVER_READY_BROWSER_NOT_VERIFIED`**

- **Server / API / canonical data:** PASS — 5m PD1 window builder returns rich pool payloads for Sep-2026 replay windows; no SSOT or density-math changes.
- **Client fix:** applied in dashboard JS (see below); static assets served from disk (no Python restart required for JS).
- **Browser:** `GET /live-charts/research` returns **401 Not authenticated** without session; automated UI could not exercise B-layer toggles or canvas draw. **BROWSER: NOT_VERIFIED.**

---

## A. Reproduction target (user scenario)

| Setting | Value |
|--------|--------|
| Symbol | DOGEUSDT |
| Pane TF | 5m |
| Pool detail | PD1 (checkbox default **checked** in template) |
| B – Exact Origin-Density | ON |
| Min origins | 2 (slider) |
| Historical LLD V2 | OFF |
| Backtester | NOT loaded |
| Replay | ~2026-09-11 12:00 UTC |

**Expected:** upper/lower density bands without any backtester overlay.

---

## B. Data path (traced)

```
researchIndExactOriginDensity (checkbox)
  → PD1ExactOriginResearch.refreshPane / scheduleViewport
  → resolveViewSec (replay GO TO | viewport | candles | chart range)
  → buildWindows + fetchWindow
  → POST /live-charts/viewer/api/window { from_utc, to_utc, timeframe: "5m", pool_detail: 1 }
  → build_landscape_window (168h warmup inside server)
  → PD1ExactDensityTrpAdapter.poolsToExactOriginZones → mergeExactOriginSegments
  → chart.setExactOriginDensity (canvas #eod-density-overlay)
```

**Mutual exclusion:** enabling B disables Historical LLD V2 (`disableHistoricalLmv2`); 5m pane never used `paneSupportsHistoricalLmv2` (15m PD10 static path).

---

## C. Data parity (API, read-only)

`HISTORICAL_LMV2_BATCH_READER=1`, line `historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1`, capabilities **2026-05-01 … 2026-09-30** (5m PD1).

| Reference UTC | ±4h window | `pools` in response | `prices` bars |
|---------------|-----------|---------------------|---------------|
| 2026-09-11 12:00 | 08:00–16:00 | 261 | 97 |
| 2026-09-11 15:35 | 11:35–19:35 | 259 | 97 |
| 2026-09-11 17:00 | 13:00–21:00 | 230 | 97 |

**Conclusion:** API supplies valid band inputs; failure mode is **host/iframe render and overlay sync**, not missing CH/catalog data for Sep-11 replay.

---

## D. Root cause (proven)

1. **Canvas path** (`localStorage phase6xt_eod_density_canvas_v1` default ON) keeps density in the **iframe canvas** (`eodDensityPayload`), not in DOM zone overlays (`PD1_EXACT_ORIGIN_DENSITY_V1`).
2. **`refreshPane`** fills `pane.exactOriginSegmentCache` and calls `pushCanvasDensity` → `setExactOriginDensity`.
3. **`loadOverlays` / `refreshOverlaysVisible`** (backtester strategy changes, BT-D23/D26 viewport sync, workspace reload) call **`syncOverlays`** on the iframe. That relayouts overlays but **does not** restore canvas density from host cache.
4. **Race / handshake:** if density was fetched while `chartApi` was unavailable, cache could be populated without a successful `setExactOriginDensity`; **`on_chart_ready`** previously did not **`repaintPaneFromCache`**.

**Not the cause:** backtester requirement for pools, Historical LLD on 5m, or empty window API for Sep-11.

---

## E. Minimal fix (dashboard only)

**Repo:** `spread_recovery_hedge_short_dev/dashboard` (working tree; not committed in this phase).

### 1. `pd1_exact_origin_density_research_v1.js` (already in tree)

- Export **`repaintPaneFromCache(pane)`** — re-push cached segments/zones through `pushCanvasDensity` / `mergeOverlays` with min-depth filter.

### 2. `research_charts.js`

- Add **`repaintExactOriginDensityFromCache(pane)`** helper.
- Call after:
  - **`attachBridge` → `on_chart_ready`** (after `syncOverlays`)
  - **`applyPaneBundle`** (after `syncOverlays`)
- **`loadOverlays`:** when canvas mode + cache hit, **`repaintExactOriginDensityFromCache`** instead of forcing a full CH **`refreshPane`** (avoids races and keeps B-layer independent of overlay HTTP).

### 3. Tests

- `test_pd1_density_research_live_integration_v1.py` — asserts wiring for `repaintExactOriginDensityFromCache` and `repaintPaneFromCache`.
- **`pytest`:** 7 passed (integration + 5m window readback).

**Explicitly unchanged:** pool definitions, LMV2 Short V2, backtester logic, `build_landscape_window` math, BT-D23/D26 runs.

---

## F. Live verification matrix

| # | Case | Server-side | Browser |
|---|------|-------------|---------|
| 1 | B ON, Backtester OFF → pools visible | API OK | NOT_VERIFIED |
| 2 | B OFF → no bands | N/A | NOT_VERIFIED |
| 3 | B ON + Backtester ON → pools + boxes | overlay merge preserves EOD flag | NOT_VERIFIED |
| 4 | Backtester OFF while B ON → pools remain | repaint from cache on `loadOverlays` | NOT_VERIFIED |
| 5 | Pan/zoom → bands update | `scheduleViewport` → `refreshPane` | NOT_VERIFIED |
| 6 | Depth=2 | adapter `filterSegmentsByMinOrigins` | NOT_VERIFIED |
| 7 | 5m PD1 historical | CH window OK | NOT_VERIFIED |
| 8 | Historical LLD V2 OFF | mutual exclusion | NOT_VERIFIED |
| 9 | No lookahead | server `known_at` contract unchanged | NOT_VERIFIED |

**Process check:** `python` PID **2832562** listening on **0.0.0.0:3000**; root URL **302**; research **401** without auth.

---

## G. Manual browser checklist (after login)

1. DOGEUSDT, pane **5m**, **PD1** checked, **B ON**, Historical LLD **OFF**, min origins **2**.
2. GO TO **2026-09-11 12:00 UTC** — hint should show `B – Exact Origin · canvas … seg … pools …`.
3. Toggle backtester strategy / **refresh overlays** — bands must **stay** (cache repaint).
4. Turn **B OFF** — bands clear; turn **B ON** — bands return without loading backtester.

---

## H. Stop condition

Targeted client decoupling fix + server/API parity checks complete. No strategy, entry/SL/TP, or SSOT density changes.
