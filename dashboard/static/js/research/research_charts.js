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
  const POLL_MS = 5000;
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
    layout: "4",
    symbol: "",
    sync: true,
    panes: {},
    pollGen: 0,
    pollTimer: null,
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
    const info = logReq(Object.assign({
      method: "GET",
      url: url,
      symbol: state.symbol,
      timeframe: meta && meta.timeframe,
      sourceAction: (meta && meta.sourceAction) || "get",
      pane: meta && meta.pane,
      generation: state.loadGen,
    }, meta || {}));
    if (inflightGets[url]) {
      info.coalesced = true;
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
        chart.setInteractionMode(pane.pendingMode || toolMode());
        pane.phase = "INTERACTION_READY";
        chart.resize();
        if (chart.setHostShift) chart.setHostShift(!!state.hostShift);
      },
      on_crosshair_move: function (unix) { handleHover(pane.id, unix); },
      on_chart_click: function (unix) { handleClick(pane.id, unix); },
      on_crosshair_leave: function () { handleHoverLeft(pane.id); },
      on_visible_range: function () {},
      on_drawing_event: function (blob) { handleDrawingEvent(pane.id, blob); },
      on_chart_key: function (key) { handleChartKey(key); },
    };
  }

  function toolMode() {
    const tool = (state.workspace && state.workspace.tool) || "select";
    return tool === "select" ? "select" : tool;
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
    Object.keys(wanted).forEach(function (id) {
      if (!prev[id]) chart.addOverlay(wanted[id]);
      else if (JSON.stringify(prev[id]) !== JSON.stringify(wanted[id])) chart.updateOverlay(wanted[id]);
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
      };
      state.panes[pid] = pane;
      iframe.addEventListener("load", function () {
        attachBridge(pane);
      });
      iframe.src = "/static/research_trp/pane.html?v=price-scale-1";
      tfSel.addEventListener("change", function () {
        pane.tf = tfSel.value;
        loadPane(pid, { force: true, sourceAction: "tf-change" });
      });
      wrap.querySelector(".trp-reset").addEventListener("click", function () {
        const chart = api(pane);
        if (chart && chart.resetView) chart.resetView();
      });
      new ResizeObserver(function () {
        const chart = api(pane);
        if (chart && !wrap.classList.contains("pooled-hidden")) chart.resize();
      }).observe(wrap);
      root.appendChild(wrap);
    });
    applyLayout(state.layout, true);
  }

  function applyLayout(layout, force) {
    if (!force && layout === state.layout) return;
    state.layout = layout;
    const root = $("researchWorkspace");
    root.className = "trp-workspace layout-" + layout;
    const vis = new Set(visibleIds());
    PANE_IDS.forEach(function (pid) {
      const pane = state.panes[pid];
      if (!pane) return;
      pane.el.classList.toggle("pooled-hidden", !vis.has(pid));
    });
    document.querySelectorAll(".trp-layout-btn").forEach(function (btn) {
      btn.classList.toggle("active", btn.dataset.layout === layout);
    });
    requestAnimationFrame(function () {
      visibleIds().forEach(function (pid) {
        const chart = api(state.panes[pid]);
        if (chart) chart.resize();
      });
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
    const ema = snap.ema || { lines: [] };
    const enabled = (ema.lines || []).filter(function (l) { return l.enabled; }).map(function (l) { return "EMA" + l.period; });
    $("trpEmaSummary").textContent = enabled.length ? enabled.join(", ") : "off";
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

  function applyPaneBundle(pane, packed, opts) {
    const payload = {
      symbol: packed.symbol,
      timeframe: packed.timeframe,
      is_demo: false,
      candles: packed.candles || [],
    };
    const nextFp = candleFingerprint(packed.candles || []);
    const skipCandles = !!(opts && opts.indicatorsOnly && pane.candleFp === nextFp && pane.pendingData);
    pane.lastTimes = new Set((packed.candles || []).map(function (c) { return c.time; }));
    pane.pendingData = payload;
    pane.candleFp = nextFp;
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
        chart.setData(payload);
        pane.phase = "DATA_READY";
      }
      chart.setEmaOverlays(pane.pendingEma);
      chart.setLowerPane(pane.pendingLower);
      chart.setLldEma(pane.pendingLldEma);
      chart.setInteractionMode(pane.pendingMode || toolMode());
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
    pane.el.querySelector(".trp-pane-status").textContent = "loading…";
    let packed;
    try {
      packed = await sendJson("/api/research/pane", "POST", {
      symbol: state.symbol,
      timeframe: pane.tf,
      ema: ws.ema || { enabled: false },
      stochastic: ws.stochastic || { enabled: false },
      liquidity: ws.liquidity || { enabled: false },
      allow_stale: !!(opts && opts.allowStale),
    }, {
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
    applyPaneBundle(pane, packed, { indicatorsOnly: !!(opts && opts.indicatorsOnly) });
    if (force) {
      const ready = api(pane);
      if (ready && ready.resetView) ready.resetView();
    }
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
  }

  function startPoll() {
    if (!state.initialLoadDone) return;
    stopPoll();
    const gen = state.pollGen;
    state.pollTimer = setInterval(function () {
      if (gen !== state.pollGen || !state.initialLoadDone) return;
      pollIncremental(gen);
    }, POLL_MS);
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
    const times = Array.from(pane.lastTimes || []);
    const last = times.length ? Math.max.apply(null, times) : null;
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
    const byTime = {};
    (pane.pendingData.candles || []).forEach(function (c) { byTime[c.time] = c; });
    incoming.forEach(function (c) { byTime[c.time] = c; });
    const merged = Object.keys(byTime).map(Number).sort(function (a, b) { return a - b; }).map(function (t) { return byTime[t]; });
    const nextFp = candleFingerprint(merged);
    if (nextFp === pane.candleFp) return;
    pane.pendingData = Object.assign({}, pane.pendingData, { candles: merged });
    pane.lastTimes = new Set(merged.map(function (c) { return c.time; }));
    pane.candleFp = nextFp;
    const chart = api(pane);
    if (chart) chart.setData(pane.pendingData);
    await loadPane(paneId, {
      allowStale: false,
      indicatorsOnly: true,
      sourceAction: "poll-indicators",
    });
  }

  async function refreshLiveBar() {
    if (!state.symbol) return;
    const body = await getJson(
      "/api/research/live-status?symbol=" + encodeURIComponent(state.symbol) + "&ensure=false"
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
    await refreshLiveBar();
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

  function pickDefaultSymbol(names) {
    const list = (names || []).slice();
    let preferred = "";
    try { preferred = localStorage.getItem(SYMBOL_KEY) || ""; } catch (e) {}
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
    document.querySelectorAll(".trp-layout-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        applyLayout(btn.dataset.layout);
        mapLimit(visibleIds().filter(function (pid) {
          const pane = state.panes[pid];
          return pane && !pane.lastTimes.size;
        }), PANE_HTTP_LIMIT, function (pid) {
          return loadPane(pid, { sourceAction: "layout-change" });
        });
      });
    });
    $("researchSymbol").addEventListener("change", function () { switchSymbol($("researchSymbol").value); });
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
      if (ev.key === "Escape") handleChartKey("escape");
      if (ev.key === "Delete" || ev.key === "Backspace") {
        const tag = (ev.target && ev.target.tagName) || "";
        if (tag !== "INPUT" && tag !== "SELECT" && tag !== "TEXTAREA") handleChartKey("delete");
      }
    });
    window.addEventListener("keyup", function (ev) {
      if (ev.key === "Shift") syncHostShift(false);
    });
    window.addEventListener("blur", function () { syncHostShift(false); });
  }

  async function boot() {
    buildTools();
    buildPanes();
    bindUi();
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
    const start = pickDefaultSymbol(names);
    if (!start) {
      setStatus("No selectable symbol", "empty");
      return;
    }
    $("researchSymbol").value = start;
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
