# DOGE-LIVE-P8B — Deployment & Browser-Vorbereitung

**Verdict:** `P8B_PARTIAL`  
**Datum (UTC):** 2026-11-12

---

## 1. Deployment-Prüfung

| Punkt | Befund |
|-------|--------|
| **Dashboard-Prozess** | PID **2189782** — `spread_recovery_hedge_short_dev/.venv/bin/python app.py` |
| **Port** | **3000** (`0.0.0.0`) |
| **Produktive Route** | **Spread-Dashboard** — `app.mount("/static", …/spread_recovery_…/dashboard/static)` |
| **Pools-Repo JS** | Parallel-Kopie mit P8-Fix; **nicht** direkt ausgeliefert |
| **Ausgelieferte JS** | `GET /static/js/research/pd1_exact_origin_density_research_v1.js` enthält **`_loadCapsReady`** und **`capsLoadInflight`** (curl-Verifikation: Treffer vorhanden) |
| **Template-Referenz** | `templates/research_charts.html` → `?v=pd1-p6-1h-4h-v1` (Cache-Buster **alt**; Inhalt auf Disk ist dennoch P8-patched) |
| **JS `VERSION`-Konstante** | `pd1-p6-1h-4h-v1` (interne Telemetrie, unverändert) |
| **Python Bounds (Spread)** | `spread_recovery/…/historical_pool_state_v1.py` — `doge_5m_pd1_viewer_bounds` **syntax-korrekt** (`}`) |
| **Python Bounds (Pools)** | Syntax-Fix `)`→`}` — für **laufenden Spread-Prozess nicht zwingend** (lädt primär Spread-`research_charts`) |
| **Neustart nötig?** | **Nein** für P8-JS (StaticFiles liest aktuelle Datei). **Nein** für Python-Syntax (Spread-Modul war bereits gültig). |

### Geschützte Jobs (unverändert)

| Job | Status |
|-----|--------|
| Bybit Collector | **RUNNING** PID 1550962, **LIVE**, WS **true** |
| BNB MC3E Worker | **RUNNING** PID 1629440 |
| SOL MC3D Worker | **WORKER_NOT_RUNNING** (bereits vor P8B; nicht durch diesen Auftrag gestoppt) |
| Dashboard | **nicht neu gestartet** |

---

## 2. Kontrollierter Neustart

**Entfallen** — P8-Fixes sind auf dem Server in der ausgelieferten JS-Datei sichtbar; Spread-Python war nicht blockiert.

**Falls später gewünscht:**  
`bash /home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard/start_tf1d_local.sh`  
(nur nach gezieltem Stop der bestehenden `app.py`-PID — nicht in P8B ausgeführt.)

---

## 3. API-Regression (Agent, ohne Browser-Session)

| Check | Ergebnis |
|-------|----------|
| Dashboard HTTP `/` | **302** |
| Collector forming DOGEUSDT | **OK** (z. B. close **0.06563**, `time` 1m-Bucket) |
| `/api/research/forming-bar` ohne Auth | **401** (erwartet) |
| **5m PD1 Bounds** (Pools-PYTHONPATH-Smoke) | `data_available_to` **2026-11-12T16:30:00Z**, gültiges 1h-Window **281 pools** (P8-Nachtest) |
| **30m / 1h / 4h** Window-Sweep in CLI | **BLOCKED** in Agent-Session (Spread-`PYTHONPATH` vs. `pool_pattern` / Modulpfade); **Produktions-`app.py` setzt erweiterten PYTHONPATH** — Live-API nicht ohne Login curl-getestet |
| Falsche 15m-Fallback-Pipeline | **Nicht reproduziert** in 5m-Bounds (`historical_mixed_frozen_doge_apr_sep_202…` publish line) |
| HTTP 422 gültiges Live-Fenster | Backend: **200** im Tail-Fenster; **422** nur bei bewusst **OUT_OF_SCOPE** (Dez-2026-Request) |

**Spread-pytest (Subset):** 4 passed, 5 failed (Harness/Template-Drift, **nicht** P8-loadCaps-spezifisch).

---

## 4. Browser-Test vorbereiten (manuell, eingeloggt)

### Ausgelieferte Artefakte

| Artefakt | Pfad / Wert |
|----------|-------------|
| Exact-Origin JS | `/static/js/research/pd1_exact_origin_density_research_v1.js?v=pd1-p6-1h-4h-v1` |
| P8-Marker im Quelltext | `_loadCapsReady`, `loadCapsOnce`, `capsLoadInflight` |
| Candle-Trace | `/static/js/research/doge_candle_live_trace_v1.js?v=p7-candle-trace-v1` |

### TRACE aktivieren

1. **Exact-Origin / Window-TRACE** (P5I/P8): Entwickler-Konsole — bestehende Exact-Origin-Trace-Flags laut P5I-Doku (localStorage / UI, projektspezifisch).
2. **Live-Candle-TRACE (P7):**  
   `localStorage.setItem('DOGE_CANDLE_LIVE_TRACE', '1');`  
   dann **Hard-Refresh** (Strg+Shift+R).

### Erwartete TRACE-Zeilen (PASS-Kriterien)

| Stage | Erwartung nach P8 |
|-------|-------------------|
| `loadCaps` | `done: true`, `doge_stored` ≠ `none`, ggf. `on_demand_*_ok: true` |
| `scopeCaps` | **Nicht** `path=capabilities_root_fallback` für DOGE **5m PD1** nach vollständigem `loadCaps` |
| `window_http_response` | **`http: 200`** für Fenster **innerhalb** `data_available_from/to` am Live-Rand |
| `window_http_response` | **422** nur, wenn Viewport **nachweislich** über Bounds (dann `OUT_OF_SCOPE` in Netzwerk-JSON) |
| `forming_candle_applied` | Periodisch bei DOGE-Live-Chart (mit `DOGE_CANDLE_LIVE_TRACE`) |

### Browser-PASS (für spätere P8B_PASS)

- [ ] Eingeloggt auf `/live-charts/research`, DOGEUSDT, Pool-TFs **5m / 30m / 1h** jeweils PD1 ohne dauerhafte 422.
- [ ] `loadCaps` → `doge_bounds_ok: true` (Netzwerk-Tab: `/api/research/doge-5m-pd1-viewer-bounds` **200**).
- [ ] Live-Preis: Forming-TRACE oder sichtbarer Tip-**close** bewegt sich über 30 s.
- [ ] Hard-Refresh einmal (wegen altem `?v=` im Template).

**P8B hat keine authentifizierte Browser-Abnahme durchgeführt.**

---

## 5. Risiken / Hinweise

- Template-Cache-Query `?v=pd1-p6-1h-4h-v1` kann **Browser-Cache** koppeln — Hard-Refresh obligatorisch.
- **SOL MC3D** aktuell **aus** — für PD1-Publish-Tail weiterer Replay ggf. separat prüfen.
- Voller **P8B_PASS** erst nach manueller Browser-Checkliste.

---

## Verdict

| Code | Bedeutung |
|------|-----------|
| **P8B_PARTIAL** | Server liefert P8-JS; Collector/Dashboard healthy; Neustart unnötig; Browser & volle TF-Regression ohne Session offen |
