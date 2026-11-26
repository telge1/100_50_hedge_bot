/**
 * MC-OPI-P3L — E2E visibility recorder (host). Default OFF; no OPI sync/fetch side effects.
 */
(function (root) {
  "use strict";

  var V = "research-mc-opi-p3s-v1";
  var MAX_EVENTS = 1800;
  var MAX_MISMATCH = 48;
  var MAX_DESYNC = 48;
  var SAMPLE_MS = 2000;

  var active = false;
  var startedAt = null;
  var stoppedAt = null;
  var events = [];
  var mismatchRing = [];
  var desyncRing = [];
  var sampleTimer = 0;
  var reqSeq = 0;

  function nowIso() {
    return new Date().toISOString();
  }

  function push(ev) {
    if (!active) return;
    events.push(Object.assign({ t: nowIso() }, ev));
    while (events.length > MAX_EVENTS) events.shift();
  }

  function hostEvent(kind, payload) {
    push({ layer: "host", kind: kind, data: payload || {} });
  }

  function pane0() {
    try {
      return root.__researchDebug && root.__researchDebug.panes
        ? root.__researchDebug.panes["pane-0"]
        : null;
    } catch (e) {
      return null;
    }
  }

  function iframeWin() {
    try {
      var p = pane0();
      return p && p.iframe ? p.iframe.contentWindow : null;
    } catch (e) {
      return null;
    }
  }

  function summarizePane(pane) {
    if (!pane) return null;
    var store = pane.opiDisplayStore;
  return {
      opiGen: pane.opiGen,
      opiInflight: !!pane.opiInflight,
      opiPending: !!pane.opiPending,
      opiRetryCount: pane.opiRetryCount || 0,
      displayStoreSize: store ? store.size : 0,
      displayStoreCtx: pane.opiDisplayStoreCtx || null,
      lastAppliedBars: pane.opiLastAppliedBars ? pane.opiLastAppliedBars.length : 0,
      opiDiagUpdateId: pane.opiDiagUpdateId || null,
    };
  }

  function sampleOnce() {
    if (!active) return;
    var pane = pane0();
    var win = iframeWin();
    var st = root.__researchDebug || {};
    var row = {
      layer: "sample",
      kind: "periodic",
      symbol: st.symbol || null,
      tf: pane && pane.tf ? pane.tf : null,
      opiWindow: st.opi && st.opi.window_minutes != null ? st.opi.window_minutes : null,
      pane: summarizePane(pane),
      mainVisible: null,
      iframe: null,
      stoch: null,
      visibility: null,
      timeAxis: null,
    };
    try {
      var chart = pane && pane.iframeApi;
      if (chart && chart.getVisibleTimeRange) {
        row.mainVisible = chart.getVisibleTimeRange();
      }
    } catch (e0) { /* ignore */ }
    try {
      if (win && win.OpiPane && win.OpiPane.measureVisibleOpiCoverage) {
        row.visibility = win.OpiPane.measureVisibleOpiCoverage();
      }
      if (win && win.OpiPane && win.OpiPane.measureTimeAxisDesync) {
        row.timeAxis = win.OpiPane.measureTimeAxisDesync();
      }
      if (win && win.OpiPane && win.OpiPane.debugTraceState) {
        row.iframe = win.OpiPane.debugTraceState();
      }
      if (win && win.OpiE2eRecord && win.OpiE2eRecord.drain) {
        var drained = win.OpiE2eRecord.drain();
        if (drained && drained.length) {
          drained.forEach(function (ev) {
            push({ layer: "iframe", kind: ev.kind || "iframe", data: ev });
          });
        }
      }
      if (win && win.chartApi && win.chartApi.stochLogicalSnapshot) {
        row.stoch = win.chartApi.stochLogicalSnapshot();
      }
    } catch (e1) { /* ignore */ }
    row.documentHidden = !!(root.document && root.document.hidden);
    push(row);
    if (row.visibility && row.visibility.mismatch) {
      captureMismatch(row.visibility);
    }
    if (row.timeAxis && row.timeAxis.desync) {
      captureTimeAxisDesync(row.timeAxis);
    }
  }

  function captureTimeAxisDesync(ta) {
    var snap = {
      t: nowIso(),
      code: ta.code || "OPI_TIME_AXIS_DESYNC",
      timeAxis: ta,
      visibility: null,
      pane: summarizePane(pane0()),
    };
    try {
      var win = iframeWin();
      if (win && win.OpiPane && win.OpiPane.measureVisibleOpiCoverage) {
        snap.visibility = win.OpiPane.measureVisibleOpiCoverage();
      }
    } catch (e0) { /* ignore */ }
    desyncRing.push(snap);
    while (desyncRing.length > MAX_DESYNC) desyncRing.shift();
    push({ layer: "alert", kind: "OPI_TIME_AXIS_DESYNC", data: snap });
  }

  function captureMismatch(vis) {
    var snap = {
      t: nowIso(),
      classification: vis.classification,
      code: vis.mismatchCode || "OPI_VISIBILITY_MISMATCH",
      vis: vis,
      pane: summarizePane(pane0()),
    };
    try {
      var win = iframeWin();
      if (win && win.OpiPane && win.OpiPane.captureOpiCanvasThumb) {
        snap.thumb = win.OpiPane.captureOpiCanvasThumb();
      }
    } catch (e) { /* ignore */ }
    mismatchRing.push(snap);
    while (mismatchRing.length > MAX_MISMATCH) mismatchRing.shift();
    push({ layer: "alert", kind: "OPI_VISIBILITY_MISMATCH", data: snap });
  }

  function buildExport() {
    var pane = pane0();
    return {
      schema: "mc-opi-p3l-e2e-v1",
      version: V,
      startedAt: startedAt,
      stoppedAt: stoppedAt || nowIso(),
      active: active,
      assetV: root.document && root.document.querySelector
        ? (function () {
          var s = root.document.querySelector('script[src*="research_charts.js"]');
          return s && s.src ? s.src.split("?v=")[1] || s.src : null;
        }())
        : null,
      userAgent: root.navigator ? root.navigator.userAgent : null,
      symbol: root.__researchDebug && root.__researchDebug.symbol,
      view: root.__researchDebug && root.__researchDebug.viewMode,
      opi: root.__researchDebug && root.__researchDebug.opi,
      paneSummary: summarizePane(pane),
      eventCount: events.length,
      mismatchCount: mismatchRing.length,
      timeAxisDesyncCount: desyncRing.length,
      events: events.slice(),
      mismatches: mismatchRing.slice(),
      timeAxisDesyncs: desyncRing.slice(),
    };
  }

  function downloadJson() {
    var blob = new Blob([JSON.stringify(buildExport(), null, 2)], { type: "application/json" });
    var a = root.document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "opi-e2e-" + (startedAt || "export").replace(/[:]/g, "-") + ".json";
    a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 5000);
  }

  function ensureUi() {
    var host = root.document.getElementById("opiE2eDiagBar");
    if (host) return host;
    var bar = root.document.createElement("div");
    bar.id = "opiE2eDiagBar";
    bar.className = "research-opi-e2e-bar";
    bar.hidden = true;
    bar.innerHTML =
      '<span class="research-opi-e2e-label">OPI E2E Diagnose</span>' +
      '<button type="button" id="opiE2eStartBtn" class="trp-btn">Start</button>' +
      '<button type="button" id="opiE2eStopBtn" class="trp-btn" disabled>Stop</button>' +
      '<button type="button" id="opiE2eExportBtn" class="trp-btn" disabled>Export JSON</button>' +
      '<span id="opiE2eStatus" class="trp-muted">inaktiv</span>';
    var shell = root.document.querySelector(".research-trp-shell");
    if (shell && shell.firstChild) shell.insertBefore(bar, shell.firstChild);
    else root.document.body.appendChild(bar);
    root.document.getElementById("opiE2eStartBtn").addEventListener("click", function () {
      root.OpiE2eDiag.start();
    });
    root.document.getElementById("opiE2eStopBtn").addEventListener("click", function () {
      root.OpiE2eDiag.stop();
    });
    root.document.getElementById("opiE2eExportBtn").addEventListener("click", function () {
      root.OpiE2eDiag.export();
    });
    return bar;
  }

  function isSingleChartView() {
    try {
      var params = new URLSearchParams(root.location.search || "");
      var q = params.get("view");
      if (q === "multi") return false;
      if (q === "single") return true;
    } catch (e0) { /* ignore */ }
    var st = root.__researchDebug;
    if (st && st.multiManual === true) return false;
    if (st && st.multiManual === false) return true;
    if (root.document.body && root.document.body.classList.contains("research-multi-manual")) return false;
    return true;
  }

  function updateUi() {
    var bar = ensureUi();
    var st = root.document.getElementById("opiE2eStatus");
    var bStart = root.document.getElementById("opiE2eStartBtn");
    var bStop = root.document.getElementById("opiE2eStopBtn");
    var bExp = root.document.getElementById("opiE2eExportBtn");
    var single = isSingleChartView();
    bar.hidden = !single;
    if (bStart) bStart.disabled = active;
    if (bStop) bStop.disabled = !active;
    if (bExp) bExp.disabled = events.length < 1;
    if (st) {
      st.textContent = active
        ? "läuft · Events " + events.length + " · Mismatch " + mismatchRing.length
          + " · Desync " + desyncRing.length
        : (events.length ? "gestoppt · " + events.length + " Events" : "inaktiv");
    }
  }

  root.bootTrace = root.bootTrace || function bootTrace(name, fields) {
    if (active) hostEvent("bootTrace", { name: name, fields: fields || {} });
  };

  root.OpiE2eDiag = {
    version: V,
    get active() { return active; },
    refreshUi: updateUi,
    isSingleChartView: isSingleChartView,
    hostEvent: hostEvent,
    nextRequestId: function () {
      reqSeq += 1;
      return reqSeq;
    },
    start: function () {
      if (active) return false;
      events.length = 0;
      mismatchRing.length = 0;
      desyncRing.length = 0;
      active = true;
      startedAt = nowIso();
      stoppedAt = null;
      try {
        if (root.OpiRuntimeTrace && root.OpiRuntimeTrace.enable) root.OpiRuntimeTrace.enable();
        if (root.OpiViewportDiag && root.OpiViewportDiag.enable) root.OpiViewportDiag.enable();
      } catch (e0) { /* ignore */ }
      try {
        var win = iframeWin();
        if (win && win.OpiE2eRecord && win.OpiE2eRecord.start) win.OpiE2eRecord.start();
      } catch (e1) { /* ignore */ }
      hostEvent("e2e_start", { version: V });
      sampleOnce();
      sampleTimer = root.setInterval(sampleOnce, SAMPLE_MS);
      updateUi();
      try { root.console.info("[OPI E2E] recording started", V); } catch (e2) { /* ignore */ }
      return true;
    },
    stop: function () {
      if (!active) return false;
      active = false;
      stoppedAt = nowIso();
      if (sampleTimer) {
        root.clearInterval(sampleTimer);
        sampleTimer = 0;
      }
      sampleOnce();
      try {
        var win = iframeWin();
        if (win && win.OpiE2eRecord && win.OpiE2eRecord.stop) win.OpiE2eRecord.stop();
      } catch (e0) { /* ignore */ }
      hostEvent("e2e_stop", {
        eventCount: events.length,
        mismatchCount: mismatchRing.length,
        timeAxisDesyncCount: desyncRing.length,
      });
      updateUi();
      return true;
    },
    export: downloadJson,
    dump: buildExport,
    status: function () {
      return {
        active: active,
        startedAt: startedAt,
        events: events.length,
        mismatches: mismatchRing.length,
        timeAxisDesyncs: desyncRing.length,
        version: V,
      };
    },
  };

  root.addEventListener("DOMContentLoaded", function () {
    ensureUi();
    updateUi();
  });
})(typeof window !== "undefined" ? window : globalThis);