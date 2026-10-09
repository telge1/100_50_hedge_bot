/* Isolated Phase 11T viewer. Does not patch the production research chart. */
(function () {
  const COLORS = ["#005BFF", "#00E5FF", "#00E85A", "#FFF12B", "#F00032"];
  const chartEl = document.getElementById("chart");
  const overlay = document.getElementById("overlay");
  const ctx = overlay.getContext("2d");
  const statusEl = document.getElementById("status");
  const closesEl = document.getElementById("closes");
  let chart;
  let series;
  let payload = null;
  let lastDraw = { original: 0, remaining: 0, colors: [] };

  function decisionUnix(iso) {
    return Math.floor(Date.parse(iso) / 1000);
  }

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
    if (showOriginal) {
      ctx.lineWidth = 1;
      for (const box of payload.original) {
        const x1 = xAt(box.t0);
        const yTop = series.priceToCoordinate(box.top);
        const yBot = series.priceToCoordinate(box.bottom);
        if (x1 == null || xRight == null || yTop == null || yBot == null) continue;
        ctx.strokeStyle = box.color;
        ctx.globalAlpha = 0.95;
        ctx.strokeRect(x1, yTop, Math.max(1, xRight - x1), Math.max(1, yBot - yTop));
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
    lastDraw = { original, remaining, colors: Array.from(colors) };
  }

  function show(data) {
    payload = data;
    const dc = decisionUnix(data.decision_close);
    const future = data.candles.filter((c) => c.time + 300 > dc);
    if (future.length) throw new Error("payload contains a candle after the decision close");
    series.setData(data.candles);
    chart.timeScale().fitContent();
    requestAnimationFrame(() => requestAnimationFrame(redraw));
    const omitted = data.fully_traversed_active_ids.length;
    statusEl.textContent =
      data.decision_close +
      " candles " + data.candles.length +
      " active " + data.original.length +
      " remaining boxes " + data.remaining.length +
      " fully traversed active omitted from fill " + omitted;
    document.querySelectorAll("button[data-close]").forEach((btn) => {
      btn.setAttribute("aria-pressed", btn.dataset.close === data.decision_close ? "true" : "false");
    });
  }

  async function loadClose(iso) {
    const name = iso.replace(/:/g, "").replace(/-/g, "");
    const res = await fetch("./payloads/" + name + ".json");
    if (!res.ok) throw new Error("payload missing " + name);
    show(await res.json());
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
    if (!payload) return { ready: false };
    const dc = decisionUnix(payload.decision_close);
    const last = payload.candles[payload.candles.length - 1];
    return {
      ready: true,
      decision_close: payload.decision_close,
      candle_count: payload.candles.length,
      last_open_unix: last ? last.time : null,
      last_close_unix: last ? last.time + 300 : null,
      future_candles: payload.candles.filter((c) => c.time + 300 > dc).length,
      active_count: payload.original.length,
      remaining_payload: payload.remaining.length,
      remaining_drawn: lastDraw.remaining,
      original_drawn: lastDraw.original,
      colors_drawn: lastDraw.colors,
      fully_traversed_omitted: payload.fully_traversed_active_ids.length,
      snapshot_id: payload.snapshot_id,
    };
  };

  document.getElementById("layer-original").addEventListener("change", redraw);
  document.getElementById("layer-remaining").addEventListener("change", redraw);

  bootChart();
  fetch("./payloads/index.json")
    .then((r) => r.json())
    .then(async (index) => {
      for (const iso of index.closes) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.dataset.close = iso;
        btn.textContent = iso.replace(".000Z", "Z").replace("+00:00", "Z");
        btn.addEventListener("click", () => loadClose(iso));
        closesEl.appendChild(btn);
      }
      await loadClose(index.closes[0]);
    })
    .catch((err) => {
      statusEl.textContent = String(err);
    });
})();
