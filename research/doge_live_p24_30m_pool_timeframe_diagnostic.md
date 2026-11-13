# DOGE-LIVE-P24 — 30m-Pools fehlen, 5m funktioniert (STRICT READ-ONLY)

**Erstellt:** 2026-11-13  
**Modus:** Kein Fix, kein Restart, kein Commit, kein Push  
**Dashboard-Prozess:** PID **2379483** (unverändert)  
**Backend-Baseline:** HEAD `5cb52d87` (Landscape); MC4-JS lokal modifiziert (uncommitted)

**Ergebnis-Kürzel:** `P24_BACKEND_30M_OK_CLIENT_BOUNDS_VIEWPORT_ASYMMETRY`  
(Browser-Konsole des Nutzers: **UNVERIFIED** — Log-/Code-Nachweis ohne Live-UI)

---

## Kurzfassung

| Frage | Befund |
|--------|--------|
| Unterstützt der Window-Service DOGE **30m**? | **Ja** — Pfad `on_demand_ch1m_30mpd1_v1` (CH-1m → 30m-Engine), getrennt von 5m-Publish. |
| Liefern 30m-Window-POSTs Pools? | **Ja** (Journal heute: **HTTP 200**, typ. **149–217 pools**, kein `pools=0` für `tf=30m`). |
| Ist 5m vs. 30m primär ein Backend-Ausfall? | **Nein** — nachgewiesen funktionsfähige 30m-Compute-Pipeline. |
| Warum kann die UI trotzdem leer sein? | **Erste clientseitige Gabelung:** 30m braucht On-Demand-Bounds-Merge + anderer Viewport-Coverage-Pfad; ohne Merge oder bei Refresh-Coalesce-Sturm keine stabilen Bänder trotz 200er-API möglich. |

**Nicht** als alleinige Ursache belegt: Auth-401 (separate P23-Spur), leerer Server-Body bei 200, fehlende 30m-Engine.

---

## Phase 1 — Requests & Einstellungen (Code + Journal)

### Gemeinsame API-Verträge (alle Fälle)

| Feld | Wert (aus `fetchWindow` in `pd1_exact_origin_density_research_v1.js`) |
|------|------------------------------------------------------------------------|
| **URL** | `POST /live-charts/viewer/api/window` |
| **Credentials** | `same-origin` |
| **Body** | `from_utc`, `to_utc`, `timeframe` (= **Pool-TF**), `pool_detail`, `symbol` (z. B. `DOGEUSDT`), `response_mode: "exact_origin_v1"`, `trace_id` |

Zusätzlich beim Caps-Laden:

| Endpoint | Zweck |
|----------|--------|
| `GET /live-charts/viewer/api/capabilities` | Root-Caps + `supported_viewer_scopes` |
| `GET /api/research/doge-5m-pd1-viewer-bounds` | 5m-Publish-SSOT-Bounds (Patch in Caps) |
| `GET /api/research/doge-pd1-on-demand-viewer-bounds?timeframe=30m&pool_detail=1` | **nur für 30m/1h On-Demand** (Patch in Caps) |

Alle genannten GETs: `require_auth` → ohne Session **401** (read-only curl bestätigt).

### A — Chart **5m**, Pool **5m** (funktionierend, Referenz)

**Client-Scope:** `{ timeframe: "5m", pool_detail: 1 }` (wenn PD1 angehakt).

**Bounds / Clip:**

- `scopeCaps` → `doge5mPd1BoundsPatch` (Publish-SSOT, live High-Water).
- `dataBoundsMsForScopes` → typ. bis **live** `data_available_to` (nicht nur Apr–Sep).
- `resolveViewSec` bei gleichem Chart-/Pool-TF: `resolveHistoricalLmv2ViewportCloseWindow` mit `membershipTfSec = 300`; `historicalLmv2CoverageBoundsUnix(300)` nutzt **DOGE-5m-Viewer-SSOT** (`research_scanner_common_helpers.js`).

**Window-Backend (`window_service_v1.py`):**

- Kein On-Demand-Scope → `build_landscape_window` + **`resolve_viewer_historical_scope`**.
- Pipeline (Contract): `historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1`.

**Journal-Beispiel (Switch zurück 5m, 15:32:06):**

```text
POST …/api/window 200  tf=5m pd=1  from=2026-11-11… to=2026-11-12…  pools=699 (cache_hit)
POST …/api/window 200  tf=5m pd=1  from=2026-11-09… to=2026-11-10…  pools=652
POST …/api/window 200  tf=5m pd=1  from=2026-11-13… to=2026-11-13…  pools=415
```

**Window-POST-Rate nach TF-Wechsel:** ~**5** POSTs in 10 s (15:32).

---

### B — Chart **30m**, Pool **30m** (Nutzer-Fehlerfall)

**Client-Scope:** `{ timeframe: "30m", pool_detail: 1 }`.

**Planner:** `exactOriginWindowPlannerForScope` → `fixed48h` / **72h-Tiles** (nicht 5m-Sliding).

**Bounds / Clip (erste Abweichung zu 5m):**

- `scopeCaps` für 30m/PD1 → `onDemandPd1BoundsPatch` aus `_onDemand30mPd1ViewerBounds` (nur wenn On-Demand-GET erfolgreich in `loadCapsOnce`).
- Ohne Patch: Fallback auf Root-`/capabilities`-Scope **Apr–Sep 2026** → `buildWindows` → **`NO_INTERSECTION`** bei Live-Chart (Nov 2026) → **kein sinnvoller Window-Schnitt**, Canvas leer (Hinweise im JS: „veraltete Caps … Hard-Refresh“, „30m On-Demand Bounds nicht verfügbar“).

**Viewport bei Chart=Pool=30m:**

- `chartTfDiffersFromPool` = **false** → Versuch `resolveHistoricalLmv2ViewportCloseWindow(..., tfSec=1800)`.
- `historicalLmv2CoverageBoundsUnix(1800)` = **Pilot Apr–Sep** (nicht On-Demand-CH).
- Live-November: oft `viewport_after_replay_cutoff: true` → Hist-Pfad wird **übersprungen**; Fallback `chart_visible_range` / Candles (Trace: `resolveViewSec`).

**Window-Backend:**

- `_on_demand_ch1m_scope("30m")` → `SCOPE_30M_PD1`
- **Pipeline:** `on_demand_ch1m_30mpd1_v1`
- Builder: `build_landscape_window_on_demand_ch1m_pd1_v1` (CH 1m, **kein** 5m-Publish-Mix).

**Journal beim TF-Wechsel (15:30:24–15:30:49, authentifizierte Browser-Session):**

```text
GET /live-charts/viewer/api/capabilities                         200
GET /api/research/doge-5m-pd1-viewer-bounds                      200
GET /api/research/doge-pd1-on-demand-viewer-bounds?timeframe=30m  200

POST …/api/window 200  tf=30m pd=1  from=2026-11-12… to=2026-11-13T14:25…  pools=149  cache_miss  [on_demand_30mpd1] engine_pools=2569
POST …/api/window 200  tf=30m pd=1  from=2026-11-09… to=2026-11-11…        pools=193  cache_hit
… (viele identische cache_hit-Wiederholungen)
```

**Auffällig:** **42** Window-POSTs in **13 s** während 30m-Session vs. **5** POSTs beim 5m-Wechsel — Hinweis auf **Frontend-Refresh-/Coalesce-Sturm** (`_eoRefreshGate`, `loadSerial`, `scheduleSupersededDensityRefresh`), nicht auf fehlende Serverdaten.

**Browser-Konsole / DogeExactOriginTrace:** in dieser Diagnose **nicht** ausgewertet (Nutzer nicht anwesend).

---

### C — Chart **30m**, Pool **5m** (optional)

- `chartTfDiffersFromPool` = **true**.
- `resolveViewSec` → `source: "chart_visible_cross_pool_tf"` (sichtbarer Chart-Bereich).
- Window-`timeframe` in Body bleibt **`5m`** (Pool-TF), nicht 30m.
- Bounds: 5m-Publish-Patch wie Fall A.

**Journal:** nach 30m→5m-Wechsel (15:32) dominieren wieder `tf=5m`-POSTs (s. Fall A).

---

## Phase 2 — Backend-Vertrag (Checkliste)

| # | Prüffrage | Befund |
|---|-----------|--------|
| 1 | 30m im Service unterstützt? | **Ja** — `ON_DEMAND_CH1M_SCOPES["30m"]`, `api.py` leitet `timeframe` durch. |
| 2 | Pipeline/Config 30m | **`on_demand_ch1m_30mpd1_v1`** (+ eigener `config_hash` / LLDv2 PD1). **Nicht** `historical_mixed_frozen_*_5mpd*`. |
| 3 | Kanonischer 30m-PD1-Datenbestand | **On-Demand aus CH-1m**, kein frozen Publish-SSOT wie 5m; Bounds via `on_demand_30mpd1_capabilities_scope()` → CH-Symbol-Meta. |
| 4 | Veröffentlichung im angefragten Fenster | Für Nov-2026-Fenster: Journal **149–193 exportierte Pools** + Engine **2569** — **daten vorhanden**. |
| 5 | Falsche Bounds (5m statt 30m)? | Server-Window nutzt korrekten Scope pro `timeframe`. **Client** kann ohne On-Demand-Merge fälschlich Apr–Sep-Caps für Clip nutzen. |
| 6 | Server ok, Browser filtert weg? | Möglich über `filterForDisplay` / Min-Origins; bei identischem Slider unwahrscheinlich. **`loadSerial`-Supersede vor `pushCanvasDensity`** plausibler bei POST-Sturm. |
| 7 | Cache-/Scope-Keys TF-Wechsel | `exactOriginScopeKey` enthält Pool-TF; Wechsel → `invalidateScopeCaches`, `stripAllPanes`, separates `exactOriginWindowStores[…]`. |
| 8 | Leeres 200 vs. echter Fehler | Heute **kein** `tf=30m` + `pools=0` im Journal; **keine** 30m-Window-4xx in der 15:30-Session. Leer = eher **kein Render** oder **kein Schnitt**, nicht HTTP-Fehler. |

**Codepfade (kurz):**

- JS: `pd1_exact_origin_density_research_v1.js` — `loadCapsOnce`, `scopeCaps`, `fetchWindow`, `buildWindows`, `refreshPaneImpl`.
- API: `pd1_landscape_viewer_v1/api.py` → `get_landscape_window_response`.
- Service: `window_service_v1.py` — `_on_demand_ch1m_scope` vs. `resolve_viewer_historical_scope`.
- On-Demand: `on_demand_ch1m_pd1_v1.py` / `on_demand_30mpd1_v1.py`.
- 5m Publish: `historical_pool_state_v1.py` — `doge_5m_pd1_viewer_bounds`.

**Hinweis:** Offline-Python-Probe scheiterte lokal an `ModuleNotFoundError: data.models` (PYTHONPATH ≠ systemd); **Laufzeit-Nachweis = Journal + laufender PID**, nicht Shell-Import.

---

## Phase 3 — Erste nachgewiesene Differenz 5m ↔ 30m

### 1) Daten- / Pipeline-Ebene (Server, bewiesen)

| | **5m** | **30m** |
|---|--------|---------|
| Pfad | Historical publish / frozen mixed | **On-demand CH1m** |
| `pipeline_line_id` | `historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1` | `on_demand_ch1m_30mpd1_v1` |
| Compute | `build_landscape_window` | `build_landscape_window_on_demand_ch1m_pd1_v1` |

→ **Hier trennen sich die Systeme bewusst** (kein 5m/30m-ID-Mix).

### 2) Client-Ebene (relevant für „keine Bänder“)

**Erste Stelle, an der 5m „einfacher“ ist und 30m scheitern kann, ohne Backend:**

1. **`loadCapsOnce`:** Zusatz-GET On-Demand-Bounds (auth-pflichtig). 5m live zusätzlich über eingebettete `SC_HELPERS` / `doge-5m-pd1-viewer-bounds`.
2. **`scopeCaps` / `dataBoundsMsForScopes`:** Ohne `_onDemand30mPd1ViewerBounds` → Clip gegen **Apr–Sep** statt CH-Live → **`buildWindows` `NO_INTERSECTION`** bei Nov-Chart.
3. **`historicalLmv2CoverageBoundsUnix`:** Nur **300 s** → Live-SSOT; **1800 s** → statischer Pilot — Asymmetrie bei Chart=Pool=30m (Cutoff, dann Fallback).

**Wenn On-Demand-Bounds 200 (Journal 15:30:25, 15:30:38):** Window-Daten kommen an → **nächste plausible Bruchstelle vor Rendering:** Refresh-Gate / wiederholte identische POSTs (**42×**) → instabiler oder abgebrochener `pushCanvasDensity`-Abschluss.

**Schluss:** Nicht aus „fehlenden Bändern“ allein auf Backend-Fehler schließen — **Server liefert 30m-Pools**; Symptom passt zu **Client-Bounds-Merge** und/oder **Render-Coalescing**.

---

## HTTP-Ergebnisse (aggregiert)

| Szenario | Window-POST | Bounds | Pools (Journal) |
|----------|-------------|--------|-----------------|
| 5m / authentifiziert | **200** | 5m-GET **200** | 415–699 |
| 30m / authentifiziert | **200** | 30m On-Demand **200** | 149–193 (kein 0) |
| Unauthentifiziert (curl) | **401** | **401** | — |

---

## Kleinstster sicherer nächster Schritt (nur Diagnose, kein Produktiv-Fix)

1. **Browser mit `DogeExactOriginTrace` aktiv:** Pool-TF 30m wählen; prüfen ob `buildWindows.result` = `OK` oder `NO_INTERSECTION`; `lastMetrics.mergedSegments` / `segmentsDisplayed` nach `setExactOriginDensity`.
2. **Netzwerk-Tab:** Ob `doge-pd1-on-demand-viewer-bounds?timeframe=30m` **200** mit `data_available_to` nahe CH-Now; ob danach 30m-Window-POSTs **non-zero pools** (Response-`pools.length`).
3. **Bei `NO_INTERSECTION`:** Hard-Refresh (`research_charts.html` Cache-Bust) → On-Demand-Merge erzwingen.
4. **Bei 200 + pools>0 aber `mergedSegments=0`:** Refresh-Sturm / `async_superseded` in Trace — Coalesce-Gate dokumentieren (Fix wäre separater Auftrag).

---

## Grenzen eingehalten

Keine Änderungen an Code, Engine, Publisher, Trading, Dashboard-Restart.

---

## STOP

P24 READ-ONLY abgeschlossen.
