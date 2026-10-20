/**
 * B – Exact Origin-Density in Research Live Chart (/live-charts/research).
 * Uses viewer window API + TRP zone overlays; mutual exclusion with Historical LLD V2.
 */
(function () {
  "use strict";

  const API_WINDOW = "/live-charts/viewer/api/window";
  /** Server applies WARMUP_HOURS=168 inside build_landscape_window; do not subtract here. */
  const MAX_API_VISIBLE_MS = 72 * 3600 * 1000;
  const API_TAIL_PAD_MS = 6 * 3600 * 1000;
  const SWEEP_PAD_MS = 2 * 3600 * 1000;
  const VIEWER_CAP = "/live-charts/viewer/api/capabilities";
  const STEP_MS = 5 * 60 * 1000;
  const TF_SEC = 900;
  const DEFAULT_MAX_CHART_ZONES = 900;
  const EOD_CANVAS_FLAG_KEY = "phase6xt_eod_density_canvas_v1";
  const EOD_FIXED_TILE_48H_FLAG_KEY = "phase6xt_eod_fixed_tile_48h_v1";
  const VERSION = "phase6xt-density-fixed-tile-integration-v1";
  const MAX_CONCURRENT_WINDOW_FETCH = 2;

  let deps = null;
  let caps = null;
  const inflight = new Map();
  let lastMetrics = null;

  function isoFromMs(ms) {
    return new Date(ms).toISOString().replace(".000", "");
  }

  function floor5mMs(ms) {
    return Math.floor(ms / STEP_MS) * STEP_MS;
  }

  function ceil5mMs(ms) {
    return Math.ceil(ms / STEP_MS) * STEP_MS;
  }

  function isEnabled() {
    const cb = deps && deps.$("researchIndExactOriginDensity");
    return !!(cb && cb.checked);
  }

  /** Fixed 48h UTC tiles for window cache (default OFF — sliding planner). */
  function isFixedTile48hEnabled() {
    try {
      return localStorage.getItem(EOD_FIXED_TILE_48H_FLAG_KEY) === "1";
    } catch (e) {
      return false;
    }
  }

  /** Sole canvas flag source — localStorage only (no host/deps callback; avoids recursion). */
  function isCanvasPathEnabled() {
    try {
      return localStorage.getItem(EOD_CANVAS_FLAG_KEY) === "1";
    } catch (e) {
      return false;
    }
  }

  function viewportRenderKey(hit) {
    return [hit.from, hit.to].join("-");
  }

  function windowStoreForPane(pane) {
    if (!pane.exactOriginWindowStore) {
      pane.exactOriginWindowStore = { chunks: {}, order: [], replayGen: null };
    }
    return pane.exactOriginWindowStore;
  }

  function resetWindowStore(pane) {
    if (pane) {
      pane.exactOriginWindowStore = { chunks: {}, order: [], replayGen: null };
      pane.exactOriginSegmentCache = null;
    }
  }

  function clearCanvasOnPane(pane) {
    const chart = deps && deps.api(pane);
    if (chart && chart.clearExactOriginDensity) chart.clearExactOriginDensity();
    if (pane) {
      pane.exactOriginSegmentCache = null;
      resetWindowStore(pane);
    }
  }

  function pushCanvasDensity(pane, segments, metrics) {
    const chart = deps.api(pane);
    mergeOverlays(pane, []);
    if (chart && chart.setExactOriginDensity) {
      chart.setExactOriginDensity({ segments: segments, metrics: metrics });
    }
    if (chart && chart.layoutOverlays) chart.layoutOverlays();
    const drawStats =
      chart && chart.getExactOriginDensityDrawStats ? chart.getExactOriginDensityDrawStats() : null;
    if (drawStats && metrics) {
      metrics.canvasDrawMs = drawStats.ms;
      metrics.canvasSegmentsDrawn = drawStats.segmentsDrawn;
      metrics.canvasSegmentsTotal = drawStats.segmentsTotal;
    }
  }

  function paneSupports(pane) {
    if (!deps || !pane || !deps.state.symbol) return false;
    if (String(deps.state.symbol).toUpperCase() !== "DOGEUSDT") return false;
    if (pane.tf !== "15m") return false;
    return true;
  }

  function maxChartZones() {
    if (deps && deps.SC_HELPERS && deps.SC_HELPERS.LIVE_MICRO_LLD_V2_MAX_CHART_ZONES) {
      return deps.SC_HELPERS.LIVE_MICRO_LLD_V2_MAX_CHART_ZONES;
    }
    return DEFAULT_MAX_CHART_ZONES;
  }

  function stripExact(list) {
    return deps.SC_HELPERS && deps.SC_HELPERS.stripExactOriginDensityOverlays
      ? deps.SC_HELPERS.stripExactOriginDensityOverlays(list)
      : (list || []).filter((p) => p && p.namespace !== "PD1_EXACT_ORIGIN_DENSITY_V1");
  }

  function stripHist(list) {
    return deps.SC_HELPERS && deps.SC_HELPERS.stripHistoricalLmv2Overlays
      ? deps.SC_HELPERS.stripHistoricalLmv2Overlays(list)
      : list || [];
  }

  function mergeOverlays(pane, zones) {
    const base = stripExact(stripHist(pane.pendingOverlays || []));
    deps.syncOverlays(pane, base.concat(zones || []));
  }

  function stripAllPanes() {
    if (!deps) return;
    deps.visibleIds().forEach((pid) => {
      const pane = deps.state.panes[pid];
      if (!pane) return;
      clearCanvasOnPane(pane);
      deps.syncOverlays(pane, stripExact(pane.pendingOverlays || []));
    });
  }

  async function loadCaps() {
    if (caps) return caps;
    const res = await fetch(VIEWER_CAP, { credentials: "same-origin" });
    caps = await res.json().catch(() => ({}));
    return caps;
  }

  function candleBoundsSec(pane) {
    const candles = (pane.pendingData && pane.pendingData.candles) || [];
    if (!candles.length) return null;
    const t0 = Number(candles[0].time);
    const t1 = Number(candles[candles.length - 1].time);
    if (!Number.isFinite(t0) || !Number.isFinite(t1)) return null;
    return { from: Math.min(t0, t1), to: Math.max(t0, t1), source: "candles" };
  }

  /**
   * Priority: explicit opts → replay GO-TO window → Historical viewport resolver → candles.
   */
  function resolveViewSec(pane, opts) {
    if (
      opts &&
      opts.fromUnix != null &&
      opts.toUnix != null &&
      Number(opts.fromUnix) < Number(opts.toUnix)
    ) {
      return {
        from: Math.floor(Number(opts.fromUnix)),
        to: Math.floor(Number(opts.toUnix)),
        source: opts.viewport ? "viewport_opts" : "explicit_opts",
      };
    }
    if (
      !(opts && opts.viewport) &&
      deps.isHistoricalReplay &&
      deps.isHistoricalReplay() &&
      deps.state.replayTargetTs != null
    ) {
      const rw = deps.state.replayWindow || {};
      const pad = rw.viewPad != null ? rw.viewPad : 4 * 3600;
      const g = Math.floor(Number(deps.state.replayTargetTs));
      return { from: g - pad, to: g + pad, source: "replay_window" };
    }
    const chart = deps.api(pane);
    if (deps.SC_HELPERS && deps.decisionContext && chart) {
      const ctx = deps.decisionContext(pane);
      const vp = deps.SC_HELPERS.resolveHistoricalLmv2ViewportCloseWindow(pane, TF_SEC, ctx, chart);
      if (vp && !vp.viewport_after_replay_cutoff) {
        const fromClose =
          vp.display_from_close_unix != null ? vp.display_from_close_unix : vp.view_from_close_unix;
        const toClose =
          vp.display_to_close_unix != null ? vp.display_to_close_unix : vp.view_to_close_unix;
        const fromOpen = Math.floor(fromClose) - TF_SEC;
        const toOpen = Math.floor(toClose);
        if (fromOpen < toOpen) {
          return { from: fromOpen, to: toOpen, source: "hist_viewport_resolver" };
        }
      }
    }
    const candles = candleBoundsSec(pane);
    if (candles) return candles;
    const chart2 = deps.api(pane);
    if (chart2 && chart2.getVisibleTimeRange) {
      const vr = chart2.getVisibleTimeRange();
      if (vr && vr.from != null && vr.to != null && Number(vr.from) < Number(vr.to)) {
        return {
          from: Math.floor(Number(vr.from)),
          to: Math.floor(Number(vr.to)),
          source: "chart_visible_range",
        };
      }
    }
    return null;
  }

  function intersectViewWithData(view, loMs, hiMs) {
    const loSec = Math.floor(loMs / 1000);
    const hiSec = Math.floor(hiMs / 1000);
    const from = Math.max(view.from, loSec);
    const to = Math.min(view.to, hiSec);
    if (from >= to) return null;
    return { from, to };
  }

  function fitApiVisibleWindow(apiFrom, apiTo, hitFromMs, hitToMs, loMs, hiMs) {
    if (apiFrom >= apiTo || apiTo - apiFrom <= MAX_API_VISIBLE_MS) {
      return { apiFrom, apiTo };
    }
    let from = apiFrom;
    let to = apiTo;
    if (hitToMs - hitFromMs >= MAX_API_VISIBLE_MS) {
      from = floor5mMs(hitFromMs);
      to = Math.min(ceil5mMs(hiMs), from + MAX_API_VISIBLE_MS);
    } else {
      const center = (hitFromMs + hitToMs) / 2;
      from = floor5mMs(Math.max(loMs, center - MAX_API_VISIBLE_MS / 2));
      to = Math.min(ceil5mMs(hiMs), from + MAX_API_VISIBLE_MS);
      from = Math.max(floor5mMs(loMs), to - MAX_API_VISIBLE_MS);
    }
    if (from > hitFromMs || to < hitToMs) {
      from = Math.max(floor5mMs(loMs), floor5mMs(hitFromMs));
      to = Math.min(ceil5mMs(hiMs), ceil5mMs(hitToMs + API_TAIL_PAD_MS));
      if (to - from > MAX_API_VISIBLE_MS) {
        to = from + MAX_API_VISIBLE_MS;
      }
    }
    return { apiFrom: from, apiTo: to };
  }

  function buildWindows(viewSec, loMs, hiMs) {
    const hit = intersectViewWithData(viewSec, loMs, hiMs);
    if (!hit) {
      return {
        ok: false,
        reason: "view_outside_data",
        view: viewSec,
        loMs,
        hiMs,
      };
    }
    const hitFromMs = hit.from * 1000;
    const hitToMs = hit.to * 1000;
    let apiFrom = floor5mMs(hitFromMs);
    let apiTo = ceil5mMs(hitToMs + API_TAIL_PAD_MS);
    apiFrom = Math.max(apiFrom, floor5mMs(loMs));
    apiTo = Math.min(apiTo, ceil5mMs(hiMs));
    const fitted = fitApiVisibleWindow(apiFrom, apiTo, hitFromMs, hitToMs, loMs, hiMs);
    apiFrom = fitted.apiFrom;
    apiTo = fitted.apiTo;
    let sweep0 = floor5mMs(hitFromMs - SWEEP_PAD_MS);
    let sweep1 = ceil5mMs(hitToMs + SWEEP_PAD_MS);
    sweep0 = Math.max(sweep0, floor5mMs(loMs));
    sweep1 = Math.min(sweep1, ceil5mMs(hiMs));
    if (apiFrom >= apiTo || sweep0 >= sweep1) {
      return {
        ok: false,
        reason: "empty_intersection",
        view: viewSec,
        hit,
        apiFrom,
        apiTo,
        loMs,
        hiMs,
      };
    }
    return {
      ok: true,
      view: viewSec,
      hit,
      apiFromMs: apiFrom,
      apiToMs: apiTo,
      sweepT0Ms: sweep0,
      sweepT1Ms: sweep1,
    };
  }

  async function fetchPlannedWindows(AD, store, planned, loadSerial, reqReplayGen, pane, retryErrors) {
    const missing = [];
    for (let i = 0; i < planned.length; i += 1) {
      const w = planned[i];
      const ck = AD.exactOriginCacheKeyForWindow(w);
      const ent = store.chunks[ck];
      if (ent && ent.status === "ok" && ent.pools) continue;
      if (ent && ent.status === "error" && !retryErrors) continue;
      missing.push(w);
    }
    if (!missing.length) {
      return { fetched: 0, failed: 0, cacheHits: planned.length };
    }
    let fetched = 0;
    let failed = 0;
    let cursor = 0;
    async function worker() {
      while (cursor < missing.length) {
        const idx = cursor;
        cursor += 1;
        const w = missing[idx];
        const ck = AD.exactOriginCacheKeyForWindow(w);
        try {
          const data = await fetchWindow(w.fromMs, w.toMs);
          if (loadSerial !== pane.exactOriginLoadSerial) return;
          if (reqReplayGen != null && deps.state.replayGen !== reqReplayGen) return;
          AD.touchExactOriginWindowCache(store, ck, {
            status: "ok",
            pools: data.pools || [],
            fromMs: w.fromMs,
            toMs: w.toMs,
            at: Date.now(),
          });
          fetched += 1;
        } catch (err) {
          AD.touchExactOriginWindowCache(store, ck, {
            status: "error",
            error: String(err && err.message ? err.message : err),
            fromMs: w.fromMs,
            toMs: w.toMs,
            at: Date.now(),
          });
          failed += 1;
        }
      }
    }
    const workers = [];
    const n = Math.min(MAX_CONCURRENT_WINDOW_FETCH, missing.length);
    for (let i = 0; i < n; i += 1) workers.push(worker());
    await Promise.all(workers);
    return {
      fetched: fetched,
      failed: failed,
      cacheHits: planned.length - missing.length,
    };
  }

  function gatherPoolsFromStore(AD, store, spanFromMs, spanToMs) {
    const lists = [];
    (store.order || []).forEach(function (key) {
      const ent = store.chunks[key];
      if (!ent || ent.status !== "ok" || !ent.pools) return;
      if (!AD.windowOverlapsSpan({ fromMs: ent.fromMs, toMs: ent.toMs }, spanFromMs, spanToMs)) return;
      lists.push(ent.pools);
    });
    return AD.mergeDensityPools(lists);
  }

  async function fetchWindow(fromMs, toMs) {
    const key = fromMs + "-" + toMs;
    if (inflight.has(key)) return inflight.get(key);
    const run = fetch(API_WINDOW, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ from_utc: isoFromMs(fromMs), to_utc: isoFromMs(toMs) }),
    })
      .then(async (res) => {
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.message || data.detail || "window " + res.status);
        return data;
      })
      .finally(() => {
        inflight.delete(key);
      });
    inflight.set(key, run);
    return run;
  }

  function applyZoneCap(zones, cap) {
    if (!zones || zones.length <= cap) {
      return { zones: zones || [], capped: false, dropped: 0 };
    }
    return { zones: zones.slice(0, cap), capped: true, dropped: zones.length - cap };
  }

  async function refreshPane(pane, opts) {
    if (!isEnabled() || !paneSupports(pane)) return;
    const loadSerial = (pane.exactOriginLoadSerial || 0) + 1;
    pane.exactOriginLoadSerial = loadSerial;
    const reqReplayGen =
      deps.isHistoricalReplay && deps.isHistoricalReplay() ? deps.state.replayGen : null;

    const AD = window.PD1ExactDensityTrpAdapter;
    if (!AD) throw new Error("PD1ExactDensityTrpAdapter missing");

    const tAdapter0 = typeof performance !== "undefined" ? performance.now() : 0;
    const viewSec = resolveViewSec(pane, opts);
    const c = await loadCaps();
    const loMs = c && c.data_available_from ? Date.parse(c.data_available_from) : 0;
    const hiMs = c && c.data_available_to ? Date.parse(c.data_available_to) : Number.MAX_SAFE_INTEGER;

    if (!viewSec) {
      const hint = deps.$("researchExactOriginDensityHint");
      if (hint) {
        hint.hidden = false;
        hint.textContent = "B – Exact Origin: Viewport nicht bereit (Chart laden oder GO TO)";
      }
      clearCanvasOnPane(pane);
      mergeOverlays(pane, []);
      return;
    }

    const win = buildWindows(viewSec, loMs, hiMs);
    if (!win.ok) {
      const hint = deps.$("researchExactOriginDensityHint");
      if (hint) {
        hint.hidden = false;
        const loIso = c && c.data_available_from ? c.data_available_from.slice(0, 10) : "?";
        const hiIso = c && c.data_available_to ? c.data_available_to.slice(0, 10) : "?";
        hint.textContent =
          "B – Exact Origin: kein Schnitt mit historischen Daten (" +
          loIso +
          " … " +
          hiIso +
          ") · Quelle " +
          viewSec.source;
      }
      clearCanvasOnPane(pane);
      mergeOverlays(pane, []);
      lastMetrics = { ok: false, reason: win.reason, view: viewSec, win };
      return;
    }

    const hitFromMs = win.hit.from * 1000;
    const hitToMs = win.hit.to * 1000;
    const canvasOn = isCanvasPathEnabled();
    const store = windowStoreForPane(pane);
    const replayGenNow =
      deps.isHistoricalReplay && deps.isHistoricalReplay() ? deps.state.replayGen : null;
    if (store.replayGen !== replayGenNow) {
      resetWindowStore(pane);
      pane.exactOriginWindowStore.replayGen = replayGenNow;
    }
    const loadSpan = AD.computeDensityLoadSpanMs(hitFromMs, hitToMs, loMs, hiMs);
    const fixedTile48h = isFixedTile48hEnabled();
    const windowPlanner = fixedTile48h ? "fixed48h" : "sliding";
    const planned = AD.planExactOriginLoadWindows(loadSpan.fromMs, loadSpan.toMs, loMs, hiMs, {
      mode: windowPlanner,
    });
    const renderKey = viewportRenderKey(win.hit);
    const fullyCached =
      !(opts && opts.force) && AD.exactOriginWindowsFullyCached(store, planned);
    if (
      canvasOn &&
      fullyCached &&
      pane.exactOriginSegmentCache &&
      pane.exactOriginSegmentCache.key === renderKey
    ) {
      pushCanvasDensity(pane, pane.exactOriginSegmentCache.segments, pane.exactOriginSegmentCache.metrics);
      lastMetrics = pane.exactOriginSegmentCache.metrics;
      const hint = deps.$("researchExactOriginDensityHint");
      if (hint && lastMetrics) {
        hint.hidden = false;
        hint.textContent =
          "B – Exact Origin · canvas " +
          lastMetrics.mergedSegments +
          " seg · " +
          lastMetrics.pools +
          " pools · cache · " +
          viewSec.source;
      }
      return;
    }

    const fetchStats = await fetchPlannedWindows(
      AD,
      store,
      planned,
      loadSerial,
      reqReplayGen,
      pane,
      !!(opts && (opts.viewport || opts.force))
    );
    if (loadSerial !== pane.exactOriginLoadSerial) return;
    if (reqReplayGen != null && deps.state.replayGen !== reqReplayGen) return;

    const pools = gatherPoolsFromStore(AD, store, loadSpan.fromMs, loadSpan.toMs);
    if (!pools.length && fetchStats.failed > 0 && fetchStats.fetched === 0) {
      const hintFail = deps.$("researchExactOriginDensityHint");
      if (hintFail) {
        hintFail.hidden = false;
        hintFail.textContent = "B – Exact Origin: window load failed (prior data retained)";
      }
      if (pane.exactOriginSegmentCache && pane.exactOriginSegmentCache.segments) {
        pushCanvasDensity(pane, pane.exactOriginSegmentCache.segments, pane.exactOriginSegmentCache.metrics);
      }
      return;
    }
    const ext = pools.length
      ? {
          yMin: Math.min(...pools.map((p) => p.bottom)) * 0.9995,
          yMax: Math.max(...pools.map((p) => p.top)) * 1.0005,
        }
      : { yMin: 0, yMax: 1 };

    let zones = AD.poolsToExactOriginZones(pools, loadSpan.fromMs, loadSpan.toMs, ext.yMin, ext.yMax, {
      sweepT0Ms: win.sweepT0Ms,
      sweepT1Ms: win.sweepT1Ms,
    });
    const rawZoneCount = zones.length;
    const tAdapter1 = typeof performance !== "undefined" ? performance.now() : 0;

    let culledCount = rawZoneCount;
    let mergedSegments = null;
    let cullMs = 0;
    let mergeMs = 0;
    const cap = maxChartZones();
    let capped = { zones: zones, capped: false, dropped: 0 };
    let registeredZones = rawZoneCount;

    if (canvasOn) {
      const tCull0 = typeof performance !== "undefined" ? performance.now() : 0;
      const bounds = AD.computeViewportCullBounds(win.hit.from, win.hit.to, ext.yMin, ext.yMax);
      const culled = AD.cullZonesToViewport(zones, bounds);
      cullMs = Math.round((typeof performance !== "undefined" ? performance.now() : 0) - tCull0);
      culledCount = culled.length;
      const tMerge0 = typeof performance !== "undefined" ? performance.now() : 0;
      mergedSegments = AD.mergeExactOriginSegments(culled);
      mergeMs = Math.round((typeof performance !== "undefined" ? performance.now() : 0) - tMerge0);
      registeredZones = mergedSegments.length;
      const maxSeg = AD.EOD_CANVAS_MAX_SEGMENTS || 8000;
      if (mergedSegments.length > maxSeg) {
        clearCanvasOnPane(pane);
        mergeOverlays(pane, []);
        lastMetrics = {
          ok: false,
          reason: "perf_segment_cap",
          maxSegments: maxSeg,
          mergedSegments: mergedSegments.length,
          rawZones: rawZoneCount,
          culledZones: culledCount,
          adapterMs: Math.round(tAdapter1 - tAdapter0),
          cullMs: cullMs,
          mergeMs: mergeMs,
          canvasPath: true,
          version: VERSION,
        };
        pane.exactOriginLastMetrics = lastMetrics;
        const hintCap = deps.$("researchExactOriginDensityHint");
        if (hintCap) {
          hintCap.hidden = false;
          hintCap.textContent =
            "B – Exact Origin: PERF_BLOCK — " +
            mergedSegments.length +
            " Segmente (max " +
            maxSeg +
            ")";
        }
        return;
      }
    } else {
      capped = applyZoneCap(zones, cap);
      zones = capped.zones;
      registeredZones = zones.length;
    }

    if (loadSerial !== pane.exactOriginLoadSerial) return;
    if (reqReplayGen != null && deps.state.replayGen !== reqReplayGen) return;

    if (canvasOn) {
      pushCanvasDensity(pane, mergedSegments, null);
    } else {
      clearCanvasOnPane(pane);
      mergeOverlays(pane, zones);
    }

    lastMetrics = {
      ok: true,
      viewSource: viewSec.source,
      viewFromSec: viewSec.from,
      viewToSec: viewSec.to,
      apiFromMs: win.apiFromMs,
      apiToMs: win.apiToMs,
      sweepT0Ms: win.sweepT0Ms,
      sweepT1Ms: win.sweepT1Ms,
      apiFromIso: isoFromMs(win.apiFromMs),
      apiToIso: isoFromMs(win.apiToMs),
      pools: pools.length,
      rawZones: rawZoneCount,
      culledZones: culledCount,
      mergedSegments: canvasOn ? registeredZones : null,
      registeredZones: registeredZones,
      zoneCap: canvasOn ? null : cap,
      zoneCapped: canvasOn ? false : capped.capped,
      zonesDropped: canvasOn ? 0 : capped.dropped,
      adapterMs: Math.round(tAdapter1 - tAdapter0),
      cullMs: canvasOn ? cullMs : null,
      mergeMs: canvasOn ? mergeMs : null,
      canvasPath: canvasOn,
      version: VERSION,
      windowPlanner: windowPlanner,
      fixedTile48h: fixedTile48h,
      windowsPlanned: planned.length,
      windowFetch: fetchStats,
      loadSpanFromMs: loadSpan.fromMs,
      loadSpanToMs: loadSpan.toMs,
    };
    if (canvasOn) {
      const chart = deps.api(pane);
      const drawStats =
        chart && chart.getExactOriginDensityDrawStats ? chart.getExactOriginDensityDrawStats() : null;
      if (drawStats) {
        lastMetrics.canvasDrawMs = drawStats.ms;
        lastMetrics.canvasSegmentsDrawn = drawStats.segmentsDrawn;
        lastMetrics.canvasSegmentsTotal = drawStats.segmentsTotal;
      }
      pane.exactOriginSegmentCache = { key: renderKey, segments: mergedSegments, metrics: lastMetrics };
    } else {
      pane.exactOriginSegmentCache = null;
    }
    pane.exactOriginLastMetrics = lastMetrics;

    const hint = deps.$("researchExactOriginDensityHint");
    if (hint) {
      hint.hidden = false;
      let line;
      if (canvasOn) {
        line =
          "B – Exact Origin · canvas " +
          registeredZones +
          " seg (raw " +
          rawZoneCount +
          ") · " +
          pools.length +
          " pools · " +
          viewSec.source;
      } else {
        line =
          "B – Exact Origin · " +
          zones.length +
          " zones · " +
          pools.length +
          " pools · " +
          viewSec.source;
        if (capped.capped) {
          line += " · CAP " + cap + " (raw " + rawZoneCount + ")";
        }
      }
      hint.textContent = line;
    }
  }

  async function refreshAll(opts) {
    if (!isEnabled()) {
      stripAllPanes();
      return;
    }
    const targets = [];
    deps.visibleIds().forEach((pid) => {
      const p = deps.state.panes[pid];
      if (p && paneSupports(p)) targets.push(p);
    });
    if (!targets.length) {
      const hint = deps.$("researchExactOriginDensityHint");
      if (hint) {
        hint.hidden = false;
        hint.textContent = "B – Exact Origin: nur DOGEUSDT 15m Historical PD1";
      }
      stripAllPanes();
      return;
    }
    for (let i = 0; i < targets.length; i += 1) {
      await refreshPane(targets[i], opts);
    }
  }

  function scheduleViewport(pane, from, to) {
    if (!isEnabled() || !paneSupports(pane)) return;
    if (pane.exactOriginViewportTimer) clearTimeout(pane.exactOriginViewportTimer);
    pane.exactOriginViewportTimer = setTimeout(function () {
      pane.exactOriginViewportTimer = null;
      refreshPane(pane, { fromUnix: from, toUnix: to, viewport: true }).catch(function (err) {
        if (!(err && err.name === "AbortError") && deps.setStatus) {
          deps.setStatus("Exact Origin: " + (err.message || err), "error");
        }
      });
    }, deps.VP_DEBOUNCE_MS || 400);
  }

  function updateUiChrome() {
    const on = isEnabled();
    const wrap = deps && deps.$("researchExactOriginDensityWrap");
    const hint = deps && deps.$("researchExactOriginDensityHint");
    if (wrap) wrap.classList.toggle("exact-origin-active", on);
    if (!on && hint) hint.hidden = true;
  }

  function disableHistoricalLmv2() {
    const h = deps.$("researchIndHistoricalLmv2Pools");
    if (h && h.checked) h.checked = false;
    deps.state.historicalLmv2Cache = {};
    deps.state.historicalLmv2ChunkStore = { chunks: {}, order: [] };
    deps.updateHistoricalLmv2UiChrome();
    deps.visibleIds().forEach((pid) => {
      const pane = deps.state.panes[pid];
      if (!pane) return;
      deps.syncOverlays(pane, stripHist(pane.pendingOverlays || []));
    });
    deps.updateHistoricalLmv2Hint(null);
  }

  function disableExactOrigin() {
    const cb = deps.$("researchIndExactOriginDensity");
    if (cb && cb.checked) cb.checked = false;
    stripAllPanes();
    updateUiChrome();
  }

  function install(d) {
    deps = d;
    const cb = deps.$("researchIndExactOriginDensity");
    if (!cb) return;
    cb.addEventListener("change", async function () {
      updateUiChrome();
      if (isEnabled()) {
        disableHistoricalLmv2();
        const pd1 = document.querySelector('input[name="researchHistoricalLmv2PoolDetail"][value="1"]');
        if (pd1) pd1.checked = true;
        try {
          await refreshAll({ force: true });
        } catch (err) {
          deps.setStatus("Exact Origin: " + (err.message || err), "error");
        }
      } else {
        stripAllPanes();
      }
    });
    const hist = deps.$("researchIndHistoricalLmv2Pools");
    if (hist) {
      hist.addEventListener("change", function () {
        if (deps.historicalLmv2PoolsEnabled() && isEnabled()) {
          disableExactOrigin();
        }
      });
    }
    updateUiChrome();
  }

  function exactOriginCanvasFlagKey() {
    return EOD_CANVAS_FLAG_KEY;
  }

  function exactOriginFixedTile48hFlagKey() {
    return EOD_FIXED_TILE_48H_FLAG_KEY;
  }

  window.RESEARCH_EXACT_ORIGIN_DENSITY_CANVAS_V1 = isCanvasPathEnabled;

  window.PD1ExactOriginResearch = {
    install: install,
    isEnabled: isEnabled,
    isCanvasPathEnabled: isCanvasPathEnabled,
    isFixedTile48hEnabled: isFixedTile48hEnabled,
    exactOriginCanvasFlagKey: exactOriginCanvasFlagKey,
    exactOriginFixedTile48hFlagKey: exactOriginFixedTile48hFlagKey,
    VERSION: VERSION,
    paneSupports: paneSupports,
    refreshAll: refreshAll,
    refreshPane: refreshPane,
    scheduleViewport: scheduleViewport,
    stripAllPanes: stripAllPanes,
    updateUiChrome: updateUiChrome,
    disableExactOrigin: disableExactOrigin,
    disableHistoricalLmv2: disableHistoricalLmv2,
    _resolveViewSec: resolveViewSec,
    _buildWindows: buildWindows,
    getLastMetrics: function () {
      return lastMetrics;
    },
  };
})();
