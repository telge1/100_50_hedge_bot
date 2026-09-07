/* Anchored market profile page.
 *
 * Candles come from lightweight-charts; every profile element is drawn on a
 * canvas above it. Anchored profiles cannot be expressed as chart series
 * because each one occupies its own horizontal slice of the time axis, so the
 * overlay maps price->y through the series and time->x through logical bar
 * indices, which stay valid when a window scrolls off screen.
 */
(function () {
  "use strict";

  // Cache-bust: mp-11 Orderbook Walls + Levels (research 1:1) on Market Profile.
  try { console.info("[mp] asset mp-11"); } catch (e) { /* ignore */ }

  var STORAGE_KEY = "mp_v1_settings";

  var COLORS = {
    buy: "rgba(38, 166, 154, 0.50)",
    sell: "rgba(239, 83, 80, 0.50)",
    total: "rgba(120, 144, 176, 0.20)",
    poc: "#ef4444",
    valueArea: "#3b82f6",
    nakedPoc: "#f0b90b",
    volumeLine: "rgba(255, 255, 255, 0.92)",
    hvn: "#a855f7",
    lvn: "#64748b",
    singlePrint: "rgba(239, 83, 80, 0.10)",
    windowEdge: "rgba(120, 130, 150, 0.14)",
    vaFill: "rgba(59, 130, 246, 0.0)"
  };

  var SHAPE_COLORS = {
    BALANCE: "#26a69a",
    TREND_UP: "#4caf50",
    TREND_DOWN: "#ef5350",
    DOUBLE_DISTRIBUTION: "#ab47bc",
    UNCLEAR: "#787b86"
  };

  var CHART_H_KEY = "mp_v1.chart_h";
  var CHART_MT_KEY = "mp_v1.chart_mt";
  var DEFAULT_CHART_H = 640;

  var chart = null;
  var candleSeries = null;
  var payload = null;
  var candleTimes = [];
  var inflight = null;
  var rafPending = false;
  var drawDebounceTimer = null;
  var chartReady = false;
  var workspace = null;
  var emaDraft = null;
  var drawingPayloads = {};
  var lldPayloads = {};
  var appliedOverlayPayloads = {};
  var lastEmaPayload = null;
  var lastLoadRange = null;
  var livePollGen = 0;
  var formingTimer = null;
  var formingInflight = false;
  var FORMING_MS = 250;
  var TF_SEC = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900,
    "30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400, "1d": 86400
  };

  var TOOLS = [
    ["select", "Auswählen"],
    ["trend", "Trendlinie"],
    ["hline", "Horizontale Linie"],
    ["vline", "Vertikale Linie"],
    ["rectangle", "Rechteck"],
    ["circle", "Kreis"],
    ["arrow", "Pfeil"],
    ["measure", "Messen"],
    ["long_position", "Long-Position"],
    ["short_position", "Short-Position"]
  ];

  var TOOL_ICONS = {
    select: "↖",
    trend: "/",
    hline: "—",
    vline: "|",
    rectangle: "▭",
    circle: "○",
    arrow: "→",
    measure: "⤢",
    long_position: "L",
    short_position: "S"
  };

  function $(id) {
    return document.getElementById(id);
  }

  function setStatus(text, kind) {
    var el = $("mpStatus");
    if (!el) return;
    el.textContent = text;
    el.className = "mp-status" + (kind ? " is-" + kind : "");
  }

  /* ---------------------------------------------------------------- settings */

  function readSettings() {
    return {
      symbol: $("mpSymbol").value,
      // Market-profile window type (periods / day / session / composite).
      // Kept as `anchor` in localStorage for back-compat; also mirrored as mpTimeframe.
      anchor: $("mpAnchor").value,
      mpTimeframe: $("mpAnchor").value,
      sessions: Array.prototype.slice
        .call(document.querySelectorAll(".mp-session:checked"))
        .map(function (el) { return el.value; }),
      days: $("mpDays").value,
      start: $("mpStart").value,
      end: $("mpEnd").value,
      // Candle resolution only — never written into MP controls.
      timeframe: $("mpTimeframe").value,
      showHistogram: $("mpShowHistogram").checked,
      showVolumeLine: $("mpShowVolumeLine") ? $("mpShowVolumeLine").checked : false,
      splitBuySell: $("mpSplitBuySell").checked,
      width: parseFloat($("mpWidth").value) || 0.45,
      showPoc: $("mpShowPoc").checked,
      showValueArea: $("mpShowValueArea").checked,
      showHvn: $("mpShowHvn").checked,
      showLvn: $("mpShowLvn").checked,
      showSinglePrints: $("mpShowSinglePrints").checked,
      showNakedPoc: $("mpShowNakedPoc").checked,
      extendLevels: $("mpExtendLevels").checked,
      showShape: $("mpShowShape").checked,
      showLiquidity: $("mpShowLiquidity").checked,
      valueAreaPct: parseFloat($("mpValueAreaPct").value) || 70,
      targetBins: parseInt($("mpTargetBins").value, 10) || 160,
      final: $("mpFinal").checked
    };
  }

  function persistSettings() {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(readSettings()));
    } catch (err) { /* private mode: settings simply do not persist */ }
  }

  function restoreSettings() {
    var raw;
    try {
      raw = window.localStorage.getItem(STORAGE_KEY);
    } catch (err) { return; }
    if (!raw) return;
    var s;
    try { s = JSON.parse(raw); } catch (err) { return; }
    if (!s || typeof s !== "object") return;

    function setVal(id, v) {
      var el = $(id);
      if (el && v !== undefined && v !== null && v !== "") el.value = String(v);
    }
    function setChk(id, v) {
      var el = $(id);
      if (el && typeof v === "boolean") el.checked = v;
    }

    setVal("mpSymbol", s.symbol);
    // Prefer explicit mpTimeframe; fall back to legacy `anchor` only.
    // Never copy candle `timeframe` into the MP control.
    var mpTf = s.mpTimeframe || s.anchor;
    if (mpTf) setVal("mpAnchor", mpTf);
    setVal("mpDays", s.days);
    setVal("mpStart", s.start);
    setVal("mpEnd", s.end);
    setVal("mpTimeframe", s.timeframe);
    setVal("mpWidth", s.width);
    setVal("mpValueAreaPct", s.valueAreaPct);
    setVal("mpTargetBins", s.targetBins);
    setChk("mpShowHistogram", s.showHistogram);
    setChk("mpShowVolumeLine", s.showVolumeLine);
    setChk("mpSplitBuySell", s.splitBuySell);
    setChk("mpShowPoc", s.showPoc);
    setChk("mpShowValueArea", s.showValueArea);
    setChk("mpShowHvn", s.showHvn);
    setChk("mpShowLvn", s.showLvn);
    setChk("mpShowSinglePrints", s.showSinglePrints);
    setChk("mpShowNakedPoc", s.showNakedPoc);
    setChk("mpExtendLevels", s.extendLevels);
    setChk("mpShowShape", s.showShape);
    setChk("mpShowLiquidity", s.showLiquidity);
    setChk("mpFinal", s.final);

    if (Array.isArray(s.sessions) && s.sessions.length) {
      Array.prototype.forEach.call(document.querySelectorAll(".mp-session"), function (el) {
        el.checked = s.sessions.indexOf(el.value) !== -1;
      });
    }
  }

  /* -------------------------------------------------------------------- range */

  function utcMidnight(d) {
    return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()) / 1000;
  }

  function resolveRange(s) {
    if (s.days === "custom") {
      if (!s.start || !s.end) return null;
      var a = Math.floor(Date.parse(s.start + "T00:00:00Z") / 1000);
      var b = Math.floor(Date.parse(s.end + "T00:00:00Z") / 1000) + 86400;
      if (!isFinite(a) || !isFinite(b) || b <= a) return null;
      return { start: a, end: b };
    }
    var days = parseInt(s.days, 10) || 7;
    // Anchor on UTC midnight so day windows are whole rather than shifted by
    // whatever time of day the page happens to be opened.
    var todayMidnight = utcMidnight(new Date());
    return { start: todayMidnight - (days - 1) * 86400, end: todayMidnight + 86400 };
  }

  /* --------------------------------------------------------------------- chart */

  function chartApi() {
    return window.chartApi || null;
  }

  function syncChartRefs() {
    var api = chartApi();
    if (!api || !api.getChart || !api.getCandleSeries) return false;
    chart = api.getChart();
    candleSeries = api.getCandleSeries();
    return !!(chart && candleSeries);
  }

  /* ---- ORDERBOOK Walls (left) + Levels (right) — research APIs 1:1 ---- */
  var OBP_KEY = "mp_v1.orderbook_profile";
  var OBL_KEY = "mp_v1.orderbook_levels.v3";
  var OBL1000_LEASE_KEY = "research.ob1000.lease_id";
  var OBP_REFRESH_MS = 2000;
  var OBL_REFRESH_MS = 5000;
  var OBL1000_REFRESH_MS = 2000;
  var OBL_FULL_REFRESH_MS = 2000;
  var OBL1000_HEARTBEAT_MS = 15000;
  var OBP_DEBOUNCE_MS = 400;

  var obState = {
    obp: { enabled: false, width: "normal", mode: "snapshot_at" },
    obl: { enabled: false, depth: 1000, mode: "aggregated", scale: "sqrt", width_px: 140 },
    obl1000: {
      leaseId: null,
      leaseSymbol: null,
      leaseDepth: null,
      leaseGen: 0,
      heartbeatTimer: null,
      uiState: "DISABLED"
    },
    obpRefreshTimer: null,
    oblRefreshTimer: null,
    obpGen: 0,
    oblGen: 0,
    obpTimer: null,
    oblTimer: null,
    obpAbort: null,
    oblAbort: null,
    oblInflight: false,
    _oblHadLive: false,
    _oblPaintSig: null
  };

  function currentSymbol() {
    return ($("mpSymbol") && $("mpSymbol").value) || "";
  }

  function currentCandleTf() {
    return ($("mpTimeframe") && $("mpTimeframe").value) || "15m";
  }

  function defaultOrderbookProfile() {
    return { enabled: false, width: "normal", mode: "snapshot_at" };
  }

  function defaultOrderbookLevels() {
    return { enabled: false, depth: 1000, mode: "aggregated", scale: "sqrt", width_px: 140 };
  }

  function readStoredOrderbookProfile() {
    try {
      var raw = localStorage.getItem(OBP_KEY);
      if (!raw) return defaultOrderbookProfile();
      var out = Object.assign(defaultOrderbookProfile(), JSON.parse(raw));
      out.mode = "snapshot_at";
      return out;
    } catch (e) {
      return defaultOrderbookProfile();
    }
  }

  function readStoredOrderbookLevels() {
    try {
      var raw = localStorage.getItem(OBL_KEY);
      if (!raw) return defaultOrderbookLevels();
      return Object.assign(defaultOrderbookLevels(), JSON.parse(raw));
    } catch (e) {
      return defaultOrderbookLevels();
    }
  }

  function persistOrderbookProfile() {
    try { localStorage.setItem(OBP_KEY, JSON.stringify(obState.obp)); } catch (e) { /* ignore */ }
  }

  function persistOrderbookLevels() {
    try { localStorage.setItem(OBL_KEY, JSON.stringify(obState.obl)); } catch (e) { /* ignore */ }
  }

  function oblDataDepth() {
    var d = obState.obl && obState.obl.depth != null ? Number(obState.obl.depth) : 1000;
    if (d === 200) return 200;
    if (d === 0) return 0;
    return 1000;
  }

  function isOnDemandBookMode() {
    var d = oblDataDepth();
    return !!(obState.obl && obState.obl.enabled && (d === 1000 || d === 0));
  }

  function oblDepthLabel(depth) {
    var d = depth != null ? Number(depth) : oblDataDepth();
    if (d === 0) return "FULL";
    if (d === 1000) return "OB1000";
    return "OB200";
  }

  function obpSettingsPayload() {
    return { enabled: !!(obState.obp && obState.obp.enabled), width: (obState.obp && obState.obp.width) || "normal" };
  }

  function oblSettingsPayload() {
    var obl = obState.obl || defaultOrderbookLevels();
    return {
      enabled: !!obl.enabled,
      depth: oblDataDepth(),
      mode: obl.mode === "raw" ? "raw" : "aggregated",
      scale: ["sqrt", "linear", "log"].indexOf(obl.scale) >= 0 ? obl.scale : "sqrt",
      width_px: Math.max(100, Math.min(220, Number(obl.width_px) || 140))
    };
  }

  function fillOrderbookProfileControls() {
    var en = $("mpObpEnabled");
    if (en) en.checked = !!(obState.obp && obState.obp.enabled);
    var legend = $("mpObpLegend");
    if (legend) legend.hidden = !(obState.obp && obState.obp.enabled);
  }

  function fillOrderbookLevelsControls() {
    var obl = obState.obl || defaultOrderbookLevels();
    var en = $("mpOblEnabled");
    if (en) en.checked = !!obl.enabled;
    var depth = $("mpOblDepth");
    if (depth) {
      var d = oblDataDepth();
      depth.value = d === 0 ? "0" : (d === 1000 ? "1000" : "200");
    }
    var mode = $("mpOblMode");
    if (mode) mode.value = obl.mode === "raw" ? "raw" : "aggregated";
    var scale = $("mpOblScale");
    if (scale) scale.value = ["sqrt", "linear", "log"].indexOf(obl.scale) >= 0 ? obl.scale : "sqrt";
    var wrap = $("mpOblControls");
    if (wrap) wrap.hidden = !obl.enabled;
  }

  function getJson(url, opts) {
    opts = opts || {};
    return fetch(url, {
      credentials: "same-origin",
      signal: opts.signal
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (body) {
        if (!res.ok) {
          var detail = body && (body.detail || body.message || body.error || body.code);
          var text = typeof detail === "string" && detail ? detail : (res.statusText || "request failed");
          throw new Error(res.status + " " + text);
        }
        return body;
      });
    });
  }

  function postJson(url, payload) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {})
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (body) {
        if (!res.ok) {
          var detail = body && (body.detail || body.message || body.error || body.code);
          var text = typeof detail === "string" && detail ? detail : (res.statusText || "request failed");
          throw new Error(res.status + " " + text);
        }
        return body;
      });
    });
  }

  function getOb1000LeaseId() {
    try {
      var id = sessionStorage.getItem(OBL1000_LEASE_KEY);
      if (!id) {
        id = "tab-" + Math.random().toString(36).slice(2) + "-" + Date.now();
        sessionStorage.setItem(OBL1000_LEASE_KEY, id);
      }
      return id;
    } catch (e) {
      return "tab-" + Date.now();
    }
  }

  function parseOb1000ApiError(err) {
    if (!err) return null;
    var msg = String(err.message || err);
    if (msg.indexOf("symbol_not_in_pilot") >= 0) return { code: "symbol_not_in_pilot", status: 400 };
    if (msg.indexOf("unknown_lease") >= 0 || msg.indexOf("no_active_lease") >= 0) {
      return { code: "unknown_lease", status: 409 };
    }
    if (msg.indexOf("disabled") >= 0) return { code: "disabled", status: 503 };
    if (msg.indexOf("collector_unavailable") >= 0 || msg.indexOf("503") >= 0) {
      return { code: "collector_unavailable", status: 503 };
    }
    if (msg.indexOf("capacity_reached") >= 0 || msg.indexOf("429") >= 0) {
      return { code: "capacity_reached", status: 429 };
    }
    return { code: "error", message: msg };
  }

  function computeOb1000UiState(payload, err) {
    if (!obState.obl || !obState.obl.enabled) return "DISABLED";
    if (!isOnDemandBookMode()) return "DISABLED";
    if (err && err.code === "symbol_not_in_pilot") return "NOT_PILOT";
    if (err && err.code === "disabled") return "DISABLED";
    if (err && (err.code === "collector_unavailable" || err.status === 503)) return "OFFLINE";
    if (err && err.code === "capacity_reached") return "CAPACITY";
    var sub = payload && payload.subscription_state ? String(payload.subscription_state) : "starting";
    if (sub === "capacity") return "CAPACITY";
    if (sub === "starting" || sub === "grace") return "STARTING";
    var fresh = payload && payload.freshness_state ? String(payload.freshness_state) : "unknown";
    var hasLevels = payload && ((payload.bids && payload.bids.length) || (payload.asks && payload.asks.length));
    if (!hasLevels) return sub === "live" ? "NO_DATA" : "STARTING";
    if (fresh === "stale") return "STALE";
    if (fresh === "delayed") return "DELAYED";
    if (fresh === "fresh") return "LIVE";
    return "NO_DATA";
  }

  function oblEmptyPayload(reqSymbol, depth, uiState) {
    return {
      symbol: reqSymbol,
      bids: [],
      asks: [],
      freshness_state: "unknown",
      freshness_ms: null,
      timestamp_utc: null,
      source: (depth === 1000 || depth === 0) ? "orderbook_v3_live_on_demand" : null,
      depth: depth,
      sequence: null,
      ui_state: uiState || null,
      subscription_state: uiState === "OFFLINE" ? "error" : "stopped"
    };
  }

  function invalidateOb1000LeaseLocal() {
    obState.obl1000.leaseId = null;
    obState.obl1000.leaseSymbol = null;
    obState.obl1000.leaseDepth = null;
  }

  function stopOb1000Heartbeat() {
    if (obState.obl1000.heartbeatTimer) {
      clearInterval(obState.obl1000.heartbeatTimer);
      obState.obl1000.heartbeatTimer = null;
    }
  }

  function releaseOb1000LeaseBestEffort() {
    if (!obState.obl1000.leaseId) return;
    var body = {
      op: "release",
      lease_id: obState.obl1000.leaseId,
      depth: obState.obl1000.leaseDepth != null ? obState.obl1000.leaseDepth : oblDataDepth()
    };
    try {
      if (navigator.sendBeacon) {
        navigator.sendBeacon(
          "/api/research/ob1000/lease",
          new Blob([JSON.stringify(body)], { type: "application/json" })
        );
      }
    } catch (e) { /* ignore */ }
    invalidateOb1000LeaseLocal();
  }

  function stopOb1000Lease() {
    stopOb1000Heartbeat();
    obState.obl1000.leaseGen += 1;
    var leaseId = obState.obl1000.leaseId;
    var leaseDepth = obState.obl1000.leaseDepth;
    invalidateOb1000LeaseLocal();
    obState.obl1000.uiState = "DISABLED";
    if (!leaseId) return Promise.resolve();
    return postJson("/api/research/ob1000/lease", {
      op: "release",
      lease_id: leaseId,
      depth: leaseDepth != null ? leaseDepth : oblDataDepth()
    }).catch(function () {});
  }

  function ensureOb1000Lease(symbol, opts) {
    if (!isOnDemandBookMode() || !symbol) return Promise.resolve(false);
    var force = !!(opts && opts.force);
    var leaseId = getOb1000LeaseId();
    var depth = oblDataDepth();
    if (
      !force &&
      obState.obl1000.leaseId === leaseId &&
      obState.obl1000.leaseSymbol === symbol &&
      obState.obl1000.leaseDepth === depth
    ) {
      return Promise.resolve(true);
    }
    var gen = ++obState.obl1000.leaseGen;
    var prep = Promise.resolve();
    if (
      obState.obl1000.leaseId &&
      (obState.obl1000.leaseSymbol !== symbol || obState.obl1000.leaseDepth !== depth)
    ) {
      prep = stopOb1000Lease();
    }
    return prep.then(function () {
      if (gen !== obState.obl1000.leaseGen) return false;
      return postJson("/api/research/ob1000/lease", {
        op: "acquire",
        symbol: symbol,
        lease_id: leaseId,
        depth: depth
      }).then(function (resp) {
        if (gen !== obState.obl1000.leaseGen) return false;
        obState.obl1000.leaseId = leaseId;
        obState.obl1000.leaseSymbol = symbol;
        obState.obl1000.leaseDepth = depth;
        obState.obl1000.uiState = computeOb1000UiState(resp, null);
        return true;
      }).catch(function (err) {
        if (gen !== obState.obl1000.leaseGen) return false;
        obState.obl1000.uiState = computeOb1000UiState(null, parseOb1000ApiError(err));
        invalidateOb1000LeaseLocal();
        return false;
      });
    });
  }

  function startOb1000Heartbeat() {
    stopOb1000Heartbeat();
    if (!isOnDemandBookMode()) return;
    obState.obl1000.heartbeatTimer = setInterval(function () {
      if (!isOnDemandBookMode() || document.hidden) return;
      var symbol = currentSymbol();
      if (!symbol) return;
      var leaseId = obState.obl1000.leaseId;
      if (!leaseId) {
        ensureOb1000Lease(symbol, { force: true }).then(function (ok) {
          if (ok) startOb1000Heartbeat();
        });
        return;
      }
      postJson("/api/research/ob1000/lease", {
        op: "heartbeat",
        lease_id: leaseId,
        symbol: symbol,
        depth: obState.obl1000.leaseDepth != null ? obState.obl1000.leaseDepth : oblDataDepth()
      }).catch(function (err) {
        var parsed = parseOb1000ApiError(err);
        if (parsed && parsed.code === "unknown_lease") {
          invalidateOb1000LeaseLocal();
          ensureOb1000Lease(symbol, { force: true }).then(function (ok) {
            if (ok) startOb1000Heartbeat();
          });
        }
      });
    }, OBL1000_HEARTBEAT_MS);
  }

  function clearOrderbookProfile() {
    obState.obpGen += 1;
    if (obState.obpTimer) {
      clearTimeout(obState.obpTimer);
      obState.obpTimer = null;
    }
    if (obState.obpAbort) {
      try { obState.obpAbort.abort(); } catch (e) { /* ignore */ }
      obState.obpAbort = null;
    }
    var api = chartApi();
    if (api && api.clearOrderbookProfile) api.clearOrderbookProfile();
  }

  function clearOrderbookLevels() {
    obState.oblGen += 1;
    obState.oblInflight = false;
    if (obState.oblTimer) {
      clearTimeout(obState.oblTimer);
      obState.oblTimer = null;
    }
    if (obState.oblAbort) {
      try { obState.oblAbort.abort(); } catch (e) { /* ignore */ }
      obState.oblAbort = null;
    }
    var api = chartApi();
    if (api && api.clearOrderbookLevels) api.clearOrderbookLevels();
    if (api && api.setOrderbookLevels) {
      api.setOrderbookLevels(null, { enabled: false, width_px: oblSettingsPayload().width_px });
    }
  }

  function stopOrderbookProfileRefresh() {
    if (obState.obpRefreshTimer) {
      clearInterval(obState.obpRefreshTimer);
      obState.obpRefreshTimer = null;
    }
  }

  function stopOrderbookLevelsRefresh() {
    if (obState.oblRefreshTimer) {
      clearInterval(obState.oblRefreshTimer);
      obState.oblRefreshTimer = null;
    }
    stopOb1000Heartbeat();
  }

  function scheduleOrderbookProfile() {
    if (!obState.obp || !obState.obp.enabled) return;
    if (obState.obpTimer) clearTimeout(obState.obpTimer);
    obState.obpTimer = setTimeout(function () {
      obState.obpTimer = null;
      refreshOrderbookProfile();
    }, OBP_DEBOUNCE_MS);
  }

  function scheduleOrderbookLevels() {
    if (!obState.obl || !obState.obl.enabled) return;
    if (obState.oblTimer) clearTimeout(obState.oblTimer);
    obState.oblTimer = setTimeout(function () {
      obState.oblTimer = null;
      refreshOrderbookLevels();
    }, 200);
  }

  function startOrderbookProfileRefresh() {
    stopOrderbookProfileRefresh();
    if (!obState.obp || !obState.obp.enabled) return;
    obState.obpRefreshTimer = setInterval(function () {
      if (!obState.obp || !obState.obp.enabled) return;
      if (document.hidden) return;
      scheduleOrderbookProfile();
    }, OBP_REFRESH_MS);
  }

  function startOrderbookLevelsRefresh() {
    stopOrderbookLevelsRefresh();
    if (!obState.obl || !obState.obl.enabled) return;
    if (isOnDemandBookMode()) {
      if (!obState.obl1000.heartbeatTimer) startOb1000Heartbeat();
    }
    var interval = isOnDemandBookMode()
      ? (oblDataDepth() === 0 ? OBL_FULL_REFRESH_MS : OBL1000_REFRESH_MS)
      : OBL_REFRESH_MS;
    obState.oblRefreshTimer = setInterval(function () {
      if (!obState.obl || !obState.obl.enabled) return;
      if (document.hidden) return;
      scheduleOrderbookLevels();
    }, interval);
  }

  function refreshOrderbookProfile() {
    if (!obState.obp || !obState.obp.enabled) {
      clearOrderbookProfile();
      return;
    }
    var symbol = currentSymbol();
    var api = chartApi();
    if (!api || !api.getVisibleTimeRange || !symbol) return;
    var range = api.getVisibleTimeRange();
    if (!range || range.firstCandle == null || range.lastCandle == null) return;
    var step = TF_SEC[currentCandleTf()] || 60;
    var start = Number(range.firstCandle);
    var end = Number(range.lastCandle) + step;
    if (!(end > start)) return;
    var gen = ++obState.obpGen;
    if (obState.obpAbort) {
      try { obState.obpAbort.abort(); } catch (e) { /* ignore */ }
    }
    obState.obpAbort = typeof AbortController !== "undefined" ? new AbortController() : null;
    var tip = Number(range.lastCandle) + step;
    var nowSec = Math.floor(Date.now() / 1000);
    var atSec = (nowSec - tip <= 180) ? nowSec : Math.floor(tip);
    var url = "/api/research/orderbook-profile?symbol=" + encodeURIComponent(symbol) +
      "&start=" + encodeURIComponent(String(start)) +
      "&end=" + encodeURIComponent(String(Math.max(end, atSec + 1))) +
      "&at=" + encodeURIComponent(String(atSec)) +
      "&mode=snapshot_at";
    getJson(url, { signal: obState.obpAbort && obState.obpAbort.signal })
      .then(function (body) {
        if (gen !== obState.obpGen) return;
        if (!obState.obp.enabled) return;
        var live = chartApi();
        if (live && live.setOrderbookProfile) {
          live.setOrderbookProfile(body, obpSettingsPayload());
        }
      })
      .catch(function (err) {
        if (err && err.name === "AbortError") return;
      });
  }

  function refreshOrderbookLevels() {
    if (!obState.obl || !obState.obl.enabled) {
      clearOrderbookLevels();
      return;
    }
    if (obState.oblInflight) return;
    var symbol = currentSymbol();
    var api = chartApi();
    if (!api || !symbol) return;
    var gen = ++obState.oblGen;
    var reqSymbol = symbol;
    var reqDepth = oblDataDepth();
    if (obState.oblAbort) {
      try { obState.oblAbort.abort(); } catch (e) { /* ignore */ }
    }
    obState.oblAbort = typeof AbortController !== "undefined" ? new AbortController() : null;
    obState.oblInflight = true;

    function finish() {
      if (gen === obState.oblGen) obState.oblInflight = false;
      else obState.oblInflight = false;
    }

    if (reqDepth === 1000 || reqDepth === 0) {
      ensureOb1000Lease(reqSymbol).then(function (leased) {
        if (gen !== obState.oblGen || reqSymbol !== currentSymbol() || oblDataDepth() !== reqDepth) {
          finish();
          return null;
        }
        if (!leased) {
          var uiState = obState.obl1000.uiState || "NO_DATA";
          var live = chartApi();
          if (live && live.setOrderbookLevels) {
            live.setOrderbookLevels(oblEmptyPayload(reqSymbol, reqDepth, uiState), oblSettingsPayload());
          }
          finish();
          return null;
        }
        var url = "/api/research/ob1000-levels?symbol=" + encodeURIComponent(reqSymbol) +
          "&depth=" + encodeURIComponent(String(reqDepth));
        if (obState.obl1000.leaseId) {
          url += "&lease_id=" + encodeURIComponent(obState.obl1000.leaseId);
        }
        return getJson(url, { signal: obState.oblAbort && obState.oblAbort.signal }).then(function (body) {
          if (gen !== obState.oblGen) return;
          if (reqSymbol !== currentSymbol() || oblDataDepth() !== reqDepth) return;
          if (body.depth == null) body.depth = reqDepth;
          body.ui_state = computeOb1000UiState(body, null);
          obState.obl1000.uiState = body.ui_state;
          var stillEmpty = !(body.bids && body.bids.length) && !(body.asks && body.asks.length);
          var live = chartApi();
          if (live && live.setOrderbookLevels) {
            var sig = String(body.timestamp_utc || "") + "|" + String(body.sequence || "") + "|" +
              String((body.bids && body.bids.length) || 0) + "|" + String((body.asks && body.asks.length) || 0) + "|" +
              String(body.ui_state || "") + "|" + String(body.depth);
            if (!stillEmpty) {
              if (obState._oblPaintSig !== sig) {
                live.setOrderbookLevels(body, oblSettingsPayload());
                obState._oblPaintSig = sig;
              }
              obState._oblHadLive = true;
            } else if (!obState._oblHadLive) {
              live.setOrderbookLevels(body, oblSettingsPayload());
              obState._oblPaintSig = sig;
            }
          }
        }).catch(function (err) {
          if (err && err.name === "AbortError") return;
          if (gen !== obState.oblGen) return;
          var parsed = parseOb1000ApiError(err);
          if (parsed && parsed.code === "unknown_lease") {
            invalidateOb1000LeaseLocal();
            return ensureOb1000Lease(reqSymbol, { force: true });
          }
          var ui = computeOb1000UiState(null, parsed);
          obState.obl1000.uiState = ui;
          if (ui === "NOT_PILOT" || ui === "OFFLINE" || ui === "DISABLED" || ui === "CAPACITY") {
            var live2 = chartApi();
            if (live2 && live2.setOrderbookLevels) {
              live2.setOrderbookLevels(oblEmptyPayload(reqSymbol, reqDepth, ui), oblSettingsPayload());
            }
          }
        });
      }).then(function () { finish(); }, function () { finish(); });
      return;
    }

    var url200 = "/api/research/ob200-levels?symbol=" + encodeURIComponent(reqSymbol);
    getJson(url200, { signal: obState.oblAbort && obState.oblAbort.signal })
      .then(function (body) {
        if (gen !== obState.oblGen) return;
        if (reqSymbol !== currentSymbol() || oblDataDepth() !== 200) return;
        body.depth = 200;
        var live = chartApi();
        if (live && live.setOrderbookLevels) live.setOrderbookLevels(body, oblSettingsPayload());
      })
      .catch(function (err) {
        if (err && err.name === "AbortError") return;
        if (gen !== obState.oblGen) return;
        var live = chartApi();
        if (live && live.setOrderbookLevels) {
          live.setOrderbookLevels(oblEmptyPayload(reqSymbol, 200, null), oblSettingsPayload());
        }
      })
      .then(function () { finish(); }, function () { finish(); });
  }

  function applyOrderbookProfileSettings(raw, skipPersist) {
    obState.obp = Object.assign(defaultOrderbookProfile(), raw || {});
    fillOrderbookProfileControls();
    if (!skipPersist) persistOrderbookProfile();
    if (!obState.obp.enabled) {
      stopOrderbookProfileRefresh();
      clearOrderbookProfile();
      return;
    }
    startOrderbookProfileRefresh();
    scheduleOrderbookProfile();
  }

  function applyOrderbookLevelsSettings(raw, skipPersist) {
    var prevDepth = oblDataDepth();
    obState.obl = Object.assign(defaultOrderbookLevels(), raw || {});
    if (obState.obl.width_px != null) {
      obState.obl.width_px = Math.max(100, Math.min(220, Number(obState.obl.width_px) || 140));
    }
    if (obState.obl.depth != null) {
      var d = Number(obState.obl.depth);
      if (d === 0) obState.obl.depth = 0;
      else if (d === 1000) obState.obl.depth = 1000;
      else obState.obl.depth = 200;
    }
    fillOrderbookLevelsControls();
    if (!skipPersist) persistOrderbookLevels();
    if (!obState.obl.enabled) {
      stopOrderbookLevelsRefresh();
      stopOb1000Lease();
      clearOrderbookLevels();
      return;
    }
    var wasOnDemand = prevDepth === 1000 || prevDepth === 0;
    var nowOnDemand = isOnDemandBookMode();
    if (wasOnDemand && (!nowOnDemand || prevDepth !== oblDataDepth())) {
      stopOb1000Lease();
      obState._oblHadLive = false;
      obState._oblPaintSig = null;
    }
    if (isOnDemandBookMode()) {
      ensureOb1000Lease(currentSymbol()).then(function () { startOb1000Heartbeat(); });
    } else {
      stopOb1000Lease();
    }
    startOrderbookLevelsRefresh();
    scheduleOrderbookLevels();
  }

  function initOrderbookControls() {
    obState.obp = readStoredOrderbookProfile();
    obState.obl = readStoredOrderbookLevels();
    fillOrderbookProfileControls();
    fillOrderbookLevelsControls();
    var obpEn = $("mpObpEnabled");
    if (obpEn && !obpEn._mpObBound) {
      obpEn.addEventListener("change", function () {
        obState.obp.enabled = obpEn.checked;
        persistOrderbookProfile();
        fillOrderbookProfileControls();
        applyOrderbookProfileSettings(obState.obp, true);
      });
      obpEn._mpObBound = true;
    }
    var oblEn = $("mpOblEnabled");
    if (oblEn && !oblEn._mpObBound) {
      oblEn.addEventListener("change", function () {
        obState.obl.enabled = oblEn.checked;
        persistOrderbookLevels();
        fillOrderbookLevelsControls();
        applyOrderbookLevelsSettings(obState.obl, true);
      });
      oblEn._mpObBound = true;
    }
    ["mpOblMode", "mpOblScale", "mpOblDepth"].forEach(function (id) {
      var el = $(id);
      if (!el || el._mpObBound) return;
      el.addEventListener("change", function () {
        if (!obState.obl) obState.obl = defaultOrderbookLevels();
        if (id === "mpOblDepth") {
          var v = $("mpOblDepth") ? $("mpOblDepth").value : "1000";
          if (v === "0") obState.obl.depth = 0;
          else if (v === "1000") obState.obl.depth = 1000;
          else obState.obl.depth = 200;
        } else {
          obState.obl.mode = ($("mpOblMode") && $("mpOblMode").value) || "aggregated";
          obState.obl.scale = ($("mpOblScale") && $("mpOblScale").value) || "sqrt";
        }
        persistOrderbookLevels();
        applyOrderbookLevelsSettings(obState.obl, true);
      });
      el._mpObBound = true;
    });
    if (obState.obp.enabled) applyOrderbookProfileSettings(obState.obp, true);
    if (obState.obl.enabled) applyOrderbookLevelsSettings(obState.obl, true);
  }

  function onOrderbookSymbolOrViewChange() {
    if (obState.obp && obState.obp.enabled) scheduleOrderbookProfile();
    if (obState.obl && obState.obl.enabled) {
      if (isOnDemandBookMode()) {
        ensureOb1000Lease(currentSymbol()).then(function () {
          startOb1000Heartbeat();
          scheduleOrderbookLevels();
        });
      } else {
        scheduleOrderbookLevels();
      }
    }
  }
  /* ---- ORDERBOOK_HOOK end ---- */

  function whenChartReady(cb) {
    var done = false;
    function fire() {
      if (done) return;
      done = true;
      chartReady = true;
      syncChartRefs();
      bindChartSurface();
      cb();
    }
    if (chartReady && syncChartRefs()) {
      fire();
      return;
    }
    var prev = window.__mpOnChartReady;
    window.__mpOnChartReady = function () {
      if (typeof prev === "function") {
        try { prev(); } catch (err) { /* ignore prior handler errors */ }
      }
      fire();
    };
    if (window.__mpChartReady) {
      window.__mpOnChartReady();
      return;
    }
    // Chart bridge can miss the ready event (script order / pane size).
    // Fall back so auto-load is never stuck on the empty placeholder.
    setTimeout(function () {
      if (!done) fire();
    }, 800);
  }

  function applyPayloadToChart(s, opts) {
    if (!payload) return false;
    var api = chartApi();
    if (!api || !api.setData) return false;
    api.setData({
      symbol: s.symbol,
      timeframe: s.timeframe,
      is_demo: false,
      candles: payload.candles || []
    }, opts || null);
    api.setInteractionMode(toolMode());
    // After TF / reload, re-assert Shift so measure works if the key is still held
    // (host key handlers can go stale while focus was on the TF select).
    if (api.setHostShift) {
      try {
        api.setHostShift(!!window.__mpHostShift);
      } catch (err) { /* ignore */ }
    }
    // Do not force followLive — only Zentrieren (resetView) recenters.
    return true;
  }

  function stopLivePoll() {
    livePollGen += 1;
    if (formingTimer) {
      clearInterval(formingTimer);
      formingTimer = null;
    }
    formingInflight = false;
  }

  function formingBarForTf(forming, tfSec, lastCandle) {
    var t1 = Number(forming && forming.time);
    var px = Number(forming && forming.close);
    if (!isFinite(t1) || !isFinite(px) || !tfSec) return null;
    var bucket = Math.floor(t1 / tfSec) * tfSec;
    var fHigh = Number(forming.high);
    var fLow = Number(forming.low);
    var hi = isFinite(fHigh) ? fHigh : px;
    var lo = isFinite(fLow) ? fLow : px;
    if (!lastCandle) {
      var o0 = Number(forming.open);
      return {
        time: bucket,
        open: isFinite(o0) ? o0 : px,
        high: Math.max(px, hi),
        low: Math.min(px, lo),
        close: px
      };
    }
    var lastT = Number(lastCandle.time);
    if (!isFinite(lastT)) return null;
    if (lastT === bucket) {
      return {
        time: bucket,
        open: Number(lastCandle.open),
        high: Math.max(Number(lastCandle.high), hi, px),
        low: Math.min(Number(lastCandle.low), lo, px),
        close: px
      };
    }
    if (lastT < bucket) {
      var o = Number(lastCandle.close);
      return {
        time: bucket,
        open: isFinite(o) ? o : px,
        high: Math.max(o, hi, px),
        low: Math.min(o, lo, px),
        close: px
      };
    }
    return {
      time: lastT,
      open: Number(lastCandle.open),
      high: Math.max(Number(lastCandle.high), hi, px),
      low: Math.min(Number(lastCandle.low), lo, px),
      close: px
    };
  }

  function applyLiveForming(forming) {
    var api = chartApi();
    if (!api || !api.updateFormingBar || !payload || !payload.candles || !payload.candles.length) {
      return false;
    }
    var s = readSettings();
    var tfSec = TF_SEC[s.timeframe] || 60;
    var candles = payload.candles;
    var last = candles[candles.length - 1];
    var bar = formingBarForTf(forming, tfSec, last);
    if (!bar) return false;
    var ok = !!api.updateFormingBar(bar);
    if (ok) {
      if (Number(candles[candles.length - 1].time) === Number(bar.time)) {
        candles[candles.length - 1] = Object.assign({}, candles[candles.length - 1], bar);
      } else if (Number(bar.time) > Number(last.time)) {
        candles.push(bar);
      }
      payload.candles = candles;
    }
    return ok;
  }

  function pollForming(gen) {
    if (gen !== livePollGen || document.hidden) return;
    if (!payload || !payload.candles || !payload.candles.length) return;
    if (formingInflight) return;
    formingInflight = true;
    var sym = readSettings().symbol;
    fetch("/api/research/forming-bar?symbol=" + encodeURIComponent(sym), {
      credentials: "same-origin"
    })
      .then(function (res) { return res.ok ? res.json() : null; })
      .then(function (body) {
        if (gen !== livePollGen) return;
        if (body && body.forming) applyLiveForming(body.forming);
      })
      .catch(function () { /* live tip is best-effort */ })
      .then(function () { formingInflight = false; });
  }

  function startLivePoll() {
    stopLivePoll();
    if (!payload || !payload.candles || !payload.candles.length) return;
    var gen = livePollGen;
    formingTimer = setInterval(function () {
      if (gen !== livePollGen || document.hidden) return;
      pollForming(gen);
    }, FORMING_MS);
    pollForming(gen);
  }

  function bindChartSurface() {
    if (!syncChartRefs()) return;
    var pane = $("price-pane");
    if (pane && !pane.__mpHoverBound) {
      pane.__mpHoverBound = true;
      pane.addEventListener("mousemove", onHover);
      pane.addEventListener("mouseleave", function () { $("mpTooltip").hidden = true; });
    }
    window.__mpOnVisibleRange = function () {
      scheduleDrawDebounced(90);
      onOrderbookSymbolOrViewChange();
    };
    try {
      chart.timeScale().subscribeVisibleLogicalRangeChange(function () {
        scheduleDrawDebounced(90);
        onOrderbookSymbolOrViewChange();
      });
    } catch (err) { /* chart may already notify via bridge */ }
    var wrap = $("price-pane") || $("mpChart");
    if (wrap && !wrap.__mpResizeBound) {
      wrap.__mpResizeBound = true;
      window.addEventListener("resize", function () {
        var api = chartApi();
        if (api && api.resize) api.resize();
        scheduleDraw();
      });
    }
  }

  function toolMode() {
    var tool = (workspace && workspace.tool) || "select";
    return tool === "select" ? "select" : tool;
  }

  function applyWorkspace(snap) {
    if (!snap || snap.success === false) return;
    workspace = snap;
    document.querySelectorAll(".trp-tool-btn[data-tool]").forEach(function (btn) {
      btn.classList.toggle("active", btn.dataset.tool === snap.tool);
    });
    if (snap.style) {
      if (snap.style.color && snap.style.color.indexOf("#") === 0 && $("mpDrawColor")) {
        $("mpDrawColor").value = snap.style.color;
      }
      if (snap.style.width && $("mpDrawWidth")) {
        $("mpDrawWidth").value = String(Math.round(snap.style.width));
      }
    }
    var ema = snap.ema || { lines: [] };
    var enabled = (ema.lines || []).filter(function (l) { return l.enabled; })
      .map(function (l) { return "EMA" + l.period; });
    if ($("mpEmaSummary")) {
      $("mpEmaSummary").textContent = enabled.length ? enabled.join(", ") : "off";
    }
    renderLldLegend(snap.liquidity || researchLiquidityConfig(), null);
    var api = chartApi();
    if (api) {
      api.setInteractionMode(toolMode());
      if (snap.preview_anchor && api.setPreviewAnchor) api.setPreviewAnchor(snap.preview_anchor);
      else if (api.clearPreview) api.clearPreview();
    }
  }

  function deactivateToolsLocal() {
    if (!workspace) workspace = {};
    workspace.tool = "select";
    workspace.pending = false;
    workspace.preview_anchor = null;
    document.querySelectorAll(".trp-tool-btn[data-tool]").forEach(function (btn) {
      btn.classList.toggle("active", btn.dataset.tool === "select");
    });
    var api = chartApi();
    if (api) {
      api.setInteractionMode("select");
      if (api.clearPreview) api.clearPreview();
    }
  }

  function sendJson(url, method, body) {
    return fetch(url, {
      method: method || "GET",
      credentials: "same-origin",
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined
    }).then(function (res) {
      return res.json().then(function (payload) {
        if (!res.ok) {
          var msg = (payload && (payload.message || payload.error)) || res.statusText;
          throw new Error(msg);
        }
        return payload;
      });
    });
  }

  function overlayNamespace(p) {
    if (!p) return "";
    if (p.namespace) return String(p.namespace);
    var id = String(p.id || "");
    if (id.indexOf("lld:") === 0 || id.indexOf("lldc:") === 0) return "LLD";
    var meta = p.metadata || {};
    if (meta.source === "drawing" || meta.drawing_id) {
      if (p.type === "position" || meta.drawing_type === "long_position" || meta.drawing_type === "short_position") {
        return "POSITION";
      }
      return "USER_DRAWING";
    }
    return "SYSTEM";
  }

  function isUserDrawingOverlay(p) {
    var ns = overlayNamespace(p);
    return ns === "USER_DRAWING" || ns === "POSITION";
  }

  function rebuildOverlays() {
    var api = chartApi();
    if (!api) return;
    var wanted = {};
    Object.keys(drawingPayloads).forEach(function (id) { wanted[id] = drawingPayloads[id]; });
    Object.keys(lldPayloads).forEach(function (id) { wanted[id] = lldPayloads[id]; });
    Object.keys(appliedOverlayPayloads).forEach(function (id) {
      if (!wanted[id]) api.removeOverlay(id);
    });
    Object.keys(wanted).forEach(function (id) {
      if (!appliedOverlayPayloads[id]) api.addOverlay(wanted[id]);
      else api.updateOverlay(wanted[id]);
    });
    appliedOverlayPayloads = wanted;
  }

  function clearLiquidityOverlays() {
    lldPayloads = {};
    rebuildOverlays();
    var api = chartApi();
    if (api && api.setLldEma) {
      api.setLldEma({ fast: [], slow: [], fast_visible: false, slow_visible: false });
    }
    if (api && api.layoutOverlays) api.layoutOverlays();
    if (lastEmaPayload) applyEmaOverlays(lastEmaPayload);
    renderLldLegend(researchLiquidityConfig(), null);
    scheduleDraw();
  }

  function syncDrawings(overlays) {
    var next = {};
    (overlays || []).forEach(function (p) {
      if (p && p.id && isUserDrawingOverlay(p)) next[p.id] = p;
    });
    drawingPayloads = next;
    rebuildOverlays();
  }

  function refreshDrawings() {
    var s = readSettings();
    return sendJson(
      "/api/research/overlays?symbol=" + encodeURIComponent(s.symbol) +
        "&timeframe=" + encodeURIComponent(s.timeframe)
    ).then(function (body) {
      syncDrawings(body.overlays || []);
    }).catch(function () { /* drawings are optional */ });
  }

  function isLldOverlay(p) {
    return overlayNamespace(p) === "LLD";
  }

  function lldConfigEnabled() {
    var s = readSettings();
    return !!s.showLiquidity;
  }

  function researchLiquidityConfig() {
    var ws = workspace || {};
    var cfg = Object.assign({}, ws.liquidity || {});
    if (lldConfigEnabled()) cfg.enabled = true;
    return cfg;
  }

  function mix(hex, t) {
    var n = hex.replace("#", "");
    var r = parseInt(n.slice(0, 2), 16);
    var g = parseInt(n.slice(2, 4), 16);
    var b = parseInt(n.slice(4, 6), 16);
    return "rgba(" + r + "," + g + "," + b + "," + (0.25 + 0.55 * t).toFixed(2) + ")";
  }

  function renderLldLegend(cfg, clusters) {
    var box = $("mpLldLegend");
    if (!box) return;
    var on = lldConfigEnabled();
    box.hidden = !on;
    if (!on) return;
    var sup = $("mpVolSupport");
    var res = $("mpVolResistance");
    if (!sup || !res) return;
    sup.innerHTML = "";
    res.innerHTML = "";
    for (var i = 0; i <= 10; i += 1) {
      var t = i / 10;
      var sEl = document.createElement("span");
      var rEl = document.createElement("span");
      sEl.className = rEl.className = "trp-vol-cell";
      sEl.textContent = rEl.textContent = String(i);
      sEl.style.background = mix((cfg && cfg.support_color) || "#228bab", t);
      rEl.style.background = mix((cfg && cfg.resistance_color) || "#ec4079", t);
      sup.appendChild(sEl);
      res.appendChild(rEl);
    }
    var c = clusters || {};
    var counts = $("mpClusterCounts");
    if (counts) {
      counts.textContent = (cfg && cfg.clusters_enabled)
        ? ("Cl 3P:" + (c["3"] || 0) + "  4-5P:" + (c["4-5"] || 0) + "  6+:" + (c["6+"] || 0))
        : "";
    }
  }

  function fillLld(cfg) {
    if (!cfg) return;
    if ($("lldAmount")) $("lldAmount").value = cfg.amount;
    if ($("lldHigh")) $("lldHigh").value = cfg.highest_len;
    if ($("lldLow")) $("lldLow").value = cfg.lowest_len;
    if ($("lldBorders")) $("lldBorders").checked = !!cfg.show_pool_borders;
    if ($("lldBorderW")) $("lldBorderW").value = cfg.pool_border_width;
    if ($("lldStrongW")) $("lldStrongW").value = cfg.strong_pool_border_width;
    if ($("lldBorderT")) $("lldBorderT").value = cfg.pool_border_transparency;
    if ($("lldClusters")) $("lldClusters").checked = !!cfg.clusters_enabled;
    if ($("lldGap")) $("lldGap").value = cfg.cluster_gap_pct;
    if ($("lldMinPools")) $("lldMinPools").value = cfg.minimum_cluster_pools;
    if ($("lldShowSingle")) $("lldShowSingle").checked = !!cfg.show_single_pools;
    if ($("lldClusterLabels")) $("lldClusterLabels").checked = !!cfg.show_cluster_labels;
    if ($("lldClusterFill")) $("lldClusterFill").value = cfg.cluster_fill_transparency;
    if ($("lldClusterBorder")) $("lldClusterBorder").value = cfg.cluster_border_width;
    if ($("lldSupport")) $("lldSupport").value = cfg.support_color;
    if ($("lldResist")) $("lldResist").value = cfg.resistance_color;
    if ($("lldFillT")) $("lldFillT").value = cfg.fill_transparency;
    if ($("lldSupBorder")) $("lldSupBorder").value = cfg.support_border_color;
    if ($("lldResBorder")) $("lldResBorder").value = cfg.resistance_border_color;
    if ($("lldEmaFast")) $("lldEmaFast").checked = !!cfg.ema_fast_enabled;
    if ($("lldEmaFastLen")) $("lldEmaFastLen").value = cfg.ema_fast_length;
    if ($("lldEmaFastColor")) $("lldEmaFastColor").value = cfg.ema_fast_color;
    if ($("lldEmaSlow")) $("lldEmaSlow").checked = !!cfg.ema_slow_enabled;
    if ($("lldEmaSlowLen")) $("lldEmaSlowLen").value = cfg.ema_slow_length;
    if ($("lldEmaSlowColor")) $("lldEmaSlowColor").value = cfg.ema_slow_color;
  }

  function readLld() {
    return {
      enabled: lldConfigEnabled(),
      amount: Number($("lldAmount").value),
      highest_len: Number($("lldHigh").value),
      lowest_len: Number($("lldLow").value),
      show_pool_borders: $("lldBorders").checked,
      pool_border_width: Number($("lldBorderW").value),
      strong_pool_border_width: Number($("lldStrongW").value),
      pool_border_transparency: Number($("lldBorderT").value),
      clusters_enabled: $("lldClusters").checked,
      cluster_gap_pct: Number($("lldGap").value),
      minimum_cluster_pools: Number($("lldMinPools").value),
      show_single_pools: $("lldShowSingle").checked,
      show_cluster_labels: $("lldClusterLabels").checked,
      cluster_fill_transparency: Number($("lldClusterFill").value),
      cluster_border_width: Number($("lldClusterBorder").value),
      support_color: $("lldSupport").value,
      resistance_color: $("lldResist").value,
      fill_transparency: Number($("lldFillT").value),
      support_border_color: $("lldSupBorder").value,
      resistance_border_color: $("lldResBorder").value,
      ema_fast_enabled: $("lldEmaFast").checked,
      ema_fast_length: Number($("lldEmaFastLen").value),
      ema_fast_color: $("lldEmaFastColor").value,
      ema_slow_enabled: $("lldEmaSlow").checked,
      ema_slow_length: Number($("lldEmaSlowLen").value),
      ema_slow_color: $("lldEmaSlowColor").value
    };
  }

  function applyLldPaneBundle(body) {
    var next = {};
    (body.overlays || []).forEach(function (p) {
      if (p && p.id && isLldOverlay(p)) next[p.id] = p;
    });
    lldPayloads = next;
    rebuildOverlays();
    var api = chartApi();
    var lldEma = body.lld_ema || (body.liquidity && body.liquidity.ema) || null;
    if (api && api.setLldEma) {
      api.setLldEma(lldEma || {
        fast: [], slow: [], fast_visible: false, slow_visible: false
      });
    }
    if (api && api.layoutOverlays) api.layoutOverlays();
    var cfg = researchLiquidityConfig();
    var clusters = body.clusters || (body.liquidity && body.liquidity.clusters);
    renderLldLegend(cfg, clusters);
    scheduleDraw();
  }

  function loadResearchPaneForLld() {
    var s = readSettings();
    if (!lastLoadRange) return Promise.reject(new Error("no range"));
    var ws = workspace || {};
    return sendJson("/api/research/pane", "POST", {
      symbol: s.symbol,
      timeframe: s.timeframe,
      from: lastLoadRange.start,
      to: lastLoadRange.end,
      ema: ws.ema || { enabled: false, lines: [] },
      stochastic: ws.stochastic || { enabled: false },
      liquidity: researchLiquidityConfig(),
      allow_stale: true
    });
  }

  function refreshLiquidityLocation() {
    var s = readSettings();
    renderLldLegend(researchLiquidityConfig(), null);
    if (!s.showLiquidity || !lastLoadRange) {
      clearLiquidityOverlays();
      return Promise.resolve();
    }
    return sendJson("/api/research/indicator-enabled", "POST", {
      name: "liquidity",
      enabled: true
    }).then(function (snap) {
      applyWorkspace(snap);
      return loadResearchPaneForLld();
    }).then(function (body) {
      // Keep Market-Profile candles + EMA on the series. Replacing them with
      // the pane bundle emptied the visible bars while LLD boxes stayed.
      applyLldPaneBundle(body);
      if (lastEmaPayload) applyEmaOverlays(lastEmaPayload);
    }).catch(function () {
      clearLiquidityOverlays();
    });
  }

  function setTool(tool) {
    return sendJson("/api/research/drawings/tool", "POST", { tool: tool }).then(function (snap) {
      applyWorkspace(snap);
      var api = chartApi();
      if (api) api.setInteractionMode(toolMode());
    });
  }

  function handleDrawingEvent(blob) {
    var data = {};
    try { data = typeof blob === "string" ? JSON.parse(blob) : blob || {}; } catch (err) { return Promise.resolve(); }
    var s = readSettings();
    data.pane_id = "mp-price";
    data.timeframe = s.timeframe;
    data.symbol = s.symbol;
    return sendJson("/api/research/drawings/event", "POST", data).then(function (snap) {
      applyWorkspace(snap);
      if (data.type !== "point" || (workspace && !workspace.pending)) {
        deactivateToolsLocal();
        var api = chartApi();
        if (api) api.setInteractionMode("select");
        return refreshDrawings();
      }
      var chartRef = chartApi();
      if (chartRef && workspace && workspace.preview_anchor && chartRef.setPreviewAnchor) {
        chartRef.setPreviewAnchor(workspace.preview_anchor);
      }
    });
  }

  function handleChartKey(key) {
    if (key === "escape") {
      return sendJson("/api/research/drawings/cancel", "POST", {}).then(applyWorkspace);
    }
    if (key === "delete") {
      return sendJson("/api/research/drawings/delete", "POST", {}).then(function (snap) {
        applyWorkspace(snap);
        return refreshDrawings();
      });
    }
    return Promise.resolve();
  }

  function buildTools() {
    var host = $("mpTools");
    if (!host) return;
    host.innerHTML = "";
    TOOLS.forEach(function (pair) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "trp-tool-btn" + (pair[0] === "select" ? " active" : "");
      btn.dataset.tool = pair[0];
      btn.title = pair[1];
      btn.setAttribute("aria-label", pair[1]);
      btn.textContent = TOOL_ICONS[pair[0]] || pair[0];
      btn.addEventListener("click", function () { setTool(pair[0]); });
      host.appendChild(btn);
    });
  }

  function openModal(id) { $(id).hidden = false; }
  function closeModal(id) { $(id).hidden = true; }

  function renderEmaRows(lines) {
    var host = $("emaRows");
    if (!host) return;
    host.innerHTML = "";
    emaDraft = (lines || []).map(function (l) { return Object.assign({}, l); });
    emaDraft.forEach(function (line, idx) {
      var row = document.createElement("div");
      row.className = "trp-ema-row";
      row.innerHTML =
        '<label class="trp-check"><input type="checkbox" data-f="enabled"' + (line.enabled ? " checked" : "") + "> EMA</label>" +
        '<label>Periode <input type="number" min="1" max="5000" data-f="period" value="' + line.period + '"></label>' +
        '<label>Farbe <input type="color" data-f="color" value="' + (line.color || "#ff9800") + '"></label>' +
        '<label>Stärke <input type="number" min="1" max="4" data-f="line_width" value="' + line.line_width + '"></label>' +
        '<label>Transp. <input type="number" min="0" max="100" data-f="transparency" value="' + line.transparency + '"></label>' +
        '<button type="button" class="trp-text-btn" data-remove="' + line.ema_id + '">Entfernen</button>';
      row.querySelectorAll("[data-f]").forEach(function (inp) {
        inp.addEventListener("change", function () {
          var f = inp.getAttribute("data-f");
          emaDraft[idx][f] = inp.type === "checkbox" ? inp.checked
            : (inp.type === "number" ? Number(inp.value) : inp.value);
        });
      });
      row.querySelector("[data-remove]").addEventListener("click", function () {
        emaDraft = emaDraft.filter(function (x) { return x.ema_id !== line.ema_id; });
        renderEmaRows(emaDraft);
      });
      host.appendChild(row);
    });
  }

  function fetchEmaOverlays(symbol, timeframe, range) {
    var emaCfg = (workspace && workspace.ema) || { enabled: true, lines: [] };
    return sendJson("/api/research/indicators", "POST", {
      symbol: symbol,
      timeframe: timeframe,
      from: range.start,
      to: range.end,
      ema: emaCfg,
      stochastic: { enabled: false },
      liquidity: { enabled: false }
    }).then(function (body) {
      return (body && body.ema) || { series: [] };
    }).catch(function () {
      return { series: [] };
    });
  }

  function applyEmaOverlays(emaPayload) {
    lastEmaPayload = emaPayload || { series: [] };
    var api = chartApi();
    if (api && api.setEmaOverlays) {
      api.setEmaOverlays(lastEmaPayload, { skipRangeRestore: true });
    }
  }

  function bindChartChrome() {
    window.__mpOnDrawingEvent = handleDrawingEvent;
    window.__mpOnToolIdle = deactivateToolsLocal;
    window.__mpOnChartKey = handleChartKey;

    buildTools();

    $("mpEmaSettings").addEventListener("click", function () {
      renderEmaRows(((workspace || {}).ema || {}).lines || []);
      openModal("modalEma");
    });
    $("emaAdd").addEventListener("click", function () {
      var used = new Set((emaDraft || []).map(function (l) { return l.period; }));
      var period = 50;
      while (used.has(period)) period += 1;
      emaDraft = (emaDraft || []).concat([{
        ema_id: "ema-" + Math.random().toString(16).slice(2, 12),
        enabled: true,
        period: period,
        color: "#3dcc91",
        line_width: 2,
        transparency: 0
      }]);
      renderEmaRows(emaDraft);
    });
    $("emaApply").addEventListener("click", function () {
      sendJson("/api/research/settings", "PUT", { ema: { lines: emaDraft } })
        .then(function (snap) {
          applyWorkspace(snap);
          closeModal("modalEma");
          if (lastLoadRange && payload) {
            var s = readSettings();
            return fetchEmaOverlays(s.symbol, s.timeframe, lastLoadRange).then(applyEmaOverlays);
          }
        })
        .catch(function (err) {
          $("emaError").hidden = false;
          $("emaError").textContent = String(err.message || err);
        });
    });
    document.querySelectorAll("[data-close]").forEach(function (btn) {
      btn.addEventListener("click", function () { closeModal(btn.getAttribute("data-close")); });
    });
    document.querySelectorAll("[data-reset]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var kind = btn.getAttribute("data-reset");
        sendJson("/api/research/settings/defaults").then(function (defaults) {
          if (kind === "ema") renderEmaRows((defaults.ema && defaults.ema.lines) || []);
          if (kind === "lld") fillLld(Object.assign({}, defaults.liquidity, { enabled: lldConfigEnabled() }));
        });
      });
    });

    $("mpLldSettings").addEventListener("click", function () {
      if ($("lldLicense")) {
        $("lldLicense").textContent = (workspace && workspace.license_notice) || "";
      }
      fillLld(researchLiquidityConfig());
      openModal("modalLld");
    });
    $("lldApply").addEventListener("click", function () {
      sendJson("/api/research/settings", "PUT", { liquidity: readLld() })
        .then(function (snap) {
          applyWorkspace(snap);
          closeModal("modalLld");
          if ($("lldError")) $("lldError").hidden = true;
          return refreshLiquidityLocation();
        })
        .catch(function (err) {
          if ($("lldError")) {
            $("lldError").hidden = false;
            $("lldError").textContent = String(err.message || err);
          }
        });
    });

    $("mpDelete").addEventListener("click", function () {
      sendJson("/api/research/drawings/delete", "POST", {}).then(function (snap) {
        applyWorkspace(snap);
        return refreshDrawings();
      });
    });
    $("mpClear").addEventListener("click", function () {
      if (!window.confirm("Zeichnungen dieses Symbols löschen?")) return;
      var s = readSettings();
      sendJson("/api/research/drawings/clear", "POST", { symbol: s.symbol }).then(function (snap) {
        applyWorkspace(snap);
        return refreshDrawings();
      });
    });
    $("mpDrawColor").addEventListener("change", function () {
      sendJson("/api/research/drawings/style", "POST", { color: $("mpDrawColor").value })
        .then(function (snap) {
          applyWorkspace(snap);
          return refreshDrawings();
        });
    });
    $("mpDrawWidth").addEventListener("change", function () {
      sendJson("/api/research/drawings/style", "POST", { width: Number($("mpDrawWidth").value) })
        .then(function (snap) {
          applyWorkspace(snap);
          return refreshDrawings();
        });
    });
    $("mpResetView").addEventListener("click", function () {
      var api = chartApi();
      if (api && api.resetView) api.resetView();
    });
    $("mpFullscreenBtn").addEventListener("click", expandChartUp);

    window.addEventListener("keydown", function (ev) {
      if (ev.key === "Shift") {
        window.__mpHostShift = true;
        var api = chartApi();
        if (api && api.setHostShift) api.setHostShift(true);
      }
      if (ev.key === "Escape") handleChartKey("escape");
      if (ev.key === "Delete" || ev.key === "Backspace") {
        var tag = (ev.target && ev.target.tagName) || "";
        if (tag !== "INPUT" && tag !== "SELECT" && tag !== "TEXTAREA") handleChartKey("delete");
      }
    });
    window.addEventListener("keyup", function (ev) {
      if (ev.key === "Shift") {
        window.__mpHostShift = false;
        var api = chartApi();
        if (api && api.setHostShift) api.setHostShift(false);
      }
    });
    window.addEventListener("blur", function () {
      window.__mpHostShift = false;
      var api = chartApi();
      if (api && api.setHostShift) api.setHostShift(false);
    });
    window.addEventListener("resize", resizeChart);
    document.addEventListener("fullscreenchange", resizeChart);
  }

  /* -------------------------------------------------------------------- layout */

  function resizeChart() {
    requestAnimationFrame(function () {
      var api = chartApi();
      if (api && api.resize) api.resize();
      scheduleDraw();
    });
  }

  function applyChartHeight(height, marginTop) {
    var stack = $("mpChartStack");
    var wrap = $("mpChart");
    var dock = stack && stack.querySelector(".mp-chart-dock");
    if (!stack || !wrap || !dock) return;
    var dockH = dock.offsetHeight || 36;
    var maxH = Math.max(240, window.innerHeight - dockH - 8);
    var h = Math.min(maxH, Math.max(240, Math.round(height)));
    var m = Math.round(marginTop);
    stack.style.marginTop = m + "px";
    var top = stack.getBoundingClientRect().top;
    if (top < 0) {
      m -= Math.round(top);
      stack.style.marginTop = m + "px";
    }
    wrap.style.height = h + "px";
    var btn = $("mpFullscreenBtn");
    var maxed = stack.getBoundingClientRect().top <= 4 && h >= maxH - 8;
    stack.classList.toggle("is-max", maxed);
    if (btn) {
      btn.classList.toggle("active", maxed);
      btn.title = maxed ? "Zurück" : "Chart aufziehen oder zurücksetzen";
    }
    try {
      localStorage.setItem(CHART_H_KEY, String(h));
      localStorage.setItem(CHART_MT_KEY, String(m));
    } catch (e) {}
    resizeChart();
  }

  function resetChartHeight() {
    applyChartHeight(Math.min(DEFAULT_CHART_H, Math.max(240, window.innerHeight - 280)), 0);
  }

  function expandChartUp() {
    var stack = $("mpChartStack");
    if (!stack) return;
    if (stack.classList.contains("is-max")) {
      resetChartHeight();
      return;
    }
    var rect = stack.getBoundingClientRect();
    var m = parseFloat(stack.style.marginTop) || 0;
    var dock = stack.querySelector(".mp-chart-dock");
    var dockH = (dock && dock.offsetHeight) || 36;
    applyChartHeight(window.innerHeight - dockH - 8, m - Math.max(0, rect.top));
  }

  function restoreChartHeight() {
    try {
      var h = parseInt(localStorage.getItem(CHART_H_KEY) || "", 10);
      var m = parseInt(localStorage.getItem(CHART_MT_KEY) || "", 10);
      if (!Number.isFinite(h) || h < 240 || h > window.innerHeight + 40) {
        resetChartHeight();
        return;
      }
      applyChartHeight(h, Number.isFinite(m) ? m : 0);
    } catch (e) {
      resetChartHeight();
    }
  }

  function plotRegion() {
    var wrap = $("price-pane") || $("mpChart");
    var w = wrap.clientWidth;
    var h = wrap.clientHeight;
    var axisW = 0;
    var axisH = 0;
    try { axisW = chart.priceScale("right").width() || 0; } catch (err) { axisW = 60; }
    try { axisH = chart.timeScale().height() || 0; } catch (err) { axisH = 28; }
    return { w: w, h: h, x1: Math.max(0, w - axisW), y1: Math.max(0, h - axisH) };
  }

  /* Map a bar index to a pixel column.
   *
   * The visible logical range covers the plot area linearly, so interpolating
   * across it is exact and works for indices that are scrolled out of view —
   * which anchored windows constantly are. timeToCoordinate returns null off
   * screen and logicalToCoordinate collapsed every window to zero width in
   * the vendored build, which silently reduced the histograms to sub-pixel
   * slivers. */
  function logicalToX(logical, region) {
    var range = chart.timeScale().getVisibleLogicalRange();
    if (!range || !isFinite(range.from) || !isFinite(range.to) || range.to === range.from) {
      return null;
    }
    return ((logical - range.from) / (range.to - range.from)) * region.x1;
  }

  /* Index of the first candle at or after `t`, and the last one before `end`. */
  function windowBarSpan(startUnix, endUnix) {
    if (!candleTimes.length) return null;
    var i0 = -1;
    var i1 = -1;
    for (var i = 0; i < candleTimes.length; i += 1) {
      var t = candleTimes[i];
      if (t >= startUnix && t < endUnix) {
        if (i0 === -1) i0 = i;
        i1 = i;
      }
    }
    if (i0 === -1) return null;
    return { i0: i0, i1: i1 };
  }

  function windowXSpan(profile, region) {
    var startUnix = Math.floor(Date.parse(profile.window.start) / 1000);
    var endUnix = Math.floor(Date.parse(profile.window.end) / 1000);
    var span = windowBarSpan(startUnix, endUnix);
    if (!span) return null;
    var x0 = logicalToX(span.i0 - 0.5, region);
    var x1 = logicalToX(span.i1 + 0.5, region);
    if (x0 === null || x1 === null || !(x1 > x0)) return null;
    return { x0: x0, x1: x1, i0: span.i0, i1: span.i1 };
  }

  function priceToY(price) {
    var y = candleSeries.priceToCoordinate(price);
    return y === null || y === undefined || !isFinite(y) ? null : y;
  }

  /* --------------------------------------------------------------------- draw */

  function scheduleDraw() {
    if (rafPending) return;
    rafPending = true;
    window.requestAnimationFrame(function () {
      rafPending = false;
      draw();
    });
  }

  function scheduleDrawDebounced(ms) {
    if (drawDebounceTimer) clearTimeout(drawDebounceTimer);
    drawDebounceTimer = setTimeout(function () {
      drawDebounceTimer = null;
      scheduleDraw();
    }, ms || 80);
  }

  function sizeCanvas(region) {
    var canvas = $("mpOverlay");
    var dpr = window.devicePixelRatio || 1;
    var w = region.w;
    var h = region.h;
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
    }
    canvas.style.width = w + "px";
    canvas.style.height = h + "px";
    var ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    return ctx;
  }

  function hLine(ctx, x0, x1, y, color, dash, width) {
    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = width || 1;
    ctx.setLineDash(dash || []);
    ctx.beginPath();
    ctx.moveTo(x0, Math.round(y) + 0.5);
    ctx.lineTo(x1, Math.round(y) + 0.5);
    ctx.stroke();
    ctx.restore();
  }

  function profileTpo(profile) {
    return profile && profile.tpo ? profile.tpo : null;
  }

  function profileVolume(profile) {
    return profile && profile.volume ? profile.volume : null;
  }

  function drawTpoHistogram(ctx, profile, span, s, region) {
    var tpo = profileTpo(profile);
    var bins = (tpo && tpo.bins) || [];
    if (!bins.length) return;

    var slotWidth = Math.max(2, span.x1 - span.x0);
    if (slotWidth < 4) return;
    // Soft silhouette only — filled bars read as a bright "box" on dark charts.
    var curveWidth = slotWidth * Math.min(0.45, s.width || 0.3);
    var maxCount = 0;
    for (var i = 0; i < bins.length; i += 1) {
      if (bins[i].tpo_count > maxCount) maxCount = bins[i].tpo_count;
    }
    if (maxCount <= 0) return;

    var points = [];
    for (var j = 0; j < bins.length; j += 1) {
      var bin = bins[j];
      if (!bin.tpo_count) continue;
      var yTop = priceToY(bin.price_high);
      var yBot = priceToY(bin.price_low);
      if (yTop === null || yBot === null) continue;
      if (yBot < -2 || yTop > region.y1 + 2) continue;
      points.push({
        x: span.x0 + (bin.tpo_count / maxCount) * curveWidth,
        y: (yTop + yBot) / 2
      });
    }
    if (points.length < 2) return;

    ctx.save();
    ctx.strokeStyle = "rgba(160, 180, 210, 0.55)";
    ctx.lineWidth = 1.25;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.beginPath();
    ctx.moveTo(points[0].x, points[0].y);
    for (var k = 1; k < points.length; k += 1) {
      ctx.lineTo(points[k].x, points[k].y);
    }
    ctx.stroke();
    ctx.restore();
  }

  function drawVolumeBuySellBars(ctx, profile, span, s, region) {
    var vol = profileVolume(profile);
    var bins = (vol && vol.bins) || [];
    if (!bins.length) return;

    var slotWidth = Math.max(2, span.x1 - span.x0);
    if (slotWidth < 6) return;
    var barsWidth = slotWidth * s.width;
    var maxVol = 0;
    for (var i = 0; i < bins.length; i += 1) {
      var base = bins[i].base_volume || 0;
      if (base > maxVol) maxVol = base;
    }
    if (maxVol <= 0) return;

    for (var j = 0; j < bins.length; j += 1) {
      var bin = bins[j];
      if (!bin.base_volume) continue;
      var yTop = priceToY(bin.price_high);
      var yBot = priceToY(bin.price_low);
      if (yTop === null || yBot === null) continue;
      if (yBot < -2 || yTop > region.y1 + 2) continue;
      var h = Math.max(1, yBot - yTop);
      var buyW = ((bin.buy_volume || 0) / maxVol) * barsWidth;
      var sellW = ((bin.sell_volume || 0) / maxVol) * barsWidth;
      ctx.fillStyle = COLORS.buy;
      ctx.fillRect(span.x0, yTop, buyW, h);
      ctx.fillStyle = COLORS.sell;
      ctx.fillRect(span.x0 + buyW, yTop, sellW, h);
    }
  }

  function drawProfileHistogram(ctx, profile, span, s, region) {
    drawTpoHistogram(ctx, profile, span, s, region);
    if (s.splitBuySell) {
      drawVolumeBuySellBars(ctx, profile, span, s, region);
    }
  }

  /* Volume profile as a single white curve: Y follows price (top→bottom),
   * X bulges right where traded volume was high. */
  function drawVolumeProfileLine(ctx, profile, span, s, region) {
    var vol = profileVolume(profile);
    var bins = (vol && vol.bins) || [];
    if (!bins.length) return;

    var slotWidth = Math.max(2, span.x1 - span.x0);
    var curveWidth = slotWidth * s.width;
    var maxVol = 0;
    for (var i = 0; i < bins.length; i += 1) {
      var base = bins[i].base_volume || 0;
      if (base > maxVol) maxVol = base;
    }
    if (maxVol <= 0) return;

    var sorted = bins.slice().sort(function (a, b) {
      var midA = a.price_high + a.price_low;
      var midB = b.price_high + b.price_low;
      return midB - midA;
    });

    var points = [];
    for (var j = 0; j < sorted.length; j += 1) {
      var bin = sorted[j];
      var yTop = priceToY(bin.price_high);
      var yBot = priceToY(bin.price_low);
      if (yTop === null || yBot === null) continue;
      if (yBot < -2 || yTop > region.y1 + 2) continue;
      var y = (yTop + yBot) / 2;
      var x = span.x0 + ((bin.base_volume || 0) / maxVol) * curveWidth;
      points.push({ x: x, y: y });
    }
    if (points.length < 2) return;

    ctx.save();
    ctx.strokeStyle = COLORS.volumeLine;
    ctx.lineWidth = 1.5;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.beginPath();
    ctx.moveTo(points[0].x, points[0].y);
    for (var k = 1; k < points.length; k += 1) {
      ctx.lineTo(points[k].x, points[k].y);
    }
    ctx.stroke();
    ctx.restore();
  }

  /* Value-area fill used to paint a translucent box over every window —
   * with short MP periods that reads as a bright "box view". Levels stay as
   * lines in drawWindowLevels; only optional single-print bands may fill. */
  function drawWindowBackground(ctx, profile, span, s) {
    if (!s.showSinglePrints) return;
    var vol = profileVolume(profile);
    var ranges = (vol && vol.nodes && vol.nodes.single_print_ranges) ||
      (profile.nodes && profile.nodes.single_print_ranges) || [];
    for (var i = 0; i < ranges.length; i += 1) {
      var yA = priceToY(ranges[i][1]);
      var yB = priceToY(ranges[i][0]);
      if (yA === null || yB === null) continue;
      ctx.fillStyle = COLORS.singlePrint;
      ctx.fillRect(span.x0, yA, span.x1 - span.x0, Math.max(1, yB - yA));
    }
  }

  /* Solid across the owning window, faded across the extension, so a level's
   * origin stays readable when a dozen of them are on screen. */
  function level(ctx, span, region, y, color, dash, width, extend) {
    hLine(ctx, span.x0, span.x1, y, color, dash, width);
    if (extend && region.x1 > span.x1) {
      ctx.save();
      ctx.globalAlpha = 0.42;
      hLine(ctx, span.x1, region.x1, y, color, dash && dash.length ? dash : [4, 5], width);
      ctx.restore();
    }
  }

  function drawWindowLevels(ctx, profile, span, s, region) {
    var tpo = profileTpo(profile);
    var va = (tpo && tpo.value_area) || profile.value_area || {};
    var vol = profileVolume(profile);
    var extend = s.extendLevels;

    if (s.showValueArea && va.vah != null && va.val != null) {
      var yH = priceToY(va.vah);
      var yL = priceToY(va.val);
      if (yH !== null) level(ctx, span, region, yH, COLORS.valueArea, [5, 4], 1, extend);
      if (yL !== null) level(ctx, span, region, yL, COLORS.valueArea, [5, 4], 1, extend);
    }

    if (s.showHvn) {
      var hvn = (vol && vol.nodes && vol.nodes.hvn) ||
        (profile.nodes && profile.nodes.hvn) || [];
      for (var k = 0; k < hvn.length; k += 1) {
        var yv = priceToY(hvn[k]);
        if (yv !== null) hLine(ctx, span.x0, span.x1, yv, COLORS.hvn, [2, 3], 1);
      }
    }

    if (s.showLvn) {
      var lvn = (vol && vol.nodes && vol.nodes.lvn) ||
        (profile.nodes && profile.nodes.lvn) || [];
      for (var m = 0; m < lvn.length; m += 1) {
        var yl = priceToY(lvn[m]);
        if (yl !== null) hLine(ctx, span.x0, span.x1, yl, COLORS.lvn, [2, 3], 1);
      }
    }

    if (s.showPoc && va.poc != null) {
      var yP = priceToY(va.poc);
      if (yP !== null) {
        // A naked POC is unfinished business, so it always reaches forward:
        // that is the whole point of flagging it.
        var naked = s.showNakedPoc && profile.naked_poc === true;
        level(
          ctx,
          span,
          region,
          yP,
          naked ? COLORS.nakedPoc : COLORS.poc,
          naked ? [7, 4] : [],
          naked ? 1.6 : 1.4,
          naked || extend
        );
      }
    }
  }

  function drawShapeLabel(ctx, profile, span, region, taken) {
    var shape = profile.shape || {};
    var kind = shape.kind || "UNCLEAR";
    var yTop = priceToY(profile.price_high);
    if (yTop === null) return;

    // Trailing asterisk marks the verdict as unvalidated, matching the table.
    var text = profile.window.label + "  " + kind + (shape.letter ? " (" + shape.letter + ")" : "") + "*";
    ctx.save();
    ctx.font = "10px -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif";
    ctx.textBaseline = "bottom";
    var w = ctx.measureText(text).width;
    var x = Math.min(span.x0 + 3, region.x1 - w - 4);
    var y = Math.max(12, yTop - 6);

    // Neighbouring windows often top out at a similar price, which stacks
    // their labels into an unreadable pile; nudge down until the slot is free.
    for (var attempt = 0; attempt < 8; attempt += 1) {
      var clash = false;
      for (var i = 0; i < taken.length; i += 1) {
        var t = taken[i];
        if (x < t.x + t.w + 4 && x + w + 4 > t.x && Math.abs(y - t.y) < 12) {
          clash = true;
          break;
        }
      }
      if (!clash) break;
      y += 13;
    }
    taken.push({ x: x, y: y, w: w });

    ctx.fillStyle = "rgba(19, 23, 34, 0.82)";
    ctx.fillRect(x - 2, y - 11, w + 4, 12);
    ctx.fillStyle = SHAPE_COLORS[kind] || "#d1d4dc";
    ctx.fillText(text, x, y);
    ctx.restore();
  }

  function draw() {
    if (!syncChartRefs()) return;
    var region = plotRegion();
    var ctx = sizeCanvas(region);
    if (!payload || !payload.profiles || !payload.profiles.length) return;

    var s = readSettings();

    ctx.save();
    ctx.beginPath();
    ctx.rect(0, 0, region.x1, region.y1);
    ctx.clip();

    // Layered in passes rather than per profile: a shaded area drawn for a
    // later window must not cover an earlier window's histogram.
    var visible = [];
    for (var i = 0; i < payload.profiles.length; i += 1) {
      var span = windowXSpan(payload.profiles[i], region);
      if (!span) continue;
      if (span.x1 < -50 || span.x0 > region.x1 + 50) continue;
      visible.push({ profile: payload.profiles[i], span: span });
    }

    var v;
    for (v = 0; v < visible.length; v += 1) {
      drawWindowBackground(ctx, visible[v].profile, visible[v].span, s);
    }

    // No vertical window dividers — they read as box borders around each profile.
    if (s.showHistogram) {
      for (v = 0; v < visible.length; v += 1) {
        drawProfileHistogram(ctx, visible[v].profile, visible[v].span, s, region);
      }
    }

    if (s.showVolumeLine) {
      for (v = 0; v < visible.length; v += 1) {
        drawVolumeProfileLine(ctx, visible[v].profile, visible[v].span, s, region);
      }
    }

    for (v = 0; v < visible.length; v += 1) {
      drawWindowLevels(ctx, visible[v].profile, visible[v].span, s, region);
    }

    if (s.showShape) {
      var taken = [];
      for (v = 0; v < visible.length; v += 1) {
        drawShapeLabel(ctx, visible[v].profile, visible[v].span, region, taken);
      }
    }
    ctx.restore();
  }

  /* ------------------------------------------------------------------ tooltip */

  function onHover(evt) {
    var tip = $("mpTooltip");
    if (!payload || !payload.profiles || !payload.profiles.length) {
      tip.hidden = true;
      return;
    }
    var wrap = $("price-pane") || $("mpChart");
    var rect = wrap.getBoundingClientRect();
    var x = evt.clientX - rect.left;
    var y = evt.clientY - rect.top;
    var region = plotRegion();
    if (x > region.x1 || y > region.y1) {
      tip.hidden = true;
      return;
    }

    var hit = null;
    var profiles = payload.profiles;
    for (var i = 0; i < profiles.length; i += 1) {
      var span = windowXSpan(profiles[i], region);
      if (!span) continue;
      if (x >= span.x0 && x <= span.x1) {
        hit = { profile: profiles[i], span: span };
        break;
      }
    }
    if (!hit) {
      tip.hidden = true;
      return;
    }

    var p = hit.profile;
    var tpo = profileTpo(p);
    var vol = profileVolume(p);
    var tpoVa = (tpo && tpo.value_area) || {};
    var volVa = (vol && vol.value_area) || {};
    var shape = p.shape || {};
    var price = candleSeries.coordinateToPrice(y);
    var tpoBin = null;
    var volBin = null;
    if (price !== null && price !== undefined) {
      if (tpo && tpo.bins) {
        for (var j = 0; j < tpo.bins.length; j += 1) {
          if (price >= tpo.bins[j].price_low && price < tpo.bins[j].price_high) {
            tpoBin = tpo.bins[j];
            break;
          }
        }
      }
      if (vol && vol.bins) {
        for (var k = 0; k < vol.bins.length; k += 1) {
          if (price >= vol.bins[k].price_low && price < vol.bins[k].price_high) {
            volBin = vol.bins[k];
            break;
          }
        }
      }
    }

    var rows = [
      "<strong>" + p.window.label + "</strong>",
      "Shape: <span style=\"color:" + (SHAPE_COLORS[shape.kind] || "#d1d4dc") + "\">" +
        (shape.kind || "-") + "</span>*",
      "TPO POC " + fmtPrice(tpoVa.poc) + " · VAH " + fmtPrice(tpoVa.vah) + " · VAL " + fmtPrice(tpoVa.val),
      "Vol VPOC " + fmtPrice(volVa.poc) + " · VVAH " + fmtPrice(volVa.vah) + " · VVAL " + fmtPrice(volVa.val),
      "Range " + fmtPrice(p.price_low) + " – " + fmtPrice(p.price_high),
      "Naked POC: " + (p.naked_poc === true ? "ja" : p.naked_poc === false ? "nein" : "–")
    ];
    if (tpoBin) {
      rows.push(
        "— TPO Bin " + fmtPrice(tpoBin.price_low) + "–" + fmtPrice(tpoBin.price_high) +
          ": count " + fmtNum(tpoBin.tpo_count)
      );
    }
    if (volBin) {
      rows.push(
        "— Vol Bin " + fmtPrice(volBin.price_low) + "–" + fmtPrice(volBin.price_high) +
          ": base " + fmtNum(volBin.base_volume) + ", Δ " + fmtNum(volBin.delta) +
          ", " + (volBin.trades || 0) + " Trades"
      );
    }

    tip.innerHTML = rows.join("<br>");
    tip.hidden = false;
    var tw = tip.offsetWidth;
    var th = tip.offsetHeight;
    var left = x + 14;
    if (left + tw > region.x1) left = Math.max(4, x - tw - 14);
    var top = y + 14;
    if (top + th > region.y1) top = Math.max(4, y - th - 14);
    tip.style.left = left + "px";
    tip.style.top = top + "px";
  }

  /* ------------------------------------------------------------------ formats */

  function fmtPrice(v) {
    if (v === null || v === undefined || !isFinite(v)) return "–";
    var abs = Math.abs(v);
    var dp = abs >= 1000 ? 1 : abs >= 10 ? 2 : abs >= 0.1 ? 4 : 6;
    return v.toFixed(dp);
  }

  function fmtNum(v) {
    if (v === null || v === undefined || !isFinite(v)) return "–";
    var abs = Math.abs(v);
    if (abs >= 1e9) return (v / 1e9).toFixed(2) + "B";
    if (abs >= 1e6) return (v / 1e6).toFixed(2) + "M";
    if (abs >= 1e3) return (v / 1e3).toFixed(2) + "K";
    return v.toFixed(2);
  }

  function renderLegend() {
    var items = [
      ["TPO POC", COLORS.poc, false],
      ["TPO VAH / VAL", COLORS.valueArea, true],
      ["Naked POC", COLORS.nakedPoc, true],
      ["Vol HVN", COLORS.hvn, true],
      ["Vol LVN", COLORS.lvn, true],
      ["Buy-Volumen", COLORS.buy, false],
      ["Sell-Volumen", COLORS.sell, false],
      ["TPO Bracket", COLORS.total, false],
      ["Volumenlinie", COLORS.volumeLine, true]
    ];
    $("mpLegend").innerHTML = items
      .map(function (it) {
        return (
          "<span class=\"mp-legend-item\"><span class=\"mp-swatch" +
          (it[2] ? " is-dashed" : "") +
          "\" style=\"" + (it[2] ? "color:" + it[1] : "background:" + it[1]) + "\"></span>" +
          it[0] + "</span>"
        );
      })
      .join("");
  }

  /* --------------------------------------------------------------------- load */

  function load() {
    var s = readSettings();
    var range = resolveRange(s);
    if (!range) {
      setStatus("Bitte Von/Bis wählen", "error");
      return;
    }
    if (s.anchor === "session" && !s.sessions.length) {
      setStatus("Mindestens eine Session wählen", "error");
      return;
    }

    if (inflight) inflight.abort();
    stopLivePoll();
    var ctrl = new AbortController();
    inflight = ctrl;

    var params = new URLSearchParams({
      symbol: s.symbol,
      start: String(range.start),
      end: String(range.end),
      mp_timeframe: s.mpTimeframe || s.anchor,
      timeframe: s.timeframe,
      value_area_pct: String(s.valueAreaPct / 100),
      target_bins: String(s.targetBins),
      final: s.final ? "1" : "0"
    });
    if ((s.mpTimeframe || s.anchor) === "session") params.set("sessions", s.sessions.join(","));

    $("mpLoad").disabled = true;
    setStatus("lädt …", "busy");
    if ($("mpEmpty")) {
      $("mpEmpty").hidden = false;
      $("mpEmpty").textContent = "Chart wird geladen …";
    }

    fetch("/api/market-profile/profiles?" + params.toString(), {
      signal: ctrl.signal,
      credentials: "same-origin"
    })
      .then(function (res) {
        return res.json().then(function (body) { return { ok: res.ok, body: body }; });
      })
      .then(function (out) {
        if (ctrl.signal.aborted) return;
        if (!out.ok || !out.body || out.body.success !== true) {
          var msg = (out.body && (out.body.message || out.body.error)) || "Fehler";
          setStatus(msg, "error");
          if ($("mpEmpty")) {
            $("mpEmpty").hidden = false;
            $("mpEmpty").textContent = msg;
          }
          return;
        }
        payload = out.body;
        lastLoadRange = range;
        candleTimes = (payload.candles || []).map(function (c) { return c.time; });
        // Paint candles + profiles first; overlays are best-effort and must not
        // turn a successful profile response into a fake Netzwerkfehler.
        try {
          applyPayloadToChart(s);
          $("mpEmpty").hidden = true;
          renderLegend();
          scheduleDraw();
          startLivePoll();
        } catch (paintErr) {
          setStatus("Chart-Render: " + (paintErr && paintErr.message ? paintErr.message : paintErr), "error");
        }

        var m = payload.meta || {};
        var skipped = (m.skipped_windows || []).length;
        setStatus(
          m.profiles_built + "/" + m.windows + " Fenster" +
            (skipped ? ", " + skipped + " ohne Daten" : "") +
            " · " + (payload.candles || []).length + " Kerzen" +
            (payload.cached ? " · cached" : "") +
            " · Overlays …"
        );

        return Promise.all([
          fetchEmaOverlays(s.symbol, s.timeframe, range).then(applyEmaOverlays).catch(function () {}),
          refreshDrawings(),
          refreshLiquidityLocation()
        ]).then(function () {
          setStatus(
            m.profiles_built + "/" + m.windows + " Fenster" +
              (skipped ? ", " + skipped + " ohne Daten" : "") +
              " · " + (payload.candles || []).length + " Kerzen" +
              (payload.cached ? " · cached" : "")
          );
          try {
            // Preserve pan/zoom after overlay refresh — first paint already set the view.
            if (applyPayloadToChart(s, { preserveView: true })) scheduleDraw();
            startLivePoll();
          } catch (err2) { /* already painted */ }
        });
      })
      .catch(function (err) {
        if (err && err.name === "AbortError") return;
        var msg = "Netzwerkfehler: " + (err && err.message ? err.message : err);
        setStatus(msg, "error");
        if ($("mpEmpty")) {
          $("mpEmpty").hidden = false;
          $("mpEmpty").textContent = msg;
        }
      })
      .then(function () {
        if (inflight === ctrl) inflight = null;
        $("mpLoad").disabled = false;
      });
  }

  /* --------------------------------------------------------------------- init */

  function syncConditionalControls() {
    $("mpSessionsGroup").hidden = $("mpAnchor").value !== "session";
    $("mpCustomRange").hidden = $("mpDays").value !== "custom";
  }

  function bind() {
    $("mpLoad").addEventListener("click", function () {
      persistSettings();
      load();
    });

    function reloadFromControls(ev) {
      syncConditionalControls();
      persistSettings();
      // Switching to "benutzerdefiniert" only reveals the date fields —
      // wait until Von/Bis are filled before fetching.
      if (ev && ev.target && ev.target.id === "mpDays" && $("mpDays").value === "custom") {
        return;
      }
      onOrderbookSymbolOrViewChange();
      load();
    }

    // Symbol / KERZEN / MARKET PROFILE / Zeitraum: independent controls;
    // each change reloads, but values are never copied across.
    ["mpSymbol", "mpTimeframe", "mpAnchor", "mpDays"].forEach(function (id) {
      $(id).addEventListener("change", reloadFromControls);
    });
    ["mpStart", "mpEnd"].forEach(function (id) {
      var el = $(id);
      if (!el) return;
      el.addEventListener("change", function () {
        persistSettings();
        if ($("mpDays").value === "custom") load();
      });
    });

    // Drawing-only toggles never refetch: the payload already carries every
    // level, so a redraw is enough and costs no ClickHouse work.
    [
      "mpShowHistogram", "mpShowVolumeLine", "mpSplitBuySell", "mpWidth", "mpShowPoc", "mpShowValueArea",
      "mpShowHvn", "mpShowLvn", "mpShowSinglePrints", "mpShowNakedPoc",
      "mpExtendLevels", "mpShowShape"
    ].forEach(function (id) {
      var el = $(id);
      if (!el) return;
      el.addEventListener("change", function () {
        persistSettings();
        scheduleDraw();
      });
    });

    $("mpShowLiquidity").addEventListener("change", function () {
      persistSettings();
      refreshLiquidityLocation();
    });

    // These change what gets computed, so they need a reload to take effect
    // (FINAL is expensive — keep as explicit Laden / advanced only).
    ["mpValueAreaPct", "mpTargetBins", "mpFinal"].forEach(function (id) {
      $(id).addEventListener("change", persistSettings);
    });
    Array.prototype.forEach.call(document.querySelectorAll(".mp-session"), function (el) {
      el.addEventListener("change", persistSettings);
    });
  }

  function start() {
    restoreSettings();
    // First visit / stale localStorage: keep 30d as the working default.
    if (!$("mpDays").value) $("mpDays").value = "30";
    // Defaults if empty — do not couple KERZEN and MARKET PROFILE.
    var candleEl = $("mpTimeframe");
    if (candleEl && !candleEl.value) candleEl.value = "15m";
    var mpEl = $("mpAnchor");
    if (mpEl && !mpEl.value) mpEl.value = "day";
    syncConditionalControls();
    bindChartChrome();
    bind();
    initOrderbookControls();
    restoreChartHeight();
    renderLegend();
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) return;
      if (!payload || !payload.candles || !payload.candles.length) return;
      if (!formingTimer) startLivePoll();
      else pollForming(livePollGen);
      onOrderbookSymbolOrViewChange();
    });
    window.addEventListener("pagehide", function () {
      releaseOb1000LeaseBestEffort();
    });
    sendJson("/api/research/workspace").then(function (snap) {
      applyWorkspace(snap);
    }).catch(function () { /* workspace optional */ });
    whenChartReady(function () {
      if (payload) {
        applyPayloadToChart(readSettings());
        scheduleDraw();
        startLivePoll();
      }
      onOrderbookSymbolOrViewChange();
    });
    // Auto-load immediately with the default/persisted range (do not wait for
    // the chart bridge — that left the empty placeholder stuck forever).
    load();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
