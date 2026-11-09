# L1 V3A — Audit: L1-Pooldaten vs. Research-Chart „Exact Unique-Origin D2“

READ-ONLY-Diagnose (2026-11-08). **Keine Codeänderungen** an Strategie, Dashboard, SSOT oder Density.

---

## A. Datenfluss Research-Chart (Depth = mindestens 2 **unabhängige Origins**)

### UI & Begriff

- Checkbox **„B – Exact Origin-Density“** (`researchIndExactOriginDensity`) in  
  [`templates/research_charts.html`](/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard/templates/research_charts.html)
- Slider **„Mindest-Origins Density-Filter“** (`researchExactOriginMinDepth`, 1–10) — **Darstellungsfilter**, kein SSOT-Parameter  
  (Tooltip: *„schwache Überlappungen ausblenden (keine SSOT-Änderung)“*).
- **PD1** = Pool-Datenbasis (`pool_detail=1` über `researchExactOriginPoolDetail` / Viewer-Scope).
- **D2** = im Chart: Preisbänder, in denen die **Anzahl gleichzeitig überlappender unterschiedlicher `origin`-Keys ≥ 2** ist — **nicht** ein separater PD2-Feed.

### Client-Pipeline (Depth-Berechnung)

| Stufe | Modul | Funktion / Verhalten |
|--------|--------|----------------------|
| API-Aufruf | `static/js/research/pd1_exact_origin_density_research_v1.js` | `POST /live-charts/viewer/api/window` |
| Band-Sweep | `static/js/research/pd1_exact_density_trp_adapter.js` | `computeExactBandsForSide()` — Event-Sweep auf ACTIVE-Pool-Preisintervallen; `depth` = `Set(aktive Origins).size` |
| Anzeige D2 | `filterSegmentsByMinOrigins(segments, minDepth)` | blendet Zonen mit `metadata.depth < 2` aus |
| Overlay | `poolsToExactOriginZones()` | 5m-Spalten, `metadata.depth`, `decision_close_ms` |

### Server-Pipeline (kanonische ACTIVE-Menge)

| Stufe | Modul |
|--------|--------|
| HTTP | `pd1_landscape_viewer_v1/api.py` → `window_service_v1.py` |
| Fenster | `pd1_landscape_viewer_v1/historical_window_v1.py` → `build_landscape_window()` |
| Scope | `research_charts/historical_lmv2_pd_contract_v1.py` → `resolve_viewer_historical_scope(timeframe="5m", pool_detail=1)` |
| Snapshot | `research_charts/historical_pool_state_v1.py` → `load_historical_pool_state_at_close()` |
| CH | ClickHouse Katalog + `PublishedActiveReader` (5m PD1-Linie `historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1`) |
| Origin-Ableitung | `historical_window_v1._origin_logical_v2_key(pool_id)` z. B. `lldv2:DOGEUSDT:5m:micro:upper:1780358700` → `lldv2:DOGEUSDT:5m:upper:1780358700` |

**Kern:** Der Chart rechnet D2-Bänder aus **dieselben** publizierten ACTIVE-Members pro `decision_close`, aggregiert sie aber per **Exact-Origin-Overlap-Sweep** — nicht als einzelne Micro-Streifen.

Alternative/analytische Cluster (nicht identisch mit D2-Overlay):  
`dynamic_liquidity_landscape_v1.analyze_side()` in `clusters_by_dc` — im Testfenster oft leer, wenn Import fehlt.

**Historische As-of-Korrektheit (Chart):**

1. **Rekonstruierbar:** ACTIVE-Membership pro veröffentlichtem 5m-`decision_close` (Apr–Sep-2026 Import-Linie).
2. **Regelwerk:** Exact-Origin-Depth ist **frontend-/Adapter-Logik** auf diesen Members (bewiesen in JS).
3. **Kein** Ersatz durch „heutige“ Pools, solange `load_historical_pool_state_at_close` am Close existiert (`MISSING_PUBLICATION` sonst).

---

## B. Datenfluss L1-Backtester / L1 V3

| Stufe | Modul |
|--------|--------|
| Kandidat / Gate | `research/manual_channel_pool_long_v1/run_l1_pilot.py` |
| Snapshot-Laden | `pools_at_confirm_open()` → `load_historical_pool_state_at_close(decision_close, timeframe="5m", pool_detail=1, include_remaining=False)` |
| Qualifikation | `qualify_upper_pools()` — jedes ACTIVE-**Micro**-Band mit `lower_bound > entry` und Abstand ≥ `MIN_POOL_DIST_PCT` (0,50 %) |
| TP-Auswahl | `pick_tp()` — nahe Kanal / Pool-Unterkante |
| V3-Metriken / Filter | `pool_landscape.py` — Zusammenführung **einzelner** qualifizierter Micro-Bänder (`ZONE_MERGE_GAP_PCT`); **`pool_unique_origin_slots`** = Anzahl `pool_id`-Suffixe (**Proxy**, nicht Origin-Overlap-Depth) |
| Simulation | `pilot_engine.py` |

**Nicht verwendet in L1:**

- `computeExactBandsForSide` / `metadata.depth ≥ 2`
- `build_landscape_window().clusters_by_dc` für Entry-Gates

---

## C. Exakter Vergleich — Referenzzeitpunkte

Entscheidungszeitpunkt = **5m-Close der C1-Bestätigungskerze** (identisch L1 & Chart-Snapshot):

| Fall | Entry UTC | Entry | Confirm-Close (SSOT) |
|------|-----------|------|----------------------|
| A Verlust | 2026-08-25 21:15 UTC | 0.085850 | **2026-08-25 21:15:00 UTC** |
| B Gewinn | 2026-08-26 15:10 UTC | 0.084440 | **2026-08-26 15:10:00 UTC** |

Rekonstruktion: dieselbe Python-Logik wie `pd1_exact_density_trp_adapter.js` (`computeExactBandsForSide`, dann `depth ≥ 2`) auf `load_historical_pool_state_at_close`-Members.

### C.1 Aggregat

| Kennzahl | 25.08. 21:15 (A) | 26.08. 15:10 (B) |
|----------|------------------:|-----------------:|
| Snapshot-ID (Prefix) | `53e61a21b198ca8d` | `554abb919bb95871` |
| ACTIVE upper **micro**-Members (SSOT) | 62 | 75 |
| L1 **Gate-qualifiziert** (≥0,5 % über Entry) | **62** | **74** |
| Exact-Origin-Bänder (alle Depths) | 82 | 103 |
| **D2-Bänder** (unique origins ≥ 2 im Sweep) | **55** | **69** |
| Erste D2-Band Unterkante / Abstand Entry | 0.08895 / **3,611 %** | 0.08604 / **1,895 %** |
| Micro-`lower_bound`, die **keine** D2-Band-Unterkante sind | **23** von 62 | **26** von 74 |

### C.2 Interpretation des Bandvergleichs

| Aspekt | Status |
|--------|--------|
| Gleicher historischer **ACTIVE**-Snapshot zum Close | **VERIFIZIERT** (identische Quelle `load_historical_pool_state_at_close`, 5m PD1) |
| Einzelne Micro-Band-Grenzen (`lower_bound`/`upper_bound`) L1 vs. Katalogzeile | **VERIFIZIERT** (1:1 dieselben Rows) |
| `pool_id` = kanonische Origin-ID | **NICHT VERIFIZIERT** — Chart nutzt `_origin_logical_v2_key(pool_id)`; bei DOGE 5m-August oft 1:1 mit Suffix, semantisch aber **logical origin**, nicht Rohtext-ID |
| Anzahl „62 / 74“ = sichtbare **D2**-Zonen | **FALSCH** — das sind **Micro-Member-Counts**, keine D2-Bänder |
| `strength` = D2-Dichte | **NICHT VERIFIZIERT** — `strength` ist LLD-Micro-Stärke, nicht Overlap-Origin-Depth |
| D2-Chart-Zonen = L1-`qualify_upper_pools` | **NEIN** — 23–26 qualifizierte Micro-Ziele haben **keine** entsprechende D2-Band-Unterkante (isolierter/dünner Streifen) |

### C.3 L1 V3 Filterwerte (25.08. / 26.08.)

| Merkmal (V3-Bericht) | Basis | Bewertung |
|----------------------|--------|-----------|
| Gate-Bänder 62 / 74 | `qualify_upper_pools` auf Micro-Members | **PROXY** (Micro-Count, nicht D2) |
| Substantielle Zonen / nächste Zone / Band-Dichte | `pool_landscape.py` Merge auf Micro | **PROXY**, nicht Chart-D2 |
| REJECT / PASS | Kombination aus Micro-Distanz & Zone-Merge | **Nicht auf kanonischer D2-Density validiert** |

Die **Filterentscheidung V3 ist diagnostisch auf Micro-SSOT**, nicht auf dem Research-Chart-D2-Overlay bewiesen.

---

## D. Urteil

### `DIFFERENT_DENSITY_SOURCE`

**Begründung:**

1. **Gemeinsame Basis:** Beide Pfade lesen dieselben historischen **5m PD1 ACTIVE**-Members zum gleichen `decision_close` (as-of korrekt im August-Fenster).
2. **Abweichende Density:** Der Research-Chart zeigt **Exact Unique-Origin Depth** (Preis-Sweep mit `depth = distinct origins`); L1 und L1 V3 nutzen **einzelne Micro-Bänder** plus 0,5%-Gate und V3-Heuristiken darauf — **ohne** D2-Overlap-Filter.
3. Die im V3-Bericht genannten **62 / 74 „Gate-Bänder“ sind nicht** die Anzahl kanonischer D2-Bänder (tatsächlich **55 / 69** D2-Bänder am gleichen Close).

`EXACT_MATCH_VERIFIED` ist für die **sichtbare D2-Landschaft vs. L1-Entscheidung** nicht erfüllbar.

---

## E. Mögliche Korrektur (Vorschlag nur, **nicht implementiert**)

| Element | Empfehlung |
|---------|------------|
| Wiederverwendbare SSOT-Funktion | Weiter `load_historical_pool_state_at_close(..., timeframe="5m", pool_detail=1)` |
| Kanonische D2-Bänder | Port von `computeExactBandsForSide` (aus `pd1_exact_density_trp_adapter.js`) **oder** serverseitig aus `build_landscape_window(...).pools` + gleicher Sweep; Filter `depth >= 2` |
| Origin-IDs | `_origin_logical_v2_key()` aus `pd1_landscape_viewer_v1/historical_window_v1.py` — nicht `pool_id`-Suffix allein |
| Minimaler L1-Adapter | Neues **read-only** Hilfsmodul, das pro Entry nur: Members laden → D2-Upper-Bänder berechnen → Entry-Gate auf **D2-Band-Unterkanten** (statt 62 Micro-Counts) |
| Fehlende Daten | Keine für diese Referenz-Closes; außerhalb Publish-Fenster: `MISSING_PUBLICATION` |
| Auswirkung auf V3-Filter | Zählwerte „substantiell / nearest dist“ würden **sinken** (weniger, breitere Ziele); Schwellen 2,20 % / 3 Bänder sind auf Micro-Merge kalibriert → **Neubewertung nötig** |

---

## F. Zeichnungs-/Repro-Kontext (L1-Läufe)

- `drawings.json` SHA256-Prefix: `3e7041de2814607b`, 19 Zeichnungen
- `d-1ff495203465` **fehlt** (4-Trade-Variante-B von Nov. nicht reproduzierbar)

---

## G. Abschluss — fünf Pflichtfragen

### 1. Sieht L1 V3 zum historischen Entry dieselben ACTIVE Upper-D2-Bänder wie Research-Charts bei Depth 2?

**Nein.** L1 nutzt **dieselben ACTIVE Micro-Members** aus derselben SSOT, aber **nicht** die vom Chart berechneten **D2-Overlap-Bänder** (`depth ≥ 2`).

### 2. Falls nein: Was unterscheidet sich?

- **Aggregation:** Chart = Preis-Sweep / unique-origin depth; L1 = einzelne Micro-Pool-Unterkanten ≥ 0,5 %.
- **Anzahl:** 62/74 (Micro qualifiziert) vs. 55/69 (D2-Bänder) an denselben Closes.
- **V3-Metriken:** Merge/„substanzielle Zonen“ auf Micro — **kein** D2-Depth.
- **`strength` ≠ origin depth.**

### 3. Können wir die exakte Research-Chart-D2-Berechnung direkt für L1 wiederverwenden?

**Ja, prinzipiell** — gleiche Members + `computeExactBandsForSide` + `min_depth=2` (JS-parität). Server-Hilfe: `build_landscape_window` liefert bereits `pools` mit `origin` und Segmenten; Sweep-Logik ist im Adapter dokumentiert.

### 4. Sind historische ACTIVE-Origin-Daten vollständig verfügbar?

**Für die Referenz-Closes im importierten Apr–Sep-2026-5m-PD1-Fenster: ja** (publizierte Snapshots). Exakte Origins werden aus `pool_id` via `_origin_logical_v2_key` abgeleitet — **kein** separates Origin-Feld in CH-Zeilen. Außerhalb Publish-Raster: **blockiert** (`MISSING_PUBLICATION`).

### 5. Kleinste notwendige technische Anpassung?

Ein **dünner read-only Adapter** im L1-Forschungsmodul: am Confirm-Close SSOT laden, D2-Upper-Bänder wie Chart berechnen, L1-Gate/V3-Metriken auf **D2-Bänder** statt roher Micro-Liste umstellen — **ohne** neue Density-Engine und **ohne** SSOT-Änderung. V3-Schwellen danach **neu validieren**, nicht übernehmen.

---

**STOP** — nur dieser Audit-Bericht; keine Implementierung.
