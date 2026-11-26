# MC-POOL-P3 — Targeted Browser Cache & Pool Processing Audit (READ-ONLY)

**Endstatus:** `MC_POOL_P3_TARGETED_AUDIT_COMPLETE`  
**Datum:** 2026-11-26  
**SSOT (Code):** `spread_recovery_hedge_short_dev/dashboard/` (Live-Dirty-Tree)  
**Messbasis:** Manuelle Browser-Tests A–E (SOLUSDT, Single Chart, 5m, Exact Origin 5m PD1, Canvas-Pfad)

**Keine produktiven Änderungen in dieser Phase.**

---

## 0. Executive Summary

| Test | Kernerkenntnis | Klassifikation |
|------|----------------|----------------|
| **A** Initial | 2 geplante API-Chunks; Client-Chunk-Store leer → 2× POST; Server-LRU 1/2 | **CONFIRMED** |
| **B** OFF→ON | `stripPane` → `clearCanvasOnPane` → **`resetAllWindowStores`**; Re-Enable mit `{ force: true }` → erneut 2× POST trotz Server-LRU 2/2 | **CONFIRMED** |
| **C** Slow pan | Chunk-Store voll → 0 HTTP; trotzdem voller `refreshPaneImpl` (Sweep/Merge); `sweepCacheHit: false` | **CONFIRMED** |
| **D** Fast pan | Neuer 48h-Tile + Viewport-Coalescing / `loadSerial`-Supersede → viele `refreshPane`-Durchläufe | **CONFIRMED** (HTTP) / **SUSPECTED** (29 Long Tasks ↔ Pipeline) |
| **E** H-Zoom | 0 HTTP, Chunk-Hits; erneute Zone-Pipeline; Segment-Cache durch `renderKey` invalidiert | **CONFIRMED** |

**Haupt-Root-Causes (Client):**

1. **Toggle OFF zerstört Window-Chunk-RAM** obwohl Server-Response-Cache warm bleibt (**CONFIRMED**).
2. **Re-Enable erzwingt `force: true`** → überspringt Segment-Render-Shortcut (**CONFIRMED**).
3. **`sweepCacheKey` bindet Sweep-Zeitfenster an Viewport-Hit** → jede Pan/Zoom-Änderung = Sweep-Miss (**CONFIRMED by design**).
4. **`exactOriginSegmentCache.key` = sichtbare `hit.from`–`hit.to`** → kein Fast-Path bei Viewport-Änderung (**CONFIRMED**).
5. **`adapterMs` misst gesamte `refreshPaneImpl` bis Sweep/Merge-Ende**, inkl. `loadCaps`, optional Bounds-Fetch, HTTP-Wartezeit, `poolsToExactOriginZones` (**CONFIRMED**).

---

## 1. Referenz — bestätigte Browser-Messwerte (A–E)

Eingaben aus MC-POOL-P2-Messung (unverändert dokumentiert):

| Test | HTTP Δ | Server cache | Browser window cache¹ | adapterMs | rawZones | mergedSeg | sweepCacheHit |
|------|--------|--------------|----------------------|-----------|----------|-----------|---------------|
| A | 2 | 1/2 | 0/2 | 1528 | 41057 | 704 | false |
| B | 2 | 2/2 | 0/2 | 1244 | 41057 | 704 | false |
| C | 0 | — | voll | 113 | 55507 | 806 | false |
| D | 1 | — | — | 67 | 30558 | 628 | false |
| E | 0 | 2/2² | — | 150 | 61109 | 831 | false |

¹ „Browser window cache“ in P2 = `last_metrics.windowFetch.cacheHits` / Harness-Interpretation von `exactOriginWindowsFullyCached` — **nicht** HTTP `Cache-Control`.

² Test E: Chunk-Store-Treffer (`fetchStats.cacheHits`), nicht Segment-Render-Cache.

Long Tasks (P2, nicht Pipeline-zugeordnet): C ~9/904ms, D ~29/3077ms, E ~10/1755ms — **NOT MEASURED** (Ursachenzuordnung).

---

## 2. Audit A — Browser Cache Lifecycle

### 2.1 Exact Origin Checkbox — Call-Chain

#### Single-Chart (nicht Multi-Manual)

```
research_charts.html — #researchIndExactOriginDensity
  └─ pd1_exact_origin_density_research_v1.js::install (L2076)
       cb.addEventListener("change", …) (L2080)
         ON  → disableHistoricalLmv2() (L2090)
              → refreshAll({ force: true }) (L2093)
         OFF → stripAllPanes() (L2098)
              → stripPane × N (L734–740)
```

#### Multi-Manual

```
research_charts.js::setMultiPaneExactOriginEnabled (L271)
  ON  → refreshPane(pane, { force: true }) (L281)
  OFF → stripPane(pane) (L286–287)
```

Toolbar-Sync ohne Doppel-Event: `suppressExactOriginToolbarEvent` (`research_charts.js` L24, L256).

### 2.2 Was passiert bei OFF?

| Aktion | Window-Chunk-Store | Segment-Cache | Sweep-Cache | Canvas/TRP |
|--------|-------------------|---------------|-------------|------------|
| `stripPane` (L727) | **GELEERT** via `clearCanvasOnPane` (L729) | **GELEERT** (`resetAllWindowStores` L546–547) | **GELEERT** | `clearExactOriginDensity` |
| `invalidateScopeCaches` (L494) | unberührt | unberührt | unberührt | — |
| Checkbox OFF | ruft **nicht** `invalidateScopeCaches` | — | — | — |

**CONFIRMED — Root Cause Test B:**

```591:595:spread_recovery_hedge_short_dev/dashboard/static/js/research/pd1_exact_origin_density_research_v1.js
  function clearCanvasOnPane(pane) {
    const chart = deps && deps.api(pane);
    if (chart && chart.clearExactOriginDensity) chart.clearExactOriginDensity();
    if (pane) resetAllWindowStores(pane);
  }
```

`stripPane` ruft `clearCanvasOnPane` auf → **bewusst gekoppelte Canvas-Löschung und Window-Store-Löschung**, nicht nur Darstellung.

`resetAllWindowStores` (L542–548): `exactOriginWindowStores`, `exactOriginSegmentCache`, `exactOriginSweepCache` alle `null` / `{}`.

**SUSPECTED:** Historische Kopplung „Canvas leer = keine stale Chunks im RAM“ (Sicherheit vor Symbol/Scope-Mix). Für Toggle OFF bei unverändertem Symbol/Scope ist das **redundant** zu Server-LRU.

### 2.3 Was passiert bei ON?

1. `refreshAll` / `refreshPane` mit **`force: true`** (Checkbox L2093, Multi L281).
2. `force` verhindert:
   - Segment-Shortcut (L1578–1602): `!(opts && opts.force)`  
   - Sweep-Shortcut (L1674, L1705): `!(opts && opts.force)`

3. `fetchPlannedWindows` (L1146): HTTP nur für `missing` Chunks (L1157–1165). Nach OFF ist Store leer → **alle geplanten Fenster fehlen** → 2× POST (Test A/B Planung, sliding 5m PD1 typisch 2 Chunks im Load-Span).

4. Server kann LRU-Hit liefern (Test B: 2/2) — Client überträgt dennoch ~412 KB JSON (**CONFIRMED** — kein clientseitiger Skip bei Server-Hit).

### 2.4 Warum zwei identische Window-Requests bei B?

**CONFIRMED:** Kein Doppel-Fetch-Bug, sondern:

- `scopes.map` → ein Scope (5m PD1) → `AD.planExactOriginLoadWindows` liefert **≥2** nicht überlappende API-Chunks (`pd1_exact_density_trp_adapter.js` L370–390, `EOD_MAX_API_CHUNK_MS`).
- Beide parallel (`maxConcurrentWindowFetch`, L1550–1577).
- Identische Server-Cache-Key-Digests = gleiche Request-Bodies, separate Chunk-Keys im Client-Store.

**NOT MEASURED:** Ob P2 „2 HTTP“ teilweise aus Harness-Clone-Parsing stammt — P3-Harness zählt Starts/Completions getrennt.

### 2.5 Caches — Übersicht

| Cache | Speicherort | Key / Invalidierung | Bei Live-/Scope-Wechsel |
|-------|-------------|---------------------|-------------------------|
| **Window Chunk Store** | `pane.exactOriginWindowStores[scopeKey]` (L533–539) | `exactOriginCacheKeyForWindow` (Adapter L332+) | `resetAllWindowStores`, Symbol (`onPaneSymbolChange` L2162–2167), Replay-Gen-Mismatch (L1554–1558), `stripPane` |
| **Caps** | Modul `caps` | `invalidateCapsCache` (L939) | Symbol-Switch (L2179), TF-Wechsel (L2199) |
| **Inflight dedupe** | `inflight` Map | `sym\|scope-from-to` (L1264–1328) | `invalidateScopeCaches` (L494–496) |
| **Sweep Cache** | `pane.exactOriginSweepCache` | `exactOriginSweepCacheKey(epoch, scope, slotMin, **win.sweepT0/T1**, **ext.yMin/yMax**)` (L573–588) | Viewport → `win` ändert sich; Scope-Reset |
| **Segment Cache** | `pane.exactOriginSegmentCache` | `viewportRenderKey(hit)` = `hit.from-hit.to` + scope (L529–531, L1577) | Pan/Zoom; `resetAllWindowStores` |
| **Server Response LRU** | Python `window_response_cache_v1` | Request-Body-Digest | Unberührt bei Client OFF |

**Chunk LRU:** `touchExactOriginWindowCache` (Adapter L354–367), max `EOD_WINDOW_CHUNK_CACHE_MAX` — **bounded**, unverändert lassen.

### 2.6 Darstellung entfernen bei OFF, Daten im RAM?

**Aktuell:** Nicht möglich ohne Code-Änderung — `stripPane` = Canvas + Stores.

**Minimal-Fix-Vorschlag (RC-A1):**

- `stripPaneVisual(pane)`: nur `clearExactOriginDensity` + Overlay-Strip, **ohne** `resetAllWindowStores`.
- `stripPane` nur bei echten Invalidierungen (Symbol, Pool-TF, PD, Replay-Gen, Live-Bounds-Advance).

**Risiken:** Stale Chunks wenn Server-Membership neu publiziert wurde aber Client nicht invalidiert — **muss** an bestehende Guards gekoppelt werden (Caps `data_available_to`, Symbol, `replayGen`).

**Invalidierung bei neuer Live-Publication (CONFIRMED partial):**

- Server: neue Closes in CH → nächster Window-Build liefert aktuelle Pools (kein Client-Bug wenn `hiMs` steigt).
- Client: `ensurePaneViewerBounds` (L685–700) aktualisiert Caps periodisch (`DOGE_5M_BOUNDS_REFRESH_MS`); kein dedizierter WebSocket-„Publication“-Hook in Exact-Origin-JS (**SUSPECTED** — Refresh passiert über GO/Indicators/`refreshAll({force:true})` in `research_charts.js` L10323–10330).
- **Bei Live-Update:** mindestens `invalidateCapsCache` + Chunk-Invalidierung für Zeitfenster ≥ alter `data_available_to` — **NOT MEASURED** im E2E-Pfad SOLUSDT.

**Wiederverwendbare Guards (unverändert):**

- `exactOriginLoadSerial` + supersede (L1375–1422, L1623–1634)
- `replayGen` Store-Reset (L1550–1558)
- `exactOriginChunkSatisfiesWindow` (Adapter L492–496)
- `inflight` Request-Dedupe (L1264–1328)

### 2.7 Frische vs. Sicherheit — Toggle ON mit `force: true`

**CONFIRMED:** `force: true` ist konservativ (nach OFF, GO, Symbol, Indicators). Für Test B (gleicher Scope, nur Checkbox) **redundant**, wenn Window-Store erhalten bliebe und Caps unverändert.

**Minimal-Fix (RC-A2):** Bei Re-Enable ohne Scope/Symbol/TF-Änderung: `refreshPane(pane, { viewport: false })` ohne `force`, sofern `exactOriginWindowsFullyCached` — Segment-Shortcut nur wenn Viewport unverändert (selten nach OFF).

---

## 3. Audit B — Pan/Zoom Processing Pipeline

### 3.1 End-to-End Pfad

```
TRP chart.js — on_visible_range(from, to)
  research_charts.js::wirePane (L2888–2902)
    PD1ExactOriginResearch.scheduleViewport(pane, from, to)  [debounce deps.VP_DEBOUNCE_MS || 400ms, L1966–1978]

scheduleViewport → refreshPane(pane, { fromUnix, toUnix, viewport: true })
  refreshPane — Coalescing-Gate (L1388–1422)
    mergeEoRefreshOpts; bei laufendem inflight + viewport/force → bumpExactOriginLoadSerial (L1396–1398)

refreshPaneImpl (L1425+)
  tAdapter0 (L1463)
  resolveViewSec (L1464)
  loadCaps (L1473)
  ensurePaneViewerBounds → optional GET bounds (L1454, L685–700)
  buildWindows (L1499) → hit, sweepT0/T1, apiFrom/To
  scopePlans + planExactOriginLoadWindows (L1561–1573)
  [optional] fullyCached + segmentCache key === renderKey → pushCanvasDensity; RETURN (L1578–1602)

  fetchPlannedWindows (L1608) — Chunk RAM / HTTP
  gatherPoolsFromStore (L1639–1642)
  ext yMin/yMax aus allen Pools im loadSpan (L1656–1660)

  Sweep branch (L1673–1719)
    sweepCacheHit nur wenn key match inkl. sweepT0/T1 + ext
    else poolsToExactOriginZones (SWEEP) → cullZonesToViewport → mergeExactOriginSegments

  tAdapter1 (L1720) → adapterMs = tAdapter1 - tAdapter0 (L1811)

  pushCanvasDensity → chart.setExactOriginDensity (L1782–1783, L631–641)
  chart.js drawEodDensity (L4917–4928, L4913–4914 draw ms)
  exactOriginSegmentCache := { key: renderKey, segments, metrics } (L1839–1844)
```

### 3.2 Warum `refreshPane` trotz Window-Cache-Hits? (Test C/E)

**CONFIRMED:** `scheduleViewport` ruft **immer** `refreshPane` auf, nie nur `repaintPaneFromCache` (L1966–1978 vs. L1990–2002).

Chunk-Cache-Hit ⇒ `fetchPlannedWindows` → `fetched: 0`, `cacheHits: N` (L1166–1167), aber:

- Kein Early-Return vor Sweep, wenn `renderKey` ≠ `exactOriginSegmentCache.key` (Pan/Zoom ändert `hit.from`/`hit.to`).
- `fullyCached` (L1578) prüft Chunks, aber Shortcut braucht **zusätzlich** `segmentCache.key === renderKey` (L1587).

**Notwendig:** Viewport-neues `hit` → neue `vpBounds` → **Cull/Merge** (viewport-abhängig).

**Redundant (SUSPECTED–CONFIRMED):** Vollständige `poolsToExactOriginZones` über **gesamten** `loadSpan`, obwohl Sweep-Zonen für festes `epoch` + loadSpan wiederverwendbar wären, wenn Sweep-Key **nicht** an `win.sweepT0Ms/T1Ms` (viewport-padded) gebunden wäre.

### 3.3 `sweepCacheHit` bleibt false (C/D/E)

**CONFIRMED — by construction:**

```573:588:spread_recovery_hedge_short_dev/dashboard/static/js/research/pd1_exact_origin_density_research_v1.js
  function exactOriginSweepCacheKey(scopeKey, slotMin, win, ext, epoch) {
    return (
      String(epoch) + "\x1f" + scopeKey + "\x1f" + slotMin +
      "\x1f" + win.sweepT0Ms + "\x1f" + win.sweepT1Ms +
      "\x1f" + ext.yMin.toFixed(12) + "\x1f" + ext.yMax.toFixed(12)
    );
  }
```

`buildWindows` setzt `sweepT0Ms`/`sweepT1Ms` aus **sichtbarem** Hit ± `SWEEP_PAD_MS` (L1102–1105). Jede Pan-Änderung → neuer Key → Miss → volle Zone-Generierung (`rawZones` steigt in C vs. A — **CONFIRMED**).

Bei Hit würde nur **Cull+Merge** auf cached `zones` laufen (L1688–1690) — Pfad existiert, wird bei Viewport-Tests nicht genutzt.

### 3.4 Was umfasst `adapterMs`?

**CONFIRMED** — Intervall `tAdapter0` (L1463, Start `refreshPaneImpl` nach Serial-Bump) bis `tAdapter1` (L1720, **nach** Sweep/Merge, **vor** Cap/Push):

Enthält:

| Phase | Viewport-only? |
|-------|----------------|
| `syncPoolDetailUi`, `ensurePaneViewerBounds` (async fetch) | teils |
| `loadCaps` | ja (modul-cache nach 1. Mal günstig) |
| `fetchPlannedWindows` + HTTP-Wartezeit | nur bei Chunk-Miss |
| `gatherPoolsFromStore` + `mergeDensityPools` | ja |
| `poolsToExactOriginZones` (teuer, ~rawZones 30k–60k) | Sweep abhängig von sweepT0/T1 (**viewport**) |
| `cullZonesToViewport` + `mergeExactOriginSegments` | **viewport** |

Enthält **nicht:** `pushCanvasDensity` / Canvas-Draw (eigene `canvasDrawMs` aus `getExactOriginDensityDrawStats`, L1827–1837).

**NOT MEASURED:** Aufschlüsselung `cullMs`/`mergeMs` in Produktion (Variablen L1723–1724 bleiben 0 — nicht instrumentiert).

### 3.5 Test D — Fast Pan

**CONFIRMED:** 1 neuer historischer Tile → 1× POST erwartbar (`planExactOriginLoadWindows` / fixed48h optional).

**CONFIRMED:** `refreshPane` Coalescing: während `refreshPaneImpl` läuft, weitere Viewport-Events → `bumpExactOriginLoadSerial` (L1396–1398) → laufende Arbeit kann superseded werden → `scheduleSupersededDensityRefresh` (L1945–1963, 48 ms) → **weitere** `refreshPane`-Läufe.

**SUSPECTED:** 29 Long Tasks korrelieren mit wiederholten Sweep-Läufen + Chart-Layout, nicht isoliert gemessen.

### 3.6 Payload / Iframe

**CONFIRMED:** `pushCanvasDensity` ruft **immer** `chart.setExactOriginDensity` (L636–641) — kein Hash-Vergleich gegen vorheriges `eodDensityPayload` in `chart.js` (L4917–4928).

**SUSPECTED:** Identische Segmente werden erneut gezeichnet (`drawEodDensity` L4913–4914) — Draw-Zeit Test C/E niedrig (1–2 ms) → Kosten liegt in **JS-Sweep**, nicht Canvas.

### 3.7 Notwendig vs. redundant (Zusammenfassung)

| Arbeit | Notwendig bei Pan innerhalb Tile | Redundant heute |
|--------|----------------------------------|-----------------|
| HTTP Window Fetch | Nein (C, E) | — |
| `poolsToExactOriginZones` ganzer loadSpan | Nein, wenn epoch+loadSpan unverändert | **Ja** (Sweep-Key an Viewport) |
| Cull + Merge | Ja | Nein |
| `gatherPoolsFromStore` 2× (L1641 + L1695) | Einmal reicht bei Sweep-Miss | **Ja** (minor) |
| `refreshPane` statt Cull-only-Pfad | Teilweise | **Ja**, wenn Sweep-Cache viewport-unabhängig |
| Segment-Cache Fast-Path | Nur bei identischem `renderKey` | Pan verhindert Hit |

### 3.8 Existierender schneller Pfad

| Pfad | Bedingung | Pan/Zoom |
|------|-----------|----------|
| `refreshPaneImpl` L1583–1602 | `fullyCached` + `segmentCache.key === renderKey` | **Miss** bei Pan |
| `sweepCacheHit` L1680–1690 | Sweep-Key match | **Miss** bei Pan |
| `repaintPaneFromCache` (L1990) | Nur Min-Depth / Reapply | **Nicht** von `scheduleViewport` genutzt |
| `research_charts.js` L2863 `repaintExactOriginDensityFromCache` | Pane resize o.ä. | nicht Viewport-Pan |

**Minimal-Fix (RC-B1):** Sweep-Cache-Key auf `(epoch, scopeKey, loadSpanFrom, loadSpanTo, ext)` — **ohne** viewport-sweepT; Cull/Merge pro Viewport aus gecachten `zones`.

**Minimal-Fix (RC-B2):** Nach Chunk-Hit ohne neuen HTTP: „viewport-only“-Pfad — wenn `epoch` gleich, überspringe `poolsToExactOriginZones`, nur `cull`+`merge` (+ optional neuer `renderKey` in Segment-Cache).

**Minimal-Fix (RC-B3):** `setExactOriginDensity` — shallow compare Segment-Referenz/Length/Hash vor `drawEodDensity`.

---

## 4. Audit C — Messgenauigkeit (P2 → P3)

P2-Limitierungen (**CONFIRMED** aus Code-Review `research/mc_pool_p2/MC_POOL_P2_BROWSER_MEASUREMENT_CONSOLE.js`):

| Issue | P2-Verhalten | P3-Korrektur |
|-------|--------------|--------------|
| PerformanceObserver | `installObservers` bei jedem `start()` ohne `disconnect` | Ein Observer; `stop()` → `disconnect()` |
| Long-Task-Zeit | `mono()` bei buffered Entries falsch relativ | `startTime` + `duration`; Dedupe via `startTime\|duration` |
| HTTP-Zählung | Section-Delta = completed JSON parses | `window_http_started` / `completed` / `error`; optional `parseBody: false` |
| Fetch-Overhead | `res.clone().json()` immer | Parsing opt-in `setParseWindowBodies(true)` |
| chart_tf | `#researchTimeframe` null | Pane-TF aus sichtbarem Iframe / `getLastMetrics()` |
| Adapter-Metriken | Snapshot | `refreshPane`/`scheduleViewport` Wrap + `mark('refresh_end')` Samples |

**Lieferung:** `research/mc_pool_p3/MC_POOL_P3_BROWSER_MEASUREMENT_CONSOLE.js` (temporär, nicht im Dashboard gebunden).

**Bestehende Diagnose nutzen:** `DogeExactOriginTrace` (`DOGE_EXACT_ORIGIN_TRACE=1`), `PD1ExactOriginResearch.getLastMetrics()`, `chart.getExactOriginDensityDrawStats()`.

---

## 5. Root-Cause-Matrix mit Minimal-Fixes

| ID | Root Cause | Klassifikation | Minimal Fix | Invalidierung |
|----|------------|----------------|-------------|---------------|
| RC-A1 | OFF löscht Window-Chunks mit Canvas | **CONFIRMED** | Visuelles `strip` ohne `resetAllWindowStores` | Symbol, PD, Pool-TF, Replay, Bounds-Advance |
| RC-A2 | ON mit `force: true` nach Toggle | **CONFIRMED** | Conditional refresh ohne `force` wenn Store warm | Wie RC-A1 |
| RC-B1 | Sweep-Key enthält viewport-sweep | **CONFIRMED** | Key auf loadSpan+epoch | epoch-Wechsel bei neuem Chunk |
| RC-B2 | Kein viewport-only Pfad nach Chunk-Hit | **CONFIRMED** | Cull/Merge aus `sweepCache.zones` | epoch / loadSpan |
| RC-B3 | `setExactOriginDensity` ohne Payload-Gleichheit | **SUSPECTED** | Compare-before-draw | min depth filter ändert segments |
| RC-C1 | Viewport-Coalescing + supersede erzeugt Mehrfachläufe | **CONFIRMED** (Design) | Debounce tuning / viewport-only queue | loadSerial Guards beibehalten |
| RC-D1 | Server-LRU-Hit löscht nicht Client-HTTP | **CONFIRMED** | Akzeptiert oder HTTP conditional (nicht empfohlen ohne Server-Contract) | — |

**Erwarteter Performance-Vorteil (UNGEMESSEN):**

- RC-A1+A2: Test B → 0 HTTP, ~0 adapterMs für Fetch-Anteil (**NOT MEASURED**).
- RC-B1+B2: Test C/E → adapterMs von ~100–150 ms Richtung Cull-only (**NOT MEASURED**; P3-Harness nötig).
- RC-B3: Canvas bei Zoom **NOT MEASURED** (Draw bereits 1–2 ms).

---

## 6. Before/After Equality-Testplan

Gleichheit = gleiche **sichtbare** Dichte-Segmente (nach Min-Depth-Filter), gleiche Pool-Metadaten, kein Stale nach Invalidierung.

| # | Szenario | Assert |
|---|----------|--------|
| E1 | Test A Baseline | Export `mergedSegments`, `rawZones`, Screenshot, Segment-Sample-Hash (P3) |
| E2 | RC-A1 Toggle B | Nach Fix: 0 HTTP; Segmente === E1 (±0) |
| E3 | Slow pan C | Gleiche Pools im Tile; `mergedSegments` ändert sich nur durch Cull (Subset-Monotonie) |
| E4 | Fast pan D | Neuer Tile: HTTP +1; keine fehlenden Lücken im sichtbaren Bereich |
| E5 | Symbolwechsel | Stores leer; **keine** alten SOL-Segmente auf neuem Symbol |
| E6 | Live-Bounds +1 Close | Nach Caps-Refresh: Window inkl. neuem Close ≠ stale (**NOT MEASURED** — Zeitpunkt definieren) |
| E7 | Replay-Gen | Chunk-Store reset, keine Cross-Gen-Pools |

Vergleichswerkzeug: P3 `exportJSON()` + optional `DogeExactOriginTrace` Stage `refreshPane_render`.

---

## 7. Risiken & Invalidierungsregeln (Fix-Phase)

1. **Niemals** Window-Chunks über Symbol-Grenzen ohne `resetAllWindowStores`.
2. **Immer** `bumpExactOriginLoadSerial` bei Symbol/TF/PD — unverändert.
3. Sweep-Cache viewport-unabhängig: bei neuem Chunk in Store **epoch** ändert sich (`chunkStoreEpoch`, L560–570) → automatischer Miss.
4. **Kein** unbounded RAM — LRU `EOD_WINDOW_CHUNK_CACHE_MAX` beibehalten.
5. Server Response Cache / Member Prefill: **nicht** umbauen ohne Nachweis.
6. Keine Pool-Mathematik-/Lifestyle-Änderungen (User-Constraints).

---

## 8. Browser-Messanleitung (Phase P3 / Post-Fix)

1. `/live-charts/research` — SOLUSDT, 5m, Exact Origin 5m PD1 an, Historical LMv2 aus.
2. DevTools Console (Top-Frame): Inhalt von `MC_POOL_P3_BROWSER_MEASUREMENT_CONSOLE.js` einfügen.
3. `__MC_POOL_P3__.start({ parseWindowBodies: false })`
4. Tests A–E wie P2; je Test `startSection('B', '…')` … Aktion … `endSection()`; zwischen Tests `sampleViewport('after_B')`.
5. Optional: `localStorage DOGE_EXACT_ORIGIN_TRACE=1` — Konsole `TRACE | …` korrelieren.
6. `__MC_POOL_P3__.exportJSON()` — prüfen: `http.started` vs `completed`, `refresh_runs[]`, `long_tasks_deduped`, `frame_times`.
7. `__MC_POOL_P3__.stop()`

**Fix-Regression:** Gleiche Schritte mit vorher exportiertem E1-JSON (Segment-Counts, keine HTTP in B/C innerhalb Tile).

---

## 9. Code-Referenz — Kernzeilen (Spread SSOT)

| Thema | Datei | Zeilen |
|-------|-------|--------|
| strip → reset stores | `static/js/research/pd1_exact_origin_density_research_v1.js` | 727–732, 591–595, 542–548 |
| Checkbox install | ebenda | 2076–2099 |
| refresh + force | `static/js/research/research_charts.js` | 271–281 |
| Coalescing / serial | `pd1_exact_origin_density_research_v1.js` | 1388–1422, 1375–1385 |
| fullyCached shortcut | ebenda | 1577–1602 |
| fetch + chunk hit | ebenda | 1146–1217; Adapter 492–505 |
| sweep cache | ebenda | 1673–1719, 573–588 |
| adapterMs | ebenda | 1463, 1720, 1811 |
| scheduleViewport | ebenda | 1966–1978 |
| Viewport hook | `static/js/research/research_charts.js` | 2888–2902 |
| Canvas push/draw | ebenda L631–641; `static/research_trp/chart.js` | 4917–4947 |
| Window planner | `static/js/research/pd1_exact_density_trp_adapter.js` | 370–428, 354–367 |

---

## 10. Abgrenzung / Nicht-Ziele

- Keine Änderung Pool-Mathematik, D2 Density, MC-LOAD-P4J Server-Pipeline.
- Keine Reduktion Pool-Anzahl, keine künstlichen Caps.
- Keine Trading-/Order-Logik.
- Performance-Zahlen in Abschnitt 6 als **UNGEMESSEN** bis P3-Harness oder Post-Fix-Lauf.

---

**MC_POOL_P3_TARGETED_AUDIT_COMPLETE**