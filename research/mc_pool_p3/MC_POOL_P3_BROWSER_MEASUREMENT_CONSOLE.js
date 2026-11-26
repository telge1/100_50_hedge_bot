/**
 * MC-POOL-P3 — temporary DevTools harness for /live-charts/research (TOP frame).
 * Fixes P2 observer/fetch/timing issues. Does not modify dashboard sources.
 *
 * Usage:
 *   __MC_POOL_P3__.start({ parseWindowBodies: false })
 *   __MC_POOL_P3__.startSection('C','slow_pan') … __MC_POOL_P3__.endSection()
 *   __MC_POOL_P3__.sampleViewport('after_C')
 *   __MC_POOL_P3__.exportJSON()
 *   __MC_POOL_P3__.stop()
 */
(function () {
  "use strict";
  if (window.__MC_POOL_P3__) {
    console.warn("[MC-POOL-P3] already installed — __MC_POOL_P3__.stop() then paste again, or use .reset()");
    return window.__MC_POOL_P3__;
  }

  var SCHEMA = "mc_pool_p3_v1";
  var tOrigin = performance.timeOrigin;
  var t0 = performance.now();
  var active = false;
  var parseBodies = false;

  var sections = [];
  var currentSection = null;
  var httpLog = [];
  var refreshRuns = [];
  var viewportSchedules = [];
  var viewportSamples = [];
  var longTasksDedup = Object.create(null);
  var longTasks = [];
  var frameTimes = [];
  var traceLines = [];
  var coalesceMarks = [];

  var poLong = null;
  var poResource = null;
  var rafLoopId = null;
  var fetchPatched = false;
  var origFetch = null;
  var wrapsInstalled = false;
  var origRefreshPane = null;
  var origScheduleViewport = null;
  var origConsoleLog = null;

  function relMs(perfNow) {
    return Math.round(perfNow - t0);
  }

  function entryRelMs(entry) {
    if (entry && Number.isFinite(entry.startTime)) {
      return Math.round(entry.startTime + tOrigin - (t0 + tOrigin));
    }
    return relMs(performance.now());
  }

  function bumpSection(name, extra) {
    if (!currentSection) return;
    currentSection.events.push(Object.assign({ t_rel_ms: relMs(performance.now()), event: name }, extra || {}));
  }

  function startSection(id, label) {
    if (currentSection) {
      currentSection.ended_ms = relMs(performance.now());
      sections.push(currentSection);
    }
    currentSection = {
      id: id,
      label: label,
      started_ms: relMs(performance.now()),
      ended_ms: null,
      events: [],
      http_started_at: httpLog.length,
      http_completed_at: null,
    };
    bumpSection("section_start");
    console.log("[MC-POOL-P3] ▶", id, "—", label);
    return currentSection;
  }

  function endSection(extra) {
    if (!currentSection) return null;
    currentSection.http_completed_at = httpLog.length;
    currentSection.http_started = countHttpSince(currentSection.http_started_at, "started");
    currentSection.http_completed = countHttpSince(currentSection.http_started_at, "completed");
    currentSection.http_errors = countHttpSince(currentSection.http_started_at, "error");
    Object.assign(currentSection, extra || {});
    currentSection.ended_ms = relMs(performance.now());
    sections.push(currentSection);
    var done = currentSection;
    currentSection = null;
    console.log(
      "[MC-POOL-P3] ■",
      done.id,
      "http_started=" + done.http_started,
      "http_completed=" + done.http_completed,
      "duration_ms=" + (done.ended_ms - done.started_ms)
    );
    return done;
  }

  function countHttpSince(idx, kind) {
    var n = 0;
    for (var i = idx; i < httpLog.length; i += 1) {
      if (httpLog[i].kind === kind) n += 1;
    }
    return n;
  }

  function paneChunkSummary() {
    var out = [];
    try {
      if (!window.PD1ExactOriginResearch || !window.PD1ExactOriginResearch.deps) return out;
      var deps = window.PD1ExactOriginResearch.deps;
      if (!deps.visibleIds || !deps.state || !deps.state.panes) return out;
      deps.visibleIds().forEach(function (pid) {
        var pane = deps.state.panes[pid];
        if (!pane) return;
        var stores = pane.exactOriginWindowStores || {};
        var keys = Object.keys(stores);
        var chunks = 0;
        keys.forEach(function (k) {
          var st = stores[k];
          if (st && st.chunks) chunks += Object.keys(st.chunks).length;
        });
        out.push({
          pane: pid,
          store_scopes: keys.length,
          chunk_entries: chunks,
          segment_cache_key: pane.exactOriginSegmentCache ? pane.exactOriginSegmentCache.key : null,
          sweep_cache_key: pane.exactOriginSweepCache ? pane.exactOriginSweepCache.key : null,
          load_serial: pane.exactOriginLoadSerial || 0,
        });
      });
    } catch (e) {
      out.push({ error: String(e) });
    }
    return out;
  }

  function chartTfFromDom() {
    try {
      var sel =
        document.querySelector("#researchPaneTf") ||
        document.querySelector('select[name="pane_tf"]') ||
        document.querySelector(".trp-pane-toolbar select");
      if (sel && sel.value) return sel.value;
    } catch (e) {
      /* ignore */
    }
    var m = window.PD1ExactOriginResearch && PD1ExactOriginResearch.getLastMetrics
      ? PD1ExactOriginResearch.getLastMetrics()
      : null;
    if (m && m.viewFromSec != null) return "from_metrics_viewport";
    return null;
  }

  function snapshotSettings() {
    var eo = document.getElementById("researchIndExactOriginDensity");
    var hist = document.getElementById("researchIndHistoricalLmv2Pools");
    var poolTf =
      document.querySelector('input[name="researchExactOriginPoolTimeframe"]:checked') || {};
    var pd1 = document.querySelector('input[name="researchExactOriginPoolDetail"][value="1"]');
    return {
      symbol: (document.getElementById("researchSymbol") || {}).value || null,
      chart_tf: chartTfFromDom(),
      exact_origin_checked: !!(eo && eo.checked),
      historical_lmv2_checked: !!(hist && hist.checked),
      pool_timeframe: poolTf.value || null,
      pool_detail_pd1_checked: !!(pd1 && pd1.checked),
      canvas_path:
        window.PD1ExactOriginResearch && PD1ExactOriginResearch.isCanvasPathEnabled
          ? !!PD1ExactOriginResearch.isCanvasPathEnabled()
          : null,
      doge_trace: !!(window.DogeExactOriginTrace && DogeExactOriginTrace.enabled()),
      chunk_stores: paneChunkSummary(),
    };
  }

  function parseWindowResponse(data, meta) {
    if (!data || typeof data !== "object") return;
    var prov = data.provenance || {};
    var rec = {
      t_rel_ms: relMs(performance.now()),
      kind: "completed",
      client_ms: meta.client_ms,
      status: meta.status,
      pools_n: Array.isArray(data.pools) ? data.pools.length : null,
      response_cache: data.response_cache || null,
      phase5s_batch_reader_active: prov.phase5s_batch_reader_active,
      request_id: data.request_id || null,
      from_utc: (data.window && data.window.from_utc) || null,
      to_utc: (data.window && data.window.to_utc) || null,
    };
    httpLog.push(rec);
    bumpSection("window_http_completed", rec);
  }

  function patchFetch() {
    if (fetchPatched || !window.fetch) return;
    origFetch = window.fetch.bind(window);
    window.fetch = function (input, init) {
      var url = typeof input === "string" ? input : input && input.url ? input.url : "";
      var isWindow = url.indexOf("/live-charts/viewer/api/window") >= 0;
      var reqId = isWindow ? "w" + httpLog.length + "-" + Date.now() : null;
      var tReq = performance.now();
      if (isWindow && active) {
        var startRec = {
          t_rel_ms: relMs(tReq),
          kind: "started",
          url: url.split("?")[0],
          req_id: reqId,
        };
        httpLog.push(startRec);
        bumpSection("window_http_started", startRec);
      }
      return origFetch(input, init).then(
        function (res) {
          if (!isWindow || !active) return res;
          var client_ms = Math.round(performance.now() - tReq);
          if (!parseBodies) {
            var light = {
              t_rel_ms: relMs(performance.now()),
              kind: "completed",
              req_id: reqId,
              client_ms: client_ms,
              status: res.status,
              parse_skipped: true,
            };
            httpLog.push(light);
            bumpSection("window_http_completed_light", light);
            return res;
          }
          var clone = res.clone();
          clone
            .json()
            .then(function (data) {
              parseWindowResponse(data, { client_ms: client_ms, status: res.status });
            })
            .catch(function () {});
          return res;
        },
        function (err) {
          if (isWindow && active) {
            var errRec = {
              t_rel_ms: relMs(performance.now()),
              kind: "error",
              req_id: reqId,
              message: String(err && err.message ? err.message : err),
            };
            httpLog.push(errRec);
            bumpSection("window_http_error", errRec);
          }
          throw err;
        }
      );
    };
    fetchPatched = true;
  }

  function unpatchFetch() {
    if (fetchPatched && origFetch) window.fetch = origFetch;
    fetchPatched = false;
  }

  function installObservers() {
    disconnectObservers();
    if (!window.PerformanceObserver) return;
    try {
      poLong = new PerformanceObserver(function (list) {
        if (!active) return;
        list.getEntries().forEach(function (e) {
          if (e.entryType !== "longtask") return;
          var key = String(e.startTime) + "|" + String(e.duration);
          if (longTasksDedup[key]) return;
          longTasksDedup[key] = 1;
          longTasks.push({
            t_rel_ms: entryRelMs(e),
            startTime: e.startTime,
            duration: Math.round(e.duration),
            name: e.name,
          });
        });
      });
      poLong.observe({ type: "longtask", buffered: true });
    } catch (e1) {
      poLong = null;
    }
    try {
      poResource = new PerformanceObserver(function (list) {
        if (!active) return;
        list.getEntries().forEach(function (e) {
          if (e.entryType !== "resource") return;
          if (String(e.name).indexOf("/viewer/api/window") < 0) return;
          bumpSection("resource_window", {
            duration: Math.round(e.duration),
            transferSize: e.transferSize,
            encodedBodySize: e.encodedBodySize,
          });
        });
      });
      poResource.observe({ type: "resource", buffered: true });
    } catch (e2) {
      poResource = null;
    }
  }

  function disconnectObservers() {
    if (poLong) {
      try {
        poLong.disconnect();
      } catch (e) {}
      poLong = null;
    }
    if (poResource) {
      try {
        poResource.disconnect();
      } catch (e) {}
      poResource = null;
    }
  }

  function startFrameSampler() {
    stopFrameSampler();
    var last = performance.now();
    function loop(now) {
      if (!active) return;
      var dt = now - last;
      last = now;
      if (dt > 0 && dt < 5000) frameTimes.push(Math.round(dt));
      if (frameTimes.length > 2000) frameTimes.shift();
      rafLoopId = requestAnimationFrame(loop);
    }
    rafLoopId = requestAnimationFrame(loop);
  }

  function stopFrameSampler() {
    if (rafLoopId != null) cancelAnimationFrame(rafLoopId);
    rafLoopId = null;
  }

  function percentile(arr, p) {
    if (!arr.length) return null;
    var s = arr.slice().sort(function (a, b) {
      return a - b;
    });
    var idx = Math.floor((p / 100) * (s.length - 1));
    return s[idx];
  }

  function installWraps() {
    if (wrapsInstalled || !window.PD1ExactOriginResearch) return;
    var R = window.PD1ExactOriginResearch;
    if (R.refreshPane) {
      origRefreshPane = R.refreshPane;
      R.refreshPane = function (pane, opts) {
        var runId = refreshRuns.length;
        var tStart = performance.now();
        var rec = {
          id: runId,
          t_rel_ms: relMs(tStart),
          opts: opts ? { force: !!opts.force, viewport: !!opts.viewport, fromUnix: opts.fromUnix, toUnix: opts.toUnix } : null,
          ended: false,
        };
        refreshRuns.push(rec);
        if (active) bumpSection("refreshPane_start", { run_id: runId, opts: rec.opts });
        return origRefreshPane.call(R, pane, opts).then(
          function (res) {
            rec.ended = true;
            rec.duration_ms = Math.round(performance.now() - tStart);
            if (window.PD1ExactOriginResearch.getLastMetrics) {
              rec.metrics = PD1ExactOriginResearch.getLastMetrics();
            }
            if (active) bumpSection("refreshPane_end", { run_id: runId, duration_ms: rec.duration_ms, metrics: rec.metrics });
            return res;
          },
          function (err) {
            rec.ended = true;
            rec.duration_ms = Math.round(performance.now() - tStart);
            rec.error = String(err && err.message ? err.message : err);
            if (active) bumpSection("refreshPane_error", rec);
            throw err;
          }
        );
      };
    }
    if (R.scheduleViewport) {
      origScheduleViewport = R.scheduleViewport;
      R.scheduleViewport = function (pane, from, to) {
        var rec = { t_rel_ms: relMs(performance.now()), from: from, to: to };
        viewportSchedules.push(rec);
        if (active) bumpSection("scheduleViewport", rec);
        return origScheduleViewport.call(R, pane, from, to);
      };
    }
    if (typeof console.log === "function") {
      origConsoleLog = console.log;
      console.log = function () {
        if (active && arguments.length && String(arguments[0]).indexOf("TRACE |") === 0) {
          traceLines.push({ t_rel_ms: relMs(performance.now()), line: String(arguments[0]) });
          if (String(arguments[0]).indexOf("refreshPane_coalesced") >= 0) {
            coalesceMarks.push({ t_rel_ms: relMs(performance.now()) });
          }
        }
        return origConsoleLog.apply(console, arguments);
      };
    }
    wrapsInstalled = true;
  }

  function removeWraps() {
    if (!wrapsInstalled || !window.PD1ExactOriginResearch) return;
    var R = window.PD1ExactOriginResearch;
    if (origRefreshPane) R.refreshPane = origRefreshPane;
    if (origScheduleViewport) R.scheduleViewport = origScheduleViewport;
    if (origConsoleLog) console.log = origConsoleLog;
    wrapsInstalled = false;
  }

  function sampleViewport(tag) {
    var out = {
      t_rel_ms: relMs(performance.now()),
      tag: tag || "sample",
      settings: snapshotSettings(),
      last_metrics: null,
      canvas_draw: null,
      visible_range: null,
    };
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
          out.visible_range = ch.timeScale().getVisibleRange();
        }
      }
    } catch (e) {
      out.iframe_error = String(e);
    }
    viewportSamples.push(out);
    return out;
  }

  function exportJSON() {
    if (currentSection) endSection({ auto_closed: true });
    var payload = {
      schema_version: SCHEMA,
      exported_wall_ms: Date.now(),
      settings_at_export: snapshotSettings(),
      sections: sections,
      http_log: httpLog,
      refresh_runs: refreshRuns,
      viewport_schedules: viewportSchedules,
      viewport_samples: viewportSamples,
      long_tasks_deduped: longTasks,
      coalesce_marks: coalesceMarks,
      trace_line_count: traceLines.length,
      trace_lines_sample: traceLines.slice(-80),
      frame_time_ms: {
        n: frameTimes.length,
        p50: percentile(frameTimes, 50),
        p95: percentile(frameTimes, 95),
        max: frameTimes.length ? Math.max.apply(null, frameTimes) : null,
      },
      notes: {
        browser_chunk_hits: "last_metrics.windowFetch.cacheHits after refreshPane_end",
        sweep_cache_hit: "last_metrics.sweepCacheHit",
        adapter_ms: "last_metrics.adapterMs (includes sweep, not canvas draw)",
      },
    };
    var ts = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
    var name = "MC_POOL_P3_SOLUSDT_5M_" + ts + ".json";
    var blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    a.click();
    setTimeout(function () {
      URL.revokeObjectURL(a.href);
    }, 3000);
    console.log("[MC-POOL-P3] exported", name);
    return payload;
  }

  function reset() {
    stop({ quiet: true });
    sections = [];
    httpLog = [];
    refreshRuns = [];
    viewportSchedules = [];
    viewportSamples = [];
    longTasks = [];
    longTasksDedup = Object.create(null);
    frameTimes = [];
    traceLines = [];
    coalesceMarks = [];
    t0 = performance.now();
  }

  function start(opts) {
    opts = opts || {};
    if (active) stop({ quiet: true });
    reset();
    active = true;
    parseBodies = !!opts.parseWindowBodies;
    installWraps();
    try {
      localStorage.setItem("DOGE_EXACT_ORIGIN_TRACE", "1");
    } catch (e) {}
    if (window.DogeExactOriginTrace && DogeExactOriginTrace.activationHelp) {
      DogeExactOriginTrace.activationHelp();
    }
    patchFetch();
    installObservers();
    startFrameSampler();
    bumpSection("measure_start", snapshotSettings());
    console.log("[MC-POOL-P3] started parseBodies=" + parseBodies, snapshotSettings());
    if (opts.auto_section_a) startSection("A", "baseline_after_pools_on");
    return true;
  }

  function stop(extra) {
    active = false;
    unpatchFetch();
    disconnectObservers();
    stopFrameSampler();
    if (!(extra && extra.quiet)) bumpSection("measure_stop", extra || {});
    if (!(extra && extra.quiet)) console.log("[MC-POOL-P3] stopped");
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
    setParseWindowBodies: function (on) {
      parseBodies = !!on;
    },
    snapshotSettings: snapshotSettings,
    help: function () {
      console.log(
        "[MC-POOL-P3] start({parseWindowBodies:false}) → tests A–E → exportJSON() → stop()",
        "Wraps refreshPane/scheduleViewport; enable DOGE_EXACT_ORIGIN_TRACE for coalesce lines"
      );
    },
  };

  window.__MC_POOL_P3__ = api;
  console.log("[MC-POOL-P3] ready — __MC_POOL_P3__.help()");
  return api;
})();