/**
 * B – Exact Origin-Density in Research Live Chart (/live-charts/research).
 * Uses viewer window API + TRP zone overlays; mutual exclusion with Historical LLD V2.
 */
(function () {
  "use strict";

  const API_WINDOW = "/live-charts/viewer/api/window";
  const STEP_MS = 5 * 60 * 1000;
  /** Match backend MAX_5M_BARS=864 inclusive: span may cover at most 863×5m between endpoints. */
  const MAX_API_VISIBLE_MS = (864 - 1) * STEP_MS;
  const API_TAIL_PAD_MS = 6 * 3600 * 1000;
  const SWEEP_PAD_MS = 2 * 3600 * 1000;
  const VIEWER_CAP = "/live-charts/viewer/api/capabilities";
  const DEFAULT_MAX_CHART_ZONES = 900;
  const EOD_CANVAS_FLAG_KEY = "phase6xt_eod_density_canvas_v1";
  const EOD_FIXED_TILE_48H_FLAG_KEY = "phase6xt_eod_fixed_tile_48h_v1";
  const MIN_ORIGINS_STORAGE_KEY = "phase6xt_eod_density_min_origins_v1";
  const MIN_ORIGINS_SLIDER_MAX = 10;
  const VERSION = "pd1-p5n-live-tile-refresh-v1";
  /** Viewer API release matrix (no silent fallbacks). */
  const RELEASED_VIEWER_SCOPES = [
    { timeframe: "15m", pool_detail: 1 },
    { timeframe: "15m", pool_detail: 5 },
    { timeframe: "15m", pool_detail: 10 },
    { timeframe: "5m", pool_detail: 1 },
    { timeframe: "5m", pool_detail: 5 },
    { timeframe: "5m", pool_detail: 10 },
    { timeframe: "30m", pool_detail: 1 },
    { timeframe: "1h", pool_detail: 1 },
    { timeframe: "4h", pool_detail: 10 },
  ];
  const POOL_DETAIL_VALUES = [1, 5, 10];
  const MAX_CONCURRENT_WINDOW_FETCH = 2;
  const MAX_CONCURRENT_WINDOW_FETCH_1H = 3;

  let deps = null;
  let caps = null;
  let capsLoadInflight = null;
  const inflight = new Map();
  let lastMetrics = null;

  function eoTr() {
    return typeof window !== "undefined" ? window.DogeExactOriginTrace : null;
  }

  function traceIdForPane(pane) {
    return pane && pane._eoTraceId ? pane._eoTraceId : null;
  }

  function ensureEoTrace(pane) {
    const T = eoTr();
    if (!T || !T.enabled()) return null;
    if (pane && !pane._eoTraceId) pane._eoTraceId = T.newTraceId();
    if (pane && pane._eoTraceId) return pane._eoTraceId;
    return T.newTraceId();
  }

  function traceContext(pane) {
    const sym =
      deps && deps.state && deps.state.symbol ? String(deps.state.symbol).toUpperCase() : "?";
    const chart = pane ? paneChartTimeframe(pane) : "?";
    const pool = selectedPoolTimeframe();
    const T = eoTr();
    const p5k = T && T.p5kCaseLabel ? T.p5kCaseLabel(chart, pool) : chart + "/" + pool;
    const candles = pane && pane.pendingData && pane.pendingData.candles;
    const lastCandle =
      candles && candles.length ? Number(candles[candles.length - 1].time) : null;
    let visible = null;
    const chartApi = deps && deps.api && pane ? deps.api(pane) : null;
    if (chartApi && chartApi.getVisibleTimeRange) {
      try {
        visible = chartApi.getVisibleTimeRange();
      } catch (eVis) {
        visible = null;
      }
    }
    const histLmv2 = deps.$("researchIndHistoricalLmv2Pools");
    const activeCb = deps.$("researchIndLiveMicroLldV2Remaining");
    return {
      p5k_case: p5k,
      chart: chart,
      pool: pool,
      symbol: sym,
      js: VERSION,
      min_origins: getMinOrigins(),
      exact_origin_on: isEnabled(),
      hist_lmv2_on: !!(histLmv2 && histLmv2.checked),
      active_overlay_on: !!(activeCb && activeCb.checked),
      historical_replay: deps.isHistoricalReplay ? deps.isHistoricalReplay() : null,
      history_blocks_live: deps.historyBlocksLive ? deps.historyBlocksLive() : null,
      last_candle_unix: lastCandle,
      visible_from_unix: visible && visible.from != null ? Math.floor(Number(visible.from)) : null,
      visible_to_unix: visible && visible.to != null ? Math.floor(Number(visible.to)) : null,
    };
  }

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

  function paneChartTimeframe(pane) {
    return pane && pane.tf ? String(pane.tf) : "";
  }

  function isScopeReleased(timeframe, poolDetail) {
    const tf = String(timeframe);
    const pd = Number(poolDetail);
    return RELEASED_VIEWER_SCOPES.some(function (s) {
      return s.timeframe === tf && s.pool_detail === pd;
    });
  }

  function selectedPoolDetailInputs() {
    return Array.prototype.slice.call(
      document.querySelectorAll('input[name="researchExactOriginPoolDetail"]')
    );
  }

  function selectedPoolDetails() {
    return selectedPoolDetailInputs()
      .filter(function (el) {
        return el.checked && !el.disabled;
      })
      .map(function (el) {
        return parseInt(el.value, 10);
      })
      .filter(function (pd) {
        return POOL_DETAIL_VALUES.indexOf(pd) >= 0;
      });
  }

  function selectedPoolTimeframe() {
    const el = document.querySelector('input[name="researchExactOriginPoolTimeframe"]:checked');
    return el ? String(el.value) : "5m";
  }

  /** Active released scopes (pool TF × checked PDs; unabhängig vom Chart-Candle-TF). */
  function activeScopesForPane(pane) {
    const poolTf = selectedPoolTimeframe();
    if (!poolTf) return [];
    return selectedPoolDetails()
      .filter(function (pd) {
        return isScopeReleased(poolTf, pd);
      })
      .map(function (pd) {
        return { timeframe: poolTf, pool_detail: pd };
      });
  }

  function primaryScopeForPane(pane) {
    const scopes = activeScopesForPane(pane);
    return scopes.length ? scopes[0] : null;
  }

  function membershipTfSecForPane(pane) {
    const AD = window.PD1ExactDensityTrpAdapter;
    const sc = primaryScopeForPane(pane);
    const tf = sc ? sc.timeframe : selectedPoolTimeframe();
    if (AD && AD.membershipSlotMinutesForTimeframe) {
      return AD.membershipSlotMinutesForTimeframe(tf) * 60;
    }
    if (tf === "5m") return 300;
    if (tf === "30m") return 1800;
    if (tf === "1h") return 3600;
    if (tf === "4h") return 14400;
    return 900;
  }

  function exactOriginScopeKeyForScope(sc) {
    const AD = window.PD1ExactDensityTrpAdapter;
    if (!sc) return "none";
    if (AD && AD.exactOriginScopeKey) return AD.exactOriginScopeKey(sc.timeframe, sc.pool_detail);
    return sc.timeframe + "\x1f" + sc.pool_detail;
  }

  function exactOriginScopeKeyForPane(pane) {
    const scopes = activeScopesForPane(pane);
    if (!scopes.length) return "none";
    return scopes
      .map(function (sc) {
        return exactOriginScopeKeyForScope(sc);
      })
      .sort()
      .join("+");
  }

  function chartTfDiffersFromPool(pane) {
    const chartTf = paneChartTimeframe(pane);
    const poolTf = selectedPoolTimeframe();
    return !!(chartTf && poolTf && chartTf !== poolTf);
  }

  const ON_DEMAND_BOUNDS_STORAGE_KEY = {
    "30m": "_onDemand30mPd1ViewerBounds",
    "1h": "_onDemand1hPd1ViewerBounds",
  };

  function onDemandPd1BoundsPatch(c, poolTf) {
    if (!c || !poolTf) return null;
    const key = ON_DEMAND_BOUNDS_STORAGE_KEY[String(poolTf)];
    if (!key) return null;
    const stored = c[key];
    return stored && stored.data_available_to ? stored : null;
  }

  function mergeOnDemandScopeIntoCaps(capsRoot, b, poolTf) {
    if (!capsRoot || !b || !b.data_available_to) return;
    const key = ON_DEMAND_BOUNDS_STORAGE_KEY[poolTf];
    if (!key) return;
    capsRoot[key] = {
      data_available_from: b.data_available_from,
      data_available_to: b.data_available_to,
      pipeline_line_id: b.pipeline_line_id,
      config_hash: b.config_hash,
      release_source: b.release_source || "on_demand_ch1m",
    };
    const scopes = capsRoot.supported_viewer_scopes || [];
    const idx = scopes.findIndex(function (s) {
      return s && s.timeframe === poolTf && Number(s.pool_detail) === 1;
    });
    const patch = {
      symbol: b.symbol || "DOGEUSDT",
      timeframe: poolTf,
      pool_detail: 1,
      pipeline_line_id: b.pipeline_line_id,
      config_hash: b.config_hash,
      data_available_from: b.data_available_from,
      data_available_to: b.data_available_to,
      release_source: b.release_source || "on_demand_ch1m",
    };
    if (idx >= 0) scopes[idx] = Object.assign({}, scopes[idx], patch);
    else scopes.push(patch);
    capsRoot.supported_viewer_scopes = scopes;
  }

  function doge5mPd1BoundsPatch(c) {
    if (!c) return null;
    const stored = c._doge5mPd1ViewerBounds;
    if (stored && stored.data_available_to) {
      const T = eoTr();
      if (T && T.enabled()) {
        T.event(null, "doge5mPd1BoundsPatch", {
          path: "caps_stored",
          from: T.shortIso(stored.data_available_from),
          to: T.shortIso(stored.data_available_to),
        });
      }
      return stored;
    }
    if (
      deps &&
      deps.SC_HELPERS &&
      deps.SC_HELPERS.doge5mPd1ViewerBoundsActive &&
      deps.SC_HELPERS.doge5mPd1ViewerBoundsActive() &&
      deps.SC_HELPERS.doge5mPd1ViewerBoundsIso
    ) {
      const iso = deps.SC_HELPERS.doge5mPd1ViewerBoundsIso();
      const T = eoTr();
      if (T && T.enabled() && iso) {
        T.event(null, "doge5mPd1BoundsPatch", {
          path: "sc_helpers_active",
          from: T.shortIso(iso.data_available_from),
          to: T.shortIso(iso.data_available_to),
        });
      }
      return iso;
    }
    const T0 = eoTr();
    if (T0 && T0.enabled()) {
      T0.event(null, "doge5mPd1BoundsPatch", { path: "none", result: "null" });
    }
    return null;
  }

  function scopeCaps(c, sc) {
    if (!sc) return c;
    let path = "root";
    let out = c;
    if (sc.timeframe === "5m" && Number(sc.pool_detail) === 1) {
      const dogeIso = doge5mPd1BoundsPatch(c);
      if (dogeIso && dogeIso.data_available_to) {
        path = "doge_5m_pd1_patch";
        out = Object.assign({}, c, {
          data_available_from: dogeIso.data_available_from,
          data_available_to: dogeIso.data_available_to,
          timeframe: "5m",
          pool_detail: 1,
          pipeline_line_id: dogeIso.pipeline_line_id || (c && c.pipeline_line_id),
          config_hash: dogeIso.config_hash || (c && c.config_hash),
        });
      }
    }
    if (path === "root" && (sc.timeframe === "30m" || sc.timeframe === "1h") && Number(sc.pool_detail) === 1) {
      const odIso = onDemandPd1BoundsPatch(c, sc.timeframe);
      if (odIso && odIso.data_available_to) {
        path = "on_demand_" + sc.timeframe + "_pd1_patch";
        out = Object.assign({}, c, {
          data_available_from: odIso.data_available_from,
          data_available_to: odIso.data_available_to,
          timeframe: sc.timeframe,
          pool_detail: 1,
          pipeline_line_id: odIso.pipeline_line_id,
          config_hash: odIso.config_hash,
        });
      }
    }
    if (path === "root") {
      const scopes = (c && c.supported_viewer_scopes) || [];
      const match = scopes.find(function (s) {
        return s && s.timeframe === sc.timeframe && Number(s.pool_detail) === sc.pool_detail;
      });
      if (!match) {
        path = "capabilities_root_fallback";
        out = c;
      } else {
        path = "capabilities_scope_match";
        out = Object.assign({}, c, {
          data_available_from: match.data_available_from,
          data_available_to: match.data_available_to,
          pipeline_line_id: match.pipeline_line_id,
          config_hash: match.config_hash,
          timeframe: match.timeframe,
          pool_detail: match.pool_detail,
        });
      }
    }
    const T = eoTr();
    if (T && T.enabled()) {
      const fromIso = out && out.data_available_from;
      const toIso = out && out.data_available_to;
      let boundHint = "dynamic";
      if (T.isAprSepStaticBound && (T.isAprSepStaticBound(fromIso) || T.isAprSepStaticBound(toIso))) {
        boundHint =
          path === "capabilities_scope_match"
            ? "apr_sep_from_capabilities_api"
            : path === "capabilities_root_fallback"
              ? "apr_sep_from_capabilities_root"
              : "apr_sep_like_dates";
      }
      T.event(null, "scopeCaps", {
        path: path,
        pool_tf: sc.timeframe,
        pd: sc.pool_detail,
        pipeline: (out && out.pipeline_line_id) || null,
        from: T.shortIso(fromIso),
        to: T.shortIso(toIso),
        bound_hint: boundHint,
        caps_cache: caps ? "warm" : "null",
      });
    }
    return out;
  }

  function referencePaneForPdUi() {
    if (!deps) return null;
    let found = null;
    deps.visibleIds().forEach(function (pid) {
      const p = deps.state.panes[pid];
      if (p && deps.state.symbol && String(deps.state.symbol).toUpperCase() === "DOGEUSDT") {
        found = p;
      }
    });
    return found;
  }

  function hasPoolTimeframeSelector() {
    return !!document.querySelector('input[name="researchExactOriginPoolTimeframe"]');
  }

  function syncPoolDetailUi(optPane) {
    const pane = optPane || referencePaneForPdUi();
    const poolTf = selectedPoolTimeframe();
    const chartTf = pane ? paneChartTimeframe(pane) : "";
    const unavailable =
      "Exact Origin Density: Pool-TF " + poolTf + " noch nicht freigegeben.";
    const hint = deps && deps.$("researchExactOriginDensityHint");
    if (hint && isEnabled() && !hasPoolTimeframeSelector() && chartTf === "1h") {
      hint.hidden = false;
      hint.textContent =
        "B – Exact Origin: Seite neu laden (Strg+Shift+R) — Pool-TF 5m/30m/1h fehlt in geladener UI.";
    }
    POOL_DETAIL_VALUES.forEach(function (pd) {
      const el = document.querySelector(
        'input[name="researchExactOriginPoolDetail"][value="' + String(pd) + '"]'
      );
      const wrap =
        pd === 1
          ? deps && deps.$("researchExactOriginPd1Wrap")
          : pd === 5
            ? deps && deps.$("researchExactOriginPd5Wrap")
            : deps && deps.$("researchExactOriginPd10Wrap");
      if (!el) return;
      const released = isScopeReleased(poolTf, pd);
      if (pd === 1) {
        // PD1: allow when released for independent pool TF (default 5m even on 1h chart).
        const pd1Ok = released || (poolTf === "5m" && isScopeReleased("5m", 1));
        el.disabled = !pd1Ok;
        if (!pd1Ok && el.checked) el.checked = false;
        if (wrap) wrap.title = pd1Ok ? "" : unavailable;
      } else {
        el.disabled = !released;
        if (!released && el.checked) el.checked = false;
        if (wrap) {
          wrap.title = released ? "" : "PD" + pd + ": für Pool-TF " + poolTf + " noch nicht freigegeben";
          wrap.classList.toggle("trp-muted", !released);
        }
      }
    });
    if (poolTf === "4h" && isScopeReleased("4h", 10)) {
      const pd10El = document.querySelector(
        'input[name="researchExactOriginPoolDetail"][value="10"]'
      );
      if (pd10El && !pd10El.disabled && !selectedPoolDetails().length) {
        pd10El.checked = true;
      }
    }
  }

  function invalidateScopeCaches() {
    caps = null;
    inflight.clear();
  }

  /** Fixed 48h UTC tiles for window cache (default OFF — sliding planner). */
  function isFixedTile48hEnabled() {
    try {
      return localStorage.getItem(EOD_FIXED_TILE_48H_FLAG_KEY) === "1";
    } catch (e) {
      return false;
    }
  }

  /** 30m/1h on-demand: stable UTC 72h tiles (API max chunk; better cache on pan). 5m unchanged. */
  function exactOriginWindowPlannerForScope(sc) {
    if (sc && (String(sc.timeframe) === "1h" || String(sc.timeframe) === "30m")) {
      return { mode: "fixed48h", tileHours: 72 };
    }
    if (isFixedTile48hEnabled()) {
      return { mode: "fixed48h", tileHours: 48 };
    }
    return { mode: "sliding", tileHours: null };
  }

  /** Sole canvas flag source — localStorage only (no host/deps callback; avoids recursion).
   * Default ON. Only the explicit value "0" keeps the DOM 900-zone path. */
  function isCanvasPathEnabled() {
    try {
      return localStorage.getItem(EOD_CANVAS_FLAG_KEY) !== "0";
    } catch (e) {
      return true;
    }
  }

  function viewportRenderKey(hit) {
    return [hit.from, hit.to].join("-");
  }

  function windowStoreForScope(pane, sc) {
    const sk = exactOriginScopeKeyForScope(sc);
    if (!pane.exactOriginWindowStores) pane.exactOriginWindowStores = {};
    if (!pane.exactOriginWindowStores[sk]) {
      pane.exactOriginWindowStores[sk] = { chunks: {}, order: [], replayGen: null };
    }
    return pane.exactOriginWindowStores[sk];
  }

  function resetAllWindowStores(pane) {
    if (!pane) return;
    pane.exactOriginWindowStore = { chunks: {}, order: [], replayGen: null };
    pane.exactOriginWindowStores = {};
    pane.exactOriginSegmentCache = null;
    pane.exactOriginSweepCache = null;
  }

  function maxConcurrentWindowFetch(scopes) {
    if (!scopes || !scopes.length) return MAX_CONCURRENT_WINDOW_FETCH;
    for (let i = 0; i < scopes.length; i += 1) {
      if (scopes[i].timeframe === "1h" || scopes[i].timeframe === "30m") {
        return MAX_CONCURRENT_WINDOW_FETCH_1H;
      }
    }
    return MAX_CONCURRENT_WINDOW_FETCH;
  }

  function chunkStoreEpoch(AD, store, planned) {
    if (!store || !planned || !planned.length) return null;
    const parts = [];
    for (let i = 0; i < planned.length; i += 1) {
      const w = planned[i];
      const ck = AD.exactOriginCacheKeyForWindow(w);
      const ent = store.chunks[ck];
      if (!ent || ent.status !== "ok" || !ent.pools) return null;
      parts.push(ck + ":" + ent.pools.length + ":" + ent.at);
    }
    return parts.join("|");
  }

  function exactOriginSweepCacheKey(scopeKey, slotMin, win, ext, epoch) {
    return (
      String(epoch) +
      "\x1f" +
      scopeKey +
      "\x1f" +
      slotMin +
      "\x1f" +
      win.sweepT0Ms +
      "\x1f" +
      win.sweepT1Ms +
      "\x1f" +
      ext.yMin.toFixed(12) +
      "\x1f" +
      ext.yMax.toFixed(12)
    );
  }

  function clearCanvasOnPane(pane) {
    const chart = deps && deps.api(pane);
    if (chart && chart.clearExactOriginDensity) chart.clearExactOriginDensity();
    if (pane) resetAllWindowStores(pane);
  }

  function getMinOrigins() {
    const slider = deps && deps.$("researchExactOriginMinDepth");
    if (slider && slider.value) {
      return Math.max(1, parseInt(slider.value, 10) || 1);
    }
    try {
      const stored = localStorage.getItem(MIN_ORIGINS_STORAGE_KEY);
      if (stored != null) return Math.max(1, parseInt(stored, 10) || 1);
    } catch (e) {
      /* ignore */
    }
    return 1;
  }

  function setMinOrigins(n) {
    const v = Math.max(1, Math.min(MIN_ORIGINS_SLIDER_MAX, parseInt(n, 10) || 1));
    const slider = deps && deps.$("researchExactOriginMinDepth");
    const label = deps && deps.$("researchExactOriginMinDepthValue");
    if (slider) slider.value = String(v);
    if (label) label.textContent = String(v);
    try {
      localStorage.setItem(MIN_ORIGINS_STORAGE_KEY, String(v));
    } catch (e) {
      /* ignore */
    }
    return v;
  }

  function filterForDisplay(segments) {
    const AD = window.PD1ExactDensityTrpAdapter;
    if (!AD || !AD.filterSegmentsByMinOrigins) return segments || [];
    return AD.filterSegmentsByMinOrigins(segments, getMinOrigins());
  }

  function pushCanvasDensity(pane, segments, metrics) {
    const chart = deps.api(pane);
    const unfiltered = segments || [];
    const filtered = filterForDisplay(unfiltered);
    mergeOverlays(pane, []);
    if (chart && chart.setExactOriginDensity) {
      const m = metrics ? Object.assign({}, metrics) : {};
      m.minOriginsFilter = getMinOrigins();
      m.segmentsUnfiltered = unfiltered.length;
      m.segmentsDisplayed = filtered.length;
      chart.setExactOriginDensity({ segments: filtered, metrics: m });
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
    if (!isEnabled() || !deps || !pane || !deps.state.symbol) return false;
    if (String(deps.state.symbol).toUpperCase() !== "DOGEUSDT") return false;
    return activeScopesForPane(pane).length > 0;
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

  const DOGE_5M_BOUNDS_REFRESH_MS = 30000;

  function applyDoge5mViewerBoundsToCaps(capRoot, b) {
    if (!capRoot || !b || !b.data_available_to) return;
    if (deps.SC_HELPERS && deps.SC_HELPERS.applyDoge5mPd1ViewerBounds) {
      deps.SC_HELPERS.applyDoge5mPd1ViewerBounds(b);
    }
    capRoot._doge5mPd1ViewerBounds = {
      data_available_from: b.data_available_from,
      data_available_to: b.data_available_to,
      pipeline_line_id: b.pipeline_line_id,
      config_hash: b.config_hash,
    };
    capRoot._doge5mBoundsFetchedAt = Date.now();
    const scopes = capRoot.supported_viewer_scopes || [];
    const idx = scopes.findIndex(function (s) {
      return s && s.timeframe === "5m" && Number(s.pool_detail) === 1;
    });
    const patch = {
      symbol: b.symbol || "DOGEUSDT",
      timeframe: "5m",
      pool_detail: 1,
      pipeline_line_id: b.pipeline_line_id,
      config_hash: b.config_hash,
      data_available_from: b.data_available_from,
      data_available_to: b.data_available_to,
    };
    if (idx >= 0) scopes[idx] = Object.assign({}, scopes[idx], patch);
    else scopes.push(patch);
    capRoot.supported_viewer_scopes = scopes;
    if (
      capRoot.timeframe === "5m" &&
      Number(capRoot.pool_detail) === 1 &&
      String(capRoot.symbol || "").toUpperCase() === "DOGEUSDT"
    ) {
      capRoot.data_available_from = b.data_available_from;
      capRoot.data_available_to = b.data_available_to;
    }
  }

  async function refreshDoge5mViewerBoundsIfStale(capRoot, traceId) {
    if (!capRoot || !capRoot._loadCapsReady) return capRoot;
    const now = Date.now();
    const last = capRoot._doge5mBoundsFetchedAt || 0;
    if (now - last < DOGE_5M_BOUNDS_REFRESH_MS) return capRoot;
    const T = eoTr();
    try {
      const bRes = await fetch("/api/research/doge-5m-pd1-viewer-bounds", {
        credentials: "same-origin",
      });
      if (T && T.enabled()) {
        T.event(traceId || traceIdForPane(referencePaneForPdUi()), "loadCaps", {
          doge_bounds_refresh: true,
          doge_bounds_http: bRes.status,
          doge_bounds_ok: bRes.ok,
        });
      }
      if (!bRes.ok) {
        capRoot._doge5mBoundsFetchedAt = now;
        return capRoot;
      }
      const body = await bRes.json().catch(function () {
        return null;
      });
      const b = body && body.data_available_to ? body : body && body.success ? body : null;
      if (b && b.data_available_to) applyDoge5mViewerBoundsToCaps(capRoot, b);
      else capRoot._doge5mBoundsFetchedAt = now;
    } catch (e) {
      capRoot._doge5mBoundsFetchedAt = now;
      if (T && T.enabled()) {
        T.event(traceId || traceIdForPane(referencePaneForPdUi()), "loadCaps", {
          doge_bounds_refresh_error: String(e && e.message ? e.message : e),
        });
      }
    }
    return capRoot;
  }

  async function loadCaps() {
    if (caps && caps._loadCapsReady) {
      const T = eoTr();
      const tid = traceIdForPane(referencePaneForPdUi()) || ensureEoTrace(referencePaneForPdUi());
      if (T && T.enabled()) {
        T.event(tid, "loadCaps", { cache: "HIT", caps_key: "module_caps_singleton" });
      }
      await refreshDoge5mViewerBoundsIfStale(caps, tid);
      return caps;
    }
    if (capsLoadInflight) {
      return capsLoadInflight;
    }
    capsLoadInflight = loadCapsOnce();
    try {
      return await capsLoadInflight;
    } finally {
      capsLoadInflight = null;
    }
  }

  async function loadCapsOnce() {
    const T = eoTr();
    const tid = traceIdForPane(referencePaneForPdUi()) || ensureEoTrace(referencePaneForPdUi());
    if (T && T.enabled()) {
      T.event(tid, "loadCaps", { cache: "MISS", fetch: VIEWER_CAP });
    }
    const t0 = typeof performance !== "undefined" ? performance.now() : 0;
    const res = await fetch(VIEWER_CAP, { credentials: "same-origin" });
    const nextCaps = await res.json().catch(() => ({}));
    caps = nextCaps;
    if (T && T.enabled()) {
      T.event(tid, "loadCaps", {
        capabilities_http: res.status,
        caps_source: "viewer_api",
        ms: typeof performance !== "undefined" ? Math.round(performance.now() - t0) : null,
      });
    }
  try {
    const bRes = await fetch("/api/research/doge-5m-pd1-viewer-bounds", {
      credentials: "same-origin",
    });
    if (T && T.enabled()) {
      T.event(tid, "loadCaps", {
        doge_bounds_http: bRes.status,
        doge_bounds_ok: bRes.ok,
      });
    }
    if (bRes.ok) {
      const b = await bRes.json().catch(function () {
        return null;
      });
      if (b && b.data_available_to) {
        applyDoge5mViewerBoundsToCaps(caps, b);
      }
    }
  } catch (e) {
    /* fail-closed: viewer caps only */
    if (T && T.enabled()) {
      T.event(tid, "loadCaps", { doge_bounds_error: String(e && e.message ? e.message : e) });
    }
  }
    try {
      const od30Res = await fetch(
        "/api/research/doge-pd1-on-demand-viewer-bounds?timeframe=30m&pool_detail=1",
        { credentials: "same-origin" }
      );
      if (T && T.enabled()) {
        T.event(tid, "loadCaps", {
          on_demand_30m_http: od30Res.status,
          on_demand_30m_ok: od30Res.ok,
        });
      }
      if (od30Res.ok) {
        const od30 = await od30Res.json().catch(function () {
          return null;
        });
        if (od30 && od30.data_available_to) {
          mergeOnDemandScopeIntoCaps(caps, od30, "30m");
        }
      } else if (od30Res.status === 422) {
        caps._onDemand30mPd1ViewerBounds = { unavailable: true };
      }
    } catch (odErr) {
      if (T && T.enabled()) {
        T.event(tid, "loadCaps", {
          on_demand_30m_error: String(odErr && odErr.message ? odErr.message : odErr),
        });
      }
    }
    if (T && T.enabled() && caps) {
      const sc0 =
        caps.supported_viewer_scopes && caps.supported_viewer_scopes.length
          ? caps.supported_viewer_scopes[0]
          : null;
      T.event(tid, "loadCaps", {
        done: true,
        root_from: T.shortIso(caps.data_available_from),
        root_to: T.shortIso(caps.data_available_to),
        doge_stored: caps._doge5mPd1ViewerBounds
          ? T.shortIso(caps._doge5mPd1ViewerBounds.data_available_to)
          : "none",
        scopes_n: (caps.supported_viewer_scopes || []).length,
        sample_scope: sc0
          ? sc0.timeframe + "/pd" + sc0.pool_detail + "@" + T.shortIso(sc0.data_available_to)
          : "none",
      });
    }
    caps._loadCapsReady = true;
    return caps;
  }

  function invalidateCapsCache() {
    const T = eoTr();
  if (T && T.enabled()) {
      T.event(traceIdForPane(referencePaneForPdUi()), "invalidateCapsCache", { caps: "cleared" });
    }
    caps = null;
    capsLoadInflight = null;
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
    const crossTf = chartTfDiffersFromPool(pane);
    if (!crossTf && deps.SC_HELPERS && deps.decisionContext && chart) {
      const ctx = deps.decisionContext(pane);
      const vp = deps.SC_HELPERS.resolveHistoricalLmv2ViewportCloseWindow(
        pane,
        membershipTfSecForPane(pane),
        ctx,
        chart
      );
      if (vp && !vp.viewport_after_replay_cutoff) {
        const fromClose =
          vp.display_from_close_unix != null ? vp.display_from_close_unix : vp.view_from_close_unix;
        const toClose =
          vp.display_to_close_unix != null ? vp.display_to_close_unix : vp.view_to_close_unix;
        const fromOpen = Math.floor(fromClose) - membershipTfSecForPane(pane);
        const toOpen = Math.floor(toClose);
        if (fromOpen < toOpen) {
          return { from: fromOpen, to: toOpen, source: "hist_viewport_resolver" };
        }
      }
    }
    if (crossTf && chart && chart.getVisibleTimeRange) {
      const vr = chart.getVisibleTimeRange();
      if (vr && vr.from != null && vr.to != null && Number(vr.from) < Number(vr.to)) {
        return {
          from: Math.floor(Number(vr.from)),
          to: Math.floor(Number(vr.to)),
          source: "chart_visible_cross_pool_tf",
        };
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
    const T = eoTr();
    if (T && T.enabled()) {
      T.event(null, "resolveViewSec", { result: "null", chart_pool_cross: chartTfDiffersFromPool(pane) });
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

  function buildWindows(viewSec, loMs, hiMs, traceId) {
    const T = eoTr();
    const hit = intersectViewWithData(viewSec, loMs, hiMs);
    if (!hit) {
      if (T && T.enabled()) {
        T.event(traceId, "buildWindows", {
          result: "NO_INTERSECTION",
          reason: "view_outside_data",
          view_from: T.isoFromUnix(viewSec && viewSec.from),
          view_to: T.isoFromUnix(viewSec && viewSec.to),
          bounds_from: T.isoFromMs(loMs),
          bounds_to: T.isoFromMs(hiMs),
          view_source: viewSec && viewSec.source,
        });
      }
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
      if (T && T.enabled()) {
        T.event(traceId, "buildWindows", {
          result: "NO_INTERSECTION",
          reason: "empty_intersection",
          api_from: T.isoFromMs(apiFrom),
          api_to: T.isoFromMs(apiTo),
        });
      }
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
    if (T && T.enabled()) {
      T.event(traceId, "buildWindows", {
        result: "OK",
        api_from: T.isoFromMs(apiFrom),
        api_to: T.isoFromMs(apiTo),
        hit_from: T.isoFromUnix(hit.from),
        hit_to: T.isoFromUnix(hit.to),
      });
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

  async function fetchPlannedWindows(
    AD,
    store,
    planned,
    loadSerial,
    reqReplayGen,
    pane,
    scope,
    retryErrors,
    maxConcurrent
  ) {
    const missing = [];
    for (let i = 0; i < planned.length; i += 1) {
      const w = planned[i];
      const ck = AD.exactOriginCacheKeyForWindow(w);
      const ent = store.chunks[ck];
      if (AD.exactOriginChunkSatisfiesWindow && AD.exactOriginChunkSatisfiesWindow(ent, w)) continue;
      if (ent && ent.status === "error" && !retryErrors) continue;
      missing.push(w);
    }
    if (!missing.length) {
      return { fetched: 0, failed: 0, cacheHits: planned.length };
    }
    if (loadSerial !== pane.exactOriginLoadSerial) {
      return { fetched: 0, failed: 0, cacheHits: planned.length, superseded: true };
    }
    let fetched = 0;
    let failed = 0;
    let cursor = 0;
    async function worker() {
      while (cursor < missing.length) {
        if (loadSerial !== pane.exactOriginLoadSerial) return;
        const idx = cursor;
        cursor += 1;
        const w = missing[idx];
        const ck = AD.exactOriginCacheKeyForWindow(w);
        try {
          const data = await fetchWindow(w.fromMs, w.toMs, scope, traceIdForPane(pane));
          if (reqReplayGen != null && deps.state.replayGen !== reqReplayGen) return;
          if (loadSerial !== pane.exactOriginLoadSerial) return;
          AD.touchExactOriginWindowCache(store, ck, {
            status: "ok",
            pools: data.pools || [],
            fromMs: w.fromMs,
            toMs: w.toMs,
            at: Date.now(),
          });
          if (loadSerial === pane.exactOriginLoadSerial) fetched += 1;
        } catch (err) {
          if (reqReplayGen != null && deps.state.replayGen !== reqReplayGen) return;
          if (loadSerial !== pane.exactOriginLoadSerial) return;
          AD.touchExactOriginWindowCache(store, ck, {
            status: "error",
            error: String(err && err.message ? err.message : err),
            fromMs: w.fromMs,
            toMs: w.toMs,
            at: Date.now(),
          });
          if (loadSerial === pane.exactOriginLoadSerial) failed += 1;
        }
      }
    }
    const workers = [];
    const conc = maxConcurrent == null ? MAX_CONCURRENT_WINDOW_FETCH : maxConcurrent;
    const n = Math.min(conc, missing.length);
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

  function clipWindowMsToScopeDataBounds(fromMs, toMs, scope) {
    const c = scopeCaps(caps, scope);
    if (!c) return { ok: true, fromMs: fromMs, toMs: toMs, clipped: false };
    let from = fromMs;
    let to = toMs;
    let clipped = false;
    if (c.data_available_from) {
      const lo = Date.parse(c.data_available_from);
      if (Number.isFinite(lo) && from < lo) {
        from = lo;
        clipped = true;
      }
    }
    if (c.data_available_to) {
      const hi = Date.parse(c.data_available_to);
      if (Number.isFinite(hi) && to > hi) {
        to = hi;
        clipped = true;
      }
    }
    if (from >= to) return { ok: false, clipped: clipped, reason: "empty_after_data_bounds_clip" };
    return { ok: true, fromMs: from, toMs: to, clipped: clipped };
  }

  async function fetchWindow(fromMs, toMs, scope, traceId) {
    const sc = scope;
    if (!sc) throw new Error("fetchWindow requires scope");
    const clip = clipWindowMsToScopeDataBounds(fromMs, toMs, sc);
    if (!clip.ok) throw new Error(clip.reason || "window empty after data bounds clip");
    fromMs = clip.fromMs;
    toMs = clip.toMs;
    const key = exactOriginScopeKeyForScope(sc) + "-" + fromMs + "-" + toMs;
    if (inflight.has(key)) return inflight.get(key);
    const clientTraceId = traceId || ensureEoTrace(referencePaneForPdUi());
    const T = eoTr();
    const t0 = typeof performance !== "undefined" ? performance.now() : 0;
    if (T && T.enabled()) {
      if (clip.clipped) {
        T.event(clientTraceId, "window_http_clip", {
          from: isoFromMs(fromMs),
          to: isoFromMs(toMs),
          pool_tf: sc.timeframe,
        });
      }
      T.event(clientTraceId, "window_http_request", {
        cache_key: key,
        from: isoFromMs(fromMs),
        to: isoFromMs(toMs),
        pool_tf: sc.timeframe,
        pd: sc.pool_detail,
      });
    }
    const run = fetch(API_WINDOW, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        from_utc: isoFromMs(fromMs),
        to_utc: isoFromMs(toMs),
        timeframe: sc.timeframe,
        pool_detail: sc.pool_detail,
        response_mode: "exact_origin_v1",
        trace_id: clientTraceId,
      }),
    })
      .then(async (res) => {
        const data = await res.json().catch(() => ({}));
        if (T && T.enabled()) {
          const pools = (data && data.pools) || [];
          T.event(clientTraceId, "window_http_response", {
            http: res.status,
            ms: typeof performance !== "undefined" ? Math.round(performance.now() - t0) : null,
            pools_n: pools.length,
            server_trace: data.request_id || data.trace_id || null,
          });
        }
        if (!res.ok) throw new Error(data.message || data.detail || "window " + res.status);
        return data;
      })
      .catch(function (err) {
        if (T && T.enabled()) {
          T.event(clientTraceId, "window_http_error", {
            cache_key: key,
            from: isoFromMs(fromMs),
            to: isoFromMs(toMs),
            ms: typeof performance !== "undefined" ? Math.round(performance.now() - t0) : null,
            message: String(err && err.message ? err.message : err),
          });
        }
        throw err;
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

  function dataBoundsMsForScopes(capsRoot, scopes) {
    let loMs = 0;
    let hiMs = Number.MAX_SAFE_INTEGER;
    scopes.forEach(function (sc) {
      const c = scopeCaps(capsRoot, sc);
      if (c && c.data_available_from) {
        const lo = Date.parse(c.data_available_from);
        if (Number.isFinite(lo)) loMs = loMs ? Math.max(loMs, lo) : lo;
      }
      if (c && c.data_available_to) {
        const hi = Date.parse(c.data_available_to);
        if (Number.isFinite(hi)) hiMs = Math.min(hiMs, hi);
      }
    });
    return { loMs: loMs || 0, hiMs: hiMs };
  }

  function mergeEoRefreshOpts(prev, next) {
    const a = prev || {};
    const b = next || {};
    const merged = {
      force: !!(a.force || b.force),
      viewport: !!(a.viewport || b.viewport),
    };
    if (b.fromUnix != null) merged.fromUnix = b.fromUnix;
    else if (a.fromUnix != null) merged.fromUnix = a.fromUnix;
    if (b.toUnix != null) merged.toUnix = b.toUnix;
    else if (a.toUnix != null) merged.toUnix = a.toUnix;
    return merged;
  }

  function eoRefreshPendingSupersedesInflight(pending) {
    const p = pending || {};
    return !!(p.viewport || p.force || (p.fromUnix != null && p.toUnix != null));
  }

  function bumpExactOriginLoadSerial(pane, reason) {
    if (!pane) return 0;
    pane.exactOriginLoadSerial = (pane.exactOriginLoadSerial || 0) + 1;
    const T = eoTr();
    if (T && T.enabled()) {
      T.event(traceIdForPane(pane), "exactOrigin_load_serial_bump", {
        reason: reason || "unspecified",
        load_serial: pane.exactOriginLoadSerial,
      });
    }
    return pane.exactOriginLoadSerial;
  }

  async function refreshPane(pane, opts) {
    if (!pane) return;
    if (!pane._eoRefreshGate) {
      pane._eoRefreshGate = { pending: null, inflight: null };
    }
    const gate = pane._eoRefreshGate;
    gate.pending = mergeEoRefreshOpts(gate.pending, opts);
    if (gate.inflight) {
      if (eoRefreshPendingSupersedesInflight(gate.pending)) {
        bumpExactOriginLoadSerial(pane, "coalesced_viewport_or_force");
      }
      const Tc = eoTr();
      if (Tc && Tc.enabled()) {
        Tc.event(traceIdForPane(pane), "refreshPane_coalesced", {
          pending_force: !!(gate.pending && gate.pending.force),
          pending_viewport: !!(gate.pending && gate.pending.viewport),
          pending_from_unix:
            gate.pending && gate.pending.fromUnix != null ? gate.pending.fromUnix : null,
          pending_to_unix: gate.pending && gate.pending.toUnix != null ? gate.pending.toUnix : null,
          load_serial: pane.exactOriginLoadSerial,
        });
      }
      return gate.inflight;
    }
    const runOpts = gate.pending || {};
    gate.pending = null;
    gate.inflight = refreshPaneImpl(pane, runOpts).finally(function () {
      gate.inflight = null;
      if (gate.pending) {
        const nextOpts = gate.pending;
        gate.pending = null;
        return refreshPane(pane, nextOpts);
      }
    });
    return gate.inflight;
  }

  async function refreshPaneImpl(pane, opts) {
    if (!isEnabled()) return;
    const T = eoTr();
    const tid = ensureEoTrace(pane);
    if (T && T.enabled() && tid === pane._eoTraceId) {
      T.activationHelp();
      T.event(tid, "refreshPane_start", traceContext(pane));
    }
    syncPoolDetailUi(pane);
    const scopes = activeScopesForPane(pane);
    if (!scopes.length) {
      clearCanvasOnPane(pane);
      mergeOverlays(pane, []);
      const hintOff = deps.$("researchExactOriginDensityHint");
      if (hintOff && paneChartTimeframe(pane)) {
        hintOff.hidden = false;
        hintOff.textContent =
          "B – Exact Origin: kein freigegebenes PD für Pool-TF " +
          selectedPoolTimeframe() +
          " (PD aktivieren oder Pool-TF wechseln)";
      }
      return;
    }
    if (!paneSupports(pane)) return;
    const loadSerial = (pane.exactOriginLoadSerial || 0) + 1;
    pane.exactOriginLoadSerial = loadSerial;
    const reqReplayGen =
      deps.isHistoricalReplay && deps.isHistoricalReplay() ? deps.state.replayGen : null;

    const AD = window.PD1ExactDensityTrpAdapter;
    if (!AD) throw new Error("PD1ExactDensityTrpAdapter missing");

    const tAdapter0 = typeof performance !== "undefined" ? performance.now() : 0;
    const viewSec = resolveViewSec(pane, opts);
    if (T && T.enabled()) {
      T.event(tid, "resolveViewSec", {
        cross_tf: chartTfDiffersFromPool(pane),
        source: viewSec ? viewSec.source : "null",
        from: viewSec ? T.isoFromUnix(viewSec.from) : null,
        to: viewSec ? T.isoFromUnix(viewSec.to) : null,
      });
    }
    const capsRoot = await loadCaps();
    const bounds = dataBoundsMsForScopes(capsRoot, scopes);
    const loMs = bounds.loMs;
    const hiMs = bounds.hiMs;
    if (T && T.enabled()) {
      T.event(tid, "scope_bounds", {
        lo: T.isoFromMs(loMs),
        hi: T.isoFromMs(hiMs),
        scopes: scopes.map(function (s) {
          return s.timeframe + "/pd" + s.pool_detail;
        }).join(","),
      });
    }

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

    const win = buildWindows(viewSec, loMs, hiMs, tid);
    if (!win.ok) {
      const hint = deps.$("researchExactOriginDensityHint");
      if (hint) {
        hint.hidden = false;
        const cap0 = scopeCaps(capsRoot, scopes[0]);
        const loIso = cap0 && cap0.data_available_from ? cap0.data_available_from.slice(0, 10) : "?";
        const hiIso = cap0 && cap0.data_available_to ? cap0.data_available_to.slice(0, 10) : "?";
        hint.textContent =
          "B – Exact Origin: kein Schnitt mit historischen Daten (" +
          loIso +
          " … " +
          hiIso +
          ") · Quelle " +
          viewSec.source;
        if (T && T.enabled()) {
          T.event(tid, "ui_hint_no_intersection", {
            result: "NO_INTERSECTION",
            hint_caps_from: loIso,
            hint_caps_to: hiIso,
            win_reason: win.reason,
            chart: traceContext(pane).chart,
            pool: traceContext(pane).pool,
          });
        }
        if (
          selectedPoolTimeframe() === "30m" &&
          capsRoot &&
          capsRoot._onDemand30mPd1ViewerBounds &&
          capsRoot._onDemand30mPd1ViewerBounds.unavailable
        ) {
          hint.textContent =
            "B – Exact Origin: 30m On-Demand Bounds nicht verfügbar (API) — kein 15m-Fallback";
        } else if (
          selectedPoolTimeframe() === "30m" &&
          loIso === "2026-04-01" &&
          hiIso.indexOf("2026-09") === 0
        ) {
          hint.textContent =
            "B – Exact Origin: 30m — veraltete Caps (Apr–Sep); On-Demand-Bounds nicht geladen · Hard-Refresh";
        }
      }
      clearCanvasOnPane(pane);
      mergeOverlays(pane, []);
      lastMetrics = { ok: false, reason: win.reason, view: viewSec, win };
      return;
    }

    const hitFromMs = win.hit.from * 1000;
    const hitToMs = win.hit.to * 1000;
    const canvasOn = isCanvasPathEnabled();
    const replayGenNow =
      deps.isHistoricalReplay && deps.isHistoricalReplay() ? deps.state.replayGen : null;
    scopes.forEach(function (sc) {
      const store = windowStoreForScope(pane, sc);
      if (store.replayGen !== replayGenNow) {
        const sk = exactOriginScopeKeyForScope(sc);
        pane.exactOriginWindowStores[sk] = { chunks: {}, order: [], replayGen: replayGenNow };
        pane.exactOriginSweepCache = null;
      }
    });
    const loadSpan = AD.computeDensityLoadSpanMs(hitFromMs, hitToMs, loMs, hiMs);
    const scopePlans = scopes.map(function (sc) {
      const scopeKey = exactOriginScopeKeyForScope(sc);
      const planner = exactOriginWindowPlannerForScope(sc);
      return {
        scope: sc,
        scopeKey: scopeKey,
        store: windowStoreForScope(pane, sc),
        planned: AD.planExactOriginLoadWindows(loadSpan.fromMs, loadSpan.toMs, loMs, hiMs, {
          mode: planner.mode,
          tileHours: planner.tileHours,
          scopeKey: scopeKey,
        }),
      };
    });
    const metricsWindowPlanner = exactOriginWindowPlannerForScope(scopes[0]);
    const metricsFixedTile48h = isFixedTile48hEnabled();
    const renderKey = viewportRenderKey(win.hit) + "|" + exactOriginScopeKeyForPane(pane);
    const fullyCached =
      !(opts && opts.force) &&
      scopePlans.every(function (sp) {
        return AD.exactOriginWindowsFullyCached(sp.store, sp.planned);
      });
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

    const fetchStats = { fetched: 0, failed: 0, cacheHits: 0 };
    const maxFetch = maxConcurrentWindowFetch(scopes);
    for (let si = 0; si < scopePlans.length; si += 1) {
      const sp = scopePlans[si];
      const one = await fetchPlannedWindows(
        AD,
        sp.store,
        sp.planned,
        loadSerial,
        reqReplayGen,
        pane,
        sp.scope,
        !!(opts && (opts.viewport || opts.force)),
        maxFetch
      );
      fetchStats.fetched += one.fetched;
      fetchStats.failed += one.failed;
      fetchStats.cacheHits += one.cacheHits;
      if (loadSerial !== pane.exactOriginLoadSerial) {
        const Tsup = eoTr();
        if (Tsup && Tsup.enabled()) {
          Tsup.event(traceIdForPane(pane), "async_superseded", {
            reason: "loadSerial_mismatch_after_fetch",
            load_serial: loadSerial,
            pane_serial: pane.exactOriginLoadSerial,
            p5k_case: traceContext(pane).p5k_case,
          });
        }
        scheduleSupersededDensityRefresh(pane, opts);
        return;
      }
      if (reqReplayGen != null && deps.state.replayGen !== reqReplayGen) return;
    }

    let allPools = [];
    scopePlans.forEach(function (sp) {
      const pools = gatherPoolsFromStore(AD, sp.store, loadSpan.fromMs, loadSpan.toMs);
      allPools = allPools.concat(pools);
    });
    if (!allPools.length && fetchStats.failed > 0 && fetchStats.fetched === 0) {
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
    const ext = allPools.length
      ? {
          yMin: Math.min(...allPools.map((p) => p.bottom)) * 0.9995,
          yMax: Math.max(...allPools.map((p) => p.top)) * 1.0005,
        }
      : { yMin: 0, yMax: 1 };

    const slotMin = AD.membershipSlotMinutesForTimeframe
      ? AD.membershipSlotMinutesForTimeframe(scopes[0].timeframe)
      : scopes[0].timeframe === "5m"
        ? 5
        : 15;

    let zones = [];
    let mergedSegments = [];
    let rawZoneCount = 0;
    let sweepCacheHit = false;
    const vpBounds = AD.computeViewportCullBounds(win.hit.from, win.hit.to, ext.yMin, ext.yMax);
  if (canvasOn && scopePlans.length === 1 && !(opts && opts.force)) {
      const sp0 = scopePlans[0];
      const epoch = chunkStoreEpoch(AD, sp0.store, sp0.planned);
      const sk0 = sp0.scopeKey;
      const sweepKey =
        epoch != null ? exactOriginSweepCacheKey(sk0, slotMin, win, ext, epoch) : null;
      if (
        sweepKey &&
        pane.exactOriginSweepCache &&
        pane.exactOriginSweepCache.key === sweepKey &&
        pane.exactOriginSweepCache.zones
      ) {
        sweepCacheHit = true;
        rawZoneCount = pane.exactOriginSweepCache.rawZoneCount || pane.exactOriginSweepCache.zones.length;
        mergedSegments = AD.mergeExactOriginSegments(
          AD.cullZonesToViewport(pane.exactOriginSweepCache.zones, vpBounds)
        );
      }
    }
    if (!sweepCacheHit) {
      scopePlans.forEach(function (sp) {
        const pools = gatherPoolsFromStore(AD, sp.store, loadSpan.fromMs, loadSpan.toMs);
        const z = AD.poolsToExactOriginZones(pools, loadSpan.fromMs, loadSpan.toMs, ext.yMin, ext.yMax, {
          sweepT0Ms: win.sweepT0Ms,
          sweepT1Ms: win.sweepT1Ms,
          slotMinutes: slotMin,
        });
        rawZoneCount += z.length;
        if (canvasOn) {
          const culled = AD.cullZonesToViewport(z, vpBounds);
          mergedSegments = mergedSegments.concat(AD.mergeExactOriginSegments(culled));
          if (scopePlans.length === 1 && !(opts && opts.force)) {
            const epoch = chunkStoreEpoch(AD, sp.store, sp.planned);
            if (epoch != null) {
              pane.exactOriginSweepCache = {
                key: exactOriginSweepCacheKey(sp.scopeKey, slotMin, win, ext, epoch),
                zones: z,
                rawZoneCount: rawZoneCount,
              };
            }
          }
        } else {
          zones = zones.concat(z);
        }
      });
    }
    const tAdapter1 = typeof performance !== "undefined" ? performance.now() : 0;

    let culledCount = rawZoneCount;
    let cullMs = 0;
    let mergeMs = 0;
    const cap = maxChartZones();
    let capped = { zones: zones, capped: false, dropped: 0 };
    let registeredZones = canvasOn ? mergedSegments.length : zones.length;

    if (canvasOn) {
      culledCount = mergedSegments.length;
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

    if (loadSerial !== pane.exactOriginLoadSerial) {
      const Tsup2 = eoTr();
      if (Tsup2 && Tsup2.enabled()) {
        Tsup2.event(traceIdForPane(pane), "async_superseded", {
          reason: "loadSerial_mismatch_before_render",
          load_serial: loadSerial,
          pane_serial: pane.exactOriginLoadSerial,
          p5k_case: traceContext(pane).p5k_case,
        });
      }
      scheduleSupersededDensityRefresh(pane, opts);
      return;
    }
    if (reqReplayGen != null && deps.state.replayGen !== reqReplayGen) return;

    if (canvasOn) {
      pushCanvasDensity(pane, mergedSegments, null);
    } else {
      clearCanvasOnPane(pane);
      mergeOverlays(pane, filterForDisplay(zones));
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
      pools: allPools.length,
      activeScopes: scopes.map(function (sc) {
        return sc.timeframe + " PD" + sc.pool_detail;
      }),
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
      sweepCacheHit: sweepCacheHit,
      version: VERSION,
      windowPlanner: metricsWindowPlanner,
      fixedTile48h: metricsFixedTile48h,
      windowsPlanned: scopePlans.reduce(function (sum, sp) {
        return sum + (sp.planned ? sp.planned.length : 0);
      }, 0),
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
      pane.exactOriginSegmentCache = {
        key: renderKey,
        segments: mergedSegments,
        zones: null,
        metrics: lastMetrics,
      };
    } else {
      pane.exactOriginSegmentCache = {
        key: renderKey,
        segments: null,
        zones: zones,
        metrics: lastMetrics,
      };
    }
    pane.exactOriginLastMetrics = lastMetrics;
    const minN = getMinOrigins();
    const beforeFilter = canvasOn ? mergedSegments.length : zones.length;
    if (T && T.enabled()) {
      T.event(tid, "refreshPane_render", {
        result: "OK",
        p5k_case: traceContext(pane).p5k_case,
        pools_raw: allPools.length,
        segments_before_min_depth: beforeFilter,
        segments_merged: canvasOn ? registeredZones : null,
        zones_overlay: canvasOn ? null : registeredZones,
        min_origins: minN,
        fetch_fetched: fetchStats.fetched,
        fetch_failed: fetchStats.failed,
        fetch_cache_hits: fetchStats.cacheHits,
        windows_planned: lastMetrics.windowsPlanned,
        api_from: lastMetrics.apiFromIso,
        api_to: lastMetrics.apiToIso,
        canvas: canvasOn,
        canvas_drawn: lastMetrics.canvasSegmentsDrawn,
        canvas_segments_total: lastMetrics.canvasSegmentsTotal,
      });
    }

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
          allPools.length +
          " pools · " +
          scopes.length +
          " PD · " +
          viewSec.source;
      } else {
        line =
          "B – Exact Origin · " +
          zones.length +
          " zones · " +
          allPools.length +
          " pools · " +
          scopes.length +
          " PD · " +
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
        hint.textContent =
          "B – Exact Origin: DOGEUSDT — Chart-Pane TF + freigegebenes PD (15m PD1/PD5/PD10; 5m PD1/PD5/PD10)";
      }
      stripAllPanes();
      return;
    }
    for (let i = 0; i < targets.length; i += 1) {
      await refreshPane(targets[i], opts);
    }
  }

  function scheduleSupersededDensityRefresh(pane, opts) {
    if (!pane) return;
    const T = eoTr();
    if (T && T.enabled()) {
      T.event(traceIdForPane(pane), "scheduleSupersededDensityRefresh", {
        p5k_case: traceContext(pane).p5k_case,
        delay_ms: 48,
      });
    }
    if (pane.exactOriginSupersedeTimer) clearTimeout(pane.exactOriginSupersedeTimer);
    pane.exactOriginSupersedeTimer = setTimeout(function () {
      pane.exactOriginSupersedeTimer = null;
      if (!isEnabled() || !paneSupports(pane)) return;
      refreshPane(pane, Object.assign({ viewport: true }, opts || {})).catch(function (err) {
        if (!(err && err.name === "AbortError") && deps && deps.setStatus) {
          deps.setStatus("Exact Origin: " + (err.message || err), "error");
        }
      });
    }, 48);
  }

  function scheduleViewport(pane, from, to) {
    if (!isEnabled() || !paneSupports(pane)) return;
    if (pane._suppressExactOriginViewport) return;
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

  function reapplyMinDepthFilterAllPanes() {
    if (!isEnabled() || !deps) return;
    deps.visibleIds().forEach(function (pid) {
      const pane = deps.state.panes[pid];
      repaintPaneFromCache(pane);
    });
  }

  function repaintPaneFromCache(pane) {
    if (!isEnabled() || !pane || !paneSupports(pane)) return false;
    const cache = pane.exactOriginSegmentCache;
    if (!cache) return false;
    if (isCanvasPathEnabled() && cache.segments) {
      pushCanvasDensity(pane, cache.segments, cache.metrics);
      return true;
    }
    if (cache.zones) {
      mergeOverlays(pane, filterForDisplay(cache.zones));
      return true;
    }
    return false;
  }

  function syncMinDepthUiFromStorage() {
    let v = 1;
    try {
      const stored = localStorage.getItem(MIN_ORIGINS_STORAGE_KEY);
      if (stored != null) v = Math.max(1, parseInt(stored, 10) || 1);
    } catch (e) {
      /* ignore */
    }
    setMinOrigins(v);
  }

  function ensureMinDepthInPaneToolbar() {
    const depthWrap = deps && deps.$("researchExactOriginMinDepthWrap");
    const nav = deps && deps.$("trpPaneNavControls");
    const layouts = deps && deps.$("trpLayouts");
    if (!depthWrap || !nav || !layouts) return;
    if (depthWrap.parentElement !== nav) nav.appendChild(depthWrap);
    if (depthWrap.previousElementSibling !== layouts) {
      layouts.insertAdjacentElement("afterend", depthWrap);
    }
    if (deps.mountDrawToolbar) deps.mountDrawToolbar();
  }

  function updateUiChrome() {
    const on = isEnabled();
    const wrap = deps && deps.$("researchExactOriginDensityWrap");
    const hint = deps && deps.$("researchExactOriginDensityHint");
    const depthWrap = deps && deps.$("researchExactOriginMinDepthWrap");
    const pdWrap = deps && deps.$("researchExactOriginPdWrap");
    const poolTfWrap = deps && deps.$("researchExactOriginPoolTfWrap");
    if (wrap) wrap.classList.toggle("exact-origin-active", on);
    if (on) ensureMinDepthInPaneToolbar();
    if (depthWrap) depthWrap.hidden = !on;
    if (pdWrap) pdWrap.hidden = !on;
    if (poolTfWrap) poolTfWrap.hidden = !on;
    if (on) syncPoolDetailUi(referencePaneForPdUi());
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
        syncPoolDetailUi(referencePaneForPdUi());
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
    syncMinDepthUiFromStorage();
    ensureMinDepthInPaneToolbar();
    updateUiChrome();
    const depthSlider = deps.$("researchExactOriginMinDepth");
    const depthReset = deps.$("researchExactOriginMinDepthReset");
    if (depthSlider) {
      depthSlider.addEventListener("input", function () {
        setMinOrigins(depthSlider.value);
        reapplyMinDepthFilterAllPanes();
      });
    }
    if (depthReset) {
      depthReset.addEventListener("click", function () {
        setMinOrigins(1);
        reapplyMinDepthFilterAllPanes();
      });
    }
    selectedPoolDetailInputs().forEach(function (el) {
      el.addEventListener("change", async function () {
        if (!isEnabled()) return;
        invalidateScopeCaches();
        stripAllPanes();
        try {
          await refreshAll({ force: true });
        } catch (err) {
          deps.setStatus("Exact Origin: " + (err.message || err), "error");
        }
      });
    });
    Array.prototype.slice
      .call(document.querySelectorAll('input[name="researchExactOriginPoolTimeframe"]'))
      .forEach(function (el) {
        el.addEventListener("change", async function () {
          if (!isEnabled()) return;
          invalidateScopeCaches();
          stripAllPanes();
          syncPoolDetailUi(referencePaneForPdUi());
          try {
            await refreshAll({ force: true });
          } catch (err) {
            deps.setStatus("Exact Origin Pool-TF: " + (err.message || err), "error");
          }
        });
      });
    updateUiChrome();
  }

  function onPaneTimeframeChange(pane) {
    if (!isEnabled()) return;
    const T = eoTr();
    if (pane) pane._eoTraceId = null;
    const tid = ensureEoTrace(pane);
    if (T && T.enabled()) {
      T.event(tid, "onPaneTimeframeChange", traceContext(pane));
    }
    syncPoolDetailUi(pane);
    invalidateCapsCache();
    invalidateScopeCaches();
    stripAllPanes();
    if (paneSupports(pane)) {
      refreshPane(pane, { force: true }).catch(function (err) {
        if (deps.setStatus) deps.setStatus("Exact Origin: " + (err.message || err), "error");
      });
    }
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
    exactOriginWindowPlannerForScope: exactOriginWindowPlannerForScope,
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
    getMinOrigins: getMinOrigins,
    setMinOrigins: setMinOrigins,
    filterSegmentsByMinOrigins: function (segments, minOrigins) {
      const AD = window.PD1ExactDensityTrpAdapter;
      return AD && AD.filterSegmentsByMinOrigins
        ? AD.filterSegmentsByMinOrigins(segments, minOrigins)
        : segments || [];
    },
    reapplyMinDepthFilterAllPanes: reapplyMinDepthFilterAllPanes,
    repaintPaneFromCache: repaintPaneFromCache,
    MIN_ORIGINS_STORAGE_KEY: MIN_ORIGINS_STORAGE_KEY,
    syncPoolDetailUi: syncPoolDetailUi,
    onPaneTimeframeChange: onPaneTimeframeChange,
    isScopeReleased: isScopeReleased,
    activeScopesForPane: activeScopesForPane,
    invalidateCapsCache: invalidateCapsCache,
    selectedPoolTimeframe: selectedPoolTimeframe,
  };
})();
