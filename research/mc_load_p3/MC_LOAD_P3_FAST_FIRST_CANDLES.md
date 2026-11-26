# MC-LOAD-P3 — Fast First Candles / Safe Two-Phase Loading

**Status:** `MC_LOAD_P3_BROWSER_PENDING`  
**Code:** `spread_recovery_hedge_short_dev/dashboard/`  
**Backup:** `dashboard/backups/mc-load-p3-<timestamp>/`  
**P2-Korrektur:** `t_rel_ms` 42478 = **42,478 s seit Trace-Start**, nicht 42 ms (siehe aktualisierte P2-Dokumente).

---

## Implementiert

### Zweiphasen-Laden (default an, Opt-out `localStorage mc.load.fastFirst=0`)

| Phase | Verhalten |
|-------|-----------|
| **A** | `resolveMcLoadPhaseA` → `from`/`to`/`limit` ≈ 120 sichtbare + EMA-Warmup (`maxEmaWarmupBars`) + 64 Buffer; Server `pane_bundle` mit Warmup-Trim |
| **B** | `schedulePaneHistoryPhaseB` → `loadPane({ sourceAction: "history-phase-b", preserveView: true, _historyPhaseB: true })` mit vollem `historyLoadLimitForTimeframe` |
| Status | Phase B: `N · ready (+history…)` während Nachladen |

Ausgeschlossen: Historical Replay, explizites History-`from`/`to`, `indicatorsOnly`, GO-TO jump, `skipFastFirst`, Quellen außerhalb erlaubter `sourceAction`-Liste.

### Loading / Post-Load

- `mcLoadPostLoadDeferred`: nach `applyPaneBundle` → **`releasePaneLoadOwner` sofort** (`first_candles_owner_release`)
- `ensurePaneCaughtUp` + `ema-post-catch-up` in **`runPaneDeferredPostLoad`** (Hintergrund)

### EMA-Doppelarbeit

- `paneEmaStaleVsCandles`: Anker **`lastClosedBarTime`** statt forming Tip; nach Pane-Bundle **`afterPaneBundle`** → kein Reload wenn EMA ≥ closed−1 bar

### Diagnostik (P3)

- `mcLoadDiagEmitHttp` + `http_id`, `endpoint`, getrennte `candle_*` nur für pane/candles
- `timing_key` + `*_sec` in `frontend_timings_ms`
- `render_proxy_scheduled` vs `first_candles_rendered` (`visibility: proxy_only`)
- `chart_setdata_completed.setdata_wall_ms` (echte setData-Dauer)
- Requests: `initial_fast_count`, `history_phase_b_count`

Asset: `mc-load-p3-v1` (HTML + diag).

### Statische Tests

`dashboard/test_mc_load_p3_fast_first_v1.py` — **PASS** (lokal ausgeführt).

---

## Kanonische Module (unverändert, wiederverwendet)

| Modul | Rolle |
|-------|--------|
| `historyLoadLimitForTimeframe` / `#lldHistoryWeeks` | Volles Limit Phase B |
| `pane_bundle` / CH | Phase A mit `from`/`to` + Server-Warmup |
| `maxEmaWarmupBars` | Phase-A-Fenster |
| `chart.setData` | Phase A + B (`preserveView` auf B) |
| `refreshPaneEmaOverlays` | Nur bei echtem Stale |
| `ensurePaneCaughtUp` | Deferred, nicht blockierend für Erst-`loading…` |

Keine Änderungen an PD1, OPI, Trading, CH-Rohdaten.

---

## Regression-Matrix (Abnahme)

| Szenario | Automatisiert | Browser |
|----------|----------------|---------|
| BNB 5m cold/warm | — | **BROWSER_PENDING** |
| SOL 5m | — | **BROWSER_PENDING** |
| GO TO / Scroll | — | **BROWSER_PENDING** |
| EMA9/20/59/100/200 Parität | — | **BROWSER_PENDING** (numerisch) |
| Pool/OPI/Multi/P3Q/P3U | — | **BROWSER_PENDING** |

### Manuelle Abnahme (Diag)

```js
__MC_LOAD_DIAG__.start({ symbol: "BNBUSDT", timeframe: "5m", test: "P3-cold" });
// Cold load BNB 5m
__MC_LOAD_DIAG__.stop();
__MC_LOAD_DIAG__.exportJSON();
```

**Kennzahlen aus JSON:**

| Feld | Bedeutung |
|------|-----------|
| `frontend_timings_ms.first_candles_owner_release` / `*_sec` | Zeit bis erste sichtbare Kerzen (Owner frei) |
| `frontend_timings_ms.chart_setdata_completed` + Event `setdata_wall_ms` | setData |
| `frontend_timings_ms.render_proxy_scheduled` | RAF-Proxy (≠ garantierte Pixel) |
| `frontend_timings_ms.loading_indicator_hidden` | Ende `loading…` (kann = first release bei P3) |
| `initial_fast_count` | Kerzen Phase A |
| `history_phase_b_count` | Kerzen nach Phase B |
| `requests[]` mit `http_id`, `endpoint_key`, `source_action` | pane vs ema-post-catch-up vs gap-backfill |

**Vorher (26.11. Referenz):** loading_hidden **~42,5 s**, initial ~66.804 Kerzen, ema-post-catch-up 50k.  
**Nachher:** erwartet **deutlich früheres** `first_candles_owner_release` + kleines `initial_fast_count`; Phase B asynchron; kein erfundener Vergleich bis Browser-Export.

Opt-out: `localStorage.setItem("mc.load.fastFirst","0")` → Legacy-Pfad.

---

## Vorher / Nachher (erwartete Mechanik, keine UX-Zeit erfunden)

| Aspekt | Vor P3 | Nach P3 |
|--------|--------|---------|
| Erster POST | ~weeks×2016 Kerzen | Phase A ~400 Bars (+Warmup) |
| Blockiert bis loading hidden | Catch-up + EMA-Reload + setData 67k | Nach Phase-A setData + Owner-Release |
| Zweiter POST 50k | oft False-Positive stale | seltener; echte Stale bleibt |
| Volle History | sofort | Phase B `preserveView` |

---

## Blocker

Kein `MC_LOAD_P3_BLOCKED` — Implementierung als sicher eingestuft unter kanonischer `pane_bundle`-EMA; **numerische EMA-Parität** erst mit Browser-Export.

**STOP** — kein Commit/Push.