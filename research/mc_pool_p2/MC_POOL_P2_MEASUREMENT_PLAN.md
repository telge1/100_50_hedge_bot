# MC-POOL-P2 — Live Pool Performance Measurement Plan

**Endstatus:** `MC_POOL_P2_BROWSER_MEASUREMENT_READY`  
**Datum:** 2026-11-26  
**SSOT:** Spread `dashboard/` (Live-Tree, Checkpoint `5d15cfaf` Referenz)  
**Scope:** SOLUSDT, Single-Chart, **5m**, **Exact Origin** (kanonische Pool-Landschaft D2)

**Keine** Produktions-Codeänderungen, **kein** Commit/Push in dieser Phase.

---

## 0. Vorhandene Diagnose-APIs (wiederverwendet)

| API | Aktivierung | Liefert |
|-----|-------------|---------|
| `window.DogeExactOriginTrace` | `localStorage DOGE_EXACT_ORIGIN_TRACE=1` oder `window.DOGE_EXACT_ORIGIN_TRACE=1` | Console `TRACE \|` — `window_http_request/response`, Viewport, P5K-Case |
| `window.PD1ExactOriginResearch` | immer auf Research-Seite | `getLastMetrics()`, `refreshPane`, `VERSION` — Adapter/Canvas-Metriken |
| `chartApi.getExactOriginDensityDrawStats()` | im TRP-**iframe** | `ms`, `segmentsDrawn`, `segmentsTotal`, Cull-Skip-Zähler |
| `window.__MC_LOAD_DIAG__` | `start()` / `mc.load.diag=1` | Candle-HTTP, **nicht** Window-POST (separat nutzbar) |
| Server JSON (Window-Response) | jeder POST | `timing_ms`, `response_cache`, `provenance.phase5s_batch_*` |

**CONFIRMED:** `fetchWindow` loggt bei aktivem Trace bereits RTT und `pools_n` (`pd1_exact_origin_density_research_v1.js`).  
**CONFIRMED:** `fullyCached` Short-Circuit setzt UI-Hint „cache“ und ruft `pushCanvasDensity` ohne HTTP (`~1578–1590`).  
**NOT FOUND:** Öffentliche globale API nur für `fullyCached` boolean — P2 leitet aus **HTTP-Zähler pro Sektion** + Hint-Text ab.

Konsolen-Harness (lokal, nicht deployed):  
[`MC_POOL_P2_BROWSER_MEASUREMENT_CONSOLE.js`](MC_POOL_P2_BROWSER_MEASUREMENT_CONSOLE.js)

---

## 1. Batch-Reader-Verifikation (READ-ONLY, Live-Prozess)

**CONFIRMED** — Umgebung des laufenden `dashboard.service` (PID bezogen, ohne Restart):

| Variable | Wert (Live) |
|----------|-------------|
| `HISTORICAL_LMV2_BATCH_READER` | **1** |
| `PHASE5S_COMPACT_BATCH_LOADER` | **1** |
| `PHASE5S_MEMBER_PREFILL_REUSE_ENABLED` | **1** |
| `PHASE5S_MEMBER_PREFILL_LOW_COPY_ENABLED` | **1** |
| `PHASE5S_BATCH_PREFILL_DIRECT_LOOKUP` | **1** |

**CONFIRMED:** Exact-Origin SOLUSDT **5m PD1** nutzt `build_landscape_window` → bei Env=1 `prefill_member_cache_phase5s` vor `_build_lifetimes` (`historical_window_v1.py`).

**Fallback-Bedingungen** (einzelne Close-Loads):

| Bedingung | Ergebnis | Klassifikation |
|-----------|----------|----------------|
| `HISTORICAL_LMV2_BATCH_READER=0` | `_load_members` → `load_historical_pool_state_at_close` pro Cache-Miss | **CONFIRMED** |
| Batch-Prefill liefert `None` (Fehler/Scope) | `membership_loader` = `load_historical_pool_state_at_close` in `provenance` | **CONFIRMED** |
| Cache-Hit auf `member_cache` key | kein erneuter Load für diesen Close | **CONFIRMED** |

**ClickHouse-Queries pro Window:**  
— **NOT MEASURED** in Prod ohne Export; Response-Feld **`provenance.phase5s_batch_stats.clickhouse_query_count`** (wenn Prefill aktiv) bzw. `timing_ms.clickhouse_candles` / `build_lifetimes` im JSON.

**Server-Logs (optional, READ-ONLY):**  
`journalctl --user -u dashboard.service -n 200` — Einträge `log_window_build_complete` / Exact-Origin-Trace (keine Secrets loggen).

---

## 2. Benutzer-Anleitung (Schritt-für-Schritt)

**Vorbereitung**

1. `/live-charts/research` öffnen (eingeloggt).
2. DevTools → **Konsole** im **obersten Frame** (nicht Pane-iframe).
3. Inhalt von `MC_POOL_P2_BROWSER_MEASUREMENT_CONSOLE.js` einfügen → Enter.
4. `__MC_POOL_P2__.start()`  
5. Optional parallel: `__MC_LOAD_DIAG__.start({ symbol: "SOLUSDT", timeframe: "5m" })` nur wenn Candle-Phase getrennt gemessen werden soll.

**Layer minimieren (SUSPECTED manuell):**

- Exact Origin **aus**; Historical LMv2 **aus**; Live Micro LLD **aus**; OPI **aus** (so weit UI es erlaubt).
- Symbol **SOLUSDT**, Chart-TF **5m**.
- Pool-TF **5m**, **PD1** anhaken; Historical LMv2 **aus** lassen.

---

### Test A — Initiales Laden (Pools ein)

| Schritt | Aktion |
|---------|--------|
| A1 | `__MC_POOL_P2__.startSection("A", "pools_off_baseline")` → Exact Origin **aus** → 5 s warten → `sampleViewport("A_off")` |
| A2 | Exact Origin **an** → **sofort** `bump("checkbox_on")` manuell oder `__MC_POOL_P2__.bump("exact_origin_on")` |
| A3 | Warten bis Hint z. B. „B – Exact Origin …“ (nicht „Viewport nicht bereit“) → `sampleViewport("A_first_hint")` |
| A4 | Weitere 10–30 s → `sampleViewport("A_stable")` → `__MC_POOL_P2__.endSection({ phase: "initial_load" })` |

**Erfassen:** `window_requests[]` (Anzahl, `timing_ms`, `response_cache.hit`, `phase5s_batch_stats`), `last_metrics.adapterMs`, `canvasDrawMs`, Hint-Zeitpunkte.

---

### Test B — Cache-Reuse (aus → an)

| Schritt | Aktion |
|---------|--------|
| B1 | `startSection("B", "toggle_off_on")` |
| B2 | Exact Origin **aus** → 3 s |
| B3 | Exact Origin **wieder an** → warten bis stabil |
| B4 | `endSection` — erwarte: zweiter Zyklus weniger POSTs wenn Browser-Chunk-Store noch gültig (**HYPOTHESE → P2 misst**) |

**Hinweis:** `bumpExactOriginLoadSerial` bei Toggle kann Store invalidieren — **SUSPECTED** teilweiser Cache-Verlust; Delta in `window_http_delta` messen, nicht annehmen.

---

### Test C — Langsamer Pan (innerhalb geladener Historie)

| Schritt | Aktion |
|---------|--------|
| C1 | Nach Test A/B: sicherstellen, dass Pools sichtbar. `startSection("C", "slow_pan")` |
| C2 | Langsam ~30 s nach links, zurück, `sampleViewport` Start/Mitte/Ende |
| C3 | `endSection` — **Erwartung:** `window_http_delta === 0`, Hint enthält ggf. „cache“ |

---

### Test D — Schneller Pan

| Schritt | Aktion |
|---------|--------|
| D1 | `startSection("D", "fast_pan")` — 15 s heftig hin/her |
| D2 | `endSection` — **Erwartung:** weiterhin 0 neue Window-POSTs innerhalb Tile; Long Tasks in `long_tasks[]` |

---

### Test E — Zoom / Preisskala / Reset

| Schritt | Aktion |
|---------|--------|
| E1 | `startSection("E", "zoom_h")` — horizontal zoomen → `endSection` |
| E2 | `startSection("E2", "zoom_v")` — Preisskala / vertikal → `endSection` |
| E3 | `startSection("E3", "reset")` — Toolbar ⟲ Reset → `endSection` |

**Prüfen:** Canvas `getExactOriginDensityDrawStats().ms` pro `sampleViewport`; HTTP-Delta.

---

### Test F — Pan außerhalb geladenen Bereichs

| Schritt | Aktion |
|---------|--------|
| F1 | `startSection("F", "pan_outside")` — weit in unbekannte Historie schieben |
| F2 | `endSection` — **Erwartung:** `window_http_delta > 0`, neue `from_utc`/`to_utc` in Requests |

---

### Test G/H — Symbol / TF (optional)

Nur wenn Zeit: SOLUSDT→Symbolwechsel bzw. 5m→15m — eigene Sektionen, erwartete Cache-Invalidierung.

---

### Test I — Live-Update

**NOT MEASURED** ohne definierten Live-Publish-Zeitpunkt.  
Manuell: während normaler Nutzung 5–10 min `startSection("I")` … neue Candle closes abwarten … `endSection`. Zähle Window-POSTs vs. sichtbare Änderung.

---

### Abschluss

```javascript
__MC_POOL_P2__.stop();
__MC_POOL_P2__.exportJSON(); // → MC_POOL_P2_SOLUSDT_5M_<timestamp>.json
```

Optional: `__MC_LOAD_DIAG__.stop(); __MC_LOAD_DIAG__.exportJSON();`

---

## 3. Auswertungsmatrix (nach JSON-Export)

| Metrik | Quelle im Export | Aggregation |
|--------|------------------|-------------|
| Initiale Ladezeit bis Hint | Sektion A Events + `t_rel_ms` | Median — **post-hoc** |
| Erneute Ladezeit Toggle B | Sektion B `ended_ms - started_ms` | |
| HTTP Window-POSTs pro Sektion | `window_http_delta` | SUM pro Sektion |
| CH-Queries pro Window | `window_requests[].phase5s_batch_stats.clickhouse_query_count` | pro Request |
| Server-Wall | `timing_ms.total`, `timing_ms.build_lifetimes` | p50/p95 |
| Cache-Hit-Rate Server | `response_cache.hit` | hits / requests |
| Übertragung | `resource_window_marks[].transferSize` oder Export `encodedBodySize` | KB/Request |
| JSON-Parse | `client_ms` − `timing_ms` ≈ Netz+Parse (**SUSPECTED**) | |
| Canvas-Render | `canvas_draw.ms`, `segmentsDrawn` | Median/P95 pro `sampleViewport` |
| fullyCached-Verhalten | Sektion C/D `window_http_delta==0` | PASS/FAIL |
| Long Tasks | `long_tasks[].duration` | count, p95 |
| Redundante Berechnung | gleiche `from_utc`/`to_utc` POSTs >1 | **CONFIRMED** wenn vorkommt |

Klassifikation pro Befund in Auswertungs-Notizen: **CONFIRMED / SUSPECTED / NOT MEASURED / NOT FOUND**.

---

## 4. Trennung der Latenz-Komponenten

| Komponente | Messhebel |
|------------|-----------|
| ClickHouse | `timing_ms`, `phase5s_batch_stats`, `membership_loader` |
| Pool-State-Rekonstruktion | `build_lifetimes` in `timing_ms`; `adapterMs` in `last_metrics` |
| Server-Cache | `response_cache.hit` |
| Netzwerk | `client_ms` vs `timing_ms.total` |
| JSON | Differenz (**SUSPECTED**), `PerformanceResourceTiming` |
| Browser-Cache | Sektion B/C/D HTTP-Delta; Hint „cache“ |
| Canvas | `getExactOriginDensityDrawStats()` |
| Viewport-Kaskaden | `long_tasks`; DogeExactOriginTrace `scheduleSuperseded*` |

---

## 5. Bekannte Messlücken & minimale Instrumentierung (nur Vorschlag)

| Lücke | Status | Minimaler Vorschlag (spätere Phase) |
|-------|--------|-------------------------------------|
| `fullyCached` Flag nicht exportiert | **NOT FOUND** in global API | Ein optionaler `window.__MC_POOL_P2__` Hook in `refreshPaneImpl` **nur hinter Flag** |
| Per-Pane Chunk-Store ohne Host-Zugriff | **CONFIRMED** | Harness erweitern: `PD1ExactOriginResearch` könnte `getPaneDebugSnapshot(paneId)` exponieren |
| CH server-side query_log | **NOT MEASURED** | Read-only `system.query_log` Stichprobe mit Request-ID Korrelation |
| Frame-Zeit (FPS) | **NOT MEASURED** | `requestAnimationFrame` delta in Harness (optional, overhead niedrig) |

**Keine** dieser Instrumentierungen in MC-POOL-P2 umgesetzt.

---

## 6. Abhängigkeiten / Grenzen

- MC-LOAD-P4J unverändert; Diag kann parallel laufen, misst aber primär Candles.
- Kein Service-Neustart, kein CH-Write.
- Browser-Messung erfordert **menschliche** Pan/Zoom-Schritte.
- Ergebnisqualität: ohne exportiertes JSON bleibt Auswertung **NOT MEASURED**.

---

## 7. Referenzen

- [MC-POOL-P1 Architektur-Audit](../mc_pool_p1/MC_POOL_P1_FULL_ARCHITECTURE_AUDIT.md)
- [MC-BASELINE-02 Recovery](../mc_baseline_02/MC_BASELINE_02_FULL_RECOVERY_CHECKPOINT.md)

---

**Endstatus:** `MC_POOL_P2_BROWSER_MEASUREMENT_READY`