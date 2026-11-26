# MC-LOAD-P4B — EMA & Viewport Regression (BLOCKER)

**Asset:** `mc-load-p4b-v1` (Host + `chart.js` iframe)  
**Backup:** `dashboard/backups/mc-load-p4b-20261126T120900Z/`  
**Browser PASS:** ausstehend — **kein PASS** ohne erneuten 1m→5m-Test.

## Symptom (Nutzer, `mc-load-p4a-v1`)

1. Schneller 5m-Wechsel, Phase A: Kerzen + EMA korrekt.
2. Phase B History: Chart **springt**, danach **EMA-Bänder weg**.
3. Trace: Phase B `request_limit` 24192, Response **66.804**, `cache: hit`; `ema_post_catch_up` `stale: true`, `skipped: true` (`history_phase_b_bundle_ema`).

---

## A. Chart-Sprung — Root Cause

**Pfad Phase B**

1. `schedulePaneHistoryPhaseB` → `loadPane(..., preserveView: true, _historyPhaseB: true)`.
2. `applyPaneBundle` → `chart.setData(..., { preserveView: true })`.
3. In `chart.js` `setData`: bei `preserveView` wird **`getVisibleLogicalRange()`** gespeichert und nach dem Load mit **`setVisibleLogicalRange`** zurückgesetzt.

**Problem:** Phase A hat ~**304** Bars, Phase B ~**66k**. Der **logische** sichtbare Bereich (Indizes 250–304) zeigt nach Backfill **völlig andere Zeitachsen** (alte Historie statt Live-Tip) → sichtbarer **Sprung**.

**Zusätzlich:** `setEmaOverlays` nach `setData` mit `followLive && !viewManual` rief **`applyDefaultView()`** auf (kein `skipRangeRestore`) → zweite Kamerabewegung.

**Fix P4B**

| Änderung | Datei |
|----------|--------|
| `preserveViewTime` für Phase B: UTC-`getVisibleRange` / `setVisibleRange` | `chart.js`, `research_charts.js` (`bundleOpts.preserveViewTime` wenn `_historyPhaseB`) |
| `setEmaOverlays`: `skipRangeRestore` wenn `preserveView` / Phase B | `research_charts.js` `applyPaneBundle` |

---

## B. EMA verschwinden — Root Cause

**1. Skip bei `stale: true`**

P4 skippte `ema-post-catch-up` **immer** bei `_historyPhaseB`, auch wenn `paneEmaStaleVsCandles(..., afterPaneBundle: true)` **true** blieb.

**Zulässig nur:** skip wenn Bundle-EMA **nicht** stale.

**Fix:** `mcLoadSkipEmaCatchUpAfterPaneBundle` skip nur wenn `!stale`; sonst `history_phase_b_stale_bundle` → ggf. `ema-post-catch-up`.

**2. Sehr großer Phase-B-`setData` (66.804) + Grid-Clip**

Bei Übersized Cache-Hit kann Bundle-EMA und Kerzen-Menge divergieren; `clipPointsToCandleExtent` kann Punkte stark reduzieren. Korrekte Kerzenanzahl (**24.192**) reduziert das Risiko (Backend-Trim + API-Neustart).

**3. Viewport-Jump + `applyDefaultView` in `setEmaOverlays`**

Kombination aus logischem `preserveView` und `applyDefaultView` erklärt „EMA kurz da, dann weg“ zusammen mit Reflow.

**Diagnose P4B:** Events `ema_series_viewport_snapshot` (`before_phase_b_apply`, `after_phase_b`), `chart_setdata_viewport_mode: preserve_time`.

---

## C. Backend Cache `24192` → `66804`

**Code (P4A)** enthält `_tail_limit_candles` auf Cache-Hit in `_packed_from_cached`.

**P4B-Browser mit weiterhin 66.804** bedeutet fast sicher:

1. **API-Prozess ohne Reload** (alter Code im Speicher), oder  
2. Reverse-Proxy/anderer Worker.

**P4B Response-Felder zur Verifikation:** `cache_tail_trimmed`, `cache_stored_count`, `candles_returned` (bei Hit).

**Nach API-Restart erwarten:** `candles.length === 24192`, `cache_tail_trimmed: true` wenn Blob > limit.

---

## D. Akzeptanz (Browser)

| # | Kriterium | Status |
|---|-----------|--------|
| 1 | Phase A: 5m + EMA | unverändert erwartet |
| 2 | Phase B lädt History | ja |
| 3 | Kein Sprung nach B | **Fix implementiert — BROWSER_PENDING** |
| 4 | EMA 9/20/59/100/200 sichtbar | **Fix implementiert — BROWSER_PENDING** |
| 5–8 | History/GO TO/Multi/Layers/Fast-First | unveränderte Semantik angestrebt |

## Testplan Browser

1. Hard-Reload Host (`research-mc-load-p4b-v1`) **und** Pane-iframe (`chart.js?v=mc-load-p4b-v1`).
2. **Dashboard/API neu starten** (Cache-Trim).
3. `mc.load.diag=1`, SOLUSDT **1m → 5m**.
4. Export: `ema_viewport_after_phase_b`, Phase-B `candles_returned`, sichtbare EMA-Serien.

## Lokal

- `pytest` P3 + cache limit: **6 passed**
- `node --check` / `py_compile service.py`: OK
- Kein Commit/Push