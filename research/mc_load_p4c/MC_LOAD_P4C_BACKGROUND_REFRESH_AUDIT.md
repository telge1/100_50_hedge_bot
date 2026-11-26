# MC-LOAD-P4C — Phase-B Background Refresh Audit (READ-ONLY)

**Datum:** 2026-11-26  
**Scope:** Code + Deployment-Kontext; **keine** Funktionsänderungen in diesem Schritt.  
**Nutzerbeobachtung (qualitativ, kein neuer Diag-Export hier):** SOLUSDT 1m→5m sehr schnell, EMA ok, **~25–30 s später** erneute sichtbare Chart-Aktualisierung.

**Endstatus:** `MC_LOAD_P4C_BLOCKED`  
(Begründung: **keine gemessene Browser-Timeline** in dieser Session; Root Cause für das 25–30-s-Fenster ist **nicht eindeutig verifiziert**, nur priorisiert.)

---

## 1. Gemessene Event-Timeline

### Was vorliegt

| Quelle | Inhalt | Zeitauflösung |
|--------|--------|----------------|
| Frühere Traces (P4/P4B) | Phase A ms, Phase B HTTP ms, EMA-Skip | Teilweise |
| Nutzer P4B manuell | „schnell“ + Update **~25–30 s** | **Schätzung** |
| Diese Audit-Session | HTTP Static/Assets, API-Cache | Kein Chart-Lauf |

### Was fehlt für PASS

`__MC_LOAD_DIAG__` Export mit `t_rel_ms` für:

- `first_candles_owner_release` (Phase A)
- `chart_setdata_started` / `completed` (A vs B, `count`)
- `mc_load_phase_b_complete`
- `ema_series_viewport_snapshot` (`before_phase_b_apply`, `after_phase_b`)
- `chart_setdata_viewport_mode` (`preserve_time`)
- `exact_origin_refresh_begin/end` (`deferred_async`)
- `all_optional_layers_scheduled`

**Ohne diese Daten: keine erfundene Millisekunden-Timeline.**

### Code-abgeleitete **Nominalsequenz** (tf-change, Fast-First)

Reihenfolge in `research_charts.js` / `chart.js`:

| Phase | Auslöser | Hauptfunktionen | Sichtbarkeit |
|-------|---------|-----------------|--------------|
| T0 | TF-`change` | `abortStalePaneWork`, `loadPane` (`tf-change`) | — |
| T0+ | Fast-First A | `sendJson` `/api/research/pane`, `applyPaneBundle` | **`setData`** (~300 Kerzen), **`setEmaOverlays`** |
| T0+ | Deferred A | `runPaneDeferredPostLoad` → `ensurePaneCaughtUp` | ggf. `mergePollCandlesIntoPane` → **`setData`** (preserveView) |
| T0+ | Schedule B | `schedulePaneHistoryPhaseB` → `setTimeout(0)` → `loadPane` (`history-phase-b`) | Status „+history…“ |
| T+B | Phase B | `applyPaneBundle` mit `preserveViewTime` | **`setData`** (~24k Kerzen), **`setEmaOverlays`** |
| T+B | Phase B Ende | `ensurePaneCaughtUp`, ggf. **`ema-post-catch-up`** | zweites `loadPane` (indicators) bei `stale` |
| T+B | Phase B | `await PD1ExactOriginResearch.refreshPane` (wenn EO an, **nicht** deferred) | Overlays / `layoutOverlays`, kein volles `setData` |
| Parallel | Poll 5 s | `pollIncremental` / `pollForming` | `updateFormingBar` oder **`setData`** (throttle 400 ms) |
| Parallel | TF+EO | `onPaneTimeframeChange` → `refreshPane({force:true})` | Overlay-Refresh (async) |
| Parallel | Phase A EO | `paneLoadReleasedEarly` → **`void refreshPane`** async | fertig **unbekannt** ms später |

**Hypothese für „zweiter sichtbarer Schub“ (Priorität):**

1. **P1 — Phase B `setData`** (~24k Kerzen): größter deterministischer Candle-Rebuild nach Phase A (Wall-Zeit = RTT + Server `pane_bundle` + iframe `setData`/`setEmaOverlays`). Passt zu „erst gut, dann History“, auch wenn nicht exakt 25–30 s in jedem Lauf.
2. **P2 — `ema-post-catch-up`** nach Phase B, wenn `stale: true` (P4B skip nur wenn nicht stale): zusätzlicher `loadPane` / `setEmaOverlays` **nach** B-`setData`.
3. **P3 — Async Exact Origin (Phase A deferred)**: `refreshPane` / `layoutOverlays` kann overlay-lastig sein; kein volles Candle-`setData`, aber wahrnehmbar bei vielen Overlays.
4. **P4 — `jumpChartsToUnix`** bei gesetztem `gotoNavViewCenterTs` / `gotoNavTargetTs` nach `whenReady` (max 8 s Wartezeit in Kette, nicht typisch 30 s allein).
5. **P5 — Poll `setdata_fallback`**: nur bei fehlgeschlagenem `updateFormingBar`; throttled, eher Mikro-Updates.

**Nicht plausibel als alleiniger 25–30-s-Timer:** `POLL_MS=5000`, `VP_DEBOUNCE_MS=400`, `FORGING_MS=250`, `schedulePaneHistoryPhaseB` delay `0`.

---

## 2. Chart-Rendering (Pfade)

### Vollständiges Candle-`setData`

| Ort | Datei | Kontext |
|-----|-------|---------|
| Phase A/B apply | `research_charts.js` → `applyPaneBundle` | `chart.setData(payload, { preserveView, preserveViewTime, skipDefaultView })` |
| Poll forming fallback | `applyFormingToPane` | `setData(..., { preserveView, skipDefaultView })` alle ≥400 ms |
| Poll merge | `mergePollCandlesIntoPane` | `chart.setData(..., { preserveView: true })` |
| Iframe ready | `on_chart_ready` | `setData(pane.pendingData)` **ohne** preserve (nur bei spätem Ready) |

### Viewport / Kamera

| Funktion | Datei | Wann nach TF-Wechsel |
|----------|-------|----------------------|
| `resetView` | `chart.js` | Phase A `seedLiveViewportForPane` wenn `tf-change` (`shouldSeedLiveViewportAfterLoad`) |
| `applyDefaultView` | `chart.js` | nur wenn **nicht** `preserveView` / in `setEmaOverlays` wenn `followLive && !viewManual` (P4B: `skipRangeRestore` bei preserve / Phase B) |
| `setVisibleLogicalRange` | `chart.js` | Phase A/B mit `preserveView` **ohne** `preserveViewTime` (P4B: B nutzt **Zeit**) |
| `setVisibleRange` | `chart.js` | `preserveViewTime` nach Phase B; EMA-Restore in `setEmaOverlays` |
| `jumpChartsToUnix` | `research_charts.js` | optional nach `tf-change` `loadPane` |

### EMA

`setEmaOverlays` nach jedem vollständigen Bundle-Apply; `clipPointsToCandleExtent` in `chart.js`.

---

## 3. Viewport-Stabilität (Soll vs. Ist)

### P4B-Mechanismen

- Phase B: `preserveViewTime: true` → `getVisibleRange` / `setVisibleRange` in `chart.js` `setData`.
- Phase B: `skipEmaRangeRestore` → verhindert `applyDefaultView` in `setEmaOverlays`.

### Architektur-Lücke (Code, nicht gemessen)

1. **Snapshot-Zeitpunkt:** `savedTimeRange` wird **am Anfang von `setData`** gelesen, nicht beim **Start von Phase-B-HTTP**. Wenn der Nutzer **während** Phase B pannt/zoomt, wird beim `setData`-Eintritt die **aktuelle** Position gesichert — das ist korrekt für „letzte Nutzerposition“. Wenn der Nutzer **nicht** pannt, ist es gleichwertig Phase-A-Position. **Wenn** der Workaround fehlt, bleibt Risiko durch andere Pfade (z. B. `resetView` nur Phase A, logical preserve).

2. **Phase B ersetzt immer alle Kerzen** (`applyPaneBundle`, kein `indicatorsOnly`) → großer Reflow auch mit preserveTime.

3. **`on_visible_range`** (debounce 180 ms → Host) triggert nach jedem programmatischen `setVisibleRange` u. a. EO `scheduleViewport`, Volume Profile, LLD — kann **Folge-Arbeit** starten, aber typisch **<1 s**, nicht 25–30 s.

### Vorher/Nachher-Werte

**Nicht erfasst** (kein Browser/Diag-Export in P4C). Erforderliche Felder: `ema_series_viewport_snapshot.visible_from/to`, Chart-API `getVisibleTimeRange`, `candle_count`, EMA `points` je Serie.

---

## 4. Zielarchitektur

**Ziel:** Phase B ergänzt History **ohne** Kamera-Reset; Position = **letzte Nutzerwahl**, nicht nur Phase-A-Snapshot.

**Bewertung:**

| Ansatz | Bewertung |
|--------|-----------|
| `preserveViewTime` (P4B) | Richtung korrekt; Snapshot-Zeitpunkt ggf. auf `history-phase-b` **Request-Start** erweitern |
| Voller Phase-B-`setData` | Notwendig für Bundle heute; Hauptkosten + sichtbarer „zweiter Schritt“ |
| Incremental history merge (nicht implementiert) | Könnte Reflow reduzieren; höherer Aufwand, EMA/Grid-Parität prüfen |
| Phase-B EO `await` | Kann Wall-Zeit verlängern; Overlays nach Candles — optional async wie Phase A |
| `ema-post-catch-up` nach B | Nur wenn Bundle wirklich stale; sonst Doppelarbeit vermeiden (P4B-Logik) |

---

## 5. Root Cause (Stand)

| Kandidat | Confidence | Verifikation |
|----------|------------|--------------|
| Phase B `applyPaneBundle` → `setData` + `setEmaOverlays` | **Hoch (mechanisch)** | Diag `chart_setdata_*` + `mc_load_phase_b_complete` Zeit |
| `ema-post-catch-up` nach B | **Mittel** | `ema_catch_up_decision`, zweites Pane-POST |
| Async EO Phase A | **Niedrig–mittel** | `exact_origin_refresh_*` `deferred_async` |
| Poll / forming | **Niedrig** für großer Sprung | `live-diag` / forming reason |

**Exakter Auslöser im 25–30-s-Fenster:** **offen** bis Diag-Export.

---

## 6. Minimaler Fix-Vorschlag (nicht implementiert)

1. **Diagnose only (nächster Schritt):** jedes `chart.setData` mit `mcLoadDiagEmit("chart_setdata", { source, count, preserveViewTime, panePhase })`.
2. **Viewport:** bei `schedulePaneHistoryPhaseB` `pane._phaseBViewFrom/To = chart.getVisibleTimeRange()`; in Phase-B-`setData` diese Range restoren (Nutzer-Pan während B berücksichtigen: max(Request-Start, aktuell) — Spezifikation klären).
3. **Sichtbarkeit:** Phase-B-Status nur in Statuszeile; optional „quiet history“-Flag ohne blocking EO `await` auf Phase-B-Pfad.
4. **`ema-post-catch-up`:** nur wenn Bundle stale **und** EMA-Punkte < Candle-Spitze nachweislich; Bundle-EMA-Reapply ohne second full span POST prüfen (read-only spike first).

---

## 7. Regressionstests (Vorschlag)

| Test | Art | Inhalt |
|------|-----|--------|
| `test_phase_b_preserve_view_time_contract` | Static/JS | `bundleOpts.preserveViewTime` bei `_historyPhaseB` |
| `test_chart_setdata_preserves_time_range` | Unit (chart.js) | Mock timeScale: Range vor/nach `setData({ preserveViewTime: true })` |
| `test_tf_change_no_second_full_setdata_from_poll` | Integration mock | Nach Phase A+B, poll mit unchanged `closedFp` → kein `setData` |
| Browser checklist | Manuell | Diag: genau **ein** großes `setData` count≈limit nach A; Pan während B, Range stabil |

---

## 8. Diagnose-Protokoll (Browser, nächster Lauf)

1. `localStorage mc.load.diag=1`, Hard-Reload `research-mc-load-p4b-v1`.
2. SOLUSDT 1m→5m; **nicht** pan bis nach 35 s (Baseline), dann Wiederholung mit Pan während „+history…“.
3. `__MC_LOAD_DIAG__.exportJSON()`; Timeline aus `event_ring` + `requests[]` + `ema_series_viewport_snapshot`.

---

## 9. Sicherheitsgrenzen

Eingehalten in P4C: READ-ONLY, kein Deployment, kein Commit, keine Trading/Pool/EMA-Formel/Collector-Änderungen; P4B-Code unverändert.

---

## Referenzen (Dateien)

- `spread_recovery_hedge_short_dev/dashboard/static/js/research/research_charts.js` — `loadPane`, `applyPaneBundle`, `schedulePaneHistoryPhaseB`, `runPaneDeferredPostLoad`, `pollIncremental`
- `spread_recovery_hedge_short_dev/dashboard/static/research_trp/chart.js` — `setData`, `setEmaOverlays`, `subscribeVisibleTimeRangeChange`
- `spread_recovery_hedge_short_dev/dashboard/static/js/research/pd1_exact_origin_density_research_v1.js` — `onPaneTimeframeChange`, `refreshPane`
- P4B Deployment: `research/mc_load_p4b/MC_LOAD_P4B_DEPLOYMENT.md`