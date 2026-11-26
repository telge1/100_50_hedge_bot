# MC-POOL-P4B — Exact Origin Cache Lifecycle Optimization

**Status:** `MC_POOL_P4B_FIX_READY_BROWSER_PENDING`  
**Datum:** 2026-11-26  
**SSOT (implementiert):** `spread_recovery_hedge_short_dev/dashboard/`  
**Scope:** OFF→ON Toggle Cache Lifecycle nur (kein Pan/Sweep/Live-Delta)

**Nicht deployed · nicht committed · kein Service-Restart**

---

## 0. Zusammenfassung

Beim reinen **Exact-Origin Ausschalten** werden Window-Chunks und Segment-Cache im RAM behalten; nur Canvas/Overlays werden entfernt und `exactOriginLoadSerial` erhöht (kein verzögertes Nachrendern).

Beim **Wiedereinschalten** läuft `refreshAllAfterReEnable` / `refreshPaneAfterReEnable` statt pauschal `force: true`: Segment-Repaint aus Cache wenn Scope, Freshness und `fullyCached` passen; sonst `refreshPane({})` (bestehende Chunk-Hits + ggf. selektiver Fetch).

**Verifizierte P4A-Baseline Test B (vor Fix):** 2 Chunks vor OFF → 0 während OFF → 2 POSTs + ~518 ms adapter nach ON.

**Browser-Nachmessung:** ausstehend (Pflicht für Freigabe).

---

## 1. Geänderte Dateien und Funktionen

| Datei | Änderung |
|-------|----------|
| `static/js/research/pd1_exact_origin_density_research_v1.js` | Visual/Data-Trennung, Re-Enable-Pfad, Freshness-Guards |
| `static/js/research/research_charts.js` | Multi-Pane ON/OFF nutzt `refreshPaneAfterReEnable` / `stripPaneVisual` |
| `static/js/research/test_mc_pool_p4b_cache_lifecycle_v1.js` | **neu** — Node Policy-Tests |

### 1.1 Neue / geänderte Funktionen (`pd1_exact_origin_density_research_v1.js`)

| Funktion | Rolle |
|----------|--------|
| `clearExactOriginVisualOnPane` | Canvas leeren **ohne** Store-Reset |
| `recordPaneVisualStripState` | Serial bump, Strip-Metadaten (Scope, TF, Symbol, `caps hiMs`) |
| `exactOriginReenableScopeUnchanged` | Guard gegen Cross-Scope/Cross-Symbol |
| `exactOriginReenableCapsHiAdvanced` | Live-Hi-Wasserzeichen seit OFF |
| `stripPaneVisual` / `stripAllPanesVisual` | Toggle-OFF (Daten bleiben) |
| `refreshPaneAfterReEnable` | Soft Re-ON mit Cache/Repaint |
| `refreshAllAfterReEnable` | Single-Chart Checkbox ON |
| `stripPane` / `clearCanvasOnPane` | **unverändert** — voller Reset bei PD/TF/Symbol-Pfaden |

**Export:** `PD1ExactOriginResearch.stripPaneVisual`, `stripAllPanesVisual`, `refreshPaneAfterReEnable`, `refreshAllAfterReEnable`, Guards.

---

## 2. Vorher / Nachher Call-Chain

### 2.1 Toggle OFF (Single-Chart)

**Vorher:**

```
#researchIndExactOriginDensity change (OFF)
  → stripAllPanes → stripPane → clearCanvasOnPane → resetAllWindowStores
```

**Nachher:**

```
#researchIndExactOriginDensity change (OFF)
  → stripAllPanesVisual → stripPaneVisual
       → recordPaneVisualStripState (bumpExactOriginLoadSerial)
       → clearExactOriginVisualOnPane
       → overlays strip (unchanged)
```

### 2.2 Toggle ON (Single-Chart)

**Vorher:**

```
ON → refreshAll({ force: true }) → refreshPaneImpl (force bypasses cache shortcuts)
```

**Nachher:**

```
ON → refreshAllAfterReEnable()
  → refreshPaneAfterReEnable(pane)
       → scope changed? → resetAllWindowStores + refreshPane({ force: true })
       → hiMs advanced? → segment/sweep null, refreshPane({})
       → fullyCached + segment key + !hiAdvanced? → repaintPaneFromCache
       → else refreshPane({})  // existing fullyCached shortcut in refreshPaneImpl
```

### 2.3 Multi-Manual (`research_charts.js`)

| Event | Vorher | Nachher |
|-------|--------|---------|
| Mini/toggle OFF | `stripPane` | `stripPaneVisual` |
| Mini/toggle ON | `refreshPane({ force: true })` | `refreshPaneAfterReEnable` |

### 2.4 Unveränderte harte Reset-Pfade

`onSymbolSwitch`, `onPaneTimeframeChange` (EO an), PD/Pool-TF-Änderung, `stripPane` bei Scope-Invalidierung — weiterhin `resetAllWindowStores`.

---

## 3. Cache-Invalidation-Matrix

| Ereignis | Window-Chunks | Segment | Sweep | Serial bump |
|----------|---------------|---------|-------|-------------|
| Toggle OFF (P4B) | **behalten** | **behalten** | **behalten** | **ja** (`exact_origin_visual_off`) |
| Toggle ON, Scope gleich, hi gleich | behalten | Repaint | — | nur bei `refreshPane` |
| Toggle ON, hiMs gestiegen | historische behalten | **invalidiert** | **invalidiert** | via refresh |
| Toggle ON, Scope/TF/Symbol geändert während OFF | **resetAllWindowStores** | null | null | ja + `force` |
| Symbol-Switch (EO an) | reset (`stripAllPanes`) | — | — | unverändert |
| LRU 32 Chunks | unverändert | — | — | — |

---

## 4. Freshness- und Generation-Guards

1. **`exactOriginLoadSerial`** — bei Visual-OFF bump; verhindert verzögerte Responses nach OFF (`refreshPaneImpl` Checks).
2. **Scope-Signatur** — `timeframe/pd` + Chart-TF + Symbol vs. Strip-Zustand.
3. **`exactOriginStripCapsHiMs` vs. aktuellem `dataBoundsMsForScopes.hiMs`** — bei Live-Advance: kein Segment-Repaint; Soft-`refreshPane` lädt fehlende Tiles (wie Test H Live-Pfad, unverändert).
4. **`exactOriginChunkSatisfiesWindow`** / **`fullyCached`** — unverändert (P4B nutzt sie).
5. **`replayGen`** Store-Reset in `refreshPaneImpl` — unverändert.

**Hinweis Test H (korrigiert):** Live-Aktualisierung kann über **automatische `scheduleViewport`**-Kette Window-POSTs auslösen (jüngster Tile). P4B **ändert diese Logik nicht**.

---

## 5. Automatisierte Testresultate

| Suite | Befehl | Ergebnis |
|-------|--------|----------|
| `test_mc_pool_p4b_cache_lifecycle_v1.js` | `node static/js/research/test_mc_pool_p4b_cache_lifecycle_v1.js` | **PASS** (8) |
| `test_p24e_coalesce_logic_v1.js` | `node static/js/research/test_p24e_coalesce_logic_v1.js` | **PASS** (5) |

**Nicht automatisiert (Browser / Equality):** A–K in Abschnitt 7 — **NOT RUN** in dieser Session.

---

## 6. Inhaltlicher Equality-Nachweis

**Kein vollständiger inhaltlicher Nachweis in P4B implementiert** (kein Pool-ID-Fingerprint im CI).

Geplante Browser-Verifikation (Test B Post-Fix):

- `chunk_stores` vor OFF = nach ON (gleiche `chunk_entries`)
- `sections.B.pool_window_started === 0`
- `viewport_samples` B: `last_metrics.windowFetch.fetched === 0`, `cacheHits` ≥ geplant
- Segment-Counts als **Vorfilter**; tieferer Content-Compare optional mit `setParseWindowBodies(true)` + manueller Pool-Stichprobe (EQ-Plan P4A)

---

## 7. Regression- / Equality-Checkliste (Browser ausstehend)

| ID | Erwartung Post-Fix | Automatisiert |
|----|-------------------|---------------|
| A OFF→ON gleicher Cache | 0 POST, Chunks erhalten | Harness B |
| B Inhaltliche Gleichheit | Stichprobe Pool-IDs/Segmente | Manuell |
| C Publication während OFF | Kein stale Segment-Repaint; ggf. 1 Live-Tile POST | Manuell |
| D Symbol OFF | Hard reset on re-enable | Policy test |
| E TF/PD OFF | `force` Pfad | Policy test |
| F Schnelles OFF→ON | Kein Race | Manuell |
| G OFF während inflight | Serial supersede | Manuell |
| H Late response | Kein Paint nach OFF | Serial bump |
| I LRU | Unverändert 32 | — |
| J Multi-Pane | Pane-isoliert | Manuell |
| K MC-LOAD-P4J | Keine Server/CH-Änderung | N/A |

---

## 8. Offene Risiken

| Risiko | Mitigation |
|--------|------------|
| Segment-Repaint obwohl nur Live-Tile fehlt | `hiAdvanced` → kein Repaint |
| Stale Segment trotz gleicher Counts | Browser Equality + optional Body-Parse |
| Chunks bleiben bei Symbol OFF im RAM (Scope-Key) | Re-ON mit Symbol-Guard → full reset |
| Pan/Sweep-Redundanz (C–G) | **OUT OF SCOPE** P4B |
| Test G duplicate POSTs | **OUT OF SCOPE** P4B |

---

## 9. Minimaler Browser-Verifikationstest (Freigabe)

1. Fix auf Spread-Dashboard laden (Hard-Refresh / neues `?v=` wenn nötig).
2. P4A-Harness wie in `MC_POOL_P4A_BASELINE_AND_FIX_PLAN.md`.
3. **Nur Test B** wiederholen + Snapshot A vor OFF speichern.
4. Pass wenn: `pool_window_started === 0`, Chunks 2→(OFF)2→(ON)2, Metriken ähnlich Test A.

Performance < 200 ms am Re-ON: **nicht gemessen / nicht behauptet**.

---

## 10. Rollback

```bash
# In spread_recovery_hedge_short_dev/dashboard (nur diese Dateien):
git checkout -- static/js/research/pd1_exact_origin_density_research_v1.js
git checkout -- static/js/research/research_charts.js
rm -f static/js/research/test_mc_pool_p4b_cache_lifecycle_v1.js
```

Oder Checkpoint `stable-mc-baseline-pre-pool-p1` / Bytes gegen Backup `mc-baseline-02` — **kein `git reset` der produktiven CWD ohne User**.

---

## 11. OUT OF SCOPE (dokumentiert, nicht geändert)

- `scheduleViewport` / Viewport-Coalescing (Test D/G)
- `exactOriginSweepCacheKey` viewport-gebunden (Test C/E)
- Historische Tile-Planung / doppelte POSTs (Test G)
- Live-Delta-Architektur (Test H bleibt wie gemessen)

---

**MC_POOL_P4B_FIX_READY_BROWSER_PENDING**