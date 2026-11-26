/* Orderflow Price Impact — single combined chart (Variant B), 1:1 main logical sync (stoch pattern). */
(function (root) {
  "use strict";

  var main = null;
  var opiChart = null;
  var alignCandle = null;
  var baseLine = null;
  var sTotal = null;
  var sDelta = null;
  var sPrice = null;
  var baseTimes = [];
  var lock = false;
  var bars = [];
  var byTime = {};
  var meta = {};
  var rafId = 0;
  var syncGen = 0;
  var postLayoutGen = 0;
  var postLayoutRaf1 = 0;
  var postLayoutRaf2 = 0;
  var scaleWidth = 84;
  var paintedOnBase = false;
  var lastPushBaseKey = "";
  var currentDiagUpdateId = null;
  var lifecycleCycles = [];
  var LIFECYCLE_CYCLES_MAX = 6;
  var lifecycleCurrent = null;
  var lastOpiLayoutKey = "";
  var layoutTraceGen = 0;
  var lastAppliedTsOptKey = "";
  var opiXhairLock = false;
  var lastOpiCrosshairTime = null;
  var suppressOpiUntilTime = null;
  var crosshairBridge = null;

  function alignDataLen() {
    try {
      if (alignCandle && typeof alignCandle.data === "function") {
        return alignCandle.data().length;
      }
    } catch (e) { /* ignore */ }
    return null;
  }

  /** OPI align candles use the same 5m index grid as Main — 1:1 logical sync is valid. */
  function sharedIndexBase() {
    if (baseTimes.length < 1) return false;
    var dataLen = alignDataLen();
    return dataLen == null || dataLen === baseTimes.length;
  }

  function viewportMetrics() {
    var ml = null;
    var ol = null;
    try { ml = main && main.timeScale().getVisibleLogicalRange(); } catch (e0) { ml = null; }
    try { ol = opiChart && opiChart.timeScale().getVisibleLogicalRange(); } catch (e1) { ol = null; }
    var row = {
      t: new Date().toISOString(),
      updateId: currentDiagUpdateId,
      mainFrom: ml && ml.from != null ? Number(ml.from) : null,
      mainTo: ml && ml.to != null ? Number(ml.to) : null,
      opiFrom: ol && ol.from != null ? Number(ol.from) : null,
      opiTo: ol && ol.to != null ? Number(ol.to) : null,
      baseTimesLen: baseTimes.length,
      alignDataLen: alignDataLen(),
      barCount: bars.length,
      symbol: meta && meta.symbol ? meta.symbol : null,
      timeframe: meta && meta.timeframe ? meta.timeframe : null,
      windowMinutes: meta && meta.window_minutes != null ? meta.window_minutes : null,
      syncGen: syncGen,
    };
    if (ml && ol && ml.from != null && ol.from != null) {
      row.dFrom = +(Number(ol.from) - Number(ml.from)).toFixed(4);
    }
    if (ml && ol && ml.to != null && ol.to != null) {
      row.dTo = +(Number(ol.to) - Number(ml.to)).toFixed(4);
    }
    return row;
  }

  function beginLifecycleCycle(trigger) {
    lifecycleCurrent = {
      updateId: currentDiagUpdateId,
      trigger: trigger || null,
      stages: [],
      skipped: [],
      startedAt: new Date().toISOString(),
    };
  }

  function finishLifecycleCycle() {
    if (!lifecycleCurrent) return;
    lifecycleCycles.push(lifecycleCurrent);
    while (lifecycleCycles.length > LIFECYCLE_CYCLES_MAX) lifecycleCycles.shift();
    lifecycleCurrent = null;
  }

  function recordLifecycleSkipped(stage, reason) {
    if (!lifecycleCurrent) beginLifecycleCycle("apply");
    lifecycleCurrent.skipped.push({
      stage: stage,
      reason: reason,
      t: new Date().toISOString(),
    });
    if (viewportDiagActive() && window.OpiRuntimeTrace && OpiRuntimeTrace._push) {
      OpiRuntimeTrace._push("opiLifecycleSkipped", { stage: stage, reason: reason, updateId: currentDiagUpdateId });
    }
  }

  function resumeLifecycleForFrames(extra) {
    if (lifecycleCurrent) return;
    var uid = extra && extra.updateId != null ? Number(extra.updateId) : currentDiagUpdateId;
    for (var i = lifecycleCycles.length - 1; i >= 0; i--) {
      if (lifecycleCycles[i].updateId === uid) {
        lifecycleCurrent = lifecycleCycles[i];
        lifecycleCycles.splice(i, 1);
        return;
      }
    }
    beginLifecycleCycle("rAF");
    lifecycleCurrent.updateId = uid;
  }

  function recordLifecycleStage(stage, extra) {
    if (stage === "next_animation_frame" || stage === "second_animation_frame") {
      resumeLifecycleForFrames(extra || {});
    } else if (!lifecycleCurrent) {
      beginLifecycleCycle("apply");
    }
    var row = viewportMetrics();
    row.stage = stage;
    if (extra) {
      Object.keys(extra).forEach(function (k) { row[k] = extra[k]; });
    }
    lifecycleCurrent.stages.push(row);
    if (viewportDiagActive() && window.OpiRuntimeTrace && OpiRuntimeTrace._push) {
      OpiRuntimeTrace._push("opiLifecycle", row);
    }
    if (stage === "second_animation_frame") finishLifecycleCycle();
  }

  function debugUpdateLifecycle() {
    var cycle = lifecycleCurrent || (lifecycleCycles.length ? lifecycleCycles[lifecycleCycles.length - 1] : null);
    if (!cycle) return { ok: false, reason: "no_lifecycle_recorded" };
    var maxAbsDFrom = 0;
    var firstSkewStage = null;
    var recoveryStage = null;
    var frame1 = null;
    var frame2 = null;
    (cycle.stages || []).forEach(function (st) {
      var df = st.dFrom != null ? Math.abs(Number(st.dFrom)) : 0;
      if (df > maxAbsDFrom) maxAbsDFrom = df;
      if (firstSkewStage == null && df > 0.05) firstSkewStage = st.stage;
      if (firstSkewStage && df <= 0.05 && recoveryStage == null && st.stage !== firstSkewStage) {
        recoveryStage = st.stage;
      }
      if (st.stage === "next_animation_frame") frame1 = st;
      if (st.stage === "second_animation_frame") frame2 = st;
    });
    return {
      ok: true,
      readOnly: true,
      updateId: cycle.updateId,
      trigger: cycle.trigger,
      startedAt: cycle.startedAt,
      stages: cycle.stages.slice(),
      skipped: cycle.skipped.slice(),
      summary: {
        maxAbsDFrom: +maxAbsDFrom.toFixed(4),
        firstSkewStage: firstSkewStage,
        recoveryStage: recoveryStage,
        frame1DFrom: frame1 && frame1.dFrom != null ? frame1.dFrom : null,
        frame2DFrom: frame2 && frame2.dFrom != null ? frame2.dFrom : null,
        barCount: cycle.stages.length ? cycle.stages[cycle.stages.length - 1].barCount : null,
        stackHidden: !!($("opi-stack") && $("opi-stack").hidden),
      },
      priorCycles: lifecycleCycles.length,
    };
  }

  function $(id) {
    return document.getElementById(id);
  }

  function num(v) {
    var n = Number(v);
    return Number.isFinite(n) ? n : null;
  }

  function compact(v) {
    var n = Number(v);
    if (!Number.isFinite(n)) return "–";
    var sign = n < 0 ? "−" : "";
    var a = Math.abs(n);
    if (a >= 1e6) return sign + (a / 1e6).toFixed(2) + " Mio";
    if (a >= 1e3) return sign + (a / 1e3).toFixed(1) + "k";
    return sign + a.toFixed(0);
  }

  function mainTimeScaleOpts() {
    if (!main) return null;
    try {
      return main.timeScale().options();
    } catch (err) {
      return null;
    }
  }

  function combinedOpts(el) {
    var ro = 0;
    var bs = 7;
    var tsOpt = mainTimeScaleOpts();
    if (tsOpt) {
      if (tsOpt.rightOffset != null) ro = tsOpt.rightOffset;
      if (tsOpt.barSpacing != null) bs = tsOpt.barSpacing;
    }
    return {
      layout: { background: { color: "#131722" }, textColor: "#d1d4dc", fontFamily: "Inter, system-ui, sans-serif" },
      grid: { vertLines: { color: "#1e222d" }, horzLines: { color: "#1e222d" } },
      leftPriceScale: {
        visible: false,
        autoScale: true,
        minimumWidth: 0,
        borderVisible: false,
        scaleMargins: { top: 0.55, bottom: 0.05 },
      },
      rightPriceScale: {
        visible: true,
        borderColor: "#2a2e39",
        autoScale: true,
        minimumWidth: scaleWidth,
        scaleMargins: { top: 0.08, bottom: 0.42 },
      },
      timeScale: {
        borderColor: "#2a2e39",
        timeVisible: false,
        secondsVisible: false,
        visible: false,
        rightOffset: ro,
        barSpacing: bs,
        minBarSpacing: 2,
      },
      crosshair: { mode: 0 },
      handleScroll: false,
      handleScale: false,
      width: Math.max(el.clientWidth || 0, 16),
      height: Math.max(el.clientHeight || 0, 16),
    };
  }

  function applyTimeScaleOpts(opt) {
    if (!opiChart || !opt) return false;
    var key = String(opt.barSpacing) + "|" + String(opt.rightOffset);
    if (key === lastAppliedTsOptKey) return false;
    lastAppliedTsOptKey = key;
    try {
      opiChart.timeScale().applyOptions({
        barSpacing: opt.barSpacing,
        rightOffset: opt.rightOffset,
      });
    } catch (err) { /* ignore */ }
    return true;
  }

  function priceScaleWidth(chart, side) {
    try {
      var w = chart.priceScale(side).width();
      if (Number.isFinite(w) && w >= 0) return Math.round(w);
    } catch (err) { /* ignore */ }
    return 0;
  }

  function timeScalePlotWidth(chart, hostEl) {
    var apiW = 0;
    try {
      var w = chart.timeScale().width();
      if (Number.isFinite(w) && w > 0) apiW = Math.round(w);
    } catch (err) { /* ignore */ }
    if (apiW > 0) return { width: apiW, source: "timeScale_api" };
    var el = hostEl || null;
    if (!el && chart === main) el = $("chart");
    if (!el && chart === opiChart) el = $("opi-combined");
    var cw = el ? el.clientWidth : 0;
    if (cw > 0) {
      var est = cw - priceScaleWidth(chart, "left") - priceScaleWidth(chart, "right");
      if (est > 0) return { width: Math.round(est), source: "client_minus_scales" };
    }
    return { width: 0, source: apiW === 0 ? "hidden_or_unready" : "unknown" };
  }

  function matchPlotWidth(traceTag) {
    if (!main || !opiChart) return;
    var opiEl = $("opi-combined");
    if (!opiEl) return;
    traceLayoutEvent((traceTag || "matchPlotWidth") + "_before", {});
    var hostW = Math.max(opiEl.clientWidth, 16);
    var leftW = priceScaleWidth(main, "left");
    var rightW = priceScaleWidth(main, "right");
    if (rightW < 40) rightW = scaleWidth;
    try {
      opiChart.applyOptions({
        width: hostW,
        height: Math.max(opiEl.clientHeight, 16),
        leftPriceScale: { visible: false, minimumWidth: leftW, borderVisible: false },
        rightPriceScale: { minimumWidth: rightW },
      });
    } catch (err) { /* ignore */ }
    alignHosts();
    traceLayoutEvent((traceTag || "matchPlotWidth") + "_after", {});
  }

  function ensureOpiChart() {
    var el = $("opi-combined");
    if (!el || typeof LightweightCharts === "undefined") return null;
    if (opiChart) return opiChart;
    opiChart = LightweightCharts.createChart(el, combinedOpts(el));
    alignCandle = opiChart.addCandlestickSeries({
      visible: false,
      priceScaleId: "left",
      upColor: "rgba(0,0,0,0)",
      downColor: "rgba(0,0,0,0)",
      borderVisible: false,
      wickVisible: false,
    });
    baseLine = opiChart.addLineSeries({
      visible: false,
      color: "rgba(0,0,0,0)",
      priceScaleId: "left",
      autoscaleInfoProvider: function () {
        return { priceRange: { minValue: 0, maxValue: 1 } };
      },
    });
  sTotal = opiChart.addHistogramSeries({
      priceScaleId: "right",
      priceFormat: opiFormat(),
      priceLineVisible: false,
      lastValueVisible: false,
    });
    sDelta = opiChart.addHistogramSeries({
      priceScaleId: "left",
      priceFormat: moneyFormat(),
      priceLineVisible: false,
      lastValueVisible: false,
    });
    sPrice = opiChart.addLineSeries({
      priceScaleId: "right",
      color: "#c9a227",
      lineWidth: 2,
      priceFormat: pctFormat(),
      priceLineVisible: false,
      lastValueVisible: false,
    });
    opiChart.subscribeCrosshairMove(function (param) {
      if (opiXhairLock) return;
      var unix = param && param.time != null ? Number(param.time) : null;
      hoverTime(unix);
      if (!crosshairBridge || !isOpiStackVisible()) return;
      if (unix == null) {
        if (lastOpiCrosshairTime != null) {
          lastOpiCrosshairTime = null;
          suppressOpiUntilTime = null;
          if (typeof crosshairBridge.on_leave === "function") {
            crosshairBridge.on_leave();
          }
        }
        return;
      }
      if (suppressOpiUntilTime != null) {
        var echoed = unix === suppressOpiUntilTime;
        suppressOpiUntilTime = null;
        if (echoed) {
          lastOpiCrosshairTime = unix;
          return;
        }
      }
      if (unix === lastOpiCrosshairTime) return;
      lastOpiCrosshairTime = unix;
      if (typeof crosshairBridge.on_move === "function") {
        crosshairBridge.on_move(unix);
      }
    });
    return opiChart;
  }

  function isOpiStackVisible() {
    var stack = $("opi-stack");
    return !!(stack && !stack.hidden);
  }

  function hasAlignTime(unix) {
    if (!Number.isFinite(unix) || !baseTimes.length) return false;
    for (var i = 0; i < baseTimes.length; i++) {
      if (baseTimes[i] === unix) return true;
    }
    return false;
  }

  function setSyncedCrosshair(unix) {
    if (!opiChart || !alignCandle || !isOpiStackVisible()) return;
    unix = Number(unix);
    if (!hasAlignTime(unix)) {
      clearSyncedCrosshair();
      return;
    }
    suppressOpiUntilTime = unix;
    lastOpiCrosshairTime = unix;
    opiXhairLock = true;
    try {
      opiChart.setCrosshairPosition(1, unix, alignCandle);
    } catch (err) { /* ignore */ }
    opiXhairLock = false;
  }

  function clearSyncedCrosshair() {
    suppressOpiUntilTime = null;
    lastOpiCrosshairTime = null;
    if (!opiChart) return;
    try {
      opiChart.clearCrosshairPosition();
    } catch (err) { /* ignore */ }
  }

  function setCrosshairBridge(bridge) {
    crosshairBridge = bridge || null;
  }

  function pushBaseKey() {
    if (!baseTimes.length) return "";
    return baseTimes.length + ":" + baseTimes[0] + ":" + baseTimes[baseTimes.length - 1];
  }

  function needsPushBase() {
    var k = pushBaseKey();
    return !!k && k !== lastPushBaseKey;
  }

  function viewportDiagActive() {
    return !!(window.OpiViewportDiag && window.OpiViewportDiag.enabled)
      || (window.OpiRuntimeTrace && OpiRuntimeTrace.isEnabled && OpiRuntimeTrace.isEnabled());
  }

  function traceViewport(stage, extra) {
    recordLifecycleStage(stage, extra);
  }

  function mainLogicalSnapshot() {
    var r = mainLogicalRange(null);
    if (!r || r.from == null || r.to == null) return null;
    return { from: Number(r.from), to: Number(r.to) };
  }

  function reassertMainLogical(reason, snap) {
    var target = snap || mainLogicalSnapshot();
    if (!target || !main || !opiChart || baseTimes.length < 1) return;
    recordLifecycleStage("before_reassertMainLogical", { reason: reason });
    applySync(false, target, reason);
    recordLifecycleStage("after_reassertMainLogical", { reason: reason });
  }

  /**
   * LWC may defer visible-range shifts until after histogram setData layout (see P3J lifecycle).
   * Reassert on the second animation frame only — avoids matchPlotWidth sync fighting layout (P3K).
   */
  function reassertAfterLayout(reason, extra) {
    if (!main || !opiChart || baseTimes.length < 1) return;
    var uid = extra && extra.updateId != null ? Number(extra.updateId) : currentDiagUpdateId;
    var token = (postLayoutGen += 1);
    if (postLayoutRaf1) {
      cancelAnimationFrame(postLayoutRaf1);
      postLayoutRaf1 = 0;
    }
    if (postLayoutRaf2) {
      cancelAnimationFrame(postLayoutRaf2);
      postLayoutRaf2 = 0;
    }
    postLayoutRaf1 = requestAnimationFrame(function () {
      postLayoutRaf1 = 0;
      if (token !== postLayoutGen) return;
      recordLifecycleStage("next_animation_frame", { updateId: uid, reason: reason });
      postLayoutRaf2 = requestAnimationFrame(function () {
        postLayoutRaf2 = 0;
        if (token !== postLayoutGen) return;
        reassertMainLogical((reason || "post_layout") + "_raf2", mainLogicalSnapshot());
        recordLifecycleStage("second_animation_frame", { updateId: uid, reason: reason });
      });
    });
  }

  function pushBase() {
    if (!opiChart || !baseTimes.length) return;
    var tsOpt = mainTimeScaleOpts();
    if (tsOpt) applyTimeScaleOpts(tsOpt);
    var data = [];
    var candles = [];
    for (var i = 0; i < baseTimes.length; i++) {
      data.push({ time: baseTimes[i] });
      candles.push({ time: baseTimes[i], open: 1, high: 1, low: 1, close: 1 });
    }
    recordLifecycleStage("before_pushBase", { key: pushBaseKey(), setDataCalls: ["alignCandle", "baseLine"] });
    try { alignCandle.setData(candles); } catch (e1) {}
    recordLifecycleStage("after_pushBase", { points: candles.length });
    lastPushBaseKey = pushBaseKey();
  }

  function opiTotalPoint(bar) {
    if (!bar || bar.opi_total == null) return { time: bar.time };
    var v = num(bar.opi_total);
    if (v == null) return { time: bar.time };
    return { time: bar.time, value: v, color: v >= 0 ? "#5b8def" : "#f0616d" };
  }

  function deltaPoint(bar) {
    if (!bar || bar.delta_usdt == null) return { time: bar.time };
    var v = num(bar.delta_usdt);
    if (v == null) return { time: bar.time };
    return { time: bar.time, value: v, color: v >= 0 ? "#3dcc91" : "#f0616d" };
  }

  function pricePoint(bar) {
    if (!bar || bar.price_change_pct == null) return { time: bar.time };
    var v = num(bar.price_change_pct);
    if (v == null) return { time: bar.time };
    return { time: bar.time, value: v };
  }

  function project(mapFn) {
    if (!baseTimes.length) {
      var loose = [];
      for (var i = 0; i < bars.length; i++) loose.push(mapFn(bars[i]));
      return loose;
    }
    var out = [];
    for (var k = 0; k < baseTimes.length; k++) {
      var t = baseTimes[k];
      var bar = byTime[t];
      if (bar) out.push(mapFn(bar));
      else out.push({ time: t });
    }
    return out;
  }

  function opiFormat() {
    return {
      type: "custom",
      formatter: function (p) {
        var v = Number(p);
        if (!Number.isFinite(v)) return "–";
        return (v >= 0 ? "" : "−") + Math.abs(v).toFixed(3) + " %/M";
      },
      minMove: 0.001,
    };
  }

  function pctFormat() {
    return {
      type: "custom",
      formatter: function (p) {
        var v = Number(p);
        if (!Number.isFinite(v)) return "–";
        return (v >= 0 ? "+" : "") + v.toFixed(3) + "%";
      },
      minMove: 0.001,
    };
  }

  function moneyFormat() {
    return {
      type: "custom",
      formatter: function (p) { return compact(p); },
      minMove: 0.01,
    };
  }

  function hasSeriesValues() {
    for (var i = 0; i < bars.length; i++) {
      var b = bars[i];
      if (!b) continue;
      if (b.opi_total != null || b.delta_usdt != null || b.price_change_pct != null) return true;
    }
    return false;
  }

  function paintSeries(mainSnap) {
    if (!ensureOpiChart()) return false;
    if (!sTotal || !sDelta || !sPrice) return false;
    var total;
    var delta;
    var price;
    if (baseTimes.length) {
      total = project(opiTotalPoint);
      delta = project(deltaPoint);
      price = project(pricePoint);
    } else if (bars.length) {
      total = [];
      delta = [];
      price = [];
      for (var i = 0; i < bars.length; i++) {
        total.push(opiTotalPoint(bars[i]));
        delta.push(deltaPoint(bars[i]));
        price.push(pricePoint(bars[i]));
      }
    } else {
      return false;
    }
    recordLifecycleStage("before_opi_setData", { points: total.length, series: ["total", "delta", "price"] });
    try { sTotal.setData(total); } catch (e1) {}
    recordLifecycleStage("after_opi_total_setData", { points: total.length });
    try { sDelta.setData(delta); } catch (e2) {}
    recordLifecycleStage("after_opi_delta_setData", { points: delta.length });
    try { sPrice.setData(price); } catch (e3) {}
    recordLifecycleStage("after_opi_price_setData", { points: price.length });
    if (mainSnap) reassertMainLogical("after_paintSeries_setData", mainSnap);
    return true;
  }

  function paintAll(opts) {
    opts = opts || {};
    if (!opiChart && !ensureOpiChart()) return;
    var snap = mainLogicalSnapshot();
    var barsOnly = !!opts.barsOnly;
    var willPush = baseTimes.length && !barsOnly && needsPushBase();
    if (!willPush) {
      recordLifecycleSkipped("before_pushBase", barsOnly ? "barsOnly" : "unchanged_pushBaseKey");
      recordLifecycleSkipped("after_pushBase", barsOnly ? "barsOnly" : "unchanged_pushBaseKey");
    }
    if (willPush) {
      pushBase();
      if (snap) reassertMainLogical("after_pushBase", snap);
    }
    if (!paintSeries(snap)) return;
    if (main && baseTimes.length && snap) {
      reassertMainLogical("paintAll_end", snap);
    }
  }

  function apply(payload) {
    currentDiagUpdateId = payload && payload._diagUpdateId != null ? Number(payload._diagUpdateId) : null;
    beginLifecycleCycle(payload && payload._diagTrigger ? payload._diagTrigger : "setOpiPane");
    recordLifecycleStage("apply_enter", { barCount: (payload && payload.bars) ? payload.bars.length : 0 });
    bars = (payload && payload.bars) || [];
    meta = {
      window_minutes: payload && payload.window_minutes,
      symbol: payload && payload.symbol,
      timeframe: payload && payload.timeframe,
    };
    byTime = {};
    bars.forEach(function (b) { byTime[Number(b.time)] = b; });
    var stack = $("opi-stack");
    var app = $("app");
    var visible = bars.length > 0 && hasSeriesValues();
    if (stack) stack.hidden = !visible;
    if (app) app.classList.toggle("opi-open", visible);
    var baseReadyForBarsOnly = paintedOnBase && !!lastPushBaseKey;
    var needsLayoutResize = visible && !paintedOnBase;
    if (visible) {
      ensureOpiChart();
      paintAll({ barsOnly: baseReadyForBarsOnly });
    } else if (bars.length && !hasSeriesValues()) {
      setError("OPI – keine messbaren Werte in diesem Zeitfenster");
    }
    paintedOnBase = baseTimes.length > 0 && visible;
    alignHosts();
    if (needsLayoutResize) {
      resize();
    } else if (visible && opiChart && main) {
      matchPlotWidth();
      alignHosts();
    }
    if (baseTimes.length) {
      reassertMainLogical("apply_end");
    }
    recordLifecycleStage("apply_exit", {});
  }

  function clear() {
    clearSyncedCrosshair();
    crosshairBridge = null;
    bars = [];
    byTime = {};
    meta = {};
    baseTimes = [];
    paintedOnBase = false;
    lastPushBaseKey = "";
    lastOpiLayoutKey = "";
    lastAppliedTsOptKey = "";
    var stack = $("opi-stack");
    if (stack) stack.hidden = true;
    var app = $("app");
    if (app) app.classList.remove("opi-open");
    var tip = $("opi-tooltip");
    if (tip) tip.hidden = true;
    if (sTotal) try { sTotal.setData([]); } catch (e1) {}
    if (sDelta) try { sDelta.setData([]); } catch (e2) {}
    if (sPrice) try { sPrice.setData([]); } catch (e3) {}
  }

  function alignHosts() {
    var chartEl = $("chart");
    var stack = $("opi-stack");
    if (!chartEl || !stack) return;
    var c = chartEl.getBoundingClientRect();
    var s = stack.getBoundingClientRect();
    var padLeft = Math.round(c.left - s.left);
    var padRight = Math.round(s.right - c.right);
    stack.style.paddingLeft = padLeft > 0 ? padLeft + "px" : "";
    stack.style.paddingRight = padRight > 0 ? padRight + "px" : "";
  }

  function layoutMetrics(extra) {
    var host = $("opi-combined");
    var stack = $("opi-stack");
    var row = {
      mainLogical: null,
      opiLogical: null,
      dFrom: null,
      dTo: null,
      hostClientW: host ? host.clientWidth : null,
      hostClientH: host ? host.clientHeight : null,
      plotW: 0,
      mainRsL: priceScaleWidth(main, "left"),
      mainRsR: priceScaleWidth(main, "right"),
      opiRsR: opiChart ? priceScaleWidth(opiChart, "right") : null,
      stackPadL: stack && stack.style ? stack.style.paddingLeft : "",
      stackPadR: stack && stack.style ? stack.style.paddingRight : "",
      barSpacing: null,
      rightOffset: null,
      mainRightOffset: null,
      mainBarSpacing: null,
    };
    if (main) {
      try { row.mainLogical = main.timeScale().getVisibleLogicalRange(); } catch (e0) { /* ignore */ }
      try {
        var mts = main.timeScale().options();
        if (mts) {
          row.mainRightOffset = mts.rightOffset;
          row.mainBarSpacing = mts.barSpacing;
        }
      } catch (eM) { /* ignore */ }
    }
    if (opiChart) {
      try { row.opiLogical = opiChart.timeScale().getVisibleLogicalRange(); } catch (e1) { /* ignore */ }
      try {
        var ts = opiChart.timeScale().options();
        row.barSpacing = ts && ts.barSpacing;
        row.rightOffset = ts && ts.rightOffset;
      } catch (e2) { /* ignore */ }
      var plot = timeScalePlotWidth(opiChart, host);
      row.plotW = plot.width;
    }
    if (row.mainLogical && row.opiLogical && row.mainLogical.from != null && row.opiLogical.from != null) {
      row.dFrom = +(Number(row.opiLogical.from) - Number(row.mainLogical.from)).toFixed(4);
    }
    if (row.mainLogical && row.opiLogical && row.mainLogical.to != null && row.opiLogical.to != null) {
      row.dTo = +(Number(row.opiLogical.to) - Number(row.mainLogical.to)).toFixed(4);
    }
    if (extra) {
      Object.keys(extra).forEach(function (k) { row[k] = extra[k]; });
    }
    return row;
  }

  function layoutResizeKey(host) {
    if (!host || !main) return "";
    var chartEl = $("chart");
    var stack = $("opi-stack");
    var c = chartEl ? chartEl.getBoundingClientRect() : null;
    var s = stack ? stack.getBoundingClientRect() : null;
    var padL = c && s ? Math.round(c.left - s.left) : 0;
    var padR = c && s ? Math.round(s.right - c.right) : 0;
    return [
      host.clientWidth,
      host.clientHeight,
      priceScaleWidth(main, "left"),
      priceScaleWidth(main, "right"),
      padL,
      padR,
    ].join("|");
  }

  function traceLayoutEvent(tag, extra) {
    if (!(root.OpiE2eRecord && OpiE2eRecord.isActive && OpiE2eRecord.isActive())) return;
    OpiE2eRecord.note("layout", Object.assign({ tag: tag, perfMs: performance.now() }, layoutMetrics(extra || {})));
  }

  function scheduleLayoutTraceFollowup(tag, token) {
    if (!(root.OpiE2eRecord && OpiE2eRecord.isActive && OpiE2eRecord.isActive())) return;
    var t0 = performance.now();
    requestAnimationFrame(function () {
      if (token !== layoutTraceGen) return;
      traceLayoutEvent(tag + "_raf1", { dtMs: +(performance.now() - t0).toFixed(2) });
      requestAnimationFrame(function () {
        if (token !== layoutTraceGen) return;
        traceLayoutEvent(tag + "_raf2", { dtMs: +(performance.now() - t0).toFixed(2) });
      });
    });
  }

  function mainLogicalRange(explicit) {
    if (explicit && explicit.from != null && explicit.to != null) {
      return { from: Number(explicit.from), to: Number(explicit.to) };
    }
    if (!main) return null;
    try {
      var r = main.timeScale().getVisibleLogicalRange();
      if (!r || r.from == null || r.to == null) return null;
      return { from: Number(r.from), to: Number(r.to) };
    } catch (err) {
      return null;
    }
  }

  function sameLogical(a, b) {
    return !!(a && b && Math.abs(Number(a.from) - Number(b.from)) < 0.05
      && Math.abs(Number(a.to) - Number(b.to)) < 0.05);
  }

  /** Map Main visible UTC to fractional index on baseTimes (same grid as OPI align). */
  function logicalIndexForUtc(unix) {
    var t = Number(unix);
    if (!Number.isFinite(t) || baseTimes.length < 1) return null;
    var hi = baseTimes.length - 1;
    if (t <= baseTimes[0]) {
      var step0 = baseTimes.length > 1 ? baseTimes[1] - baseTimes[0] : 60;
      if (!(step0 > 0)) step0 = 60;
      return (t - baseTimes[0]) / step0;
    }
    if (t >= baseTimes[hi]) {
      var stepN = hi > 0 ? baseTimes[hi] - baseTimes[hi - 1] : 60;
      if (!(stepN > 0)) stepN = 60;
      return hi + (t - baseTimes[hi]) / stepN;
    }
    var lo = 0;
    var end = hi;
    while (lo + 1 < end) {
      var mid = (lo + end) >> 1;
      if (baseTimes[mid] <= t) lo = mid;
      else end = mid;
    }
    var t0 = baseTimes[lo];
    var t1 = baseTimes[end];
    var span = t1 - t0;
    var frac = span > 0 ? (t - t0) / span : 0;
    return lo + Math.max(0, Math.min(1, frac));
  }

  function resolveOpiVisibleLogicalRange() {
    var utcRange = null;
    var mainLogical = null;
    try { utcRange = main.timeScale().getVisibleRange(); } catch (errU) { utcRange = null; }
    try { mainLogical = main.timeScale().getVisibleLogicalRange(); } catch (errL) { mainLogical = null; }
    if (!utcRange || utcRange.from == null || utcRange.to == null) {
      if (!mainLogical || mainLogical.from == null || mainLogical.to == null) return null;
      return { from: Number(mainLogical.from), to: Number(mainLogical.to) };
    }
    var lf = logicalIndexForUtc(Number(utcRange.from));
    var lt = logicalIndexForUtc(Number(utcRange.to));
    if (lf == null || lt == null) return null;
    if (!(lt > lf) && mainLogical && mainLogical.from != null && mainLogical.to != null) {
      var span = Number(mainLogical.to) - Number(mainLogical.from);
      if (span > 0) lt = lf + span;
    }
    /* P3Q: keep full visible width (incl. empty right margin) — do not clip at last data bar. */
    if (mainLogical && mainLogical.from != null && mainLogical.to != null) {
      var mainSpan = Number(mainLogical.to) - Number(mainLogical.from);
      if (mainSpan > 0) {
        if (sharedIndexBase()) {
          return {
            from: Number(mainLogical.from),
            to: Number(mainLogical.to),
          };
        }
        if (lf != null && Number.isFinite(lf)) {
          lt = Number(lf) + mainSpan;
        }
      }
    }
    if (!(lt > lf)) return null;
    return { from: lf, to: lt };
  }

  function opiTargetLogicalRange(mainRange) {
    var copied = mainLogicalRange(mainRange);
    var resolved = resolveOpiVisibleLogicalRange();
    var picked = null;
    var pickMode = null;
    if (copied && copied.from != null && copied.to != null && sharedIndexBase()) {
      picked = copied;
      pickMode = "main_logical_1_1";
    } else if (resolved && resolved.from != null && resolved.to != null) {
      picked = resolved;
      pickMode = "utc_resolved";
    } else if (copied && copied.from != null && copied.to != null) {
      picked = copied;
      pickMode = "main_logical_fallback";
    }
    if (root.OpiE2eRecord && OpiE2eRecord.isActive && OpiE2eRecord.isActive()) {
      var utc = null;
      try { utc = main && main.timeScale().getVisibleRange(); } catch (eU) { utc = null; }
      OpiE2eRecord.note("sync_target", {
        reason: "opiTargetLogicalRange",
        pickMode: pickMode,
        sharedIndexBase: sharedIndexBase(),
        copied: copied,
        resolved: resolved,
        picked: picked,
        mainUtc: utc,
      });
    }
    return picked;
  }

  function setOpiVisibleLogicalRange(logRange, traceReason, matchScaleNow, phase) {
    if (!logRange) return;
    var cur = null;
    try { cur = opiChart.timeScale().getVisibleLogicalRange(); } catch (e1) { cur = null; }
    if (sameLogical(cur, logRange)) return;
    if (viewportDiagActive() && window.OpiRuntimeTrace && OpiRuntimeTrace._push) {
      OpiRuntimeTrace._push("opiSetVisibleLogicalRange", {
        reason: traceReason || null,
        phase: phase || null,
        matchScaleNow: !!matchScaleNow,
        syncGen: syncGen,
        beforeFrom: cur && cur.from != null ? Number(cur.from) : null,
        beforeTo: cur && cur.to != null ? Number(cur.to) : null,
        afterFrom: logRange.from,
        afterTo: logRange.to,
      });
    }
    try {
      opiChart.timeScale().setVisibleLogicalRange({
        from: logRange.from,
        to: logRange.to,
      });
      if (root.OpiE2eRecord && OpiE2eRecord.isActive && OpiE2eRecord.isActive()) {
        var after = null;
        try { after = opiChart.timeScale().getVisibleLogicalRange(); } catch (e3) { after = null; }
        OpiE2eRecord.note("setVisibleLogicalRange", {
          reason: traceReason || null,
          phase: phase || null,
          matchScaleNow: !!matchScaleNow,
          syncGen: syncGen,
          before: cur,
          target: logRange,
          after: after,
        });
      }
    } catch (e2) { /* no points yet */ }
  }

  /** Main UTC → OPI grid; avoid redundant timeScale applyOptions (P3P). */
  function applySync(matchScaleNow, mainRange, traceReason) {
    if (!main || !opiChart || lock || baseTimes.length < 1) return;
    var logRange = opiTargetLogicalRange(mainRange);
    if (!logRange) return;
    var tsOpt = mainTimeScaleOpts();
    lock = true;
    try {
      if (matchScaleNow) matchPlotWidth();
      else alignHosts();
      /* Range before timeScale opts — reapplied once if opts changed (LWC may reset viewport). */
      setOpiVisibleLogicalRange(logRange, traceReason, matchScaleNow, "pre_tsOpt");
      var tsChanged = applyTimeScaleOpts(tsOpt);
      if (tsChanged) {
        setOpiVisibleLogicalRange(logRange, traceReason, matchScaleNow, "post_tsOpt");
      }
    } finally {
      lock = false;
    }
  }

  function combinedLogical() {
    if (!opiChart) return null;
    try { return opiChart.timeScale().getVisibleLogicalRange(); } catch (err) { return null; }
  }

  function debugLogicalRanges() {
    if (!main) return null;
    var out = { charts: {}, baseTimesLen: baseTimes.length, variant: "p1s_single_chart" };
    try { out.main = main.timeScale().getVisibleLogicalRange(); } catch (err) { out.main = null; }
    try {
      var opt = main.timeScale().options();
      out.mainBarSpacing = opt && opt.barSpacing;
      out.mainRightOffset = opt && opt.rightOffset;
    } catch (err2) { /* ignore */ }
    var combined = combinedLogical();
    var tsOpt = null;
    if (opiChart) {
      try { tsOpt = opiChart.timeScale().options(); } catch (e3) { tsOpt = null; }
    }
    ["opi_total", "delta", "price"].forEach(function (key) {
      out.charts[key] = combined;
      if (tsOpt) {
        out.charts[key + "_barSpacing"] = tsOpt.barSpacing;
        out.charts[key + "_rightOffset"] = tsOpt.rightOffset;
      }
    });
    return out;
  }

  function resize(caller) {
    if (!opiChart || !main) return;
    var host = $("opi-combined");
    if (!host || host.clientWidth < 16 || host.clientHeight < 16) return;
    var layoutKey = layoutResizeKey(host);
    var snap = mainLogicalSnapshot();
    traceLayoutEvent("resize_enter", {
      caller: caller || null,
      layoutKey: layoutKey,
      prevLayoutKey: lastOpiLayoutKey,
    });
    /* P3O: Main #chart ResizeObserver fires often with unchanged OPI layout — skip LWC applyOptions. */
    if (layoutKey && layoutKey === lastOpiLayoutKey) {
      traceLayoutEvent("resize_skip_unchanged", { caller: caller || null, layoutKey: layoutKey });
      return;
    }
    lastOpiLayoutKey = layoutKey;
    var w = Math.max(host.clientWidth, 16);
    var h = Math.max(host.clientHeight, 16);
    layoutTraceGen += 1;
    var traceTok = layoutTraceGen;
    try {
      opiChart.applyOptions({ width: w, height: h });
    } catch (err) { /* ignore */ }
    matchPlotWidth("resize");
    applySync(false, snap || mainLogicalRange(null), "resize");
    scheduleLayoutTraceFollowup("resize", traceTok);
  }

  function sync(range, immediate) {
    if (!main || baseTimes.length < 1) return;
    syncGen += 1;
    var myGen = syncGen;
    if (immediate) {
      if (rafId) {
        cancelAnimationFrame(rafId);
        rafId = 0;
      }
      /* P3N: 1:1 logical only — matchPlotWidth only in resize() (stoch pattern). */
      applySync(false, mainLogicalRange(range), "subscribe_sync");
      return;
    }
    if (rafId) cancelAnimationFrame(rafId);
    rafId = requestAnimationFrame(function () {
      rafId = 0;
      if (myGen !== syncGen) return;
      applySync(false, mainLogicalRange(range), "subscribe_sync_raf");
    });
  }

  function setTimeBase(times) {
    var next = [];
    var last = null;
    (times || []).forEach(function (t) {
      var n = Number(t);
      if (!Number.isFinite(n) || (last != null && n <= last)) return;
      last = n;
      next.push(n);
    });
    if (
      next.length === baseTimes.length
      && next.length
      && next[0] === baseTimes[0]
      && next[next.length - 1] === baseTimes[baseTimes.length - 1]
      && paintedOnBase
    ) {
      if (bars.length) {
        ensureOpiChart();
        paintAll({ barsOnly: true });
      }
      sync(null, true);
      return;
    }
    lastPushBaseKey = "";
    baseTimes = next;
    if (bars.length) {
      ensureOpiChart();
      paintAll();
      paintedOnBase = baseTimes.length > 0;
    } else if (opiChart) {
      pushBase();
    }
    sync(null, true);
  }

  function setError(text) {
    var el = $("opi-error");
    if (!text) {
      if (el) {
        el.hidden = true;
        el.textContent = "";
      }
      return;
    }
    if (el) {
      el.hidden = false;
      el.textContent = text;
    }
    var stack = $("opi-stack");
    if (stack) stack.hidden = false;
  }

  function hoverTime(unix) {
    var tip = $("opi-tooltip");
    if (!tip) return;
    var bar = unix == null ? null : byTime[unix];
    if (!bar) {
      tip.hidden = true;
      return;
    }
    var lines = [];
    lines.push("OPI · " + (meta.window_minutes || bar.window_minutes || "?") + " min");
    if (bar.status) lines.push(bar.status + (bar.quality && bar.quality !== "ok" ? " · " + bar.quality : ""));
    if (bar.buy_usdt != null) {
      lines.push("Buy " + compact(bar.buy_usdt) + " USDT");
      lines.push("Sell " + compact(bar.sell_usdt) + " USDT");
      lines.push("Total " + compact(bar.total_usdt) + " USDT");
      lines.push("Delta " + compact(bar.delta_usdt) + " USDT");
    }
    if (bar.price_change_pct != null) {
      lines.push("Preisänderung " + bar.price_change_pct + " %");
    }
    if (bar.opi_total != null) lines.push("OPI Total " + bar.opi_total + " % pro 1M USDT Vol");
    if (bar.opi_delta != null) lines.push("OPI Delta " + bar.opi_delta + " % pro 1M USDT |Delta|");
    tip.textContent = lines.join("\n");
    tip.hidden = false;
  }

  function attach(chart) {
    main = chart;
  }

  function debugIndexParity() {
    var out = {
      ok: false,
      syncMode: "stoch_1_1_logical",
      baseTimesLen: baseTimes.length,
      alignDataLen: alignCandle && alignCandle.data ? alignCandle.data().length : null,
      firstBaseUtc: baseTimes.length ? baseTimes[0] : null,
      lastBaseUtc: baseTimes.length ? baseTimes[baseTimes.length - 1] : null,
    };
    if (baseTimes.length < 1) return out;
    var dataLen = null;
    try {
      if (alignCandle && typeof alignCandle.data === "function") {
        dataLen = alignCandle.data().length;
      }
    } catch (e0) { dataLen = null; }
    out.alignDataLen = dataLen;
    out.ok = dataLen == null || dataLen === baseTimes.length;
    if (main) {
      try {
        var ml = main.timeScale().getVisibleLogicalRange();
        var ol = opiChart ? opiChart.timeScale().getVisibleLogicalRange() : null;
        out.mainLogical = ml;
        out.opiLogical = ol;
        out.logicalParity = !!(ml && ol && sameLogical(ml, ol));
      } catch (e1) {
        out.logicalParity = false;
      }
    }
    return out;
  }

  function debugTraceState() {
    var times = [];
    for (var i = 0; i < bars.length; i++) {
      var t = Number(bars[i] && bars[i].time);
      if (Number.isFinite(t)) times.push(t);
    }
    times.sort(function (a, b) { return a - b; });
    var stack = $("opi-stack");
  return {
      barCount: bars.length,
      firstBar: times.length ? times[0] : null,
      lastBar: times.length ? times[times.length - 1] : null,
      baseTimesLen: baseTimes.length,
      stackHidden: !!(stack && stack.hidden),
      hasError: !!($("opi-error") && !$("opi-error").hidden),
    };
  }

  function pickAlignmentAnchorTimes(maxN) {
    var n = maxN == null ? 3 : Math.max(1, Number(maxN) || 3);
    var utc = null;
    try { utc = main && main.timeScale().getVisibleRange(); } catch (e0) { utc = null; }
    if (!utc || utc.from == null || utc.to == null) return [];
    var from = Number(utc.from);
    var to = Number(utc.to);
    var opiTimes = [];
    for (var i = 0; i < bars.length; i++) {
      var b = bars[i];
      if (!b) continue;
      var t = Number(b.time);
      if (!Number.isFinite(t) || t < from || t > to) continue;
      if (b.opi_total == null && b.delta_usdt == null && b.price_change_pct == null) continue;
      opiTimes.push(t);
    }
    opiTimes.sort(function (a, b) { return a - b; });
    if (!opiTimes.length && baseTimes.length) {
      for (var k = 0; k < baseTimes.length; k++) {
        var bt = baseTimes[k];
        if (bt >= from && bt <= to) opiTimes.push(bt);
      }
    }
    if (!opiTimes.length) return [];
    if (opiTimes.length <= n) return opiTimes.slice();
    var picked = [opiTimes[0]];
    picked.push(opiTimes[Math.floor(opiTimes.length / 2)]);
    if (n >= 3) picked.push(opiTimes[opiTimes.length - 1]);
    return picked.slice(0, n);
  }

  function debugPlotAlignment(anchorTimes) {
    if (!main || !opiChart) return { ok: false, err: "no_charts", anchors: [] };
    var mainWr = $("chart");
    var opiWr = $("opi-combined");
    var mRect = mainWr ? mainWr.getBoundingClientRect() : null;
    var oRect = opiWr ? opiWr.getBoundingClientRect() : null;
    var mainRs = null;
    var opiRs = null;
    try { mainRs = main.priceScale("right").width(); } catch (e1) {}
    try { opiRs = opiChart.priceScale("right").width(); } catch (e2) {}
    var times = anchorTimes && anchorTimes.length ? anchorTimes : pickAlignmentAnchorTimes(3);
    var mainPlot = timeScalePlotWidth(main, mainWr);
    var opiPlot = timeScalePlotWidth(opiChart, opiWr);
    var mainPlotW = mainPlot.width;
    var opiPlotW = opiPlot.width;
    var out = {
      mainPlotRightWidth: mainRs,
      opiPlotRightWidth: opiRs,
      mainLeftScaleWidth: priceScaleWidth(main, "left"),
      opiLeftScaleWidth: priceScaleWidth(opiChart, "left"),
      mainTimeScalePlotWidth: mainPlotW,
      mainTimeScalePlotWidthSource: mainPlot.source,
      opiTimeScalePlotWidth: opiPlotW,
      opiTimeScalePlotWidthSource: opiPlot.source,
      note: opiPlot.source === "hidden_or_unready"
        ? "OPI timeScale.visible=false — api width() is 0; estimated via clientWidth−scales"
        : null,
      timeScaleWidthDelta: mainPlotW && opiPlotW ? mainPlotW - opiPlotW : null,
      timeScaleWidthRatio: mainPlotW && opiPlotW ? +(opiPlotW / mainPlotW).toFixed(6) : null,
      devicePixelRatio: typeof window !== "undefined" && window.devicePixelRatio != null
        ? window.devicePixelRatio
        : null,
      mainChartClientWidth: mainWr ? mainWr.clientWidth : null,
      opiChartClientWidth: opiWr ? opiWr.clientWidth : null,
      stackPaddingLeft: ($("opi-stack") && $("opi-stack").style.paddingLeft) || "",
      stackPaddingRight: ($("opi-stack") && $("opi-stack").style.paddingRight) || "",
      anchorSource: anchorTimes && anchorTimes.length ? "caller" : "auto",
      pickedAnchors: times.slice(),
      anchors: [],
      noAnchorsReason: null,
    };
    if (!times.length) {
      out.noAnchorsReason = bars.length
        ? "no_bar_times_in_visible_utc"
        : "no_opi_bars";
      out.ok = false;
      return out;
    }
    times.forEach(function (t) {
      var unix = Number(t);
      var mx = null;
      var ox = null;
      try { mx = main.timeScale().timeToCoordinate(unix); } catch (e3) {}
      try { ox = opiChart.timeScale().timeToCoordinate(unix); } catch (e4) {}
      var dxLocal = mx != null && ox != null ? +(ox - mx).toFixed(3) : null;
      var dxScreen = null;
      if (mx != null && ox != null && mRect && oRect) {
        dxScreen = +((oRect.left + ox) - (mRect.left + mx)).toFixed(3);
      }
      var dx = dxScreen != null ? dxScreen : dxLocal;
      var measurable = dx != null;
      out.anchors.push({
        time: unix,
        mainX: mx,
        opiX: ox,
        deltaXLocal: dxLocal,
        deltaX: dx,
        ok: measurable && Math.abs(dx) <= 2,
      });
    });
    out.ok = out.anchors.length > 0 && out.anchors.every(function (a) { return a.ok; });
    if (out.anchors.length >= 2) {
      var first = out.anchors[0];
      var last = out.anchors[out.anchors.length - 1];
      if (first.mainX != null && last.mainX != null && first.opiX != null && last.opiX != null) {
        out.mainAnchorSpanLocal = +(last.mainX - first.mainX).toFixed(3);
        out.opiAnchorSpanLocal = +(last.opiX - first.opiX).toFixed(3);
        out.anchorSpanRatio = out.mainAnchorSpanLocal
          ? +(out.opiAnchorSpanLocal / out.mainAnchorSpanLocal).toFixed(6)
          : null;
      }
      if (mRect && oRect) {
        var m0 = mRect.left + first.mainX;
        var m1 = mRect.left + last.mainX;
        var o0 = oRect.left + first.opiX;
        var o1 = oRect.left + last.opiX;
        out.mainAnchorSpanScreen = +(m1 - m0).toFixed(3);
        out.opiAnchorSpanScreen = +(o1 - o0).toFixed(3);
        out.anchorSpanScreenRatio = out.mainAnchorSpanScreen
          ? +(out.opiAnchorSpanScreen / out.mainAnchorSpanScreen).toFixed(6)
          : null;
        out.deltaSlopeScreen = out.mainAnchorSpanScreen
          ? +((last.deltaX - first.deltaX) / out.mainAnchorSpanScreen).toFixed(6)
          : null;
      }
    }
    return out;
  }

  function barHasValue(bar) {
    if (!bar) return false;
    return bar.opi_total != null || bar.delta_usdt != null || bar.price_change_pct != null;
  }

  var TIME_AXIS_DESYNC_TOL = 0.08;

  /** P3Q — time-axis parity independent of OPI bar coverage (FULL can still desync). */
  function measureTimeAxisDesync() {
    var out = {
      ok: true,
      desync: false,
      code: null,
      sharedIndexBase: sharedIndexBase(),
      mainLogical: null,
      opiLogical: null,
      dFrom: null,
      dTo: null,
      mainSpan: null,
      opiSpan: null,
      dSpan: null,
      utcFrom: null,
      utcTo: null,
      anchors: null,
      anchorOk: null,
    };
    if (!main || !opiChart || baseTimes.length < 1) {
      out.ok = false;
      out.code = "OPI_TIME_AXIS_DESYNC";
      out.desync = true;
      return out;
    }
    try {
      var utc = main.timeScale().getVisibleRange();
      out.utcFrom = utc && utc.from != null ? Number(utc.from) : null;
      out.utcTo = utc && utc.to != null ? Number(utc.to) : null;
      out.mainLogical = main.timeScale().getVisibleLogicalRange();
      out.opiLogical = opiChart.timeScale().getVisibleLogicalRange();
    } catch (e0) {
      out.ok = false;
      out.desync = true;
      out.code = "OPI_TIME_AXIS_DESYNC";
      return out;
    }
    if (!out.mainLogical || !out.opiLogical
      || out.mainLogical.from == null || out.mainLogical.to == null
      || out.opiLogical.from == null || out.opiLogical.to == null) {
      return out;
    }
    var mf = Number(out.mainLogical.from);
    var mt = Number(out.mainLogical.to);
    var of = Number(out.opiLogical.from);
    var ot = Number(out.opiLogical.to);
    out.dFrom = +(of - mf).toFixed(4);
    out.dTo = +(ot - mt).toFixed(4);
    out.mainSpan = +(mt - mf).toFixed(4);
    out.opiSpan = +(ot - of).toFixed(4);
    out.dSpan = +(out.opiSpan - out.mainSpan).toFixed(4);
    if (out.sharedIndexBase) {
      out.desync = Math.abs(out.dFrom) > TIME_AXIS_DESYNC_TOL
        || Math.abs(out.dTo) > TIME_AXIS_DESYNC_TOL
        || Math.abs(out.dSpan) > TIME_AXIS_DESYNC_TOL;
    } else {
      var align = debugPlotAlignment(pickAlignmentAnchorTimes(3));
      out.anchors = align && align.anchors ? align.anchors : null;
      out.anchorOk = !!(align && align.ok);
      out.desync = !out.anchorOk;
    }
    if (out.desync) {
      out.ok = false;
      out.code = "OPI_TIME_AXIS_DESYNC";
    }
    return out;
  }

  /** Read-only visibility profile for P3L E2E (no sync / no setData). */
  function measureVisibleOpiCoverage() {
    var stack = $("opi-stack");
    var hostEl = $("opi-combined");
    var out = {
      ok: false,
      classification: "UNKNOWN",
      mismatch: false,
      mismatchCode: null,
      utcFrom: null,
      utcTo: null,
      mainLogical: null,
      opiLogical: null,
      dFrom: null,
      plotWidth: 0,
      stackHidden: !!(stack && stack.hidden),
      canvasCount: 0,
      barsTotal: bars.length,
      barsWithValue: 0,
      barsInVisibleUtc: 0,
      barsWithValueInUtc: 0,
      coordsInPlot: 0,
      coordsLeftOnly: 0,
      expectedCoverageRatio: null,
      visibleCoverageRatio: null,
    };
    if (!main || !opiChart || out.stackHidden) {
      out.classification = "HIDDEN";
      return out;
    }
    try {
      var utc = main.timeScale().getVisibleRange();
      out.utcFrom = utc && utc.from != null ? Number(utc.from) : null;
      out.utcTo = utc && utc.to != null ? Number(utc.to) : null;
      out.mainLogical = main.timeScale().getVisibleLogicalRange();
      out.opiLogical = opiChart.timeScale().getVisibleLogicalRange();
      if (out.mainLogical && out.opiLogical) {
        out.dFrom = +(Number(out.opiLogical.from) - Number(out.mainLogical.from)).toFixed(4);
      }
    } catch (e0) { /* ignore */ }
    var plot = timeScalePlotWidth(opiChart, hostEl);
    out.plotWidth = plot.width || 0;
    if (hostEl) out.canvasCount = hostEl.querySelectorAll("canvas").length;
    var from = out.utcFrom;
    var to = out.utcTo;
    for (var i = 0; i < bars.length; i++) {
      var b = bars[i];
      if (!b) continue;
      if (barHasValue(b)) out.barsWithValue += 1;
      var t = Number(b.time);
      if (!Number.isFinite(t) || from == null || to == null || t < from || t > to) continue;
      out.barsInVisibleUtc += 1;
      if (!barHasValue(b)) continue;
      out.barsWithValueInUtc += 1;
      var x = null;
      try { x = opiChart.timeScale().timeToCoordinate(t); } catch (e1) { x = null; }
      if (x == null || !Number.isFinite(Number(x))) continue;
      var nx = Number(x);
      if (nx >= 0 && nx <= out.plotWidth) out.coordsInPlot += 1;
      else if (nx >= 0 && nx < out.plotWidth * 0.35) out.coordsLeftOnly += 1;
    }
    if (out.barsWithValueInUtc > 0) {
      out.expectedCoverageRatio = +(out.barsWithValueInUtc / Math.max(1, out.barsInVisibleUtc)).toFixed(4);
      out.visibleCoverageRatio = +(out.coordsInPlot / out.barsWithValueInUtc).toFixed(4);
    }
    if (out.barsWithValue < 1) {
      out.classification = "EMPTY";
      out.ok = !out.stackHidden;
    } else if (out.coordsInPlot < 2) {
      out.classification = "EMPTY_VISIBLE";
      out.mismatch = out.barsWithValueInUtc >= 8;
      out.mismatchCode = out.mismatch ? "OPI_VISIBILITY_MISMATCH" : null;
    } else if (out.visibleCoverageRatio != null && out.visibleCoverageRatio < 0.35
      && out.barsWithValueInUtc >= 12) {
      out.classification = "PARTIAL";
      out.mismatch = true;
      out.mismatchCode = "OPI_VISIBILITY_MISMATCH";
    } else {
      out.classification = "FULL";
      out.ok = true;
    }
    return out;
  }

  function captureOpiCanvasThumb() {
    var hostEl = $("opi-combined");
    if (!hostEl) return null;
    try {
      var c = hostEl.querySelector("canvas");
      if (!c || !c.toDataURL) return null;
      var data = c.toDataURL("image/jpeg", 0.55);
      if (data && data.length > 320000) return { truncated: true, len: data.length };
      return { mime: "image/jpeg", data: data };
    } catch (e) {
      return { err: String(e) };
    }
  }

  root.OpiPane = {
    attach: attach,
    apply: apply,
    clear: clear,
    resize: resize,
    sync: sync,
    setTimeBase: setTimeBase,
    setError: setError,
    hoverTime: hoverTime,
    setSyncedCrosshair: setSyncedCrosshair,
    clearSyncedCrosshair: clearSyncedCrosshair,
    setCrosshairBridge: setCrosshairBridge,
    debugLogicalRanges: debugLogicalRanges,
    debugPlotAlignment: debugPlotAlignment,
    pickAlignmentAnchorTimes: pickAlignmentAnchorTimes,
    debugIndexParity: debugIndexParity,
    debugTraceState: debugTraceState,
    reassertMainLogical: reassertMainLogical,
    reassertAfterLayout: reassertAfterLayout,
    traceViewport: traceViewport,
    debugUpdateLifecycle: debugUpdateLifecycle,
    recordLifecycleStage: recordLifecycleStage,
    measureVisibleOpiCoverage: measureVisibleOpiCoverage,
    measureTimeAxisDesync: measureTimeAxisDesync,
    captureOpiCanvasThumb: captureOpiCanvasThumb,
    layoutMetrics: layoutMetrics,
    traceLayoutEvent: traceLayoutEvent,
  };
  root.OpiViewportDiag = { enabled: false };
})(typeof window !== "undefined" ? window : globalThis);