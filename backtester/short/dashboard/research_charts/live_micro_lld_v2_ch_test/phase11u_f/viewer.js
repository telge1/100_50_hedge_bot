/* Isolated Phase 11U-F viewer. The Phase 11T files are not modified. */
(function () {
  const COLORS = ["#005BFF", "#00E5FF", "#00E85A", "#FFF12B", "#F00032"];
  const chartEl = document.getElementById("chart");
  const overlay = document.getElementById("overlay");
  const ctx = overlay.getContext("2d");
  const errorEl = document.getElementById("error");
  const metaEl = document.getElementById("meta");
  const selectEl = document.getElementById("close-select");
  const caseEl = document.getElementById("case-select");
  let chart;
  let series;
  let payload = null;
  let index = null;
  let lastDraw = { original: 0, remaining: 0, colors: [], focus: false };
  const errors = [];
  window.addEventListener("error", (event) => errors.push(String(event.message)));

  function resizeOverlay() {
    const dpr = window.devicePixelRatio || 1;
    overlay.width = Math.max(1, chartEl.clientWidth * dpr);
    overlay.height = Math.max(1, chartEl.clientHeight * dpr);
    overlay.style.width = chartEl.clientWidth + "px";
    overlay.style.height = chartEl.clientHeight + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function redraw() {
    if (!chart || !series || !payload) return;
    resizeOverlay();
    ctx.clearRect(0, 0, chartEl.clientWidth, chartEl.clientHeight);
    const showOriginal = document.getElementById("layer-original").checked;
    const showRemaining = document.getElementById("layer-remaining").checked;
    const ts = chart.timeScale();
    const times = payload.candles.map((c) => c.time);
    function xAt(t) {
      let chosen = times[0];
      for (const time of times) {
        if (time <= t) chosen = time;
        else break;
      }
      return ts.timeToCoordinate(chosen);
    }
    const xLast = ts.timeToCoordinate(times[times.length - 1]);
    const xPrev = ts.timeToCoordinate(times[Math.max(0, times.length - 2)]);
    const xRight = xLast == null || xPrev == null ? null : xLast + Math.max(1, xLast - xPrev);
    let original = 0;
    let remaining = 0;
    const colors = new Set();
    let focus = false;
    if (showOriginal) {
      for (const box of payload.original) {
        const x1 = xAt(box.t0);
        const yTop = series.priceToCoordinate(box.top);
        const yBot = series.priceToCoordinate(box.bottom);
        if (x1 == null || xRight == null || yTop == null || yBot == null) continue;
        ctx.save();
        ctx.strokeStyle = box.color;
        ctx.lineWidth = box.pool_id === payload.focus_pool_id ? 2 : 1;
        ctx.setLineDash(box.side === "lower" ? [4, 3] : []);
        ctx.globalAlpha = 0.95;
        ctx.strokeRect(x1, yTop, Math.max(1, xRight - x1), Math.max(1, yBot - yTop));
        ctx.restore();
        if (box.pool_id === payload.focus_pool_id) focus = true;
        original += 1;
      }
    }
    if (showRemaining) {
      for (const box of payload.remaining) {
        const x1 = xAt(box.t0);
        const yTop = series.priceToCoordinate(box.top);
        const yBot = series.priceToCoordinate(box.bottom);
        if (x1 == null || xRight == null || yTop == null || yBot == null) continue;
        if (!COLORS.includes(box.color)) continue;
        ctx.fillStyle = box.color;
        ctx.globalAlpha = 0.72;
        ctx.fillRect(x1, yTop, Math.max(1, xRight - x1), Math.max(1, yBot - yTop));
        colors.add(box.color);
        remaining += 1;
      }
    }
    ctx.globalAlpha = 1;
    lastDraw = { original, remaining, colors: Array.from(colors), focus };
    window.__scale = {
      top: series.coordinateToPrice(8),
      bottom: series.coordinateToPrice(Math.max(9, chartEl.clientHeight - 8)),
    };
  }

  function show(data) {
    const dc = Math.floor(Date.parse(data.decision_close) / 1000);
    if (data.candles.some((candle) => candle.time + 300 > dc)) {
      throw new Error("payload contains a candle after the decision close");
    }
    payload = data;
    const focus = data.focus_pool_id ? data.original.find((row) => row.pool_id === data.focus_pool_id) : null;
    if (focus) {
      const pad = Math.max((focus.top - focus.bottom) * 6, focus.top * 0.0015);
      series.applyOptions({
        autoscaleInfoProvider: () => ({
          priceRange: { minValue: focus.bottom - pad, maxValue: focus.top + pad },
        }),
      });
    } else {
      series.applyOptions({ autoscaleInfoProvider: undefined });
    }
    series.setData(data.candles);
    chart.timeScale().fitContent();
    redraw();
    requestAnimationFrame(redraw);
    selectEl.value = data.decision_close;
    errorEl.textContent = "";
    metaEl.textContent =
      data.decision_close +
      " | snapshot " + data.snapshot_id.slice(0, 12) +
      " | composite " + data.composite_input_id.slice(0, 12) +
      " | active " + data.original.length +
      " | remaining " + data.remaining.length +
      " | candles " + data.candles.length;
  }

  async function loadClose(iso, focus) {
    const url = "./api/payload?close=" + encodeURIComponent(iso) + (focus ? "&focus=" + encodeURIComponent(focus) : "");
    const started = performance.now();
    const res = await fetch(url);
    if (!res.ok) {
      const body = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(body.detail || "payload request failed");
    }
    const data = await res.json();
    data.__fetch_ms = performance.now() - started;
    show(data);
  }

  function bootChart() {
    chart = LightweightCharts.createChart(chartEl, {
      layout: { background: { color: "#0e1116" }, textColor: "#d5dbe3" },
      grid: { vertLines: { color: "#1c2430" }, horzLines: { color: "#1c2430" } },
      rightPriceScale: { borderColor: "#3a4658" },
      timeScale: { borderColor: "#3a4658", timeVisible: true, secondsVisible: false },
      crosshair: { mode: 0 },
    });
    series = chart.addCandlestickSeries({
      upColor: "#d5dbe3",
      downColor: "#6e7a89",
      borderUpColor: "#d5dbe3",
      borderDownColor: "#6e7a89",
      wickUpColor: "#d5dbe3",
      wickDownColor: "#6e7a89",
    });
    chart.timeScale().subscribeVisibleLogicalRangeChange(redraw);
    new ResizeObserver(redraw).observe(chartEl);
  }

  window.__pocAudit = function () {
    if (!payload || !index) return { ready: false, errors };
    const dc = Math.floor(Date.parse(payload.decision_close) / 1000);
    const last = payload.candles[payload.candles.length - 1];
    return {
      ready: true,
      errors,
      decision_close: payload.decision_close,
      close_count: index.closes.length,
      composite_input_id: payload.composite_input_id,
      snapshot_id: payload.snapshot_id,
      candle_count: payload.candles.length,
      last_close_unix: last ? last.time + 300 : null,
      future_candles: payload.candles.filter((candle) => candle.time + 300 > dc).length,
      active_count: payload.original.length,
      remaining_payload: payload.remaining.length,
      remaining_drawn: lastDraw.remaining,
      original_drawn: lastDraw.original,
      colors_drawn: lastDraw.colors,
      focus_pool_id: payload.focus_pool_id,
      focus_drawn: lastDraw.focus,
      fully_traversed_omitted: payload.fully_traversed_active_ids.length,
      original_layer: document.getElementById("layer-original").checked,
      remaining_layer: document.getElementById("layer-remaining").checked,
      fetch_ms: payload.__fetch_ms || null,
      price_scale: window.__scale || null,
    };
  };

  document.getElementById("layer-original").addEventListener("change", redraw);
  document.getElementById("layer-remaining").addEventListener("change", redraw);
  selectEl.addEventListener("change", () => loadClose(selectEl.value).catch((err) => { errorEl.textContent = String(err); }));
  document.getElementById("prev").addEventListener("click", () => {
    const pos = index.closes.indexOf(payload.decision_close);
    if (pos > 0) loadClose(index.closes[pos - 1]).catch((err) => { errorEl.textContent = String(err); });
  });
  document.getElementById("next").addEventListener("click", () => {
    const pos = index.closes.indexOf(payload.decision_close);
    if (pos >= 0 && pos + 1 < index.closes.length) loadClose(index.closes[pos + 1]).catch((err) => { errorEl.textContent = String(err); });
  });
  document.getElementById("goto-btn").addEventListener("click", () => {
    loadClose(document.getElementById("goto").value).catch((err) => { errorEl.textContent = String(err); });
  });
  document.getElementById("case-btn").addEventListener("click", () => {
    const item = index.cases[Number(caseEl.value)];
    if (item) loadClose(item.decision_close, item.pool_id).catch((err) => { errorEl.textContent = String(err); });
  });

  bootChart();
  fetch("./api/index")
    .then((res) => res.json())
    .then(async (body) => {
      index = body;
      for (const iso of body.closes) {
        const option = document.createElement("option");
        option.value = iso;
        option.textContent = iso;
        selectEl.appendChild(option);
      }
      body.cases.forEach((item, pos) => {
        const option = document.createElement("option");
        option.value = String(pos);
        option.textContent = item.decision_close + " " + item.side + " " + item.pool_id.split(":").slice(-2).join(":");
        caseEl.appendChild(option);
      });
      await loadClose(body.closes[0]);
    })
    .catch((err) => { errorEl.textContent = String(err); });
})();
