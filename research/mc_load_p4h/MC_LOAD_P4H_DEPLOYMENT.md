# MC-LOAD-P4H — P4G Live Deployment & Viewport Verification

**Endstatus:** `MC_LOAD_P4H_DEPLOYED_BROWSER_PENDING`

Keine funktionalen Codeänderungen in P4H. Kein Commit/Push.

---

## Pre-Deployment

| Check | Ergebnis |
|-------|----------|
| Regression | **12 passed** (`test_mc_load_p3`, `p4e`, `p4g`, `test_candle_cache_limit_hit`) |
| P4G-Diff-Fokus | `chart.js`: `restoreViewportAfterDataChange`, `captureViewportForRestore`, `emitViewportMutation`; Host: `on_viewport_mutation` → `mcLoadDiagEmit` |
| P4E History/Cache/EMA | Unverändert (`mcLoadHistoryPhaseBRange`, `_range_end_is_live`, EMA-Gates) |
| Right margin | **Kein** fixes „96 Bars“ — Restore nur wenn gespeichertes **logical.to > lastIdx** (+ optional gespeichertes `barSpacing`) |

---

## Deployment

| | |
|---|---|
| Unit | `systemctl --user restart dashboard.service` |
| PID vorher | **1917822** |
| PID nachher | **1924025** |
| Status | `active` |
| Health | `GET :3000/login` → **200** |

Nicht neu gestartet: nginx, ClickHouse, Trading, Orderbook, Queue Consumer, Collectors.

---

## Asset-Verifikation (`127.0.0.1:3000` = öffentliche Route via nginx)

| Asset | Verifiziert |
|-------|-------------|
| Host | `research-mc-load-p4g-v1` |
| Pane | `chart.js?v=mc-load-p4g-v1` |
| Diag | `ASSET_V = "mc-load-p4g-v1"` |
| `viewport_mutation` | `chart.js` → `bridge.on_viewport_mutation` → Host `mcLoadDiagEmit("viewport_mutation", { before, after, reason, … })` |

---

## Browser-Diagnose — kopierfertiger Konsolenblock (120 s)

**Voraussetzungen:** Eingeloggt, `/live-charts/research`, Konsole im **Top-Fenster**, Hard-Reload `mc-load-p4g-v1`.

Nutzt ausschließlich: `__MC_LOAD_DIAG__` (`enable`, `start`, `emit`, `stop`, `exportJSON`), `chartApi.debugInfo()` / `getVisibleTimeRange()` im Pane-Iframe.

```javascript
(function MC_LOAD_P4H_RUN() {
  var D = window.__MC_LOAD_DIAG__;
  if (!D) {
    console.error("MC-LOAD-P4H: __MC_LOAD_DIAG__ fehlt. Top-Frame, Hard-Reload mc-load-p4g-v1.");
    return;
  }
  console.log("MC-LOAD-P4H where", D.where ? D.where() : "?");
  D.enable();
  var loadId = D.start({
    symbol: "SOLUSDT",
    timeframe: "1m",
    test: "p4h-no-interaction-viewport-120s",
  });
  window.__MC_LOAD_P4H__ = {
    loadId: loadId,
    samples: [],
    startedWallMs: Date.now(),
    duration_ms: 120000,
    sample_interval_ms: 2000,
  };
  function paneApi() {
    var fr = document.querySelector(".trp-pane iframe, iframe[src*='pane.html']");
    try {
      return fr && fr.contentWindow && fr.contentWindow.chartApi ? fr.contentWindow.chartApi : null;
    } catch (e) { return null; }
  }
  function barsAfterFromDebug(di) {
    if (!di || !di.logical || di.payloadCount == null) return null;
    var n = Number(di.payloadCount);
    var to = Number(di.logical.to);
    if (!Number.isFinite(n) || n < 1 || !Number.isFinite(to)) return null;
    return to - (n - 1);
  }
  function sample(tag) {
    var api = paneApi();
    var di = api && api.debugInfo ? api.debugInfo() : { error: "no_chartApi" };
    if (api && api.getVisibleTimeRange) {
      try { di.visibleTimeApi = api.getVisibleTimeRange(); } catch (e) { /* ignore */ }
    }
    di.barsAfterComputed = barsAfterFromDebug(di);
    var row = {
      tag: tag,
      wall_ms: Date.now(),
      t_rel_ms: Date.now() - window.__MC_LOAD_P4H__.startedWallMs,
      debugInfo: di,
    };
    window.__MC_LOAD_P4H__.samples.push(row);
    D.emit("p4h_viewport_sample", row);
    return row;
  }
  sample("start_before_tf");
  console.info("MC-LOAD-P4H: JETZT SOLUSDT + 1m→5m. 120s KEINE Interaktion (kein Pan/Zoom/Resize).");
  window.__MC_LOAD_P4H__.interval = setInterval(function () { sample("periodic"); }, 2000);
  setTimeout(function () {
    clearInterval(window.__MC_LOAD_P4H__.interval);
    sample("t120_end");
    D.stop({
      test: "p4h",
      p4h_samples: window.__MC_LOAD_P4H__.samples,
      duration_ms: window.__MC_LOAD_P4H__.duration_ms,
    });
    var full = D.exportJSON();
    var viewportEvents = (full.event_ring || []).filter(function (e) {
      return e && (e.event === "viewport_mutation" || e.name === "viewport_mutation");
    });
    var bundle = {
      mc_load_p4h: true,
      load_id: loadId,
      diag: full,
      p4h_samples: window.__MC_LOAD_P4H__.samples,
      viewport_mutation_events: viewportEvents,
    };
    try {
      var a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob([JSON.stringify(bundle, null, 2)], { type: "application/json" }));
      a.download = "MC_LOAD_P4H_" + loadId + ".json";
      a.click();
    } catch (e2) { console.warn("download failed", e2); }
    console.log("MC-LOAD-P4H DONE", {
      loadId: loadId,
      events: (full.event_ring || []).length,
      viewport_mutations: viewportEvents.length,
    });
    console.log("MC-LOAD-P4H payload (copy):", bundle);
  }, 120000);
  return { loadId: loadId, hint: "120s running — switch 1m→5m now, no interaction" };
})();
```

**Auswertung `viewport_mutation`:** In `event_ring` (bzw. `viewport_mutation_events` im Export) je Event `reason`, `before` / `after` mit `logical`, `time`, `barSpacing`, `barsAfter`.

---

## Erfolgskriterien (Browser — vom Benutzer)

- Phase A schnell (~wie P4F).
- Phase B korrekte Historienmenge (~24 192).
- `ema_stale` / Snapshot: **false**.
- `barsAfterComputed` stabil (negativ = rechter Freiraum), kein Sprung auf **0** ohne Interaktion.
- `barSpacing` ohne unerwünschten Sprung bei gleicher Visible-Time-Range.
- Kein verspäteter Viewport-Sprung (P4D t≈26 / P4F t≈44).
- Live/Forming-Updates ok.
- Pan/Zoom-Tests separat (mit Interaktion), nicht in diesem No-Interaction-Lauf.

---

## Nach Browser-Export

JSON bereitstellen → Bewertung `MC_LOAD_P4H_BROWSER_PASS` oder Follow-up.