/* Chart frontend: rendering and interaction only. No TF bucket math. */
(function () {
  "use strict";

  const COLORS = {
    bg: "#131722",
    text: "#d1d4dc",
    grid: "#1e222d",
    border: "#2a2e39",
    up: "#3dcc91",
    down: "#f0616d",
    ema20: "#5b8def",
    ema35: "#c9a227",
    selected: "#8b96a8",
    muted: "#8b93a7",
  };

  const DEFAULT_VISIBLE_BARS = 120;
  const DEFAULT_RIGHT_OFFSET = 6;
  const DEFAULT_BAR_SPACING = 7;
  const PRICE_SCALE_MARGINS = { top: 0.08, bottom: 0.08 };
  const OSC_SCALE_MARGINS = { top: 0.04, bottom: 0.04 };

  let chart = null;
  let candleSeries = null;
  let emaOverlaySeries = new Map();
  let lastEmaOverlays = { series: [] };
  let lldEmaFastSeries = null;
  let lldEmaSlowSeries = null;
  let lastPayload = null;
  /** Keep the live tip in view unless the user scrolls away from the right edge. */
  let followLive = true;
  let replayViewLock = null;
  let livePriceLine = null;
  let lastCrosshairTime = null;
  let lastSelectedUnix = null;
  let suppressUntilTime = null;
  let candleByTime = new Map();
  let lastSetDataError = null;
  let lastCandleSetCount = 0;
  let lastAppliedSize = { w: 0, h: 0 };
  const overlayRegistry = new Map();
  /** EZM / research point markers — canvas layer (DOM overlays freeze with 1k+ markers). */
  const researchMarkers = new Map();
  /** Public-trade bubbles (causal buckets) — own canvas above candles. */
  const tradeBubbles = new Map();
  let tradeBubbleAlpha = 0.55;
  let tradeBubbleHoverId = null;
  let tradeBubblePaintTimer = null;
  let tradeBubblePaintQueued = false;
  let tradeBubblePaintRaf = null;
  let researchPaintTimer = null;
  let researchPaintQueued = false;
  let researchPaintRaf = null;
  let interactionMode = "select";
  let toolClickCount = 0;
  const ONE_POINT_TOOLS = { hline: true, vline: true };
  const TWO_POINT_TOOLS = {
    trend: true,
    rectangle: true,
    circle: true,
    arrow: true,
    measure: true,
    long_position: true,
    short_position: true,
  };
  let previewAnchor = null;
  let dragState = null;
  let suppressNextClick = false;
  let overlayLayoutCount = 0;
  let scaleWatchRaf = null;
  let scaleWatchUntil = 0;
  let pointerScaleWatch = false;
  let lastPriceYSample = null;
  let lastPriceYPrice = null;
  let oscChart = null;
  let oscTimeBase = null;
  let oscSeriesById = new Map();
  let oscPriceLines = [];
  let lastLowerPayload = null;
  let lowerVisible = false;
  let timeSyncLock = false;
  let programmaticNavDepth = 0;
  const TIME_SYNC_MAX_DEPTH = 6;
  let localXhairLock = false;
  let oscValueByTime = new Map();
  let lastAppliedOscSize = { w: 0, h: 0 };
  let resetViewCount = 0;
  let lastResetResult = null;
  let shiftMeasure = null;
  let shiftMeasureRaf = null;
  let shiftMeasurePendingXy = null;
  let shiftMeasureCaptureEl = null;
  let shiftMeasureHandlersBound = false;
  let hostShift = false;
  let lastCursorPrice = null;
  let lastCursorXy = null;
  let lastPriceFormat = { type: "price", precision: 2, minMove: 0.01 };
  let vpPayload = null;
  let vpSettings = {
    enabled: false,
    display: "buy_sell",
    poc: true,
    value_area: true,
    width: "normal",
  };
  let vpHoverIndex = -1;
  let obpPayload = null;
  let obpSettings = {
    enabled: false,
    width: "normal",
  };
  let obpHoverIndex = -1;
  let oblPayload = null;
  let oblSettings = {
    enabled: false,
    mode: "aggregated",
    scale: "sqrt",
    width_px: 140,
  };
  let oblHitBars = [];
  let oblHover = null;
  let oblListenersBound = false;

  function beginProgrammaticNav() {
    programmaticNavDepth += 1;
  }

  function endProgrammaticNav() {
    programmaticNavDepth = Math.max(0, programmaticNavDepth - 1);
  }

  function runProgrammaticNav(fn) {
    beginProgrammaticNav();
    try {
      return fn();
    } finally {
      endProgrammaticNav();
    }
  }

  function $(id) {
    return document.getElementById(id);
  }

  function chartSize() {
    const el = $("chart");
    return {
      w: el ? el.clientWidth : 0,
      h: el ? el.clientHeight : 0,
    };
  }

  function priceAxisWidth() {
    try {
      if (chart && typeof chart.priceScale === "function") {
        const ps = chart.priceScale("right");
        if (ps && typeof ps.width === "function") {
          const w = Number(ps.width());
          if (w > 0) return w;
        }
      }
    } catch (e) {}
    const host = $("chart");
    if (host) {
      const cells = host.querySelectorAll("td");
      if (cells.length) {
        const last = cells[cells.length - 1];
        const w = last.clientWidth;
        if (w > 8 && w < host.clientWidth * 0.45) return w;
      }
    }
    return 64;
  }

  function plotRightX() {
    const size = chartSize();
    try {
      if (chart && chart.timeScale) {
        const tw = Number(chart.timeScale().width());
        if (tw > 16 && tw < size.w) return tw;
      }
    } catch (e) {}
    return Math.max(0, size.w - priceAxisWidth());
  }

  function timeAxisHeight() {
    try {
      const host = $("chart");
      if (host) {
        const rows = host.querySelectorAll("table tr");
        if (rows.length >= 2) {
          const h = rows[rows.length - 1].clientHeight;
          if (h > 4 && h < host.clientHeight * 0.45) return h;
        }
      }
    } catch (e) {}
    return 28;
  }

  function plotBottomY() {
    return Math.max(0, chartSize().h - timeAxisHeight());
  }

  function clipOverlayLayerToPlot() {
    const layer = overlayLayer();
    if (!layer) return;
    layer.style.right = Math.max(0, priceAxisWidth()) + "px";
    layer.style.bottom = Math.max(0, timeAxisHeight()) + "px";
  }

  function toOhlc(candles) {
    return candles.map(function (c) {
      return {
        time: Number(c.time),
        open: Number(c.open),
        high: Number(c.high),
        low: Number(c.low),
        close: Number(c.close),
      };
    }).filter(function (c) {
      return Number.isFinite(c.time) && Number.isFinite(c.open) && Number.isFinite(c.close);
    });
  }

  function inferPriceFormat(candles) {
    let maxAbs = 0;
    const list = candles || [];
    for (let i = 0; i < list.length; i++) {
      const c = list[i];
      maxAbs = Math.max(
        maxAbs,
        Math.abs(Number(c.high) || 0),
        Math.abs(Number(c.low) || 0),
        Math.abs(Number(c.close) || 0),
        Math.abs(Number(c.open) || 0)
      );
    }
    let precision = 2;
    if (maxAbs > 0 && maxAbs < 0.0001) precision = 8;
    else if (maxAbs < 0.001) precision = 8;
    else if (maxAbs < 0.01) precision = 7;
    else if (maxAbs < 0.1) precision = 6;
    else if (maxAbs < 1) precision = 5;
    else if (maxAbs < 10) precision = 4;
    else if (maxAbs < 100) precision = 3;
    else precision = 2;
    const minMove = Number((10 ** -precision).toFixed(precision));
    return { type: "price", precision: precision, minMove: minMove };
  }

  function applyPriceFormat(fmt) {
    lastPriceFormat = fmt || lastPriceFormat;
    if (candleSeries) candleSeries.applyOptions({ priceFormat: lastPriceFormat });
    emaOverlaySeries.forEach(function (series) {
      series.applyOptions({ priceFormat: lastPriceFormat, lastValueVisible: false, priceLineVisible: false });
    });
    if (lldEmaFastSeries) {
      lldEmaFastSeries.applyOptions({ priceFormat: lastPriceFormat, lastValueVisible: false, priceLineVisible: false });
    }
    if (lldEmaSlowSeries) {
      lldEmaSlowSeries.applyOptions({ priceFormat: lastPriceFormat, lastValueVisible: false, priceLineVisible: false });
    }
  }

  function applySeriesData(payload) {
    const candles = (payload && payload.candles) || [];
    if (!candleSeries) {
      return false;
    }
    if (!candles.length) {
      candleSeries.setData([]);
      emaOverlaySeries.forEach(function (series) {
        series.setData([]);
      });
      if (lldEmaFastSeries) lldEmaFastSeries.setData([]);
      if (lldEmaSlowSeries) lldEmaSlowSeries.setData([]);
      candleSeries.setMarkers([]);
      lastCandleSetCount = 0;
      lastSetDataError = null;
      return true;
    }
    applyPriceFormat(inferPriceFormat(candles));
    const ohlc = toOhlc(candles);
    try {
      candleSeries.setData(ohlc);
      lastCandleSetCount = ohlc.length;
      lastSetDataError = null;
    } catch (err) {
      lastSetDataError = String(err && err.message ? err.message : err);
      lastCandleSetCount = 0;
      return false;
    }
    return true;
  }

  function initChart() {
    const el = $("chart");
    const size = chartSize();
    const w = Math.max(size.w, 16);
    const h = Math.max(size.h, 16);
    chart = LightweightCharts.createChart(el, {
      layout: {
        background: { color: COLORS.bg },
        textColor: COLORS.text,
        fontFamily: 'Inter, "Segoe UI", Ubuntu, system-ui, sans-serif',
      },
      grid: {
        vertLines: { color: COLORS.grid },
        horzLines: { color: COLORS.grid },
      },
      crosshair: {
        mode: LightweightCharts.CrosshairMode.Normal,
        vertLine: { color: "#5b6478", width: 1, style: 3, labelBackgroundColor: "#2a2e39" },
        horzLine: { color: "#5b6478", width: 1, style: 3, labelBackgroundColor: "#2a2e39" },
      },
      rightPriceScale: {
        borderColor: COLORS.border,
        scaleMargins: PRICE_SCALE_MARGINS,
        autoScale: true,
        minimumWidth: 84,
      },
      timeScale: {
        borderColor: COLORS.border,
        timeVisible: true,
        secondsVisible: false,
        rightOffset: DEFAULT_RIGHT_OFFSET,
        barSpacing: DEFAULT_BAR_SPACING,
        minBarSpacing: 2,
      },
      handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true },
      handleScale: {
        axisPressedMouseMove: true,
        mouseWheel: true,
        pinch: true,
        axisDoubleClickReset: { time: true, price: true },
      },
      width: w,
      height: h,
    });
    lastAppliedSize = { w: size.w, h: size.h };

    candleSeries = chart.addCandlestickSeries({
      upColor: COLORS.up,
      downColor: COLORS.down,
      borderVisible: false,
      wickVisible: true,
      wickUpColor: COLORS.up,
      wickDownColor: COLORS.down,
      priceScaleId: "right",
      visible: true,
      lastValueVisible: true,
      priceLineVisible: true,
      priceFormat: lastPriceFormat,
    });

    lldEmaFastSeries = chart.addLineSeries({
      color: "#00ffff",
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: true,
      priceScaleId: "right",
      visible: false,
      priceFormat: lastPriceFormat,
      autoscaleInfoProvider: excludeOverlayFromAutoscale,
    });
    lldEmaSlowSeries = chart.addLineSeries({
      color: "#d40047",
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: true,
      priceScaleId: "right",
      visible: false,
      priceFormat: lastPriceFormat,
      autoscaleInfoProvider: excludeOverlayFromAutoscale,
    });

    chart.subscribeCrosshairMove(function (param) {
      if (param && param.point && candleSeries) {
        lastCursorXy = { x: param.point.x, y: param.point.y };
        const px = candleSeries.coordinateToPrice(param.point.y);
        if (px != null && !Number.isNaN(Number(px))) lastCursorPrice = Number(px);
        updateVolumeProfileHover(param.point.x, param.point.y);
        if (tradeBubbles.size) {
          showPtbTooltipLocal(tradeBubbleAtPoint(param.point.x, param.point.y));
        }
      } else if (tradeBubbles.size) {
        showPtbTooltipLocal(null);
      }
      updateLegend(param);
      updateSelectedLine();
      updatePreviewFromParam(param);
      if (localXhairLock) {
        return;
      }
      if (suppressUntilTime != null) {
        const echoed = param && param.time === suppressUntilTime;
        suppressUntilTime = null;
        if (echoed) {
          lastCrosshairTime = param.time;
          return;
        }
      }
      if (!window.bridge) return;
      if (!param || param.time == null) {
        if (lastCrosshairTime != null) {
          lastCrosshairTime = null;
          window.bridge.on_crosshair_leave();
        }
        return;
      }
      if (param.time === lastCrosshairTime) {
        applyLocalOscCrosshair(Number(param.time));
        return;
      }
      lastCrosshairTime = param.time;
      applyLocalOscCrosshair(Number(param.time));
      window.bridge.on_crosshair_move(Number(param.time));
    });

    if (typeof chart.subscribeDblClick === "function") {
      chart.subscribeDblClick(function (param) {
        const pt = pointFromParam(param);
        const hit = pt.x != null && pt.y != null ? hitTestXY(pt.x, pt.y) : null;
        if (hit) {
          emitDrawing({ type: "edit", overlay_id: hit });
          return;
        }
        noteScaleInteraction(180);
        layoutOverlays();
        updateSelectedLine();
      });
    }

    chart.subscribeClick(function (param) {
      if (suppressNextClick) {
        suppressNextClick = false;
        return;
      }
      if (clearShiftMeasureIfIdle()) {
        return;
      }
      if (!window.bridge) return;
      const pt = pointFromParam(param);
      if (interactionMode && interactionMode !== "select") {
        const mode = interactionMode;
        emitDrawing({ type: "point", time: pt.time, price: pt.price });
        toolClickCount += 1;
        if (ONE_POINT_TOOLS[mode] || (TWO_POINT_TOOLS[mode] && toolClickCount >= 2)) {
          finishToolToSelect();
        }
        return;
      }
      const hit = pt.x != null && pt.y != null ? hitTestXY(pt.x, pt.y) : null;
      if (hit) {
        emitDrawing({ type: "hit", overlay_id: hit });
        return;
      }
      if (pt.time == null) return;
      window.bridge.on_chart_click(Number(pt.time));
    });

    let rangeTimer = null;
    let overlayRangeTimer = null;
    chart.timeScale().subscribeVisibleTimeRangeChange(function (range) {
      updateSelectedLine();
      // During horizontal pan: refresh research markers lightly; defer heavy VP/OBP.
      scheduleResearchMarkersPaint();
      scheduleTradeBubblesPaint();
      if (overlayRangeTimer) clearTimeout(overlayRangeTimer);
      overlayRangeTimer = setTimeout(function () {
        overlayRangeTimer = null;
        layoutOverlays();
      }, researchMarkers.size > 200 ? 140 : 90);
      if (!range || range.from == null || range.to == null) {
        return;
      }
      const from = Number(range.from);
      const to = Number(range.to);
      if (rangeTimer) clearTimeout(rangeTimer);
      rangeTimer = setTimeout(function () {
        if (window.bridge) {
          window.bridge.on_visible_range(from, to);
        }
      }, 180);
    });

    chart.timeScale().subscribeVisibleLogicalRangeChange(function (range) {
      if (programmaticNavDepth > 0) return;
      syncOscLogicalFromMain(range);
      const n = ((lastPayload && lastPayload.candles) || []).length;
      followLive = isNearLiveLogicalRange(range, n);
      // Keep candlesticks in view after horizontal pan (EMA/walls can pin a bad scale).
      scheduleFitPriceScaleToCandles(false);
    });

    const ro = new ResizeObserver(function () {
      resize();
    });
    ro.observe(el);
    bindShiftMeasureHandlers();
    el.addEventListener("contextmenu", onChartContextMenu);
    const pricePane = $("price-pane");
    if (pricePane) pricePane.addEventListener("contextmenu", onChartContextMenu);
    el.addEventListener("pointerdown", onScalePointerDown, true);
    el.addEventListener("pointerdown", onPointerDown, true);
    el.addEventListener("wheel", onScaleWheel, { passive: true });
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", onScalePointerUp, true);
    window.addEventListener("pointerup", onPointerUp);
    window.addEventListener("pointercancel", onPointerUp);
    window.addEventListener("pointercancel", onScalePointerUp, true);
    document.addEventListener("keydown", onChartKey);

    const rmCanvas = researchMarkersCanvas();
    if (rmCanvas) rmCanvas.style.pointerEvents = "none";
    const ptbCanvas = ptbOverlayCanvas();
    if (ptbCanvas) ptbCanvas.style.pointerEvents = "none";

    initLowerSplit();
    const oscEl = $("oscillator");
    if (oscEl && typeof ResizeObserver !== "undefined") {
      const oscRo = new ResizeObserver(function () {
        resizeOsc();
      });
      oscRo.observe(oscEl);
    }

    // Payload may have arrived before the series existed.
    if (lastPayload) {
      setData(lastPayload);
    }
    if (lastLowerPayload) {
      setLowerPane(lastLowerPayload);
    }
    if (lastEmaOverlays && lastEmaOverlays.series && lastEmaOverlays.series.length) {
      setEmaOverlays(lastEmaOverlays);
    }
  }

  function resize() {
    if (!chart) return;
    const size = chartSize();
    if (size.w < 16 || size.h < 16) return;
    const wasInvalid = lastAppliedSize.w < 16 || lastAppliedSize.h < 16;
    chart.applyOptions({ width: size.w, height: size.h });
    lastAppliedSize = size;
    // Candlestick series set at 0×0 often never paints; line series still does.
    // Re-apply once the pane has a real size. Do not do this on every resize
    // (that would reset the user's zoom via applyDefaultView).
    if (wasInvalid && lastPayload && lastPayload.candles && lastPayload.candles.length) {
      applySeriesData(lastPayload);
      applyDefaultView();
    }
    if (wasInvalid) {
      recreateNativeOverlays();
      if (lastLowerPayload) {
        setLowerPane(lastLowerPayload);
      }
      if (lastEmaOverlays) {
        setEmaOverlays(lastEmaOverlays);
      }
    }
    clipOverlayLayerToPlot();
    layoutOverlays();
    drawVolumeProfile();
    scheduleResearchMarkersPaint(true);
    scheduleTradeBubblesPaint(true);
    updateSelectedLine();
    resizeOsc();
  }

  function fmt(n) {
    if (n == null || Number.isNaN(n)) return "—";
    const abs = Math.abs(n);
    const digits = abs >= 100 ? 2 : abs >= 1 ? 4 : 6;
    return Number(n).toFixed(digits);
  }

  function updateLegend(param) {
    const legend = $("legend");
    if (!lastPayload) {
      legend.textContent = "";
      return;
    }
    const meta = lastPayload.symbol + "  " + lastPayload.timeframe;
    let candle = null;
    if (param && param.seriesData && candleSeries) {
      candle = param.seriesData.get(candleSeries);
    }
    if (!candle && lastPayload.candles && lastPayload.candles.length) {
      // Prefer a candle in the visible window — live tip misleads after history pan.
      try {
        const range = chart ? chart.timeScale().getVisibleLogicalRange() : null;
        const bounds = visibleCandlePriceBounds(range);
        if (bounds && lastPayload.candles[bounds.toIdx]) {
          candle = lastPayload.candles[bounds.toIdx];
        }
      } catch (err) {
        candle = null;
      }
      if (!candle) candle = lastPayload.candles[lastPayload.candles.length - 1];
    }
    if (!candle) {
      legend.innerHTML = '<span class="sym">' + meta + "</span>";
      return;
    }
    const cls = candle.close >= candle.open ? "up" : "dn";
    let emaBits = "";
    if (param && param.seriesData) {
      emaOverlaySeries.forEach(function (series, id) {
        const point = param.seriesData.get(series);
        if (!point) return;
        const spec = emaOverlaySpec(id);
        const label = spec && spec.title ? spec.title : id;
        const color = spec && spec.color ? spec.color : COLORS.ema20;
        emaBits +=
          '  <span style="color:' + color + '">' + label + " " + fmt(point.value) + "</span>";
      });
    }
    legend.innerHTML =
      '<span class="sym">' +
      meta +
      '</span><span class="' +
      cls +
      '">O ' +
      fmt(candle.open) +
      "  H " +
      fmt(candle.high) +
      "  L " +
      fmt(candle.low) +
      "  C " +
      fmt(candle.close) +
      "</span>" +
      emaBits;
  }

  function emaOverlaySpec(id) {
    const list = (lastEmaOverlays && lastEmaOverlays.series) || [];
    for (let i = 0; i < list.length; i++) {
      if (list[i] && list[i].id === id) return list[i];
    }
    return null;
  }

  function rebuildIndex(candles) {
    candleByTime = new Map();
    for (let i = 0; i < candles.length; i++) {
      candleByTime.set(candles[i].time, candles[i]);
    }
  }

  function ensureLivePriceLine(price) {
    if (!candleSeries || !Number.isFinite(price)) return;
    try {
      if (!livePriceLine) {
        livePriceLine = candleSeries.createPriceLine({
          price: price,
          color: "#f0b429",
          lineWidth: 1,
          lineStyle: LightweightCharts.LineStyle.SparseDotted,
          axisLabelVisible: true,
          title: "live",
        });
      } else {
        livePriceLine.applyOptions({ price: price });
      }
    } catch (err) {
      /* price line is best-effort */
    }
  }

  function clearLivePriceLine() {
    if (!candleSeries || !livePriceLine) {
      livePriceLine = null;
      return;
    }
    try {
      candleSeries.removePriceLine(livePriceLine);
    } catch (err) {
      /* ignore */
    }
    livePriceLine = null;
  }

  function stickToLiveEdge() {
    if (!chart || !followLive || replayViewLock) return;
    // Never fight an in-progress user pan/zoom on the time/price scale.
    if (pointerScaleWatch || performance.now() < scaleWatchUntil) return;
    try {
      programmaticNavDepth += 1;
      chart.timeScale().scrollToRealTime();
    } catch (err) {
      /* older builds / empty series */
    } finally {
      programmaticNavDepth = Math.max(0, programmaticNavDepth - 1);
    }
  }

  function isNearLiveLogicalRange(range, barCount) {
    const candles = (lastPayload && lastPayload.candles) || [];
    if (!candles.length || !chart) return false;
    // Time-based: logical indices break when overlays expand the timescale
    // (followLive stayed true → forming ticks yanked horizontal pans back).
    try {
      const vr = chart.timeScale().getVisibleRange();
      if (vr && vr.to != null) {
        const lastT = Number(candles[candles.length - 1].time);
        if (!Number.isFinite(lastT)) return false;
        const barSec = estimateBarSec(candles);
        return Number(vr.to) >= lastT - barSec * 0.25;
      }
    } catch (err) {
      /* fall through */
    }
    const n = Number(barCount) || candles.length;
    if (!range || n < 1 || range.to == null) return false;
    const liveTo = (n - 1) + DEFAULT_RIGHT_OFFSET;
    return Number(range.to) >= liveTo - 0.75;
  }

  // Always apply series.update when OHLC changed — never trust shared lastPayload
  // mutations from the host (pendingData === lastPayload).
  function updateFormingBar(bar) {
    if (!candleSeries || !bar || bar.time == null) return false;
    const point = {
      time: Number(bar.time),
      open: Number(bar.open),
      high: Number(bar.high),
      low: Number(bar.low),
      close: Number(bar.close),
    };
    if (!Number.isFinite(point.time) || !Number.isFinite(point.close)) return false;
    if (!Number.isFinite(point.high) || point.high < point.close) point.high = Math.max(point.open, point.close);
    if (!Number.isFinite(point.low) || point.low > point.close) point.low = Math.min(point.open, point.close);
    if (point.high < point.low) {
      const mid = point.close;
      point.high = Math.max(point.high, mid);
      point.low = Math.min(point.low, mid);
    }
    const candles = (lastPayload && lastPayload.candles) || [];
    if (!candles.length) return false;
    const last = candles[candles.length - 1];
    const lastT = Number(last.time);
    if (!Number.isFinite(lastT)) return false;
    // If host tip is behind chart tip, paint live price onto the chart tip.
    if (point.time < lastT) {
      point.time = lastT;
      point.open = Number(last.open);
      point.high = Math.max(Number(last.high), point.high, point.close);
      point.low = Math.min(Number(last.low), point.low, point.close);
    }
    try {
      // Always push to the series so the candle body tracks live price even when
      // lastPayload was already mutated to match (shared object with host).
      candleSeries.update(point);
    } catch (err) {
      ensureLivePriceLine(point.close);
      return false;
    }
    if (Number(candles[candles.length - 1].time) === point.time) {
      candles[candles.length - 1] = Object.assign({}, candles[candles.length - 1], point);
    } else {
      candles.push(Object.assign({}, point));
    }
    lastPayload.candles = candles;
    candleByTime.set(point.time, candles[candles.length - 1]);
    ensureLivePriceLine(point.close);
    // Do not stickToLiveEdge here — live ticks were yanking horizontal pans back.
    const now = performance.now();
    if (now - (updateFormingBar._legendAt || 0) > 250) {
      updateFormingBar._legendAt = now;
      updateLegend(null);
    }
    return true;
  }

  function setData(payload, opts) {
    const preserveView = !!(opts && opts.preserveView);
    const skipDefaultView = !!(opts && opts.skipDefaultView);
    let savedRange = null;
    if (preserveView && chart) {
      try {
        savedRange = chart.timeScale().getVisibleLogicalRange();
      } catch (err) {
        savedRange = null;
      }
    }
    clearShiftMeasure();
    lastPayload = payload || { candles: [] };
    const candles = lastPayload.candles || [];
    const empty = $("empty");
    const badge = $("demo-badge");

    if (lastPayload.is_demo) {
      badge.classList.add("visible");
      badge.textContent = lastPayload.demo_note || "DEMO · synthetic data";
    } else {
      badge.classList.remove("visible");
    }

    rebuildIndex(candles);

    if (!candles.length) {
      empty.classList.add("visible");
      if (candleSeries) {
        applySeriesData(lastPayload);
      }
      clearLivePriceLine();
      $("legend").textContent = "";
      hideSelectedLine();
      return;
    }

    empty.classList.remove("visible");
    if (!candleSeries) {
      return;
    }
    applySeriesData(lastPayload);
    const tip = candles[candles.length - 1];
    if (tip && tip.close != null) ensureLivePriceLine(Number(tip.close));
    updateOscTimeBase();
    if (!preserveView && !skipDefaultView) {
      applyDefaultView();
      resize();
    } else if (preserveView && savedRange) {
      try {
        chart.timeScale().setVisibleLogicalRange(savedRange);
      } catch (err) {
        /* keep current view */
      }
      syncOscLogicalFromMain(savedRange);
      if (followLive && !replayViewLock) stickToLiveEdge();
      resize();
    } else {
      // skipDefaultView (jumpToUnix pending): never yank to live tip — that hides
      // historical research markers that the host is about to focus.
      if (replayViewLock) enforceReplayViewLock();
      resize();
    }
    scheduleFitPriceScaleToCandles(true);
    if (lastSelectedUnix != null) {
      applySelectedMarker(lastSelectedUnix);
    }
    updateLegend(null);
  }

  function hideSelectedLine() {
    const el = $("selected-line");
    if (el) el.style.display = "none";
  }

  function updateSelectedLine() {
    const el = $("selected-line");
    const host = $("chart");
    if (!el || !chart || lastSelectedUnix == null) {
      hideSelectedLine();
      return;
    }
    if (!candleByTime.has(lastSelectedUnix)) {
      hideSelectedLine();
      return;
    }
    const x = chart.timeScale().timeToCoordinate(lastSelectedUnix);
    if (x == null) {
      hideSelectedLine();
      return;
    }
    el.style.display = "block";
    el.style.left = host.offsetLeft + x + "px";
  }

  function applySelectedMarker(unixTime) {
    if (!candleByTime.has(unixTime)) {
      candleSeries.setMarkers([]);
      hideSelectedLine();
      return;
    }
    candleSeries.setMarkers([
      {
        time: unixTime,
        position: "inBar",
        color: COLORS.selected,
        shape: "circle",
      },
    ]);
    updateSelectedLine();
  }

  function setSelectedMarker(unixTime) {
    if (unixTime == null) {
      lastSelectedUnix = null;
      if (candleSeries) candleSeries.setMarkers([]);
      hideSelectedLine();
      return;
    }
    lastSelectedUnix = Number(unixTime);
    applySelectedMarker(lastSelectedUnix);
  }

  function setSyncedCrosshair(unixTime, _generation) {
    unixTime = Number(unixTime);
    const candle = candleByTime.get(unixTime);
    if (!candle) {
      if (chart) chart.clearCrosshairPosition();
      if (oscChart) oscChart.clearCrosshairPosition();
      return;
    }
    suppressUntilTime = unixTime;
    lastCrosshairTime = unixTime;
    localXhairLock = true;
    try {
      chart.setCrosshairPosition(candle.close, unixTime, candleSeries);
      applyLocalOscCrosshairUnlocked(unixTime);
    } finally {
      localXhairLock = false;
    }
  }

  function clearSyncedCrosshair() {
    suppressUntilTime = null;
    lastCrosshairTime = null;
    if (chart) chart.clearCrosshairPosition();
    if (oscChart) oscChart.clearCrosshairPosition();
  }

  function setIndicatorVisible(name, visible) {
    const series = emaOverlaySeries.get(name);
    if (!series) return;
    series.applyOptions({ visible: !!visible });
  }

  function setEmaOverlays(payload, opts) {
    lastEmaOverlays = payload || { series: [] };
    if (!chart) return;
    const skipRangeRestore = !!(opts && opts.skipRangeRestore);
    let savedTimeRange = null;
    if (!skipRangeRestore) {
      try {
        savedTimeRange = chart.timeScale().getVisibleRange();
      } catch (err) {
        savedTimeRange = null;
      }
    }
    const wanted = {};
    const list = lastEmaOverlays.series || [];
    for (let i = 0; i < list.length; i++) {
      const spec = list[i];
      if (spec && spec.id) wanted[spec.id] = spec;
    }
    emaOverlaySeries.forEach(function (series, id) {
      if (!wanted[id]) {
        try {
          chart.removeSeries(series);
        } catch (err) {
          /* ignore */
        }
        emaOverlaySeries.delete(id);
      }
    });
    Object.keys(wanted).forEach(function (id) {
      const spec = wanted[id];
      let series = emaOverlaySeries.get(id);
      const seriesOpts = applyOverlaySeriesScaleOpts({
        color: spec.color || COLORS.ema20,
        lineWidth: spec.line_width || 2,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: true,
        visible: spec.visible !== false,
        priceScaleId: "right",
        priceFormat: lastPriceFormat,
      });
      if (!series) {
        series = chart.addLineSeries(seriesOpts);
        emaOverlaySeries.set(id, series);
      } else {
        series.applyOptions(seriesOpts);
      }
      // Clip warmup-only points so overlays cannot expand the timescale past candles.
      series.setData(clipPointsToCandleExtent(spec.data || []));
    });
    if (replayViewLock) {
      enforceReplayViewLock();
    } else if (followLive && !skipRangeRestore) {
      applyDefaultView();
    } else if (savedTimeRange && savedTimeRange.from != null && savedTimeRange.to != null) {
      runProgrammaticNav(function () {
        try {
          chart.timeScale().setVisibleRange({
            from: Number(savedTimeRange.from),
            to: Number(savedTimeRange.to),
          });
        } catch (err) {
          /* ignore */
        }
        syncOscLogicalFromMain(chart.timeScale().getVisibleLogicalRange());
      });
    }
    scheduleFitPriceScaleToCandles(true);
  }

  function setLldEma(payload) {
    const data = payload || {};
    if (lldEmaFastSeries) {
      lldEmaFastSeries.applyOptions({
        color: data.fast_color || "#00ffff",
        visible: !!data.fast_visible,
        autoscaleInfoProvider: excludeOverlayFromAutoscale,
      });
      lldEmaFastSeries.setData(clipPointsToCandleExtent(data.fast || []));
    }
    if (lldEmaSlowSeries) {
      lldEmaSlowSeries.applyOptions({
        color: data.slow_color || "#d40047",
        visible: !!data.slow_visible,
        autoscaleInfoProvider: excludeOverlayFromAutoscale,
      });
      lldEmaSlowSeries.setData(clipPointsToCandleExtent(data.slow || []));
    }
  }

  function clear() {
    lastSelectedUnix = null;
    setData({ candles: [], ema20: [], ema35: [], is_demo: false });
  }

  function lineStyleEnum(name) {
    const LS = LightweightCharts.LineStyle || {};
    if (name === "dotted") return LS.Dotted != null ? LS.Dotted : 1;
    if (name === "dashed") return LS.Dashed != null ? LS.Dashed : 2;
    return LS.Solid != null ? LS.Solid : 0;
  }

  function overlayLayer() {
    return $("overlay-layer");
  }

  function researchMarkersCanvas() {
    return $("research-markers");
  }

  function clipResearchMarkersToPlot() {
    const canvas = researchMarkersCanvas();
    if (!canvas) return;
    canvas.style.right = Math.max(0, priceAxisWidth()) + "px";
    canvas.style.bottom = Math.max(0, timeAxisHeight()) + "px";
  }

  function isCanvasResearchMarker(payload) {
    if (!payload || payload.type !== "marker") return false;
    if (payload.metadata && payload.metadata.drawing_id) return false;
    const id = String(payload.id || "");
    if (id.indexOf("__preview") === 0) return false;
    // APS uses DOM markers (overlay-layer) — canvas path was easy to miss behind
    // live-tip view / sizing; 29 arrows are fine as DOM.
    if (/^aps-/.test(id)) return false;
    if (/^(ezm-|edc-|csw-|stoch-)/.test(id)) return true;
    const meta = payload.metadata || {};
    const origin = String(meta.origin || meta.source || "");
    if (origin === "a_plus_pool_signal_scanner_v1" || String(meta.strategy_id || "").indexOf("a_plus") === 0) {
      return false;
    }
    if (
      origin === "ezm_candidate_discovery" ||
      origin.indexOf("backtester") >= 0 ||
      origin.indexOf("candidate") >= 0
    ) {
      return true;
    }
    const kind = String(meta.kind || "");
    if (kind.indexOf("APS") === 0) return false;
    return kind.indexOf("EZM") === 0 || kind.indexOf("EDC") === 0 || kind.indexOf("CSW") === 0;
  }

  function scheduleResearchMarkersPaint(immediate) {
    if (immediate) {
      if (researchPaintTimer != null) {
        clearTimeout(researchPaintTimer);
        researchPaintTimer = null;
      }
      researchPaintQueued = false;
      paintResearchMarkers();
      return;
    }
    if (researchPaintQueued) return;
    researchPaintQueued = true;
    researchPaintTimer = setTimeout(function () {
      researchPaintTimer = null;
      researchPaintQueued = false;
      if (researchPaintRaf != null) cancelAnimationFrame(researchPaintRaf);
      researchPaintRaf = requestAnimationFrame(function () {
        researchPaintRaf = null;
        paintResearchMarkers();
      });
    }, researchMarkers.size > 400 ? 48 : 16);
  }

  function ptbOverlayCanvas() {
    return $("ptb-overlay") || researchMarkersCanvas();
  }

  function clipPtbOverlayToPlot() {
    const canvas = ptbOverlayCanvas();
    if (!canvas) return;
    // Match VP overlay: explicit CSS box so canvas is never stuck at default 300×150.
    const size = chartSize();
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(1, size.w || 0);
    const h = Math.max(1, size.h || 0);
    canvas.style.position = "absolute";
    canvas.style.left = "0";
    canvas.style.top = "0";
    canvas.style.right = "auto";
    canvas.style.bottom = "auto";
    canvas.style.width = w + "px";
    canvas.style.height = h + "px";
    canvas.style.pointerEvents = "none";
    canvas.style.zIndex = "5";
    const wantW = Math.max(1, Math.floor(w * dpr));
    const wantH = Math.max(1, Math.floor(h * dpr));
    if (canvas.width !== wantW || canvas.height !== wantH) {
      canvas.width = wantW;
      canvas.height = wantH;
    }
  }

  function bubbleRadiusPx(sizeClass, totalNotional) {
    const cls = String(sizeClass || "UNCALIBRATED");
    // Large enough to read on 1m charts; hard-cap so extremes don't cover the pane
    if (cls === "EXTREME") return 36;
    if (cls === "LARGE") return 26;
    if (cls === "MEDIUM") return 18;
    if (cls === "SMALL") return 10;
    const n = Math.max(0, Number(totalNotional) || 0);
    return Math.min(14, 6 + Math.sqrt(n) / 60);
  }

  function bubbleFill(side, forming) {
    const buy = side === "BUY";
    const base = buy ? COLORS.up : (side === "SELL" ? COLORS.down : COLORS.muted);
    return hexAlpha(base, forming ? tradeBubbleAlpha * 0.5 : tradeBubbleAlpha);
  }

  function bubbleStroke(side) {
    const buy = side === "BUY";
    return buy ? COLORS.up : (side === "SELL" ? COLORS.down : COLORS.muted);
  }

  /** Place bubble within the 1m (or TF) bar using sub-bar time fraction. */
  function xOfBubble(unix) {
    if (!chart || unix == null) return null;
    const t = Number(unix);
    if (!Number.isFinite(t)) return null;
    const direct = chart.timeScale().timeToCoordinate(t);
    if (direct != null) return direct;
    const bar = snapUnixToBar(t);
    if (bar == null) return null;
    const x0 = chart.timeScale().timeToCoordinate(bar);
    if (x0 == null) return null;
    const candles = (lastPayload && lastPayload.candles) || [];
    let barSec = 60;
    const idx = candleByTime.has(bar)
      ? (function () {
          for (let i = 0; i < candles.length; i++) {
            if (Number(candles[i].time) === Number(bar)) return i;
          }
          return -1;
        })()
      : -1;
    if (idx >= 0 && idx + 1 < candles.length) {
      barSec = Math.max(1, Number(candles[idx + 1].time) - Number(bar));
    }
    const x1 = chart.timeScale().timeToCoordinate(Number(bar) + barSec);
    if (x1 == null || x1 === x0) return x0;
    const frac = Math.max(0, Math.min(0.95, (t - Number(bar)) / barSec));
    return x0 + frac * (x1 - x0);
  }

  function scheduleTradeBubblesPaint(immediate) {
    if (immediate) {
      if (tradeBubblePaintTimer != null) {
        clearTimeout(tradeBubblePaintTimer);
        tradeBubblePaintTimer = null;
      }
      tradeBubblePaintQueued = false;
      paintTradeBubblesLayer();
      return;
    }
    if (tradeBubblePaintQueued) return;
    tradeBubblePaintQueued = true;
    tradeBubblePaintTimer = setTimeout(function () {
      tradeBubblePaintTimer = null;
      tradeBubblePaintQueued = false;
      if (tradeBubblePaintRaf != null) cancelAnimationFrame(tradeBubblePaintRaf);
      tradeBubblePaintRaf = requestAnimationFrame(function () {
        tradeBubblePaintRaf = null;
        paintTradeBubblesLayer();
      });
    }, tradeBubbles.size > 800 ? 48 : 16);
  }

  function paintTradeBubblesLayer() {
    const canvas = ptbOverlayCanvas();
    if (!canvas) return;
    clipPtbOverlayToPlot();
    if (!tradeBubbles.size) {
      const ctxEmpty = canvas.getContext("2d");
      if (ctxEmpty) {
        ctxEmpty.setTransform(1, 0, 0, 1, 0, 0);
        ctxEmpty.clearRect(0, 0, canvas.width || 1, canvas.height || 1);
      }
      canvas.style.display = "none";
      return;
    }
    canvas.style.display = "block";
    const size = chartSize();
    const dpr = window.devicePixelRatio || 1;
    const boxW = Math.max(1, size.w);
    const boxH = Math.max(1, size.h);
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, boxW, boxH);
    if (!chart || boxW < 16 || boxH < 16) return;

    const pad = 24;
    const maxDraw = 2500;
    let n = 0;
    tradeBubbles.forEach(function (b) {
      if (n >= maxDraw) return;
      const x = xOfBubble(b.timestamp);
      if (x == null || x < -pad || x > boxW + pad) return;
      const y = yOf(b.price);
      if (y == null || y < -pad || y > boxH + pad) return;
      const r = Math.min(44, bubbleRadiusPx(b.size_class, b.total_notional));
      b._x = x;
      b._y = y;
      b._r = r;
      ctx.beginPath();
      ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.fillStyle = bubbleFill(b.dominant_side, !!b.forming);
      ctx.fill();
      ctx.strokeStyle = bubbleStroke(b.dominant_side);
      ctx.lineWidth = b.bubble_id === tradeBubbleHoverId ? 2.5 : (b.forming ? 1.5 : 1.25);
      if (b.forming) ctx.setLineDash([4, 3]);
      ctx.stroke();
      ctx.setLineDash([]);
      n += 1;
    });
  }

  function setTradeBubbles(items, opts) {
    tradeBubbles.clear();
    tradeBubbleHoverId = null;
    if (opts && opts.alpha != null) {
      const a = Number(opts.alpha);
      if (Number.isFinite(a)) tradeBubbleAlpha = Math.min(0.85, Math.max(0.2, a));
    }
    const seen = Object.create(null);
    (items || []).forEach(function (raw) {
      if (!raw || raw.bubble_id == null) return;
      const id = String(raw.bubble_id);
      if (seen[id]) return;
      seen[id] = true;
      const ts = Number(raw.timestamp);
      const price = Number(raw.price);
      if (!Number.isFinite(ts) || !Number.isFinite(price)) return;
      tradeBubbles.set(id, {
        bubble_id: id,
        timestamp: ts,
        price: price,
        buy_notional: Number(raw.buy_notional) || 0,
        sell_notional: Number(raw.sell_notional) || 0,
        total_notional: Number(raw.total_notional) || 0,
        delta_notional: Number(raw.delta_notional) || 0,
        trade_count: Number(raw.trade_count) || 0,
        max_single_trade_notional: Number(raw.max_single_trade_notional) || 0,
        dominant_side: String(raw.dominant_side || "FLAT"),
        size_class: String(raw.size_class || "UNCALIBRATED"),
        known_at: raw.known_at || "",
        forming: !!raw.forming,
        source_quality: raw.source_quality || "ok",
        research_only: true,
      });
    });
    scheduleTradeBubblesPaint(true);
    return true;
  }

  function clearTradeBubbles() {
    tradeBubbles.clear();
    tradeBubbleHoverId = null;
    const tip = $("ptb-tooltip");
    if (tip) {
      tip.hidden = true;
      tip.textContent = "";
    }
    scheduleTradeBubblesPaint(true);
    return true;
  }

  function tradeBubbleAtPoint(x, y) {
    if (x == null || y == null || !tradeBubbles.size) return null;
    let best = null;
    let bestD = Infinity;
    tradeBubbles.forEach(function (b) {
      if (b._x == null || b._y == null) return;
      const dx = b._x - x;
      const dy = b._y - y;
      const hitR = Math.max(10, (b._r || 8) + 4);
      const d2 = dx * dx + dy * dy;
      if (d2 <= hitR * hitR && d2 < bestD) {
        bestD = d2;
        best = b;
      }
    });
    return best;
  }

  /** Legacy host API: only exact-second match (no ±90s candle spam). */
  function tradeBubbleAtUnix(unix) {
    if (unix == null || !tradeBubbles.size) return null;
    const t = Number(unix);
    if (!Number.isFinite(t)) return null;
    let best = null;
    let bestDt = Infinity;
    tradeBubbles.forEach(function (b) {
      const dt = Math.abs(b.timestamp - t);
      if (dt < bestDt) {
        bestDt = dt;
        best = b;
      }
    });
    if (!best || bestDt > 1.5) return null;
    return best;
  }

  function showPtbTooltipLocal(bubble) {
    const tip = $("ptb-tooltip");
    if (!tip) return;
    if (!bubble) {
      tip.hidden = true;
      tip.textContent = "";
      tradeBubbleHoverId = null;
      scheduleTradeBubblesPaint(true);
      return;
    }
    const fmt = function (n) {
      const v = Number(n);
      if (!Number.isFinite(v)) return "—";
      if (Math.abs(v) >= 1000) return v.toFixed(0);
      if (Math.abs(v) >= 1) return v.toFixed(2);
      return v.toPrecision(4);
    };
    const utc = new Date(bubble.timestamp * 1000).toISOString().replace(".000Z", "Z");
    tip.textContent = [
      "RESEARCH ONLY · Public Trade Bubble",
      "UTC " + utc,
      "price " + fmt(bubble.price),
      "buy " + fmt(bubble.buy_notional) + " · sell " + fmt(bubble.sell_notional),
      "delta " + fmt(bubble.delta_notional) + " · total " + fmt(bubble.total_notional),
      "trades " + bubble.trade_count + " · max " + fmt(bubble.max_single_trade_notional),
      "side " + bubble.dominant_side + " · class " + bubble.size_class +
        (bubble.forming ? " · FORMING" : ""),
      "known_at " + (bubble.known_at || "—") + " · " + (bubble.source_quality || "ok"),
    ].join("\n");
    tip.hidden = false;
    const left = Math.max(8, Math.min((bubble._x || 12) + 14, (chartSize().w || 400) - 220));
    const top = Math.max(8, (bubble._y || 24) - 10);
    tip.style.left = left + "px";
    tip.style.top = top + "px";
    if (tradeBubbleHoverId !== bubble.bubble_id) {
      tradeBubbleHoverId = bubble.bubble_id;
      scheduleTradeBubblesPaint(true);
    }
  }

  function paintResearchMarkers() {
    const canvas = researchMarkersCanvas();
    if (!canvas) return;
    // Must never steal chart drag / pan — even if CSS cache is stale.
    canvas.style.pointerEvents = "none";
    clipResearchMarkersToPlot();
    if (!researchMarkers.size) {
      const ctxEmpty = canvas.getContext("2d");
      if (ctxEmpty) {
        ctxEmpty.setTransform(1, 0, 0, 1, 0, 0);
        ctxEmpty.clearRect(0, 0, canvas.width || 1, canvas.height || 1);
      }
      canvas.style.display = "none";
      return;
    }
    canvas.style.display = "block";
    const size = chartSize();
    const dpr = window.devicePixelRatio || 1;
    const cssW = Math.max(1, size.w - Math.max(0, priceAxisWidth()));
    const cssH = Math.max(1, size.h - Math.max(0, timeAxisHeight()));
    // Prefer CSS inset box; do not fight absolute layout with fixed px width/height
    // (that previously let the canvas cover the plot and block pan if events leaked).
    const boxW = canvas.clientWidth > 0 ? canvas.clientWidth : cssW;
    const boxH = canvas.clientHeight > 0 ? canvas.clientHeight : cssH;
    const wantW = Math.max(1, Math.floor(boxW * dpr));
    const wantH = Math.max(1, Math.floor(boxH * dpr));
    if (canvas.width !== wantW || canvas.height !== wantH) {
      canvas.width = wantW;
      canvas.height = wantH;
    }
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, boxW, boxH);
    if (!chart || boxW < 16 || boxH < 16) return;

    const pad = 24;
    const drawn = [];
    researchMarkers.forEach(function (rec) {
      const p = rec.payload;
      if (!p || p.visible === false) return;
      const x = xOf(p.timestamp);
      if (x == null || x < -pad || x > boxW + pad) return;
      let y = p.price != null ? yOf(p.price) : null;
      if (y == null) y = 24;
      if (p.position === "above") y -= 12;
      if (p.position === "below") y += 12;
      if (y < -pad || y > boxH + pad) return;
      drawn.push({
        x: x,
        y: y,
        shape: p.shape || "circle",
        color: (p.style && p.style.color) || "#888",
        sizePx: Math.max(4, Number(p.size) || 8),
        text: p.text || "",
      });
    });

    const showText = drawn.length <= 280;
    for (let i = 0; i < drawn.length; i++) {
      const m = drawn[i];
      ctx.save();
      ctx.translate(m.x, m.y);
      ctx.fillStyle = m.color;
      ctx.strokeStyle = m.color;
      if (m.shape === "diamond") {
        const r = m.sizePx * 0.55;
        ctx.beginPath();
        ctx.moveTo(0, -r);
        ctx.lineTo(r, 0);
        ctx.lineTo(0, r);
        ctx.lineTo(-r, 0);
        ctx.closePath();
        ctx.fill();
      } else if (m.shape === "arrow_up") {
        const hw = m.sizePx;
        const hh = m.sizePx * 1.4;
        ctx.beginPath();
        ctx.moveTo(0, -hh * 0.55);
        ctx.lineTo(hw, hh * 0.45);
        ctx.lineTo(-hw, hh * 0.45);
        ctx.closePath();
        ctx.fill();
      } else if (m.shape === "arrow_down") {
        const hw = m.sizePx;
        const hh = m.sizePx * 1.4;
        ctx.beginPath();
        ctx.moveTo(0, hh * 0.55);
        ctx.lineTo(hw, -hh * 0.45);
        ctx.lineTo(-hw, -hh * 0.45);
        ctx.closePath();
        ctx.fill();
      } else {
        ctx.beginPath();
        ctx.arc(0, 0, m.sizePx * 0.5, 0, Math.PI * 2);
        ctx.fill();
      }
      if (showText && m.text) {
        ctx.font = "10px sans-serif";
        ctx.textAlign = "left";
        ctx.textBaseline = "middle";
        const tw = ctx.measureText(m.text).width;
        const padX = 3;
        const bx = m.sizePx * 0.7;
        const by = -7;
        ctx.fillStyle = hexAlpha(m.color, 0.18);
        ctx.fillRect(bx, by, tw + padX * 2, 14);
        ctx.fillStyle = m.color;
        ctx.fillText(m.text, bx + padX, by + 7);
      }
      ctx.restore();
    }
  }

  function snapUnixToBar(unix) {
    const t = Number(unix);
    if (!Number.isFinite(t)) return unix;
    if (candleByTime.has(t)) return t;
    const candles = lastPayload && lastPayload.candles;
    if (!candles || !candles.length) return unix;
    if (t <= candles[0].time) return candles[0].time;
    const last = candles[candles.length - 1];
    if (t >= last.time) return last.time;
    let lo = 0;
    let hi = candles.length - 1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      const mt = Number(candles[mid].time);
      if (mt === t) return t;
      if (mt < t) lo = mid + 1;
      else hi = mid - 1;
    }
    return candles[Math.max(0, hi)].time;
  }

  function xOf(unix) {
    if (!chart || unix == null) return null;
    const t = Number(unix);
    if (!Number.isFinite(t)) return null;
    let coord = chart.timeScale().timeToCoordinate(t);
    if (coord != null) return coord;
    const snapped = snapUnixToBar(t);
    if (snapped == null) return null;
    coord = chart.timeScale().timeToCoordinate(Number(snapped));
    return coord != null ? coord : null;
  }

  function yOf(price) {
    if (!candleSeries || price == null) return null;
    return candleSeries.priceToCoordinate(price);
  }

  function sentinelPrice() {
    if (lastPayload && lastPayload.candles && lastPayload.candles.length) {
      return lastPayload.candles[lastPayload.candles.length - 1].close;
    }
    return null;
  }

  function samplePriceY() {
    const price = sentinelPrice();
    if (price == null) return null;
    const y = yOf(price);
    if (y == null || Number.isNaN(y)) return null;
    return { price: price, y: y };
  }

  function priceMapChanged() {
    const sample = samplePriceY();
    if (!sample) return false;
    if (lastPriceYSample == null || lastPriceYPrice !== sample.price) {
      lastPriceYSample = sample.y;
      lastPriceYPrice = sample.price;
      return false;
    }
    if (sample.y !== lastPriceYSample) {
      lastPriceYSample = sample.y;
      return true;
    }
    return false;
  }

  function noteScaleInteraction(holdMs) {
    const extra = holdMs == null ? 80 : holdMs;
    const until = performance.now() + extra;
    if (until > scaleWatchUntil) scaleWatchUntil = until;
    if (scaleWatchRaf == null) {
      const sample = samplePriceY();
      if (sample) {
        lastPriceYSample = sample.y;
        lastPriceYPrice = sample.price;
      }
      scaleWatchRaf = requestAnimationFrame(scaleWatchTick);
    }
  }

  function scaleWatchTick() {
    if (priceMapChanged()) {
      updateSelectedLine();
      // Coalesce heavy overlay paints while the price scale is moving.
      if (scaleWatchTick._layoutTimer == null) {
        scaleWatchTick._layoutTimer = setTimeout(function () {
          scaleWatchTick._layoutTimer = null;
          layoutOverlays();
        }, researchMarkers.size > 200 ? 90 : 50);
      }
    }
    const keep = pointerScaleWatch || performance.now() < scaleWatchUntil;
    if (keep) {
      scaleWatchRaf = requestAnimationFrame(scaleWatchTick);
      return;
    }
    scaleWatchRaf = null;
    if (scaleWatchTick._layoutTimer != null) {
      clearTimeout(scaleWatchTick._layoutTimer);
      scaleWatchTick._layoutTimer = null;
    }
    layoutOverlays();
    updateSelectedLine();
  }

  function onScalePointerDown() {
    pointerScaleWatch = true;
    noteScaleInteraction(80);
  }

  function onScalePointerUp() {
    if (!pointerScaleWatch) return;
    pointerScaleWatch = false;
    noteScaleInteraction(120);
    // Do not scheduleFit here: a vertical price-axis drag sets autoScale=false
    // and must stick. Horizontal time pan recovers via visibleLogicalRangeChange
    // only while autoScale remains on.
  }

  function onScaleWheel() {
    noteScaleInteraction(180);
  }

  function applyPriceScaleMargins(top, bottom) {
    if (!chart) return false;
    try {
      chart.priceScale("right").applyOptions({
        autoScale: false,
        scaleMargins: { top: top, bottom: bottom },
      });
    } catch (e) {
      return false;
    }
    noteScaleInteraction(250);
    return true;
  }

  function scrollTimeScale(bars) {
    if (!chart) return false;
    const range = chart.timeScale().getVisibleLogicalRange();
    if (!range || range.from == null || range.to == null) return false;
    const delta = Number(bars) || 2;
    chart.timeScale().setVisibleLogicalRange({
      from: range.from + delta,
      to: range.to + delta,
    });
    syncOscLogicalFromMain(chart.timeScale().getVisibleLogicalRange());
    return true;
  }

  function hexAlpha(hex, alpha) {
    const h = (hex || "#c9a227").replace("#", "");
    const r = parseInt(h.slice(0, 2), 16);
    const g = parseInt(h.slice(2, 4), 16);
    const b = parseInt(h.slice(4, 6), 16);
    return "rgba(" + r + "," + g + "," + b + "," + alpha + ")";
  }

  function computeDefaultLogicalRange(barCount) {
    const n = Number(barCount) || 0;
    if (n < 1) return null;
    const last = n - 1;
    const visible = Math.min(DEFAULT_VISIBLE_BARS, n);
    return {
      from: Math.max(0, last - visible + 1),
      to: last + DEFAULT_RIGHT_OFFSET,
    };
  }

  function excludeOverlayFromAutoscale() {
    // Overlay lines stay on the right scale for alignment, but must not drive
    // autoscaling — wrong/stale EMA or LLD points hide candlesticks (esp. HTF).
    return null;
  }

  function applyOverlaySeriesScaleOpts(opts) {
    const out = opts || {};
    out.autoscaleInfoProvider = excludeOverlayFromAutoscale;
    return out;
  }

  function resetPriceScales() {
    if (chart) {
      try {
        chart.applyOptions({
          rightPriceScale: {
            autoScale: true,
            scaleMargins: PRICE_SCALE_MARGINS,
          },
        });
      } catch (err) {
        /* public applyOptions only */
      }
    }
    if (oscChart && lowerVisible) {
      try {
        oscChart.applyOptions({
          rightPriceScale: {
            autoScale: true,
            scaleMargins: OSC_SCALE_MARGINS,
          },
        });
        if (oscTimeBase) {
          oscTimeBase.applyOptions({ autoscaleInfoProvider: lockedScaleProvider() });
        }
        oscSeriesById.forEach(function (series) {
          series.applyOptions({ autoscaleInfoProvider: lockedScaleProvider() });
        });
      } catch (err) {
        /* public applyOptions only */
      }
    }
  }

  function visibleCandlePriceBounds(range) {
    const candles = (lastPayload && lastPayload.candles) || [];
    if (!candles.length) return null;
    let fromIdx = 0;
    let toIdx = candles.length - 1;
    if (range && range.from != null && range.to != null) {
      const n = candles.length;
      let rawFrom = Math.floor(Number(range.from));
      let rawTo = Math.ceil(Number(range.to));
      // After TF switches, a stale logical range from a denser TF can sit far
      // outside the new series — treat that as "use full series".
      if (rawFrom > n - 1 || rawTo < 0) {
        rawFrom = 0;
        rawTo = n - 1;
      }
      fromIdx = Math.max(0, rawFrom);
      toIdx = Math.min(n - 1, rawTo);
      if (toIdx < fromIdx) {
        fromIdx = 0;
        toIdx = n - 1;
      }
    }
    let lo = Infinity;
    let hi = -Infinity;
    let count = 0;
    for (let i = fromIdx; i <= toIdx; i++) {
      const c = candles[i];
      if (!c) continue;
      const low = Number(c.low);
      const high = Number(c.high);
      const close = Number(c.close);
      if (Number.isFinite(low)) lo = Math.min(lo, low);
      if (Number.isFinite(high)) hi = Math.max(hi, high);
      if (Number.isFinite(close)) {
        lo = Math.min(lo, close);
        hi = Math.max(hi, close);
        count += 1;
      }
    }
    if (!count || !(hi >= lo) || !Number.isFinite(lo) || !Number.isFinite(hi)) return null;
    return { lo: lo, hi: hi, n: count, fromIdx: fromIdx, toIdx: toIdx };
  }

  function candlesOutOfPriceView(range) {
    if (!candleSeries) return false;
    const bounds = visibleCandlePriceBounds(range);
    if (!bounds) return false;
    const samples = [bounds.lo, bounds.hi, (bounds.lo + bounds.hi) / 2];
    const plotH = plotBottomY();
    let outside = 0;
    for (let i = 0; i < samples.length; i++) {
      const y = yOf(samples[i]);
      if (y == null || Number.isNaN(y) || y < -8 || y > plotH + 8) outside += 1;
    }
    return outside >= 2;
  }

  function ensureCandleSeriesVisible() {
    if (!candleSeries) return;
    try {
      candleSeries.applyOptions({ visible: true });
    } catch (err) {
      /* ignore */
    }
    const candles = (lastPayload && lastPayload.candles) || [];
    if (candles.length && lastCandleSetCount !== candles.length) {
      applySeriesData(lastPayload);
    }
  }

  function fitPriceScaleToVisibleCandles(force) {
    if (!chart || !candleSeries) return false;
    ensureCandleSeriesVisible();
    let range = null;
    try {
      range = chart.timeScale().getVisibleLogicalRange();
    } catch (err) {
      range = null;
    }
    let autoScaleOn = true;
    try {
      const opts = chart.priceScale("right").options();
      autoScaleOn = !opts || opts.autoScale !== false;
    } catch (err) {
      autoScaleOn = true;
    }
    // Vertical price-axis drag turns autoScale off — never yank it back unless
    // force (reset / TF change / fresh setData / applyDefaultView).
    if (!force && !autoScaleOn) return false;
    const outOfView = candlesOutOfPriceView(range);
    if (!force && !outOfView) return false;
    resetPriceScales();
    return true;
  }

  let fitPriceTimer = null;
  function scheduleFitPriceScaleToCandles(force) {
    if (fitPriceTimer != null) clearTimeout(fitPriceTimer);
    fitPriceTimer = setTimeout(function () {
      fitPriceTimer = null;
      if (pointerScaleWatch && !force) return;
      fitPriceScaleToVisibleCandles(!!force);
    }, force ? 30 : 60);
  }

  function candleTimeExtent() {
    const candles = (lastPayload && lastPayload.candles) || [];
    if (!candles.length) return null;
    const from = Number(candles[0].time);
    const to = Number(candles[candles.length - 1].time);
    if (!Number.isFinite(from) || !Number.isFinite(to) || to < from) return null;
    return { from: from, to: to };
  }

  function clipPointsToCandleExtent(points) {
    const ext = candleTimeExtent();
    const list = points || [];
    if (!ext || !list.length) return list;
    const pad = estimateBarSec((lastPayload && lastPayload.candles) || []) || 60;
    const lo = ext.from - pad;
    const hi = ext.to + pad;
    const out = [];
    for (let i = 0; i < list.length; i++) {
      const p = list[i];
      const t = Number(p && p.time);
      if (!Number.isFinite(t) || t < lo || t > hi) continue;
      out.push(p);
    }
    return out;
  }

  function enforceReplayViewLock() {
    if (!replayViewLock || !chart) return false;
    return applyVisibleTimeRange(replayViewLock.from, replayViewLock.to);
  }

  function setReplayViewLock(lock) {
    if (lock && lock.from != null && lock.to != null) {
      replayViewLock = {
        from: Number(lock.from),
        to: Number(lock.to),
        center: lock.center != null ? Number(lock.center) : null,
      };
      followLive = false;
      return enforceReplayViewLock();
    }
    replayViewLock = null;
    return true;
  }

  function clearReplayViewLock() {
    replayViewLock = null;
    return true;
  }

  function applyDefaultView() {
    if (!chart) return false;
    if (replayViewLock) return enforceReplayViewLock();
    followLive = true;
    resetPriceScales();
    const candles = (lastPayload && lastPayload.candles) || [];
    if (!candles.length) return false;
    // Use unix time range from the candle series — never logical indices.
    // Overlay EMA warmup can expand the timescale far beyond candles; logical
    // ranges based on candle count then land in an empty (EMA-only) window.
    const barSec = estimateBarSec(candles);
    const lastT = Number(candles[candles.length - 1].time);
    const fromIdx = Math.max(0, candles.length - DEFAULT_VISIBLE_BARS);
    const fromT = Number(candles[fromIdx].time);
    let ok = false;
    try {
      chart.timeScale().setVisibleRange({
        from: fromT,
        to: lastT + barSec * DEFAULT_RIGHT_OFFSET,
      });
      ok = true;
    } catch (err) {
      const range = computeDefaultLogicalRange(candles.length);
      if (range) {
        try {
          chart.timeScale().setVisibleLogicalRange(range);
          ok = true;
        } catch (err2) {
          try {
            chart.timeScale().scrollToRealTime();
            ok = true;
          } catch (err3) {
            return false;
          }
        }
      }
    }
    syncOscLogicalFromMain(chart.timeScale().getVisibleLogicalRange());
    layoutOverlays();
    updateSelectedLine();
    scheduleFitPriceScaleToCandles(true);
    return ok;
  }

  function resetView() {
    clearShiftMeasure();
    resetViewCount += 1;
    const candles = (lastPayload && lastPayload.candles) || [];
    const range = computeDefaultLogicalRange(candles.length);
    const ok = applyDefaultView();
    let applied = null;
    try {
      applied = chart ? chart.timeScale().getVisibleLogicalRange() : null;
    } catch (err) {
      applied = null;
    }
    lastResetResult = {
      ok: ok,
      barCount: candles.length,
      range: range,
      applied: applied,
      count: resetViewCount,
    };
    return lastResetResult;
  }

  function clearHandles(rec) {
    if (!rec || !rec.handles) return;
    rec.handles.forEach(function (h) {
      if (h && h.parentNode) h.parentNode.removeChild(h);
    });
    rec.handles = [];
  }

  function syncHandles(rec, points) {
    const layer = overlayLayer();
    if (!layer) return;
    if (!rec.handles) rec.handles = [];
    while (rec.handles.length > points.length) {
      const extra = rec.handles.pop();
      if (extra && extra.parentNode) extra.parentNode.removeChild(extra);
    }
    while (rec.handles.length < points.length) {
      const h = document.createElement("div");
      h.className = "ov-handle";
      layer.appendChild(h);
      rec.handles.push(h);
    }
    points.forEach(function (pt, i) {
      rec.handles[i].style.left = pt.x + "px";
      rec.handles[i].style.top = pt.y + "px";
      rec.handles[i].style.display = "block";
    });
  }

  function destroyOverlayVisual(rec) {
    if (rec.priceLine && candleSeries) {
      try {
        candleSeries.removePriceLine(rec.priceLine);
      } catch (e) {
        /* series may already be gone */
      }
      rec.priceLine = null;
    }
    clearHandles(rec);
    if (rec.el && rec.el.parentNode) {
      rec.el.parentNode.removeChild(rec.el);
    }
    rec.el = null;
  }

  function ensureDom(rec, className) {
    const layer = overlayLayer();
    if (!layer) return null;
    if (rec.el && rec.el.tagName !== "DIV") {
      if (rec.el.parentNode) rec.el.parentNode.removeChild(rec.el);
      rec.el = null;
    }
    if (!rec.el) {
      rec.el = document.createElement("div");
      rec.el.className = className;
      rec.el.setAttribute("data-overlay-id", rec.payload.id);
      layer.appendChild(rec.el);
    } else if (rec.el.className.indexOf(className) === -1) {
      rec.el.className = className;
    }
    return rec.el;
  }

  function hideEl(el) {
    if (el) el.style.display = "none";
  }

  function renderHorizontal(rec) {
    const p = rec.payload;
    if (!candleSeries || p.price == null) return;
    const opts = {
      price: p.price,
      color: p.style.color,
      lineWidth: Math.max(1, Math.round(p.style.width || 1)),
      lineStyle: lineStyleEnum(p.style.line_style),
      axisLabelVisible: true,
      title: p.label_text || "",
    };
    if (!rec.priceLine) {
      rec.priceLine = candleSeries.createPriceLine(opts);
    } else {
      rec.priceLine.applyOptions(opts);
    }
    if (rec.el) hideEl(rec.el);
  }

  function renderVertical(rec) {
    const el = ensureDom(rec, "ov-vline");
    if (!el) return;
    const x = xOf(rec.payload.timestamp);
    if (x == null) {
      hideEl(el);
      return;
    }
    const color = rec.payload.style.color;
    el.style.display = "block";
    el.style.left = x + "px";
    el.style.background = color;
    el.style.opacity = String(rec.payload.style.opacity);
    el.style.width = Math.max(1, rec.payload.style.width || 1) + "px";
    const label = rec.payload.label_text;
    if (label) {
      el.textContent = "";
      const tag = document.createElement("div");
      tag.className = "ov-vline-label";
      tag.textContent = label;
      tag.style.color = color;
      tag.style.background = hexAlpha(color, 0.15);
      tag.style.left = "4px";
      tag.style.top = "8px";
      el.appendChild(tag);
    } else {
      el.textContent = "";
    }
  }

  function renderSegment(rec) {
    const el = ensureDom(rec, "ov-vline");
    if (!el) return;
    const p = rec.payload;
    const x1 = xOf(p.start_timestamp);
    const y1 = yOf(p.start_price);
    const x2 = xOf(p.end_timestamp);
    const y2 = yOf(p.end_price);
    if (x1 == null || y1 == null || x2 == null || y2 == null) {
      hideEl(el);
      return;
    }
    const dx = x2 - x1;
    const dy = y2 - y1;
    const len = Math.sqrt(dx * dx + dy * dy);
    el.style.display = "block";
    el.style.left = x1 + "px";
    el.style.top = y1 + "px";
    el.style.width = len + "px";
    el.style.height = Math.max(1, p.style.width || 1) + "px";
    el.style.bottom = "auto";
    el.style.background = p.style.color;
    el.style.opacity = String(p.style.opacity);
    el.style.transformOrigin = "0 50%";
    el.style.transform = "rotate(" + Math.atan2(dy, dx) + "rad)";
    el.textContent = "";
  }

  function renderZone(rec) {
    const el = ensureDom(rec, "ov-zone");
    if (!el) return;
    const p = rec.payload;
    const plotRight = plotRightX();
    let x1 = xOf(p.start_timestamp);
    let x2 = p.end_timestamp != null ? xOf(p.end_timestamp) : null;
    if (p.extend_right) {
      x2 = plotRight;
    }
    if (x1 == null && x2 == null) {
      hideEl(el);
      return;
    }
    if (x1 == null) x1 = 0;
    if (x2 == null) x2 = p.extend_right ? plotRight : x1;
    const yTop = yOf(p.top_price);
    const yBot = yOf(p.bottom_price);
    if (yTop == null || yBot == null) {
      hideEl(el);
      return;
    }
    const left = Math.min(x1, x2);
    const plotBottom = plotBottomY();
    if (left >= plotRight) {
      hideEl(el);
      return;
    }
    const minSize = p.shape === "ellipse" ? 4 : 1;
    const width = Math.max(minSize, Math.min(Math.abs(x2 - x1), plotRight - left));
    const top = Math.min(yTop, yBot);
    if (top >= plotBottom) {
      hideEl(el);
      return;
    }
    const height = Math.max(minSize, Math.min(Math.abs(yBot - yTop), plotBottom - top));
    const color = p.style.color;
    el.style.display = "block";
    el.style.left = left + "px";
    el.style.top = top + "px";
    el.style.width = width + "px";
    el.style.height = height + "px";
    el.style.borderRadius = p.shape === "ellipse" ? "50%" : "";
    el.style.background = hexAlpha(color, p.opacity != null ? p.opacity : 0.16);
    const bw =
      p.border_width != null
        ? Number(p.border_width)
        : Number((p.style && p.style.width) || 1);
    const bc = p.border_color || color;
    const bAlpha =
      p.metadata && p.metadata.border_alpha != null
        ? Number(p.metadata.border_alpha)
        : 0.7;
    if (p.metadata && p.metadata.projected_after_as_of) {
      el.style.border =
        bw + "px dashed " + hexAlpha(bc, bAlpha);
    } else if (bw <= 0) {
      el.style.border = "none";
    } else {
      el.style.border =
        bw + "px " + (p.border_style || "solid") + " " + hexAlpha(bc, bAlpha);
    }
    el.textContent = "";
    if (p.label_text) {
      const tag = document.createElement("div");
      tag.className = "ov-zone-label";
      tag.textContent = p.label_text;
      tag.style.color = color;
      tag.style.background = hexAlpha(color, 0.18);
      tag.style.left = "4px";
      tag.style.top = "2px";
      el.appendChild(tag);
    }
    // LLD causality tooltip: known_at vs source_timestamp (unix seconds).
    const meta = p.metadata || {};
    if (meta.source === "lld" || meta.source === "lld-cluster") {
      const known = meta.available_at != null ? meta.available_at : (meta.known_at != null ? meta.known_at : p.start_timestamp);
      const src = meta.source_timestamp != null ? meta.source_timestamp : null;
      const cStart = meta.confirmation_bar_start != null ? meta.confirmation_bar_start : null;
      const cEnd = meta.confirmation_bar_end != null ? meta.confirmation_bar_end : null;
      const lines = [];
      if (meta.pool_id) lines.push("pool: " + meta.pool_id);
      if (meta.cluster_id) lines.push("cluster: " + meta.cluster_id);
      if (known != null) lines.push("available_at: " + new Date(Number(known) * 1000).toISOString());
      if (known != null) lines.push("known_at: " + new Date(Number(known) * 1000).toISOString());
      if (cStart != null) lines.push("confirmation_bar_start: " + new Date(Number(cStart) * 1000).toISOString());
      if (cEnd != null) lines.push("confirmation_bar_end: " + new Date(Number(cEnd) * 1000).toISOString());
      if (src != null) lines.push("source_timestamp: " + new Date(Number(src) * 1000).toISOString());
      if (meta.side) lines.push("side: " + meta.side);
      if (meta.strength != null) lines.push("strength: " + meta.strength);
      if (meta.pool_status) lines.push("status: " + meta.pool_status);
      if (meta.closed_bar_confirmed) lines.push("CLOSED-BAR CONFIRMED");
      el.title = lines.join("\n");
    } else {
      el.removeAttribute("title");
    }
  }

  function dasharray(name) {
    if (name === "dashed") return "8 5";
    if (name === "dotted") return "2 4";
    return "";
  }

  function renderArrow(rec) {
    const layer = overlayLayer();
    if (!layer) return;
    const p = rec.payload;
    const x1 = xOf(p.start_timestamp);
    const y1 = yOf(p.start_price);
    const x2 = xOf(p.end_timestamp);
    const y2 = yOf(p.end_price);
    if (x1 == null || y1 == null || x2 == null || y2 == null) {
      if (rec.el) hideEl(rec.el);
      return;
    }
    let el = rec.el;
    if (!el || el.tagName !== "svg") {
      if (el && el.parentNode) el.parentNode.removeChild(el);
      el = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      el.setAttribute("class", "ov-arrow");
      el.style.position = "absolute";
      el.style.left = "0";
      el.style.top = "0";
      el.style.overflow = "visible";
      el.style.pointerEvents = "none";
      el.setAttribute("data-overlay-id", p.id);
      layer.appendChild(el);
      rec.el = el;
    }
    const size = chartSize();
    el.setAttribute("width", String(size.w));
    el.setAttribute("height", String(size.h));
    const color = p.style.color;
    const width = Math.max(1, Number(p.style.width) || 1);
    const opacity = p.style.opacity == null ? 1 : p.style.opacity;
    const dx = x2 - x1;
    const dy = y2 - y1;
    const len = Math.hypot(dx, dy) || 1;
    const head = Math.min(Math.max(8, width * 4), Math.max(4, len * 0.55));
    const ux = dx / len;
    const uy = dy / len;
    const bx = x2 - ux * head;
    const by = y2 - uy * head;
    const px = -uy;
    const py = ux;
    const spread = head * 0.45;
    const p1x = bx + px * spread;
    const p1y = by + py * spread;
    const p2x = bx - px * spread;
    const p2y = by - py * spread;
    el.innerHTML = "";
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line.setAttribute("x1", String(x1));
    line.setAttribute("y1", String(y1));
    line.setAttribute("x2", String(bx));
    line.setAttribute("y2", String(by));
    line.setAttribute("stroke", color);
    line.setAttribute("stroke-width", String(width));
    line.setAttribute("stroke-linecap", "round");
    line.setAttribute("opacity", String(opacity));
    const dash = dasharray(p.style.line_style);
    if (dash) line.setAttribute("stroke-dasharray", dash);
    const poly = document.createElementNS("http://www.w3.org/2000/svg", "polygon");
    poly.setAttribute("points", x2 + "," + y2 + " " + p1x + "," + p1y + " " + p2x + "," + p2y);
    poly.setAttribute("fill", color);
    poly.setAttribute("opacity", String(opacity));
    el.appendChild(line);
    el.appendChild(poly);
    el.style.display = "block";
  }

  function markerShapeEl(shape, color, size) {
    const s = document.createElement("div");
    s.className = "ov-marker-shape " + shape;
    if (shape === "arrow_up") {
      s.style.borderLeft = size + "px solid transparent";
      s.style.borderRight = size + "px solid transparent";
      s.style.borderBottom = size * 1.4 + "px solid " + color;
      s.style.width = "0";
      s.style.height = "0";
    } else if (shape === "arrow_down") {
      s.style.borderLeft = size + "px solid transparent";
      s.style.borderRight = size + "px solid transparent";
      s.style.borderTop = size * 1.4 + "px solid " + color;
      s.style.width = "0";
      s.style.height = "0";
    } else {
      s.style.width = size + "px";
      s.style.height = size + "px";
      s.style.background = color;
    }
    return s;
  }

  function renderMarker(rec) {
    const el = ensureDom(rec, "ov-marker");
    if (!el) return;
    const p = rec.payload;
    const x = xOf(p.timestamp);
    let y = p.price != null ? yOf(p.price) : null;
    if (x == null) {
      hideEl(el);
      return;
    }
    if (y == null) {
      y = 24;
    }
    if (p.position === "above") y -= 12;
    if (p.position === "below") y += 12;
    // Fast path: EZM / research markers can be thousands — avoid rebuilding DOM
    // on every layoutOverlays() tick (forming price / scale watch).
    const shape = p.shape || "circle";
    const color = (p.style && p.style.color) || "#888";
    const size = p.size || 8;
    const text = p.text || "";
    const sig = shape + "|" + color + "|" + size + "|" + text;
    if (rec._mx === x && rec._my === y && rec._msig === sig && el.childNodes.length) {
      el.style.display = "flex";
      return;
    }
    rec._mx = x;
    rec._my = y;
    el.style.display = "flex";
    el.style.left = x + "px";
    el.style.top = y + "px";
    if (rec._msig !== sig || !el.childNodes.length) {
      rec._msig = sig;
      el.textContent = "";
      el.appendChild(markerShapeEl(shape, color, size));
      if (text) {
        const t = document.createElement("div");
        t.className = "ov-marker-text";
        t.textContent = text;
        t.style.color = color;
        t.style.background = hexAlpha(color, 0.18);
        el.appendChild(t);
      }
    }
  }

  function renderLabel(rec) {
    const el = ensureDom(rec, "ov-label");
    if (!el) return;
    const p = rec.payload;
    const x = xOf(p.timestamp);
    const y = yOf(p.price);
    if (x == null || y == null) {
      hideEl(el);
      return;
    }
    el.style.display = "block";
    el.style.top = y + "px";
    el.textContent = p.text;
    el.style.color = "#e6eaf2";
    el.style.background = hexAlpha(p.style.color, p.background_opacity != null ? p.background_opacity : 0.7);
    el.style.fontSize = (p.text_size || 11) + "px";
    const align = p.alignment || "left";
    if (align === "center") {
      el.style.left = x + "px";
      el.style.transform = "translate(-50%, -50%)";
    } else if (align === "right") {
      el.style.left = x + "px";
      el.style.transform = "translate(-100%, -50%)";
    } else {
      el.style.left = x + "px";
      el.style.transform = "translate(6px, -50%)";
    }
  }

  var DEFAULT_POS_RR = 2.0;
  var DEFAULT_POS_STOP_PCT = 0.005;
  var DEFAULT_POS_NOTIONAL = 100.0;

  function posFinitePositive(n) {
    return typeof n === "number" && isFinite(n) && n > 0;
  }

  function normalizePositionPrices(side, entry, stop, target) {
    if (!posFinitePositive(entry)) return null;
    if (!posFinitePositive(stop) || !posFinitePositive(target)) return null;
    if (side === "long") {
      if (stop >= entry) stop = entry * (1 - DEFAULT_POS_STOP_PCT);
      if (target <= entry) {
        var riskL = entry - stop;
        target = entry + Math.max(riskL, entry * DEFAULT_POS_STOP_PCT) * DEFAULT_POS_RR;
      }
    } else {
      if (stop <= entry) stop = entry * (1 + DEFAULT_POS_STOP_PCT);
      if (target >= entry) {
        var riskS = stop - entry;
        target = entry - Math.max(riskS, entry * DEFAULT_POS_STOP_PCT) * DEFAULT_POS_RR;
      }
    }
    if (!posFinitePositive(stop) || !posFinitePositive(target)) return null;
    return { entry: entry, stop: stop, target: target };
  }

  function pricesFromTwoPoints(side, entry, other, rr, stopPct) {
    rr = rr != null ? rr : DEFAULT_POS_RR;
    stopPct = stopPct != null ? stopPct : DEFAULT_POS_STOP_PCT;
    if (!posFinitePositive(entry) || other == null || !isFinite(other)) return null;
    var stop;
    var target;
    if (Math.abs(other - entry) < 1e-12) {
      var risk = entry * stopPct;
      if (side === "long") {
        stop = entry - risk;
        target = entry + risk * rr;
      } else {
        stop = entry + risk;
        target = entry - risk * rr;
      }
      return normalizePositionPrices(side, entry, stop, target);
    }
    if (side === "long") {
      if (other > entry) {
        target = other;
        stop = entry - (target - entry) / rr;
      } else {
        stop = other;
        target = entry + (entry - stop) * rr;
      }
    } else if (other < entry) {
      target = other;
      stop = entry + (entry - target) / rr;
    } else {
      stop = other;
      target = entry - (stop - entry) * rr;
    }
    return normalizePositionPrices(side, entry, stop, target);
  }

  function computePositionStats(side, entry, stop, target, notional) {
    var norm = normalizePositionPrices(side, entry, stop, target);
    if (!norm) return null;
    entry = norm.entry;
    stop = norm.stop;
    target = norm.target;
    notional = posFinitePositive(notional) ? notional : DEFAULT_POS_NOTIONAL;
    var risk = side === "long" ? entry - stop : stop - entry;
    var reward = side === "long" ? target - entry : entry - target;
    var rr = Math.abs(risk) < 1e-12 ? null : reward / risk;
    var qty = Math.abs(entry) < 1e-12 ? null : notional / entry;
    var stopPct = Math.abs(entry) < 1e-12 ? null : (Math.abs(stop - entry) / entry) * 100;
    var targetPct = Math.abs(entry) < 1e-12 ? null : (Math.abs(target - entry) / entry) * 100;
    var profit = qty == null ? null : side === "long" ? qty * (target - entry) : qty * (entry - target);
    var loss = qty == null ? null : side === "long" ? qty * (entry - stop) : qty * (stop - entry);
    return {
      side: side,
      entry: entry,
      stop: stop,
      target: target,
      notional: notional,
      quantity: qty,
      risk: risk,
      reward: reward,
      riskReward: rr,
      stopPercent: stopPct,
      targetPercent: targetPct,
      stopChange: stop - entry,
      targetChange: target - entry,
      profitAtTarget: profit,
      lossAtStop: loss,
    };
  }

  function fmtSignedFixed(value, digits) {
    if (!isFinite(value)) return "—";
    if (Math.abs(value) < 1e-12) return (0).toFixed(digits);
    return (value > 0 ? "+" : "\u2212") + Math.abs(value).toFixed(digits);
  }

  function formatPositionLabels(stats) {
    if (!stats) return { entry: "", stop: "", target: "" };
    var tpPct = fmtSignedFixed(((stats.target - stats.entry) / stats.entry) * 100, 2) + "%";
    var slPct = fmtSignedFixed(((stats.stop - stats.entry) / stats.entry) * 100, 2) + "%";
    var pnl = stats.profitAtTarget != null ? fmtSignedFixed(stats.profitAtTarget, 2) + " USDT" : "";
    var loss = stats.lossAtStop != null ? fmtSignedFixed(-stats.lossAtStop, 2) + " USDT" : "";
    var rr = stats.riskReward == null ? "—" : stats.riskReward.toFixed(2);
    return {
      target: ["Ziel: " + fmt(stats.target), tpPct, pnl].filter(Boolean).join(" \u00b7 "),
      entry:
        "Entry: " +
        fmt(stats.entry) +
        " \u00b7 Größe: " +
        fmt(stats.notional) +
        " USDT \u00b7 R:R " +
        rr,
      stop: ["Stop: " + fmt(stats.stop), slPct, loss].filter(Boolean).join(" \u00b7 "),
    };
  }

  function refreshPositionLabels(p) {
    var stats = computePositionStats(
      p.side,
      Number(p.entry_price),
      Number(p.stop_price),
      Number(p.target_price),
      Number(p.position_notional)
    );
    if (!stats) return;
    var labels = formatPositionLabels(stats);
    p.entry_label = labels.entry;
    p.stop_label = labels.stop;
    p.target_label = labels.target;
  }

  function ensurePositionDom(rec) {
    var el = ensureDom(rec, "ov-position");
    if (!el) return null;
    if (!el.querySelector(".pos-profit")) {
      el.innerHTML =
        '<div class="pos-zone pos-profit"></div>' +
        '<div class="pos-zone pos-loss"></div>' +
        '<div class="pos-entry"></div>' +
        '<div class="pos-box pos-box-tp"></div>' +
        '<div class="pos-box pos-box-entry"></div>' +
        '<div class="pos-box pos-box-sl"></div>';
    }
    return el;
  }

  function renderPosition(rec) {
    var el = ensurePositionDom(rec);
    if (!el) return;
    var p = rec.payload;
    var x1 = xOf(p.start_timestamp);
    var x2 = xOf(p.end_timestamp);
    var yEntry = yOf(p.entry_price);
    var yStop = yOf(p.stop_price);
    var yTarget = yOf(p.target_price);
    if (x1 == null || x2 == null || yEntry == null || yStop == null || yTarget == null) {
      hideEl(el);
      return;
    }
    refreshPositionLabels(p);
    var left = Math.min(x1, x2);
    var width = Math.max(4, Math.abs(x2 - x1));
    var top = Math.min(yStop, yTarget, yEntry);
    var bot = Math.max(yStop, yTarget, yEntry);
    el.style.display = "block";
    el.style.left = left + "px";
    el.style.top = top + "px";
    el.style.width = width + "px";
    el.style.height = Math.max(4, bot - top) + "px";
    var profit = p.profit_color || COLORS.up;
    var loss = p.loss_color || COLORS.down;
    var fill = p.fill_opacity != null ? p.fill_opacity : 0.18;
    var yTpLocal = yTarget - top;
    var ySlLocal = yStop - top;
    var yEnLocal = yEntry - top;
    var profitEl = el.querySelector(".pos-profit");
    var lossEl = el.querySelector(".pos-loss");
    var entryEl = el.querySelector(".pos-entry");
    function placeZone(node, ya, yb, color) {
      var zt = Math.min(ya, yb);
      var zh = Math.max(2, Math.abs(yb - ya));
      node.style.left = "0";
      node.style.width = "100%";
      node.style.top = zt + "px";
      node.style.height = zh + "px";
      node.style.background = hexAlpha(color, fill);
      node.style.border = "1px solid " + hexAlpha(color, 0.55);
    }
    placeZone(profitEl, yEnLocal, yTpLocal, profit);
    placeZone(lossEl, yEnLocal, ySlLocal, loss);
    var lw = Math.max(1, (p.style && p.style.width) || 2);
    entryEl.style.left = "0";
    entryEl.style.width = "100%";
    entryEl.style.top = yEnLocal + "px";
    entryEl.style.height = lw + "px";
    entryEl.style.marginTop = -lw / 2 + "px";
    entryEl.style.background = (p.style && p.style.color) || COLORS.ema20;
    function placeBox(node, text, yLocal, color) {
      node.textContent = text || "";
      node.style.color = "#e6eaf2";
      node.style.background = hexAlpha(color, 0.88);
      node.style.border = "1px solid " + hexAlpha(color, 0.7);
      var bw = Math.min(width - 6, 220);
      node.style.maxWidth = Math.max(64, bw) + "px";
      var nx = 4;
      var ny = yLocal - 8;
      if (ny < 2) ny = yLocal + 4;
      if (ny + 16 > bot - top) ny = Math.max(2, yLocal - 18);
      node.style.left = nx + "px";
      node.style.top = ny + "px";
    }
    placeBox(el.querySelector(".pos-box-tp"), p.target_label, yTpLocal, profit);
    placeBox(el.querySelector(".pos-box-entry"), p.entry_label, yEnLocal, (p.style && p.style.color) || COLORS.ema20);
    placeBox(el.querySelector(".pos-box-sl"), p.stop_label, ySlLocal, loss);
  }

  function positionHandlePoints(p) {
    var x1 = xOf(p.start_timestamp);
    var x2 = xOf(p.end_timestamp);
    var yE = yOf(p.entry_price);
    var yT = yOf(p.target_price);
    var yS = yOf(p.stop_price);
    if (x1 == null || x2 == null || yE == null || yT == null || yS == null) return null;
    var left = Math.min(x1, x2);
    var right = Math.max(x1, x2);
    var midX = (left + right) / 2;
    return [
      { mode: "resize-left", x: left, y: yE },
      { mode: "resize-right", x: right, y: yE },
      { mode: "resize-entry", x: midX, y: yE },
      { mode: "resize-tp", x: midX, y: yT },
      { mode: "resize-sl", x: midX, y: yS },
    ];
  }

  function renderOneOverlay(rec) {
    if (!rec || !rec.payload || rec.payload.visible === false) {
      destroyOverlayVisual(rec);
      return;
    }
    const size = chartSize();
    if (size.w < 16 || size.h < 16 || !chart) {
      return;
    }
    const type = rec.payload.type;
    if (type === "line" && rec.payload.kind === "horizontal") {
      renderHorizontal(rec);
    } else if (type === "line" && rec.payload.kind === "vertical") {
      renderVertical(rec);
    } else if (type === "line" && rec.payload.kind === "arrow") {
      renderArrow(rec);
    } else if (type === "line" && rec.payload.kind === "segment") {
      renderSegment(rec);
    } else if (type === "zone") {
      renderZone(rec);
    } else if (type === "marker") {
      renderMarker(rec);
    } else if (type === "label") {
      renderLabel(rec);
    } else if (type === "position") {
      renderPosition(rec);
    }
    if (rec.el) {
      if (rec.payload.metadata && rec.payload.metadata.selected) {
        rec.el.classList.add("ov-selected");
      } else {
        rec.el.classList.remove("ov-selected");
      }
    }
    const selected = rec.payload.metadata && rec.payload.metadata.selected;
    const p = rec.payload;
    if (selected && p.kind === "arrow") {
      const ax1 = xOf(p.start_timestamp);
      const ay1 = yOf(p.start_price);
      const ax2 = xOf(p.end_timestamp);
      const ay2 = yOf(p.end_price);
      if (ax1 != null && ay1 != null && ax2 != null && ay2 != null) {
        syncHandles(rec, [
          { x: ax1, y: ay1 },
          { x: ax2, y: ay2 },
        ]);
      } else {
        clearHandles(rec);
      }
    } else if (selected && p.type === "position") {
      const pts = positionHandlePoints(p);
      if (pts) {
        syncHandles(
          rec,
          pts.map(function (h) {
            return { x: h.x, y: h.y };
          })
        );
      } else {
        clearHandles(rec);
      }
    } else if (selected && p.shape === "ellipse") {
      const zx1 = xOf(p.start_timestamp);
      const zx2 = p.end_timestamp != null ? xOf(p.end_timestamp) : null;
      const zy1 = yOf(p.top_price);
      const zy2 = yOf(p.bottom_price);
      if (zx1 != null && zx2 != null && zy1 != null && zy2 != null) {
        const left = Math.min(zx1, zx2);
        const right = Math.max(zx1, zx2);
        const top = Math.min(zy1, zy2);
        const bot = Math.max(zy1, zy2);
        syncHandles(rec, [
          { x: left, y: top },
          { x: right, y: top },
          { x: left, y: bot },
          { x: right, y: bot },
        ]);
      } else {
        clearHandles(rec);
      }
    } else {
      clearHandles(rec);
    }
  }

  function vpWidthFrac() {
    if (vpSettings.width === "compact") return 0.18;
    if (vpSettings.width === "wide") return 0.32;
    return 0.25;
  }

  function vpRegion() {
    const size = chartSize();
    const right = plotRightX();
    const w = Math.max(48, Math.min(right * vpWidthFrac(), right * 0.4));
    return { x0: right - w, x1: right, w: w, h: size.h };
  }

  function setVolumeProfile(payload, settings) {
    vpPayload = payload || null;
    if (settings) {
      vpSettings = Object.assign({}, vpSettings, settings);
    }
    drawVolumeProfile();
    return true;
  }

  function clearVolumeProfile() {
    vpPayload = null;
    vpHoverIndex = -1;
    drawVolumeProfile();
    const tip = $("vp-tooltip");
    if (tip) {
      tip.hidden = true;
      tip.textContent = "";
    }
    const badge = $("vp-badge");
    if (badge) {
      badge.hidden = true;
      badge.textContent = "";
    }
    return true;
  }

  function obpWidthFrac() {
    if (obpSettings.width === "compact") return 0.14;
    if (obpSettings.width === "wide") return 0.26;
    return 0.2;
  }

  function obpRegion() {
    const size = chartSize();
    const right = plotRightX();
    const w = Math.max(40, Math.min(right * obpWidthFrac(), right * 0.35));
    // Sit just left of Volume Profile when both are on, else flush right.
    let x1 = right;
    if (vpSettings.enabled && vpPayload && vpPayload.bins && vpPayload.bins.length) {
      const vp = vpRegion();
      x1 = Math.max(w + 8, vp.x0 - 4);
    }
    return { x0: x1 - w, x1: x1, w: w, h: size.h };
  }

  function setOrderbookProfile(payload, settings) {
    obpPayload = payload || null;
    if (settings) {
      obpSettings = Object.assign({}, obpSettings, settings);
    }
    drawOrderbookProfile();
    return true;
  }

  function clearOrderbookProfile() {
    obpPayload = null;
    obpHoverIndex = -1;
    drawOrderbookProfile();
    const tip = $("obp-tooltip");
    if (tip) {
      tip.hidden = true;
      tip.textContent = "";
    }
    const badge = $("obp-badge");
    if (badge) {
      badge.hidden = true;
      badge.textContent = "";
    }
    return true;
  }

  function oblBarLength(notional, maxNotional, scale, panelWidth) {
    if (!(notional > 0) || !(maxNotional > 0) || !(panelWidth > 0)) return 0;
    const ratio = notional / maxNotional;
    let frac;
    const mode = String(scale || "sqrt").toLowerCase();
    if (mode === "linear") frac = ratio;
    else if (mode === "log") frac = Math.log1p(ratio * 9) / Math.log1p(9);
    else frac = Math.sqrt(ratio);
    if (!Number.isFinite(frac) || frac < 0) frac = 0;
    if (frac > 1) frac = 1;
    return frac * panelWidth * 0.95;
  }

  function oblAggregate(levels, bucketSize, side) {
    const sideL = String(side || "").toLowerCase();
    if (!(bucketSize > 0) || !Number.isFinite(bucketSize)) {
      return (levels || []).filter(function (l) { return l && l.side === sideL; }).map(function (l) {
        return Object.assign({}, l);
      });
    }
    const buckets = {};
    (levels || []).forEach(function (lvl) {
      if (!lvl || lvl.side !== sideL) return;
      const p = Number(lvl.price);
      const s = Number(lvl.size);
      if (!Number.isFinite(p) || !Number.isFinite(s) || s < 0) return;
      const idx = Math.floor(p / bucketSize + 1e-12);
      const low = idx * bucketSize;
      const high = low + bucketSize;
      if (!buckets[idx]) {
        buckets[idx] = {
          price: p,
          size: s,
          side: sideL,
          bucket_low: low,
          bucket_high: high,
          raw_level_count: 1,
          _notional: p * s,
        };
      } else {
        buckets[idx].size += s;
        buckets[idx].raw_level_count += 1;
        buckets[idx]._notional += p * s;
        buckets[idx].price = buckets[idx].size > 0 ? buckets[idx]._notional / buckets[idx].size : p;
      }
    });
    const out = Object.keys(buckets).map(function (k) {
      const b = buckets[k];
      delete b._notional;
      return b;
    });
    out.sort(function (a, b) {
      return sideL === "bid" ? b.price - a.price : a.price - b.price;
    });
    return out;
  }

  function oblAutoBucket(tick, low, high) {
    const t = tick > 0 && Number.isFinite(tick) ? tick : 1e-4;
    if (!(high > low) || !Number.isFinite(low) || !Number.isFinite(high)) return t * 10;
    const span = high - low;
    const raw = span / 80;
    const n = Math.max(1, Math.ceil(raw / t));
    return n * t;
  }

  function oblFilterVisible(levels, visLow, visHigh) {
    if (!(visHigh > visLow) || !Number.isFinite(visLow) || !Number.isFinite(visHigh)) {
      return { visible: levels.slice(), above: 0, below: 0 };
    }
    const visible = [];
    let above = 0;
    let below = 0;
    (levels || []).forEach(function (lvl) {
      const p = Number(lvl.price);
      if (!Number.isFinite(p)) return;
      if (p > visHigh) above += 1;
      else if (p < visLow) below += 1;
      else visible.push(lvl);
    });
    return { visible: visible, above: above, below: below };
  }

  function getChartLivePrice() {
    if (lastPayload && lastPayload.candles && lastPayload.candles.length) {
      const c = Number(lastPayload.candles[lastPayload.candles.length - 1].close);
      if (Number.isFinite(c)) return c;
    }
    return null;
  }

  function oblSyncTolerance(tick, bestBid, bestAsk, mid) {
    const t = tick > 0 && Number.isFinite(tick) ? tick : 0.1;
    let spread = 0;
    if (Number.isFinite(bestBid) && Number.isFinite(bestAsk) && bestAsk > bestBid) {
      spread = bestAsk - bestBid;
    }
    const midEps = mid > 0 && Number.isFinite(mid) ? mid * 1e-5 : 0;
    return Math.max(2 * t, 0.5 * spread, midEps);
  }

  function oblBookChartSyncStatus(chartPrice, bestBid, bestAsk, mid, tick, freshnessMs) {
    const bookMid = mid != null && Number.isFinite(mid)
      ? mid
      : (Number.isFinite(bestBid) && Number.isFinite(bestAsk) ? (bestBid + bestAsk) / 2 : null);
    const tol = oblSyncTolerance(tick, bestBid, bestAsk, bookMid);
    let delta = null;
    let deltaPct = null;
    if (chartPrice != null && bookMid != null && Number.isFinite(chartPrice) && Number.isFinite(bookMid)) {
      delta = chartPrice - bookMid;
      if (bookMid !== 0) deltaPct = (delta / bookMid) * 100;
    }
    let fresh = "unknown";
    if (freshnessMs == null || freshnessMs < 0) fresh = "unknown";
    else if (freshnessMs <= 15000) fresh = "fresh";
    else if (freshnessMs <= 180000) fresh = "delayed";
    else fresh = "stale";

    let state = "UNKNOWN";
    if (fresh === "stale") state = "STALE";
    else if (chartPrice == null || !Number.isFinite(chartPrice)) state = "UNKNOWN";
    else if (!Number.isFinite(bestBid) || !Number.isFinite(bestAsk)) state = "UNKNOWN";
    else if (chartPrice > bestAsk + tol) state = "DESYNC_UP";
    else if (chartPrice < bestBid - tol) state = "DESYNC_DOWN";
    else if (fresh === "delayed") state = "DELAYED";
    else if (fresh === "unknown") state = "UNKNOWN";
    else state = "SYNC";

    return {
      sync_state: state,
      chart_price: chartPrice,
      book_mid: bookMid,
      best_bid: bestBid,
      best_ask: bestAsk,
      tolerance: tol,
      delta: delta,
      delta_pct: deltaPct,
      freshness_ms: freshnessMs,
      misleading_as_live: state === "DESYNC_UP" || state === "DESYNC_DOWN" || state === "STALE",
    };
  }

  function oblAuditLevels(bids, asks) {
    const bidPrices = (bids || []).map(function (b) { return Number(b.price); }).filter(Number.isFinite);
    const askPrices = (asks || []).map(function (a) { return Number(a.price); }).filter(Number.isFinite);
    let sortedBids = true;
    for (let i = 0; i < bidPrices.length - 1; i++) {
      if (bidPrices[i] < bidPrices[i + 1]) { sortedBids = false; break; }
    }
    let sortedAsks = true;
    for (let i = 0; i < askPrices.length - 1; i++) {
      if (askPrices[i] > askPrices[i + 1]) { sortedAsks = false; break; }
    }
    const bestBid = bidPrices.length ? bidPrices[0] : null;
    const bestAsk = askPrices.length ? askPrices[0] : null;
    const mid = bestBid != null && bestAsk != null ? (bestBid + bestAsk) / 2 : null;
    const uncrossed = bestBid != null && bestAsk != null && bestBid < bestAsk
      && bidPrices.every(function (p) { return p <= bestBid + 1e-12; })
      && askPrices.every(function (p) { return p >= bestAsk - 1e-12; });
    return {
      ok: sortedBids && sortedAsks && uncrossed,
      sorted_bids: sortedBids,
      sorted_asks: sortedAsks,
      uncrossed: uncrossed,
      best_bid: bestBid,
      best_ask: bestAsk,
      mid: mid,
      bid_count: bidPrices.length,
      ask_count: askPrices.length,
      lowest_bid: bidPrices.length ? bidPrices[bidPrices.length - 1] : null,
      highest_ask: askPrices.length ? askPrices[askPrices.length - 1] : null,
    };
  }

  function applyObLevelsPanelLayout() {
    const panel = $("ob-levels-panel");
    if (!panel) return;
    const enabled = !!(oblSettings && oblSettings.enabled);
    panel.hidden = !enabled;
    const w = Math.max(100, Math.min(220, Number(oblSettings.width_px) || 140));
    panel.style.flexBasis = w + "px";
    panel.style.width = w + "px";
    // Chart ResizeObserver on #chart will fire after layout.
    if (typeof resize === "function") {
      try { resize(); } catch (e) {}
    }
  }

  function setOrderbookLevels(payload, settings) {
    if (settings) {
      oblSettings = Object.assign({}, oblSettings, settings);
    }
    oblPayload = payload || null;
    applyObLevelsPanelLayout();
    drawOrderbookLevels();
    return true;
  }

  function clearOrderbookLevels() {
    oblPayload = null;
    oblHitBars = [];
    oblHover = null;
    const tip = $("ob-levels-tooltip");
    if (tip) {
      tip.hidden = true;
      tip.textContent = "";
    }
    drawOrderbookLevels();
    return true;
  }

  function ensureObLevelsListeners() {
    if (oblListenersBound) return;
    const canvas = $("ob-levels-canvas");
    if (!canvas) return;
    oblListenersBound = true;
    canvas.addEventListener("mousemove", function (ev) {
      const rect = canvas.getBoundingClientRect();
      const y = ev.clientY - rect.top;
      let hit = null;
      for (let i = 0; i < oblHitBars.length; i++) {
        const b = oblHitBars[i];
        if (Math.abs(y - b.y) <= Math.max(3, b.h * 0.5 + 1)) {
          hit = b;
          break;
        }
      }
      oblHover = hit;
      const tip = $("ob-levels-tooltip");
      if (!tip) return;
      if (!hit) {
        tip.hidden = true;
        tip.textContent = "";
        return;
      }
      const mid = oblPayload && oblPayload.mid != null ? Number(oblPayload.mid) : null;
      const distBps = mid && mid > 0 ? ((hit.price - mid) / mid) * 10000 : null;
      let text =
        "Side " + String(hit.side).toUpperCase() +
        "\nPrice " + hit.price +
        "\nSize " + hit.size +
        "\nNotional " + hit.notional.toFixed(2) +
        (distBps != null && Number.isFinite(distBps) ? ("\nDist mid " + distBps.toFixed(2) + " bps") : "") +
        (oblPayload && oblPayload.timestamp_utc ? ("\nBook " + oblPayload.timestamp_utc) : "");
      if (hit.raw_level_count != null) {
        text +=
          "\nBucket " + hit.bucket_low + " – " + hit.bucket_high +
          "\nRaw levels " + hit.raw_level_count;
      }
      tip.textContent = text;
      tip.hidden = false;
      const tx = Math.min(rect.width - 8, Math.max(4, ev.clientX - rect.left + 10));
      const ty = Math.min(rect.height - 8, Math.max(4, y + 12));
      tip.style.left = tx + "px";
      tip.style.top = ty + "px";
    });
    canvas.addEventListener("mouseleave", function () {
      oblHover = null;
      const tip = $("ob-levels-tooltip");
      if (tip) {
        tip.hidden = true;
        tip.textContent = "";
      }
    });
  }

  function drawOrderbookLevels() {
    ensureObLevelsListeners();
    const panel = $("ob-levels-panel");
    const canvas = $("ob-levels-canvas");
    const headerMeta = $("ob-levels-meta");
    const headerFresh = $("ob-levels-fresh");
    const headerTitle = $("ob-levels-title");
    if (!panel || !canvas) return;
    if (!oblSettings.enabled) {
      panel.hidden = true;
      oblHitBars = [];
      return;
    }
    panel.hidden = false;
    const chartEl = $("chart");
    const rect = canvas.getBoundingClientRect();
    const chartRect = chartEl ? chartEl.getBoundingClientRect() : null;
    const w = Math.max(1, Math.floor(rect.width));
    const h = Math.max(1, Math.floor(rect.height));
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, Math.floor(w * dpr));
    canvas.height = Math.max(1, Math.floor(h * dpr));
    canvas.style.width = w + "px";
    canvas.style.height = h + "px";
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    // Canvas fills the full panel (header overlays). Chart and panel share height,
    // so priceToCoordinate maps 1:1 without a synthetic pixel offset.
    const headerOffsetPx = chartRect ? Math.round(rect.top - chartRect.top) : 0;
    const heightDeltaPx = chartRect ? Math.round(rect.height - chartRect.height) : 0;

    if (!oblPayload || !candleSeries) {
      oblHitBars = [];
      panel.classList.remove("stale-book", "desync-book");
      return;
    }

    const depth = oblPayload.depth != null ? Number(oblPayload.depth) : 200;
    const depthLabel = depth === 1000 ? "OB1000" : "OB200";
    if (headerTitle) headerTitle.textContent = depthLabel;

    const rawBids = (oblPayload.bids || []).slice();
    const rawAsks = (oblPayload.asks || []).slice();
    const audit = oblAuditLevels(rawBids, rawAsks);
    const bestBid = audit.best_bid != null ? audit.best_bid : (oblPayload.best_bid != null ? Number(oblPayload.best_bid) : null);
    const bestAsk = audit.best_ask != null ? audit.best_ask : (oblPayload.best_ask != null ? Number(oblPayload.best_ask) : null);
    const bookMid = audit.mid != null ? audit.mid : (oblPayload.mid != null ? Number(oblPayload.mid) : null);
    const tick = Number(oblPayload.tick_size) || 0.0001;
    const chartPrice = getChartLivePrice();
    const sync = oblBookChartSyncStatus(
      chartPrice,
      bestBid,
      bestAsk,
      bookMid,
      tick,
      oblPayload.freshness_ms != null ? Number(oblPayload.freshness_ms) : null
    );
    oblPayload._audit = audit;
    oblPayload._sync = sync;
    oblPayload._geometry = {
      canvas_css_h: h,
      canvas_css_w: w,
      canvas_pixel_h: canvas.height,
      canvas_pixel_w: canvas.width,
      dpr: dpr,
      header_offset_px: headerOffsetPx,
      height_delta_px: heightDeltaPx,
      chart_h: chartRect ? Math.round(chartRect.height) : null,
    };

    const desync = !!sync.misleading_as_live;
    panel.classList.toggle("stale-book", sync.sync_state === "STALE");
    panel.classList.toggle("desync-book", desync && sync.sync_state !== "STALE");

    let visLow = null;
    let visHigh = null;
    try {
      const visible = candleSeries.priceScale && candleSeries.priceScale().getVisibleRange
        ? candleSeries.priceScale().getVisibleRange()
        : (chart && chart.priceScale("right").getVisibleRange && chart.priceScale("right").getVisibleRange());
      if (visible && visible.from != null && visible.to != null) {
        visLow = Math.min(Number(visible.from), Number(visible.to));
        visHigh = Math.max(Number(visible.from), Number(visible.to));
      }
    } catch (e) {}

    // Aggregate sides separately on the full book, then filter by visible range.
    let bids = rawBids;
    let asks = rawAsks;
    let bucketSize = null;
    if (String(oblSettings.mode) === "aggregated") {
      bucketSize = oblAutoBucket(tick, visLow, visHigh);
      bids = oblAggregate(bids, bucketSize, "bid");
      asks = oblAggregate(asks, bucketSize, "ask");
    }
    const bidVis = oblFilterVisible(bids, visLow, visHigh);
    const askVis = oblFilterVisible(asks, visLow, visHigh);
    bids = bidVis.visible;
    asks = askVis.visible;

    const ageSec = oblPayload.freshness_ms != null ? Math.round(Number(oblPayload.freshness_ms) / 1000) : null;
    const covLow = audit.lowest_bid;
    const covHigh = audit.highest_ask;
    let covBelowPct = null;
    let covAbovePct = null;
    if (bookMid && bookMid > 0 && covLow != null) covBelowPct = ((bookMid - covLow) / bookMid) * 100;
    if (bookMid && bookMid > 0 && covHigh != null) covAbovePct = ((covHigh - bookMid) / bookMid) * 100;

    if (headerMeta) {
      const mode = String(oblSettings.mode || "aggregated");
      const parts = [
        depthLabel,
        mode === "raw" ? "Raw" : "Agg",
        "B " + bidVis.visible.length + "/" + (bidVis.visible.length + bidVis.above + bidVis.below),
        "A " + askVis.visible.length + "/" + (askVis.visible.length + askVis.above + askVis.below),
      ];
      if (covLow != null && covHigh != null) {
        parts.push("cov " + covLow.toFixed(1) + "…" + covHigh.toFixed(1));
      }
      if (covBelowPct != null && covAbovePct != null) {
        parts.push((-covBelowPct).toFixed(2) + "% / +" + covAbovePct.toFixed(2) + "%");
      }
      if (bookMid != null) parts.push("mid " + bookMid.toFixed(2));
      if (sync.delta != null) parts.push("Δ " + (sync.delta >= 0 ? "+" : "") + sync.delta.toFixed(2));
      if (bucketSize != null) parts.push("Δpx " + bucketSize);
      headerMeta.textContent = parts.join(" · ");
    }
    if (headerFresh) {
      if (oblPayload.ui_state && depth === 1000 && !desync) {
        const labels = {
          DISABLED: "OB1000 DISABLED",
          STARTING: "OB1000 STARTING",
          LIVE: "OB1000 LIVE",
          DELAYED: "OB1000 DELAYED",
          STALE: "OB1000 STALE",
          CAPACITY: "OB1000 CAPACITY",
          OFFLINE: "OB1000 COLLECTOR OFFLINE",
          NO_DATA: "OB1000 NO DATA",
        };
        headerFresh.className = "ob-levels-fresh " + String(oblPayload.ui_state).toLowerCase();
        headerFresh.textContent = labels[oblPayload.ui_state] || String(oblPayload.ui_state);
      } else {
        const st = sync.sync_state;
        headerFresh.className = "ob-levels-fresh " + String(st).toLowerCase();
        if (st === "DESYNC_UP") {
          headerFresh.textContent = "DESYNC ↑" + (ageSec != null ? (" " + ageSec + "s") : "");
        } else if (st === "DESYNC_DOWN") {
          headerFresh.textContent = "DESYNC ↓" + (ageSec != null ? (" " + ageSec + "s") : "");
        } else if (st === "STALE") {
          headerFresh.textContent = "STALE" + (ageSec != null ? (" " + ageSec + "s") : "");
        } else if (st === "DELAYED") {
          headerFresh.textContent = "delayed " + (ageSec != null ? ageSec + "s" : "");
        } else if (st === "SYNC") {
          headerFresh.textContent = ageSec != null ? (ageSec + "s") : "sync";
        } else {
          headerFresh.textContent = "no data";
        }
      }
    }

    // Desync/stale: do not paint as a normal live book (no recolor, no move).
    if (desync) {
      oblHitBars = [];
      // Coverage indicators only — no misleading live bars.
      if (bidVis.above + askVis.above > 0) {
        ctx.fillStyle = "rgba(240, 97, 109, 0.55)";
        ctx.beginPath();
        ctx.moveTo(w * 0.5, 4);
        ctx.lineTo(w * 0.5 - 6, 14);
        ctx.lineTo(w * 0.5 + 6, 14);
        ctx.closePath();
        ctx.fill();
      }
      if (bidVis.below + askVis.below > 0) {
        ctx.fillStyle = "rgba(61, 204, 145, 0.55)";
        ctx.beginPath();
        ctx.moveTo(w * 0.5, h - 4);
        ctx.lineTo(w * 0.5 - 6, h - 14);
        ctx.lineTo(w * 0.5 + 6, h - 14);
        ctx.closePath();
        ctx.fill();
      }
      return;
    }

    const levels = bids.concat(asks);
    const notionals = [];
    levels.forEach(function (lvl) {
      const n = Number(lvl.price) * Number(lvl.size);
      if (Number.isFinite(n) && n > 0) notionals.push(n);
    });
    let maxN = 0;
    notionals.forEach(function (n) { if (n > maxN) maxN = n; });

    oblHitBars = [];
    const barH = Math.max(1.5, Math.min(4, h / 220));
    function paintSide(sideLevels, color, bestPrice) {
      sideLevels.forEach(function (lvl) {
        const y = yOf(lvl.price);
        if (y == null || !Number.isFinite(y) || y < 0 || y > h) return;
        const notional = Number(lvl.price) * Number(lvl.size);
        const len = oblBarLength(notional, maxN, oblSettings.scale, w);
        const isBest = bestPrice != null && Math.abs(Number(lvl.price) - Number(bestPrice)) < tick * 0.51;
        ctx.fillStyle = color;
        ctx.globalAlpha = isBest ? 0.95 : 0.55;
        ctx.fillRect(0, y - barH / 2, len, barH);
        if (isBest) {
          ctx.globalAlpha = 1;
          ctx.fillRect(0, y - 0.5, Math.max(len, 8), 1.5);
        }
        oblHitBars.push({
          y: y,
          h: barH,
          price: lvl.price,
          size: lvl.size,
          side: lvl.side,
          notional: notional,
          bucket_low: lvl.bucket_low,
          bucket_high: lvl.bucket_high,
          raw_level_count: lvl.raw_level_count,
        });
      });
      ctx.globalAlpha = 1;
    }

    // Ask = red/pink above mid; Bid = green/teal below mid. Never recolor by chart price.
    paintSide(asks, "rgba(240, 97, 109, 0.9)", bestAsk);
    paintSide(bids, "rgba(61, 204, 145, 0.9)", bestBid);

    if (bookMid != null) {
      const ym = yOf(bookMid);
      if (ym != null && ym >= 0 && ym <= h) {
        ctx.strokeStyle = "rgba(139, 147, 167, 0.55)";
        ctx.setLineDash([3, 3]);
        ctx.beginPath();
        ctx.moveTo(0, ym);
        ctx.lineTo(w, ym);
        ctx.stroke();
        ctx.setLineDash([]);
      }
    }

    // Outside-viewport coverage indicators (no bars on wrong prices).
    if (askVis.above + bidVis.above > 0) {
      ctx.fillStyle = "rgba(240, 97, 109, 0.7)";
      ctx.beginPath();
      ctx.moveTo(w - 10, 6);
      ctx.lineTo(w - 4, 14);
      ctx.lineTo(w - 16, 14);
      ctx.closePath();
      ctx.fill();
    }
    if (askVis.below + bidVis.below > 0) {
      ctx.fillStyle = "rgba(61, 204, 145, 0.7)";
      ctx.beginPath();
      ctx.moveTo(w - 10, h - 6);
      ctx.lineTo(w - 4, h - 14);
      ctx.lineTo(w - 16, h - 14);
      ctx.closePath();
      ctx.fill();
    }
  }

  function debugOrderbookLevels() {
    return {
      payload: oblPayload,
      settings: oblSettings,
      geometry: oblPayload && oblPayload._geometry,
      audit: oblPayload && oblPayload._audit,
      sync: oblPayload && oblPayload._sync,
      chart_price: getChartLivePrice(),
    };
  }

  function drawOrderbookProfile() {
    const canvas = $("obp-overlay");
    if (!canvas) return;
    const size = chartSize();
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, Math.floor(size.w * dpr));
    canvas.height = Math.max(1, Math.floor(size.h * dpr));
    canvas.style.width = size.w + "px";
    canvas.style.height = size.h + "px";
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, size.w, size.h);
    const badge = $("obp-badge");
    if (!obpSettings.enabled || !obpPayload || !obpPayload.bars || !obpPayload.bars.length) {
      if (badge) {
        const warn = obpPayload && obpPayload.warning;
        if (obpSettings.enabled && warn) {
          badge.hidden = false;
          badge.textContent = "OB Profile · " + warn;
        } else {
          badge.hidden = true;
          badge.textContent = "";
        }
      }
      return;
    }
    const bars = obpPayload.bars;
    const region = obpRegion();
    let maxVal = 0;
    for (let i = 0; i < bars.length; i++) {
      const v = Number(bars[i].value) || 0;
      if (v > maxVal) maxVal = v;
    }
    if (maxVal <= 0) maxVal = 1;
    const barH = 5;
    ctx.save();
    ctx.beginPath();
    ctx.rect(0, 0, region.x1, size.h);
    ctx.clip();
    for (let i = 0; i < bars.length; i++) {
      const b = bars[i];
      const y = yOf(b.price);
      if (y == null) continue;
      const w = Math.max(2, (Number(b.value) / maxVal) * region.w);
      const y0 = y - barH / 2;
      const isBid = String(b.side).toUpperCase() === "BID";
      const alpha = i === obpHoverIndex ? 0.92 : (b.carried_forward ? 0.45 : 0.72);
      ctx.fillStyle = isBid
        ? "rgba(61, 204, 145, " + alpha + ")"
        : "rgba(240, 97, 109, " + alpha + ")";
      ctx.fillRect(region.x1 - w, y0, w, barH);
      if (i === obpHoverIndex) {
        ctx.strokeStyle = "rgba(255,255,255,0.65)";
        ctx.strokeRect(region.x1 - w, y0, w, barH);
      }
    }
    // Legend ticks near top of region
    ctx.fillStyle = "rgba(61, 204, 145, 0.9)";
    ctx.fillRect(region.x0 + 2, 6, 10, 4);
    ctx.fillStyle = "rgba(240, 97, 109, 0.9)";
    ctx.fillRect(region.x0 + 2, 14, 10, 4);
    ctx.fillStyle = COLORS.muted;
    ctx.font = "10px Inter, Segoe UI, sans-serif";
    ctx.fillText("Bid Wall", region.x0 + 16, 11);
    ctx.fillText("Ask Wall", region.x0 + 16, 19);
    ctx.restore();
    if (badge) {
      badge.hidden = false;
      const label = (obpPayload && obpPayload.label) || "Orderbook Walls";
      const n = bars.length;
      badge.textContent = label + " · " + n;
    }
  }

  function obpBarAt(x, y) {
    if (!obpSettings.enabled || !obpPayload || !obpPayload.bars) return -1;
    const region = obpRegion();
    if (x < region.x0 - 2 || x > region.x1 + 2) return -1;
    const bars = obpPayload.bars;
    let best = -1;
    let bestDist = 8;
    for (let i = 0; i < bars.length; i++) {
      const py = yOf(bars[i].price);
      if (py == null) continue;
      const d = Math.abs(py - y);
      if (d < bestDist) {
        bestDist = d;
        best = i;
      }
    }
    return best;
  }

  function updateOrderbookProfileHover(x, y) {
    const tip = $("obp-tooltip");
    const idx = obpBarAt(x, y);
    if (idx !== obpHoverIndex) {
      obpHoverIndex = idx;
      drawOrderbookProfile();
    }
    if (!tip) return;
    if (idx < 0 || !obpPayload || !obpPayload.bars[idx]) {
      tip.hidden = true;
      return;
    }
    const b = obpPayload.bars[idx];
    const fmtN = function (n, dig) {
      const v = Number(n);
      if (!Number.isFinite(v)) return "—";
      const d = dig != null ? dig : (Math.abs(v) >= 1000 ? 1 : 6);
      return v.toFixed(d);
    };
    const ts = b.timestamp != null ? new Date(Number(b.timestamp) * 1000).toISOString().replace(".000Z", "Z") : "—";
    tip.hidden = false;
    tip.style.left = Math.max(8, x - 190) + "px";
    tip.style.top = Math.max(8, y - 8) + "px";
    tip.innerHTML =
      "<div><strong>Current Orderbook Walls</strong></div>" +
      "<div>" + String(b.side || "") + " Wall (latest snapshot)</div>" +
      "<div>Price " + fmtN(b.price) + "</div>" +
      "<div>Notional " + fmtN(b.value, 2) + " (" + (b.value_type || "notional_quote") + ")</div>" +
      "<div>Qty " + fmtN(b.qty, 2) + " (" + (b.qty_unit || "base") + ")</div>" +
      "<div>Mid " + fmtN(b.reference_price) + "</div>" +
      "<div>Dist " + fmtN(b.distance_abs) + " · " + fmtN(b.distance_bps, 2) + " bps</div>" +
      "<div>UTC " + ts + "</div>" +
      "<div>carried_forward: " + (b.carried_forward ? "true" : "false") + "</div>" +
      "<div>quality: " + (b.quality_flags || "—") + "</div>";
  }

  function getVisibleTimeRange() {
    if (!chart) return null;
    try {
      const range = chart.timeScale().getVisibleRange();
      if (!range || range.from == null || range.to == null) return null;
      const candles = (lastPayload && lastPayload.candles) || [];
      let first = null;
      let last = null;
      const from = Number(range.from);
      const to = Number(range.to);
      for (let i = 0; i < candles.length; i++) {
        const t = Number(candles[i].time);
        if (t >= from && t <= to) {
          if (first == null) first = t;
          last = t;
        }
      }
      if (first == null && candles.length) {
        first = Number(candles[0].time);
        last = Number(candles[candles.length - 1].time);
      }
      return {
        from: from,
        to: to,
        firstCandle: first,
        lastCandle: last,
      };
    } catch (e) {
      return null;
    }
  }

  function applyVisibleTimeRange(fromUnix, toUnix) {
    if (!chart) return false;
    const from = Number(fromUnix);
    const to = Number(toUnix);
    if (!(to > from)) return false;
    try {
      followLive = false;
      chart.timeScale().setVisibleRange({ from: from, to: to });
      syncOscLogicalFromMain(chart.timeScale().getVisibleLogicalRange());
      layoutOverlays();
      updateSelectedLine();
      scheduleResearchMarkersPaint(true);
      scheduleFitPriceScaleToCandles(true);
      return true;
    } catch (e) {
      return false;
    }
  }

  function setFollowLive(on) {
    followLive = !!on;
    return followLive;
  }

  function setVisibleTimeRange(fromUnix, toUnix) {
    if (programmaticNavDepth > 0) return applyVisibleTimeRange(fromUnix, toUnix);
    return runProgrammaticNav(function () {
      return applyVisibleTimeRange(fromUnix, toUnix);
    });
  }

  function estimateBarSec(candles) {
    const rows = candles || (lastPayload && lastPayload.candles) || [];
    if (rows.length < 2) return 60;
    const d = Number(rows[rows.length - 1].time) - Number(rows[rows.length - 2].time);
    return Number.isFinite(d) && d > 0 ? d : 60;
  }

  function focusOnTime(centerUnix, padSec) {
    if (!chart) return false;
    const candles = (lastPayload && lastPayload.candles) || [];
    if (!candles.length) return false;
    return runProgrammaticNav(function () {
      const target = Math.floor(Number(centerUnix));
      const firstT = Number(candles[0].time);
      const lastT = Number(candles[candles.length - 1].time);
      if (!Number.isFinite(target) || target < firstT || target > lastT) return false;
      const center = snapUnixToBar(target);
      if (center < firstT || center > lastT) return false;
      const pad = Math.max(Number(padSec) || 3600, estimateBarSec(candles) * 10);
      const wantFrom = Math.max(firstT, center - pad);
      const wantTo = Math.min(lastT, center + pad);
      if (wantTo <= wantFrom) return false;
      if (applyVisibleTimeRange(wantFrom, wantTo)) return true;
      let idx = 0;
      for (let i = 0; i < candles.length; i++) {
        if (Number(candles[i].time) <= center) idx = i;
        else break;
      }
      const barSec = estimateBarSec(candles);
      const padBars = Math.max(30, Math.ceil(pad / barSec));
      const fromIdx = Math.max(0, idx - padBars);
      const toIdx = Math.min(candles.length - 1, idx + padBars);
      try {
        chart.timeScale().setVisibleLogicalRange({
          from: fromIdx,
          to: toIdx + DEFAULT_RIGHT_OFFSET,
        });
        syncOscLogicalFromMain(chart.timeScale().getVisibleLogicalRange());
        layoutOverlays();
        updateSelectedLine();
        return true;
      } catch (e) {
        return false;
      }
    });
  }

  function drawVolumeProfile() {
    const canvas = $("vp-overlay");
    if (!canvas) return;
    const size = chartSize();
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, Math.floor(size.w * dpr));
    canvas.height = Math.max(1, Math.floor(size.h * dpr));
    canvas.style.width = size.w + "px";
    canvas.style.height = size.h + "px";
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, size.w, size.h);
    const badge = $("vp-badge");
    if (!vpSettings.enabled || !vpPayload || !vpPayload.bins || !vpPayload.bins.length) {
      if (badge) {
        const warn = vpPayload && vpPayload.warning;
        if (vpSettings.enabled && warn) {
          badge.hidden = false;
          badge.textContent = warn;
        } else {
          badge.hidden = true;
          badge.textContent = "";
        }
      }
      return;
    }
    const bins = vpPayload.bins;
    const region = vpRegion();
    const display = vpSettings.display || "buy_sell";
    let maxLen = 0;
    for (let i = 0; i < bins.length; i++) {
      const b = bins[i];
      let len = Number(b.total_volume) || 0;
      if (display === "delta") len = Math.abs(Number(b.delta) || 0);
      if (len > maxLen) maxLen = len;
    }
    if (maxLen <= 0) maxLen = 1;
    ctx.save();
    ctx.beginPath();
    ctx.rect(0, 0, region.x1, size.h);
    ctx.clip();
    for (let i = 0; i < bins.length; i++) {
      const b = bins[i];
      const yTop = yOf(b.price_high);
      const yBot = yOf(b.price_low);
      if (yTop == null || yBot == null) continue;
      const y0 = Math.min(yTop, yBot);
      const y1 = Math.max(yTop, yBot);
      const h = Math.max(1, y1 - y0);
      const va = !!b.in_value_area;
      const alpha = va ? 0.82 : 0.45;
      if (display === "delta") {
        const d = Number(b.delta) || 0;
        const w = (Math.abs(d) / maxLen) * region.w;
        ctx.fillStyle = d >= 0 ? "rgba(56, 189, 248, " + alpha + ")" : "rgba(245, 158, 11, " + alpha + ")";
        ctx.fillRect(region.x1 - w, y0, w, h);
      } else if (display === "total") {
        const w = (Number(b.total_volume) / maxLen) * region.w;
        ctx.fillStyle = va ? "rgba(148, 163, 184, " + alpha + ")" : "rgba(100, 116, 139, " + (alpha * 0.7) + ")";
        ctx.fillRect(region.x1 - w, y0, w, h);
      } else {
        const buy = Number(b.buy_volume) || 0;
        const sell = Number(b.sell_volume) || 0;
        const tot = buy + sell;
        const w = (tot / maxLen) * region.w;
        const sellW = tot > 0 ? (sell / tot) * w : 0;
        const buyW = tot > 0 ? (buy / tot) * w : 0;
        ctx.fillStyle = "rgba(245, 158, 11, " + alpha + ")";
        ctx.fillRect(region.x1 - sellW, y0, sellW, h);
        ctx.fillStyle = "rgba(34, 211, 238, " + alpha + ")";
        ctx.fillRect(region.x1 - sellW - buyW, y0, buyW, h);
      }
      if (i === vpHoverIndex) {
        ctx.strokeStyle = "rgba(255,255,255,0.55)";
        ctx.strokeRect(region.x0, y0, region.w, h);
      }
    }
    if (vpSettings.poc && vpPayload.poc && vpPayload.poc.price_mid != null) {
      const y = yOf(vpPayload.poc.price_mid);
      if (y != null) {
        ctx.strokeStyle = "#ef4444";
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(region.x1, y);
        ctx.stroke();
      }
    }
    if (vpSettings.value_area) {
      ctx.setLineDash([4, 3]);
      ctx.lineWidth = 1;
      if (vpPayload.vah != null) {
        const y = yOf(vpPayload.vah);
        if (y != null) {
          ctx.strokeStyle = "rgba(226, 232, 240, 0.7)";
          ctx.beginPath();
          ctx.moveTo(0, y);
          ctx.lineTo(region.x1, y);
          ctx.stroke();
        }
      }
      if (vpPayload.val != null) {
        const y = yOf(vpPayload.val);
        if (y != null) {
          ctx.strokeStyle = "rgba(148, 163, 184, 0.7)";
          ctx.beginPath();
          ctx.moveTo(0, y);
          ctx.lineTo(region.x1, y);
          ctx.stroke();
        }
      }
      ctx.setLineDash([]);
    }
    ctx.restore();
    if (badge) {
      const warn = vpPayload.warning || (vpPayload.coverage_complete ? "" : vpPayload.coverage_label);
      if (warn) {
        badge.hidden = false;
        badge.textContent = warn;
      } else {
        badge.hidden = true;
        badge.textContent = "";
      }
    }
    drawOrderbookProfile();
  }

  function vpBinAt(x, y) {
    if (!vpSettings.enabled || !vpPayload || !vpPayload.bins) return -1;
    const region = vpRegion();
    if (x < region.x0 || x > region.x1) return -1;
    const bins = vpPayload.bins;
    for (let i = 0; i < bins.length; i++) {
      const yTop = yOf(bins[i].price_high);
      const yBot = yOf(bins[i].price_low);
      if (yTop == null || yBot == null) continue;
      const y0 = Math.min(yTop, yBot);
      const y1 = Math.max(yTop, yBot);
      if (y >= y0 && y <= y1) return i;
    }
    return -1;
  }

  function updateVolumeProfileHover(x, y) {
    const tip = $("vp-tooltip");
    const idx = vpBinAt(x, y);
    if (idx !== vpHoverIndex) {
      vpHoverIndex = idx;
      drawVolumeProfile();
    }
    if (!tip) return;
    if (idx < 0 || !vpPayload || !vpPayload.bins[idx]) {
      tip.hidden = true;
      // Fall through to OBP tooltip when not on a VP bin.
      updateOrderbookProfileHover(x, y);
      return;
    }
    // Prefer VP tooltip when hovering VP region; hide OBP tip.
    const obpTip = $("obp-tooltip");
    if (obpTip) obpTip.hidden = true;
    const b = vpPayload.bins[idx];
    const fmtN = function (n) {
      const v = Number(n);
      if (!Number.isFinite(v)) return "—";
      const abs = Math.abs(v);
      if (abs >= 1000) return v.toFixed(1);
      if (abs >= 1) return v.toFixed(4);
      return v.toFixed(6);
    };
    tip.hidden = false;
    tip.style.left = Math.max(8, x - 170) + "px";
    tip.style.top = Math.max(8, y - 8) + "px";
    tip.innerHTML =
      "<div>" + fmtN(b.price_low) + " – " + fmtN(b.price_high) + "</div>" +
      "<div>Buy " + fmtN(b.buy_volume) + "</div>" +
      "<div>Sell " + fmtN(b.sell_volume) + "</div>" +
      "<div>Total " + fmtN(b.total_volume) + "</div>" +
      "<div>Delta " + fmtN(b.delta) + "</div>" +
      "<div>Trades " + (b.total_count || 0) + "</div>" +
      "<div>" + (b.is_poc ? "POC · " : "") + (b.in_value_area ? "Value Area" : "Outside VA") + "</div>";
  }

  function layoutOverlays() {
    overlayLayoutCount += 1;
    clipOverlayLayerToPlot();
    clipResearchMarkersToPlot();
    overlayRegistry.forEach(function (rec) {
      renderOneOverlay(rec);
    });
    scheduleResearchMarkersPaint();
    layoutShiftMeasure();
    drawVolumeProfile();
    drawOrderbookProfile();
    drawOrderbookLevels();
  }

  function overlayDebugSamples() {
    const samples = [];
    overlayRegistry.forEach(function (rec) {
      if (samples.length >= 12) return;
      const p = rec.payload;
      if (!p) return;
      const item = {
        id: p.id,
        type: p.type,
        kind: p.kind || null,
        start_timestamp: p.start_timestamp != null ? p.start_timestamp : null,
        end_timestamp: p.end_timestamp != null ? p.end_timestamp : null,
        top_price: p.top_price != null ? p.top_price : null,
        bottom_price: p.bottom_price != null ? p.bottom_price : null,
        price: p.price != null ? p.price : null,
        start_price: p.start_price != null ? p.start_price : null,
        end_price: p.end_price != null ? p.end_price : null,
        entry_price: p.entry_price != null ? p.entry_price : null,
        stop_price: p.stop_price != null ? p.stop_price : null,
        target_price: p.target_price != null ? p.target_price : null,
        side: p.side || null,
      };
      if (item.top_price != null) item.yTop = yOf(item.top_price);
      if (item.bottom_price != null) item.yBot = yOf(item.bottom_price);
      if (item.price != null) item.yPrice = yOf(item.price);
      if (item.start_price != null) item.yStart = yOf(item.start_price);
      if (item.end_price != null) item.yEnd = yOf(item.end_price);
      if (rec.el) {
        item.domTop = rec.el.style.top || "";
        item.domHeight = rec.el.style.height || "";
        item.domLeft = rec.el.style.left || "";
      }
      samples.push(item);
    });
    return samples;
  }

  function recreateNativeOverlays() {
    overlayRegistry.forEach(function (rec) {
      if (rec.priceLine && candleSeries) {
        try {
          candleSeries.removePriceLine(rec.priceLine);
        } catch (e) {
          /* ignore */
        }
        rec.priceLine = null;
      }
    });
    layoutOverlays();
  }

  function addOverlay(payload) {
    if (!payload || !payload.id) return;
    if (isCanvasResearchMarker(payload)) {
      // Leave DOM registry if this id was previously a drawing.
      if (overlayRegistry.has(payload.id)) {
        removeOverlay(payload.id);
      }
      let rec = researchMarkers.get(payload.id);
      if (!rec) {
        rec = { payload: payload };
        researchMarkers.set(payload.id, rec);
      } else {
        rec.payload = payload;
      }
      scheduleResearchMarkersPaint();
      return;
    }
    // Migrating off canvas → DOM (rare).
    if (researchMarkers.has(payload.id)) {
      researchMarkers.delete(payload.id);
      scheduleResearchMarkersPaint();
    }
    let rec = overlayRegistry.get(payload.id);
    if (!rec) {
      rec = { payload: payload, priceLine: null, el: null };
      overlayRegistry.set(payload.id, rec);
    } else {
      const prevType = rec.payload && rec.payload.type;
      const prevKind = rec.payload && rec.payload.kind;
      rec.payload = payload;
      if (prevType !== payload.type || prevKind !== payload.kind) {
        destroyOverlayVisual(rec);
      }
    }
    renderOneOverlay(rec);
  }

  function updateOverlay(payload) {
    addOverlay(payload);
  }

  function removeOverlay(id) {
    if (researchMarkers.has(id)) {
      researchMarkers.delete(id);
      scheduleResearchMarkersPaint(true);
    }
    const rec = overlayRegistry.get(id);
    if (!rec) return;
    destroyOverlayVisual(rec);
    overlayRegistry.delete(id);
  }

  function clearOverlays() {
    researchMarkers.clear();
    scheduleResearchMarkersPaint(true);
    overlayRegistry.forEach(function (rec) {
      destroyOverlayVisual(rec);
    });
    overlayRegistry.clear();
  }

  function pointFromParam(param) {
    const pt = { time: null, price: null, x: null, y: null };
    if (!param) return pt;
    if (param.point) {
      pt.x = param.point.x;
      pt.y = param.point.y;
      if (candleSeries) {
        const price = candleSeries.coordinateToPrice(param.point.y);
        if (price != null && !Number.isNaN(price)) pt.price = price;
      }
    }
    if (param.time != null) {
      pt.time = Number(param.time);
    } else if (pt.x != null && chart) {
      const t = chart.timeScale().coordinateToTime(pt.x);
      if (t != null) pt.time = Number(t);
    }
    return pt;
  }

  function emitDrawing(obj) {
    if (!window.bridge || !window.bridge.on_drawing_event) return;
    window.bridge.on_drawing_event(JSON.stringify(obj));
  }

  function distToSeg(px, py, x1, y1, x2, y2) {
    const dx = x2 - x1;
    const dy = y2 - y1;
    const len2 = dx * dx + dy * dy;
    if (len2 <= 0) return Math.hypot(px - x1, py - y1);
    let t = ((px - x1) * dx + (py - y1) * dy) / len2;
    t = Math.max(0, Math.min(1, t));
    return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy));
  }

  function hitThreshold(payload) {
    const w = Number(
      (payload && payload.style && payload.style.width) ||
        payload.border_width ||
        1
    );
    return Math.max(6, w + 4);
  }

  function hitTestXY(x, y) {
    let best = null;
    let bestZ = -1e9;
    overlayRegistry.forEach(function (rec) {
      const p = rec.payload;
      if (!p || !p.metadata || !p.metadata.drawing_id) return;
      if (String(p.id || "").indexOf("__preview") === 0) return;
      let hit = false;
      const thresh = hitThreshold(p);
      if (p.type === "line" && p.kind === "horizontal") {
        const py = yOf(p.price);
        hit = py != null && Math.abs(py - y) <= thresh;
      } else if (p.type === "line" && p.kind === "vertical") {
        const px = xOf(p.timestamp);
        hit = px != null && Math.abs(px - x) <= thresh;
      } else if (p.type === "line" && (p.kind === "segment" || p.kind === "arrow")) {
        const x1 = xOf(p.start_timestamp);
        const y1 = yOf(p.start_price);
        const x2 = xOf(p.end_timestamp);
        const y2 = yOf(p.end_price);
        if (x1 != null && y1 != null && x2 != null && y2 != null) {
          hit = distToSeg(x, y, x1, y1, x2, y2) <= Math.max(7, thresh);
        }
      } else if (p.type === "zone") {
        const x1 = xOf(p.start_timestamp);
        const x2 = p.end_timestamp != null ? xOf(p.end_timestamp) : null;
        const y1 = yOf(p.top_price);
        const y2 = yOf(p.bottom_price);
        if (x1 != null && x2 != null && y1 != null && y2 != null) {
          const left = Math.min(x1, x2);
          const right = Math.max(x1, x2);
          const top = Math.min(y1, y2);
          const bot = Math.max(y1, y2);
          if (p.shape === "ellipse") {
            const cx = (left + right) / 2;
            const cy = (top + bot) / 2;
            const rx = Math.max(2, (right - left) / 2);
            const ry = Math.max(2, (bot - top) / 2);
            const nx = (x - cx) / rx;
            const ny = (y - cy) / ry;
            hit = nx * nx + ny * ny <= 1.12;
          } else {
            hit = x >= left && x <= right && y >= top && y <= bot;
          }
        }
      } else if (p.type === "position") {
        const x1 = xOf(p.start_timestamp);
        const x2 = xOf(p.end_timestamp);
        const yE = yOf(p.entry_price);
        const yT = yOf(p.target_price);
        const yS = yOf(p.stop_price);
          const pts = positionHandlePoints(p);
          if (pts) {
            for (let hi = 0; hi < pts.length; hi++) {
              if (Math.hypot(x - pts[hi].x, y - pts[hi].y) <= 14) {
                hit = true;
                break;
              }
            }
          }
          if (!hit && x1 != null && x2 != null && yE != null && yT != null && yS != null) {
          const left = Math.min(x1, x2);
          const right = Math.max(x1, x2);
          const top = Math.min(yE, yT, yS);
          const bot = Math.max(yE, yT, yS);
          const inside = x >= left && x <= right && y >= top && y <= bot;
          const nearEntry = Math.abs(y - yE) <= thresh && x >= left && x <= right;
          hit = inside || nearEntry;
        }
      } else if (p.type === "label" || p.type === "marker") {
        const px = xOf(p.timestamp);
        const py = p.price != null ? yOf(p.price) : null;
        hit = px != null && py != null && Math.hypot(px - x, py - y) <= 18;
      }
      if (hit && (p.z_order || 0) >= bestZ) {
        bestZ = p.z_order || 0;
        best = p.id;
      }
    });
    return best;
  }

  function setPanEnabled(on) {
    if (!chart) return;
    chart.applyOptions({
      handleScroll: {
        mouseWheel: true,
        pressedMouseMove: !!on,
        horzTouchDrag: !!on,
      },
    });
  }

  function setInteractionMode(mode) {
    interactionMode = mode || "select";
    if (interactionMode === "select") {
      toolClickCount = 0;
      setPanEnabled(!dragState);
    } else {
      toolClickCount = 0;
      setPanEnabled(false);
    }
  }

  function finishToolToSelect() {
    setInteractionMode("select");
    clearPreview();
    if (window.bridge && window.bridge.on_tool_idle) {
      window.bridge.on_tool_idle();
    }
  }

  function clearPreview() {
    previewAnchor = null;
    if (overlayRegistry.has("__preview__")) {
      removeOverlay("__preview__");
    }
  }

  function setPreviewAnchor(anchor) {
    if (!anchor) {
      clearPreview();
      return;
    }
    previewAnchor = anchor;
  }

  function previewStyle() {
    const color = (previewAnchor && previewAnchor.color) || "#8b9bb4";
    const width = Number((previewAnchor && previewAnchor.width) || 2);
    return { color: color, line_style: "dashed", width: width, opacity: 0.75 };
  }

  function previewPayload(tool, aTime, aPrice, bTime, bPrice) {
    const style = previewStyle();
    if (tool === "trend" || tool === "measure") {
      return {
        id: "__preview__",
        type: "line",
        kind: "segment",
        start_timestamp: aTime,
        start_price: aPrice,
        end_timestamp: bTime,
        end_price: bPrice,
        style: style,
        visible: true,
        z_order: 90,
        metadata: { source: "preview" },
      };
    }
    if (tool === "arrow") {
      return {
        id: "__preview__",
        type: "line",
        kind: "arrow",
        start_timestamp: aTime,
        start_price: aPrice,
        end_timestamp: bTime,
        end_price: bPrice,
        style: style,
        visible: true,
        z_order: 90,
        metadata: { source: "preview" },
      };
    }
    if (tool === "long_position" || tool === "short_position") {
      const side = tool === "long_position" ? "long" : "short";
      const prices = pricesFromTwoPoints(side, aPrice, bPrice);
      if (!prices) return null;
      const stats = computePositionStats(
        side,
        prices.entry,
        prices.stop,
        prices.target,
        DEFAULT_POS_NOTIONAL
      );
      const labels = formatPositionLabels(stats);
      return {
        id: "__preview__",
        type: "position",
        side: side,
        start_timestamp: Math.min(aTime, bTime),
        end_timestamp: Math.max(aTime, bTime),
        entry_price: prices.entry,
        stop_price: prices.stop,
        target_price: prices.target,
        position_notional: DEFAULT_POS_NOTIONAL,
        entry_label: labels.entry,
        stop_label: labels.stop,
        target_label: labels.target,
        profit_color: COLORS.up,
        loss_color: COLORS.down,
        fill_opacity: 0.18,
        style: { color: COLORS.ema20, line_style: "dashed", width: style.width, opacity: 0.9 },
        visible: true,
        z_order: 90,
        metadata: { source: "preview" },
      };
    }
    if (tool === "rectangle" || tool === "circle") {
      return {
        id: "__preview__",
        type: "zone",
        shape: tool === "circle" ? "ellipse" : "rect",
        start_timestamp: Math.min(aTime, bTime),
        end_timestamp: Math.max(aTime, bTime),
        top_price: Math.max(aPrice, bPrice),
        bottom_price: Math.min(aPrice, bPrice),
        opacity: tool === "circle" ? 0.08 : 0.1,
        border_style: "dashed",
        border_width: style.width,
        style: style,
        visible: true,
        z_order: 90,
        metadata: { source: "preview" },
      };
    }
    return null;
  }

  function updatePreviewFromParam(param) {
    if (!previewAnchor || !previewAnchor.tool) return;
    const pt = pointFromParam(param);
    if (pt.time == null || pt.price == null) return;
    const payload = previewPayload(
      previewAnchor.tool,
      previewAnchor.time,
      previewAnchor.price,
      pt.time,
      pt.price
    );
    if (payload) addOverlay(payload);
  }

  function xyFromEvent(ev) {
    const el = $("chart");
    if (!el) return null;
    const rect = el.getBoundingClientRect();
    return { x: ev.clientX - rect.left, y: ev.clientY - rect.top };
  }

  function marketPointFromXY(xy) {
    if (!chart || !candleSeries || !xy) return null;
    const time = chart.timeScale().coordinateToTime(xy.x);
    const price = candleSeries.coordinateToPrice(xy.y);
    if (time == null || price == null || Number.isNaN(price)) return null;
    return { time: Number(time), price: Number(price) };
  }

  function snapshotGeometry(p) {
    return {
      price: p.price,
      timestamp: p.timestamp,
      start_timestamp: p.start_timestamp,
      start_price: p.start_price,
      end_timestamp: p.end_timestamp,
      end_price: p.end_price,
      top_price: p.top_price,
      bottom_price: p.bottom_price,
      entry_price: p.entry_price,
      stop_price: p.stop_price,
      target_price: p.target_price,
      position_notional: p.position_notional,
    };
  }

  function detectDragMode(p, xy) {
    const HANDLE = 14;
    if (p.type === "position") {
      const pts = positionHandlePoints(p);
      if (pts) {
        for (let i = 0; i < pts.length; i++) {
          if (Math.hypot(xy.x - pts[i].x, xy.y - pts[i].y) <= HANDLE) {
            return pts[i].mode;
          }
        }
      }
      return "move";
    }
    if (p.kind === "arrow") {
      const x1 = xOf(p.start_timestamp);
      const y1 = yOf(p.start_price);
      const x2 = xOf(p.end_timestamp);
      const y2 = yOf(p.end_price);
      if (x1 != null && y1 != null && Math.hypot(xy.x - x1, xy.y - y1) <= HANDLE) {
        return "resize-start";
      }
      if (x2 != null && y2 != null && Math.hypot(xy.x - x2, xy.y - y2) <= HANDLE) {
        return "resize-end";
      }
      return "move";
    }
    if (p.shape === "ellipse") {
      const x1 = xOf(p.start_timestamp);
      const x2 = p.end_timestamp != null ? xOf(p.end_timestamp) : null;
      const y1 = yOf(p.top_price);
      const y2 = yOf(p.bottom_price);
      if (x1 == null || x2 == null || y1 == null || y2 == null) return "move";
      const left = Math.min(x1, x2);
      const right = Math.max(x1, x2);
      const top = Math.min(y1, y2);
      const bot = Math.max(y1, y2);
      const corners = [
        { mode: "resize-nw", x: left, y: top },
        { mode: "resize-ne", x: right, y: top },
        { mode: "resize-sw", x: left, y: bot },
        { mode: "resize-se", x: right, y: bot },
      ];
      for (let i = 0; i < corners.length; i++) {
        if (Math.hypot(xy.x - corners[i].x, xy.y - corners[i].y) <= HANDLE) {
          return corners[i].mode;
        }
      }
      return "move";
    }
    return "move";
  }

  function candleIndexForTime(unix, candles) {
    candles = candles || (lastPayload && lastPayload.candles) || [];
    if (!candles.length || unix == null || !Number.isFinite(Number(unix))) {
      return null;
    }
    const t = Number(unix);
    let lo = 0;
    let hi = candles.length - 1;
    if (t <= candles[0].time) return 0;
    if (t >= candles[hi].time) return hi;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      const mt = candles[mid].time;
      if (mt === t) return mid;
      if (mt < t) lo = mid + 1;
      else hi = mid - 1;
    }
    const a = Math.max(0, lo - 1);
    const b = Math.min(candles.length - 1, lo);
    return Math.abs(candles[a].time - t) <= Math.abs(candles[b].time - t) ? a : b;
  }

  function normalizeMeasureAnchor(a) {
    if (!a) return null;
    if (a.price == null || a.time == null) return null;
    const price = Number(a.price);
    const time = Number(a.time);
    if (!Number.isFinite(price) || !Number.isFinite(time)) return null;
    let logical = a.logical != null ? Number(a.logical) : null;
    if (logical != null && !Number.isFinite(logical)) logical = null;
    const index = a.index != null ? a.index : candleIndexForTime(time);
    return { time: time, price: price, index: index, logical: logical };
  }

  function measureAnchorFromXY(xy) {
    const mp = marketPointFromXY(xy);
    if (!mp) return null;
    let logical = null;
    if (chart && chart.timeScale().coordinateToLogical) {
      try {
        logical = chart.timeScale().coordinateToLogical(xy.x);
      } catch (err) {
        logical = null;
      }
    }
    return normalizeMeasureAnchor({
      time: mp.time,
      price: mp.price,
      logical: logical,
    });
  }

  function computeShiftMeasure(start, end, candles) {
    const a = normalizeMeasureAnchor(start);
    const b = normalizeMeasureAnchor(end);
    if (!a || !b) return null;
    const series = candles || (lastPayload && lastPayload.candles) || [];
    const priceChange = b.price - a.price;
    let percentChange = null;
    if (a.price !== 0) {
      percentChange = (priceChange / a.price) * 100;
    }
    const i0 = candles
      ? candleIndexForTime(a.time, series)
      : a.index != null
        ? a.index
        : candleIndexForTime(a.time, series);
    const i1 = candles
      ? candleIndexForTime(b.time, series)
      : b.index != null
        ? b.index
        : candleIndexForTime(b.time, series);
    const barDistance =
      i0 == null || i1 == null ? 0 : Math.abs(i1 - i0);
    const barInclusive = barDistance + 1;
    const elapsedSeconds = Math.abs(b.time - a.time);
    let high = null;
    let low = null;
    let volume = null;
    if (i0 != null && i1 != null && series.length) {
      const from = Math.min(i0, i1);
      const to = Math.max(i0, i1);
      let hi = -Infinity;
      let lo = Infinity;
      let vol = 0;
      let hasVol = false;
      for (let i = from; i <= to && i < series.length; i++) {
        const c = series[i];
        if (!c) continue;
        if (c.high != null && Number(c.high) > hi) hi = Number(c.high);
        if (c.low != null && Number(c.low) < lo) lo = Number(c.low);
        if (c.volume != null && Number.isFinite(Number(c.volume))) {
          vol += Number(c.volume);
          hasVol = true;
        }
      }
      if (hi !== -Infinity) high = hi;
      if (lo !== Infinity) low = lo;
      if (hasVol) volume = vol;
    }
    let tone = "neutral";
    if (priceChange > 0) tone = "up";
    else if (priceChange < 0) tone = "down";
    const color =
      tone === "up" ? COLORS.up : tone === "down" ? COLORS.down : COLORS.muted;
    return {
      startPrice: a.price,
      endPrice: b.price,
      startTime: a.time,
      endTime: b.time,
      startIndex: i0,
      endIndex: i1,
      priceChange: priceChange,
      percentChange: percentChange,
      barDistance: barDistance,
      barInclusive: barInclusive,
      elapsedSeconds: elapsedSeconds,
      high: high,
      low: low,
      volume: volume,
      tone: tone,
      color: color,
    };
  }

  function formatElapsedSeconds(seconds) {
    let s = Math.abs(Math.round(Number(seconds) || 0));
    const days = Math.floor(s / 86400);
    s %= 86400;
    const hours = Math.floor(s / 3600);
    s %= 3600;
    const minutes = Math.floor(s / 60);
    const secs = s % 60;
    const parts = [];
    if (days) parts.push(days + "d");
    if (hours) parts.push(hours + "h");
    if (minutes) parts.push(minutes + "m");
    if (!parts.length) parts.push(secs ? secs + "s" : "0m");
    return parts.join(" ");
  }

  function formatSignedFixed(value, digits) {
    if (!Number.isFinite(value)) return "—";
    if (Math.abs(value) < 1e-12) return (0).toFixed(digits);
    const sign = value > 0 ? "+" : "\u2212";
    return sign + Math.abs(value).toFixed(digits);
  }

  function shiftMeasureColor() {
    if (!shiftMeasure || !shiftMeasure.stats) return COLORS.muted;
    return shiftMeasure.stats.color || COLORS.muted;
  }

  function ensureShiftMeasureDom() {
    let root = $("shift-measure");
    if (root) return root;
    const layer = overlayLayer();
    if (!layer) return null;
    root = document.createElement("div");
    root.id = "shift-measure";
    root.innerHTML =
      '<div class="sm-rect"></div>' +
      '<svg class="sm-svg" xmlns="http://www.w3.org/2000/svg">' +
      '<line class="sm-line" x1="0" y1="0" x2="0" y2="0" />' +
      "</svg>" +
      '<div class="sm-dot sm-dot-start"></div>' +
      '<div class="sm-dot sm-dot-end"></div>' +
      '<div class="sm-box">' +
      '<div class="sm-prices"></div>' +
      '<div class="sm-delta"></div>' +
      '<div class="sm-meta"></div>' +
      '<div class="sm-extra"></div>' +
      "</div>";
    layer.appendChild(root);
    return root;
  }

  function hideShiftMeasureDom() {
    const root = $("shift-measure");
    if (root) {
      root.style.display = "none";
      root.classList.remove("visible");
    }
  }

  function layoutShiftMeasure() {
    if (!shiftMeasure) {
      hideShiftMeasureDom();
      return;
    }
    const root = ensureShiftMeasureDom();
    if (!root) return;
    const x1 = xOf(shiftMeasure.start.time);
    const y1 = yOf(shiftMeasure.start.price);
    const x2 = xOf(shiftMeasure.end.time);
    const y2 = yOf(shiftMeasure.end.price);
    if (x1 == null || y1 == null || x2 == null || y2 == null) {
      root.style.display = "none";
      root.classList.remove("visible");
      return;
    }
    const stats = shiftMeasure.stats || computeShiftMeasure(shiftMeasure.start, shiftMeasure.end);
    shiftMeasure.stats = stats;
    const color = (stats && stats.color) || COLORS.muted;
    root.style.setProperty("--sm-color", color);
    root.style.setProperty("--sm-fill", hexAlpha(color, 0.14));
    root.style.display = "block";
    root.classList.add("visible");
    const left = Math.min(x1, x2);
    const top = Math.min(y1, y2);
    const width = Math.max(1, Math.abs(x2 - x1));
    const height = Math.max(1, Math.abs(y2 - y1));
    const rect = root.querySelector(".sm-rect");
    rect.style.left = left + "px";
    rect.style.top = top + "px";
    rect.style.width = width + "px";
    rect.style.height = height + "px";
    const line = root.querySelector(".sm-line");
    line.setAttribute("x1", String(x1));
    line.setAttribute("y1", String(y1));
    line.setAttribute("x2", String(x2));
    line.setAttribute("y2", String(y2));
    line.setAttribute("stroke", color);
    const d1 = root.querySelector(".sm-dot-start");
    const d2 = root.querySelector(".sm-dot-end");
    d1.style.left = x1 + "px";
    d1.style.top = y1 + "px";
    d2.style.left = x2 + "px";
    d2.style.top = y2 + "px";
    const box = root.querySelector(".sm-box");
    const pct =
      stats && stats.percentChange != null
        ? " (" + formatSignedFixed(stats.percentChange, 2) + "%)"
        : "";
    box.querySelector(".sm-prices").textContent =
      fmt(stats.startPrice) + " \u2192 " + fmt(stats.endPrice);
    box.querySelector(".sm-delta").textContent =
      formatSignedFixed(stats.priceChange, 4) + pct;
    const kerzen = stats.barDistance === 1 ? "1 Kerze" : stats.barDistance + " Kerzen";
    box.querySelector(".sm-meta").textContent =
      kerzen + " \u00b7 " + formatElapsedSeconds(stats.elapsedSeconds);
    const extra = box.querySelector(".sm-extra");
    const extraParts = [];
    if (stats.high != null && stats.low != null) {
      extraParts.push("H " + fmt(stats.high) + "  L " + fmt(stats.low));
    }
    extra.textContent = extraParts.join("  ");
    extra.style.display = extraParts.length ? "block" : "none";
    const size = chartSize();
    const bw = Math.max(box.offsetWidth || 0, 148);
    const bh = Math.max(box.offsetHeight || 0, 52);
    let bx = x2 + 12;
    let by = y2 - bh / 2;
    if (bx + bw > size.w - 6) bx = x2 - bw - 12;
    if (bx < 6) bx = 6;
    if (bx + bw > size.w - 6) bx = Math.max(6, size.w - bw - 6);
    if (by < 6) by = y2 + 12;
    if (by + bh > size.h - 6) by = size.h - bh - 6;
    if (by < 6) by = 6;
    box.style.left = bx + "px";
    box.style.top = by + "px";
  }

  function abortDrawingPreview() {
    if (previewAnchor) {
      clearPreview();
    }
    emitDrawing({ type: "cancel_preview" });
  }

  function releaseShiftMeasureCapture() {
    const el = shiftMeasureCaptureEl || $("chart");
    const pid = shiftMeasure && shiftMeasure.pointerId;
    if (el && pid != null && el.releasePointerCapture) {
      try {
        if (!el.hasPointerCapture || el.hasPointerCapture(pid)) {
          el.releasePointerCapture(pid);
        }
      } catch (err) {
        /* already released */
      }
    }
    shiftMeasureCaptureEl = null;
  }

  function restorePanAfterMeasure() {
    setPanEnabled(interactionMode === "select" && !dragState);
  }

  function beginShiftMeasure(anchor, ev) {
    abortDrawingPreview();
    if (dragState) {
      dragState = null;
    }
    setPanEnabled(false);
    shiftMeasure = {
      dragging: true,
      pointerId: ev && ev.pointerId != null ? ev.pointerId : null,
      start: anchor,
      end: {
        time: anchor.time,
        price: anchor.price,
        index: anchor.index,
        logical: anchor.logical,
      },
    };
    shiftMeasure.stats = computeShiftMeasure(shiftMeasure.start, shiftMeasure.end);
    const captureEl = (ev && ev.target) || $("chart");
    if (captureEl && ev && ev.pointerId != null && captureEl.setPointerCapture) {
      try {
        captureEl.setPointerCapture(ev.pointerId);
        shiftMeasureCaptureEl = captureEl;
      } catch (err) {
        shiftMeasureCaptureEl = null;
      }
    }
    layoutShiftMeasure();
    return true;
  }

  function updateShiftMeasure(anchor) {
    if (!shiftMeasure || !anchor) return false;
    shiftMeasure.end = anchor;
    shiftMeasure.stats = computeShiftMeasure(shiftMeasure.start, shiftMeasure.end);
    layoutShiftMeasure();
    return true;
  }

  function finishShiftMeasure(ev) {
    if (!shiftMeasure) return false;
    if (shiftMeasure.dragging) {
      if (ev) {
        const xy = xyFromEvent(ev);
        if (xy) {
          const anchor = measureAnchorFromXY(xy);
          if (anchor) updateShiftMeasure(anchor);
        }
      }
      shiftMeasure.dragging = false;
      releaseShiftMeasureCapture();
      suppressNextClick = true;
      restorePanAfterMeasure();
    }
    layoutShiftMeasure();
    return true;
  }

  function clearShiftMeasure() {
    const was = !!shiftMeasure;
    if (shiftMeasure && shiftMeasure.dragging) {
      releaseShiftMeasureCapture();
      restorePanAfterMeasure();
    }
    shiftMeasure = null;
    shiftMeasurePendingXy = null;
    if (shiftMeasureRaf != null) {
      cancelAnimationFrame(shiftMeasureRaf);
      shiftMeasureRaf = null;
    }
    hideShiftMeasureDom();
    return was;
  }

  function clearShiftMeasureIfIdle() {
    if (shiftMeasure && !shiftMeasure.dragging) {
      clearShiftMeasure();
      return true;
    }
    return false;
  }

  function scheduleShiftMeasureMove(ev) {
    const xy = xyFromEvent(ev);
    if (!xy) return;
    shiftMeasurePendingXy = xy;
    if (shiftMeasureRaf != null) return;
    shiftMeasureRaf = requestAnimationFrame(function () {
      shiftMeasureRaf = null;
      if (!shiftMeasure || !shiftMeasure.dragging || !shiftMeasurePendingXy) return;
      const anchor = measureAnchorFromXY(shiftMeasurePendingXy);
      if (anchor) updateShiftMeasure(anchor);
    });
  }

  function shiftHeld(ev) {
    if (ev) {
      if (ev.shiftKey) return true;
      if (ev.getModifierState && ev.getModifierState("Shift")) return true;
    }
    return !!(hostShift || window.__hostShift);
  }

  function setHostShift(on) {
    hostShift = !!on;
    window.__hostShift = !!on;
    return hostShift;
  }

  function formatCopiedPrice(price) {
    const n = Number(price);
    if (!Number.isFinite(n)) return "";
    const digits = lastPriceFormat && lastPriceFormat.precision != null ? lastPriceFormat.precision : 6;
    return String(Number(n.toFixed(digits)));
  }

  function showCopyToast(text) {
    let el = $("copy-toast");
    if (!el) {
      el = document.createElement("div");
      el.id = "copy-toast";
      document.body.appendChild(el);
    }
    el.textContent = "Preis kopiert: " + text;
    el.classList.add("visible");
    clearTimeout(showCopyToast._t);
    showCopyToast._t = setTimeout(function () {
      el.classList.remove("visible");
    }, 1400);
  }

  function copyText(text) {
    if (!text) return Promise.resolve(false);
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).then(function () { return true; }).catch(function () {
        return copyTextFallback(text);
      });
    }
    return Promise.resolve(copyTextFallback(text));
  }

  function copyTextFallback(text) {
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.left = "-9999px";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(ta);
      return !!ok;
    } catch (err) {
      return false;
    }
  }

  function priceAtEvent(ev) {
    const xy = xyFromEvent(ev);
    if (xy && candleSeries) {
      const px = candleSeries.coordinateToPrice(xy.y);
      if (px != null && !Number.isNaN(Number(px))) return Number(px);
    }
    return lastCursorPrice;
  }

  function onChartContextMenu(ev) {
    if (ev.preventDefault) ev.preventDefault();
    if (ev.stopPropagation) ev.stopPropagation();
    const xy = xyFromEvent(ev);
    if (xy) {
      const size = chartSize();
      if (xy.y < 0 || xy.y > size.h || xy.x < 0 || xy.x > size.w) return false;
    }
    const price = priceAtEvent(ev);
    const text = formatCopiedPrice(price);
    if (!text) return false;
    copyText(text).then(function (ok) {
      if (ok) showCopyToast(text);
    });
    return false;
  }

  function onShiftMeasureDown(ev) {
    if (shiftMeasure && shiftMeasure.dragging) return false;
    if (ev.button != null && ev.button !== 0) return false;
    if (ev.buttons != null && ev.buttons !== 0 && ev.buttons !== 1) return false;
    if (!shiftHeld(ev)) return false;
    if (!chart || !candleSeries) return false;
    const xy = xyFromEvent(ev);
    if (!xy) return false;
    const size = chartSize();
    if (xy.x < 0 || xy.y < 0 || xy.x > size.w || xy.y > size.h) return false;
    const anchor = measureAnchorFromXY(xy);
    if (!anchor) return false;
    if (ev.preventDefault) ev.preventDefault();
    if (ev.stopPropagation) ev.stopPropagation();
    if (ev.stopImmediatePropagation) ev.stopImmediatePropagation();
    beginShiftMeasure(anchor, ev);
    return true;
  }

  function onShiftMeasureMoveCapture(ev) {
    if (!shiftMeasure || !shiftMeasure.dragging) return;
    if (ev.preventDefault) ev.preventDefault();
    if (ev.stopPropagation) ev.stopPropagation();
    scheduleShiftMeasureMove(ev);
  }

  function onShiftMeasureUpCapture(ev) {
    if (!shiftMeasure || !shiftMeasure.dragging) return;
    if (ev.pointerId != null && shiftMeasure.pointerId != null && ev.pointerId !== shiftMeasure.pointerId) {
      return;
    }
    if (ev.preventDefault) ev.preventDefault();
    if (ev.stopPropagation) ev.stopPropagation();
    finishShiftMeasure(ev);
  }

  function onLostPointerCapture(ev) {
    if (!shiftMeasure || !shiftMeasure.dragging) return;
    if (ev && (ev.buttons || ev.pressure > 0)) return;
  }

  function onWindowBlur() {
    if (shiftMeasure && shiftMeasure.dragging) {
      finishShiftMeasure();
    }
  }

  function onDocumentMouseOut(ev) {
    if (ev.relatedTarget != null) return;
    if (shiftMeasure && shiftMeasure.dragging) {
      finishShiftMeasure();
    }
  }

  function bindShiftMeasureHandlers() {
    if (shiftMeasureHandlersBound) return;
    const el = $("chart");
    shiftMeasureHandlersBound = true;
    window.addEventListener("pointerdown", onShiftMeasureDown, true);
    window.addEventListener("pointermove", onShiftMeasureMoveCapture, true);
    window.addEventListener("pointerup", onShiftMeasureUpCapture, true);
    window.addEventListener("pointercancel", onShiftMeasureUpCapture, true);
    if (el) el.addEventListener("lostpointercapture", onLostPointerCapture);
    window.addEventListener("blur", onWindowBlur);
    window.addEventListener("keydown", onShiftKey);
    window.addEventListener("keyup", onShiftKey);
    document.addEventListener("mouseout", onDocumentMouseOut);
  }

  function unbindShiftMeasureHandlers() {
    if (!shiftMeasureHandlersBound) return;
    const el = $("chart");
    window.removeEventListener("pointerdown", onShiftMeasureDown, true);
    window.removeEventListener("pointermove", onShiftMeasureMoveCapture, true);
    window.removeEventListener("pointerup", onShiftMeasureUpCapture, true);
    window.removeEventListener("pointercancel", onShiftMeasureUpCapture, true);
    if (el) el.removeEventListener("lostpointercapture", onLostPointerCapture);
    window.removeEventListener("blur", onWindowBlur);
    window.removeEventListener("keydown", onShiftKey);
    window.removeEventListener("keyup", onShiftKey);
    document.removeEventListener("mouseout", onDocumentMouseOut);
    shiftMeasureHandlersBound = false;
  }

  function onShiftKey(ev) {
    if (ev.key !== "Shift" && ev.code !== "ShiftLeft" && ev.code !== "ShiftRight") return;
    setHostShift(ev.type === "keydown");
  }

  function destroyUi() {
    clearShiftMeasure();
    unbindShiftMeasureHandlers();
    return true;
  }

  function snapshotShiftMeasure() {
    if (!shiftMeasure) return null;
    const root = $("shift-measure");
    return {
      dragging: !!shiftMeasure.dragging,
      visible: !!(root && root.style.display !== "none"),
      start: {
        time: shiftMeasure.start.time,
        price: shiftMeasure.start.price,
        index: shiftMeasure.start.index,
      },
      end: {
        time: shiftMeasure.end.time,
        price: shiftMeasure.end.price,
        index: shiftMeasure.end.index,
      },
      stats: shiftMeasure.stats || null,
      tone: shiftMeasure.stats ? shiftMeasure.stats.tone : null,
      color: shiftMeasureColor(),
      inOverlayRegistry: overlayRegistry.has("shift-measure"),
      handlersBound: shiftMeasureHandlersBound,
    };
  }

  function apiStartShiftMeasure(start) {
    const anchor = normalizeMeasureAnchor(start);
    if (!anchor) return false;
    return beginShiftMeasure(anchor, null);
  }

  function apiUpdateShiftMeasure(end) {
    if (!shiftMeasure) return false;
    const anchor = normalizeMeasureAnchor(end);
    if (!anchor) return false;
    return updateShiftMeasure(anchor);
  }

  function makeFakePointer(opts) {
    opts = opts || {};
    const el = $("chart");
    const rect = el ? el.getBoundingClientRect() : { left: 0, top: 0 };
    const x = opts.x != null ? opts.x : (opts.clientX != null ? opts.clientX - rect.left : 40);
    const y = opts.y != null ? opts.y : (opts.clientY != null ? opts.clientY - rect.top : 40);
    return {
      button: opts.button != null ? opts.button : 0,
      shiftKey: !!opts.shiftKey,
      pointerId: opts.pointerId != null ? opts.pointerId : 1,
      clientX: rect.left + x,
      clientY: rect.top + y,
      preventDefault: function () {},
      stopPropagation: function () {},
      stopImmediatePropagation: function () {},
    };
  }

  function panPressedMouseMove() {
    if (!chart) return null;
    try {
      const opts = chart.options();
      return opts && opts.handleScroll ? !!opts.handleScroll.pressedMouseMove : null;
    } catch (err) {
      return null;
    }
  }

  function cursorForDragMode(mode) {
    if (mode === "resize-tp" || mode === "resize-sl" || mode === "resize-entry") return "ns-resize";
    if (mode === "resize-left" || mode === "resize-right") return "ew-resize";
    if (mode === "resize-start" || mode === "resize-end") return "nwse-resize";
    if (mode && String(mode).indexOf("resize-") === 0) return "nwse-resize";
    return "move";
  }

  function setChartCursor(kind) {
    const el = $("chart");
    const pane = $("price-pane");
    const value = kind || "crosshair";
    if (el) el.style.cursor = value;
    if (pane) pane.style.cursor = value;
    if (el) {
      const canvases = el.querySelectorAll("canvas");
      for (let i = 0; i < canvases.length; i++) canvases[i].style.cursor = value;
    }
  }

  function onPointerDown(ev) {
    if (ev.button != null && ev.button !== 0) return;
    if (shiftHeld(ev) || (shiftMeasure && shiftMeasure.dragging)) return;
    if (interactionMode !== "select" || !chart) return;
    const xy = xyFromEvent(ev);
    if (!xy) return;
    const hit = hitTestXY(xy.x, xy.y);
    if (!hit) return;
    const rec = overlayRegistry.get(hit);
    if (!rec || !rec.payload || !rec.payload.metadata) return;
    if (rec.payload.metadata.locked) return;
    const p = rec.payload;
    const kind = p.kind;
    const movable =
      (p.type === "line" && (kind === "horizontal" || kind === "vertical" || kind === "arrow")) ||
      (p.type === "zone" && p.shape === "ellipse") ||
      p.type === "position";
    if (!movable) return;
    if (ev.preventDefault) ev.preventDefault();
    if (ev.stopPropagation) ev.stopPropagation();
    if (ev.stopImmediatePropagation) ev.stopImmediatePropagation();
    const mode = detectDragMode(p, xy);
    dragState = {
      overlayId: hit,
      kind: kind || p.shape || p.type,
      mode: mode,
      moved: false,
      grab: marketPointFromXY(xy),
      orig: snapshotGeometry(p),
    };
    setPanEnabled(false);
    setChartCursor(cursorForDragMode(mode));
    const el = $("chart");
    if (el && ev.pointerId != null && el.setPointerCapture) {
      try {
        el.setPointerCapture(ev.pointerId);
      } catch (err) {
        /* ignore */
      }
    }
  }

  function onPointerMove(ev) {
    if (shiftMeasure && shiftMeasure.dragging) {
      scheduleShiftMeasureMove(ev);
      return;
    }
    if (!dragState || !candleSeries || !chart) return;
    const xy = xyFromEvent(ev);
    if (!xy) return;
    const rec = overlayRegistry.get(dragState.overlayId);
    if (!rec || !rec.payload) return;
    dragState.moved = true;
    if (dragState.kind === "horizontal") {
      const price = candleSeries.coordinateToPrice(xy.y);
      if (price == null || Number.isNaN(price)) return;
      rec.payload.price = price;
      renderHorizontal(rec);
      return;
    }
    if (dragState.kind === "vertical") {
      const t = chart.timeScale().coordinateToTime(xy.x);
      if (t == null) return;
      rec.payload.timestamp = Number(t);
      renderVertical(rec);
      return;
    }
    const mp = marketPointFromXY(xy);
    if (!mp) return;
    const orig = dragState.orig || {};
    const grab = dragState.grab;
    const mode = dragState.mode || "move";
    if (mode === "move" && grab) {
      const dt = mp.time - grab.time;
      const dp = mp.price - grab.price;
      if (rec.payload.kind === "arrow") {
        rec.payload.start_timestamp = orig.start_timestamp + dt;
        rec.payload.end_timestamp = orig.end_timestamp + dt;
        rec.payload.start_price = orig.start_price + dp;
        rec.payload.end_price = orig.end_price + dp;
      } else if (rec.payload.shape === "ellipse") {
        rec.payload.start_timestamp = orig.start_timestamp + dt;
        rec.payload.end_timestamp = orig.end_timestamp + dt;
        rec.payload.top_price = orig.top_price + dp;
        rec.payload.bottom_price = orig.bottom_price + dp;
      } else if (rec.payload.type === "position") {
        rec.payload.start_timestamp = orig.start_timestamp + dt;
        rec.payload.end_timestamp = orig.end_timestamp + dt;
        rec.payload.entry_price = orig.entry_price + dp;
        rec.payload.stop_price = orig.stop_price + dp;
        rec.payload.target_price = orig.target_price + dp;
      }
    } else if (rec.payload.type === "position") {
      if (mode === "resize-entry" && grab) {
        const dp = mp.price - grab.price;
        rec.payload.entry_price = orig.entry_price + dp;
        rec.payload.stop_price = orig.stop_price + dp;
        rec.payload.target_price = orig.target_price + dp;
      } else if (mode === "resize-tp") {
        rec.payload.target_price = mp.price;
      } else if (mode === "resize-sl") {
        rec.payload.stop_price = mp.price;
      } else if (mode === "resize-left") {
        rec.payload.start_timestamp = mp.time;
      } else if (mode === "resize-right") {
        rec.payload.end_timestamp = mp.time;
      }
    } else if (mode === "resize-start") {
      rec.payload.start_timestamp = mp.time;
      rec.payload.start_price = mp.price;
    } else if (mode === "resize-end") {
      rec.payload.end_timestamp = mp.time;
      rec.payload.end_price = mp.price;
    } else if (mode.indexOf("resize-") === 0 && rec.payload.shape === "ellipse") {
      if (mode === "resize-nw" || mode === "resize-sw") {
        rec.payload.start_timestamp = mp.time;
      } else {
        rec.payload.end_timestamp = mp.time;
      }
      if (mode === "resize-nw" || mode === "resize-ne") {
        rec.payload.top_price = mp.price;
      } else {
        rec.payload.bottom_price = mp.price;
      }
    }
    renderOneOverlay(rec);
  }

  function onPointerUp(ev) {
    if (shiftMeasure && shiftMeasure.dragging) {
      finishShiftMeasure(ev);
      return;
    }
    if (!dragState) return;
    const rec = overlayRegistry.get(dragState.overlayId);
    const moved = dragState.moved;
    const mode = dragState.mode || "move";
    const payload = rec ? rec.payload : null;
    const overlayId = dragState.overlayId;
    dragState = null;
    setPanEnabled(interactionMode === "select");
    setChartCursor("crosshair");
    if (moved && payload) {
      suppressNextClick = true;
      const event = {
        type: "drag",
        overlay_id: overlayId,
        time: payload.timestamp != null ? payload.timestamp : payload.end_timestamp,
        price: payload.price != null ? payload.price : payload.end_price,
        mode: mode,
      };
      if (payload.kind === "arrow") {
        event.start_timestamp = payload.start_timestamp;
        event.start_price = payload.start_price;
        event.end_timestamp = payload.end_timestamp;
        event.end_price = payload.end_price;
      } else if (payload.shape === "ellipse") {
        event.start_timestamp = payload.start_timestamp;
        event.start_price = payload.top_price;
        event.end_timestamp = payload.end_timestamp;
        event.end_price = payload.bottom_price;
      } else if (payload.type === "position") {
        event.start_timestamp = payload.start_timestamp;
        event.end_timestamp = payload.end_timestamp;
        event.entry_price = payload.entry_price;
        event.stop_price = payload.stop_price;
        event.target_price = payload.target_price;
        event.position_notional = payload.position_notional;
        event.price = payload.entry_price;
        event.time = payload.end_timestamp;
      }
      emitDrawing(event);
    }
  }

  function onChartKey(ev) {
    if (ev.key === "Escape" && shiftMeasure) {
      clearShiftMeasure();
      ev.preventDefault();
    }
    if (!window.bridge || !window.bridge.on_chart_key) return;
    if (ev.key === "Escape") {
      window.bridge.on_chart_key("escape");
      ev.preventDefault();
    } else if (ev.key === "Delete" || ev.key === "Backspace") {
      window.bridge.on_chart_key("delete");
      ev.preventDefault();
    }
  }

  function lockedScaleProvider() {
    const minV =
      lastLowerPayload && lastLowerPayload.price_min != null
        ? Number(lastLowerPayload.price_min)
        : 0;
    const maxV =
      lastLowerPayload && lastLowerPayload.price_max != null
        ? Number(lastLowerPayload.price_max)
        : 100;
    return function () {
      return { priceRange: { minValue: minV, maxValue: maxV } };
    };
  }

  function oscSize() {
    const el = $("oscillator");
    return {
      w: el ? el.clientWidth : 0,
      h: el ? el.clientHeight : 0,
    };
  }

  function setLowerOpen(open) {
    const app = $("app");
    if (!app) return;
    if (open) {
      app.classList.add("lower-open");
    } else {
      app.classList.remove("lower-open");
    }
    lowerVisible = !!open;
  }

  function initLowerSplit() {
    const handle = $("lower-split");
    const app = $("app");
    const lower = $("lower-pane");
    if (!handle || !app || !lower) return;
    let dragging = false;
    handle.addEventListener("pointerdown", function (ev) {
      if (!lowerVisible) return;
      dragging = true;
      handle.setPointerCapture(ev.pointerId);
      ev.preventDefault();
    });
    handle.addEventListener("pointermove", function (ev) {
      if (!dragging) return;
      const rect = app.getBoundingClientRect();
      if (!rect.height) return;
      let pct = ((rect.bottom - ev.clientY) / rect.height) * 100;
      if (pct < 12) pct = 12;
      if (pct > 40) pct = 40;
      lower.style.height = pct + "%";
      resize();
    });
    function stopDrag() {
      dragging = false;
    }
    handle.addEventListener("pointerup", stopDrag);
    handle.addEventListener("pointercancel", stopDrag);
  }

  function ensureOscChart() {
    if (oscChart) return oscChart;
    const el = $("oscillator");
    if (!el || typeof LightweightCharts === "undefined") return null;
    const size = oscSize();
    const w = Math.max(size.w, 16);
    const h = Math.max(size.h, 16);
    oscChart = LightweightCharts.createChart(el, {
      layout: {
        background: { color: COLORS.bg },
        textColor: COLORS.text,
        fontFamily: 'Inter, "Segoe UI", Ubuntu, system-ui, sans-serif',
      },
      grid: {
        vertLines: { color: COLORS.grid },
        horzLines: { color: COLORS.grid },
      },
      crosshair: {
        mode: LightweightCharts.CrosshairMode.Normal,
        vertLine: { color: "#5b6478", width: 1, style: 3, labelBackgroundColor: "#2a2e39" },
        horzLine: { color: "#5b6478", width: 1, style: 3, labelBackgroundColor: "#2a2e39" },
      },
      rightPriceScale: {
        borderColor: COLORS.border,
        autoScale: true,
        scaleMargins: OSC_SCALE_MARGINS,
        minimumWidth: 56,
      },
      timeScale: {
        borderColor: COLORS.border,
        timeVisible: false,
        secondsVisible: false,
        rightOffset: DEFAULT_RIGHT_OFFSET,
        barSpacing: DEFAULT_BAR_SPACING,
        minBarSpacing: 2,
        visible: true,
      },
      handleScroll: {
        mouseWheel: true,
        pressedMouseMove: true,
        horzTouchDrag: true,
        vertTouchDrag: false,
      },
      handleScale: {
        axisPressedMouseMove: { time: true, price: false },
        mouseWheel: true,
        pinch: true,
        axisDoubleClickReset: { time: true, price: false },
      },
      width: w,
      height: h,
    });
    lastAppliedOscSize = { w: size.w, h: size.h };

    oscTimeBase = oscChart.addLineSeries({
      color: "rgba(0,0,0,0)",
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
      visible: true,
      autoscaleInfoProvider: lockedScaleProvider(),
    });

    oscChart.timeScale().subscribeVisibleLogicalRangeChange(function (range) {
      if (programmaticNavDepth > 0) return;
      if (!chart || !lowerVisible || timeSyncLock || !range) return;
      timeSyncLock = true;
      try {
        const cur = chart.timeScale().getVisibleLogicalRange();
        if (!sameLogical(cur, range)) {
          chart.timeScale().setVisibleLogicalRange(range);
        }
      } catch (err) {
        /* ignore */
      }
      timeSyncLock = false;
    });

    oscChart.subscribeCrosshairMove(function (param) {
      if (localXhairLock) return;
      if (!window.bridge) return;
      if (!param || param.time == null) {
        return;
      }
      const unix = Number(param.time);
      applyLocalPriceCrosshair(unix);
      if (unix === lastCrosshairTime) return;
      lastCrosshairTime = unix;
      window.bridge.on_crosshair_move(unix);
    });

    oscChart.subscribeClick(function (param) {
      if (!window.bridge || !param || param.time == null) return;
      window.bridge.on_chart_click(Number(param.time));
    });

    return oscChart;
  }

  function resizeOsc() {
    if (!oscChart || !lowerVisible) return;
    const size = oscSize();
    if (size.w < 16 || size.h < 16) return;
    oscChart.applyOptions({ width: size.w, height: size.h });
    lastAppliedOscSize = size;
    syncOscLogicalFromMain(
      chart ? chart.timeScale().getVisibleLogicalRange() : null
    );
  }

  function sameLogical(a, b) {
    if (!a || !b) return false;
    return Math.abs(Number(a.from) - Number(b.from)) < 0.05
      && Math.abs(Number(a.to) - Number(b.to)) < 0.05;
  }

  function syncOscLogicalFromMain(range) {
    if (!oscChart || !lowerVisible || timeSyncLock || !range) return;
    if (programmaticNavDepth > TIME_SYNC_MAX_DEPTH) return;
    timeSyncLock = true;
    try {
      const cur = oscChart.timeScale().getVisibleLogicalRange();
      if (!sameLogical(cur, range)) {
        oscChart.timeScale().setVisibleLogicalRange(range);
      }
    } catch (err) {
      /* ignore */
    }
    timeSyncLock = false;
  }

  function updateOscTimeBase() {
    if (!oscTimeBase) return;
    const candles = (lastPayload && lastPayload.candles) || [];
    const data = candles.map(function (c) {
      return { time: c.time };
    });
    oscTimeBase.setData(data);
  }

  function clearOscPriceLines() {
    oscPriceLines.forEach(function (item) {
      try {
        if (item && item.series && item.line) item.series.removePriceLine(item.line);
      } catch (err) {
        /* ignore */
      }
    });
    oscPriceLines = [];
  }

  function applyOscLevels(levels) {
    clearOscPriceLines();
    const host = oscSeriesById.get("k") || oscSeriesById.get("d") || oscTimeBase;
    if (!host || !levels) return;
    const style =
      LightweightCharts.LineStyle && LightweightCharts.LineStyle.Dashed != null
        ? LightweightCharts.LineStyle.Dashed
        : 2;
    for (let i = 0; i < levels.length; i++) {
      const lv = levels[i];
      if (!lv || lv.price == null) continue;
      const line = host.createPriceLine({
        price: Number(lv.price),
        color: lv.color || "#6b7388",
        lineWidth: 1,
        lineStyle: style,
        axisLabelVisible: true,
        title: lv.title || "",
      });
      oscPriceLines.push({ series: host, line: line });
    }
  }

  function applyOscSeries(seriesList) {
    oscValueByTime = new Map();
    const wanted = {};
    (seriesList || []).forEach(function (spec) {
      if (spec && spec.id) wanted[spec.id] = spec;
    });
    oscSeriesById.forEach(function (series, id) {
      if (!wanted[id]) {
        try {
          oscChart.removeSeries(series);
        } catch (err) {
          /* ignore */
        }
        oscSeriesById.delete(id);
      }
    });
    Object.keys(wanted).forEach(function (id) {
      const spec = wanted[id];
      let series = oscSeriesById.get(id);
      if (!series) {
        series = oscChart.addLineSeries({
          color: spec.color || "#5b8def",
          lineWidth: 2,
          priceLineVisible: false,
          lastValueVisible: true,
          crosshairMarkerVisible: true,
          visible: spec.visible !== false,
          autoscaleInfoProvider: lockedScaleProvider(),
        });
        oscSeriesById.set(id, series);
      } else {
        series.applyOptions({
          color: spec.color || "#5b8def",
          visible: spec.visible !== false,
          autoscaleInfoProvider: lockedScaleProvider(),
        });
      }
      const data = spec.data || [];
      series.setData(data);
      if (id === "k" || oscValueByTime.size === 0) {
        for (let i = 0; i < data.length; i++) {
          oscValueByTime.set(data[i].time, data[i].value);
        }
      }
    });
  }

  function applyLocalOscCrosshair(unix) {
    if (!oscChart || !lowerVisible || localXhairLock) return;
    localXhairLock = true;
    try {
      applyLocalOscCrosshairUnlocked(unix);
    } finally {
      localXhairLock = false;
    }
  }

  function applyLocalOscCrosshairUnlocked(unix) {
    if (!oscChart || !lowerVisible) return;
    const series = oscSeriesById.get("k") || oscTimeBase;
    if (!series) return;
    const value = oscValueByTime.has(unix) ? oscValueByTime.get(unix) : 50;
    try {
      oscChart.setCrosshairPosition(value, unix, series);
    } catch (err) {
      /* ignore */
    }
  }

  function applyLocalPriceCrosshair(unix) {
    if (!chart || !candleSeries || localXhairLock) return;
    const candle = candleByTime.get(unix);
    if (!candle) return;
    localXhairLock = true;
    try {
      chart.setCrosshairPosition(candle.close, unix, candleSeries);
    } catch (err) {
      /* ignore */
    }
    localXhairLock = false;
  }

  function setLowerPane(payload) {
    lastLowerPayload = payload || { id: "stochastic", visible: false };
    const visible = !!(lastLowerPayload && lastLowerPayload.visible);
    const legend = $("osc-legend");
    if (!visible) {
      setLowerOpen(false);
      if (legend) legend.textContent = "";
      if (oscChart) {
        applyOscSeries([]);
        clearOscPriceLines();
        if (oscTimeBase) oscTimeBase.setData([]);
      }
      if (chart) {
        resize();
      }
      return;
    }
    setLowerOpen(true);
    ensureOscChart();
    if (chart && oscChart) {
      try {
        const opt = chart.timeScale().options();
        oscChart.timeScale().applyOptions({
          barSpacing: opt.barSpacing,
          rightOffset: opt.rightOffset,
        });
      } catch (err) {
        /* ignore */
      }
    }
    if (legend) legend.textContent = lastLowerPayload.title || "";
    updateOscTimeBase();
    applyOscSeries(lastLowerPayload.series || []);
    applyOscLevels(lastLowerPayload.levels || []);
    resize();
    syncOscLogicalFromMain(
      chart ? chart.timeScale().getVisibleLogicalRange() : null
    );
  }

  function connectBridge() {
    if (window.bridge) {
      if (window.bridge.on_chart_ready) {
        window.bridge.on_chart_ready();
      }
      return;
    }
    if (
      typeof qt !== "undefined" &&
      qt.webChannelTransport &&
      typeof QWebChannel === "function"
    ) {
      new QWebChannel(qt.webChannelTransport, function (channel) {
        window.bridge = channel.objects.bridge;
        if (window.bridge && window.bridge.on_chart_ready) {
          window.bridge.on_chart_ready();
        }
      });
      return;
    }
    setTimeout(connectBridge, 40);
  }

  function debugInfo() {
    const candles = (lastPayload && lastPayload.candles) || [];
    const el = $("chart");
    let candleOpts = null;
    let logical = null;
    let barsInfo = null;
    try {
      candleOpts = candleSeries ? candleSeries.options() : null;
    } catch (e) {
      candleOpts = { error: String(e) };
    }
    try {
      logical = chart ? chart.timeScale().getVisibleLogicalRange() : null;
    } catch (e) {
      logical = { error: String(e) };
    }
    try {
      if (candleSeries && logical && candleSeries.barsInLogicalRange) {
        barsInfo = candleSeries.barsInLogicalRange(logical);
      }
    } catch (e) {
      barsInfo = { error: String(e) };
    }
    return {
      paneReady: !!(chart && candleSeries),
      timeframe: lastPayload ? lastPayload.timeframe : null,
      payloadCount: candles.length,
      candleSetCount: lastCandleSetCount,
      setDataError: lastSetDataError,
      first3: candles.slice(0, 3),
      last3: candles.slice(-3),
      emaOverlayCount: emaOverlaySeries.size,
      emaOverlayIds: Array.from(emaOverlaySeries.keys()),
      emaOverlayPeriods: ((lastEmaOverlays && lastEmaOverlays.series) || []).map(function (s) {
        return s.period;
      }),
      size: { w: el ? el.clientWidth : 0, h: el ? el.clientHeight : 0 },
      candleVisible: candleOpts ? candleOpts.visible : null,
      candleUpColor: candleOpts ? candleOpts.upColor : null,
      wickVisible: candleOpts ? candleOpts.wickVisible : null,
      logical: logical,
      barsInfo: barsInfo,
      overlayCount: overlayRegistry.size + researchMarkers.size,
      researchMarkerCount: researchMarkers.size,
      overlayLayoutCount: overlayLayoutCount,
      overlaySamples: overlayDebugSamples(),
      priceYSample: lastPriceYSample,
      interactionMode: interactionMode,
      previewActive: !!previewAnchor,
      priceAutoScale: priceScaleAuto(),
      barSpacing: timeScaleOption("barSpacing"),
      rightOffset: timeScaleOption("rightOffset"),
      defaultVisibleBars: DEFAULT_VISIBLE_BARS,
      defaultRightOffset: DEFAULT_RIGHT_OFFSET,
      resetViewCount: resetViewCount,
      lastReset: lastResetResult,
      shiftMeasure: snapshotShiftMeasure(),
      shiftMeasureVisible: !!(shiftMeasure && $("shift-measure") && $("shift-measure").style.display !== "none"),
      shiftMeasureDragging: !!(shiftMeasure && shiftMeasure.dragging),
      panPressedMouseMove: panPressedMouseMove(),
      shiftMeasureHandlersBound: shiftMeasureHandlersBound,
      lastCursorPrice: lastCursorPrice,
      lowerPaneVisible: lowerVisible,
      lowerPaneId: lastLowerPayload ? lastLowerPayload.id : null,
      lowerPriceMin:
        lastLowerPayload && lastLowerPayload.price_min != null
          ? lastLowerPayload.price_min
          : null,
      lowerPriceMax:
        lastLowerPayload && lastLowerPayload.price_max != null
          ? lastLowerPayload.price_max
          : null,
      lowerKCount: lowerSeriesCount("k"),
      lowerDCount: lowerSeriesCount("d"),
      lowerLogical: oscLogicalRange(),
      lowerSize: oscSize(),
      lowerOpen: !!(
        $("app") && $("app").classList.contains("lower-open")
      ),
    };
  }

  function lowerSeriesCount(id) {
    const list =
      lastLowerPayload && lastLowerPayload.series ? lastLowerPayload.series : [];
    for (let i = 0; i < list.length; i++) {
      if (list[i] && list[i].id === id) {
        return (list[i].data || []).length;
      }
    }
    return 0;
  }

  function oscLogicalRange() {
    if (!oscChart) return null;
    try {
      return oscChart.timeScale().getVisibleLogicalRange();
    } catch (e) {
      return { error: String(e) };
    }
  }

  function priceScaleAuto() {
    if (!chart) return null;
    try {
      const opts = chart.priceScale("right").options();
      return opts ? !!opts.autoScale : null;
    } catch (e) {
      return null;
    }
  }

  function timeScaleOption(name) {
    if (!chart) return null;
    try {
      const opts = chart.timeScale().options();
      return opts ? opts[name] : null;
    } catch (e) {
      return null;
    }
  }

  window.chartApi = {
    getChart: function () {
      return chart;
    },
    getCandleSeries: function () {
      return candleSeries;
    },
    setData: setData,
    updateFormingBar: updateFormingBar,
    setIndicatorVisible: setIndicatorVisible,
    clear: clear,
    resize: resize,
    setSyncedCrosshair: setSyncedCrosshair,
    clearSyncedCrosshair: clearSyncedCrosshair,
    setSelectedMarker: setSelectedMarker,
    addOverlay: addOverlay,
    updateOverlay: updateOverlay,
    removeOverlay: removeOverlay,
    clearOverlays: clearOverlays,
    setInteractionMode: setInteractionMode,
    setPreviewAnchor: setPreviewAnchor,
    clearPreview: clearPreview,
    setLldEma: setLldEma,
    setEmaOverlays: setEmaOverlays,
    setLowerPane: setLowerPane,
    syncLowerTime: function () {
      updateOscTimeBase();
      syncOscLogicalFromMain(
        chart ? chart.timeScale().getVisibleLogicalRange() : null
      );
      return true;
    },
    layoutOverlays: layoutOverlays,
    applyPriceScaleMargins: applyPriceScaleMargins,
    noteScaleInteraction: noteScaleInteraction,
    scrollTimeScale: scrollTimeScale,
    computeDefaultLogicalRange: computeDefaultLogicalRange,
    applyDefaultView: applyDefaultView,
    resetView: resetView,
    DEFAULT_VISIBLE_BARS: DEFAULT_VISIBLE_BARS,
    DEFAULT_RIGHT_OFFSET: DEFAULT_RIGHT_OFFSET,
    computePositionStats: computePositionStats,
    pricesFromTwoPoints: pricesFromTwoPoints,
    computeShiftMeasure: computeShiftMeasure,
    startShiftMeasure: apiStartShiftMeasure,
    updateShiftMeasure: apiUpdateShiftMeasure,
    endShiftMeasure: finishShiftMeasure,
    clearShiftMeasure: clearShiftMeasure,
    getShiftMeasure: snapshotShiftMeasure,
    simulateShiftPointerDown: function (opts) {
      return onShiftMeasureDown(makeFakePointer(opts));
    },
    simulateLostPointerCapture: onLostPointerCapture,
    simulateWindowBlur: onWindowBlur,
    simulateWindowLeave: function () {
      onDocumentMouseOut({ relatedTarget: null });
    },
    dismissShiftMeasureByClick: clearShiftMeasureIfIdle,
    destroyUi: destroyUi,
    setHostShift: setHostShift,
    setVolumeProfile: setVolumeProfile,
    clearVolumeProfile: clearVolumeProfile,
    setOrderbookProfile: setOrderbookProfile,
    clearOrderbookProfile: clearOrderbookProfile,
    setOrderbookLevels: setOrderbookLevels,
    clearOrderbookLevels: clearOrderbookLevels,
    debugOrderbookLevels: debugOrderbookLevels,
    setTradeBubbles: setTradeBubbles,
    clearTradeBubbles: clearTradeBubbles,
    tradeBubbleAtUnix: tradeBubbleAtUnix,
    tradeBubbleAtPoint: tradeBubbleAtPoint,
    getVisibleTimeRange: getVisibleTimeRange,
    setVisibleTimeRange: setVisibleTimeRange,
    focusOnTime: focusOnTime,
    setFollowLive: setFollowLive,
    setReplayViewLock: setReplayViewLock,
    clearReplayViewLock: clearReplayViewLock,
    enforceReplayViewLock: enforceReplayViewLock,
    debugInfo: debugInfo,
  };

  window.addEventListener("DOMContentLoaded", function () {
    initChart();
    connectBridge();
  });
})();
