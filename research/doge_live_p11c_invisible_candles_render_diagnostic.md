# DOGE-LIVE-P11C — Kerzen in Trace OK, im Browser unsichtbar

**Nutzerbefund (nach P11B):** Konsole zeigt u. a.  
`loadPane_pane_api_ok | n=66804` → `loadPane_setData_ok` → `loadPane_apply_ok | chart_ready=true` → `forming_candle_applied | painted=true` → `chart_last_price_updated | close=0.0657` — **visuell keine Kerzen**, teils weiterhin `loading…`.

**Verdikt:** **PARTIAL** — Root Cause im Renderer (Iframe-Größe vs. Candlestick-Rasterisierung) belegt; minimaler Fix auf produktivem Spread deployed; **kein authentifizierter Browser-PASS** in dieser Session.

---

## Kurzfassung (Ursache)

| Ebene | Befund |
|-------|--------|
| **Daten / API** | `POST /api/research/pane` liefert Kerzen; `n=66804` passt zu `historyLoadLimitForTimeframe` (großer 1m-History-Spanne, nicht „leer“). |
| **Host / Bridge** | `chart_ready=true`, `setData` erreicht `chart.js`; Forming-Update + Live-Preislinie laufen → Serie und Preisskala leben. |
| **Rendering** | **Lightweight-Charts-Candlesticks werden beim ersten `setData` bei zu kleinem Plot oft nicht gemalt** (im Code dokumentiert); Host rief `setData` teils **vor** realem Iframe-Layout auf und **ohne** garantiertes Repaint nach Resize. |
| **`loading…`** | Host-Label `.trp-pane-status`, **nicht** Iframe-`#empty` (nur bei 0 Kerzen). Kann parallel zu „unsichtbaren“ Kerzen durch zweiten `loadPane` / hängenden Owner bestehen; nach erfolgreichem `applyPaneBundle` soll Status `N · load` sein (`updatePaneLoadStatusFromBundle` + `releasePaneLoadOwner`). |

---

## Phase A — Rendering-Call-Chain (READ-ONLY)

### Architektur

1. **Host:** `templates/research_charts.html` → `static/js/research/research_charts.js`
2. **Pro Pane:** `<iframe src="/static/research_trp/pane.html">` → `static/research_trp/chart.js` + Lightweight Charts
3. **Datenpfad:** `loadPane` → HTTP `pane` → `applyPaneBundle` → `api(pane).setData(payload)` (postMessage-Bridge)

### Checkliste (logisch / Code)

| # | Frage | Ergebnis |
|---|--------|----------|
| 1 | Chart-Instanz? | Ja, wenn `loadPane_apply_ok chart_ready=true`. |
| 2 | Candlestick-Serie? | `candleSeries = chart.addCandlestickSeries(...)` in `initChart()`; `setData` bricht ohne Serie ab. |
| 3 | Serie sichtbar? | `visible: true` in Series-Options; `debugInfo().candleVisible`. |
| 4 | Richtiges Pane? | Ein Iframe-Renderer pro Pane; `api(pane)` bindet an genau dieses Frame. |
| 5–6 | Container / Canvas Größe? | `#chart` `clientWidth/Height` via `chartSize()`; Chart `width/height` aus `resize()`. **Kritischer Pfad:** beim Boot oft **0×0** im DOM, während `initChart` mit `Math.max(size, 16)` erzeugt. |
| 7 | Loading-Layer? | Iframe: `#empty` nur bei 0 Candles. Host: `loading…` Text — **deckt Chart nicht ab**. |
| 8–9 | CSS hidden / falscher Iframe? | Kein strukturelles `display:none` auf Chart-Pfad im Normalfall; Problem ist **Größe 0 / zu klein**, nicht Verstecken. |
| 10 | Leeren nach `setData`? | Kein bewusster `setData([])` nach erfolgreichem Load in `loadPane`; TF-Wechsel superseded ist separater Pfad (P11B). |

**Kommentar im Produktionscode (`chart.js` `resize()`):**

> *Candlestick series set at 0×0 often never paints; line series still does.*

Damit erklärt sich: **Trace `setData_ok` + `forming_candle_applied` + `chart_last_price_updated`**, obwohl **Kehlwerk unsichtbar** — Preislinie/Updates ≠ Candle-Raster.

`initChart()` setzt zusätzlich:

```725:772:spread_recovery_hedge_short_dev/dashboard/static/research_trp/chart.js
  function initChart() {
    const el = $("chart");
    const size = chartSize();
    const w = Math.max(size.w, 16);
    const h = Math.max(size.h, 16);
    chart = LightweightCharts.createChart(el, { /* ... */ width: w, height: h });
    lastAppliedSize = { w: size.w, h: size.h };  // raw DOM — kann 0 sein
```

---

## Phase B — Zeitachse / Preisachse

| Punkt | Befund |
|-------|--------|
| Timestamps an `setData` | Aus API-`packed.candles`; `applySeriesData` sortiert/validiert (bestehende Logik). |
| Aufsteigend / Duplikate | Bei `n=66804` ohne `setDataError` in Trace → SetData-Pfad erfolgreich. |
| Erster / letzter Bar | Über `debugInfo().first3/last3` bzw. `render_diag` (P11C) erfassbar. |
| Sichtbarer Bereich | Frischer Load: `applyDefaultView()` wenn `!preserveView && !skipDefaultView && !viewManual`. |
| Viewport-Restore | Replay / GO-TO nutzen `preserveView` / `skipDefaultView` — hier **sekundär**, da Forming-Preis sichtbar aktualisiert wird. |
| Preisskala | `autoScale: true`; Live-Price-Line bei `close=0.0657` → Skala nicht „leer“. |

**Fazit B:** Viewport ist unwahrscheinlich Hauptursache für **komplett fehlende Kerzen** bei gleichzeitig funktionierender Forming-Preis-Aktualisierung. Primär bleibt **Paint bei Initialgröße**.

---

## Phase C — Loading-Overlay & Races

| Thema | Befund |
|-------|--------|
| `loading…` vs. Daten | Overlay im **Host**, nicht im Chart-DOM. |
| `loadPane`-Reihenfolge (vor Fix) | `whenReady` (Bridge) **ohne** `waitPaneSize` — anders als `loadNewlyVisiblePanes()`. |
| Zweite Initialisierung / TF-Wechsel | `exitSupersededPaneLoad` kann Owner verwerfen; P11B Recovery — **nicht** in P11C erneut umgebaut. |
| Datenverlust nach `setData` | `lastPayload` im Iframe bleibt für Repaint erhalten; kein Leeren bei Resize. |

Abfolge nach erfolgreichem Load (Soll):

`applyPaneBundle` → `updatePaneLoadStatusFromBundle` (Status **nicht** mehr `loading…`) → `setData` → … → `releasePaneLoadOwner` (falls noch `loading…` + `pendingData` → `N · ready`).

Wenn Nutzer **trotz** `loadPane_apply_ok` noch `loading…` sieht: prüfen, ob **paralleler** `loadPane` (anderer `paneGen`/`loadGen`) Status zurücksetzt oder Recovery hängt — getrennt vom **Invisible-Candles**-Bug.

---

## Phase D — Diagnosemodus (minimal, default aus)

**Aktivierung:** bestehendes `DOGE_CANDLE_LIVE_TRACE` / `RESEARCH_BOOT_TRACE` (wie P11B).

**Neu in P11C:**

- Host: `scheduleRenderDiagnosis(pane)` → Events `render_diag` nach **1 s / 3 s / 10 s** mit u. a. `frame_w/h`, `chart_w/h`, `candle_set_count`, `candle_paint_pending`, `visible_time`, `empty_overlay`, `pane_status`.
- Trace-Felder bei Load: `loadPane_apply_ok` / `loadPane_setData_ok` mit `chart_w/h`, `candle_paint_pending`.

**Keine** neue Chart-Engine, **keine** FSM.

---

## Phase E — Minimaler Fix (nur bei belegter Ursache)

**Produktion:** `spread_recovery_hedge_short_dev/dashboard/`

| Datei | Änderung |
|-------|----------|
| `static/research_trp/chart.js` | `candlePaintPending` wenn `setData` bei `<48×48`; `resize()` → `needsCandleRepaint` (pending / wasInvalid / signifikantes Wachstum) + bedingtes `applyDefaultView`; `debugInfo` erweitert. |
| `static/js/research/research_charts.js` | `await waitPaneSize(pane, 5000)` vor `applyPaneBundle`; `scheduleRenderDiagnosis`; `requestAnimationFrame` → `chart.resize()` nach Apply; erweiterte Boot-Traces. |
| `static/research_trp/pane.html` | `chart.js?v=p11c-render-v1` |
| `templates/research_charts.html` | `research_charts.js?v=p11c-render-v1` |

**Mirror (Repo):** `pools+ob+delta_bot/backtester/short/dashboard/static/research_trp/chart.js` — gleiche Renderer-Logik; Host-P11C primär Spread.

**Nicht geändert:** PD1/D2, Collector, Publisher, Candle-API, Trading-Boxen, manuelle Zeichnungen, P11/P11B Boot-Bounds / Supersede-Logik (nur ergänzt: Size-Wait + Repaint).

**Viewport:** `applyDefaultView` nur bei `wasInvalid || candlePaintPending` im Repaint-Zweig — **nicht** bei jedem Resize (manuelle Zoom-Position bleibt geschützt).

---

## Phase F — Verifikation

| # | Kriterium | Session-Ergebnis |
|---|-----------|------------------|
| 1 | DOGE 5m Kerzen sichtbar | **OFFEN** (kein authentifizierter UI-Retest) |
| 2 | `loading…` weg | **OFFEN** |
| 3–4 | Letzte / Forming-Candle im Fenster | Trace spricht für Forming-OK; **visuell OFFEN** |
| 5 | 15m/30m/1h/4h | **OFFEN** |
| 6–8 | Pool, Zeichnungen, keine Chart-Neuerstellung | **OFFEN** |
| 9 | Schneller TF-Wechsel | **OFFEN** |

**Server-Check (diese Session):**

- `GET /static/js/research/research_charts.js?v=p11c-render-v1` → **200**, enthält `scheduleRenderDiagnosis`.
- `GET /static/research_trp/chart.js?v=p11c-render-v1` → **200**, enthält `candlePaintPending`.

**Nutzer-Retest (empfohlen):** Hard-Refresh → Konsole `localStorage.DOGE_CANDLE_LIVE_TRACE=1` → Reload. Erwartung:

1. `loadPane_apply_ok` mit `chart_w/h` ≥ 48 und `candle_paint_pending=false` (oder kurz `true`, dann `render_diag` @3s mit `candle_paint_pending=false`).
2. Sichtbare Kerzen nach Layout; Status `66804 · load` o. ä., nicht dauerhaft `loading…`.
3. `render_diag` zeigt `empty_overlay=false`, `bars_in_view` > 0.

---

## Fehlerhafte Call-Chain (exakt)

```
loadPane (Host)
  → POST pane OK (66804 rows)
  → whenReady(bridge) OK
  → [vor P11C: kein waitPaneSize]
  → applyPaneBundle → chart.setData()
       → chart.js setData: applySeriesData @ chartSize() noch klein
       → LW-Charts: candlesticks nicht gerendert
       → live price / forming updates trotzdem (Line/Overlay-Pfad)
  → [vor P11C: resize() nur bedingt; wasInvalid nur bei lastAppliedSize < 16]
  → Host-Trace: setData_ok / apply_ok (Daten OK, Bild leer)
```

**Fix-Kette:**

```
waitPaneSize → applyPaneBundle → setData
  → candlePaintPending wenn <48×48
  → rAF resize() → needsCandleRepaint → applySeriesData + applyDefaultView (nur wenn pending/invalid)
```

---

## Offene Punkte

1. **Visueller PASS** durch Nutzer mit hartem Reload + `p11c-render-v1` (Template + Iframe).
2. Falls `loading…` **nach** Fix bleibt: `render_diag.pane_status` und parallele `loadPane_start` (Supersede) auswerten — separates P11B-Thema.
3. Optional: Boot-Standard-Timeframe (1m vs. 5m) für `n=66804` in UI-Doku festhalten.

---

## Artefakte

- Report: `research/doge_live_p11c_invisible_candles_render_diagnostic.md`
- Vorläufer: `research/doge_live_p11_chart_initial_loading_diagnostic.md`, `research/doge_live_p11b_persistent_loading_root_cause.md`

**Kein Commit. Kein Push. Publisher/Collector unverändert laufend gelassen.**
