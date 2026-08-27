/**
 * Research Charts host — TRP chart.js is the renderer (iframe per pane).
 * This file is the Qt MainWindow equivalent: toolbar, pane pool, REST bridge.
 * JS does not compute stochastic, EMA, liquidity, TF aggregation, or talk to Bybit.
 * Live delivery: 5s incremental poll of closed 1m from existing Research APIs.
 */
(function () {
  "use strict";

  const PANE_IDS = ["pane-0", "pane-1", "pane-2", "pane-3"];
  const PANE_COUNT = { 1: 1, "2H": 2, "2V": 2, 4: 4 };
  const DEFAULT_TFS = { "pane-0": "1m", "pane-1": "5m", "pane-2": "15m", "pane-3": "1h" };
  const TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h"];
  const TF_SEC = { "1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400 };
  const SYMBOL_KEY = "research.symbol";
  const LAYOUT_KEY = "research.layout";
  const HISTORY_KEY = "research.history";
  const SYNC_CHART_KEY = "research.sync_chart_after_bt";
  const HISTORY_SPAN_DAYS = { rolling: 17, "7d": 7, "30d": 30, "90d": 90 };
  const ASSET_V = "live-16";
  const VP_KEY = "research.volume_profile";
  const OBP_KEY = "research.orderbook_profile";
  const VP_DEBOUNCE_MS = 400;
  const OBP_REFRESH_MS = 60 * 1000;
  const STOCH_STRATEGY_KEY = "stoch.strategy_version";
  const STOCH_SYMBOL_KEY = "stoch.last_symbol";
  const STOCH_JOB_KEY = "stoch.research_job_id";
  const STOCH_SOURCE_KEY = "stoch.signal_source";
  const STOCH_EVAL_KEY = "stoch.research_evaluation_id";
  const POLL_MS = 5000;
  const FORMING_MS = 250;
  const LIVE_DIAG = true;

  const TOOLS = [
    ["select", "Auswählen"],
    ["trend", "Trendlinie"],
    ["hline", "Horizontale Linie"],
    ["vline", "Vertikale Linie"],
    ["rectangle", "Rechteck"],
    ["circle", "Kreis"],
    ["arrow", "Pfeil"],
    ["measure", "Messen"],
    ["long_position", "Long-Position"],
    ["short_position", "Short-Position"],
  ];

  const state = {
    symbols: [],
    layout: "1",
    paneFs: null,
    symbol: "",
    sync: true,
    panes: {},
    pollGen: 0,
    pollTimer: null,
    formingTimer: null,
    obpRefreshTimer: null,
    loadGen: 0,
    loadAbort: null,
    initialLoadDone: false,
    requestLog: [],
    liveStatus: null,
    workspace: null,
    hoverUnix: null,
    hoverPane: null,
    selectedUnix: null,
    selectedPane: null,
    syncGeneration: 0,
    emaDraft: null,
    overlayTest: false,
    posGuard: false,
    phase: "IFRAME_LOADING",
    hostShift: false,
    vp: { enabled: false, rows: "auto", display: "buy_sell", poc: true, value_area: true, width: "normal", volume_mode: "base" },
    obp: { enabled: false, width: "normal", mode: "snapshot_at" },
    history: {
      preset: "30d",
      customStart: "",
      customEnd: "",
      loadedFrom: null,
      loadedTo: null,
      pinned: false,
    },
  };
  const inflightGets = {};
  const inflightPosts = {};
  const PANE_HTTP_LIMIT = 2;

  const $ = (id) => document.getElementById(id);

  function logReq(entry) {
    const row = Object.assign({ t: Date.now() }, entry || {});
    state.requestLog.push(row);
    if (state.requestLog.length > 120) state.requestLog.shift();
    return row;
  }

  async function mapLimit(items, limit, fn) {
    const list = items || [];
    const out = new Array(list.length);
    let cursor = 0;
    async function worker() {
      while (cursor < list.length) {
        const idx = cursor++;
        out[idx] = await fn(list[idx], idx);
      }
    }
    const n = Math.max(0, Math.min(limit || 1, list.length));
    await Promise.all(Array.from({ length: n }, worker));
    return out;
  }

  function floorUtc(unix, tf) {
    const step = TF_SEC[tf] || 60;
    return Math.floor(Number(unix) / step) * step;
  }

  function fmtUtc(unix) {
    if (unix == null) return "—";
    const d = new Date(Number(unix) * 1000);
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())} UTC`;
  }

  function toLocalInputValue(d) {
    const pad = (n) => String(n).padStart(2, "0");
    return d.getUTCFullYear() + "-" + pad(d.getUTCMonth() + 1) + "-" + pad(d.getUTCDate())
      + "T" + pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes());
  }

  function utcInputToUnix(v) {
    if (!v) return null;
    const ms = Date.parse(String(v).length === 16 ? String(v) + ":00Z" : String(v));
    return Number.isFinite(ms) ? Math.floor(ms / 1000) : null;
  }

  function readHistoryFromUi() {
    const preset = ($("researchHistoryPreset") || {}).value || "30d";
    state.history.preset = preset;
    state.history.customStart = ($("researchHistoryStart") || {}).value || "";
    state.history.customEnd = ($("researchHistoryEnd") || {}).value || "";
    try {
      localStorage.setItem(HISTORY_KEY, JSON.stringify({
        preset: state.history.preset,
        customStart: state.history.customStart,
        customEnd: state.history.customEnd,
      }));
    } catch (e) { /* ignore */ }
  }

  function restoreHistoryPrefs() {
    try {
      const raw = localStorage.getItem(HISTORY_KEY);
      if (raw) {
        const o = JSON.parse(raw);
        if (o && o.preset) state.history.preset = o.preset;
        if (o && o.customStart) state.history.customStart = o.customStart;
        if (o && o.customEnd) state.history.customEnd = o.customEnd;
      }
    } catch (e) { /* ignore */ }
    try {
      const sync = localStorage.getItem(SYNC_CHART_KEY);
      if ($("researchSyncChartAfterBt") && sync != null) {
        $("researchSyncChartAfterBt").checked = sync === "1";
      }
    } catch (e2) { /* ignore */ }
    if ($("researchHistoryPreset")) $("researchHistoryPreset").value = state.history.preset || "30d";
    if ($("researchHistoryStart") && state.history.customStart) {
      $("researchHistoryStart").value = state.history.customStart;
    }
    if ($("researchHistoryEnd") && state.history.customEnd) {
      $("researchHistoryEnd").value = state.history.customEnd;
    }
    syncHistoryCustomUi();
    updateHistoryHint();
  }

  function syncHistoryCustomUi() {
    const custom = ($("researchHistoryPreset") || {}).value === "custom";
    if ($("researchHistoryCustomWrap")) $("researchHistoryCustomWrap").hidden = !custom;
    if (custom && $("researchHistoryStart") && !$("researchHistoryStart").value) {
      const end = new Date();
      const start = new Date(end.getTime() - 30 * 86400000);
      $("researchHistoryStart").value = toLocalInputValue(start);
      $("researchHistoryEnd").value = toLocalInputValue(end);
    }
  }

  function historySpanSeconds() {
    const p = state.history.preset || "30d";
    if (p === "custom") {
      const from = utcInputToUnix(state.history.customStart);
      const to = utcInputToUnix(state.history.customEnd);
      if (from != null && to != null && to > from) return to - from;
    }
    const days = HISTORY_SPAN_DAYS[p];
    return (days || 30) * 86400;
  }

  function computeHistoryRangeUnix(override) {
    if (override && override.from != null && override.to != null) {
      return { from: Math.floor(Number(override.from)), to: Math.floor(Number(override.to)) };
    }
    const now = Math.floor(Date.now() / 1000);
    const p = state.history.preset || "30d";
    if (p === "rolling") return { from: null, to: null };
    if (p === "7d" || p === "30d" || p === "90d") {
      const days = HISTORY_SPAN_DAYS[p] || 30;
      return { from: now - days * 86400, to: now };
    }
    if (p === "custom") {
      const from = utcInputToUnix(state.history.customStart);
      const to = utcInputToUnix(state.history.customEnd);
      if (from != null && to != null && to > from) return { from: from, to: to };
    }
    return { from: null, to: null };
  }

  function resolvePaneLoadRange(opts) {
    if (opts && opts.from != null && opts.to != null) {
      return { from: Math.floor(Number(opts.from)), to: Math.floor(Number(opts.to)) };
    }
    return computeHistoryRangeUnix();
  }

  function updateHistoryHint(range) {
    const hint = $("researchHistoryHint");
    if (!hint) return;
    const r = range || computeHistoryRangeUnix();
    if (r.from == null || r.to == null) {
      hint.textContent = "Rolling · Backend-Limit (~17d @15m)";
      return;
    }
    hint.textContent = "Geladen UTC: " + fmtUtc(r.from) + " → " + fmtUtc(r.to);
  }

  /** Freeze live forming/poll only for true historical pins (past custom/backtest end). */
  function historyBlocksLive() {
    if (!state.history || !state.history.pinned) return false;
    const preset = state.history.preset || "30d";
    // Rolling / N-day presets are live windows ending at "now".
    if (preset === "rolling" || preset === "7d" || preset === "30d" || preset === "90d") {
      return false;
    }
    const to = state.history.loadedTo;
    if (to == null) return false;
    const now = Math.floor(Date.now() / 1000);
    return (now - Number(to)) > 300;
  }

  async function reloadVisibleHistory(opts) {
    readHistoryFromUi();
    const o = opts || {};
    const range = resolvePaneLoadRange(o);
    if (range.from != null) {
      state.history.loadedFrom = range.from;
      state.history.loadedTo = range.to;
      state.history.pinned = true;
    } else {
      state.history.loadedFrom = null;
      state.history.loadedTo = null;
      state.history.pinned = false;
    }
    updateHistoryHint(range);
    await mapLimit(visibleIds(), PANE_HTTP_LIMIT, function (pid) {
      return loadPane(pid, Object.assign({
        force: !o.jumpToUnix,
        sourceAction: o.sourceAction || "history-reload",
        from: range.from,
        to: range.to,
        jumpToUnix: o.jumpToUnix,
        jumpPadSec: o.jumpPadSec,
      }, o));
    });
    if (o.jumpToUnix != null) {
      await jumpChartsToUnix(o.jumpToUnix, o.jumpPadSec);
    }
  }

  async function jumpChartsToUnix(ts, padSec) {
    const center = Math.floor(Number(ts));
    if (!Number.isFinite(center)) return false;
    const pad = padSec != null ? padSec : Math.max(900 * 40, Math.floor(historySpanSeconds() / 4));
    let ok = false;
    await Promise.all(visibleIds().map(async function (pid) {
      const pane = state.panes[pid];
      if (!pane) return;
      const chart = api(pane) || await whenReady(pane, 8000);
      if (!chart) return;
      if (chart.focusOnTime && chart.focusOnTime(center, pad)) ok = true;
      else if (chart.setVisibleTimeRange) {
        try {
          if (chart.setVisibleTimeRange(center - pad, center + pad)) ok = true;
        } catch (e) { /* ignore */ }
      }
    }));
    return ok;
  }

  function paneCandleBounds(pane) {
    const candles = (pane && pane.pendingData && pane.pendingData.candles) || [];
    if (!candles.length) return null;
    const times = candles.map(function (c) { return Number(c.time); }).filter(Number.isFinite);
    if (!times.length) return null;
    return { from: Math.min.apply(null, times), to: Math.max.apply(null, times) };
  }

  function visiblePanesCoverRange(from, to) {
    if (from == null || to == null) return false;
    const ids = visibleIds();
    if (!ids.length) return false;
    return ids.every(function (pid) {
      const b = paneCandleBounds(state.panes[pid]);
      if (!b) return false;
      return b.from <= from && b.to >= to;
    });
  }

  function visiblePanesContainTime(ts) {
    const t = Math.floor(Number(ts));
    if (!Number.isFinite(t)) return false;
    const ids = visibleIds();
    if (!ids.length) return false;
    return ids.every(function (pid) {
      const b = paneCandleBounds(state.panes[pid]);
      if (!b) return false;
      return b.from <= t && b.to >= t;
    });
  }

  function mergedPaneBounds() {
    let from = null;
    let to = null;
    visibleIds().forEach(function (pid) {
      const b = paneCandleBounds(state.panes[pid]);
      if (!b) return;
      from = from == null ? b.from : Math.min(from, b.from);
      to = to == null ? b.to : Math.max(to, b.to);
    });
    return from == null ? null : { from: from, to: to };
  }

  async function goToDateTime() {
    if (!state.symbol) return;
    const ts = utcInputToUnix(($("researchGoTo") || {}).value);
    if (ts == null) {
      setStatus("Go To: Datum/Zeit (UTC) eingeben", "error");
      return;
    }
    const span = historySpanSeconds();
    const half = Math.max(Math.floor(span / 2), 86400);
    const viewPad = Math.min(half, 86400 * 14);
    const now = Math.floor(Date.now() / 1000);
    let loadFrom = ts - half;
    let loadTo = ts + half;
    if (loadTo > now + 900) loadTo = now + 900;
    state.history.pinned = true;
    const needsLoad = !visiblePanesContainTime(ts) || !visiblePanesCoverRange(loadFrom, loadTo);
    if (needsLoad) {
      setStatus("Go To: History laden " + fmtUtc(loadFrom) + " …");
      await reloadVisibleHistory({
        from: loadFrom,
        to: loadTo,
        sourceAction: "go-to-load",
      });
    }
    if (!visiblePanesContainTime(ts)) {
      setStatus("Go To: Nachladen " + fmtUtc(loadFrom) + " …");
      await reloadVisibleHistory({
        from: loadFrom,
        to: loadTo,
        sourceAction: "go-to-retry",
      });
    }
    if (!visiblePanesContainTime(ts)) {
      const bounds = mergedPaneBounds();
      const detail = bounds
        ? (" · Kerzen UTC " + fmtUtc(bounds.from) + " → " + fmtUtc(bounds.to))
        : "";
      setStatus("Go To: " + fmtUtc(ts) + " liegt außerhalb der geladenen Kerzen" + detail, "error");
      return;
    }
    if (!(await jumpChartsToUnix(ts, viewPad))) {
      setStatus("Go To: Zoom auf " + fmtUtc(ts) + " fehlgeschlagen", "error");
      return;
    }
    setStatus("Go To · " + fmtUtc(ts));
  }

  async function syncChartAfterBacktest(startIso, endIso, focusIso) {
    if (!$("researchSyncChartAfterBt") || !$("researchSyncChartAfterBt").checked) return;
    const start = startIso ? Math.floor(Date.parse(startIso) / 1000) : null;
    const end = endIso ? Math.floor(Date.parse(endIso) / 1000) : null;
    if (start == null || end == null || end <= start) return;
    const warmup = 2 * 86400;
    const from = start - warmup;
    const to = end + 86400;
    const focus = focusIso ? Math.floor(Date.parse(focusIso) / 1000) : Math.floor((start + end) / 2);
    const pad = Math.min(Math.floor((end - start) / 2) + warmup, 86400 * 10);
    setStatus("Chart sync Backtest-Fenster …");
    await reloadVisibleHistory({
      from: from,
      to: to,
      jumpToUnix: focus,
      jumpPadSec: pad,
      sourceAction: "backtest-sync",
    });
  }

  function bindHistoryUi() {
    if ($("researchHistoryPreset")) {
      $("researchHistoryPreset").addEventListener("change", function () {
        syncHistoryCustomUi();
        readHistoryFromUi();
        updateHistoryHint();
      });
    }
    if ($("researchHistoryApply")) {
      $("researchHistoryApply").addEventListener("click", async function () {
        readHistoryFromUi();
        const preset = state.history.preset || "30d";
        state.history.pinned = preset !== "rolling";
        setStatus("History laden …");
        try {
          await reloadVisibleHistory({ sourceAction: "history-apply" });
          setStatus("History geladen · " + (($("researchHistoryHint") || {}).textContent || ""));
        } catch (err) {
          setStatus("History laden fehlgeschlagen: " + (err.message || err), "error");
        }
      });
    }
    if ($("researchGoToBtn")) {
      $("researchGoToBtn").addEventListener("click", function () {
        goToDateTime().catch(function (err) {
          setStatus("Go To fehlgeschlagen: " + (err.message || err), "error");
        });
      });
    }
    if ($("researchGoTo")) {
      $("researchGoTo").addEventListener("keydown", function (ev) {
        if (ev.key === "Enter") {
          ev.preventDefault();
          goToDateTime().catch(function (err) {
            setStatus("Go To fehlgeschlagen: " + (err.message || err), "error");
          });
        }
      });
    }
    if ($("researchSyncChartAfterBt")) {
      $("researchSyncChartAfterBt").addEventListener("change", function () {
        try {
          localStorage.setItem(SYNC_CHART_KEY, $("researchSyncChartAfterBt").checked ? "1" : "0");
        } catch (e) { /* ignore */ }
      });
    }
  }

  function setStatus(text, kind) {
    const el = $("researchStatus");
    if (!el) return;
    el.className = "research-status" + (kind ? " " + kind : "");
    el.textContent = text || "";
  }

  function chipClass(label) {
    const v = String(label || "").toUpperCase();
    if (v === "LIVE" || v === "RUNNING") return "research-chip research-chip-live";
    if (["RECOVERING", "STARTING", "CONNECTING", "SUBSCRIBING", "RECONNECTING", "STALE", "UNAVAILABLE"].includes(v)) {
      return "research-chip research-chip-warn";
    }
    if (["ERROR", "DEGRADED", "LIVE NOT AVAILABLE", "LIVE_NOT_AVAILABLE"].includes(v)) {
      return "research-chip research-chip-err";
    }
    if (["STOPPED", "STOPPING", "HISTORICAL", "HISTORICAL ONLY"].includes(v)) {
      return "research-chip research-chip-hist";
    }
    return "research-chip";
  }

  function setChip(id, label) {
    const el = $(id);
    if (!el) return;
    const text = label == null || label === "" ? "–" : String(label);
    el.className = chipClass(text);
    el.textContent = text;
  }

  function visibleIds() {
    if (state.paneFs && PANE_IDS.indexOf(state.paneFs) >= 0) return [state.paneFs];
    return PANE_IDS.slice(0, PANE_COUNT[state.layout] || 1);
  }

  function api(pane) {
    const win = pane && pane.iframe && pane.iframe.contentWindow;
    return win && win.chartApi ? win.chartApi : null;
  }

  function syncHostShift(on) {
    state.hostShift = !!on;
    PANE_IDS.forEach(function (pid) {
      const pane = state.panes[pid];
      const win = pane && pane.iframe && pane.iframe.contentWindow;
      if (!win) return;
      win.__hostShift = state.hostShift;
      const chart = api(pane);
      if (chart && chart.setHostShift) chart.setHostShift(state.hostShift);
    });
  }

  function httpError(res, body, url) {
    const detail = body && (body.message || body.error || body.detail);
    const text = typeof detail === "string" && detail ? detail : (res.statusText || "request failed");
    return new Error(res.status + " " + text + " (" + url + ")");
  }

  async function getJson(url, meta) {
    const silent = !!(meta && meta.silent);
    const info = silent ? null : logReq(Object.assign({
      method: "GET",
      url: url,
      symbol: state.symbol,
      timeframe: meta && meta.timeframe,
      sourceAction: (meta && meta.sourceAction) || "get",
      pane: meta && meta.pane,
      generation: state.loadGen,
    }, meta || {}));
    if (inflightGets[url]) {
      if (info) info.coalesced = true;
      return inflightGets[url];
    }
    const pending = (async function () {
      const res = await fetch(url, {
        credentials: "same-origin",
        signal: meta && meta.signal,
      });
      const body = await res.json().catch(function () { return {}; });
      if (!res.ok) throw httpError(res, body, url);
      return body;
    })();
    inflightGets[url] = pending;
    try {
      return await pending;
    } finally {
      if (inflightGets[url] === pending) delete inflightGets[url];
    }
  }

  async function sendJson(url, method, payload, meta) {
    const key = method + " " + url + "\n" + JSON.stringify(payload || {});
    const info = logReq(Object.assign({
      method: method,
      url: url,
      symbol: (payload && payload.symbol) || state.symbol,
      timeframe: payload && payload.timeframe,
      sourceAction: (meta && meta.sourceAction) || method.toLowerCase(),
      pane: meta && meta.pane,
      generation: state.loadGen,
    }, meta || {}));
    if (inflightPosts[key]) {
      info.coalesced = true;
      return inflightPosts[key];
    }
    const pending = (async function () {
      const res = await fetch(url, {
        method: method,
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload || {}),
        signal: meta && meta.signal,
      });
      const body = await res.json().catch(function () { return {}; });
      if (!res.ok) throw httpError(res, body, url);
      return body;
    })();
    inflightPosts[key] = pending;
    try {
      return await pending;
    } finally {
      if (inflightPosts[key] === pending) delete inflightPosts[key];
    }
  }

  function makeReadyGate() {
    let resolveReady;
    const promise = new Promise(function (resolve) { resolveReady = resolve; });
    return { promise: promise, resolve: resolveReady };
  }

  async function whenReady(pane, timeoutMs) {
    const existing = api(pane);
    if (existing) {
      pane.ready = true;
      pane.phase = "CHART_API_READY";
      return existing;
    }
    if (!pane || !pane.readyPromise) return null;
    await Promise.race([
      pane.readyPromise,
      new Promise(function (resolve) { setTimeout(resolve, timeoutMs || 8000); }),
    ]);
    return api(pane);
  }

  function attachBridge(pane) {
    const win = pane.iframe && pane.iframe.contentWindow;
    if (!win) return;
    win.bridge = {
      on_chart_ready: function () {
        pane.ready = true;
        pane.phase = "CHART_API_READY";
        if (pane._resolveReady) pane._resolveReady();
        const chart = api(pane);
        if (!chart) return;
        if (pane.pendingData) {
          chart.setData(pane.pendingData);
          pane.phase = "DATA_READY";
        }
        if (pane.pendingEma) chart.setEmaOverlays(pane.pendingEma);
        if (pane.pendingLower) chart.setLowerPane(pane.pendingLower);
        if (pane.pendingLldEma) chart.setLldEma(pane.pendingLldEma);
        if (pane.pendingOverlays) syncOverlays(pane, pane.pendingOverlays);
        chart.setInteractionMode(toolMode());
        pane.phase = "INTERACTION_READY";
        chart.resize();
        if (chart.setHostShift) chart.setHostShift(!!state.hostShift);
      },
      on_crosshair_move: function (unix) { handleHover(pane.id, unix); },
      on_chart_click: function (unix) { handleClick(pane.id, unix); },
      on_crosshair_leave: function () { handleHoverLeft(pane.id); },
      on_visible_range: function (from, to) {
        scheduleVolumeProfile(pane, from, to);
        scheduleOrderbookProfile(pane, from, to);
      },
      on_drawing_event: function (blob) { handleDrawingEvent(pane.id, blob); },
      on_tool_idle: function () { deactivateToolsLocal(); },
      on_chart_key: function (key) { handleChartKey(key); },
    };
  }

  function toolMode() {
    const tool = (state.workspace && state.workspace.tool) || "select";
    return tool === "select" ? "select" : tool;
  }

  function deactivateToolsLocal() {
    if (!state.workspace) state.workspace = {};
    state.workspace.tool = "select";
    state.workspace.pending = false;
    state.workspace.preview_anchor = null;
    document.querySelectorAll(".trp-tool-btn[data-tool]").forEach(function (btn) {
      btn.classList.toggle("active", btn.dataset.tool === "select");
    });
    PANE_IDS.forEach(function (pid) {
      const pane = state.panes[pid];
      if (!pane) return;
      pane.pendingMode = "select";
      const chart = api(pane);
      if (chart) {
        chart.setInteractionMode("select");
        if (chart.clearPreview) chart.clearPreview();
      }
    });
    refreshStatusLine();
  }

  function syncOverlays(pane, payloads) {
    const chart = api(pane);
    const list = payloads || [];
    if (!chart) {
      pane.pendingOverlays = list;
      return;
    }
    const wanted = {};
    list.forEach(function (p) {
      if (p && p.id) wanted[p.id] = p;
    });
    const prev = pane.overlayPayloads || {};
    Object.keys(prev).forEach(function (id) {
      if (!wanted[id]) chart.removeOverlay(id);
    });
    function overlayFp(p) {
      // Avoid JSON.stringify of multi-MB EZM metadata on every sync.
      return [
        p.type || "",
        p.shape || "",
        p.timestamp || "",
        p.price || "",
        p.text || "",
        (p.style && p.style.color) || "",
        p.visible === false ? "0" : "1",
      ].join("|");
    }
    Object.keys(wanted).forEach(function (id) {
      if (!prev[id]) chart.addOverlay(wanted[id]);
      else if (overlayFp(prev[id]) !== overlayFp(wanted[id])) chart.updateOverlay(wanted[id]);
    });
    pane.overlayPayloads = wanted;
    pane.pendingOverlays = list;
  }

  function buildTools() {
    const host = $("trpTools");
    if (!host) return;
    host.innerHTML = "";
    TOOLS.forEach(function (pair) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "trp-tool-btn" + (pair[0] === "select" ? " active" : "");
      btn.dataset.tool = pair[0];
      btn.title = pair[1];
      btn.setAttribute("aria-label", pair[1]);
      btn.textContent = {
        select: "↖",
        trend: "/",
        hline: "—",
        vline: "|",
        rectangle: "▭",
        circle: "○",
        arrow: "→",
        measure: "⤢",
        long_position: "L",
        short_position: "S",
      }[pair[0]] || pair[0];
      btn.addEventListener("click", function () { setTool(pair[0]); });
      host.appendChild(btn);
    });
  }

  function buildPanes() {
    const root = $("researchWorkspace");
    root.innerHTML = "";
    PANE_IDS.forEach(function (pid) {
      const wrap = document.createElement("div");
      wrap.className = "trp-pane";
      wrap.dataset.paneId = pid;
      wrap.innerHTML =
        '<div class="trp-pane-header">' +
        '<span class="trp-pane-symbol"></span><span class="trp-pane-sep">|</span>' +
        '<select class="trp-pane-tf"></select>' +
        '<button type="button" class="trp-reset" title="Chartansicht zurücksetzen" aria-label="Chartansicht zurücksetzen">⟲</button>' +
        '<button type="button" class="trp-fs" title="Chart Vollbild" aria-label="Chart Vollbild">⛶</button>' +
        '<span class="trp-pane-status"></span></div>' +
        '<iframe class="trp-pane-frame" title="' + pid + '"></iframe>';
      const tfSel = wrap.querySelector(".trp-pane-tf");
      TIMEFRAMES.forEach(function (tf) {
        const opt = document.createElement("option");
        opt.value = tf;
        opt.textContent = tf;
        tfSel.appendChild(opt);
      });
      tfSel.value = DEFAULT_TFS[pid];
      const iframe = wrap.querySelector("iframe");
      const gate = makeReadyGate();
      const pane = {
        id: pid,
        el: wrap,
        iframe: iframe,
        tf: DEFAULT_TFS[pid],
        ready: false,
        phase: "IFRAME_LOADING",
        readyPromise: gate.promise,
        _resolveReady: gate.resolve,
        overlayPayloads: {},
        lastTimes: new Set(),
        paneGen: 0,
        vpGen: 0,
        vpTimer: null,
        vpAbort: null,
        obpGen: 0,
        obpTimer: null,
        obpAbort: null,
      };
      state.panes[pid] = pane;
      iframe.addEventListener("load", function () {
        attachBridge(pane);
      });
      iframe.src = "/static/research_trp/pane.html?v=" + ASSET_V;
      tfSel.addEventListener("change", function () {
        pane.tf = tfSel.value;
        loadPane(pid, { force: true, sourceAction: "tf-change" });
      });
      wrap.querySelector(".trp-reset").addEventListener("click", function () {
        const chart = api(pane);
        if (chart && chart.resetView) chart.resetView();
      });
      wrap.querySelector(".trp-fs").addEventListener("click", function () {
        togglePaneFullscreen(pid);
      });
      let resizeTimer = null;
      let lastBox = "0x0";
      new ResizeObserver(function () {
        if (wrap.classList.contains("pooled-hidden")) return;
        const box = wrap.clientWidth + "x" + wrap.clientHeight;
        if (box === lastBox) return;
        lastBox = box;
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(function () {
          const chart = api(pane);
          if (chart && chart.resize) chart.resize();
        }, 80);
      }).observe(wrap);
      root.appendChild(wrap);
    });
    applyLayout(state.layout, true);
  }

  function applyLayout(layout, force) {
    if (!PANE_COUNT[layout]) layout = "1";
    if (!force && layout === state.layout && !state.paneFs) return;
    state.layout = layout;
    try { localStorage.setItem(LAYOUT_KEY, layout); } catch (e) {}
    const root = $("researchWorkspace");
    root.className = "trp-workspace layout-" + layout + (state.paneFs ? " pane-fs" : "");
    const vis = new Set(visibleIds());
    PANE_IDS.forEach(function (pid) {
      const pane = state.panes[pid];
      if (!pane) return;
      pane.el.classList.toggle("pooled-hidden", !vis.has(pid));
      pane.el.classList.toggle("pane-fs-active", state.paneFs === pid);
      const fsBtn = pane.el.querySelector(".trp-fs");
      if (fsBtn) fsBtn.classList.toggle("active", state.paneFs === pid);
    });
    document.querySelectorAll(".trp-layout-btn").forEach(function (btn) {
      btn.classList.toggle("active", btn.dataset.layout === layout);
    });
    requestAnimationFrame(function () {
      visibleIds().forEach(function (pid) {
        if (state.paneFs && pid !== state.paneFs) return;
        const chart = api(state.panes[pid]);
        if (chart) chart.resize();
      });
    });
  }

  function waitPaneSize(pane, timeoutMs) {
    return new Promise(function (resolve) {
      const t0 = Date.now();
      function tick() {
        const frame = pane && pane.iframe;
        if (frame && frame.clientWidth >= 16 && frame.clientHeight >= 16) {
          resolve(true);
          return;
        }
        if (Date.now() - t0 > (timeoutMs || 900)) {
          resolve(false);
          return;
        }
        requestAnimationFrame(tick);
      }
      tick();
    });
  }

  async function loadNewlyVisiblePanes() {
    const ids = visibleIds().filter(function (pid) {
      const pane = state.panes[pid];
      return pane && !(pane.lastTimes && pane.lastTimes.size);
    });
    if (!ids.length) return;
    await Promise.all(ids.map(function (pid) { return waitPaneSize(state.panes[pid]); }));
    await mapLimit(ids, PANE_HTTP_LIMIT, function (pid) {
      return loadPane(pid, { sourceAction: "layout-change" });
    });
    requestAnimationFrame(function () {
      ids.forEach(function (pid) {
        const chart = api(state.panes[pid]);
        if (chart && chart.resize) chart.resize();
      });
    });
  }

  function togglePaneFullscreen(paneId) {
    const entering = state.paneFs !== paneId;
    state.paneFs = entering ? paneId : null;
    applyLayout(state.layout, true);
    setBrowserFs(entering);
    if (entering) loadNewlyVisiblePanes();
  }

  function isBrowserFs() {
    return document.body.classList.contains("research-browser-fs");
  }

  function setBrowserFs(on) {
    document.body.classList.toggle("research-browser-fs", !!on);
    const btn = $("researchFullscreenBtn");
    if (btn) btn.classList.toggle("active", !!on);
    requestAnimationFrame(function () {
      visibleIds().forEach(function (pid) {
        const chart = api(state.panes[pid]);
        if (chart && chart.resize) chart.resize();
      });
    });
  }

  function toggleWorkspaceFullscreen() {
    expandWorkspaceUp();
  }

  function resizeVisibleCharts() {
    requestAnimationFrame(function () {
      visibleIds().forEach(function (pid) {
        const chart = api(state.panes[pid]);
        if (chart && chart.resize) chart.resize();
      });
    });
  }

  function applyWorkspaceHeight(height, marginTop) {
    const dock = $("researchChartDock");
    const root = $("researchWorkspace");
    const handle = $("researchHeightHandle");
    const bar = $("researchDockBar");
    if (!dock || !root) return;
    const handleH = (bar && bar.offsetHeight) || (handle && handle.offsetHeight) || 36;
    const maxH = Math.max(240, window.innerHeight - handleH - 8);
    const h = Math.min(maxH, Math.max(240, Math.round(height)));
    let m = Math.round(marginTop);
    dock.style.marginTop = m + "px";
    const top = dock.getBoundingClientRect().top;
    if (top < 0) {
      m -= Math.round(top);
      dock.style.marginTop = m + "px";
    }
    root.style.height = h + "px";
    root.style.minHeight = h + "px";
    root.style.marginTop = "0px";
    const btn = $("researchFullscreenBtn");
    const maxed = dock.getBoundingClientRect().top <= 4 && h >= maxH - 8;
    dock.classList.toggle("is-max", maxed);
    if (btn) {
      btn.classList.toggle("active", maxed);
      btn.title = maxed ? "Zurück" : "Aufziehen";
    }
    try {
      localStorage.setItem("research.workspace_h", String(h));
      localStorage.setItem("research.workspace_mt", String(m));
    } catch (e) {}
    resizeVisibleCharts();
  }

  function resetWorkspaceHeight() {
    applyWorkspaceHeight(Math.min(640, Math.max(240, window.innerHeight - 280)), 0);
  }

  function expandWorkspaceUp() {
    const dock = $("researchChartDock");
    const handle = $("researchHeightHandle");
    if (!dock) return;
    if (dock.classList.contains("is-max")) {
      resetWorkspaceHeight();
      return;
    }
    const rect = dock.getBoundingClientRect();
    const m = parseFloat(dock.style.marginTop) || 0;
    const handleH = ($("researchDockBar") && $("researchDockBar").offsetHeight) || 36;
    applyWorkspaceHeight(window.innerHeight - handleH - 8, m - Math.max(0, rect.top));
  }

  function restoreWorkspaceHeight() {
    try {
      const h = parseInt(localStorage.getItem("research.workspace_h") || "", 10);
      const m = parseInt(localStorage.getItem("research.workspace_mt") || "", 10);
      if (!Number.isFinite(h) || h < 240 || h > window.innerHeight + 40) {
        resetWorkspaceHeight();
        return;
      }
      applyWorkspaceHeight(h, Number.isFinite(m) ? m : 0);
    } catch (e) {
      resetWorkspaceHeight();
    }
  }

  function bindHeightDrag() {
    const handle = $("researchHeightHandle");
    const dock = $("researchChartDock");
    const root = $("researchWorkspace");
    if (!handle || !dock || !root) return;
    handle.addEventListener("dblclick", function () { expandWorkspaceUp(); });
    handle.addEventListener("pointerdown", function (ev) {
      if (ev.button !== 0) return;
      ev.preventDefault();
      handle.classList.add("dragging");
      handle.setPointerCapture(ev.pointerId);
      const startY = ev.clientY;
      const startH = root.getBoundingClientRect().height;
      const startM = parseFloat(dock.style.marginTop) || 0;
      const startTop = dock.getBoundingClientRect().top;
      function onMove(e) {
        const maxUp = Math.max(0, startTop);
        const maxDown = startH - 240;
        let up = startY - e.clientY;
        up = Math.max(-maxDown, Math.min(maxUp, up));
        applyWorkspaceHeight(startH + up, startM - up);
      }
      function onUp() {
        handle.classList.remove("dragging");
        handle.removeEventListener("pointermove", onMove);
        handle.removeEventListener("pointerup", onUp);
        handle.removeEventListener("pointercancel", onUp);
      }
      handle.addEventListener("pointermove", onMove);
      handle.addEventListener("pointerup", onUp);
      handle.addEventListener("pointercancel", onUp);
    });
  }

  function refreshStatusLine() {
    const ws = state.workspace || {};
    const parts = [
      state.symbol || "—",
      "layout " + state.layout,
      "hover " + fmtUtc(state.hoverUnix),
      "sel " + fmtUtc(state.selectedUnix),
    ];
    if (!state.sync) parts.push("sync off");
    if (ws.tool && ws.tool !== "select") parts.push("tool " + ws.tool);
    if (ws.selected_id) parts.push("draw " + ws.selected_id);
    const el = $("researchSelected");
    if (el) el.textContent = parts.join("  ·  ");
  }

  function defaultOrderbookProfile() {
    return { enabled: false, width: "normal", mode: "snapshot_at" };
  }

  function readStoredOrderbookProfile() {
    try {
      const raw = localStorage.getItem(OBP_KEY);
      if (!raw) return defaultOrderbookProfile();
      const out = Object.assign(defaultOrderbookProfile(), JSON.parse(raw));
      // Always current snapshot in the UI (ignore legacy history mashup).
      out.mode = "snapshot_at";
      return out;
    } catch (e) {
      return defaultOrderbookProfile();
    }
  }

  function persistOrderbookProfile() {
    try { localStorage.setItem(OBP_KEY, JSON.stringify(state.obp)); } catch (e) {}
  }

  function fillOrderbookProfileControls() {
    const obp = state.obp || defaultOrderbookProfile();
    const en = $("researchObpEnabled");
    if (en) en.checked = !!obp.enabled;
    const legend = $("researchObpLegend");
    if (legend) legend.hidden = !obp.enabled;
  }

  function applyOrderbookProfileSettings(raw, skipPersist) {
    state.obp = Object.assign(defaultOrderbookProfile(), raw || {});
    fillOrderbookProfileControls();
    if (!skipPersist) persistOrderbookProfile();
    if (!state.obp.enabled) {
      stopOrderbookProfileRefresh();
      visibleIds().forEach(function (pid) { clearPaneOrderbookProfile(state.panes[pid]); });
      return;
    }
    startOrderbookProfileRefresh();
    visibleIds().forEach(function (pid) { scheduleOrderbookProfile(state.panes[pid]); });
  }

  function obpSettingsPayload() {
    const obp = state.obp || defaultOrderbookProfile();
    return { enabled: !!obp.enabled, width: obp.width || "normal" };
  }

  function clearPaneOrderbookProfile(pane) {
    if (!pane) return;
    pane.obpGen += 1;
    if (pane.obpTimer) {
      clearTimeout(pane.obpTimer);
      pane.obpTimer = null;
    }
    if (pane.obpAbort) {
      try { pane.obpAbort.abort(); } catch (e) {}
      pane.obpAbort = null;
    }
    const chart = api(pane);
    if (chart && chart.clearOrderbookProfile) chart.clearOrderbookProfile();
  }

  function scheduleOrderbookProfile(pane) {
    if (!pane) return;
    if (pane.obpTimer) clearTimeout(pane.obpTimer);
    pane.obpTimer = setTimeout(function () {
      pane.obpTimer = null;
      refreshPaneOrderbookProfile(pane);
    }, VP_DEBOUNCE_MS);
  }

  function refreshOrderbookProfileVisible() {
    if (!state.obp || !state.obp.enabled) return;
    visibleIds().forEach(function (pid) {
      scheduleOrderbookProfile(state.panes[pid]);
    });
  }

  function stopOrderbookProfileRefresh() {
    if (state.obpRefreshTimer) {
      clearInterval(state.obpRefreshTimer);
      state.obpRefreshTimer = null;
    }
  }

  function startOrderbookProfileRefresh() {
    stopOrderbookProfileRefresh();
    if (!state.obp || !state.obp.enabled) return;
    state.obpRefreshTimer = setInterval(function () {
      if (!state.obp || !state.obp.enabled || !state.initialLoadDone) return;
      refreshOrderbookProfileVisible();
    }, OBP_REFRESH_MS);
  }

  async function refreshPaneOrderbookProfile(pane) {
    if (!pane || !state.obp || !state.obp.enabled || !state.symbol) {
      clearPaneOrderbookProfile(pane);
      return;
    }
    const chart = api(pane);
    if (!chart || !chart.getVisibleTimeRange) return;
    const range = chart.getVisibleTimeRange();
    if (!range || range.firstCandle == null || range.lastCandle == null) return;
    const step = TF_SEC[pane.tf] || 60;
    const start = Number(range.firstCandle);
    const end = Number(range.lastCandle) + step;
    if (!(end > start)) return;
    const gen = ++pane.obpGen;
    if (pane.obpAbort) {
      try { pane.obpAbort.abort(); } catch (e) {}
    }
    pane.obpAbort = (typeof AbortController !== "undefined") ? new AbortController() : null;
    const tip = Number(range.lastCandle) + step;
    const nowSec = Math.floor(Date.now() / 1000);
    // Live tip → use wall-clock now; scrolled history → as-of right edge.
    const atSec = (nowSec - tip <= 180) ? nowSec : Math.floor(tip);
    let url = "/api/research/orderbook-profile?symbol=" + encodeURIComponent(state.symbol) +
      "&start=" + encodeURIComponent(String(start)) +
      "&end=" + encodeURIComponent(String(Math.max(end, atSec + 1))) +
      "&at=" + encodeURIComponent(String(atSec)) +
      "&mode=snapshot_at";
    try {
      const body = await getJson(url, {
        sourceAction: "orderbook-profile",
        pane: pane.id,
        timeframe: pane.tf,
        signal: pane.obpAbort && pane.obpAbort.signal,
      });
      if (gen !== pane.obpGen) return;
      if (!state.obp.enabled) return;
      const live = api(pane);
      if (live && live.setOrderbookProfile) {
        live.setOrderbookProfile(body, obpSettingsPayload());
      } else {
        setStatus("Orderbook Profile: Chart-API fehlt (iframe neu laden)", "error");
      }
      if (body && body.warning === "no_wall_data") {
        setStatus("Orderbook Walls: keine aktuellen Daten", "empty");
      } else if (body && body.bar_count > 0) {
        const src = body.profile_kind === "ob200_multi_walls" ? "OB200" : "Features";
        const ob = body.ob200 || {};
        const live = ob.live_open ? " · live" : "";
        const lag = (ob.live_open && ob.lag_seconds != null) ? (" · lag " + Math.round(ob.lag_seconds) + "s") : "";
        const clamp = body.warning === "ob200_clamped_to_coverage_end" ? " · clamped" : "";
        setStatus(
          "Orderbook Walls · " + src + live + " · " + body.bar_count +
            " (B" + (body.bid_count || 0) + "/A" + (body.ask_count || 0) +
            (body.as_of ? " @" + fmtUtc(body.as_of) : "") + lag + clamp + ")",
          body.warning ? "empty" : ""
        );
      }
    } catch (err) {
      if (err && err.name === "AbortError") return;
      if (gen !== pane.obpGen) return;
      setStatus("Orderbook Profile fehlgeschlagen: " + String((err && err.message) || err), "error");
    }
  }

  function defaultVolumeProfile() {
    return {
      enabled: false,
      rows: "auto",
      display: "buy_sell",
      poc: true,
      value_area: true,
      width: "normal",
      volume_mode: "base",
    };
  }

  function readStoredVolumeProfile() {
    try {
      const raw = localStorage.getItem(VP_KEY);
      if (!raw) return defaultVolumeProfile();
      return Object.assign(defaultVolumeProfile(), JSON.parse(raw));
    } catch (e) {
      return defaultVolumeProfile();
    }
  }

  function persistVolumeProfile() {
    try { localStorage.setItem(VP_KEY, JSON.stringify(state.vp)); } catch (e) {}
  }

  function fillVolumeProfileControls() {
    const vp = state.vp || defaultVolumeProfile();
    const en = $("researchVpEnabled");
    if (en) en.checked = !!vp.enabled;
    const rows = $("researchVpRows");
    if (rows) rows.value = String(vp.rows || "auto");
    const display = $("researchVpDisplay");
    if (display) display.value = vp.display || "buy_sell";
    const poc = $("researchVpPoc");
    if (poc) poc.checked = vp.poc !== false;
    const va = $("researchVpVa");
    if (va) va.checked = vp.value_area !== false;
    const width = $("researchVpWidth");
    if (width) width.value = vp.width || "normal";
    const mode = $("researchVpMode");
    if (mode) mode.value = vp.volume_mode || "base";
  }

  function applyVolumeProfileSettings(raw, skipPersist) {
    state.vp = Object.assign(defaultVolumeProfile(), raw || {});
    fillVolumeProfileControls();
    if (!skipPersist) persistVolumeProfile();
    if (!state.vp.enabled) {
      visibleIds().forEach(function (pid) { clearPaneVolumeProfile(state.panes[pid]); });
      return;
    }
    visibleIds().forEach(function (pid) { scheduleVolumeProfile(state.panes[pid]); });
  }

  function vpSettingsPayload() {
    const vp = state.vp || defaultVolumeProfile();
    return {
      enabled: !!vp.enabled,
      display: vp.display || "buy_sell",
      poc: vp.poc !== false,
      value_area: vp.value_area !== false,
      width: vp.width || "normal",
    };
  }

  function clearPaneVolumeProfile(pane) {
    if (!pane) return;
    pane.vpGen += 1;
    if (pane.vpTimer) {
      clearTimeout(pane.vpTimer);
      pane.vpTimer = null;
    }
    if (pane.vpAbort) {
      try { pane.vpAbort.abort(); } catch (e) {}
      pane.vpAbort = null;
    }
    const chart = api(pane);
    if (chart && chart.clearVolumeProfile) chart.clearVolumeProfile();
  }

  function scheduleVolumeProfile(pane) {
    if (!pane) return;
    if (pane.vpTimer) clearTimeout(pane.vpTimer);
    pane.vpTimer = setTimeout(function () {
      pane.vpTimer = null;
      fetchPaneVolumeProfile(pane);
    }, VP_DEBOUNCE_MS);
  }

  async function fetchPaneVolumeProfile(pane) {
    if (!pane || !state.symbol) return;
    if (!state.vp || !state.vp.enabled) {
      clearPaneVolumeProfile(pane);
      return;
    }
    const chart = api(pane);
    if (!chart || !chart.getVisibleTimeRange) return;
    const range = chart.getVisibleTimeRange();
    if (!range || range.firstCandle == null || range.lastCandle == null) return;
    const step = TF_SEC[pane.tf] || 60;
    const start = Number(range.firstCandle);
    const end = Number(range.lastCandle) + step;
    if (!(end > start)) return;
    const gen = ++pane.vpGen;
    if (pane.vpAbort) {
      try { pane.vpAbort.abort(); } catch (e) {}
    }
    pane.vpAbort = (typeof AbortController !== "undefined") ? new AbortController() : null;
    const rows = state.vp.rows || "auto";
    const url = "/api/research/volume-profile?symbol=" + encodeURIComponent(state.symbol) +
      "&start=" + encodeURIComponent(String(start)) +
      "&end=" + encodeURIComponent(String(end)) +
      "&rows=" + encodeURIComponent(String(rows)) +
      "&volume_mode=" + encodeURIComponent(state.vp.volume_mode || "base");
    try {
      const body = await getJson(url, {
        sourceAction: "volume-profile",
        pane: pane.id,
        timeframe: pane.tf,
        signal: pane.vpAbort && pane.vpAbort.signal,
      });
      if (gen !== pane.vpGen) return;
      if (!state.vp.enabled) return;
      const live = api(pane);
      if (live && live.setVolumeProfile) live.setVolumeProfile(body, vpSettingsPayload());
    } catch (err) {
      if (err && err.name === "AbortError") return;
      if (gen !== pane.vpGen) return;
    }
  }

  function btStrategy() {
    const el = $("researchBtStrategy");
    return el ? el.value : "stoch_fade";
  }

  function ezmLayerMode() {
    const el = $("researchEzmLayerMode");
    return el ? el.value : "both";
  }

  function ezmComputationMode() {
    const el = $("researchEzmComputationMode");
    return el ? el.value : "ema_plus_microstructure";
  }

  function ezmComputationModeLabel(mode) {
    if (mode === "ema_only") return "Nur EMA";
    return "EMA plus Orderbuch";
  }

  function ezmLayerModeLabel(mode) {
    if (mode === "ema_only") return "Nur EMA";
    if (mode === "micro_only") return "Nur Mikrostruktur";
    return "EMA und Mikrostruktur";
  }

  function syncEzmLayerUi(snap) {
    const isEzm = btStrategy() === "ema_zone_microstructure_confirmation_v1";
    const wrap = $("researchEzmLayerWrap");
    const compWrap = $("researchEzmComputationWrap");
    const legend = $("researchEzmLegend");
    const sel = $("researchEzmLayerMode");
    if (wrap) wrap.hidden = !isEzm;
    if (compWrap) compWrap.hidden = !isEzm;
    if (legend) legend.hidden = !isEzm;
    if (!isEzm) return;
    const ez = (snap && snap.ezm) || (state.workspace && state.workspace.ezm) || {};
    if (sel && ez.layer_mode) sel.value = ez.layer_mode;
  }

  function applyWorkspace(snap) {
    if (!snap || snap.success === false) return;
    state.workspace = snap;
    document.querySelectorAll(".trp-tool-btn[data-tool]").forEach(function (btn) {
      btn.classList.toggle("active", btn.dataset.tool === snap.tool);
    });
    if (snap.style) {
      if (snap.style.color && snap.style.color.startsWith("#")) $("trpDrawColor").value = snap.style.color;
      if (snap.style.width) $("trpDrawWidth").value = String(Math.round(snap.style.width));
    }
    $("trpPositionSettings").disabled = !snap.position_settings;
    $("researchIndStoch").checked = !!(snap.stochastic && snap.stochastic.enabled);
    $("researchIndLld").checked = !!(snap.liquidity && snap.liquidity.enabled);
    if (snap.volume_profile) applyVolumeProfileSettings(snap.volume_profile, true);
    if (snap.orderbook_profile) applyOrderbookProfileSettings(snap.orderbook_profile, true);
    const ema = snap.ema || { lines: [] };
    const enabled = (ema.lines || []).filter(function (l) { return l.enabled; }).map(function (l) { return "EMA" + l.period; });
    $("trpEmaSummary").textContent = enabled.length ? enabled.join(", ") : "off";
    syncEzmLayerUi(snap);
    const mode = toolMode();
    PANE_IDS.forEach(function (pid) {
      const pane = state.panes[pid];
      if (!pane) return;
      pane.pendingMode = mode;
      const chart = api(pane);
      if (chart) {
        chart.setInteractionMode(mode);
        if (snap.preview_anchor) chart.setPreviewAnchor(snap.preview_anchor);
        else chart.clearPreview();
      }
    });
    refreshStatusLine();
  }

  async function pushInteractionMode() {
    const mode = toolMode();
    await Promise.all(PANE_IDS.map(async function (pid) {
      const pane = state.panes[pid];
      if (!pane) return;
      pane.pendingMode = mode;
      const chart = api(pane) || await whenReady(pane, 4000);
      if (chart) chart.setInteractionMode(mode);
    }));
  }

  async function setTool(tool) {
    applyWorkspace(await sendJson("/api/research/drawings/tool", "POST", { tool: tool }, {
      sourceAction: "set-tool",
    }));
    await pushInteractionMode();
  }

  async function handleChartKey(key) {
    if (key === "escape") applyWorkspace(await sendJson("/api/research/drawings/cancel", "POST", {}));
    else if (key === "delete") {
      applyWorkspace(await sendJson("/api/research/drawings/delete", "POST", {}));
      await refreshOverlaysVisible();
    }
  }

  async function handleDrawingEvent(paneId, blob) {
    let data = {};
    try { data = typeof blob === "string" ? JSON.parse(blob) : blob || {}; } catch (e) { return; }
    const pane = state.panes[paneId];
    data.pane_id = paneId;
    data.timeframe = pane.tf;
    data.symbol = state.symbol;
    if (data.type === "edit") {
      applyWorkspace(await sendJson("/api/research/drawings/event", "POST", data));
      openPositionModal();
      return;
    }
    applyWorkspace(await sendJson("/api/research/drawings/event", "POST", data));
    if (data.type !== "point" || (state.workspace && !state.workspace.pending)) {
      deactivateToolsLocal();
      await pushInteractionMode();
      await refreshOverlaysVisible();
    } else {
      visibleIds().forEach(function (pid) {
        const chart = api(state.panes[pid]);
        if (chart && state.workspace.preview_anchor) chart.setPreviewAnchor(state.workspace.preview_anchor);
      });
    }
  }

  function isEchoHover(paneId, unix) {
    if (state.hoverUnix == null || !state.hoverPane || paneId === state.hoverPane) return false;
    const tf = state.panes[paneId].tf;
    return floorUtc(unix, tf) === floorUtc(state.hoverUnix, tf);
  }

  function handleHover(paneId, unix) {
    if (isEchoHover(paneId, unix)) return;
    state.hoverUnix = Number(unix);
    state.hoverPane = paneId;
    if (state.sync) {
      const gen = ++state.syncGeneration;
      visibleIds().forEach(function (pid) {
        if (pid === paneId) return;
        const chart = api(state.panes[pid]);
        if (chart) chart.setSyncedCrosshair(floorUtc(unix, state.panes[pid].tf), gen);
      });
    }
    refreshStatusLine();
  }

  function handleHoverLeft(paneId) {
    if (state.hoverPane !== paneId) return;
    state.hoverUnix = null;
    state.hoverPane = null;
    visibleIds().forEach(function (pid) {
      if (pid === paneId) return;
      const chart = api(state.panes[pid]);
      if (chart) chart.clearSyncedCrosshair();
    });
    refreshStatusLine();
  }

  function handleClick(paneId, unix) {
    state.selectedUnix = Number(unix);
    state.selectedPane = paneId;
    visibleIds().forEach(function (pid) {
      const chart = api(state.panes[pid]);
      if (chart) chart.setSelectedMarker(floorUtc(unix, state.panes[pid].tf));
    });
    refreshStatusLine();
  }

  function candleFingerprint(candles) {
    if (!candles || !candles.length) return "0";
    const first = candles[0];
    const last = candles[candles.length - 1];
    return candles.length + ":" + first.time + ":" + last.time + ":" + last.close;
  }

  function closedCandleFingerprint(candles, tf) {
    const step = TF_SEC[tf] || 60;
    const now = Math.floor(Date.now() / 1000);
    const closed = (candles || []).filter(function (c) {
      return Number(c.time) + step <= now;
    });
    return candleFingerprint(closed);
  }

  function lastClosedBarTime(pane) {
    const step = TF_SEC[pane.tf] || 60;
    const now = Math.floor(Date.now() / 1000);
    const candles = (pane.pendingData && pane.pendingData.candles) || [];
    for (let i = candles.length - 1; i >= 0; i--) {
      const t = Number(candles[i].time);
      if (t + step <= now) return t;
    }
    return null;
  }

  function applyPaneBundle(pane, packed, opts) {
    const payload = {
      symbol: packed.symbol,
      timeframe: packed.timeframe,
      is_demo: false,
      candles: packed.candles || [],
    };
    const nextFp = candleFingerprint(packed.candles || []);
    const skipCandles = !!(opts && opts.indicatorsOnly && pane.pendingData);
    const preserveView = !!(opts && (opts.preserveView || opts.indicatorsOnly));
    const skipDefaultView = !!(opts && opts.skipDefaultView);
    if (!skipCandles) {
      pane.lastTimes = new Set((packed.candles || []).map(function (c) { return c.time; }));
      pane.pendingData = payload;
      pane.candleFp = nextFp;
      pane.closedFp = closedCandleFingerprint(packed.candles || [], pane.tf);
    }
    pane.pendingEma = packed.ema || { series: [] };
    pane.pendingLower = packed.stochastic || { id: "stochastic", visible: false };
    pane.pendingLldEma = packed.lld_ema || (packed.liquidity && packed.liquidity.ema) || {
      fast: [], slow: [], fast_visible: false, slow_visible: false,
    };
    pane.pendingOverlays = packed.overlays || [];
    pane.el.querySelector(".trp-pane-symbol").textContent = packed.symbol || state.symbol;
    pane.el.querySelector(".trp-pane-status").textContent =
      (packed.candles || []).length + " · " + (packed.cache || "load");
    const chart = api(pane);
    if (chart) {
      if (!skipCandles) {
        chart.setData(payload, {
          preserveView: preserveView,
          skipDefaultView: skipDefaultView,
        });
        pane.phase = "DATA_READY";
      }
      if (!preserveView && chart.resize) chart.resize();
      chart.setEmaOverlays(pane.pendingEma, {
        skipRangeRestore: !!(opts && opts.skipEmaRangeRestore),
      });
      chart.setLowerPane(pane.pendingLower);
      chart.setLldEma(pane.pendingLldEma);
      chart.setInteractionMode(toolMode());
      pane.phase = "INTERACTION_READY";
    }
    syncOverlays(pane, pane.pendingOverlays);
    if (packed.clusters || (packed.liquidity && packed.liquidity.clusters)) {
      renderLegend((state.workspace || {}).liquidity, packed.clusters || packed.liquidity.clusters);
    }
  }

  async function loadPane(paneId, opts) {
    const pane = state.panes[paneId];
    if (!pane || !state.symbol) return;
    const force = opts && opts.force;
    const gen = (opts && opts.gen != null) ? opts.gen : state.loadGen;
    const paneGen = ++pane.paneGen;
    const ws = state.workspace || {};
    const range = resolvePaneLoadRange(opts || {});
    const reqBody = {
      symbol: state.symbol,
      timeframe: pane.tf,
      ema: ws.ema || { enabled: false },
      stochastic: ws.stochastic || { enabled: false },
      liquidity: ws.liquidity || { enabled: false },
      allow_stale: !!(opts && opts.allowStale),
    };
    if (range.from != null) reqBody.from = range.from;
    if (range.to != null) reqBody.to = range.to;
    pane.el.querySelector(".trp-pane-status").textContent = "loading…";
    let packed;
    try {
      packed = await sendJson("/api/research/pane", "POST", reqBody, {
      sourceAction: (opts && opts.sourceAction) || "pane-load",
      pane: paneId,
      timeframe: pane.tf,
      signal: (opts && opts.signal) || (state.loadAbort && state.loadAbort.signal),
    });
    } catch (err) {
      if (err && err.name === "AbortError") return;
      throw err;
    }
    if (gen !== state.loadGen || paneGen !== pane.paneGen) return;
    if (packed.timeframe && packed.timeframe !== pane.tf) return;
    await whenReady(pane, 8000);
    if (gen !== state.loadGen || paneGen !== pane.paneGen) return;
    applyPaneBundle(pane, packed, {
      indicatorsOnly: !!(opts && opts.indicatorsOnly),
      preserveView: !!(opts && opts.preserveView),
      skipDefaultView: !!(opts && opts.jumpToUnix != null),
      skipEmaRangeRestore: !!(opts && opts.jumpToUnix != null),
    });
    if (packed.from != null && packed.to != null) {
      state.history.loadedFrom = Number(packed.from);
      state.history.loadedTo = Number(packed.to);
      updateHistoryHint({ from: state.history.loadedFrom, to: state.history.loadedTo });
    } else if ((packed.candles || []).length) {
      const times = packed.candles.map(function (c) { return Number(c.time); }).filter(Number.isFinite);
      if (times.length) {
        state.history.loadedFrom = Math.min.apply(null, times);
        state.history.loadedTo = Math.max.apply(null, times);
        if (state.history.preset === "rolling") {
          updateHistoryHint({ from: state.history.loadedFrom, to: state.history.loadedTo });
        }
      }
    }
    if (force && !(opts && opts.jumpToUnix != null)) {
      const ready = api(pane);
      if (ready && ready.resetView) ready.resetView();
    }
    // Stamp live tip immediately so higher TFs don't sit on a frozen closed bar.
    if (!historyBlocksLive()) {
      if (packed.forming) {
        applyFormingToPane(pane, packed.forming);
      } else if (packed.live_tip && (packed.candles || []).length) {
        const tip = packed.candles[packed.candles.length - 1];
        applyFormingToPane(pane, {
          time: tip.time,
          open: tip.open,
          high: tip.high,
          low: tip.low,
          close: tip.close,
        });
      }
    }
    scheduleVolumeProfile(pane);
    scheduleOrderbookProfile(pane);
  }

  async function refreshIndicatorsVisible(sourceAction) {
    await mapLimit(visibleIds(), PANE_HTTP_LIMIT, function (pid) {
      return loadPane(pid, {
        allowStale: true,
        indicatorsOnly: true,
        sourceAction: sourceAction || "indicator-apply",
      });
    });
  }

  async function loadOverlays(paneId) {
    const pane = state.panes[paneId];
    const body = await getJson(
      "/api/research/overlays?symbol=" + encodeURIComponent(state.symbol) +
      "&timeframe=" + encodeURIComponent(pane.tf),
      { sourceAction: "overlays", pane: paneId, timeframe: pane.tf }
    );
    syncOverlays(pane, body.overlays || []);
    const chart = api(pane);
    if (chart && body.lld_ema) chart.setLldEma(body.lld_ema);
    if (body.clusters) renderLegend((state.workspace || {}).liquidity, body.clusters);
  }

  async function refreshOverlaysVisible() {
    await mapLimit(visibleIds(), PANE_HTTP_LIMIT, loadOverlays);
  }

  function renderLegend(cfg, clusters) {
    const box = $("trpLldLegend");
    if (!box) return;
    const on = !!(cfg && cfg.enabled);
    box.hidden = !on;
    if (!on) return;
    const sup = $("trpVolSupport");
    const res = $("trpVolResistance");
    sup.innerHTML = "";
    res.innerHTML = "";
    for (let i = 0; i <= 10; i++) {
      const t = i / 10;
      const s = document.createElement("span");
      const r = document.createElement("span");
      s.className = r.className = "trp-vol-cell";
      s.textContent = r.textContent = String(i);
      s.style.background = mix(cfg.support_color || "#228bab", t);
      r.style.background = mix(cfg.resistance_color || "#ec4079", t);
      sup.appendChild(s);
      res.appendChild(r);
    }
    const c = clusters || {};
    $("trpClusterCounts").textContent = cfg.clusters_enabled
      ? ("Cl 3P:" + (c["3"] || 0) + "  4-5P:" + (c["4-5"] || 0) + "  6+:" + (c["6+"] || 0))
      : "";
  }

  function mix(hex, t) {
    const n = hex.replace("#", "");
    const r = parseInt(n.slice(0, 2), 16);
    const g = parseInt(n.slice(2, 4), 16);
    const b = parseInt(n.slice(4, 6), 16);
    return "rgba(" + r + "," + g + "," + b + "," + (0.25 + 0.55 * t).toFixed(2) + ")";
  }

  function stopPoll() {
    state.pollGen += 1;
    if (state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
    if (state.formingTimer) {
      clearInterval(state.formingTimer);
      state.formingTimer = null;
    }
  }

  function startPoll() {
    if (!state.initialLoadDone) return;
    stopPoll();
    const gen = state.pollGen;
    state.pollTimer = setInterval(function () {
      if (gen !== state.pollGen || !state.initialLoadDone) return;
      pollIncremental(gen);
      refreshLiveBar(false);
    }, POLL_MS);
    state.formingTimer = setInterval(function () {
      if (gen !== state.pollGen || !state.initialLoadDone) return;
      pollForming(gen);
    }, FORMING_MS);
    // Immediate first tick so price moves without waiting for the interval.
    pollForming(gen);
    if (state.obp && state.obp.enabled) startOrderbookProfileRefresh();
  }

  function formingBarForTf(forming, tfSec, lastCandle) {
    const t1 = Number(forming && forming.time);
    const px = Number(forming && forming.close);
    if (!Number.isFinite(t1) || !Number.isFinite(px) || !tfSec) return null;
    const bucket = Math.floor(t1 / tfSec) * tfSec;
    const fHigh = Number(forming.high);
    const fLow = Number(forming.low);
    const hi = Number.isFinite(fHigh) ? fHigh : px;
    const lo = Number.isFinite(fLow) ? fLow : px;

    if (!lastCandle) {
      const o = Number(forming.open);
      return {
        time: bucket,
        open: Number.isFinite(o) ? o : px,
        high: Math.max(px, hi),
        low: Math.min(px, lo),
        close: px,
      };
    }

    const lastT = Number(lastCandle.time);
    if (!Number.isFinite(lastT)) return null;

    // Normal: extend / update current TF bucket.
    if (lastT === bucket) {
      return {
        time: bucket,
        open: Number(lastCandle.open),
        high: Math.max(Number(lastCandle.high), hi, px),
        low: Math.min(Number(lastCandle.low), lo, px),
        close: px,
      };
    }
    // Gap: CH lags behind forming → open a new tip bucket.
    if (lastT < bucket) {
      const o = Number(lastCandle.close);
      return {
        time: bucket,
        open: Number.isFinite(o) ? o : px,
        high: Math.max(o, hi, px),
        low: Math.min(o, lo, px),
        close: px,
      };
    }
    // Desync: tip candle is ahead of forming timestamp → still paint live price on tip.
    return {
      time: lastT,
      open: Number(lastCandle.open),
      high: Math.max(Number(lastCandle.high), hi, px),
      low: Math.min(Number(lastCandle.low), lo, px),
      close: px,
    };
  }

  function postLiveDiag(row) {
    if (!LIVE_DIAG) return;
    try {
      fetch("/api/research/live-diag", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.assign({ t: Date.now() }, row || {})),
      }).catch(function () {});
    } catch (e) {}
  }

  function applyFormingToPane(pane, forming) {
    const diag = {
      symbol: state.symbol,
      pane: pane && pane.id,
      tf: pane && pane.tf,
      forming_close: forming && forming.close,
      forming_time: forming && forming.time,
      poll_gen: state.pollGen,
      blocked_history: !!historyBlocksLive(),
    };
    function emit(reason, extra) {
      // Always log failures; throttle successful paints to keep the ring useful.
      if (reason === "painted" || reason === "skipped_unchanged") {
        const now = Date.now();
        const key = "_diagOkAt_" + reason;
        if (pane && now - (pane[key] || 0) < 1500 && reason === "skipped_unchanged") return;
        if (pane && reason === "painted") {
          const closeKey = String(diag.bar_close);
          if (pane._diagPaintClose === closeKey && now - (pane._diagOkAt_painted || 0) < 2000) return;
          pane._diagPaintClose = closeKey;
          pane._diagOkAt_painted = now;
        } else if (pane) {
          pane[key] = now;
        }
      }
      postLiveDiag(Object.assign({}, diag, extra || {}, { reason: reason }));
    }
    if (!pane || !pane.tf) {
      emit("no_pending", { detail: "no_pane" });
      return false;
    }
    if (!pane.pendingData || !(pane.pendingData.candles || []).length) {
      emit("no_pending");
      return false;
    }
    const candles = pane.pendingData.candles;
    const last = candles[candles.length - 1];
    diag.last_time = last && last.time;
    diag.last_close = last && last.close;
    const bar = formingBarForTf(forming, TF_SEC[pane.tf] || 60, last);
    if (!bar) {
      emit("no_pending", { detail: "formingBar_null" });
      return false;
    }
    diag.bar_time = bar.time;
    diag.bar_close = bar.close;
    const prev = candles[candles.length - 1];
    const unchanged = !!(
      prev &&
      Number(prev.time) === Number(bar.time) &&
      Number(prev.open) === Number(bar.open) &&
      Number(prev.high) === Number(bar.high) &&
      Number(prev.low) === Number(bar.low) &&
      Number(prev.close) === Number(bar.close)
    );
    const chart = api(pane);
    if (!chart) {
      emit("no_chart_api", { unchanged: unchanged });
      return false;
    }
    // Paint the series BEFORE mutating pendingData. pendingData === chart lastPayload
    // (same object); pre-mutating made updateFormingBar think OHLC was unchanged and
    // only move the live price line.
    let painted = false;
    let updateOk = null;
    if (chart.updateFormingBar) {
      updateOk = !!chart.updateFormingBar(bar);
      painted = updateOk;
      if (!updateOk) emit("update_false", { unchanged: unchanged });
    } else {
      emit("no_chart_api", { detail: "missing_updateFormingBar" });
    }
    if (!unchanged) {
      if (prev && Number(prev.time) === Number(bar.time)) {
        candles[candles.length - 1] = Object.assign({}, prev, bar);
      } else if (prev && Number(bar.time) > Number(prev.time)) {
        candles.push(bar);
      } else {
        candles[candles.length - 1] = Object.assign({}, prev, bar);
      }
      pane.pendingData.candles = candles;
    }
    // If incremental update is unavailable/rejected, push full tip via setData (throttled).
    if (!painted && chart.setData) {
      const now = Date.now();
      if (now - (pane._lastFormingSetDataAt || 0) >= 400) {
        pane._lastFormingSetDataAt = now;
        chart.setData(pane.pendingData, { preserveView: true, skipDefaultView: true });
        painted = true;
        emit("setdata_fallback", { unchanged: unchanged });
      }
    }
    if (painted) {
      emit("painted", { unchanged: unchanged, updateOk: updateOk });
    } else if (unchanged) {
      emit("skipped_unchanged");
    } else {
      emit("update_false", { detail: "unpainted_changed", unchanged: unchanged });
    }
    if (painted || !unchanged) {
      const st = pane.el && pane.el.querySelector(".trp-pane-status");
      if (st) st.textContent = "live " + Number(bar.close);
    }
    return painted || !unchanged;
  }

  let formingInflight = null;
  let formingWantRestart = false;
  async function pollForming(gen) {
    if (gen !== state.pollGen || !state.symbol || !state.initialLoadDone) return;
    if (historyBlocksLive()) {
      postLiveDiag({
        reason: "blocked_history",
        symbol: state.symbol,
        pinned: !!(state.history && state.history.pinned),
        preset: state.history && state.history.preset,
      });
      return;
    }
    if (formingInflight) {
      formingWantRestart = true;
      return;
    }
    formingInflight = true;
    try {
      do {
        formingWantRestart = false;
        let formingStatus = 0;
        const body = await fetch(
          "/api/research/forming-bar?symbol=" + encodeURIComponent(state.symbol),
          { credentials: "same-origin" }
        ).then(async function (res) {
          formingStatus = res.status;
          const j = await res.json().catch(function () { return null; });
          if (!res.ok) return null;
          return j;
        }).catch(function () { return null; });
        if (gen !== state.pollGen) return;
        if (formingStatus === 401) {
          const now = Date.now();
          if (now - (pollForming._lastAuthDiagAt || 0) >= 3000) {
            pollForming._lastAuthDiagAt = now;
            postLiveDiag({ reason: "auth_401", symbol: state.symbol, status: 401 });
          }
          continue;
        }
        const forming = body && body.forming;
        if (!forming) {
          postLiveDiag({ reason: "no_forming", symbol: state.symbol, status: formingStatus });
          continue;
        }
        visibleIds().forEach(function (pid) {
          applyFormingToPane(state.panes[pid], forming);
        });
      } while (formingWantRestart && gen === state.pollGen);
    } finally {
      formingInflight = null;
    }
  }

  async function pollIncremental(gen) {
    if (gen !== state.pollGen || !state.symbol || !state.initialLoadDone) return;
    try {
      await mapLimit(visibleIds(), PANE_HTTP_LIMIT, function (pid) {
        return pollPane(pid, gen);
      });
    } catch (err) {
      if (gen !== state.pollGen) return;
      setStatus(String(err.message || err), "error");
    }
  }

  async function pollPane(paneId, gen) {
    const pane = state.panes[paneId];
    if (!pane || !state.symbol) return;
    if (gen !== state.pollGen) return;
    if (historyBlocksLive()) return;
    const last = lastClosedBarTime(pane);
    if (last == null || !pane.pendingData) return;
    let url = "/api/research/candles?symbol=" + encodeURIComponent(state.symbol) +
      "&timeframe=" + encodeURIComponent(pane.tf) + "&from=" + last + "&limit=50";
    const packed = await getJson(url, {
      sourceAction: "poll",
      pane: paneId,
      timeframe: pane.tf,
      start: last,
    });
    if (gen !== state.pollGen || pane.el.querySelector(".trp-pane-symbol").textContent !== state.symbol) return;
    const incoming = packed.candles || [];
    if (!incoming.length) return;
    const step = TF_SEC[pane.tf] || 60;
    const nowSec = Math.floor(Date.now() / 1000);
    const oldTip = (pane.pendingData.candles || [])[(pane.pendingData.candles || []).length - 1] || null;
    const byTime = {};
    (pane.pendingData.candles || []).forEach(function (c) { byTime[c.time] = c; });
    incoming.forEach(function (c) { byTime[c.time] = c; });
    // Keep unfinished live tip if poll returned only lagged CH bars.
    if (oldTip && Number(oldTip.time) + step > nowSec) {
      const t = Number(oldTip.time);
      const cur = byTime[t];
      if (!cur) {
        byTime[t] = oldTip;
      } else {
        byTime[t] = Object.assign({}, cur, {
          high: Math.max(Number(cur.high), Number(oldTip.high)),
          low: Math.min(Number(cur.low), Number(oldTip.low)),
          close: Number(oldTip.close),
        });
      }
    }
    const merged = Object.keys(byTime).map(Number).sort(function (a, b) { return a - b; }).map(function (t) { return byTime[t]; });
    const nextClosed = closedCandleFingerprint(merged, pane.tf);
    if (nextClosed === pane.closedFp) {
      // Still refresh live tip from poll payload if server stamped forming.
      if (packed.live_tip && packed.forming) applyFormingToPane(pane, packed.forming);
      return;
    }
    pane.pendingData = Object.assign({}, pane.pendingData, { candles: merged });
    pane.lastTimes = new Set(merged.map(function (c) { return c.time; }));
    pane.candleFp = candleFingerprint(merged);
    pane.closedFp = nextClosed;
    const chart = api(pane);
    if (chart) chart.setData(pane.pendingData, { preserveView: true });
    if (packed.forming) {
      applyFormingToPane(pane, packed.forming);
    } else {
      try {
        const formingBody = await getJson(
          "/api/research/forming-bar?symbol=" + encodeURIComponent(state.symbol),
          { sourceAction: "forming-after-poll", silent: true }
        );
        if (gen === state.pollGen && formingBody && formingBody.forming) {
          applyFormingToPane(pane, formingBody.forming);
        }
      } catch (e) {}
    }
  }

  async function refreshLiveBar(ensure) {
    if (!state.symbol) return;
    const q = ensure ? "&ensure=true" : "&ensure=false";
    const body = await getJson(
      "/api/research/live-status?symbol=" + encodeURIComponent(state.symbol) + q
    );
    state.liveStatus = body;
    const ui = body.ui_status || body.status || "HISTORICAL";
    setChip("researchUiStatusChip",
      ui === "LIVE_NOT_AVAILABLE" ? "LIVE NOT AVAILABLE"
        : ui === "UNAVAILABLE" ? "COLLECTOR UNAVAILABLE"
        : ui
    );
    setChip("researchCollectorChip", body.collector_state || "–");
    setChip("researchSymbolStateChip", body.symbol_state || body.live_capability || "–");
    $("researchLiveDetail").textContent = body.detail || body.message || "–";
    if (ui === "UNAVAILABLE") setStatus("COLLECTOR UNAVAILABLE · History " + state.symbol, "empty");
    else if (ui === "LIVE_NOT_AVAILABLE") setStatus("LIVE NOT AVAILABLE · History " + state.symbol);
  }

  async function switchSymbol(symbol) {
    const next = String(symbol || "").trim().toUpperCase();
    if (!next) return;
    if (state.loadAbort) {
      try { state.loadAbort.abort(); } catch (e) {}
    }
    stopPoll();
    state.initialLoadDone = false;
    const gen = ++state.loadGen;
    state.loadAbort = (typeof AbortController !== "undefined") ? new AbortController() : null;
    state.symbol = next;
    try { localStorage.setItem(SYMBOL_KEY, next); } catch (e) {}
    const sel = $("researchSymbol");
    if (sel && sel.value !== next) sel.value = next;
    PANE_IDS.forEach(function (pid) {
      const pane = state.panes[pid];
      if (!pane) return;
      pane.el.querySelector(".trp-pane-symbol").textContent = next;
      pane.overlayPayloads = {};
      pane.pendingOverlays = [];
      pane.pendingData = null;
      pane.candleFp = null;
      pane.lastTimes = new Set();
      pane.paneGen += 1;
      const chart = api(pane);
      if (chart && chart.clearOverlays) chart.clearOverlays();
      clearPaneVolumeProfile(pane);
      clearPaneOrderbookProfile(pane);
    });
    if (state.overlayTest) {
      applyWorkspace(await sendJson("/api/research/overlay-test", "POST", { enabled: true, symbol: next }, {
        sourceAction: "overlay-test",
      }));
    }
    if (gen !== state.loadGen) return;
    await mapLimit(visibleIds(), PANE_HTTP_LIMIT, function (pid) {
      return loadPane(pid, { force: true, gen: gen, sourceAction: "symbol-switch" });
    });
    if (gen !== state.loadGen) return;
    await refreshLiveBar(true);
    if (gen !== state.loadGen) return;
    state.initialLoadDone = true;
    state.phase = "INTERACTION_READY";
    startPoll();
  }

  function fillSymbolSelect(rows) {
    const sel = $("researchSymbol");
    if (!sel) return [];
    const list = (rows || [])
      .map(function (row) { return row && row.symbol ? String(row.symbol).toUpperCase() : ""; })
      .filter(Boolean)
      .sort();
    const bySym = {};
    (rows || []).forEach(function (row) {
      if (row && row.symbol) bySym[String(row.symbol).toUpperCase()] = row;
    });
    sel.innerHTML = "";
    list.forEach(function (sym) {
      const opt = document.createElement("option");
      opt.value = sym;
      opt.textContent = bySym[sym] && bySym[sym].collector_configured ? sym + " · live" : sym;
      sel.appendChild(opt);
    });
    return list;
  }

  function lastStochStrategy() {
    try {
      return localStorage.getItem(STOCH_STRATEGY_KEY) || "wave_fade_no_be50_v1";
    } catch (e) {
      return "wave_fade_no_be50_v1";
    }
  }

  function lastStochResearchJob() {
    try {
      const source = localStorage.getItem(STOCH_SOURCE_KEY) || "";
      const jobId = localStorage.getItem(STOCH_JOB_KEY) || "";
      if (
        (source === "FROZEN_RESEARCH_JOB" || source === "FROZEN_RESEARCH_EVALUATION") &&
        /^[0-9a-f]{32}$/.test(jobId)
      ) {
        return jobId;
      }
    } catch (e) {}
    return "";
  }

  function lastStochEvaluation() {
    try {
      const evid = localStorage.getItem(STOCH_EVAL_KEY) || "";
      if (/^[0-9a-f]{32}$/.test(evid)) return evid;
    } catch (e) {}
    return "";
  }

  function applyResearchJobSourceNote() {
    const el = $("researchJobSourceNote");
    if (!el) return;
    const jobId = lastStochResearchJob();
    const evalId = lastStochEvaluation();
    if (!jobId) {
      el.hidden = true;
      el.textContent = "";
      return;
    }
    el.hidden = false;
    if (evalId) {
      el.textContent =
        "Frozen NO_BE50 Evaluation " + evalId +
        " · Job " + jobId +
        " · Signale wave_fade_frozen_f16ae32 · Exit NO_BE50 / SL_FIRST · WIN/LOSS/OPEN · PnL gross";
      return;
    }
    el.textContent =
      "Frozen Research Job " + jobId +
      " · wave_fade_frozen_f16ae32 · PLANNED_NO_OUTCOME · Job-Fenster aus Manifest · kein NO_BE50 · 4h nur visuelle Projektion";
  }

  function pickDefaultSymbol(names) {
    const list = (names || []).slice();
    let preferred = "";
    try {
      if (lastStochStrategy() === "POOL_ORDER_PLAN_V1") {
        preferred = localStorage.getItem(STOCH_SYMBOL_KEY) || "";
      }
      if (!preferred) preferred = localStorage.getItem(SYMBOL_KEY) || "";
    } catch (e) {}
    preferred = String(preferred).trim().toUpperCase();
    if (preferred && list.indexOf(preferred) >= 0) return preferred;
    return list[0] || "";
  }

  function openModal(id) { $(id).hidden = false; }
  function closeModal(id) { $(id).hidden = true; }

  function renderEmaRows(lines) {
    const host = $("emaRows");
    host.innerHTML = "";
    state.emaDraft = (lines || []).map(function (l) { return Object.assign({}, l); });
    state.emaDraft.forEach(function (line, idx) {
      const row = document.createElement("div");
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
          const f = inp.getAttribute("data-f");
          state.emaDraft[idx][f] = inp.type === "checkbox" ? inp.checked
            : (inp.type === "number" ? Number(inp.value) : inp.value);
        });
      });
      row.querySelector("[data-remove]").addEventListener("click", function () {
        state.emaDraft = state.emaDraft.filter(function (x) { return x.ema_id !== line.ema_id; });
        renderEmaRows(state.emaDraft);
      });
      host.appendChild(row);
    });
  }

  function fillStoch(cfg) {
    $("stochKLen").value = cfg.k_length;
    $("stochKSmooth").value = cfg.k_smoothing;
    $("stochDSmooth").value = cfg.d_smoothing;
    $("stochOb").value = cfg.overbought_level;
    $("stochOs").value = cfg.oversold_level;
    $("stochShowLevels").checked = !!cfg.show_levels;
    $("stochShowK").checked = !!cfg.show_k;
    $("stochShowD").checked = !!cfg.show_d;
    $("stochKColor").value = cfg.k_color;
    $("stochDColor").value = cfg.d_color;
  }

  function fillLld(cfg) {
    $("lldAmount").value = cfg.amount;
    $("lldHigh").value = cfg.highest_len;
    $("lldLow").value = cfg.lowest_len;
    $("lldBorders").checked = !!cfg.show_pool_borders;
    $("lldBorderW").value = cfg.pool_border_width;
    $("lldStrongW").value = cfg.strong_pool_border_width;
    $("lldBorderT").value = cfg.pool_border_transparency;
    $("lldClusters").checked = !!cfg.clusters_enabled;
    $("lldGap").value = cfg.cluster_gap_pct;
    $("lldMinPools").value = cfg.minimum_cluster_pools;
    $("lldShowSingle").checked = !!cfg.show_single_pools;
    $("lldClusterLabels").checked = !!cfg.show_cluster_labels;
    $("lldClusterFill").value = cfg.cluster_fill_transparency;
    $("lldClusterBorder").value = cfg.cluster_border_width;
    $("lldSupport").value = cfg.support_color;
    $("lldResist").value = cfg.resistance_color;
    $("lldFillT").value = cfg.fill_transparency;
    $("lldSupBorder").value = cfg.support_border_color;
    $("lldResBorder").value = cfg.resistance_border_color;
    $("lldEmaFast").checked = !!cfg.ema_fast_enabled;
    $("lldEmaFastLen").value = cfg.ema_fast_length;
    $("lldEmaFastColor").value = cfg.ema_fast_color;
    $("lldEmaSlow").checked = !!cfg.ema_slow_enabled;
    $("lldEmaSlowLen").value = cfg.ema_slow_length;
    $("lldEmaSlowColor").value = cfg.ema_slow_color;
  }

  function readLld() {
    return {
      enabled: $("researchIndLld").checked,
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
      ema_slow_color: $("lldEmaSlowColor").value,
    };
  }

  function readStoch() {
    return Object.assign({}, (state.workspace || {}).stochastic || {}, {
      enabled: $("researchIndStoch").checked,
      k_length: Number($("stochKLen").value),
      k_smoothing: Number($("stochKSmooth").value),
      d_smoothing: Number($("stochDSmooth").value),
      overbought_level: Number($("stochOb").value),
      oversold_level: Number($("stochOs").value),
      show_levels: $("stochShowLevels").checked,
      show_k: $("stochShowK").checked,
      show_d: $("stochShowD").checked,
      k_color: $("stochKColor").value,
      d_color: $("stochDColor").value,
    });
  }

  async function reloadVisible() {
    await mapLimit(visibleIds(), PANE_HTTP_LIMIT, function (pid) {
      return loadPane(pid, { sourceAction: "reload-visible" });
    });
  }

  async function openPositionModal() {
    const body = await getJson("/api/research/position").catch(function () { return null; });
    if (!body || !body.position) return;
    const p = body.position;
    $("posTitle").textContent = p.drawing_type === "short_position" ? "Short-Position" : "Long-Position";
    $("posEntry").value = p.entry_price;
    $("posStop").value = p.stop_price;
    $("posTarget").value = p.target_price;
    $("posNotional").value = p.position_notional;
    $("posQty").value = (p.position_notional || 0) / Math.max(p.entry_price || 1, 1e-9);
    $("posRr").value = p.default_risk_reward || 2;
    const st = p.style || {};
    $("posProfit").value = st.profit_color || "#3dcc91";
    $("posLoss").value = st.loss_color || "#f0616d";
    $("posEntryColor").value = (st.color && st.color.startsWith("#")) ? st.color : "#5b8def";
    $("posWidth").value = st.width || 2;
    $("posFill").value = Math.round((st.fill_opacity || 0.18) * 100);
    $("posPreview").textContent = "";
    openModal("modalPos");
  }

  function bindUi() {
    bindHistoryUi();
    restoreHistoryPrefs();
    document.querySelectorAll(".trp-layout-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        state.paneFs = null;
        applyLayout(btn.dataset.layout);
        loadNewlyVisiblePanes();
      });
    });
    $("researchSymbol").addEventListener("change", function () { switchSymbol($("researchSymbol").value); });
    const vpEn = $("researchVpEnabled");
    if (vpEn) {
      vpEn.addEventListener("change", function () {
        state.vp.enabled = vpEn.checked;
        persistVolumeProfile();
        sendJson("/api/research/settings", "PUT", { volume_profile: state.vp }, { sourceAction: "vp-settings" }).catch(function () {});
        if (!state.vp.enabled) {
          visibleIds().forEach(function (pid) { clearPaneVolumeProfile(state.panes[pid]); });
        } else {
          visibleIds().forEach(function (pid) { scheduleVolumeProfile(state.panes[pid]); });
        }
      });
    }
    const obpEn = $("researchObpEnabled");
    if (obpEn) {
      obpEn.addEventListener("change", function () {
        state.obp.enabled = obpEn.checked;
        persistOrderbookProfile();
        fillOrderbookProfileControls();
        sendJson("/api/research/settings", "PUT", { orderbook_profile: state.obp }, { sourceAction: "obp-settings" }).catch(function () {});
        if (!state.obp.enabled) {
          stopOrderbookProfileRefresh();
          visibleIds().forEach(function (pid) { clearPaneOrderbookProfile(state.panes[pid]); });
        } else {
          startOrderbookProfileRefresh();
          visibleIds().forEach(function (pid) { scheduleOrderbookProfile(state.panes[pid]); });
        }
      });
    }
    ["researchVpRows", "researchVpDisplay", "researchVpWidth", "researchVpMode"].forEach(function (id) {
      const el = $(id);
      if (!el) return;
      el.addEventListener("change", function () {
        if (id === "researchVpRows") state.vp.rows = el.value;
        if (id === "researchVpDisplay") state.vp.display = el.value;
        if (id === "researchVpWidth") state.vp.width = el.value;
        if (id === "researchVpMode") state.vp.volume_mode = el.value;
        persistVolumeProfile();
        sendJson("/api/research/settings", "PUT", { volume_profile: state.vp }, { sourceAction: "vp-settings" }).catch(function () {});
        visibleIds().forEach(function (pid) { scheduleVolumeProfile(state.panes[pid]); });
      });
    });
    ["researchVpPoc", "researchVpVa"].forEach(function (id) {
      const el = $(id);
      if (!el) return;
      el.addEventListener("change", function () {
        if (id === "researchVpPoc") state.vp.poc = el.checked;
        if (id === "researchVpVa") state.vp.value_area = el.checked;
        persistVolumeProfile();
        sendJson("/api/research/settings", "PUT", { volume_profile: state.vp }, { sourceAction: "vp-settings" }).catch(function () {});
        visibleIds().forEach(function (pid) {
          const pane = state.panes[pid];
          const chart = api(pane);
          if (chart && chart.setVolumeProfile && pane) {
            scheduleVolumeProfile(pane);
          }
        });
      });
    });
    $("researchCrosshairSync").addEventListener("change", function () {
      state.sync = $("researchCrosshairSync").checked;
      if (!state.sync) handleHoverLeft(state.hoverPane);
      refreshStatusLine();
    });
    $("researchOverlayTest").addEventListener("change", async function () {
      state.overlayTest = $("researchOverlayTest").checked;
      applyWorkspace(await sendJson("/api/research/overlay-test", "POST", {
        enabled: state.overlayTest, symbol: state.symbol,
      }));
      await refreshOverlaysVisible();
    });
    const fsBtn = $("researchFullscreenBtn");
    if (fsBtn) {
      fsBtn.addEventListener("click", function () { expandWorkspaceUp(); });
    }
    bindHeightDrag();

    async function applyEzmLayerMode(mode) {
      if (!state.symbol) return;
      const snap = await sendJson("/api/research/backtester/load", "POST", {
        strategy_id: "ema_zone_microstructure_confirmation_v1",
        symbol: state.symbol,
        ezm_layer_mode: mode,
        layer_only: true,
      }, { sourceAction: "ezm-layer" });
      applyWorkspace(snap);
      syncEzmLayerUi(snap);
      await refreshOverlaysVisible();
      const ez = snap.ezm || {};
      setStatus(
        "EZM " + ezmLayerModeLabel(mode) + " · "
          + (ez.n_setup_markers || 0) + " Setup · "
          + (ez.n_micro_markers || 0) + " Micro · keine Trades/PnL"
      );
    }

    function syncBtStrategyUi() {
      const sid = btStrategy();
      const isCsw = sid === "cluster_sweep_ema_9_20_59";
      const isEdc = sid === "ema_dual_cross_multisource_v1";
      const isEzm = sid === "ema_zone_microstructure_confirmation_v1";
      if ($("researchBtRunBtn")) {
        $("researchBtRunBtn").hidden = !(isCsw || isEdc || isEzm);
        if (isEzm) $("researchBtRunBtn").title = "EZM Candidate Discovery starten";
        else if (isEdc) $("researchBtRunBtn").title = "EMA Dual Cross Backtest starten";
        else $("researchBtRunBtn").title = "Cluster-Sweep Backtest starten";
      }
      if ($("researchCswSettingsBtn")) $("researchCswSettingsBtn").hidden = !isCsw;
      if ($("researchCswNav")) $("researchCswNav").hidden = !isCsw;
      if ($("researchEdcSettingsBtn")) $("researchEdcSettingsBtn").hidden = !isEdc;
      if ($("researchEdcNav")) $("researchEdcNav").hidden = !isEdc;
      syncEzmLayerUi(state.workspace);
      applyResearchJobSourceNote();
    }

    function fromLocalInputValue(v) {
      // Treat datetime-local as UTC for research (labeled UTC in modal)
      if (!v) return null;
      return String(v) + ":00Z";
    }

    function ensureEdcDefaults() {
      const end = new Date();
      const start = new Date(end.getTime() - 30 * 24 * 3600 * 1000);
      if ($("edcStart") && !$("edcStart").value) $("edcStart").value = toLocalInputValue(start);
      if ($("edcEnd") && !$("edcEnd").value) $("edcEnd").value = toLocalInputValue(end);
      const hint = $("edcRangeHint");
      if (hint && $("edcStart") && $("edcEnd") && $("edcStart").value && $("edcEnd").value) {
        hint.textContent = "Zeitraum UTC: " + $("edcStart").value.replace("T", " ")
          + " → " + $("edcEnd").value.replace("T", " ") + " · nur aktuelles Symbol";
      }
    }

    function updateEdcNav(snap) {
      const ed = (snap && snap.ema_dual_cross) || (state.workspace && state.workspace.ema_dual_cross) || {};
      const n = ed.n_candidates || 0;
      const idx = (ed.candidate_index || 0) + (n ? 1 : 0);
      if ($("researchEdcIndex")) $("researchEdcIndex").textContent = (n ? idx : 0) + "/" + n;
      const panel = $("edcCandidatePanel");
      const detail = $("edcCandidateDetail");
      if (panel && detail && ed.candidate) {
        panel.hidden = false;
        const c = ed.candidate;
        detail.textContent = JSON.stringify({
          setup: {
            candidate_id: c.candidate_id,
            episode_id: c.episode_id,
            direction: c.direction,
            candidate_type: c.candidate_type,
            candidate_at: c.candidate_at,
            decision_at: c.decision_at,
            entry_at: c.entry_at,
            entry_price: c.entry_price,
            final_verdict: c.final_verdict,
            reason_codes: c.reason_codes,
            policy_version: c.policy_version,
          },
          ema: {
            before: c.ema_before,
            after: c.ema_after,
            metrics: c.ema_metrics,
          },
          evidence: {
            source_verdicts: c.source_verdicts,
            features: c.features,
          },
          coverage: c.coverage,
          outcomes_1h_4h: fmtCswOutcomes(c.outcomes_1h_4h),
        }, null, 2);
      }
    }

    async function runEmaDualCrossBacktest() {
      if (!state.symbol) return;
      ensureEdcDefaults();
      const tf = (($("edcTf") || {}).value) || "15m";
      const body = {
        strategy_id: "ema_dual_cross_multisource_v1",
        symbol: state.symbol,
        timeframe: tf,
        start: fromLocalInputValue($("edcStart").value),
        end: fromLocalInputValue($("edcEnd").value),
        show_candidates: !!($("edcShowCand") && $("edcShowCand").checked),
        show_allow: !!($("edcShowAllow") && $("edcShowAllow").checked),
        show_block: !!($("edcShowBlock") && $("edcShowBlock").checked),
        show_inconclusive: !!($("edcShowInc") && $("edcShowInc").checked),
        show_rejected: !!($("edcShowRej") && $("edcShowRej").checked),
        enable_sync_cross: !!($("edcEnableSync") && $("edcEnableSync").checked),
        enable_compressed_rebound: !!($("edcEnableRebound") && $("edcEnableRebound").checked),
      };
      setStatus("EMA Dual Cross " + body.symbol + " " + body.timeframe + " …");
      try {
        const snap = await sendJson("/api/research/backtester/run", "POST", body, { sourceAction: "edc-run" });
        applyWorkspace(snap);
        updateEdcNav(snap);
        const meta = (snap.ema_dual_cross_result && snap.ema_dual_cross_result.meta) || {};
        const summary = (snap.ema_dual_cross_result && snap.ema_dual_cross_result.summary) || {};
        const nCand = snap.ema_dual_cross_result && snap.ema_dual_cross_result.n_candidates;
        setStatus(
          "EMA Dual Cross bereit · Kandidaten=" + (nCand != null ? nCand : (summary.n_candidates || 0))
            + " · ALLOW=" + (summary.n_allow || meta.n_allow || 0)
            + " BLOCK=" + (summary.n_block || meta.n_block || 0)
            + " INC=" + (summary.n_inconclusive || meta.n_inconclusive || 0)
            + " · Backtester klicken zum Einblenden"
        );
        const ed = snap.ema_dual_cross || {};
        const focus = ed.candidate && (ed.candidate.candidate_at || ed.candidate.decision_at);
        await syncChartAfterBacktest(body.start, body.end, focus);
      } catch (err) {
        setStatus("EMA Dual Cross fehlgeschlagen: " + (err.message || err), "error");
      }
    }

    function unixToIsoMinuteZ(unix) {
      const d = new Date(Math.floor(Number(unix)) * 1000);
      const p = (n) => String(n).padStart(2, "0");
      return d.getUTCFullYear() + "-" + p(d.getUTCMonth() + 1) + "-" + p(d.getUTCDate())
        + "T" + p(d.getUTCHours()) + ":" + p(d.getUTCMinutes()) + ":00Z";
    }

    function resolveEzmWindowIso() {
      readHistoryFromUi();
      let from = state.history.loadedFrom;
      let to = state.history.loadedTo;
      if (from == null || to == null) {
        const range = computeHistoryRangeUnix();
        from = range.from;
        to = range.to;
      }
      if (from == null || to == null || !(Number(to) > Number(from))) {
        return null;
      }
      // Floor to UTC minutes; end is exclusive signal window end.
      const startUnix = Math.floor(Number(from) / 60) * 60;
      let endUnix = Math.floor(Number(to) / 60) * 60;
      if (endUnix <= startUnix) endUnix = startUnix + 60;
      return {
        start: unixToIsoMinuteZ(startUnix),
        end: unixToIsoMinuteZ(endUnix),
        from: startUnix,
        to: endUnix,
      };
    }

    function ezmCoinFailureDetail(job) {
      const coin = (job.coins && job.coins[0]) || {};
      const st = String(coin.state || "");
      const msg = String(coin.message || coin.error_code || job.error_summary || job.message || st || "");
      if (st === "DATA_INCOMPLETE" && msg.indexOf("orderbook_ob200_v3_raw") >= 0
          && ezmComputationMode() === "ema_only") {
        return msg + " — Job lief vermutlich ohne computation_mode=ema_only (Dashboard neu starten, dann erneut starten)";
      }
      if (st === "DATA_INCOMPLETE" && msg.indexOf("orderbook_ob200_v3_raw") >= 0) {
        return msg + " — für Nur-EMA: Prüfung auf «Nur EMA» stellen (nicht nur Anzeige)";
      }
      return msg || st || String(job.state || "FAILED");
    }

    function formatEzmJobStatus(job) {
      const jobState = job.state || "?";
      const pct = job.progress_percent != null ? (job.progress_percent + "%") : "–";
      const sym = job.current_symbol || state.symbol || "–";
      const win = (job.signal_start && job.signal_end_exclusive)
        ? (job.signal_start + " → " + job.signal_end_exclusive)
        : "";
      const coin = (job.coins && job.coins[0]) || {};
      const cov = coin.state || coin.error_code || "";
      const fail = job.failed_coins != null ? (" · fail=" + job.failed_coins) : "";
      const msg = coin.message || job.message || job.error_summary || "";
      return "EZM " + jobState + " · " + pct + " · " + sym
        + (win ? (" · " + win) : "")
        + (cov ? (" · " + cov) : "")
        + fail
        + (msg ? (" — " + msg) : "");
    }

    async function pollEzmJob(jobId) {
      const url = "/api/research/ezm/status?job_id=" + encodeURIComponent(jobId);
      const res = await fetch(url, { credentials: "same-origin" });
      const body = await res.json().catch(function () { return {}; });
      if (!res.ok) throw httpError(res, body, url);
      return body;
    }

    async function runEzmCandidateDiscovery() {
      if (!state.symbol) return;
      if (state._ezmPollTimer) {
        clearTimeout(state._ezmPollTimer);
        state._ezmPollTimer = null;
      }
      const win = resolveEzmWindowIso();
      if (!win) {
        setStatus("EZM: History-Zeitraum laden (Preset/Custom anwenden)", "error");
        return;
      }
      const body = {
        symbol: state.symbol,
        start: win.start,
        end: win.end,
        strategy_id: "ema_zone_microstructure_confirmation_v1",
        computation_mode: ezmComputationMode(),
      };
      setStatus(
        "EZM Candidate Discovery startet · "
          + ezmComputationModeLabel(body.computation_mode) + " · "
          + body.symbol + " · " + body.start + " → " + body.end
      );
      try {
        const started = await sendJson("/api/research/ezm/run", "POST", body, { sourceAction: "ezm-run" });
        const jobId = started.job_id;
        if (!jobId) throw new Error(started.error || "job_id missing");
        state._ezmJobId = jobId;
        setStatus(formatEzmJobStatus(Object.assign({}, started, {
          signal_start: started.signal_start || body.start,
          signal_end_exclusive: started.signal_end_exclusive || body.end,
        })));

        const terminal = { COMPLETED: 1, COMPLETED_WITH_ERRORS: 1, FAILED: 1, CANCELLED: 1 };
        const pollOnce = async function () {
          const job = await pollEzmJob(jobId);
          setStatus(formatEzmJobStatus(job));
          const st = String(job.state || "");
          if (terminal[st]) {
            state._ezmPollTimer = null;
            const coin = (job.coins && job.coins[0]) || {};
            const coinState = String(coin.state || "");
            if (st === "FAILED" || st === "CANCELLED" || coinState === "DATA_INCOMPLETE" || coinState === "FAILED") {
              setStatus("EZM fehlgeschlagen: " + ezmCoinFailureDetail(job), "error");
              return;
            }
            const snap = await sendJson("/api/research/ezm/import", "POST", {
              job_id: jobId,
              symbol: state.symbol,
            }, { sourceAction: "ezm-import" });
            applyWorkspace(snap);
            const ez = snap.ezm_result || {};
            const ezmSnap = snap.ezm || {};
            const n = ez.n_markers != null ? ez.n_markers : (ezmSnap.n_markers_total || ezmSnap.n_markers || 0);
            setStatus(
              "EZM bereit · " + n + " Marker · "
                + (ezmSnap.n_ema_setup_events || 0) + " Setup · "
                + (ezmSnap.n_micro_events || 0) + " Micro · Job " + jobId
                + " · Backtester klicken"
                + (st === "COMPLETED_WITH_ERRORS" ? " · mit Fehlern" : "")
            );
            await syncChartAfterBacktest(body.start, body.end, null);
            return;
          }
          state._ezmPollTimer = setTimeout(function () {
            pollOnce().catch(function (err) {
              setStatus("EZM Statusfehler: " + (err.message || err), "error");
            });
          }, 3000);
        };
        await pollOnce();
      } catch (err) {
        setStatus("EZM Start fehlgeschlagen: " + (err.message || err), "error");
      }
    }

    async function runActiveBacktest() {
      if (btStrategy() === "ema_zone_microstructure_confirmation_v1") return runEzmCandidateDiscovery();
      if (btStrategy() === "ema_dual_cross_multisource_v1") return runEmaDualCrossBacktest();
      return runClusterSweepBacktest();
    }

    function ensureCswDefaults() {
      const end = new Date();
      const start = new Date(end.getTime() - 8 * 3600 * 1000);
      if ($("cswStart") && !$("cswStart").value) $("cswStart").value = toLocalInputValue(start);
      if ($("cswEnd") && !$("cswEnd").value) $("cswEnd").value = toLocalInputValue(end);
    }

    function fmtPct(v) {
      if (v == null || v === "") return "—";
      const n = Number(v);
      return Number.isFinite(n) ? n.toFixed(3) + "%" : String(v);
    }

    function fmtCswHorizonOutcomes(o, label) {
      if (!o) return null;
      const p = label;
      return {
        mfe_pct: o["mfe_" + p + "_pct"],
        mfe_at: o["mfe_" + p + "_at"],
        minutes_to_mfe: o["minutes_to_mfe_" + p],
        mae_pct: o["mae_" + p + "_pct"],
        mae_at: o["mae_" + p + "_at"],
        minutes_to_mae: o["minutes_to_mae_" + p],
        close_return_pct: o["close_return_" + p + "_pct"],
        first_extreme: o["first_extreme_" + p],
        coverage: o["coverage_" + p],
        first_hit: o["first_hit_" + p] || null,
      };
    }

    function fmtCswOutcomes(o) {
      if (!o) return null;
      return {
        entry_variant: o.entry_variant || "AGGRESSIVE",
        mfe_1h_pct: o.mfe_1h_pct,
        mae_1h_pct: o.mae_1h_pct,
        mfe_4h_pct: o.mfe_4h_pct,
        mae_4h_pct: o.mae_4h_pct,
        "1h": fmtCswHorizonOutcomes(o, "1h"),
        "4h": fmtCswHorizonOutcomes(o, "4h"),
        ema9_side_at_entry: o.ema9_side_at_entry,
        overlapping_outcome: o.overlapping_outcome,
        same_cluster_family: o.same_cluster_family,
        previous_entry_still_in_horizon: o.previous_entry_still_in_horizon,
      };
    }

    function updateCswNav(snap) {
      const cs = (snap && snap.cluster_sweep) || (state.workspace && state.workspace.cluster_sweep) || {};
      const n = cs.n_events || 0;
      const idx = (cs.event_index || 0) + (n ? 1 : 0);
      if ($("researchCswIndex")) $("researchCswIndex").textContent = (n ? idx : 0) + "/" + n;
      const badge = $("researchCswDebugBadge");
      if (badge) {
        const debug = !!(cs.meta && cs.meta.debug_low_pool_zones);
        badge.hidden = !debug;
      }
      const panel = $("cswEventPanel");
      const detail = $("cswEventDetail");
      if (panel && detail && cs.event) {
        panel.hidden = false;
        const e = cs.event;
        const oc = e.outcomes_1h_4h || null;
        detail.textContent = JSON.stringify({
          setup: {
            event_id: e.event_id,
            direction: e.direction,
            status: e.final_status,
            confirmation_type: e.confirmation_type,
            confirmation_at: e.confirmation_at,
            entry_at: e.entry_at,
            entry_price: e.entry_price,
            invalidated_at: e.invalidated_at,
          },
          cluster: {
            cluster_id: e.cluster_id,
            pool_count: e.cluster_pool_count,
            bounds: [e.cluster_low, e.cluster_high],
            strength_mean: e.cluster_strength_mean,
            created_at: e.cluster_created_at,
            as_of: e.cluster_as_of,
            prior_touch_count: e.prior_touch_count,
          },
          ema_audit: e.ema_audit,
          coverage: e.orderflow_coverage,
          outcomes_1h_4h: fmtCswOutcomes(oc),
          outcomes_summary_line: oc ? (
            "MFE 1h " + fmtPct(oc.mfe_1h_pct) + " · MAE 1h " + fmtPct(oc.mae_1h_pct)
            + " · MFE 4h " + fmtPct(oc.mfe_4h_pct) + " · MAE 4h " + fmtPct(oc.mae_4h_pct)
            + " · 1h " + (oc.first_extreme_1h || "—")
            + " · cov1h " + (oc.coverage_1h || "—")
            + (oc.overlapping_outcome ? " · OVERLAP" : "")
          ) : null,
          legacy_bar_outcomes: { mfe: e.mfe, mae: e.mae },
        }, null, 2);
      }
    }

    async function runClusterSweepBacktest() {
      if (!state.symbol) return;
      ensureCswDefaults();
      const debug = !!($("cswDebugLowPool") && $("cswDebugLowPool").checked);
      let minPools = Number(($("cswMinPools") || {}).value || 3);
      if (debug) minPools = Math.min(minPools, 1);
      const tf = (($("cswTf") || {}).value) || "5m";
      const body = {
        strategy_id: "cluster_sweep_ema_9_20_59",
        symbol: state.symbol,
        timeframe: tf,
        start: fromLocalInputValue($("cswStart").value),
        end: fromLocalInputValue($("cswEnd").value),
        minimum_cluster_pools: minPools,
        debug_low_pool: debug,
        ema_fast: Number(($("cswEmaFast") || {}).value || 9),
        ema_medium: Number(($("cswEmaMed") || {}).value || 20),
        ema_slow: Number(($("cswEmaSlow") || {}).value || 59),
        show_detail_markers: !!($("cswDetailMarkers") && $("cswDetailMarkers").checked),
        expire_bars: Number(($("cswExpire") || {}).value || 24),
      };
      setStatus("Cluster Sweep Backtest " + body.symbol + " " + body.timeframe + " …");
      try {
        const snap = await sendJson("/api/research/backtester/run", "POST", body, { sourceAction: "csw-run" });
        applyWorkspace(snap);
        updateCswNav(snap);
        const meta = (snap.cluster_sweep_result && snap.cluster_sweep_result.meta) || {};
        setStatus(
          "Cluster Sweep bereit · " + (meta.n_events || 0) + " Events · "
            + "bull=" + (meta.n_bullish || 0) + " bear=" + (meta.n_bearish || 0)
            + " · Backtester klicken zum Einblenden"
            + (meta.debug_low_pool_zones ? " · LOW-POOL DEBUG" : "")
        );
        const cs = snap.cluster_sweep || {};
        const ev = cs.event || {};
        const focus = ev.confirmation_at || ev.entry_at || ev.approach_at || ev.first_touch_at;
        await syncChartAfterBacktest(body.start, body.end, focus);
      } catch (err) {
        setStatus("Cluster Sweep fehlgeschlagen: " + (err.message || err), "error");
      }
    }

    async function zoomToClusterEvent(ev) {
      if (!ev) return;
      const t0 = Date.parse(ev.approach_at || ev.first_touch_at || ev.cluster_entry_at || "");
      const t1 = Date.parse(ev.entry_at || ev.confirmation_at || ev.invalidated_at || ev.first_touch_at || "");
      if (!t0) return;
      const from = (t0 / 1000) - 30 * 60;
      const to = ((t1 || t0) / 1000) + 30 * 60;
      await Promise.all(PANE_IDS.map(async function (pid) {
        const pane = state.panes[pid];
        if (!pane) return;
        const chart = api(pane) || await whenReady(pane, 4000);
        if (!chart || !chart.setVisibleTimeRange) return;
        try { chart.setVisibleTimeRange(from, to); } catch (e) { /* ignore */ }
      }));
    }

    async function zoomToEdcCandidate(c) {
      if (!c) return;
      const t0 = Date.parse(c.candidate_at || c.decision_at || "");
      if (!t0) return;
      const tf = String(c.timeframe || "15m");
      const tfMin = tf.endsWith("m") ? parseInt(tf, 10) || 15 : 15;
      const pad = 25 * tfMin * 60;
      const from = (t0 / 1000) - pad;
      const to = (t0 / 1000) + pad;
      await Promise.all(PANE_IDS.map(async function (pid) {
        const pane = state.panes[pid];
        if (!pane) return;
        const chart = api(pane) || await whenReady(pane, 4000);
        if (!chart || !chart.setVisibleTimeRange) return;
        try { chart.setVisibleTimeRange(from, to); } catch (e) { /* ignore */ }
      }));
    }

    if ($("researchBtStrategy")) {
      $("researchBtStrategy").addEventListener("change", async function () {
        syncBtStrategyUi();
        const sid = btStrategy();
        try {
          applyWorkspace(await sendJson("/api/research/backtester/load", "POST", {
            strategy_id: "cluster_sweep_ema_9_20_59",
            symbol: state.symbol,
            visible: false,
          }, { sourceAction: "strategy-switch" }));
          applyWorkspace(await sendJson("/api/research/backtester/load", "POST", {
            strategy_id: "ema_dual_cross_multisource_v1",
            symbol: state.symbol,
            visible: false,
          }, { sourceAction: "strategy-switch" }));
          applyWorkspace(await sendJson("/api/research/backtester/load", "POST", {
            strategy_id: "ema_zone_microstructure_confirmation_v1",
            symbol: state.symbol,
            visible: false,
          }, { sourceAction: "strategy-switch" }));
        } catch (e) { /* ignore */ }
        await refreshOverlaysVisible();
        setStatus("Strategie: " + sid);
      });
      syncBtStrategyUi();
    }
    if ($("researchEzmLayerMode")) {
      $("researchEzmLayerMode").addEventListener("change", async function () {
        if (btStrategy() !== "ema_zone_microstructure_confirmation_v1") return;
        try {
          await applyEzmLayerMode(ezmLayerMode());
        } catch (err) {
          setStatus("EZM Layer fehlgeschlagen: " + (err.message || err), "error");
        }
      });
    }
    if ($("researchBtRunBtn")) $("researchBtRunBtn").addEventListener("click", runActiveBacktest);
    if ($("cswRunFromModal")) $("cswRunFromModal").addEventListener("click", async function () {
      await runClusterSweepBacktest();
    });
    if ($("edcRunFromModal")) $("edcRunFromModal").addEventListener("click", async function () {
      await runEmaDualCrossBacktest();
    });
    if ($("researchEdcSettingsBtn")) {
      $("researchEdcSettingsBtn").addEventListener("click", function () {
        ensureEdcDefaults();
        $("modalEdc").hidden = false;
      });
    }
    if ($("researchEdcPrev")) {
      $("researchEdcPrev").addEventListener("click", async function () {
        const snap = await sendJson("/api/research/backtester/ema-dual-cross/nav", "POST", { delta: -1 });
        applyWorkspace(snap);
        updateEdcNav(snap);
        await zoomToEdcCandidate(((snap.ema_dual_cross || {}).candidate));
      });
    }
    if ($("researchEdcNext")) {
      $("researchEdcNext").addEventListener("click", async function () {
        const snap = await sendJson("/api/research/backtester/ema-dual-cross/nav", "POST", { delta: 1 });
        applyWorkspace(snap);
        updateEdcNav(snap);
        await zoomToEdcCandidate(((snap.ema_dual_cross || {}).candidate));
      });
    }
    if ($("researchEdcZoom")) {
      $("researchEdcZoom").addEventListener("click", async function () {
        const ed = (state.workspace || {}).ema_dual_cross || {};
        await zoomToEdcCandidate(ed.candidate);
      });
    }
    if ($("researchCswSettingsBtn")) {
      $("researchCswSettingsBtn").addEventListener("click", function () {
        ensureCswDefaults();
        $("modalCsw").hidden = false;
      });
    }
    if ($("researchCswPrev")) {
      $("researchCswPrev").addEventListener("click", async function () {
        const snap = await sendJson("/api/research/backtester/cluster-sweep/nav", "POST", { delta: -1 });
        applyWorkspace(snap);
        updateCswNav(snap);
        await zoomToClusterEvent((snap.cluster_sweep || {}).event);
      });
    }
    if ($("researchCswNext")) {
      $("researchCswNext").addEventListener("click", async function () {
        const snap = await sendJson("/api/research/backtester/cluster-sweep/nav", "POST", { delta: 1 });
        applyWorkspace(snap);
        updateCswNav(snap);
        await zoomToClusterEvent((snap.cluster_sweep || {}).event);
      });
    }
    if ($("researchCswZoom")) {
      $("researchCswZoom").addEventListener("click", async function () {
        const cs = (state.workspace || {}).cluster_sweep || {};
        await zoomToClusterEvent(cs.event);
      });
    }

    $("researchBacktesterBtn").addEventListener("click", async function () {
      if (!state.symbol) return;
      if (btStrategy() === "cluster_sweep_ema_9_20_59") {
        setStatus("Cluster Sweep Backtester umschalten …");
        try {
          const snap = await sendJson("/api/research/backtester/load", "POST", {
            strategy_id: "cluster_sweep_ema_9_20_59",
            symbol: state.symbol,
          }, { sourceAction: "backtester" });
          applyWorkspace(snap);
          updateCswNav(snap);
          await refreshOverlaysVisible();
          const bt = snap.backtester || {};
          const cs = snap.cluster_sweep || {};
          setStatus(
            "Cluster Sweep " + state.symbol + " · "
              + (bt.visible ? "sichtbar" : "ausgeblendet") + " · "
              + (cs.n_events || 0) + " Events"
              + ((cs.meta && cs.meta.debug_low_pool_zones) ? " · LOW-POOL DEBUG" : "")
          );
        } catch (err) {
          setStatus("Backtester fehlgeschlagen: " + (err.message || err), "error");
        }
        return;
      }
      if (btStrategy() === "ema_dual_cross_multisource_v1") {
        setStatus("EMA Dual Cross Backtester umschalten …");
        try {
          const snap = await sendJson("/api/research/backtester/load", "POST", {
            strategy_id: "ema_dual_cross_multisource_v1",
            symbol: state.symbol,
          }, { sourceAction: "backtester" });
          applyWorkspace(snap);
          updateEdcNav(snap);
          await refreshOverlaysVisible();
          const bt = snap.backtester || {};
          const ed = snap.ema_dual_cross || {};
          setStatus(
            "EMA Dual Cross " + state.symbol + " · "
              + (bt.visible ? "sichtbar" : "ausgeblendet") + " · "
              + (ed.n_candidates || 0) + " Kandidaten"
          );
        } catch (err) {
          setStatus("Backtester fehlgeschlagen: " + (err.message || err), "error");
        }
        return;
      }
      if (btStrategy() === "ema_zone_microstructure_confirmation_v1") {
        setStatus("EZM Backtester umschalten …");
        try {
          const snap = await sendJson("/api/research/backtester/load", "POST", {
            strategy_id: "ema_zone_microstructure_confirmation_v1",
            symbol: state.symbol,
            ezm_layer_mode: ezmLayerMode(),
          }, { sourceAction: "backtester" });
          applyWorkspace(snap);
          syncEzmLayerUi(snap);
          await refreshOverlaysVisible();
          const bt = snap.backtester || {};
          const ez = snap.ezm || {};
          setStatus(
            "EZM " + state.symbol + " · "
              + ezmLayerModeLabel(ez.layer_mode || ezmLayerMode()) + " · "
              + (bt.visible ? "sichtbar" : "ausgeblendet") + " · "
              + (ez.n_setup_markers || bt.n_setup_markers || 0) + " Setup · "
              + (ez.n_micro_markers || bt.n_micro_markers || 0) + " Micro · keine Trades/PnL"
          );
        } catch (err) {
          setStatus("Backtester fehlgeschlagen: " + (err.message || err), "error");
        }
        return;
      }
      const strategy = lastStochStrategy();
      const jobId = lastStochResearchJob();
      const evalId = lastStochEvaluation();
      applyResearchJobSourceNote();
      setStatus("Backtester: lade " + (evalId ? "Evaluation " + evalId : (jobId ? "Job " + jobId : strategy)) + " für " + state.symbol + " …");
      try {
        const body = { symbol: state.symbol, strategy_id: "stoch_fade" };
        if (jobId) {
          body.job_id = jobId;
          if (evalId) {
            body.source = "FROZEN_RESEARCH_EVALUATION";
            body.evaluation_id = evalId;
          } else {
            body.source = "FROZEN_RESEARCH_JOB";
          }
        } else {
          body.hours = 48;
          body.strategy_version = strategy;
        }
        const snap = await sendJson("/api/research/backtester/load", "POST", body, { sourceAction: "backtester" });
        applyWorkspace(snap);
        await refreshOverlaysVisible();
        const bt = snap.backtester || {};
        const windowLabel = (bt.signal_start && bt.signal_end_exclusive)
          ? (" · Fenster " + bt.signal_start + " – " + bt.signal_end_exclusive)
          : "";
        const modeLabel = evalId
          ? ("FROZEN_RESEARCH_EVALUATION " + (bt.display_mode || "FROZEN_NO_BE50_EVALUATED"))
          : (jobId ? ("FROZEN_RESEARCH_JOB " + (bt.display_mode || "PLANNED_NO_OUTCOME")) : (bt.strategy_version || strategy));
        setStatus(
          "Backtester " + state.symbol + " · "
            + modeLabel
            + windowLabel + ": "
            + (bt.loaded || 0) + " Long/Short (Entry/TP/SL)"
            + (bt.skipped ? ", " + bt.skipped + " übersprungen" : "")
            + (evalId ? " — echte Exit-Marker · NO_BE50 / SL_FIRST · keine BE-Exits" : (jobId ? " — 4h-Planhorizont nur visuelle Projektion · keine Exit-/OPEN-/PnL-Marker" : ""))
            + (bt.message ? " — " + bt.message : "")
        );
      } catch (err) {
        setStatus("Backtester fehlgeschlagen: " + (err.message || err), "error");
      }
    });
    $("researchIndStoch").addEventListener("change", async function () {
      applyWorkspace(await sendJson("/api/research/indicator-enabled", "POST", {
        name: "stochastic", enabled: $("researchIndStoch").checked,
      }, { sourceAction: "stoch-toggle" }));
      await refreshIndicatorsVisible("stoch-toggle");
    });
    $("researchIndLld").addEventListener("change", async function () {
      applyWorkspace(await sendJson("/api/research/indicator-enabled", "POST", {
        name: "liquidity", enabled: $("researchIndLld").checked,
      }, { sourceAction: "lld-toggle" }));
      await refreshIndicatorsVisible("lld-toggle");
    });
    $("trpEmaSettings").addEventListener("click", function () {
      renderEmaRows(((state.workspace || {}).ema || {}).lines || []);
      openModal("modalEma");
    });
    $("trpStochSettings").addEventListener("click", function () {
      fillStoch((state.workspace || {}).stochastic || {});
      openModal("modalStoch");
    });
    $("trpLldSettings").addEventListener("click", function () {
      $("lldLicense").textContent = (state.workspace || {}).license_notice || "";
      fillLld((state.workspace || {}).liquidity || {});
      openModal("modalLld");
    });
    $("emaAdd").addEventListener("click", function () {
      const used = new Set((state.emaDraft || []).map(function (l) { return l.period; }));
      let period = 50;
      while (used.has(period)) period += 1;
      state.emaDraft = (state.emaDraft || []).concat([{
        ema_id: "ema-" + Math.random().toString(16).slice(2, 12),
        enabled: true, period: period, color: "#3dcc91", line_width: 2, transparency: 0,
      }]);
      renderEmaRows(state.emaDraft);
    });
    $("emaApply").addEventListener("click", async function () {
      try {
        applyWorkspace(await sendJson("/api/research/settings", "PUT", { ema: { lines: state.emaDraft } }, {
          sourceAction: "ema-apply",
        }));
        closeModal("modalEma");
        await refreshIndicatorsVisible("ema-apply");
      } catch (err) {
        $("emaError").hidden = false;
        $("emaError").textContent = String(err.message || err);
      }
    });
    $("stochApply").addEventListener("click", async function () {
      try {
        applyWorkspace(await sendJson("/api/research/settings", "PUT", { stochastic: readStoch() }, {
          sourceAction: "stoch-apply",
        }));
        closeModal("modalStoch");
        await refreshIndicatorsVisible("stoch-apply");
      } catch (err) {
        $("stochError").hidden = false;
        $("stochError").textContent = String(err.message || err);
      }
    });
    $("lldApply").addEventListener("click", async function () {
      try {
        applyWorkspace(await sendJson("/api/research/settings", "PUT", { liquidity: readLld() }, {
          sourceAction: "lld-apply",
        }));
        closeModal("modalLld");
        await refreshIndicatorsVisible("lld-apply");
      } catch (err) {
        $("lldError").hidden = false;
        $("lldError").textContent = String(err.message || err);
      }
    });
    $("trpDelete").addEventListener("click", async function () {
      applyWorkspace(await sendJson("/api/research/drawings/delete", "POST", {}));
      await refreshOverlaysVisible();
    });
    $("trpClear").addEventListener("click", async function () {
      if (!window.confirm("Zeichnungen dieses Symbols löschen?")) return;
      applyWorkspace(await sendJson("/api/research/drawings/clear", "POST", { symbol: state.symbol }));
      await refreshOverlaysVisible();
    });
    $("trpDrawColor").addEventListener("change", async function () {
      applyWorkspace(await sendJson("/api/research/drawings/style", "POST", { color: $("trpDrawColor").value }));
      await refreshOverlaysVisible();
    });
    $("trpDrawWidth").addEventListener("change", async function () {
      applyWorkspace(await sendJson("/api/research/drawings/style", "POST", { width: Number($("trpDrawWidth").value) }));
      await refreshOverlaysVisible();
    });
    $("trpPositionSettings").addEventListener("click", openPositionModal);
    $("posNotional").addEventListener("change", function () {
      if (state.posGuard) return;
      state.posGuard = true;
      $("posQty").value = Number($("posNotional").value) / Math.max(Number($("posEntry").value) || 1, 1e-9);
      state.posGuard = false;
    });
    $("posQty").addEventListener("change", function () {
      if (state.posGuard) return;
      state.posGuard = true;
      $("posNotional").value = Number($("posQty").value) * Number($("posEntry").value);
      state.posGuard = false;
    });
    $("posApply").addEventListener("click", async function () {
      applyWorkspace(await sendJson("/api/research/position", "POST", {
        drawing_id: (state.workspace || {}).selected_id,
        entry_price: Number($("posEntry").value),
        stop_price: Number($("posStop").value),
        target_price: Number($("posTarget").value),
        position_notional: Number($("posNotional").value),
        default_risk_reward: Number($("posRr").value),
        profit_color: $("posProfit").value,
        loss_color: $("posLoss").value,
        color: $("posEntryColor").value,
        width: Number($("posWidth").value),
        fill_opacity: Number($("posFill").value) / 100,
      }));
      closeModal("modalPos");
      await refreshOverlaysVisible();
    });
    document.querySelectorAll("[data-close]").forEach(function (btn) {
      btn.addEventListener("click", function () { closeModal(btn.getAttribute("data-close")); });
    });
    document.querySelectorAll("[data-reset]").forEach(function (btn) {
      btn.addEventListener("click", async function () {
        const kind = btn.getAttribute("data-reset");
        const defaults = await getJson("/api/research/settings/defaults");
        if (kind === "ema") renderEmaRows(defaults.ema.lines);
        if (kind === "stoch") fillStoch(Object.assign({}, defaults.stochastic, { enabled: $("researchIndStoch").checked }));
        if (kind === "lld") fillLld(Object.assign({}, defaults.liquidity, { enabled: $("researchIndLld").checked }));
      });
    });
    window.addEventListener("keydown", function (ev) {
      if (ev.key === "Shift") syncHostShift(true);
      if (ev.key === "Escape") {
        if (state.paneFs || isBrowserFs()) {
          state.paneFs = null;
          applyLayout(state.layout, true);
          setBrowserFs(false);
          return;
        }
        handleChartKey("escape");
      }
      if (ev.key === "Delete" || ev.key === "Backspace") {
        const tag = (ev.target && ev.target.tagName) || "";
        if (tag !== "INPUT" && tag !== "SELECT" && tag !== "TEXTAREA") handleChartKey("delete");
      }
    });
    window.addEventListener("keyup", function (ev) {
      if (ev.key === "Shift") syncHostShift(false);
    });
    window.addEventListener("blur", function () { syncHostShift(false); });
    document.addEventListener("fullscreenchange", function () {
      requestAnimationFrame(function () {
        visibleIds().forEach(function (pid) {
          const chart = api(state.panes[pid]);
          if (chart && chart.resize) chart.resize();
        });
      });
    });
  }

  async function boot() {
    try {
      const saved = localStorage.getItem(LAYOUT_KEY);
      if (saved && PANE_COUNT[saved]) state.layout = saved;
    } catch (e) {}
    buildTools();
    buildPanes();
    bindUi();
    restoreWorkspaceHeight();
    const symbols = await getJson("/api/research/symbols");
    state.symbols = symbols.symbols || [];
    const names = fillSymbolSelect(state.symbols);
    if (!names.length) {
      setStatus("No symbols from /api/research/symbols", "empty");
      return;
    }
    try {
      applyWorkspace(await getJson("/api/research/workspace"));
    } catch (err) {
      setStatus(String(err.message || err), "error");
    }
    state.vp = Object.assign(
      defaultVolumeProfile(),
      (state.workspace && state.workspace.volume_profile) || {},
      readStoredVolumeProfile()
    );
    fillVolumeProfileControls();
    state.obp = Object.assign(
      defaultOrderbookProfile(),
      (state.workspace && state.workspace.orderbook_profile) || {},
      readStoredOrderbookProfile()
    );
    fillOrderbookProfileControls();
    const start = pickDefaultSymbol(names);
    if (!start) {
      setStatus("No selectable symbol", "empty");
      return;
    }
    $("researchSymbol").value = start;
    applyResearchJobSourceNote();
    await switchSymbol(start);
  }

  window.__researchDebug = state;

  document.addEventListener("DOMContentLoaded", function () {
    boot().catch(function (err) {
      if (err && err.name === "AbortError") return;
      setStatus(String(err.message || err), "error");
    });
  });
})();
