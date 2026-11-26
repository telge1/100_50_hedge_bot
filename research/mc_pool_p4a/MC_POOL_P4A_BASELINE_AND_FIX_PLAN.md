# MC-POOL-P4A — Clean Baseline & Cache Lifecycle Fix Preparation

**Endstatus:** `MC_POOL_P4A_CLEAN_BASELINE_READY`  
**Datum:** 2026-11-26  
**SSOT:** `spread_recovery_hedge_short_dev/dashboard/`  
**Vorgänger:** [MC-POOL-P3 Audit](../mc_pool_p3/MC_POOL_P3_TARGETED_CACHE_PROCESSING_AUDIT.md)

**Keine produktiven Codeänderungen in dieser Phase.**

---

## 0. Executive Summary

Diese Phase liefert (1) einen **geprüften P4A-Browser-Harness** für eine **einzelne Messsession** mit Export **einer** JSON-Datei (Tests A–H), und (2) einen **minimalen Fix-Plan** für P4B, der Darstellung, Window-Chunks, Segment-/Sweep-Cache und Invalidierung **trennt**, ohne Pool-Mathematik oder Server-Pfad zu ändern.

**P4B-Akzeptanzziele (noch UNGEMESSEN):** Bei gültigem Cache OFF→ON → 0 Window-POSTs, keine redundante Aufbereitung, identische Landschaft, Ziel **< 200 ms** bei gültigem Segment-Cache.

---

## A. Korrigierte Baseline-Messung

### A.1 Harness-Review (P3 → P4A)

| Anforderung | P4A-Status |
|-------------|------------|
| Kein mehrfaches PerformanceObserver ohne Disconnect | **Ja** — `disconnectObservers()` vor Neu-Install; `stop()` disconnect |
| Keine alten buffered Long Tasks | **Ja** — `buffered: false` + Filter `startTime >= sessionPerfStart` |
| Echte Entry-Timestamps | **Ja** — `longTaskRelMs(entry)` aus `entry.startTime` |
| HTTP Starts / Completions / Errors / Aborted getrennt | **Ja** — `http_log[].kind` |
| Kein Body-Parse im Standard | **Ja** — `parseWindowBodies: false` (Default) |
| SOLUSDT + Chart 5m + Pool 5m PD1 | **Ja** — `validateTargets()` via `__researchDebug` + DOM |
| Kein Eingriff in Pool-Pipeline-Logik | **Ja** — nur fetch-Wrap + `refreshPane`/`scheduleViewport` Wrap (restored on `stop()`) |
| Live vs. User für HTTP | **Ja** — `userClick()` / `markUserInteraction` → Feld `trigger` |

**Quelldatei (Repo):** `research/mc_pool_p4a/MC_POOL_P4A_BROWSER_MEASUREMENT_CONSOLE.js`  
**API:** `window.__MC_POOL_P4A__`

### A.2 Manuelle Testanleitung — eine Session, ein Export

**Setup (vor Harness):** `/live-charts/research`, SOLUSDT, Single Chart, Pane-TF **5m**, Exact Origin **an**, Pool-TF **5m**, **PD1**, Historical LMv2 **aus**.

1. DevTools → **Console (Top-Frame)** → Harness aus Anhang unten einfügen (Enter).
2. `__MC_POOL_P4A__.start()` — Konsole muss `setup check OK` zeigen.
3. Für **jede** manuelle UI-Aktion zuerst: `__MC_POOL_P4A__.userClick()`
4. Pro Test: `startSection` → Aktion → kurz warten → `sampleViewport` → `endSection`

| Schritt | Konsole | UI (nach `userClick()`) |
|---------|---------|-------------------------|
| A Baseline | `startSection('A','initial',{interaction:'user'})` | Pools bereits an, warten bis stabil |
| | `sampleViewport('A_end'); endSection()` | |
| B OFF→ON | `startSection('B','toggle_off_on',{interaction:'user'})` | Exact Origin aus, dann wieder an |
| | `sampleViewport('B_end'); endSection()` | |
| C Slow pan | `startSection('C','slow_pan',{interaction:'user'})` | Langsam horizontal schieben |
| | `sampleViewport('C_end'); endSection()` | |
| D Fast pan | `startSection('D','fast_pan',{interaction:'user'})` | Schnell bis neuer Tile sichtbar |
| | `sampleViewport('D_end'); endSection()` | |
| E H-Zoom | `startSection('E','h_zoom',{interaction:'user'})` | Zeitachse zoomen |
| | `sampleViewport('E_end'); endSection()` | |
| F V-Zoom | `startSection('F','v_zoom_price_scale',{interaction:'user'})` | Preisskala zoomen |
| | `sampleViewport('F_end'); endSection()` | |
| G Neuer Tile | `startSection('G','historical_tile',{interaction:'user'})` | Pan bis **neuer** historischer Bereich |
| | `sampleViewport('G_end'); endSection()` | |
| H Live 5m | `startSection('H','live_5m_close',{interaction:'live'})` | **Keine** UI — bis nächster 5m-Close (max ~6 min) |
| | `sampleViewport('H_end'); endSection()` | |

5. `__MC_POOL_P4A__.exportJSON()` — eine Datei `MC_POOL_P4A_SOLUSDT_5M_*.json`
6. `__MC_POOL_P4A__.stop()` — fetch/original refresh wiederhergestellt

**Auswertung in JSON:**

- Pro Test: `sections[].pool_window_started` / `pool_window_completed` / `pool_window_aborted`
- Global: `http_summary.pool_window_started_by_trigger.user` vs `live_or_background`
- Test H: Pool-POSTs mit `trigger: live_or_background` und `section_id: "H"` (Candles/Live-Poll separat unter `candles` / `live_status`)

**Hinweis Test H (CONFIRMED Architektur):** Live-Poll (`refreshLiveBar`, `pollIncremental`) aktualisiert Kerzen; Exact Origin wird **nicht** pro 5m-Close automatisch refreshed. Erwartung: **0** `pool_window`-Starts in H, sofern kein Viewport-Event folgt — Abweichung dokumentieren.

### A.3 Metriken pro Sample

`sampleViewport` erfasst: `last_metrics` (adapterMs, rawZones, sweepCacheHit, windowFetch), `canvas_draw`, `chunk_stores` (über `window.__researchDebug`).

---

## B. Minimaler Cache-Lifecycle-Fix (P4B-Vorschlag, noch nicht implementiert)

### B.1 Bestätigte OFF→ON Call-Chain (Spread SSOT)

```
#researchIndExactOriginDensity change (pd1_exact_origin_density_research_v1.js L2080)
  OFF → stripAllPanes() → stripPane (L727)
        → clearCanvasOnPane (L591)
        → resetAllWindowStores (L542)   // Chunks + Segment + Sweep weg
  ON  → refreshAll({ force: true }) (L2093)

Multi-Manual: research_charts.js setMultiPaneExactOriginEnabled (L271)
  OFF → stripPane
  ON  → refreshPane(pane, { force: true })
```

### B.2 Zustände trennen — Design-Skizze P4B

| Zustand | Heute | P4B-Vorschlag |
|---------|--------|----------------|
| 1. Darstellung entfernen | `stripPane` = Canvas + Store-Reset | **`stripPaneVisual`**: nur `clearExactOriginDensity` + Overlay-Strip |
| 2. Window-Daten behalten | bei OFF gelöscht | OFF (ohne Scope-Wechsel): **Chunk-Store LRU behalten** |
| 3. Segment-Cache behalten | gelöscht | Optional behalten wenn `scopeKey`+`renderKey` noch gültig |
| 4. Veraltete Daten | Reset „alles“ | Gezielt: Symbol, PD, Pool-TF, TF, `replayGen`, Bounds-Advance |
| 5. Inflight / Generation | `loadSerial`, `inflight` Map | **unverändert** — weiterhin supersede |

### B.3 `clearCanvasOnPane` ohne `resetAllWindowStores`?

**Ja, technisch** für reinen Canvas-Clear (`chart.clearExactOriginDensity` L593) — **CONFIRMED**.

**Zwingend mit vollem Reset** heute an:

| Aufrufer | Grund |
|----------|--------|
| `stripPane` (Toggle OFF) | sollte P4B → nur Visual |
| `refreshPaneImpl` NO_INTERSECTION / empty (L1494, L1541) | UI leer, **SUSPECTED**: Chunks könnten bleiben, Canvas leer |
| PERF segment cap (L1733) | Canvas leer |
| Nicht-canvas Pfad (L1785) | Overlay leer |
| `onPaneSymbolChange` (L2164) | löscht nur Canvas — **ohne** `resetAllWindowStores` (nur Segment/Sweep null) |

**Explizit voller Reset:** `resetAllWindowStores`, `onSymbolSwitch` → `stripAllPanes` (wenn EO an), Scope/`replayGen`-Mismatch (L1554–1558), PD/Pool-TF change (strip + `invalidateScopeCaches`).

**Neue Funktion (Vorschlag):**

```javascript
function clearCanvasOnPaneOnly(pane) { /* chart.clear — NO reset stores */ }
function invalidateExactOriginPaneData(pane, reason) { resetAllWindowStores(pane); bumpExactOriginLoadSerial(...); }
```

Toggle OFF ruft `clearCanvasOnPaneOnly`; Symbolwechsel ruft `invalidateExactOriginPaneData`.

### B.4 `force: true` ersetzen bei OFF→ON?

**Heute blockiert `force`:**

- Segment-Fast-Path (L1578–1602): `!(opts && opts.force)`
- Sweep-Cache (L1674, L1705): `!(opts && opts.force)`

**Vorschlag P4B:** Checkbox-ON / Multi-ON ohne Scope-Änderung:

```javascript
const warm =
  exactOriginWindowsFullyCached(...) &&
  pane.exactOriginSegmentCache &&
  viewportRenderKey(hit) === pane.exactOriginSegmentCache.key; // optional
await refreshPane(pane, warm ? {} : { force: false });
// oder: repaintPaneFromCache wenn renderKey match
```

**`force: true` beibehalten bei:** Symbol (`onSymbolSwitch`), GO (`research_charts.js` L2281), Indicators-Refresh (L10325), TF-Wechsel (`onPaneTimeframeChange` L2204), PD/Pool-TF (L2134+), Overlay-Rebuild (L10375), Pane-Boot (L9770, L11045).

### B.5 Live-Publication während Pools OFF

**CONFIRMED:** Kein Hintergrund-`refreshPane` wenn Exact Origin aus (`isEnabled()` / `exactOriginEnabledForPane` false). Chunks im RAM (nach P4B) könnten älter sein als `data_available_to`.

**Invalidierung bei Re-ON:**

- `ensurePaneViewerBounds` (L685) + `loadCaps` — wenn `hiMs` gewachsen → `loadSpan` größer → ggf. **ein** neuer Chunk nötig (legitim, kein voller Refetch aller).
- Optional P4B: Wenn `caps.data_available_to` > cached max `ent.toMs` → nur fehlende Tiles planen.

### B.6 Symbolwechsel — keine alten Pools

**CONFIRMED:** `switchSymbol` ruft nur `onSymbolSwitch` wenn EO **an** (L10818). Wenn EO aus: alte Chunks blieben ohne P4B im RAM — **darum** bei Symbolwechsel immer `invalidateExactOriginPaneData` pro Pane (auch wenn EO aus), oder Scope-Key enthält Symbol (`exactOriginScopeKeyForScope`) sodass neuer Key Store leer ist — **Scope-Key enthält Symbol** (**CONFIRMED** in Adapter) → alter Store unter altem Key, neuer Lookup leer → **kein Cross-Symbol-Render**, aber **RAM** bis LRU.

Bei EO-an: `onSymbolSwitch` → `stripAllPanes` + `invalidateScopeCaches` + Serial bump.

### B.7 LRU / RAM-Limits

| Limit | Wert | Ort |
|-------|------|-----|
| Window-Chunks pro Scope | **32** | `EOD_WINDOW_CHUNK_CACHE_MAX` (`pd1_exact_density_trp_adapter.js` L306) |
| Inflight dedupe | pro Request-Key | `fetchWindow` L1264–1328 |
| Segment/Sweep | 1 pro Pane | Pane-Properties |

Keine unbeschränkten Caches — P4B darf LRU nicht aushebeln.

### B.8 P4B-Änderungsdiff (minimal, Spread)

1. `pd1_exact_origin_density_research_v1.js`: split `clearCanvasOnPane` / `stripPaneVisual`; Toggle OFF nutzt Visual-only.
2. `install` + `setMultiPaneExactOriginEnabled`: ON ohne `force` wenn `exactOriginWindowsFullyCached` + optional Segment-Key.
3. Keine Änderung an `poolsToExactOriginZones`, Server, CH.

**Risiko:** Stale Chunk nach Publication — mit Bounds/Caps-Vergleich absichern (Equality E6).

---

## C. Verbindlicher Equality-Testplan (vor/nach P4B)

### C.1 Segment-/Pool-Inhalt (nicht nur Counts)

| ID | Szenario | Prüfung |
|----|----------|---------|
| EQ-01 | OFF→ON gleicher Viewport | `sampleViewport` A vs B: gleiche `mergedSegments`, `rawZones`, `pools` in metrics; optional P4B mit `parseWindowBodies: true` einmalig: Pool-ID-Set Hash |
| EQ-02 | Pool-IDs / Lifetimes | Stichprobe: 20 `pool_id` aus Response, `segments[].metadata` depth/color — bytegleich |
| EQ-03 | Density-Werte | Gleiche `top`/`bottom` pro `pool_id` in overlapping window |
| EQ-04 | Publication während OFF | Nach H: Re-ON → neuer Close im sichtbaren Bereich sichtbar (kein permanentes Loch) |
| EQ-05 | Symbolwechsel während OFF | Re-ON SOL: kein BNB/DOGE Segment |
| EQ-06 | TF-Wechsel während OFF | Full reset, neuer TF korrekte Caps |
| EQ-07 | Schnelles OFF→ON×3 | 0 POST nach erstem Load; kein Serial-Race (`loadSerial` monoton) |
| EQ-08 | OFF während inflight | Kein Render aus alter Serial; `refreshPane_error`/`superseded` ok |
| EQ-09 | LRU Eviction | Pan weit → ältester Chunk drop; zurückpan → max 1 POST für Tile |
| EQ-10 | Multi-Pane | Pane-1 OFF ohne Pane-2 Store zu löschen |
| EQ-11 | Stale Response | Ältere `loadSerial` zeichnet nicht nach (`async_superseded`) |

### C.2 Werkzeuge

- P4A JSON `refresh_runs[].metrics`
- Optional: `DogeExactOriginTrace` Stage `refreshPane_render`
- Post-Fix: gleiche Session A–B wie Baseline-JSON diff

---

## D. Unveränderliche Anforderungen

Wie MC-POOL-P3 — kanonisch 5m PD1 ACTIVE SSOT, D2 Density, keine Pool-Math-/CH-/Trading-Änderungen, MC-LOAD-P4J unberührt.

---

## E. P4B-Akzeptanzziele (UNGEMESSEN)

Bei gültigem Cache nach OFF→ON (gleicher Scope):

- 0 `pool_window` POSTs
- `windowFetch.fetched === 0`, `cacheHits === planned`
- `refreshPane` Nutzung Segment-Cache oder `repaintPaneFromCache`
- Visuelle Gleichheit (EQ-01)
- Wall-clock Re-Enable **< 200 ms** (wenn Segment-Cache hit) — **NOT MEASURED**

Pan/Zoom-Optimierung (Sweep-Key / viewport-only) ist **P4C-Kandidat**, nicht Teil des minimalen Lifecycle-Fix.

---

## Anhang — Vollständiger Browser-Harness (kopierfertig)

In DevTools **Top-Frame** einfügen:

```javascript
/**
 * MC-POOL-P4A — DevTools harness (TOP frame, /live-charts/research).
 * Read-only: patches fetch + wraps refreshPane until stop(); restores on stop().
 */
(function () {
  "use strict";
  if (window.__MC_POOL_P4A__) {
    console.warn("[MC-POOL-P4A] already installed — __MC_POOL_P4A__.stop() first");
    return window.__MC_POOL_P4A__;
  }

  var SCHEMA = "mc_pool_p4a_v1";
  var t0 = performance.now();
  var sessionPerfStart = 0;
  var active = false;
  var parseBodies = false;

  var sections = [];
  var currentSection = null;
  var httpLog = [];
  var httpPending = Object.create(null);
  var refreshRuns = [];
  var viewportSchedules = [];
  var viewportSamples = [];
  var longTasksDedup = Object.create(null);
  var longTasks = [];
  var frameTimes = [];
  var traceLines = [];
  var coalesceMarks = [];
  var userInteractionUntil = 0;

  var poLong = null;
  var poResource = null;
  var rafLoopId = null;
  var fetchPatched = false;
  var origFetch = null;
  var wrapsInstalled = false;
  var origRefreshPane = null;
  var origScheduleViewport = null;
  var origConsoleLog = null;

  function relMs(perfNow) {
    return Math.round(perfNow - t0);
  }

  function longTaskRelMs(entry) {
    if (!entry || !Number.isFinite(entry.startTime)) return relMs(performance.now());
    return Math.round(entry.startTime - sessionPerfStart);
  }

  function markUserInteraction(ms) {
    userInteractionUntil = performance.now() + (ms == null ? 2500 : ms);
  }

  function httpTriggerTag() {
    return performance.now() <= userInteractionUntil ? "user" : "live_or_background";
  }

  function classifyUrl(url) {
    var u = String(url || "");
    if (u.indexOf("/live-charts/viewer/api/window") >= 0) return { category: "pool_window", pool: true };
    if (u.indexOf("/live-charts/viewer/api/capabilities") >= 0) return { category: "pool_capabilities", pool: true };
    if (u.indexOf("viewer-bounds") >= 0) return { category: "pool_bounds", pool: true };
    if (u.indexOf("/api/research/live-status") >= 0) return { category: "live_status", pool: false };
    if (u.indexOf("/api/research/pane") >= 0 || u.indexOf("/api/research/candles") >= 0) {
      return { category: "candles", pool: false };
    }
    if (u.indexOf("/api/research/") >= 0) return { category: "research_other", pool: false };
    return { category: "other", pool: false };
  }

  function bumpSection(name, extra) {
    if (!currentSection) return;
    currentSection.events.push(
      Object.assign({ t_rel_ms: relMs(performance.now()), event: name }, extra || {})
    );
  }

  function startSection(id, label, meta) {
    if (currentSection) endSection({ auto_closed: true });
    meta = meta || {};
    currentSection = {
      id: id,
      label: label,
      interaction: meta.interaction || "user",
      started_ms: relMs(performance.now()),
      ended_ms: null,
      events: [],
      http_idx_start: httpLog.length,
    };
    bumpSection("section_start", { interaction: currentSection.interaction });
    console.log("[MC-POOL-P4A] ▶", id, "—", label, "interaction=" + currentSection.interaction);
    return currentSection;
  }

  function endSection(extra) {
    if (!currentSection) return null;
    var idx = currentSection.http_idx_start;
    var slice = httpLog.slice(idx);
    var poolStarted = 0;
    var poolCompleted = 0;
    var poolErrors = 0;
    var poolAborted = 0;
    var liveStarted = 0;
    slice.forEach(function (r) {
      if (r.category === "pool_window") {
        if (r.kind === "started") poolStarted += 1;
        if (r.kind === "completed" || r.kind === "completed_light") poolCompleted += 1;
        if (r.kind === "error") poolErrors += 1;
        if (r.kind === "aborted") poolAborted += 1;
      } else if (r.kind === "started" && !r.pool) liveStarted += 1;
    });
    currentSection.pool_window_started = poolStarted;
    currentSection.pool_window_completed = poolCompleted;
    currentSection.pool_window_errors = poolErrors;
    currentSection.pool_window_aborted = poolAborted;
    currentSection.non_pool_http_started = liveStarted;
    Object.assign(currentSection, extra || {});
    currentSection.ended_ms = relMs(performance.now());
    sections.push(currentSection);
    var done = currentSection;
    currentSection = null;
    reconcileAborted();
    console.log(
      "[MC-POOL-P4A] ■",
      done.id,
      "pool_POST_started=" + done.pool_window_started,
      "pool_POST_completed=" + done.pool_window_completed,
      "duration_ms=" + (done.ended_ms - done.started_ms)
    );
    return done;
  }

  function reconcileAborted() {
    Object.keys(httpPending).forEach(function (id) {
      var p = httpPending[id];
      if (!p || p.settled) return;
      var rec = {
        t_rel_ms: relMs(performance.now()),
        kind: "aborted",
        req_id: id,
        category: p.category,
        pool: p.pool,
        trigger: p.trigger,
        section_id: p.section_id,
      };
      httpLog.push(rec);
      bumpSection("window_http_aborted", rec);
      p.settled = true;
      delete httpPending[id];
    });
  }

  function researchState() {
    try {
      return window.__researchDebug || null;
    } catch (e) {
      return null;
    }
  }

  function paneChunkSummary() {
    var out = [];
    var st = researchState();
    if (!st || !st.panes) return out;
    var ids = Object.keys(st.panes);
    ids.forEach(function (pid) {
      var pane = st.panes[pid];
      if (!pane) return;
      var stores = pane.exactOriginWindowStores || {};
      var scopeKeys = Object.keys(stores);
      var chunks = 0;
      scopeKeys.forEach(function (k) {
        var store = stores[k];
        if (store && store.chunks) chunks += Object.keys(store.chunks).length;
      });
      out.push({
        pane: pid,
        chart_tf: pane.tf || null,
        pane_symbol: pane.symbol || null,
        exact_origin_enabled: !!pane.exactOriginEnabled,
        store_scopes: scopeKeys.length,
        chunk_entries: chunks,
        segment_cache_key: pane.exactOriginSegmentCache ? pane.exactOriginSegmentCache.key : null,
        sweep_cache_key: pane.exactOriginSweepCache ? pane.exactOriginSweepCache.key : null,
        load_serial: pane.exactOriginLoadSerial || 0,
      });
    });
    return out;
  }

  function chartTfFromState() {
    var st = researchState();
    if (!st || !st.panes) return null;
    var pane = st.panes["pane-0"] || st.panes[Object.keys(st.panes)[0]];
    if (pane && pane.tf) return pane.tf;
    try {
      var sel = document.querySelector(".trp-pane-tf");
      if (sel && sel.value) return sel.value;
    } catch (e2) {
      /* ignore */
    }
    return null;
  }

  function snapshotSettings() {
    var eo = document.getElementById("researchIndExactOriginDensity");
    var hist = document.getElementById("researchIndHistoricalLmv2Pools");
    var poolTf =
      document.querySelector('input[name="researchExactOriginPoolTimeframe"]:checked') || {};
    var pd1 = document.querySelector('input[name="researchExactOriginPoolDetail"][value="1"]');
    var symEl = document.getElementById("researchSymbol");
    var st = researchState();
    return {
      symbol: (symEl && symEl.value) || (st && st.symbol) || null,
      chart_tf: chartTfFromState(),
      exact_origin_checked: !!(eo && eo.checked),
      historical_lmv2_checked: !!(hist && hist.checked),
      pool_timeframe: poolTf.value || null,
      pool_detail_pd1_checked: !!(pd1 && pd1.checked),
      canvas_path:
        window.PD1ExactOriginResearch && PD1ExactOriginResearch.isCanvasPathEnabled
          ? !!PD1ExactOriginResearch.isCanvasPathEnabled()
          : null,
      doge_trace: !!(window.DogeExactOriginTrace && DogeExactOriginTrace.enabled()),
      chunk_stores: paneChunkSummary(),
      targets_ok: validateTargets(false),
    };
  }

  function validateTargets(log) {
    var symEl = document.getElementById("researchSymbol");
    var st = researchState();
    var eo = document.getElementById("researchIndExactOriginDensity");
    var hist = document.getElementById("researchIndHistoricalLmv2Pools");
    var poolTf =
      document.querySelector('input[name="researchExactOriginPoolTimeframe"]:checked') || {};
    var pd1 = document.querySelector('input[name="researchExactOriginPoolDetail"][value="1"]');
    var check = {
      symbol: (symEl && symEl.value) || (st && st.symbol) || null,
      chart_tf: chartTfFromState(),
      pool_timeframe: poolTf.value || null,
      pool_detail_pd1: !!(pd1 && pd1.checked),
      exact_origin: !!(eo && eo.checked),
      historical_lmv2: !!(hist && hist.checked),
    };
    var ok =
      String(check.symbol || "").toUpperCase() === "SOLUSDT" &&
      String(check.chart_tf || "") === "5m" &&
      String(check.pool_timeframe || "") === "5m" &&
      check.pool_detail_pd1 &&
      check.exact_origin &&
      !check.historical_lmv2;
    if (log && !ok) console.warn("[MC-POOL-P4A] setup check FAILED", check);
    else if (log && ok) console.log("[MC-POOL-P4A] setup check OK", check);
    return ok;
  }

  function parseWindowResponse(data, meta) {
    if (!data || typeof data !== "object") return;
    var prov = data.provenance || {};
    var rec = {
      t_rel_ms: relMs(performance.now()),
      kind: "completed",
      category: "pool_window",
      pool: true,
      trigger: meta.trigger,
      section_id: meta.section_id,
      client_ms: meta.client_ms,
      status: meta.status,
      pools_n: Array.isArray(data.pools) ? data.pools.length : null,
      response_cache: data.response_cache || null,
      phase5s_batch_reader_active: prov.phase5s_batch_reader_active,
      request_id: data.request_id || null,
      from_utc: (data.window && data.window.from_utc) || null,
      to_utc: (data.window && data.window.to_utc) || null,
      req_id: meta.req_id,
    };
    httpLog.push(rec);
    if (meta.req_id && httpPending[meta.req_id]) {
      httpPending[meta.req_id].settled = true;
      delete httpPending[meta.req_id];
    }
    bumpSection("window_http_completed", rec);
  }

  function patchFetch() {
    if (fetchPatched || !window.fetch) return;
    origFetch = window.fetch.bind(window);
    window.fetch = function (input, init) {
      var url = typeof input === "string" ? input : input && input.url ? input.url : "";
      var cls = classifyUrl(url);
      var isTracked = active && (cls.pool || url.indexOf("/api/research/") >= 0);
      var reqId = isTracked ? "h" + httpLog.length + "-" + Date.now() : null;
      var tReq = performance.now();
      var trigger = httpTriggerTag();
      var sectionId = currentSection ? currentSection.id : null;
      if (isTracked) {
        var startRec = {
          t_rel_ms: relMs(tReq),
          kind: "started",
          category: cls.category,
          pool: !!cls.pool,
          trigger: trigger,
          section_id: sectionId,
          url: url.split("?")[0],
          req_id: reqId,
        };
        httpLog.push(startRec);
        if (reqId) {
          httpPending[reqId] = {
            category: cls.category,
            pool: !!cls.pool,
            trigger: trigger,
            section_id: sectionId,
            settled: false,
          };
        }
        bumpSection("http_started", startRec);
      }
      return origFetch(input, init).then(
        function (res) {
          if (!isTracked) return res;
          var client_ms = Math.round(performance.now() - tReq);
          if (cls.pool && !parseBodies) {
            var light = {
              t_rel_ms: relMs(performance.now()),
              kind: "completed_light",
              category: cls.category,
              pool: true,
              trigger: trigger,
              section_id: sectionId,
              req_id: reqId,
              client_ms: client_ms,
              status: res.status,
              parse_skipped: true,
            };
            httpLog.push(light);
            if (reqId && httpPending[reqId]) {
              httpPending[reqId].settled = true;
              delete httpPending[reqId];
            }
            bumpSection("window_http_completed_light", light);
            return res;
          }
          if (cls.pool && parseBodies) {
            var clone = res.clone();
            clone
              .json()
              .then(function (data) {
                parseWindowResponse(data, {
                  client_ms: client_ms,
                  status: res.status,
                  trigger: trigger,
                  section_id: sectionId,
                  req_id: reqId,
                });
              })
              .catch(function () {});
          } else {
            var doneRec = {
              t_rel_ms: relMs(performance.now()),
              kind: "completed",
              category: cls.category,
              pool: false,
              trigger: trigger,
              section_id: sectionId,
              req_id: reqId,
              client_ms: client_ms,
              status: res.status,
            };
            httpLog.push(doneRec);
            if (reqId && httpPending[reqId]) {
              httpPending[reqId].settled = true;
              delete httpPending[reqId];
            }
          }
          return res;
        },
        function (err) {
          if (isTracked) {
            var errRec = {
              t_rel_ms: relMs(performance.now()),
              kind: "error",
              category: cls.category,
              pool: !!cls.pool,
              trigger: trigger,
              section_id: sectionId,
              req_id: reqId,
              message: String(err && err.message ? err.message : err),
              aborted: !!(err && err.name === "AbortError"),
            };
            httpLog.push(errRec);
            if (reqId && httpPending[reqId]) {
              httpPending[reqId].settled = true;
              delete httpPending[reqId];
            }
            bumpSection("http_error", errRec);
          }
          throw err;
        }
      );
    };
    fetchPatched = true;
  }

  function unpatchFetch() {
    if (fetchPatched && origFetch) window.fetch = origFetch;
    fetchPatched = false;
  }

  function installObservers() {
    disconnectObservers();
    if (!window.PerformanceObserver) return;
    try {
      poLong = new PerformanceObserver(function (list) {
        if (!active) return;
        list.getEntries().forEach(function (e) {
          if (e.entryType !== "longtask") return;
          if (e.startTime < sessionPerfStart - 1) return;
          var key = String(e.startTime) + "|" + String(e.duration);
          if (longTasksDedup[key]) return;
          longTasksDedup[key] = 1;
          longTasks.push({
            t_rel_ms: longTaskRelMs(e),
            startTime: e.startTime,
            duration: Math.round(e.duration),
            name: e.name,
          });
        });
      });
      poLong.observe({ type: "longtask", buffered: false });
    } catch (e1) {
      poLong = null;
    }
    try {
      poResource = new PerformanceObserver(function (list) {
        if (!active) return;
        list.getEntries().forEach(function (e) {
          if (e.entryType !== "resource") return;
          if (e.startTime < sessionPerfStart - 1) return;
          if (String(e.name).indexOf("/viewer/api/window") < 0) return;
          bumpSection("resource_window", {
            duration: Math.round(e.duration),
            transferSize: e.transferSize,
            encodedBodySize: e.encodedBodySize,
          });
        });
      });
      poResource.observe({ type: "resource", buffered: false });
    } catch (e2) {
      poResource = null;
    }
  }

  function disconnectObservers() {
    if (poLong) {
      try {
        poLong.disconnect();
      } catch (e) {}
      poLong = null;
    }
    if (poResource) {
      try {
        poResource.disconnect();
      } catch (e) {}
      poResource = null;
    }
  }

  function startFrameSampler() {
    stopFrameSampler();
    var last = performance.now();
    function loop(now) {
      if (!active) return;
      var dt = now - last;
      last = now;
      if (dt > 0 && dt < 5000) frameTimes.push(Math.round(dt));
      if (frameTimes.length > 2000) frameTimes.shift();
      rafLoopId = requestAnimationFrame(loop);
    }
    rafLoopId = requestAnimationFrame(loop);
  }

  function stopFrameSampler() {
    if (rafLoopId != null) cancelAnimationFrame(rafLoopId);
    rafLoopId = null;
  }

  function percentile(arr, p) {
    if (!arr.length) return null;
    var s = arr.slice().sort(function (a, b) {
      return a - b;
    });
    var idx = Math.floor((p / 100) * (s.length - 1));
    return s[idx];
  }

  function installWraps() {
    if (wrapsInstalled || !window.PD1ExactOriginResearch) return;
    var R = window.PD1ExactOriginResearch;
    if (R.refreshPane) {
      origRefreshPane = R.refreshPane;
      R.refreshPane = function (pane, opts) {
        var runId = refreshRuns.length;
        var tStart = performance.now();
        var rec = {
          id: runId,
          t_rel_ms: relMs(tStart),
          opts: opts
            ? {
                force: !!opts.force,
                viewport: !!opts.viewport,
                fromUnix: opts.fromUnix,
                toUnix: opts.toUnix,
              }
            : null,
          ended: false,
        };
        refreshRuns.push(rec);
        if (active) bumpSection("refreshPane_start", { run_id: runId, opts: rec.opts });
        return origRefreshPane.call(R, pane, opts).then(
          function (res) {
            rec.ended = true;
            rec.duration_ms = Math.round(performance.now() - tStart);
            if (window.PD1ExactOriginResearch.getLastMetrics) {
              rec.metrics = JSON.parse(JSON.stringify(PD1ExactOriginResearch.getLastMetrics() || {}));
            }
            if (active) bumpSection("refreshPane_end", { run_id: runId, duration_ms: rec.duration_ms });
            return res;
          },
          function (err) {
            rec.ended = true;
            rec.duration_ms = Math.round(performance.now() - tStart);
            rec.error = String(err && err.message ? err.message : err);
            rec.aborted = !!(err && err.name === "AbortError");
            if (active) bumpSection("refreshPane_error", rec);
            throw err;
          }
        );
      };
    }
    if (R.scheduleViewport) {
      origScheduleViewport = R.scheduleViewport;
      R.scheduleViewport = function (pane, from, to) {
        var rec = { t_rel_ms: relMs(performance.now()), from: from, to: to };
        viewportSchedules.push(rec);
        if (active) bumpSection("scheduleViewport", rec);
        return origScheduleViewport.call(R, pane, from, to);
      };
    }
    if (typeof console.log === "function") {
      origConsoleLog = console.log;
      console.log = function () {
        if (active && arguments.length && String(arguments[0]).indexOf("TRACE |") === 0) {
          traceLines.push({ t_rel_ms: relMs(performance.now()), line: String(arguments[0]) });
          if (String(arguments[0]).indexOf("refreshPane_coalesced") >= 0) {
            coalesceMarks.push({ t_rel_ms: relMs(performance.now()) });
          }
        }
        return origConsoleLog.apply(console, arguments);
      };
    }
    wrapsInstalled = true;
  }

  function removeWraps() {
    if (!wrapsInstalled || !window.PD1ExactOriginResearch) return;
    var R = window.PD1ExactOriginResearch;
    if (origRefreshPane) R.refreshPane = origRefreshPane;
    if (origScheduleViewport) R.scheduleViewport = origScheduleViewport;
    if (origConsoleLog) console.log = origConsoleLog;
    wrapsInstalled = false;
  }

  function sampleViewport(tag) {
    markUserInteraction(800);
    var out = {
      t_rel_ms: relMs(performance.now()),
      tag: tag || "sample",
      settings: snapshotSettings(),
      last_metrics: null,
      canvas_draw: null,
      visible_range: null,
    };
    if (window.PD1ExactOriginResearch && PD1ExactOriginResearch.getLastMetrics) {
      out.last_metrics = PD1ExactOriginResearch.getLastMetrics();
    }
    var iframe = document.querySelector(".trp-pane iframe, iframe.trp-pane-frame, iframe");
    try {
      var cw = iframe && iframe.contentWindow;
      if (cw && cw.chartApi && cw.chartApi.getExactOriginDensityDrawStats) {
        out.canvas_draw = cw.chartApi.getExactOriginDensityDrawStats();
      }
      if (cw && cw.chartApi && cw.chartApi.getChart) {
        var ch = cw.chartApi.getChart();
        if (ch && ch.timeScale) out.visible_range = ch.timeScale().getVisibleRange();
      }
    } catch (e) {
      out.iframe_error = String(e);
    }
    viewportSamples.push(out);
    return out;
  }

  function summarizeHttp() {
    var byCat = {};
    var poolByTrigger = { user: 0, live_or_background: 0, unknown: 0 };
    httpLog.forEach(function (r) {
      if (r.kind !== "started") return;
      byCat[r.category] = (byCat[r.category] || 0) + 1;
      if (r.category === "pool_window") {
        var t = r.trigger || "unknown";
        poolByTrigger[t] = (poolByTrigger[t] || 0) + 1;
      }
    });
    return { started_by_category: byCat, pool_window_started_by_trigger: poolByTrigger };
  }

  function exportJSON() {
    if (currentSection) endSection({ auto_closed: true });
    reconcileAborted();
    var payload = {
      schema_version: SCHEMA,
      exported_wall_ms: Date.now(),
      settings_at_export: snapshotSettings(),
      sections: sections,
      http_log: httpLog,
      http_summary: summarizeHttp(),
      refresh_runs: refreshRuns,
      viewport_schedules: viewportSchedules,
      viewport_samples: viewportSamples,
      long_tasks_deduped: longTasks,
      coalesce_marks: coalesceMarks,
      trace_line_count: traceLines.length,
      trace_lines_sample: traceLines.slice(-100),
      frame_time_ms: {
        n: frameTimes.length,
        p50: percentile(frameTimes, 50),
        p95: percentile(frameTimes, 95),
        max: frameTimes.length ? Math.max.apply(null, frameTimes) : null,
      },
      harness_checks: {
        single_performance_observer: true,
        disconnect_on_stop: true,
        longtask_session_only: true,
        default_no_body_parse: !parseBodies,
        http_starts_completions_separate: true,
      },
    };
    var ts = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
    var name = "MC_POOL_P4A_SOLUSDT_5M_" + ts + ".json";
    var blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    a.click();
    setTimeout(function () {
      URL.revokeObjectURL(a.href);
    }, 3000);
    console.log("[MC-POOL-P4A] exported", name);
    return payload;
  }

  function hardResetData() {
    sections = [];
    httpLog = [];
    httpPending = Object.create(null);
    refreshRuns = [];
    viewportSchedules = [];
    viewportSamples = [];
    longTasks = [];
    longTasksDedup = Object.create(null);
    frameTimes = [];
    traceLines = [];
    coalesceMarks = [];
    userInteractionUntil = 0;
    t0 = performance.now();
    sessionPerfStart = performance.now();
    currentSection = null;
  }

  function start(opts) {
    opts = opts || {};
    stop({ quiet: true });
    hardResetData();
    active = true;
    parseBodies = !!opts.parseWindowBodies;
    installWraps();
    try {
      if (opts.enableTrace !== false) localStorage.setItem("DOGE_EXACT_ORIGIN_TRACE", "1");
    } catch (e) {}
    if (window.DogeExactOriginTrace && DogeExactOriginTrace.activationHelp) {
      DogeExactOriginTrace.activationHelp();
    }
    patchFetch();
    installObservers();
    startFrameSampler();
    validateTargets(true);
    console.log("[MC-POOL-P4A] started", { parseBodies: parseBodies, settings: snapshotSettings() });
    if (opts.auto_section_a) startSection("A", "baseline_pools_on", { interaction: "user" });
    return true;
  }

  function stop(extra) {
    active = false;
    unpatchFetch();
    disconnectObservers();
    stopFrameSampler();
    removeWraps();
    if (currentSection) endSection({ stopped_mid_section: true });
    if (!(extra && extra.quiet)) console.log("[MC-POOL-P4A] stopped", extra || {});
  }

  function userClick() {
    markUserInteraction(3000);
  }

  var api = {
    SCHEMA: SCHEMA,
    start: start,
    stop: stop,
    exportJSON: exportJSON,
    startSection: startSection,
    endSection: endSection,
    sampleViewport: sampleViewport,
    markUserInteraction: markUserInteraction,
    userClick: userClick,
    validateTargets: function () {
      return validateTargets(true);
    },
    snapshotSettings: snapshotSettings,
    setParseWindowBodies: function (on) {
      parseBodies = !!on;
    },
    help: function () {
      console.log(
        "[MC-POOL-P4A] start() → A–H in one session → exportJSON() → stop()",
        "Before each manual UI step: __MC_POOL_P4A__.userClick()",
        "Tests: startSection/endSection + sampleViewport"
      );
    },
  };

  window.__MC_POOL_P4A__ = api;
  console.log("[MC-POOL-P4A] ready — __MC_POOL_P4A__.help()");
  return api;
})();
```
