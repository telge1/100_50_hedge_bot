# MC-LOAD-P4J — P4I Live Deployment

**Endstatus:** `MC_LOAD_P4J_DEPLOYED_BROWSER_PENDING`

Keine funktionalen Codeänderungen in P4J. Kein Commit/Push.

---

## 1. Pre-Deployment

| Check | Ergebnis |
|-------|----------|
| Regression | **15 passed** (P3, P4E, P4G, P4I, Cache-Limit) |
| P4I-Diff (Scope) | `runFinalPaneViewResetLikeToolbar`, `scheduleFinalPaneViewReset`, `mcLoadShouldFinalizePaneView`; Toolbar unverändert: `trp-reset` → `chart.resetView()` |
| Layer-Reihenfolge | Finaler Reset erst **nach** `loadPane`-Ende inkl. `exact_origin_refresh_*` (await bei Phase B), `all_optional_layers_scheduled`, EMA-Catch-up in derselben `loadPane`-Kette; Reset **+50 ms, double rAF** |
| Async-Risiko (Read-Only) | Phase-A-EO **deferred_async** kann parallel zu Phase B laufen; überschreibt Viewport typisch via Overlays, **nicht** erneutes `resetView`. Poll/Forming: **kein** finaler Reset-Pfad. **Kein** zusätzlicher Code in P4J. |
| P4E / P4G | `mcLoadHistoryPhaseBRange`, Cache-Tail, `restoreViewportAfterDataChange`, `viewport_mutation` unverändert |

---

## 2. Deployment

| | |
|---|---|
| Unit | `systemctl --user restart dashboard.service` |
| PID vorher | **1924025** |
| PID nachher | **1928817** |
| Port | 3000 |
| Health | `GET :3000/login` → **200** |

Nicht neu gestartet: nginx, ClickHouse, Trading, Orderbook, Queue Consumer, Collectors.

---

## 3. Asset-Verifikation (:3000)

| Asset | Status |
|-------|--------|
| Host | `research-mc-load-p4i-v1` |
| Pane | `chart.js?v=mc-load-p4i-v1` |
| Diag | `ASSET_V = "mc-load-p4i-v1"` |
| Final-Reset Diag | `mc_load_final_view_reset`, `mc_load_final_view_reset_skipped` in `research_charts.js` |

---

## 4. Browser-Diagnose — Konsolenblock (120 s)

Hard-Reload `mc-load-p4i-v1`, Top-Frame `/live-charts/research`, **keine Interaktion** (außer 1m→5m).

```javascript
(function MC_LOAD_P4J_RUN() {
  var D = window.__MC_LOAD_DIAG__;
  if (!D) {
    console.error("MC-LOAD-P4J: __MC_LOAD_DIAG__ fehlt. Hard-Reload mc-load-p4i-v1.");
    return;
  }
  D.enable();
  var loadId = D.start({
    symbol: "SOLUSDT",
    timeframe: "1m",
    test: "p4j-final-reset-120s-no-interaction",
  });
  window.__MC_LOAD_P4J__ = {
    loadId: loadId,
    samples: [],
    startedWallMs: Date.now(),
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
    if (api && api.isViewManual) {
      try { di.viewManualApi = api.isViewManual(); } catch (e2) { /* ignore */ }
    }
    di.barsAfterComputed = barsAfterFromDebug(di);
    var row = {
      tag: tag,
      wall_ms: Date.now(),
      t_rel_ms: Date.now() - window.__MC_LOAD_P4J__.startedWallMs,
      debugInfo: di,
    };
    window.__MC_LOAD_P4J__.samples.push(row);
    D.emit("p4j_viewport_sample", row);
    return row;
  }
  sample("start_before_tf");
  console.info("MC-LOAD-P4J: SOLUSDT, 1m→5m, 120s ohne Pan/Zoom/Resize.");
  window.__MC_LOAD_P4J__.interval = setInterval(function () { sample("periodic"); }, 2000);
  setTimeout(function () {
    clearInterval(window.__MC_LOAD_P4J__.interval);
    sample("t120_end");
    D.stop({ test: "p4j", p4j_samples: window.__MC_LOAD_P4J__.samples });
    var full = D.exportJSON();
    var ring = full.event_ring || [];
    function pick(ev) {
      return ring.filter(function (e) { return e && e.event === ev; });
    }
    var bundle = {
      mc_load_p4j: true,
      load_id: loadId,
      diag: full,
      p4j_samples: window.__MC_LOAD_P4J__.samples,
      mc_load_final_view_reset: pick("mc_load_final_view_reset"),
      mc_load_final_view_reset_skipped: pick("mc_load_final_view_reset_skipped"),
      viewport_mutation: pick("viewport_mutation"),
      mc_load_phase_b_complete: pick("mc_load_phase_b_complete"),
      ema_series_viewport_snapshot: pick("ema_series_viewport_snapshot"),
      ema_catch_up_decision: pick("ema_catch_up_decision"),
    };
    try {
      var a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob([JSON.stringify(bundle, null, 2)], { type: "application/json" }));
      a.download = "MC_LOAD_P4J_" + loadId + ".json";
      a.click();
    } catch (e3) {}
    console.log("MC-LOAD-P4J DONE", {
      loadId: loadId,
      events: ring.length,
      final_reset: bundle.mc_load_final_view_reset.length,
      final_reset_skipped: bundle.mc_load_final_view_reset_skipped.length,
      viewport_mutations: bundle.viewport_mutation.length,
      phase_b: bundle.mc_load_phase_b_complete.length,
    });
    console.log("MC-LOAD-P4J bundle:", bundle);
  }, 120000);
  return { loadId: loadId };
})();
```

**Samples enthalten:** `resetViewCount`, `barSpacing`, `logical`, `visibleTime` / `visibleTimeApi`, `barsAfterComputed`, `viewManual` / `viewManualApi`, `emaOverlayIds`, `payloadCount`.

---

## 5. Erfolgskriterien (Browser)

- Phase A schnell; Phase B ~24 192; `ema_catch_up_decision` / Snapshots: nicht stale.
- **Genau ein** `mc_load_final_view_reset` (oder `skipped` bei manuellem View).
- `resetViewCount` springt einmal nach Phase B; danach 120 s stabil.
- Kein ungewollter `barSpacing`-Sprung; `viewport_mutation` ohne unerwartete Shrinks.
- Live/Forming nach Reset weiter ok.

---

## Nach Export

JSON bereitstellen → `MC_LOAD_P4J_BROWSER_PASS` oder Follow-up.