/**
 * V2 historical trade chart: candles + frozen snapshot zones + Entry/SL/TP lines.
 */
(function (global) {
  "use strict";

  const FROZEN = "cfae2571806c823407767e4d81da82fe984a928ce29843c9f061fc66d79c115b";
  const CATALOG = "doge_multi_month_2026";

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

  function ChartModal(opts) {
    this.backdrop = document.getElementById(opts.backdropId || "emaV2ChartModal");
    this.titleEl = document.getElementById(opts.titleId || "emaV2ChartTitle");
    this.subEl = document.getElementById(opts.subId || "emaV2ChartSub");
    this.hostEl = document.getElementById(opts.hostId || "emaV2ChartHost");
    this.closeBtn = document.getElementById(opts.closeId || "emaV2ChartClose");
    this.chart = null;
    this.series = null;
    this.priceLines = [];

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
    if (this.chart) {
      try {
        this.chart.remove();
      } catch (_) {}
    }
    this.chart = null;
    this.series = null;
    if (this.hostEl) this.hostEl.innerHTML = "";
  };

  ChartModal.prototype.close = function () {
    if (this.backdrop) this.backdrop.classList.remove("open");
    this._destroyChart();
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

  ChartModal.prototype._drawZones = function (zones, triggerPoolId) {
    const max = 120;
    const list = Array.isArray(zones) ? zones.slice(0, max) : [];
    list.forEach((z) => {
      const pid = String(z.pool_id || "");
      const isTrig = pid && pid === String(triggerPoolId || "");
      const col = isTrig ? "rgba(250, 204, 21, 0.85)" : "rgba(148, 163, 184, 0.35)";
      const w = isTrig ? 2 : 1;
      [z.bottom_price, z.top_price].forEach((p) => {
        if (p == null) return;
        try {
          const line = this.series.createPriceLine({
            price: Number(p),
            color: col,
            lineWidth: w,
            lineStyle: isTrig ? 0 : 2,
            axisLabelVisible: isTrig,
            title: isTrig ? "TRIGGER" : "",
          });
          this.priceLines.push(line);
        } catch (_) {}
      });
    });
  };

  ChartModal.prototype.open = async function (trade) {
    if (!this.backdrop || !this.hostEl || !global.LightweightCharts) {
      console.warn("V2 chart modal: missing DOM or LightweightCharts");
      return;
    }
    this._destroyChart();
    this.backdrop.classList.add("open");

    const symbol = String(trade.symbol || "DOGEUSDT").toUpperCase();
    const dc = trade.decision_close;
    if (this.titleEl) {
      this.titleEl.textContent = `${symbol} SHORT · HISTORICAL DECISION SNAPSHOT`;
    }
    if (this.subEl) {
      this.subEl.textContent = `Lade … · ${fmtUtc(dc)}`;
    }

    const chart = global.LightweightCharts.createChart(this.hostEl, {
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

      this._drawZones(snap.zones || [], trade.pool_id);
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
      if (this.subEl) {
        this.subEl.textContent =
          `HISTORICAL DECISION SNAPSHOT @ ${fmtUtc(dc)}` +
          ` · fingerprint ${snap.active_state_fingerprint}` +
          ` · ACTIVE pools ${snap.active_pool_count}` +
          ` · frozen ${snap.frozen_input_id}` +
          ` · trigger ${trade.pool_id}` +
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
        chart.applyOptions({ width: this.hostEl.clientWidth, height: this.hostEl.clientHeight });
      } catch (_) {}
    });
  };

  global.EmaSignaleV2ChartModal = ChartModal;
})(window);
