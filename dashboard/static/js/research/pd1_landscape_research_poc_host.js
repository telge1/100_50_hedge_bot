/**
 * PD1 Landscape × Research TRP chart.js — isolated POC host (no canvas replay).
 */
(function () {
  "use strict";

  const POC = {
    symbol: "DOGEUSDT",
    timeframe: "15m",
    poolDetail: 1,
    gotoDefault: "2026-09-15 01:45:00 UTC",
  };
  const API_BASE = "/live-charts/viewer/";
  const VIEW_PAD_SEC = 4 * 3600;
  const ASSET_V = "phase6xt_reuse_research_poc_v1";

  const statusEl = document.getElementById("pocStatus");
  const iframe = document.getElementById("pocChartFrame");
  const symSel = document.getElementById("pocSymbol");
  const tfSel = document.getElementById("pocTimeframe");

  let pane = { ready: false, overlayIds: {}, pendingData: null, pendingZones: [] };
  let yMin = 0;
  let yMax = 1;

  function setStatus(msg, kind) {
    if (!statusEl) return;
    statusEl.textContent = msg;
    statusEl.dataset.kind = kind || "info";
  }

  function chartApi() {
    const w = iframe && iframe.contentWindow;
    return w && w.chartApi ? w.chartApi : null;
  }

  function attachBridge() {
    const w = iframe.contentWindow;
    if (!w) return;
    w.bridge = {
      on_chart_ready: function () {
        pane.ready = true;
        const chart = chartApi();
        if (!chart) return;
        if (pane.pendingData) chart.setData(pane.pendingData);
        syncZones();
        chart.resize();
        if (pane.focusTs) focusTime(pane.focusTs);
      },
      on_visible_range: function () {
        layoutZones();
      },
    };
  }

  function isoFromMs(ms) {
    return new Date(ms).toISOString().replace(".000", "");
  }

  async function fetchWindow(fromMs, toMs) {
    const res = await fetch(API_BASE + "api/window", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ from_utc: isoFromMs(fromMs), to_utc: isoFromMs(toMs) }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.message || data.detail || "window " + res.status);
    return data;
  }

  function poolYExtents(pools) {
    if (!pools || !pools.length) return { yMin: 0, yMax: 1 };
    return {
      yMin: Math.min(...pools.map((p) => p.bottom)),
      yMax: Math.max(...pools.map((p) => p.top)),
    };
  }

  function applyPayload(data, focusSec) {
    const AD = window.PD1ExactDensityTrpAdapter;
    if (!AD) throw new Error("density adapter missing");
    const candles = AD.landscapePricesToCandles(data.prices);
    const ext = poolYExtents(data.pools);
    yMin = ext.yMin * 0.9995;
    yMax = ext.yMax * 1.0005;
    const t0 = candles.length ? candles[0].time * 1000 : Date.now();
    const t1 = candles.length ? candles[candles.length - 1].time * 1000 : t0;
    pane.pendingZones = AD.poolsToExactOriginZones(data.pools, t0, t1 + 5 * 60 * 1000, yMin, yMax);
    pane.pendingData = { candles: candles, is_demo: false };
    pane.focusTs = focusSec;
    const chart = chartApi();
    if (chart) {
      chart.setData(pane.pendingData);
      syncZones();
      if (focusSec != null) focusTime(focusSec);
    }
    setStatus(
      "Ready · " + candles.length + " candles · " + pane.pendingZones.length + " density zones · Phase5S",
      "ok"
    );
  }

  function syncZones() {
    const chart = chartApi();
    if (!chart) return;
    Object.keys(pane.overlayIds).forEach((id) => {
      if (!pane.pendingZones.find((z) => z.id === id)) chart.removeOverlay(id);
    });
    pane.overlayIds = {};
    pane.pendingZones.forEach((z) => {
      chart.addOverlay(z);
      pane.overlayIds[z.id] = true;
    });
    if (chart.layoutOverlays) chart.layoutOverlays();
  }

  function layoutZones() {
    const chart = chartApi();
    if (chart && chart.layoutOverlays) chart.layoutOverlays();
  }

  function focusTime(tsSec) {
    const chart = chartApi();
    if (!chart) return;
    const pad = VIEW_PAD_SEC;
    if (chart.focusOnTime && chart.focusOnTime(tsSec, pad)) return;
    if (chart.setVisibleTimeRange) chart.setVisibleTimeRange(tsSec - pad, tsSec + pad);
  }

  function unsupportedSelection() {
    const sym = symSel ? symSel.value : POC.symbol;
    const tf = tfSel ? tfSel.value : POC.timeframe;
    return sym !== POC.symbol || tf !== POC.timeframe;
  }

  async function goTo() {
    if (unsupportedSelection()) {
      setStatus("POC nur DOGEUSDT 15m PD1 — andere Auswahl nicht freigeschaltet", "error");
      return;
    }
    const raw = (document.getElementById("pocGoTo") || {}).value || POC.gotoDefault;
    const ts =
      typeof ResearchGotoTime !== "undefined" && ResearchGotoTime.parseGotoUtcToUnix
        ? ResearchGotoTime.parseGotoUtcToUnix(raw)
        : null;
    if (ts == null) {
      setStatus("GO TO: ungültiges UTC-Format", "error");
      return;
    }
    const win =
      typeof ResearchGotoTime !== "undefined" && ResearchGotoTime.gotoLoadWindow
        ? ResearchGotoTime.gotoLoadWindow(ts)
        : { from: ts - VIEW_PAD_SEC, to: ts + VIEW_PAD_SEC };
    setStatus("Lade historisches Fenster …", "info");
    try {
      const data = await fetchWindow(win.from * 1000, win.to * 1000);
      applyPayload(data, ts);
    } catch (e) {
      setStatus("GO TO fehlgeschlagen: " + (e.message || e), "error");
    }
  }

  function initControls() {
    if (symSel) {
      symSel.innerHTML = '<option value="DOGEUSDT" selected>DOGEUSDT</option><option value="OTHER" disabled>andere (nicht POC)</option>';
    }
    if (tfSel) {
      tfSel.innerHTML = '<option value="15m" selected>15m</option><option value="5m" disabled>5m (nicht POC)</option>';
    }
    const gt = document.getElementById("pocGoTo");
    if (gt && !gt.value) gt.value = "2026-09-15 01:45:00 UTC";
    document.getElementById("pocGoBtn")?.addEventListener("click", goTo);
    document.getElementById("pocReset")?.addEventListener("click", () => {
      const c = chartApi();
      if (c && c.resetView) c.resetView();
    });
    document.getElementById("pocFs")?.addEventListener("click", () => {
      const el = document.getElementById("pocPane");
      if (!el) return;
      if (!document.fullscreenElement) el.requestFullscreen?.();
      else document.exitFullscreen?.();
    });
    symSel?.addEventListener("change", () => {
      if (unsupportedSelection()) setStatus("Nur DOGEUSDT 15m unterstützt", "warn");
    });
    tfSel?.addEventListener("change", () => {
      if (unsupportedSelection()) setStatus("Nur DOGEUSDT 15m unterstützt", "warn");
    });
  }

  function boot() {
    initControls();
    if (!iframe) return;
    iframe.addEventListener("load", attachBridge);
    iframe.src = "/static/research_trp/pane.html?v=" + ASSET_V;
    setStatus("POC: TRP chart.js lädt …", "info");
    goTo();
  }

  window.PD1_LANDSCAPE_RESEARCH_POC = {
    POC: POC,
    goTo: goTo,
    chartApi: chartApi,
    pane: pane,
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
