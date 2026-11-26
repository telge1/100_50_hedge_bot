# MC-LOAD-P1B — Diagnose-Export im Browser reparieren

**Status:** `MC_LOAD_P1B_PARTIAL` — Code-Fix im Dashboard-Tree; Browser-Abnahme **BROWSER_PENDING** (kein authentifizierter Research-Tab in dieser Session).

---

## Symptom (vor P1B)

- Konsole: `ReferenceError: __MC_LOAD_DIAG__ is not defined` bei `__MC_LOAD_DIAG__.start()` / `stop()`.
- P1-Instrumentierung in `research_charts.js` war vorhanden, das API-Objekt in der Konsole aber nicht erreichbar.

---

## Root cause (P1B)

| # | Ursache | Klasse |
|---|---------|--------|
| 1 | **DevTools-Kontext = Chart-Iframe** (`/static/research_trp/pane.html`). `mc_load_diag_v1.js` wird nur im **Host** (`research_charts.html`) geladen, nicht im Iframe. | **CONFIRMED** (Architektur) |
| 2 | **Cache / alte HTML** ohne `mc_load_diag`-Script-Tag oder mit alter `?v=` → 404 oder fehlendes Objekt am Host. | **SUSPECTED** (häufig bei Hard-Reload ohne Template-Deploy) |
| 3 | Laufender Server zeigt ggf. noch Template **ohne** P1-Script (z. B. Kopie `pools+ob+delta_bot/backtester/...` statt `spread_recovery_hedge_short_dev/dashboard`). | **SUSPECTED** |

**Nicht** die Ursache für `ReferenceError`: fehlende Hooks in `research_charts.js` (die prüfen nur, wenn Objekt existiert).

---

## Fix (nur `__MC_LOAD_DIAG__`-Verfügbarkeit)

**Pfad-Basis:** `spread_recovery_hedge_short_dev/dashboard/`

| Datei | Änderung |
|-------|----------|
| `static/js/research/mc_load_diag_v1.js` | `publishDiag()` → `window`, `top`, `parent` (same-origin); `noteError()`; `where()` / `help()`; Asset `mc-load-p1b-v1`; DOMContentLoaded-Log |
| `static/js/research/mc_load_diag_bridge_v1.js` | **neu** — im Iframe: Referenz auf Host-API oder klare Fehlermeldung |
| `static/research_trp/pane.html` | Bridge-Script nach `chart.js` |
| `templates/research_charts.html` | Cache-Bust: `mc-load-p1b-v1`, `research-mc-load-p1b-v1` |
| `static/js/research/research_charts.js` | `mcLoadDiagNoteError` bei HTTP/Apply-Fehlern; `load_incomplete`-Event |

**Keine** Änderungen an Trading, PD1, OPI, Candle-Aggregation, Performance.

---

## Trace-Abdeckung (unverändert + P1B)

Wenn `start()` aktiv (oder `localStorage mc.load.diag=1` + `start()` für `currentRun`):

| Phase | Events / Felder |
|-------|-----------------|
| Request | `candle_request_started`, `noteRequest` / `patchRequest` (GET/POST inkl. `/api/research/pane`) |
| Parse | `candle_response_received`, `response_parse_completed` |
| Iframe | `chart_iframe_ready_wait_*` |
| setData | `chart_setdata_started` / `completed`, `candle_normalization_completed` |
| Render | `markRenderProxy` → `first_candles_rendered` |
| Loading UI | `loading_indicator_hidden` → `loading_hidden_ms` |
| Post-load | `ensure_pane_caught_up_*`, `exact_origin_refresh_*`, `all_optional_layers_scheduled` |
| Fehler (P1B) | `load_error` + `runs[].errors[]`, `load_incomplete` |

`exportJSON()` exportiert `runs`, `latest`, `event_ring` auch bei Fehlern/unvollständigen Läufen (sofern `start()` vorher lief).

---

## Debug-Trace vs. zusätzliche Requests

| Mechanismus | Extra-Traffic? |
|-------------|----------------|
| `mcLoadDiagEmit` / Ring-Buffer | **Nein** — nur In-Memory |
| `X-MC-Load-Diag: 1` auf POST `/api/research/pane` | **Nur wenn Diag aktiv** — Server hängt `_diag`-Timings an bestehende Pane-Response (kein zweiter Pane-POST) |
| `noteRequest` für HTTP | **Nein** — protokolliert bestehende `fetch`/`sendJson` |
| Bridge / `help()` | **Nein** |

**Fazit:** Diag löst **keine** zusätzlichen Pane-/Pool-Requests aus; beobachtete Langläufer und 409er sind unabhängig (siehe Befunde).

---

## Separate Befunde (Konsole BNBUSDT 5m — nicht in P1B gefixt)

Dokumentiert für MC-LOAD-P2 / Infrastruktur; **keine** Ursachenbehauptung für P1-Hauptpfad.

| Befund | Beobachtung | Klasse |
|--------|-------------|--------|
| Pool-Window-Requests | **11–17 s** Dauer | **USER_OBSERVED** |
| `/api/research/ob1000/lease` | viele **HTTP 409** (Lease-Konflikt / parallele Holder) | **USER_OBSERVED** + Code-Pfad in `research_charts.js` (`ob1000/lease` POST) |
| `refreshPane_coalesced` | wiederholt (PD1 Exact-Origin / `Tc.event`) | **USER_OBSERVED** — Coalescing bei Viewport/Refresh, nicht durch MC-LOAD-Diag |
| `load_serial` | Serialisierung Pane-Loads (`research_charts.js`) | **SUSPECTED** — erwartetes Load-Gating, kein Diag-Artefakt |

---

## Deployment-Check (Server)

```bash
# Static file (ohne Auth) — muss 200 + P1B-Header liefern:
curl -sI "http://<host>:<port>/static/js/research/mc_load_diag_v1.js?v=mc-load-p1b-v1"

# Nach Template-Deploy ggf. App neustarten, damit research_charts.html neu gerendert wird.
grep mc_load_diag_v1 spread_recovery_hedge_short_dev/dashboard/templates/research_charts.html
```

**Lokal diese Session:** `http://127.0.0.1:3000/static/js/research/mc_load_diag_v1.js` → **200**, Datei enthält `mc-load-p1b-v1`.

---

## Manuelle Browser-E2E (wenn `BROWSER_PENDING`)

1. Hard-Reload **Research Host**: `/live-charts/research` (Cache leeren).
2. DevTools → **Console** → Kontext-Dropdown: **top** / Research-Seite (nicht `pane.html`).
3. Prüfen: `typeof __MC_LOAD_DIAG__` → `"object"`.
4. Optional: `__MC_LOAD_DIAG__.help()` → Log `[MC-LOAD-P1B] __MC_LOAD_DIAG__ ready`.
5. Trace:
   ```js
   __MC_LOAD_DIAG__.start({ symbol: "BNBUSDT", timeframe: "5m", test: "A" });
   // Symbol wechseln oder TF 5m laden
   __MC_LOAD_DIAG__.stop({ outcome: "manual" });
   __MC_LOAD_DIAG__.exportJSON();
   ```
6. **Iframe-Kontext:** `__MC_LOAD_DIAG__.start()` sollte nach P1B-Bridge auf **dieselbe** Host-API delegieren (wenn Host geladen). Wenn Host-Script fehlt: gewollte Warnung + Hinweis auf Top-Frame.
7. Network: bei aktivem Diag ein POST `/api/research/pane` mit Header `X-MC-Load-Diag: 1`; keine **zusätzlichen** Pane-POSTs nur wegen Diag.

---

## Browser-Test diese Session

| Test | Ergebnis |
|------|----------|
| Authentifizierter `/live-charts/research` | **NOT_RUN** (kein Login / kein Tab) |
| Static `mc_load_diag_v1.js` | **MEASURED** 200 auf `:3000` |
| `start` / `stop` / `exportJSON` im echten UI | **BROWSER_PENDING** |

---

## Geänderte Dateien (Übersicht)

```
spread_recovery_hedge_short_dev/dashboard/
  static/js/research/mc_load_diag_v1.js
  static/js/research/mc_load_diag_bridge_v1.js          (new)
  static/js/research/research_charts.js                 (error hooks only)
  static/research_trp/pane.html
  templates/research_charts.html

pools+ob+delta_bot/research/mc_load_p1/
  MC_LOAD_P1B_DIAG_EXPORT_FIX.md                        (this file)
```

**STOP** — kein Commit, kein Push, keine Performance-Optimierung.