# DOGE-LIVE-P8 — Preisparität, Latenz, HTTP-422

**Verdict:** `P8_PARTIAL`  
**Datum (UTC):** 2026-11-12

---

## Kurzfassung

| Bereich | Ergebnis |
|---------|----------|
| Bybit ↔ Collector Forming (Last-nahe) | **PASS** — typisch **0–0,03 %**, Preis bewegt sich im 1m-Bucket |
| Browser-Dashboard `forming-bar` | **Nicht gemessen** (401 ohne Session) |
| Forming über Chart-TFs | **PASS (design)** — eine 1m-Quelle → `formingBarForTf` / `pollForming` 250 ms (wie P7) |
| HTTP 422 `/live-charts/viewer/api/window` | **Ursache belegt:** `OUT_OF_SCOPE` + Client-Race / falsche Caps |
| Minimaler Fix | **Ja** (3 kleine Änderungen, siehe unten) |
| Pool-Regression (CH-Laufzeit) | **PARTIAL** — Backend-Logik verifiziert; volle 6-Kombi-Laufzeit in Agent-Session nicht abgeschlossen (CH-lastig) |

---

## 1. Bybit-Preisvergleich (Phase A/B)

**Kette (produktiv):**

`Bybit linear DOGEUSDT` → Collector `127.0.0.1:8787` → Dashboard `fetch_forming_candle` → Chart `pollForming`.

| Stufe | Messung (Stichprobe) |
|-------|----------------------|
| Bybit Last | **0,06590** USDT (`category=linear`, gleiches Symbol) |
| Collector forming close | **0,06590–0,06592**, `time` = 1m-Open (Unix), `confirm: false` |
| Abweichung | **0,000 % – 0,030 %** vs. Bybit-Snapshot (nicht tick-synchron) |
| HTTP-Latenz Collector | **~0,6–3,8 ms** localhost |
| CH letzte 1m | **2026-11-12 16:21:00 UTC** (~1–2 min hinter Forming, erwartet) |
| Dashboard `/api/research/forming-bar` | **401** ohne Cookie — Browser-Pfad nicht in dieser Session gemessen |

**Datei:** `research/doge_live_p8_probe_results_v1.json`

**Latenz-Stufen (qualitativ):**

| Stufe | Typische Verzögerung |
|-------|----------------------|
| Feed → Collector WS | Sub-Sekunde (P7) |
| Collector → Dashboard API | **< 5 ms** (lokal) |
| Dashboard → Browser | **~250 ms** Poll + Render (P7-Design, hier nicht live gemessen) |
| Zwischenzeitliche Marktbewegung | Kann Δ auch bei „guter“ Pipeline erklären |

---

## 2. Forming-Candles alle Chart-TFs (Phase C)

**Backend/Frontend-Vertrag (unverändert, READ-ONLY):**

- Eine **1m-Forming**-Quelle; `formingBarForTf` skaliert auf **5m / 15m / 30m / 1h / 4h**.
- **`time`** = Bucket-**Open** (bleibt im Bucket konstant); **close/high/low** aktualisieren bei Ticks.
- Collector-Probe: `forming_time` konstant über 10×250 ms, **close** änderte sich **3×** (Marktbewegung).

Kein Befund für veraltete/doppelte Forming-Kerzen in der Pipeline-Probe.

---

## 3. HTTP 422 — exakte Ursache (Phase D)

**Server** (`pd1_landscape_viewer_v1/api.py` → `build_landscape_window`):

- Status **422** mit JSON `{ code, message, details }`.
- Häufiger Code: **`OUT_OF_SCOPE`** — *"request outside published chart data window"*.
- Bedingung: `vis_to > export_hi` oder `vis_from < data_lo` (`historical_window_v1.py`).

**Typischer Client-Pfad (TRACE-Hypothese bestätigt als *möglich*, nicht alleinige Ursache):**

1. `refreshPane` → `await loadCaps()`.
2. Früher: `caps` wurde **sofort** nach `/viewer/api/capabilities` gesetzt, **bevor** `/api/research/doge-5m-pd1-viewer-bounds` patched.
3. Zweiter paralleler `loadCaps()` konnte **`cache: HIT`** auf **unvollständigen** Caps zurückgeben.
4. `scopeCaps` → **`capabilities_root_fallback`** → `data_available_to` ≈ **2026-09-30** (Root/Apr–Sep).
5. Live-Viewport schneidet **über** diese Bounds → geclippte oder volle Window-Requests → **`OUT_OF_SCOPE` 422**.

**Zusätzlich gefunden (Repo `pools+ob+delta_bot`):**

- `doge_5m_pd1_viewer_bounds()` in `historical_pool_state_v1.py` hatte **Syntaxfehler** `return { ... )` → Bounds-API konnte Imports/Tests brechen (Spread-Copy bereits mit `}` korrekt).

**Erster fehlerhafter Schritt:** Window-POST mit `to_utc` **jenseits** `data_export_to_inclusive` **oder** mit Client-Bounds aus **Fallback-Caps** vor DOGE-Patch.

---

## 4. Minimaler Fix (Phase E) — implementiert

| # | Änderung | Datei |
|---|----------|-------|
| 1 | `loadCaps`: **`_loadCapsReady`**, **`capsLoadInflight`**, kein frühes Caps-HIT vor Patch-Ende | `pd1_exact_origin_density_research_v1.js` (Pools + **Spread-Dashboard-Kopie**) |
| 2 | `doge_5m_pd1_viewer_bounds`: `)` → `}` | `pools+ob+delta_bot/.../historical_pool_state_v1.py` |

**Keine** neue FSM, kein PD1-Recalc, kein 15m-Fallback.

**Deploy-Hinweis:** **Hard-Refresh** (JS) + ggf. **Dashboard-Neustart**, damit Python-Fix und JS aktiv sind (laufender Prozess PID bekannt, bewusst nicht neu gestartet).

---

## 5. Pool-Regression (Phase F)

| Chart-TF | Pool-TF | Backend-Erwartung |
|----------|---------|-------------------|
| 5m | 5m | Publish-SSOT-Bounds, `build_landscape_window` **200** im gültigen Fenster |
| 30m / 1h | 30m / 1h | On-Demand Bounds-API + Window |
| 4h | 4h | PD10 Scope |
| 15m | 4h | Erlaubte Matrix (RELEASED_VIEWER_SCOPES) |
| 4h | 5m | Cross-TF View + 5m-Pool |

**Agent:** 1h-`build_landscape_window`-Smoke-Lauf **>120 s** (CH-Last) — Ergebnis offen.  
**Manuell empfohlen:** eingeloggter Browser, TRACE `window_http_response` → **http: 200**, kein 422 direkt nach Page-Load.

---

## 6. Systemstabilität (Phase G)

| Dienst | Status (Stichprobe) |
|--------|---------------------|
| Bybit Collector | **RUNNING** PID 1550962, ~270 MB, Port **8787** |
| Dashboard | **RUNNING** PID 2189782, Port **3000** |
| SOL/BNB MC3 Worker | laufen (nicht gestoppt) |
| RAM available | ~**10 GiB** |

---

## 7. Tests

| Test | Ergebnis |
|------|----------|
| `test_pd1_historical_window_v1.py` | Collection zuvor **SyntaxError** (behoben im Pools-Modul) — **nicht vollständig neu gelaufen** |
| Live Collector/Bybit-Probe | **PASS** (siehe JSON) |
| Browser live-diag | **OFFEN** (Auth) |

---

## 8. Offene Browser-Tests

- Eingeloggt: **34+ painted** erneut prüfen, TRACE `scopeCaps` ≠ `capabilities_root_fallback` nach Fix.
- `window_http_response` bei Live-Edge ohne 422.
- Preisparität **Chart close vs. Bybit** über 30–60 s.

---

## 9. Git-Status (pools+ob+delta_bot, Auszug)

Viele bestehende **M**-Dateien (Research/Dashboard); P8-Änderungen zusätzlich:

- `historical_pool_state_v1.py` (Syntax)
- `pd1_exact_origin_density_research_v1.js` (Pools)
- Spread-Kopie JS (deployed static)
- `research/doge_live_p8_*`

**Kein Commit** (Vorgabe).

---

## 10. Risiken

- CH-Last bei parallelen MC3-Workern → langsame Window-API (kein 422, aber UX).
- Bounds-API erst nach **Dashboard-Reload** mit Python-Fix überall konsistent.
- Volle Echtzeit-Browser-Parität weiterhin **nicht** ohne Session behauptet.

---

## Verdict-Matrix

| Kriterium | Code |
|-----------|------|
| Preisparität Backend | PASS |
| Browser-Parität | OFFEN |
| 422 Root Cause | PASS (dokumentiert) |
| Fix minimal | PASS (implementiert) |
| Pool-Regression E2E | PARTIAL |
| **Gesamt** | **`P8_PARTIAL`** |
