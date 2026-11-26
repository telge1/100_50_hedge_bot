/**
 * MC-LOAD-P1 / P1B — candle load E2E trace (debug only, ring buffer).
 * Enable: window.__MC_LOAD_DIAG__.start() or localStorage mc.load.diag=1
 * Host page: /live-charts/research (not the pane iframe — use bridge or top frame).
 */
(function (root) {
  "use strict";

  var SCHEMA = "mc_load_p4_v1";
  var ASSET_V = "mc-load-p4i-v1";
  var MAX_EVENTS = 400;
  var MAX_REQUESTS = 120;
  var MAX_RUNS = 12;

  var active = false;
  var loadId = null;
  var runT0 = null;
  var events = [];
  var requests = [];
  var runs = [];
  var currentRun = null;
  var seq = 0;

  function enabledViaStorage() {
    try {
      return localStorage.getItem("mc.load.diag") === "1";
    } catch (e) {
      return false;
    }
  }

  function isActive() {
    return active || enabledViaStorage();
  }

  function monotonicMs() {
    return typeof performance !== "undefined" && performance.now
      ? performance.now()
      : Date.now();
  }

  function newLoadId() {
    seq += 1;
    return "mcl-" + Date.now().toString(36) + "-" + seq;
  }

  function pushEvent(name, extra) {
    if (!isActive()) return;
    var rel = runT0 != null ? Math.round(monotonicMs() - runT0) : null;
    var row = Object.assign(
      { event: name, t_rel_ms: rel, wall_ms: Date.now() },
      extra || {}
    );
    events.push(row);
    if (events.length > MAX_EVENTS) events.shift();
    if (currentRun && currentRun.frontend_timings_ms) {
      var tKey = (extra && extra.timing_key) ? extra.timing_key : name;
      if (rel != null && currentRun.frontend_timings_ms[tKey] == null) {
        currentRun.frontend_timings_ms[tKey] = rel;
        currentRun.frontend_timings_ms[tKey + "_sec"] = Math.round(rel) / 1000;
      }
    }
  }

  function noteRequest(req) {
    if (!isActive()) return;
    var ep = req && req.endpoint ? String(req.endpoint).split("?")[0] : null;
    var row = Object.assign(
      {
        load_id: loadId,
        started_wall_ms: Date.now(),
        endpoint_key: ep,
      },
      req || {}
    );
    requests.push(row);
    if (requests.length > MAX_REQUESTS) requests.shift();
    if (currentRun) currentRun.requests.push(row);
  }

  function patchRequest(id, patch) {
    var i;
    for (i = requests.length - 1; i >= 0; i--) {
      if (requests[i].id === id) {
        Object.assign(requests[i], patch || {});
        break;
      }
    }
    if (currentRun) {
      for (i = currentRun.requests.length - 1; i >= 0; i--) {
        if (currentRun.requests[i].id === id) {
          Object.assign(currentRun.requests[i], patch || {});
          break;
        }
      }
    }
  }

  function snapshotSettings(extra) {
    var ws = extra && extra.workspace;
    return {
      active_pools: !!(extra && extra.active_pools),
      exact_origin_density: !!(extra && extra.exact_origin),
      orderbook_levels: !!(extra && extra.ob_levels),
      opi: !!(extra && extra.opi),
      liquidity_lld: !!(ws && ws.liquidity && ws.liquidity.enabled !== false),
      open_interest: !!(ws && ws.open_interest && ws.open_interest.visible),
      stochastic: !!(ws && ws.stochastic && ws.stochastic.visible),
    };
  }

  function finishRun(extra) {
    if (!currentRun) return null;
    Object.assign(currentRun.diagnosis, extra || {});
    currentRun.finished_wall_ms = Date.now();
    if (runT0 != null) {
      currentRun.total_trace_ms = Math.round(monotonicMs() - runT0);
    }
    runs.push(currentRun);
    if (runs.length > MAX_RUNS) runs.shift();
    var done = currentRun;
    currentRun = null;
    active = false;
    loadId = null;
    runT0 = null;
    return done;
  }

  function start(opts) {
    opts = opts || {};
    active = true;
    try {
      localStorage.setItem("mc.load.diag", "1");
    } catch (e) { /* ignore */ }
    loadId = opts.load_id || newLoadId();
    runT0 = monotonicMs();
    events = [];
    currentRun = {
      schema_version: SCHEMA,
      load_id: loadId,
      symbol: opts.symbol || null,
      timeframe: opts.timeframe || null,
      view: opts.view || null,
      settings: snapshotSettings(opts),
      started_wall_ms: Date.now(),
      frontend_timings_ms: {},
      requests: [],
      backend_timings_ms: opts.backend_timings_ms || {},
      candle_count: null,
      first_candle_visible_ms: null,
      loading_hidden_ms: null,
      render_verification: null,
      errors: [],
      diagnosis: {},
      tests: opts.test || null,
      effective_fast_first: null,
      fast_first_fallback_reason: null,
      phase_requested: null,
      phase_executed: null,
      phase_a_candle_count: null,
      phase_b_candle_count: null,
      phase_a_http_id: null,
      phase_b_http_id: null,
      symbol_at_request: opts.symbol || null,
      timeframe_at_request: opts.timeframe || null,
      load_generation: null,
      request_completion_status: null,
      phase_a_decision: null,
      phase_b_decision: null,
      phase_b_decision_resolve: null,
      phase_decisions: [],
      mc_load_p3b_code: null,
      mc_load_p4_code: ASSET_V,
    };
    pushEvent("load_started", { symbol: currentRun.symbol, timeframe: currentRun.timeframe });
    return loadId;
  }

  function stop(extra) {
    if (!active && !currentRun) return getLatest();
    pushEvent("trace_stop", extra || {});
    return finishRun(extra);
  }

  function getLatest() {
    if (currentRun) return currentRun;
    return runs.length ? runs[runs.length - 1] : null;
  }

  function exportJSON() {
    var payload = {
      schema_version: SCHEMA,
      exported_wall_ms: Date.now(),
      active: active,
      load_id: loadId,
      runs: runs.slice(),
      latest: getLatest(),
      event_ring: events.slice(),
    };
    var blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "MC_LOAD_P1_" + (loadId || "export") + ".json";
    a.click();
    setTimeout(function () {
      URL.revokeObjectURL(a.href);
    }, 2000);
    return payload;
  }

  function markRenderProxy(kind) {
    pushEvent("render_proxy_scheduled", {
      render_proxy: kind || "double_raf",
      timing_key: "render_proxy_scheduled",
      note: "not_guaranteed_pixels_on_screen",
    });
    pushEvent("first_candles_rendered", {
      render_proxy: kind || "double_raf",
      timing_key: "first_candles_rendered",
      visibility: "proxy_only",
    });
    if (currentRun) {
      currentRun.render_verification = kind || "render_proxy";
      if (currentRun.frontend_timings_ms.first_candles_rendered != null) {
        currentRun.first_candle_visible_ms = currentRun.frontend_timings_ms.first_candles_rendered;
        currentRun.first_candle_visible_sec =
          Math.round(currentRun.first_candle_visible_ms) / 1000;
      }
    }
  }

  function noteError(err, extra) {
    var row = {
      message: err && err.message ? String(err.message) : String(err || "error"),
      name: err && err.name ? String(err.name) : null,
      stack: err && err.stack ? String(err.stack).slice(0, 1200) : null,
    };
    if (extra) Object.assign(row, extra);
    pushEvent("load_error", row);
    if (currentRun && currentRun.errors) currentRun.errors.push(row);
  }

  function whereAmI() {
    var host = false;
    var iframe = false;
    try {
      host = root === root.top;
    } catch (e) { /* cross-origin */ }
    try {
      iframe = root.frameElement != null;
    } catch (e2) { /* ignore */ }
    return {
      asset_v: ASSET_V,
      host_page: !!host,
      in_iframe: !!iframe,
      hint: host
        ? "DevTools context: top — OK"
        : "Diag runs on host — use top frame or __MC_LOAD_DIAG__.help()",
    };
  }

  function publishDiag(api) {
    root.__MC_LOAD_DIAG__ = api;
    var seen = [root];
    function tryAssign(w) {
      if (!w || seen.indexOf(w) >= 0) return;
      seen.push(w);
      try {
        if (!w.__MC_LOAD_DIAG__) w.__MC_LOAD_DIAG__ = api;
      } catch (e) { /* cross-origin */ }
    }
    try { tryAssign(root.top); } catch (e0) { /* ignore */ }
    try { tryAssign(root.parent); } catch (e1) { /* ignore */ }
  }

  var api = {
    SCHEMA: SCHEMA,
    ASSET_V: ASSET_V,
    start: start,
    stop: stop,
    exportJSON: exportJSON,
    getLatest: getLatest,
    isActive: isActive,
    emit: pushEvent,
    noteRequest: noteRequest,
    patchRequest: patchRequest,
    markRenderProxy: markRenderProxy,
    noteError: noteError,
    snapshotSettings: snapshotSettings,
    where: whereAmI,
    help: function () {
      try {
        root.console.info(
          "[MC-LOAD-P1B] __MC_LOAD_DIAG__ on research host window.",
          "start({symbol,timeframe}) → change symbol/tf → stop() → exportJSON()",
          whereAmI()
        );
      } catch (e) { /* ignore */ }
      return whereAmI();
    },
    _runs: function () { return runs.slice(); },
    enable: function () {
      try { localStorage.setItem("mc.load.diag", "1"); } catch (e) { /* ignore */ }
      return true;
    },
    disable: function () {
      try { localStorage.removeItem("mc.load.diag"); } catch (e) { /* ignore */ }
      active = false;
      return true;
    },
  };

  publishDiag(api);

  try {
    root.addEventListener("DOMContentLoaded", function () {
      try {
        root.console.info("[MC-LOAD-P1B] __MC_LOAD_DIAG__ ready", ASSET_V, whereAmI());
      } catch (e) { /* ignore */ }
    });
  } catch (e2) { /* ignore */ }
})(typeof window !== "undefined" ? window : globalThis);