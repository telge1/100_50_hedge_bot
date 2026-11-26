/**
 * MC-POOL-P2 — paste into DevTools Console on /live-charts/research (TOP frame).
 * READ-ONLY harness: no pool/trading/state mutation except trace localStorage flags.
 * Stop: __MC_POOL_P2__.stop(); Export: __MC_POOL_P2__.exportJSON()
 */
(function () {
  "use strict";
  if (window.__MC_POOL_P2__) {
    console.warn("[MC-POOL-P2] already installed — call __MC_POOL_P2__.reset() first");
    return window.__MC_POOL_P2__;
  }

  var SCHEMA = "mc_pool_p2_v1";
  var seq = 0;
  var t0 = performance.now();
  var active = false;
  var sections = [];
  var currentSection = null;
  var windowRequests = [];
  var viewportSamples = [];
  var longTasks = [];
  var resourceMarks = [];
  var fetchPatched = false;
  var origFetch = null;

  function mono() {
    return Math.round(performance.now() - t0);
  }

  function bumpSection(name, extra) {
    if (!currentSection) return;
    currentSection.events.push(
      Object.assign({ t_rel_ms: mono(), event: name }, extra || {})
    );
  }

  function startSection(id, label) {
    if (currentSection) {
      currentSection.ended_ms = mono();
      sections.push(currentSection);
    }
    currentSection = {
      id: id,
      label: label,
      started_ms: mono(),
      ended_ms: null,
      events: [],
      window_http_count_start: windowRequests.length,
      window_http_count_end: null,
    };
    bumpSection("section_start");
    console.log("[MC-POOL-P2] ▶", id, "—", label);
    return currentSection;
  }

  function endSection(extra) {
    if (!currentSection) return null;
    currentSection.window_http_count_end = windowRequests.length;
    currentSection.window_http_delta =
      currentSection.window_http_count_end - currentSection.window_http_count_start;
    Object.assign(currentSection, extra || {});
    currentSection.ended_ms = mono();
    sections.push(currentSection);
    var done = currentSection;
    currentSection = null;
    console.log(
      "[MC-POOL-P2] ■",
      done.id,
      "window_POST_delta=" + done.window_http_delta,
      "duration_ms=" + (done.ended_ms - done.started_ms)
    );
    return done;
  }

  function parseWindowResponse(data, meta) {
    if (!data || typeof data !== "object") return;
    var prov = data.provenance || {};
    var rec = {
      t_rel_ms: mono(),
      client_ms: meta.client_ms,
      transferSize: meta.transferSize,
      encodedBodySize: meta.encodedBodySize,
      decodedBodySize: meta.decodedBodySize,
      pools_n: Array.isArray(data.pools) ? data.pools.length : null,
      pool_count_meta: data.meta && data.meta.pool_count,
      timing_ms: data.timing_ms || null,
      response_cache: data.response_cache || null,
      phase5s_batch_reader_active: prov.phase5s_batch_reader_active,
      membership_loader: prov.membership_loader,
      phase5s_batch_stats: prov.phase5s_batch_stats || null,
      request_id: data.request_id || null,
      client_trace_id: data.client_trace_id || null,
      from_utc: (data.window && data.window.from_utc) || null,
      to_utc: (data.window && data.window.to_utc) || null,
    };
    windowRequests.push(rec);
    bumpSection("window_http_completed", rec);
  }

  function patchFetch() {
    if (fetchPatched || !window.fetch) return;
    origFetch = window.fetch.bind(window);
    window.fetch = function (input, init) {
      var url = typeof input === "string" ? input : input && input.url ? input.url : "";
      var isWindow =
        url.indexOf("/live-charts/viewer/api/window") >= 0;
      var tReq = performance.now();
      if (isWindow && active) {
        bumpSection("window_http_started", { url: url.split("?")[0] });
      }
      return origFetch(input, init).then(function (res) {
        if (!isWindow || !active) return res;
        var clone = res.clone();
        var client_ms = Math.round(performance.now() - tReq);
        clone
          .json()
          .then(function (data) {
            parseWindowResponse(data, {
              client_ms: client_ms,
              status: res.status,
              transferSize: null,
              encodedBodySize: null,
              decodedBodySize: null,
            });
          })
          .catch(function () {});
        return res;
      });
    };
    fetchPatched = true;
  }

  function unpatchFetch() {
    if (fetchPatched && origFetch) window.fetch = origFetch;
    fetchPatched = false;
  }

  function sampleViewport(tag) {
    var out = {
      t_rel_ms: mono(),
      tag: tag || "sample",
      exact_origin_hint: null,
      last_metrics: null,
      canvas_draw: null,
      segment_cache: null,
      chunk_stores: null,
    };
    var hint = document.getElementById("researchExactOriginDensityHint");
    if (hint && !hint.hidden) out.exact_origin_hint = hint.textContent;
    if (window.PD1ExactOriginResearch && PD1ExactOriginResearch.getLastMetrics) {
      out.last_metrics = PD1ExactOriginResearch.getLastMetrics();
    }
    var iframe = document.querySelector(".trp-pane iframe, iframe.trp-pane-frame, iframe");
    try {
      var cw = iframe && iframe.contentWindow;
      if (cw && cw.chartApi && cw.chartApi.getExactOriginDensityDrawStats) {
        out.canvas_draw = cw.chartApi.getExactOriginDensityDrawStats();
      }
      if (cw && cw.chartApi && cw.chartApi.getChart) {
        var ch = cw.chartApi.getChart();
        if (ch && ch.timeScale) {
          var vr = ch.timeScale().getVisibleRange();
          out.visible_range = vr;
        }
      }
    } catch (e) {
      out.iframe_error = String(e);
    }
    viewportSamples.push(out);
    return out;
  }

  function installObservers() {
    try {
      if (window.PerformanceObserver) {
        var po = new PerformanceObserver(function (list) {
          if (!active) return;
          list.getEntries().forEach(function (e) {
            if (e.entryType === "longtask") {
              longTasks.push({
                t_rel_ms: mono(),
                duration: Math.round(e.duration),
                name: e.name,
              });
            }
            if (e.entryType === "resource" && String(e.name).indexOf("/viewer/api/window") >= 0) {
              resourceMarks.push({
                t_rel_ms: mono(),
                name: e.name,
                duration: Math.round(e.duration),
                transferSize: e.transferSize,
                encodedBodySize: e.encodedBodySize,
                decodedBodySize: e.decodedBodySize,
              });
            }
          });
        });
        try {
          po.observe({ type: "longtask", buffered: true });
        } catch (e1) {}
        try {
          po.observe({ type: "resource", buffered: true });
        } catch (e2) {}
      }
    } catch (e) {}
  }

  function snapshotSettings() {
    var eo = document.getElementById("researchIndExactOriginDensity");
    var hist = document.getElementById("researchIndHistoricalLmv2Pools");
    var poolTf =
      document.querySelector('input[name="researchExactOriginPoolTimeframe"]:checked') || {};
    var pd1 = document.querySelector('input[name="researchExactOriginPoolDetail"][value="1"]');
    return {
      symbol: (document.getElementById("researchSymbol") || {}).value || null,
      chart_tf: (document.getElementById("researchTimeframe") || {}).value || null,
      exact_origin_checked: !!(eo && eo.checked),
      historical_lmv2_checked: !!(hist && hist.checked),
      pool_timeframe: poolTf.value || null,
      pool_detail_pd1_checked: !!(pd1 && pd1.checked),
      canvas_path:
        window.PD1ExactOriginResearch && PD1ExactOriginResearch.isCanvasPathEnabled
          ? !!PD1ExactOriginResearch.isCanvasPathEnabled()
          : null,
      mc_load_diag: !!(window.__MC_LOAD_DIAG__ && __MC_LOAD_DIAG__.isActive && __MC_LOAD_DIAG__.isActive()),
      doge_trace: !!(window.DogeExactOriginTrace && DogeExactOriginTrace.enabled()),
    };
  }

  function exportJSON() {
    if (currentSection) endSection({ auto_closed: true });
    var payload = {
      schema_version: SCHEMA,
      exported_wall_ms: Date.now(),
      symbol_target: "SOLUSDT",
      chart_target: "5m",
      settings_at_export: snapshotSettings(),
      sections: sections,
      window_requests: windowRequests,
      viewport_samples: viewportSamples,
      long_tasks: longTasks,
      resource_window_marks: resourceMarks,
      analysis_hint: {
        pan_inside_tile_expect_window_post_delta_0: "CONFIRMED design goal — verify sections C/D/E",
        batch_reader_field: "provenance.phase5s_batch_reader_active + phase5s_batch_stats",
        server_cache_field: "response_cache.hit",
        ch_queries_field: "provenance.phase5s_batch_stats.clickhouse_query_count OR timing_ms",
      },
    };
    var ts = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
    var name = "MC_POOL_P2_SOLUSDT_5M_" + ts + ".json";
    var blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    a.click();
    setTimeout(function () {
      URL.revokeObjectURL(a.href);
    }, 3000);
    console.log("[MC-POOL-P2] exported", name);
    return payload;
  }

  function reset() {
    stop({ reset: true });
    sections = [];
    windowRequests = [];
    viewportSamples = [];
    longTasks = [];
    resourceMarks = [];
    t0 = performance.now();
    seq = 0;
  }

  function start(opts) {
    opts = opts || {};
    reset();
    active = true;
    try {
      localStorage.setItem("DOGE_EXACT_ORIGIN_TRACE", "1");
    } catch (e) {}
    if (window.DogeExactOriginTrace && DogeExactOriginTrace.activationHelp) {
      DogeExactOriginTrace.activationHelp();
    }
    patchFetch();
    installObservers();
    bumpSection("measure_start", snapshotSettings());
    console.log("[MC-POOL-P2] started — settings:", snapshotSettings());
    if (opts.auto_section_a) startSection("A", "baseline_after_pools_on");
    return true;
  }

  function stop(extra) {
    active = false;
    unpatchFetch();
    bumpSection("measure_stop", extra || {});
    console.log("[MC-POOL-P2] stopped");
  }

  var api = {
    SCHEMA: SCHEMA,
    start: start,
    stop: stop,
    reset: reset,
    exportJSON: exportJSON,
    startSection: startSection,
    endSection: endSection,
    sampleViewport: sampleViewport,
    bump: bumpSection,
    snapshotSettings: snapshotSettings,
    getWindowRequests: function () {
      return windowRequests.slice();
    },
    help: function () {
      console.log(
        "[MC-POOL-P2] start() → run user steps → startSection/endSection → exportJSON()",
        "Trace: DogeExactOriginTrace (console TRACE | …)",
        "Optional: __MC_LOAD_DIAG__.start({symbol:'SOLUSDT',timeframe:'5m'}) for candle HTTP (separate)"
      );
    },
  };

  window.__MC_POOL_P2__ = api;
  console.log("[MC-POOL-P2] ready — __MC_POOL_P2__.help()");
  return api;
})();