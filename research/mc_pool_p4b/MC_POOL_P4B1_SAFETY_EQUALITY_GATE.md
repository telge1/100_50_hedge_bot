# MC-POOL-P4B1 — Cache Lifecycle Safety & Equality Gate

**Endstatus:** `MC_POOL_P4B1_SAFETY_PASS_BROWSER_PENDING`  
**Datum:** 2026-11-26  
**Code-SSOT:** `spread_recovery_hedge_short_dev/dashboard/`  
**Vorgänger:** [P4B Implementierung](./MC_POOL_P4B_CACHE_LIFECYCLE_IMPLEMENTATION.md)

**Kein Deployment · kein Service-Restart · kein Commit**

---

## 0. Ergebnis auf einen Blick

| Kategorie | Status |
|-----------|--------|
| Freshness-Guard Code-Review | **Durchgeführt** — 1 Lücke geschlossen (P4B1-Micro-Fix) |
| Policy-Tests (Node) | **13/13 PASS** (inkl. P4B1) |
| MC-LOAD-P4J pytest (statisch) | **11/11 PASS** |
| Race-Condition | **Code-Review + bestehende Serial-Guards** — kein Browser-E2E |
| Inhaltliche Equality Pool/Segment | **NICHT nachgewiesen** — Browser offen |
| Multi-Pane Isolation | **Code-Review PASS** — Browser offen |
| Live-Dashboard liefert P4B+fix | **Nur nach Datei-Sync + Cache-Bust** — siehe §6 |

**P4B1-Micro-Fix (lokal):**

1. `exactOriginReenableFreshnessBaselineKnown` — kein `repaintPaneFromCache`, wenn bei OFF kein `stripCapsHiMs` erfasst wurde.
2. `refreshPaneImpl` — `if (!isPaneExactOriginEnabled(pane)) return` unmittelbar vor Canvas-Render (Schutz gegen verspätetes Nachrendern nach OFF).

---

## 1. Freshness-Guard — Verifikation

### 1.1 Call-Chain Re-Enable

```
refreshAllAfterReEnable / refreshPaneAfterReEnable
  → exactOriginReenableScopeUnchanged? → sonst resetAllWindowStores + force
  → ensurePaneViewerBounds(pane)     // GET viewer-bounds, mutiert caps
  → loadCaps()                         // Modul-Singleton; ggf. refreshDoge5mViewerBoundsIfStale (30s)
  → dataBoundsMsForScopes(caps, scopes, sym) → bounds.hiMs
  → exactOriginReenableCapsHiAdvanced(pane, bounds.hiMs)
  → exactOriginReenableFreshnessBaselineKnown(pane)
  → fullyCached + segMatch + !hiAdvanced + freshnessKnown → repaintPaneFromCache
  → else refreshPane(pane, {})
```

**Referenz:** `pd1_exact_origin_density_research_v1.js` — `refreshPaneAfterReEnable` (~L1986), `recordPaneVisualStripState` (~L609), `loadCaps` (~L892), `refreshDoge5mViewerBoundsIfStale` (~L851).

### 1.2 Woher kommt `hiMs` beim Re-Enable?

| Quelle | Pfad |
|--------|------|
| **Primär** | `dataBoundsMsForScopes(capsRoot, scopes, paneSym)` nach `ensurePaneViewerBounds` + `loadCaps` |
| **Caps-Wurzel** | `GET /live-charts/viewer/api/capabilities` (einmalig, `_loadCapsReady`) |
| **Live-Kante SOL/BNB** | `GET …/multicoin-5m-pd1-viewer-bounds?symbol=` in `ensurePaneViewerBounds` **jedes Re-Enable** |
| **Staleness innerhalb 30s** | `loadCaps()` HIT ruft `refreshDoge5mViewerBoundsIfStale` nur wenn `now - _doge5mBoundsFetchedAt ≥ 30000` (`DOGE_5M_BOUNDS_REFRESH_MS`) |

### 1.3 Kann `hiMs` aus veraltetem Caps-Cache stammen?

**Ja, eingeschränkt (SUSPECTED, nicht Browser-verifiziert):**

- Das **Capabilities-JSON** (`/viewer/api/capabilities`) bleibt im Modul-`caps` bis `invalidateCapsCache`.
- **`data_available_to` für 5m PD1 SOL** wird über **`applyDoge5mViewerBoundsToCaps`** aus **Viewer-Bounds** überschrieben, nicht nur aus Roh-Caps.
- **Re-Enable** holt Bounds **sofort** via `ensurePaneViewerBounds` (vor `loadCaps`-Rückgabe für hi-Vergleich) → **hiMsNow ist typisch aktuell**.
- **Restrisiko:** Bounds-GET scheitert still → `hiAdvanced` bleibt false; dann greifen **Chunk-`satisfiesWindow`** + Soft-`refreshPane` (kein blindes Repaint mehr ohne Baseline, siehe P4B1-Fix).

### 1.4 Neue 5m-Publication während OFF

| Mechanismus | Erkennung |
|-------------|-----------|
| `stripCapsHiMs` bei OFF vs. `bounds.hiMs` bei ON | `exactOriginReenableCapsHiAdvanced` → Segment/Sweep invalidiert, kein Repaint |
| `exactOriginChunkSatisfiesWindow` (`ent.toMs < w.toMs`) | Soft `refreshPane` fordert **nur fehlende** Tiles an |
| Baseline unbekannt (`stripCapsHiMs == null`) | **P4B1:** kein `repaintPaneFromCache`, Soft-Refresh |

**CONFIRMED (Design):** Publication über Live-Kante wird nicht allein über Segment-Repaint ignoriert, wenn hi advance erkannt oder Chunks unvollständig.

**NOT MEASURED:** Echter SOL-Live-Close während OFF im Browser.

### 1.5 Kann `repaintPaneFromCache` veraltete ACTIVE Pools zeigen?

**Vor P4B1:** Ja, wenn `hiAdvanced` nicht feuert (z. B. Baseline `null` oder Bounds-Fehler).

**Nach P4B1:** Repaint nur bei `freshnessKnown && !hiAdvanced && fullyCached && renderKey match`. Sonst `refreshPane({})` mit kanonischer Pipeline.

**Verbleibendes Risiko (dokumentiert, kein Fix in P4B1):** `hiAdvanced` false trotz neuer Publication, wenn OFF- und ON-Bounds identisch fehlerhaft/stale **und** Segment-Cache inhaltlich veraltet aber **Counts/Key gleich** → **inhaltlicher Equality-Browser-Test nötig**.

### 1.6 Viewport-Coverage während OFF

**CONFIRMED:** `renderKey = viewportRenderKey(hit) + "|" + scopeKey` — Viewport-Pan während OFF ändert `hit` → `segMatch` false → kein Repaint-Shortcut → `refreshPane({})` mit aktuellem `resolveViewSec`.

Geplante Windows: `planExactOriginLoadWindows(loadSpan...)` — bei Viewport-Änderung anderer `loadSpan` / ggf. zusätzliche Chunks → `fullyCached` false oder selektiver Fetch.

### 1.7 Unbekannte Freshness → sicherer Pfad

**CONFIRMED (P4B1):** `!freshnessKnown` → Segment/Sweep null, **kein** `repaintPaneFromCache`, nur `refreshPane({})` oder `force` bei Scope-Bruch.

---

## 2. Single- und Multi-Chart

| Modus | OFF | ON |
|-------|-----|-----|
| **Single** | Checkbox → `stripAllPanesVisual` | `refreshAllAfterReEnable` |
| **Multi** | `setMultiPaneExactOriginEnabled` → `stripPaneVisual(pane)` | `refreshPaneAfterReEnable(pane)` |

**Cross-Pane (Multi):**

- Stores liegen auf **`pane.exactOriginWindowStores`** pro Pane — OFF eines Panes berührt andere nicht.
- `refreshAllAfterReEnable` iteriert nur Panes mit `isPaneExactOriginEnabled(pane)`.

**Cross-Symbol:**

- Chunk-Keys: `sym + "|" + timeframe/pd` (`exactOriginScopeKeyForScope`, ~L208).
- Symbolwechsel: `exactOriginReenableScopeUnchanged` false → `resetAllWindowStores` + `force`.

**Code-Review:** **PASS** — kein gemeinsamer globaler Window-Store.

**Browser Multi-Pane:** **OFFEN**

---

## 3. Race-Condition-Analyse

| Szenario | Schutz | Ausgeführt |
|----------|--------|------------|
| OFF während Window-Request | `bumpExactOriginLoadSerial` in `stripPaneVisual`; Fetch/worker bricht bei Serial-Mismatch ab (~L1237, L1253) | Code-Review |
| Verspätete Response nach OFF | Kein `touchExactOriginWindowCache` bei Serial-Mismatch; Render blockiert ~L1835 | Code-Review |
| **Verspätetes Render nach OFF** | **P4B1:** `!isPaneExactOriginEnabled` vor `pushCanvasDensity` (~L1849) | Code-Review |
| Schnelles OFF→ON→OFF | Serial-Monotonie + Checkbox-State | **OFFEN** (Browser) |
| Symbol/TF/PD während OFF | `exactOriginReenableScopeUnchanged` → hard reset | Policy-Test |
| Publication während OFF | hi-Guard + Chunk-satisfies | Design + Policy |
| Viewport während OFF | `renderKey` bei ON | Design |
| LRU während OFF | Unverändert `EOD_WINDOW_CHUNK_CACHE_MAX=32` | Kein Test |

`scheduleSupersededDensityRefresh`: prüft `isPaneExactOriginEnabled` beim Timer (~L2112) — kein Refresh nach OFF.

---

## 4. Inhaltliche Equality

**NICHT durchgeführt** — keine Fixtures mit vollständigen Pool-Objekten/Segment-Payloads im Repo.

| Feld | Nachweis |
|------|----------|
| Pool-IDs, Lifetimes, Membership, Density | **FEHLT** |
| Segment-Zeit/Preisgrenzen | **FEHLT** |
| Canvas-Payload | **FEHLT** |

**Vergleichspfad (Browser, manuell):**

1. Test A: `setParseWindowBodies(true)` einmalig, Pool-Hash Stichprobe **oder** Export Server-Response vor OFF (optional).
2. Test B Post-P4B: gleicher Viewport, `windowFetch.fetched===0`, visueller Diff + Metrik-Snapshot.
3. Kein PASS auf reine Count-Gleichheit (P4A-Baseline 1100 Pools / 673 merged war nur Vorfilter).

**Ehrliche Deklaration:** P4B erfüllt Akzeptanz **inhaltlich** erst nach Browser-Equality — **nicht** in P4B1.

---

## 5. Regression MC-LOAD-P4J

**Ausgeführt** (Spread `dashboard/`):

```bash
python3 -m pytest test_mc_load_p4i_v1.py test_mc_load_p4g_v1.py test_mc_load_p4e_v1.py test_mc_load_p3_fast_first_v1.py -q
```

**Ergebnis:** **11 passed** (Hooks `runFinalPaneViewResetLikeToolbar`, Asset-Token, Fast-Path-Marker in `research_charts.js` / Template).

**Nicht geprüft:** Laufzeit-Browser (EMA, Zentrierung, Candle-History) — P4B ändert nur Exact-Origin-Lifecycle, **keine** Änderung an `loadPane`/Viewport/MC-LOAD-Pfade.

---

## 6. Deployment-Realität (READ-ONLY)

### 6.1 Ausgelieferte Dateien (Default-Template)

| Asset | Pfad | Query `?v=` |
|-------|------|-------------|
| Exact Origin (kanonisch) | `/static/js/research/pd1_exact_origin_density_research_v1.js` | `pd1-mc-multi-pane-pools-v5` |
| Host | `/static/js/research/research_charts.js` | `research-mc-load-p4i-v1` |

Alternative Branch-Skripte (mc5e/mc5h/…) nur bei **Query-Param** `pd1=…` im Template — Standard-SOL-Produktion nutzt **kanonische** Datei oben.

### 6.2 Liefern geänderte Dateien das Live-Dashboard?

- **Typisch:** Flask/Gunicorn serviert `dashboard/static/` **direkt** — **kein Python-Restart** für reine `.js`-Änderungen.
- **Voraussetzung:** Laufender Prozess **CWD / PYTHONPATH** zeigt auf `spread_recovery_hedge_short_dev/dashboard` (Baseline-Audit: Live-Dirty-Tree).
- **Browser:** Feste `?v=` → **Hard-Refresh reicht nicht immer**; für sichere Invalidierung **`?v=` bump** in `research_charts.html` empfohlen (nur bei Freigabe, nicht in P4B1 ausgeführt).

### 6.3 Service-Restart

**Nicht erforderlich** für statische JS-Dateien, sofern Prozess bereits aus Spread-`dashboard/` liest.

### 6.4 Kontrollierte Aktivierung / Rollback

| Aktion | Schritte |
|--------|----------|
| **Aktivieren** | Geänderte `pd1_exact_origin_density_research_v1.js` + `research_charts.js` im Live-Tree; optional `?v=` bump; Browser Hard-Refresh |
| **Rollback** | `git checkout --` der zwei JS-Dateien im Spread-`dashboard/` (siehe P4B §10) |

**Hinweis:** P4B ist **lokal im Spread-Tree** geändert; ob Live-Prozess dieselbe Datei liest, hängt vom **Deployment-Layout** ab — **vor Browser-Test verifizieren** (Dateigröße/Mtime oder kurzer `grep stripPaneVisual` über HTTP).

---

## 7. Ausgeführte vs. offene Tests

### 7.1 Tatsächlich ausgeführt

| Test | Tool | Result |
|------|------|--------|
| Policy Guards | `node test_mc_pool_p4b_cache_lifecycle_v1.js` | **13 PASS** |
| Coalesce (unverändert) | `node test_p24e_coalesce_logic_v1.js` | **5 PASS** |
| MC-LOAD statisch | `pytest` 4 Dateien | **11 PASS** |

### 7.2 Policy-only (kein DOM)

Scope/Symbol/TF-Guards, hi-Advance, Repaint-Allow-Matrix, Store-Preservation vs. Full-Reset.

### 7.3 Offene Browser-Verifikation (Pflicht vor Freigabe)

- Test B: 0 POST, Chunk-Erhalt, **inhaltliche** Gleichheit
- Race: OFF während Network pending
- Multi-Pane: Pane A OFF, Pane B ON
- Publication-during-OFF (kurzer Live-Warteblock)
- Performance Re-ON &lt; 200 ms — **nicht behauptet**

---

## 8. Geänderte Dateien in P4B1 (zusätzlich zu P4B)

| Datei | Änderung |
|-------|----------|
| `pd1_exact_origin_density_research_v1.js` | `exactOriginReenableFreshnessBaselineKnown`; Repaint-Gate; Render-Guard `isPaneExactOriginEnabled` |
| `test_mc_pool_p4b_cache_lifecycle_v1.js` | +5 Tests |

---

## 9. Rollback P4B1

Zusammen mit P4B-Rollback oder nur die drei genannten Zeilen-/Funktionsblöcke revertieren.

---

## 10. OUT OF SCOPE (unverändert)

Pan/Sweep/Live-Delta (P4A C–H, G-Duplicate-POST), Pool-Mathematik, Server-CH.

---

**MC_POOL_P4B1_SAFETY_PASS_BROWSER_PENDING**