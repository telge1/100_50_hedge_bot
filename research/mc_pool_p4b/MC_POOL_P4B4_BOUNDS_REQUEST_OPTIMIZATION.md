# MC-POOL-P4B4 — Duplicate Bounds Request Optimization

**Status:** `MC_POOL_P4B4_FIX_READY_BROWSER_PENDING`  
**Datum:** 2026-11-26  
**SSOT (Code):** `spread_recovery_hedge_short_dev/dashboard/static/js/research/pd1_exact_origin_density_research_v1.js`  
**Kein Service-Restart · kein Commit · kein Push**

**Warnung Live-Static:** Geänderte `pd1_exact_origin_density_research_v1.js` wird vom laufenden Dashboard direkt ausgeliefert. Template-`?v=` unverändert (`pd1-mc-pool-p4b-v1`); Hard-Reload oder Cache-Bust nötig, sonst alter JS-Cache im Browser.

**Post-Fix SHA-256 (pd1):** `2974910be900e1ea2f7fb1e6542bba16727ea7d38ac0f1dd2bea5078fbbf0466`

---

## 0. Kontext P4B3 (verifiziert, Browser)

| Metrik | Wert |
|--------|------|
| Window-Chunks vor OFF / während OFF / nach ON | 2 / 2 / 2 |
| Pool-IDs | 1109 / 1109 / 1109 |
| Pool-Inhalte (Chunks) | Equality **TRUE** |
| Window-POSTs | **0** |
| Window-Bytes | **0** |
| Adapter nach Re-ON | **111 ms** |
| Canvas Draw | **1 ms** |
| Checkbox→sichtbare Canvas | **1148 ms** (dominiert von Bounds) |

Segment-/renderKey-Equality: **FALSE** (siehe §5).

---

## 1. Root Cause (Codebeleg)

### 1.1 Gemessenes Muster

Zwei aufeinanderfolgende GETs auf  
`/api/research/multicoin-5m-pd1-viewer-bounds?symbol=SOLUSDT`  
~5 ms Abstand, je ~512 ms — Summe **~1026 ms**.

Single-Chart ON-Pfad:

```
#researchIndExactOriginDensity change (checked)
  → refreshAllAfterReEnable()
       → refreshPaneAfterReEnable(pane)   // eine Pane
```

### 1.2 Doppelte Caller in `refreshPaneAfterReEnable` (vor Fix)

Reihenfolge **ohne** `refreshPane` im Cache-Hit-Pfad:

| Schritt | Funktion | Netzwerk |
|---------|----------|----------|
| 1 | `ensurePaneViewerBounds(pane)` | **Immer** `fetch(boundsUrl)` — kein TTL, keine Single-Flight |
| 2 | `loadCaps()` (Caps-Cache HIT) | `refreshDoge5mViewerBoundsIfStale(caps)` — zweiter `fetch(boundsUrl)` wenn `_doge5mBoundsFetchedAt` älter als 30 s **oder** Schritt 1 `applyDoge5mViewerBoundsToCaps` nicht lief (Body-Parsing strenger als Refresh-Pfad) |

**Gleicher Scope / gleiche Parameter:** Beide nutzen `viewerBoundsUrl(sym)` mit `sym = paneSymbolUpper(pane)` bzw. `capRoot.symbol || currentSymbolUpper()` — für SOLUSDT Single-Chart identische URL.

**Nicht** die Ursache im Cache-Repaint-Pfad: Bei erfolgreichem `repaintPaneFromCache` wird `refreshPane` **nicht** aufgerufen (`refreshPaneImpl` enthält erneut `ensurePaneViewerBounds` + `loadCaps` — wäre sonst Request 3+4).

### 1.3 Zusätzliche Bounds-Quellen (Referenz)

| Funktion | Wann |
|----------|------|
| `loadCapsOnce` | Erst-Load: Capabilities + Bounds (einmalig) |
| `refreshPaneImpl` | Jeder volle Refresh: `ensurePaneViewerBounds` + `loadCaps` |
| `refreshDoge5mViewerBoundsIfStale` | TTL 30 s (`DOGE_5M_BOUNDS_REFRESH_MS`) |

Re-Enable-Optimierung (P4B) hat **neben** bestehender `loadCaps`-Stale-Refresh-Logik einen **zweiten** unconditional Pfad in `ensurePaneViewerBounds` eingeführt.

---

## 2. Fix (minimal)

### 2.1 `fetchViewerBoundsAndApply(sym, traceId)`

- Ein **GET pro `boundsUrl`** via `viewerBoundsInflight` (Single-Flight).
- Fail-closed: `!bRes.ok` oder kein `data_available_to` → **kein** Caps-Patch, **kein** Freshness-Advance für `hiAdvanced`.
- Gemeinsames Body-Parsing `normalizeViewerBoundsBody` (wie früher `refreshDoge5mViewerBoundsIfStale`).
- `applyDoge5mViewerBoundsToCaps` setzt `_doge5mBoundsFetchedAt` nur bei erfolgreichem Apply (unverändert).

### 2.2 `ensurePaneViewerBounds`

- Caps nicht ready → `loadCaps()` (Bounds in `loadCapsOnce` über `fetchViewerBoundsAndApply`).
- Caps ready → ein Aufruf `fetchViewerBoundsAndApply`.

### 2.3 `refreshPaneAfterReEnable`

```text
await ensurePaneViewerBounds(pane);
await loadCaps({ skipStaleBoundsRefresh: true });
```

→ **max. ein** Bounds-GET pro Re-Enable, wenn Caps bereits warm.

### 2.4 `loadCaps(opts)`

- `skipStaleBoundsRefresh: true` unterdrückt den zweiten Pfad `refreshDoge5mViewerBoundsIfStale` in derselben Operation.

**Nicht geändert:** 30 s TTL global, Pane/Symbol-Isolation, P4B1 Freshness-Guards, Window-Cache, Pool-Mathematik, MC-LOAD-P4J, `research_charts.js`.

### 2.5 Erwartete Performance (UNGEMESSEN)

Removal ~512 ms Bounds-Duplikat → Re-ON-Walltime näher an Adapter+Canvas (~112 ms), sofern kein anderer Blocker (UI/Layout). **Erst nach Browser-Messung bewerten.**

---

## 3. Freshness-Vertrag (unverändert + Fail-closed)

| Regel | Verhalten |
|-------|-----------|
| Bounds-GET fehlgeschlagen | Kein `data_available_to` in Caps → `apply` skip; `hiAdvanced` fail-closed; **kein** blindes `repaintPaneFromCache` wenn Freshness unbekannt |
| `exactOriginReenableFreshnessBaselineKnown` | Erfordert `exactOriginStripCapsHiMs` von OFF (P4B1) |
| `hiAdvanced` | Nur wenn `bounds.hiMs > stripCapsHiMs` nach **erfolgreichem** Bounds-Apply |
| Publication während OFF | Ein Bounds-GET bei Re-Enable reicht für Hi-Check; kein globaler TTL-Bypass |

**Nicht implementiert:** Überspringen des Bounds-GETs nur weil Caps bereits einen `data_available_to` tragen — ohne verifizierten Gleichstand zu Live-Publication wäre das unsicher.

---

## 4. Tests

| Test | Erwartung |
|------|-----------|
| Node `test_mc_pool_p4b_cache_lifecycle_v1.js` | 13 PASS (nach Fix) |
| Re-ON, voller Window-Cache | 0 Window-POSTs, **1** Bounds-GET in P4A-HTTP-Log |
| Publication während OFF | 1 Bounds-GET; ggf. selektiver Fetch, kein falscher Cache-Repaint |
| Bounds-GET 4xx/5xx | Kein Freshness-PASS; kein erzwungener Repaint |
| Symbol / TF / Multi-Pane | Sym-spezifische `boundsUrl`; Pane-Isolation wie zuvor |
| Verspätete Response | Single-Flight: zweiter Caller teilt dasselbe Promise |
| MC-LOAD pytest (lokal) | Regression unverändert ausführen vor Freigabe |

---

## 5. READ-ONLY — B1 Viewport / Segment-Equality

### 5.1 Warum API-Zeitfenster unterschiedlich

`resolveViewSec` (5m Chart = Pool 5m, nicht cross-TF) nutzt primär  
`resolveHistoricalLmv2ViewportCloseWindow` → `hist_viewport_resolver`  
(`pd1_exact_origin_density_research_v1.js`).

Viewport hängt an **aktuellen Close-Fenstern** des Charts (Live-Updates, neue 5m-Closes, Resolver). Zwischen:

1. `refreshPane({ force: true })` (Referenz) und  
2. Re-ON (Candidate)

können **neue Kerzen/Closes** das Fenster verschieben (Beobachtung: 06:30–13:00 UTC vs. 07:50–13:00 UTC) → andere `renderKey`, andere Segmentanzahl (734 vs. 678), obwohl **Pool-IDs in Chunks identisch**.

**Kein MC-LOAD-P4J-Code geändert** — nur Erklärung.

### 5.2 Reproduzierbarer Equality-Test (historisch, identischer Viewport)

**Ziel:** `renderKey`, `segment_sig`, `pool_ids` vergleichbar.

1. Harness + `__MC_POOL_P4B2_EQ__` laden.
2. **Replay / GO TO:** festes historisches Datum (kein Live-Rand) oder Replay-Modus mit fixiertem `replayTargetTs`.
3. Exact Origin laden, warten bis stabil.
4. Optional: **kein** `force: true` — Referenz-Fingerprint **direkt vor OFF**:  
   `var ref = __MC_POOL_P4B2_EQ__.fingerprint('ref_pre_off');`  
   sowie  
   `var vr = chart.getVisibleTimeRange(); console.log(vr, PD1ExactOriginResearch.getLastMetrics().apiFromIso, apiToIso);`
5. OFF → kurz warten (**kein Pan/Zoom**, Live abwarten minimieren) → ON.
6. `cand = __MC_POOL_P4B2_EQ__.fingerprint('cand_on');`  
   `__MC_POOL_P4B2_EQ__.compare(ref, cand)`  
   Zusätzlich: dieselben `apiFromIso` / `apiToIso` in Metrics.

**Strengerer Check:**

```javascript
var p = __researchDebug.panes["pane-0"];
var before = p.exactOriginSegmentCache && p.exactOriginSegmentCache.key;
// OFF → ON
var after = p.exactOriginSegmentCache && p.exactOriginSegmentCache.key;
console.log({ before, after, match: before === after });
```

---

## 6. Rollback P4B4 (nur pd1)

1. Datei aus P4B2A-Backup **nur wenn bewusst P4B+P4B4 verwerfen:**  
   `.rollback_mc_pool_p4b2a_20261126T123710Z/pd1_exact_origin_density_research_v1.js`  
   (SHA `e5073ab8…` — **ohne** P4B4)
2. Oder Git/_diff nur die P4B4-Hunks rückgängig.

---

## 7. Abschluss

| Item | Wert |
|------|------|
| Root Cause | `refreshPaneAfterReEnable`: `ensurePaneViewerBounds` + `loadCaps`/`refreshDoge5mViewerBoundsIfStale` → 2× gleicher Bounds-GET |
| Fix | Single-Flight + `skipStaleBoundsRefresh` auf Re-Enable |
| Window-Cache P4B | Unberührt |
| Browser-Freigabe | Ausstehend |

**Token:** `MC_POOL_P4B4_FIX_READY_BROWSER_PENDING`