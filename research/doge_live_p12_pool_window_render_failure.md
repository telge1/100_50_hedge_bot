# DOGE-LIVE-P12 — Kerzen OK, PD1-Pool-Bänder fehlen (5m/5m)

**Nutzer-Trace (UTC):** 2026-11-13 **07:34–07:36**  
**Verdikt:** **P12_PARTIAL** — Ursache im Frontend-Lifecycle belegt, Backend liefert Pools; minimaler Fix deployed (Coalesce); **Browser-PASS offen**.

---

## Executive Summary

| Ebene | Ergebnis |
|-------|----------|
| Kerzen / Forming | **PASS** (P11C; Trace: `forming_candle_applied \| painted=true`) |
| Caps + DOGE-Bounds | **PASS** (`capabilities_http=200`, `doge_bounds_http=200`, `scopeCaps path=doge_5m_pd1_patch`) |
| `buildWindows` | **PASS** (wiederholt `result=OK`) |
| Window-API (Backend) | **PASS** — 283–441 Pools/Chunk, ~9–10 s Wall-Zeit |
| `window_http_response` / `refreshPane_render` im Nutzer-Trace | **FEHLEN** |
| Sichtbare Pool-Bänder | **FAIL** (Nutzer) — kein Render-Abschluss in der Trace-Fensterzeit |

**Erster fehlerhafter Schritt:** **Frontend** — `refreshPane` bricht wegen **`loadSerial_mismatch`** (Supersede) ab, **bevor** langsame Window-HTTP-Chunks (~10 s) und der Sweep/Render (`pushCanvasDensity` → `refreshPane_render`) fertig werden.

---

## 1. Call-Chain (Browser-Trace ↔ Code)

Nutzer-Konfiguration: **Chart 5m · Pool 5m · PD1 · min_origins=2 · Exact Origin Canvas-Pfad**.

| Stufe | Trace-Event | Befund 07:34 UTC |
|-------|-------------|------------------|
| Host Boot / Load | `loadPane_*`, P11C `waitPaneSize`, `resize` | Kerzen-Pipeline läuft |
| Caps | `loadCaps`, `capabilities_http=200`, `doge_bounds_http=200` | OK |
| Scope | `scopeCaps \| path=doge_5m_pd1_patch` | OK |
| Bounds | `scope_bounds \| hi=2026-11-13T07:25:00Z` | Publish-Tail **~10 min hinter Live-Kerze** (siehe §3) — **kein** `NO_INTERSECTION` |
| Viewport | `resolveViewSec` + `buildWindows` | Mehrfach **OK** |
| Plan/Fetch | `window_http_request` (mind. Kachel **2026-11-09 … 11-10**) | Request gestartet |
| HTTP-Antwort | `window_http_response` | **Nicht** im Trace |
| Merge/Sweep | Adapter `poolsToExactOriginZones` | **Nicht erreicht** |
| Iframe | `setExactOriginDensity` | **Nicht erreicht** |
| Abschluss | `refreshPane_render` | **Nicht** im Trace |

**Inbound-Reuse:** `fetchWindow` dedupliziert per `inflight` Map (Scope-Key + `fromMs` + `toMs`); **HIT** teilt dieselbe Promise.  
**Abort:** Kein explizites `AbortController` — Stornierung erfolgt logisch über **`exactOriginLoadSerial`**, nicht über HTTP-Abbruch.

---

## 2. HTTP / Cache / Inflight

| Prüfpunkt | Ergebnis |
|-----------|----------|
| HTTP 401/422/500 im Server-Log (07:30–07:40 UTC) | **Keine** Treffer im Dashboard-Journal für dieses Fenster |
| Backend `get_landscape_window_response` 5m PD1 | **200** (simuliert), siehe `doge_live_p12_backend_window_probe_v1.json` |
| Window-Laufzeit | **~9–10 s** pro 24–72 h-Kachel (Batch-Reader aktiv) |
| Chunk-Cache (`exactOriginChunkSatisfiesWindow`) | Würde nach erfolgreichem Response **HIT** liefern — ohne Response kein Eintrag |
| Inflight-Hänger | **Nicht belegt** — Server antwortet zuverlässig; Problem ist **vorzeitiges Supersede**, nicht blockierende Map |
| 09–10.11-Kachel | Erwartbar bei **breitem Load-Span** (`EOD_DEFAULT_PREFETCH_MS` = 36 h + sichtbarer Viewport über mehrere 72-h-Sliding-Chunks); **kein** blockierender „alte Kachel“-Bug im Backend |

**Zusatz im Fix:** `window_http_error` Trace bei Fetch-Fehlern (vorher nur stiller Reject ohne Response-Event).

---

## 3. Daten-Tail vs. Anzeige-Tail (UTC)

| Quelle | Wert (Trace-Zeit ~07:34) | Wert (Audit ~07:41) |
|--------|---------------------------|----------------------|
| Kerzen (forming) | läuft | — |
| Publish SSOT `data_available_to` (Bounds-API) | **`2026-11-13T07:25:00Z`** | **`2026-11-13T07:35:00Z`** |
| Publisher-Journal | `06:20Z` @ 07:25:11, nächster Bucket @ 07:30 | `06:30Z` @ 07:35:20 |

Der Trace-`hi=07:25` ist **konsistent** mit dem **5m-Publish-Rhythmus** (~1 geschlossener 5m-Bucket hinter der letzten veröffentlichten Pool-Bar), **nicht** mit fehlenden Kerzen.

---

## 4. Pool-Anzahl Backend vs. Frontend

| Fenster (UTC) | Backend `pools` (Exact-Origin-Window) | Frontend (Trace) |
|---------------|----------------------------------------|------------------|
| `2026-11-13T06:00` → `07:35` | **283** | **0** (kein Render) |
| `2026-11-13T01:25` → `07:35` | **335** | **0** |
| `2026-11-09T00:00` → `2026-11-10T00:00` | **441** | Request ohne Response im Trace |

**D2 / min_origins=2:** Window liefert Roh-Pools; D2-Sweep + Mindest-Tiefe passieren erst nach erfolgreichem Fetch in `refreshPaneImpl` — ohne Render **0** Segmente.

---

## 5. Nachgewiesene Ursache

1. **P11C** erhöht **Resize-/Visible-Range-Churn** (`ResizeObserver`, `setData` + `resize`, debounced `on_visible_range` → `scheduleViewport` alle **400 ms**).
2. Parallel: **`loadPane` → `refreshPane({ force: true })`**, Pool-Bucket-Ticks, **`scheduleSupersededDensityRefresh` (48 ms)** nach Serial-Mismatch.
3. Jeder neue `refreshPane`-Start erhöht **`exactOriginLoadSerial`**; nach **`fetchPlannedWindows`** bricht bei Mismatch der Pfad ab (**kein** `refreshPane_render`), obwohl HTTP weiterläuft.
4. Window-Chunks brauchen **~10 s** bei nur **2** parallelen Requests über **mehrere** Sliding-Chunks → typisches Erst-Request-Signal ist eine **frühe** Kachel (z. B. 09–10.11) noch **ohne** `window_http_response` im 2-Minuten-Trace.

**Nicht Ursache:** fehlende PD1-Daten, 422 OUT_OF_SCOPE für das November-Fenster, Publisher-Ausfall, P11C-only Candle-Paint ohne Segmente (Segmente werden nie gesetzt).

---

## 6. Minimaler Fix + Tests

**Dateien (Spread-Produktion):**

| Datei | Änderung |
|-------|----------|
| `static/js/research/pd1_exact_origin_density_research_v1.js` | **`refreshPane` Coalescing** (`_eoRefreshGate`): maximal ein inflight Refresh/Pane; weitere Aufrufe → `refreshPane_coalesced` + merged opts nach Abschluss |
| | `window_http_error` Trace |
| `templates/research_charts.html` | Cache-Bust `?v=pd1-p12-refresh-coalesce-v1` |
| Mirror | `pools+ob+delta_bot/backtester/short/.../pd1_exact_origin_density_research_v1.js` |

**Keine Änderung:** PD1/D2-Semantik, Publisher, P11C-Kerzen-Fix, Trading-Boxen, Window-Batch-Backend.

**Tests (durchgeführt / offen):**

| Test | Status |
|------|--------|
| Backend Window 5m PD1 (Nov-13 + Nov-09 Kachel) | **PASS** |
| Publisher-Takt 07:20–07:35 UTC | **PASS** (fortlaufend) |
| Ausgelieferte JS enthält Coalesce-Marker (curl) | **PASS** |
| DOGE 5m/5m, 30m/5m, 30m/30m, 1h, 4h — Regression im Browser | **OFFEN** |
| Kerzen + forming nach Hard-Refresh | **OFFEN** (Nutzer) |

**Browser-Abnahme:** Hard-Refresh mit `?v=pd1-p12-refresh-coalesce-v1`, optional `DOGE_EXACT_ORIGIN_TRACE=1` — erwarten: `window_http_response` → `refreshPane_render` → sichtbare Bänder.

---

## 7. Verdikt

**P12_PARTIAL**

- **Ursache:** Refresh-Race / `loadSerial`-Supersede vor Window+Render-Abschluss (verstärkt durch P11C-Viewport-Churn).  
- **Backend:** liefert qualifizierte Pool-Daten.  
- **Fix:** Coalescing deployt; **kein Commit/Push** in dieser Session.  
- **STOP** gemäß Auftrag.
