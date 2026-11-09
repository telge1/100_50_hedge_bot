/**
 * V2 historical trade chart: candles + frozen snapshot pool bands + Entry/SL/TP lines.
 */
(function (global) {
  "use strict";

  const FROZEN = "cfae2571806c823407767e4d81da82fe984a928ce29843c9f061fc66d79c115b";
  const CATALOG = "doge_multi_month_2026";

  const DEFAULT_UPPER_FILL = "rgba(239, 68, 68, 0.22)";
  const DEFAULT_LOWER_FILL = "rgba(59, 130, 246, 0.22)";
  const TRIGGER_FILL = "rgba(250, 204, 21, 0.42)";
  const TRIGGER_BORDER = "rgba(250, 204, 21, 0.95)";

  function pad2(n) {
    return String(n).padStart(2, "0");
  }

  function fmtUtc(iso) {
    if (!iso) return "–";
    const d = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z");
    if (Number.isNaN(d.getTime())) return String(iso);
    return (
      `${d.getUTCFullYear()}-${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())} ` +
      `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}:${pad2(d.getUTCSeconds())} UTC`
    );
  }

  function normalizeRenderZones(snap, triggerPoolId) {
    const trig = String(triggerPoolId || "");
    const fromApi = Array.isArray(snap.render_zones) ? snap.render_zones : [];
    if (fromApi.length) {
      return fromApi.map((z) => ({
        ...z,
        is_trigger_pool: trig && String(z.pool_id) === trig,
      }));
    }
    const raw = Array.isArray(snap.zones) ? snap.zones : [];
    return raw.map((z) => {
      const style = z.style || {};
      const meta = z.metadata || {};
      const pid = String(z.pool_id || "");
      return {
        pool_id: pid,
        side: String(z.side || ""),
        bottom_price: Number(z.bottom_price),
        top_price: Number(z.top_price),
        strength_class: z.strength_class,
        known_at: meta.known_at,
        fill_color: style.fillColor,
        border_color: style.color,
        start_timestamp: Number(z.start_timestamp || 0),
        end_timestamp: Number(z.end_timestamp || 0),
        is_trigger_pool: trig && pid === trig,
      };
    });
  }

  function fillForZone(z) {
    if (z.is_trigger_pool) return TRIGGER_FILL;
    if (z.fill_color && String(z.fill_color).length >= 7) {
      return String(z.fill_color);
    }
    const side = String(z.side || "").toLowerCase();
    return side === "upper" ? DEFAULT_UPPER_FILL : DEFAULT_LOWER_FILL;
  }

  function ChartModal(opts) {
    this.backdrop = document.getElementById(opts.backdropId || "emaV2ChartModal");
    this.titleEl = document.getElementById(opts.titleId || "emaV2ChartTitle");
    this.subEl = document.getElementById(opts.subId || "emaV2ChartSub");
    this.hostEl = document.getElementById(opts.hostId || "emaV2ChartHost");
    this.closeBtn = document.getElementById(opts.closeId || "emaV2ChartClose");
    this.chart = null;
    this.series = null;
    this.chartEl = null;
    this.overlayCanvas = null;
    this.priceLines = [];
    this._overlayState = null;
    this._overlayRedraw = null;

    if (this.closeBtn) {
      this.closeBtn.addEventListener("click", () => this.close());
    }
    if (this.backdrop) {
      this.backdrop.addEventListener("click", (e) => {
        if (e.target === this.backdrop) this.close();
      });
    }
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && this.backdrop && this.backdrop.classList.contains("open")) {
        this.close();
      }
    });
  }

  ChartModal.prototype._destroyChart = function () {
    this.priceLines = [];
    this._overlayState = null;
    this._overlayRedraw = null;
    if (this.chart) {
      try {
        this.chart.remove();
      } catch (_) {}
    }
    this.chart = null;
    this.series = null;
    this.chartEl = null;
    this.overlayCanvas = null;
    if (this.hostEl) this.hostEl.innerHTML = "";
  };

  ChartModal.prototype.close = function () {
    if (this.backdrop) this.backdrop.classList.remove("open");
    this._destroyChart();
  };

  ChartModal.prototype._mountChartShell = function () {
    if (!this.hostEl) return;
    this.hostEl.innerHTML = "";
    this.hostEl.style.position = "relative";
    this.hostEl.style.minHeight = "420px";

    const wrap = document.createElement("div");
    wrap.className = "v2-chart-wrap";
    wrap.style.cssText = "position:relative;width:100%;height:100%;min-height:420px;";

    this.chartEl = document.createElement("div");
    this.chartEl.className = "v2-chart-lw";
    this.chartEl.style.cssText = "position:absolute;inset:0;";

    this.overlayCanvas = document.createElement("canvas");
    this.overlayCanvas.className = "v2-pool-overlay";
    this.overlayCanvas.setAttribute("aria-hidden", "true");
    this.overlayCanvas.style.cssText =
      "position:absolute;inset:0;width:100%;height:100%;pointer-events:none;z-index:2;";

    wrap.appendChild(this.chartEl);
    wrap.appendChild(this.overlayCanvas);
    this.hostEl.appendChild(wrap);
  };

  ChartModal.prototype._addLine = function (price, color, title, dashed) {
    if (!this.series || price == null || !Number.isFinite(Number(price))) return;
    const line = this.series.createPriceLine({
      price: Number(price),
      color,
      lineWidth: 2,
      lineStyle: dashed ? 2 : 0,
      axisLabelVisible: true,
      title: title || "",
    });
    this.priceLines.push(line);
  };

  ChartModal.prototype._drawPoolOverlay = function (timeFrom, timeTo) {
    if (!this.chart || !this.series || !this.overlayCanvas || !this._overlayState) return;
    const zones = this._overlayState.zones || [];
    const w = this.chartEl ? this.chartEl.clientWidth : 0;
    const h = this.chartEl ? this.chartEl.clientHeight : 0;
    if (!w || !h) return;

    const canvas = this.overlayCanvas;
    const dpr = global.devicePixelRatio || 1;
    canvas.width = Math.floor(w * dpr);
    canvas.height = Math.floor(h * dpr);
    canvas.style.width = w + "px";
    canvas.style.height = h + "px";

    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    const ts = this.chart.timeScale();
    const x0 = ts.timeToCoordinate(timeFrom);
    const x1 = ts.timeToCoordinate(timeTo);
    if (x0 == null || x1 == null) return;
    const left = Math.min(x0, x1);
    const width = Math.max(2, Math.abs(x1 - x0));

    const regular = zones.filter((z) => !z.is_trigger_pool);
    const triggers = zones.filter((z) => z.is_trigger_pool);

    function paintZone(z, emphasis) {
      const yTop = this.series.priceToCoordinate(Number(z.top_price));
      const yBot = this.series.priceToCoordinate(Number(z.bottom_price));
      if (yTop == null || yBot == null) return;
      const top = Math.min(yTop, yBot);
      const height = Math.max(1, Math.abs(yBot - yTop));
      ctx.fillStyle = fillForZone(z);
      ctx.fillRect(left, top, width, height);
      if (emphasis) {
        ctx.strokeStyle = TRIGGER_BORDER;
        ctx.lineWidth = 2;
        ctx.strokeRect(left, top, width, height);
      } else if (z.border_color) {
        ctx.strokeStyle = String(z.border_color);
        ctx.globalAlpha = 0.55;
        ctx.lineWidth = 1;
        ctx.strokeRect(left + 0.5, top + 0.5, width - 1, height - 1);
        ctx.globalAlpha = 1;
      }
    }

    regular.forEach((z) => paintZone.call(this, z, false));
    triggers.forEach((z) => paintZone.call(this, z, true));
  };

  ChartModal.prototype._wirePoolOverlay = function (zones, timeFrom, timeTo) {
    this._overlayState = { zones, timeFrom, timeTo };
    const redraw = () => this._drawPoolOverlay(timeFrom, timeTo);
    this._overlayRedraw = redraw;
    redraw();
    try {
      this.chart.timeScale().subscribeVisibleLogicalRangeChange(redraw);
    } catch (_) {}
    try {
      this.chart.subscribeCrosshairMove(redraw);
    } catch (_) {}
  };

  ChartModal.prototype.open = async function (trade) {
    if (!this.backdrop || !this.hostEl || !global.LightweightCharts) {
      console.warn("V2 chart modal: missing DOM or LightweightCharts");
      return;
    }
    this._destroyChart();
    this.backdrop.classList.add("open");
    this._mountChartShell();

    const symbol = String(trade.symbol || "DOGEUSDT").toUpperCase();
    const dc = trade.decision_close;
    const triggerPoolId = trade.pool_id;
    if (this.titleEl) {
      this.titleEl.textContent = `${symbol} SHORT · HISTORICAL DECISION SNAPSHOT`;
    }
    if (this.subEl) {
      this.subEl.textContent = `Lade … · ${fmtUtc(dc)}`;
    }

    const chart = global.LightweightCharts.createChart(this.chartEl, {
      layout: { background: { color: "#020617" }, textColor: "#e5e7eb" },
      grid: { vertLines: { color: "#1f2937" }, horzLines: { color: "#1f2937" } },
      timeScale: { timeVisible: true, secondsVisible: false },
    });
    const series = chart.addSeries(global.LightweightCharts.CandlestickSeries, {
      upColor: "#26a69a",
      downColor: "#ef5350",
      borderVisible: false,
      wickUpColor: "#26a69a",
      wickDownColor: "#ef5350",
      lastValueVisible: false,
      priceLineVisible: false,
    });
    this.chart = chart;
    this.series = series;

    try {
      const tDec = Math.floor(Date.parse(dc) / 1000);
      const t0 = tDec - 6 * 3600;
      const t1 = tDec + 6 * 3600;
      const candleUrl =
        `/api/research/candles?symbol=${encodeURIComponent(symbol)}` +
        `&timeframe=15m&from=${t0}&to=${t1}`;
      const snapRes = await fetch("/api/research/historical-lmv2/decision-snapshot", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          catalog_id: trade.catalog_id || CATALOG,
          symbol,
          timeframe: trade.timeframe || "15m",
          pool_detail: trade.pool_detail || 10,
          decision_close: dc,
          expected_frozen_input_id: trade.frozen_input_id || FROZEN,
        }),
      });
      const snap = await snapRes.json();
      if (!snapRes.ok || !snap.success) {
        throw new Error(snap.message || snap.error || `Snapshot HTTP ${snapRes.status}`);
      }

      const renderZones = normalizeRenderZones(snap, triggerPoolId);
      if (!renderZones.length) {
        throw new Error("Snapshot enthält keine Pool-Zonen zum Rendern");
      }

      const cRes = await fetch(candleUrl, { credentials: "include" });
      const cData = await cRes.json();
      const candles = (cData.candles || []).map((c) => ({
        time: Number(c.time),
        open: Number(c.open),
        high: Number(c.high),
        low: Number(c.low),
        close: Number(c.close),
      }));
      if (!candles.length) throw new Error("Keine 15m-Kerzen für dieses Fenster");
      series.setData(candles);

      this._addLine(trade.entry_price, "#9ca3af", "Entry", false);
      this._addLine(trade.stop, "#ef4444", "SL", true);
      this._addLine(trade.tp, "#22c55e", "TP", true);

      const markers = [];
      const decCandle = candles.find((c) => c.time >= tDec) || candles[0];
      if (decCandle) {
        markers.push({
          time: decCandle.time,
          position: "aboveBar",
          color: "#94a3b8",
          shape: "circle",
          text: "DC",
        });
      }
      if (trade.exit_time) {
        const tEx = Math.floor(Date.parse(trade.exit_time) / 1000);
        const exC = candles.find((c) => c.time >= tEx);
        if (exC) {
          markers.push({
            time: exC.time,
            position: "belowBar",
            color: trade.exit_reason === "TP" ? "#22c55e" : "#ef4444",
            shape: "arrowDown",
            text: String(trade.exit_reason || "EXIT"),
          });
        }
      }
      if (markers.length && typeof global.LightweightCharts.createSeriesMarkers === "function") {
        try {
          global.LightweightCharts.createSeriesMarkers(series, markers);
        } catch (_) {}
      }

      chart.timeScale().fitContent();
      const bandFrom = candles[0].time;
      const bandTo = candles[candles.length - 1].time;
      this._wirePoolOverlay(renderZones, bandFrom, bandTo);

      const trigCount = renderZones.filter((z) => z.is_trigger_pool).length;
      if (this.subEl) {
        this.subEl.textContent =
          `HISTORICAL DECISION SNAPSHOT @ ${fmtUtc(dc)}` +
          ` · fingerprint ${snap.active_state_fingerprint}` +
          ` · ACTIVE pools ${snap.active_pool_count}` +
          ` · zones drawn ${renderZones.length}` +
          (trigCount ? " · trigger highlighted" : " · trigger NOT in zone set") +
          ` · layers: Candles · Historical V2 Pools · Entry/SL/TP` +
          ` · frozen ${snap.frozen_input_id}` +
          ` · trigger ${triggerPoolId}` +
          ` · bridge ${trade.bridge_upper_id || "NOT_TRACED"}` +
          ` · tp_lower ${trade.tp_lower_id || "NOT_TRACED"}`;
      }
    } catch (err) {
      if (this.subEl) {
        this.subEl.textContent = "Chart-Fehler: " + (err.message || err);
      }
    }

    requestAnimationFrame(() => {
      try {
        chart.applyOptions({ width: this.chartEl.clientWidth, height: this.chartEl.clientHeight });
        if (this._overlayRedraw) this._overlayRedraw();
      } catch (_) {}
    });
  };

  global.EmaSignaleV2ChartModal = ChartModal;
})(window);
