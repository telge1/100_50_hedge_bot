# PHASE 6XT – MULTI-PD EXACT ORIGIN-DENSITY RENDER COMPLETENESS AUDIT V1

**Date:** 2026-10-21  
**Mode:** Read-only (keine Produktiv-Änderungen, kein Dashboard-Restart)

## VERDICT

**`MULTI_PD_RENDER_COMPLETENESS_NOT_VERIFIED`**

**Begründung (kurz):** Für das deterministische Referenzfenster ist die **Multi-PD-Komposition bis einschließlich Min-Depth=2** reproduzierbar **ohne Identitätsverlust** nachgewiesen (Multiset-Inklusion 0 fehlend). **Nicht** belegt sind: echte Browser-Canvas-`drawEodDensity`-Sichtbarkeit, chart-internes Viewport-Clipping beim Draw, visuelle Occlusion (gleiche Farben/Alpha), sowie Live-JS-Cache/Inflight unter Checkbox-Wechsel.

**Teilbefund:** Kein Hinweis auf **`MULTI_PD_COMPOSITION_DATA_LOSS_FOUND`** im geprüften Pfad.

**Artefakte:** `logs/historical_import/overnight_2026/phase6xt_5m_multi_pd_render_completeness_audit_v1/`  
(`export_window_pools.py`, `render_pipeline_audit.js`, `window_pools_summary.json`, `render_pipeline_audit.json`)

---

## 1. Testszenario

| Feld | Wert |
|------|------|
| Symbol | DOGEUSDT |
| Chart-TF | **5m** |
| Modus | B – Exact Origin-Density (Canvas-Pfad angenommen: `localStorage phase6xt_eod_density_canvas_v1=1`) |
| PDs (Kernfall) | **PD1 + PD5 + PD10** |
| Min-Depth | **2** |
| **Deterministisches Fenster** | `2026-09-16T00:00:00Z` … `2026-09-17T23:55:00Z` (576 × 5m-Preise je PD) |
| Screenshot-Viewport | **Nicht rekonstruiert** — keine Pixel-/Viewport-Parität behauptet |

Sweep-Pad wie Produktion: ±2h; Load-Span via `computeDensityLoadSpanMs` (168h-Prefetch-Kontext ab `2026-09-09`).

---

## 2. Datenfluss (belegte Funktionen)

```
CH Publish-Line (je PD)
  → build_landscape_window (Python, Phase-5S batch reader)
  → pools[] (+ prices[], provenance)
  → [Browser] fetchWindow → exactOriginWindowStores[tf⌑pd] (Pools, nicht Rechtecke)
  → poolsToExactOriginZones (Origin-Sweep, 5m slotMinutes=5)
  → cullZonesToViewport
  → mergeExactOriginSegments (nur innerhalb einer PD)
  → concat über scopePlans-Reihenfolge (1→5→10 bei Standard-Checkboxen)
  → filterSegmentsByMinOrigins (global, gleiche Regel je PD)
  → setExactOriginDensity → drawEodDensity (chart.js)
```

**Publish-Lines (je PD verifiziert in Export):**

| PD | Line | Pools (API) | Preise |
|----|------|------------:|-------:|
| 1 | `…_5mpd1_v1` | 599 | 576 |
| 5 | `…_5mpd5_v1` | 692 | 576 |
| 10 | `…_5mpd10_v1` | 913 | 576 |

Keine Vermischung der Lines im Python-Export.

---

## 3. Ergebnismatrix (Referenzfenster, Min-Depth=2)

Spaltenbedeutung:

- **API-Sweep-Zones (A):** `poolsToExactOriginZones` Rohausgabe (kein REST-„Rechteck“-Feld; Sweep über alle 5m-Slots im Sweep-Span).
- **Nach Viewport-Cull (C):** `cullZonesToViewport` — hier identisch mit A (volles Pilotfenster).
- **Composer (D):** `mergeExactOriginSegments` **pro PD**, dann `concat`.
- **Min-Depth=2 (B/E):** `filterSegmentsByMinOrigins`.
- **Draw-Sim:** alle Segmente würden **ohne** `chart.timeScale().getVisibleRange()` gezeichnet (`fillRect` pro Segment) — **nicht** Produktions-Canvas.

| PD / Kombi | API-Sweep-Zones | Composer | Min-Depth=2 | Upper | Lower | Fingerprint (geom) |
|------------|----------------:|---------:|------------:|------:|------:|-------------------|
| PD1 | 150 685 | 2 080 | **1 473** | 671 | 802 | `927a7082…` |
| PD5 | 100 390 | 1 201 | **556** | 342 | 214 | `e5764ecb…` |
| PD10 | 85 981 | 876 | **205** | 119 | 86 | `f2eb9fab…` |
| PD1+5 | — | 3 281 | **2 029** | — | — | `345d175e…` |
| PD1+10 | — | 2 956 | **1 678** | — | — | `896671f2…` |
| PD5+10 | — | 2 077 | **761** | — | — | (s. JSON) |
| **PD1+5+10** | — | **4 157** | **2 234** | — | — | `3431751c…` |

**Summe Einzel-PD (Min-Depth=2):** 1 473 + 556 + 205 = **2 234** = Triple-Kombi (**keine fehlenden geom-Keys**, `missing_from_multi: 0` je PD).

**JS-Cache (C):** speichert **Pools** pro `scopeKey`+Tile, nicht Rechtecke; Segment-Cache (`exactOriginSegmentCache`) erst nach Komposition — im Audit **nicht** browser-instrumentiert.

---

## 4. Multi-PD-Composition (Code + Audit)

| Prüfpunkt | Ergebnis |
|-----------|----------|
| Drei unabhängige Pool-Arrays | **Ja** — je `gatherPoolsFromStore` pro Scope |
| Vollständige concat nach per-PD-merge | **Ja** — `mergedSegments.concat(...)` |
| Reihenfolge 1→5→10 | Standard `POOL_DETAIL_VALUES`; **Fingerprint unabhängig von Reihenfolge** (`order_sensitive_fingerprint: false`) |
| Map/Set-Deduplikation über PDs | **Nein** — kein `mergeDensityPools` über Scopes |
| Scope-Cache-Überschreibung | **Nein** — getrennte `exactOriginWindowStores["5m⌑pd"]` |
| `mergeDensityPools` nur innerhalb Tiles **einer** Line | **Ja** (unverändert) |
| Globales yMin/yMax im Triple | Union aller aktiven Pools; im Pilot **identisch** zu PD1-only (`ext_y_range` gleich) |
| Segment-Cap 8000 | **Nein** — 4 157 < 8 000 |
| Stale Inflight | Code: `loadSerial` / `replayGen` — **NOT_VERIFIED** live |

---

## 5. Einzel vs. Gesamt (Multiset-Inklusion, Min-Depth=2)

| Kombination | Inklusion vs. Einzel-PD |
|-------------|-------------------------|
| PD1+5+10 | PD1: 0 missing; PD5: 0; PD10: 0 |
| PD1+5 | PD1: 0; PD5: 0 |
| PD1+10 | PD1: 0; PD10: 0 |
| PD5+10 | PD5: 0; PD10: 0 |

Vergleichsidentität: `side|t0|t1|bottom|top|depth|color|opacity` (temporär, nur Audit).

---

## 6. Visuelle Überlagerung / Occlusion

| Thema | Status |
|-------|--------|
| Draw-Order | PD1-Segmente zuerst, dann PD5, dann PD10 (concat-Reihenfolge) |
| Farben | Upper violett / Lower blau — **gleich für alle PDs** |
| Alpha | `opacityForDepth(bucket)` 0.35–0.78 |
| Deckungsgleiche Rechtecke über PDs | möglich; späterer Draw übermalt früheren (**DRAWN_BUT_OCCLUDED** möglich) |
| Browser-Nachweis | **NOT_VERIFIED** |

---

## 7. Regressionsschutz (read-only)

| Check | Ergebnis |
|-------|----------|
| 15m PD1 | **97** Preise, Line `…_15mpd1_v1` |
| Live API 4 Scopes | **4** (Activation-Probe unverändert) |
| Min-Depth 1 / 3 (PD1, gleiches Fenster) | 1 473 → höhere Zählung bei depth=1; depth=3 niedriger (s. JSON) |
| 48h-Tile-Isolation | Code: `scopeKey` in Tile-Keys — **nicht** separat im Audit replayed (Sliding-Planner im Replay) |
| Historical LLD V2 | Nicht Teil dieses Pfads |

---

## 8. Was für `MULTI_PD_ALL_RECTS_RENDERED_VERIFIED` fehlt

1. Authentifizierter Browser-Lauf mit realem Chart-Viewport (nicht nur volles Pilotfenster).  
2. `getExactOriginDensityDrawStats()` / `segmentsDrawn` vs. `segmentsTotal` unter sichtbarem Zoom.  
3. Occlusion-Klassifikation (DRAWN vs. OCCLUDED vs. NOT_DRAWN) mit Canvas-Zugriff.  
4. Checkbox-Wechsel + Inflight-Stale-Test im laufenden Dashboard.

---

## 9. STOP

Read-only Audit abgeschlossen. Keine Code-, CH- oder Dashboard-Änderungen.
