# MC-LOAD-P4D — Runtime Browser Diagnosis

**Endstatus:** `MC_LOAD_P4D_USER_BROWSER_EXPORT_REQUIRED`

---

## 1. Browser-Infrastruktur (Agent-Lauf)

| Kanal | Ergebnis |
|-------|----------|
| **Cursor IDE Browser (MCP)** | Tab erreichbar; `http://127.0.0.1:3000/live-charts/research` → Redirect **`/login`** (401). **Keine Dashboard-Credentials** im Agent-Kontext → Research-Charts nicht steuerbar. |
| **Playwright im Repo** | nicht gefunden |
| **Lokales Dashboard** | aktiv auf `:3000` (P4B deployt, siehe `MC_LOAD_P4B_DEPLOYMENT.md`) |

**Folge:** Keine gemessenen Timelines für Test A/B/C durch den Agenten. **Keine erfundenen Messwerte.**

---

## 2. Verifizierte `__MC_LOAD_DIAG__` API

Quelle: `spread_recovery_hedge_short_dev/dashboard/static/js/research/mc_load_diag_v1.js` (ausgeliefert: `ASSET_V = mc-load-p4b-v1`, Schema `mc_load_p4_v1`).

| Member | Signatur / Verhalten |
|--------|----------------------|
| `enable()` | `localStorage["mc.load.diag"] = "1"` |
| `disable()` | entfernt Flag, `active = false` |
| `start(opts?)` | `opts`: `symbol`, `timeframe`, `view`, `test`, `backend_timings_ms` → setzt `runT0`, neuer `currentRun`, liefert `load_id` |
| `stop(extra?)` | `finishRun`, Event `trace_stop` |
| `isActive()` | `active \|\| localStorage` |
| `getLatest()` | `currentRun` oder letzter Eintrag in internem `runs` |
| `exportJSON()` | **Return:** Objekt mit `schema_version`, `runs`, `latest`, **`event_ring`**, `load_id`; plus **Download** `MC_LOAD_P1_<load_id>.json` |
| `emit(name, extra)` | Ring-Event mit `t_rel_ms` (nur wenn `runT0` gesetzt, d. h. nach `start`) |
| `noteRequest` / `patchRequest` | HTTP-Korrelation |
| `markRenderProxy` | Render-Proxy-Timings |
| `noteError` | Fehlerliste |
| `snapshotSettings(extra)` | Workspace-Flags |
| `where()` / `help()` | Host vs. Iframe-Hinweis |
| `_runs()` | Kopie abgeschlossener Runs |

**Wichtig:** Host-Seite `/live-charts/research` (top window), **nicht** Pane-Iframe.

P4B-Ereignisse (bereits im Host-JS, wenn Diag aktiv): u. a. `chart_setdata_started` / `completed`, `mc_load_phase_b_complete`, `ema_series_viewport_snapshot`, `chart_setdata_viewport_mode`, `ema_catch_up_decision`, `exact_origin_refresh_begin/end`.

**Viewport (Pane-Iframe):** `iframe.contentWindow.chartApi.debugInfo()` → `visibleTime`, `logical`, `barSpacing`, `rightOffset`, `payloadCount`, `emaOverlayIds`, `clipOverlayStats`, `setData` via `candleSetCount` / Timings indirekt.

---

## 3. Kopierfertiger Konsolen-Befehl (ein Block)

**Voraussetzungen:** Auf [dash.immotel.de](https://dash.immotel.de) oder `http://127.0.0.1:3000` eingeloggt, Seite **`/live-charts/research`**, DevTools-Konsole im **obersten** Fenster (nicht Iframe).

Führt **Test A** (60 s Beobachtung + Viewport-Samples) aus; danach Export. Für B/C denselben Block nach `test`-Label anpassen (`p4d-b` / `p4d-c`) und manuell pan/zoomen wie in Abschnitt 4.

```javascript
(function MC_LOAD_P4D_RUN() {
  var D = window.__MC_LOAD_DIAG__;
  if (!D) {
    console.error("MC-LOAD-P4D: __MC_LOAD_DIAG__ fehlt. Top-Frame /live-charts/research, Hard-Reload mc-load-p4b-v1.");
    return;
  }
  console.log("MC-LOAD-P4D where", D.where ? D.where() : "?");
  D.enable();
  var loadId = D.start({ symbol: "SOLUSDT", timeframe: "1m", test: "p4d-a-no-interaction" });
  window.__MC_LOAD_P4D__ = { loadId: loadId, samples: [], startedWallMs: Date.now() };
  function paneApi() {
    var fr = document.querySelector(".trp-pane iframe, iframe[src*='pane.html']");
    try {
      return fr && fr.contentWindow && fr.contentWindow.chartApi ? fr.contentWindow.chartApi : null;
    } catch (e) { return null; }
  }
  function sample(tag) {
    var api = paneApi();
    var di = api && api.debugInfo ? api.debugInfo() : { error: "no_chartApi" };
    var emaPts = {};
    if (api && api.getVisibleTimeRange) {
      var vt = api.getVisibleTimeRange();
      di.visibleTimeApi = vt;
    }
    var row = {
      tag: tag,
      wall_ms: Date.now(),
      t_rel_ms: Date.now() - window.__MC_LOAD_P4D__.startedWallMs,
      debugInfo: di,
    };
    window.__MC_LOAD_P4D__.samples.push(row);
    D.emit("p4d_viewport_sample", row);
    return row;
  }
  sample("start_before_tf");
  console.info("MC-LOAD-P4D: JETZT (1) SOLUSDT bestätigen (2) TF 1m→5m wechseln. Pan nur bei Test B/C.");
  window.__MC_LOAD_P4D__.interval = setInterval(function () { sample("periodic"); }, 2000);
  setTimeout(function () {
    clearInterval(window.__MC_LOAD_P4D__.interval);
    sample("t60_end");
    var payload = D.stop({ test: "p4d-a", p4d_samples: window.__MC_LOAD_P4D__.samples });
    var full = D.exportJSON();
    try {
      var blob = new Blob([JSON.stringify({
        mc_load_p4d: true,
        diag: full,
        p4d_samples: window.__MC_LOAD_P4D__.samples,
      }, null, 2)], { type: "application/json" });
      var a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "MC_LOAD_P4D_" + loadId + ".json";
      a.click();
    } catch (e2) { console.warn("download failed", e2); }
    console.log("MC-LOAD-P4D DONE", { loadId: loadId, events: (full.event_ring || []).length });
    console.log("MC-LOAD-P4D payload (copy from console):", full);
  }, 60000);
  return { loadId: loadId, hint: "60s running — switch 1m→5m now" };
})();
```

**Nach Export:** JSON als `research/mc_load_p4d/artifacts/MC_LOAD_P4D_<load_id>.json` ablegen (manuell) für Auswertung.

---

## 4. Test B / C (Anleitung)

Gleicher Skriptblock, `test: "p4d-b-pan"` bzw. `"p4d-c-zoom"`:

| Test | Aktion |
|------|--------|
| **B** | Nach sichtbarer Phase A horizontal pannen; weiter bis t=60s |
| **C** | Während „+history…“ / Phase B zoomen (Mausrad/trackpad) |

Auswertung in JSON:

- `p4d_samples[]`: `debugInfo.visibleTime`, `logical`, `barSpacing`, `rightOffset`, `payloadCount` vor/nach `chart_setdata_*` in `event_ring`
- Phase-B-Zeit: `event` === `mc_load_phase_b_complete` → `t_rel_ms`
- Große `setData`: `chart_setdata_started` → `count`; Dauer: `chart_setdata_completed.setdata_wall_ms`

**Viewport-Anforderung (t=20 Zoom nach t=10 Pan):** Vergleiche letztes `p4d_viewport_sample` **unmittelbar vor** `chart_setdata_started` mit `count` ≫ 1000 (Phase B) gegen Sample bei t=60s. Sprung = `visibleTime.from/to` weicht stark ab ohne Nutzeraktion nach Phase B.

---

## 5. Root Cause (ohne Export **nicht bestätigt**)

Aus P4C + statischem Pfad bleibt **Hypothese P1**: sichtbare Aktualisierung = **`history-phase-b` → `applyPaneBundle` → `setData` + `setEmaOverlays`**.  
**Bestätigung** erst wenn `event_ring` zeigt:

- `chart_setdata_started` mit `count` ≈ History-Limit **~25–30 s** nach `start`,
- kein zweites volles `setData` gleicher Größe kurz danach (dann eher `ema-post-catch-up` oder Poll-Fallback).

---

## 6. Minimaler Fix-Vorschlag (nach bestätigtem Export)

1. **Viewport unmittelbar vor Apply:** Snapshot in `applyPaneBundle` direkt vor `chart.setData` (nicht nur zu Beginn von `setData` im Iframe) — entspricht Nutzer-Position unmittelbar vor Rendering.
2. **Phase B:** Historie per Merge/Append wo möglich statt vollem `setData` (größerer Eingriff; EMA-Grid-Parität).
3. **Nach Apply:** kein `applyDefaultView` / keine async Range-Restore-Kette bei `preserveViewTime` (P4B teilweise vorhanden); EO auf Phase B optional `void` wie Phase A.
4. **Diag:** `p4d_viewport_sample` bereits per Skript oben in `event_ring` für künftige Läufe.

---

## 7. Redundante Renders

| Pfad | Vermeidbar? |
|------|-------------|
| Phase B volles `setData` | Nur mit inkrementeller History-Erweiterung |
| `ema-post-catch-up` wenn Bundle ok | Ja (P4B stale-Gate) |
| Poll `setdata_fallback` | Nur bei forming/update-Fehler |
| EO `refreshPane` | Async + Viewport-debounced; Overlays, nicht immer Candles |

Vollständig ohne Phase-B-Candle-Redraw **nein**, wenn History als ein Bundle geliefert wird — Ziel: **ein** sichtbarer History-Apply **ohne** Viewport-Reset.

---

## 8. Nächster Schritt → `MC_LOAD_P4D_ROOT_CAUSE_CONFIRMED`

1. Skript ausführen (Test A, ggf. B/C).
2. JSON bereitstellen.
3. Agent wertet `event_ring` + `p4d_samples` aus (kein erneuter Static-Audit).

**Kein Commit/Push. Kein Deployment in P4D.**