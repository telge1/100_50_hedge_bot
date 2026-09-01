/**
 * Shared UTC helpers + chart modal for Stoch-Profite / Stoch-Signale.
 * All timestamps displayed in UTC. Entry marked with axis label + candle marker.
 */
(function (global) {
  "use strict";

  function pad2(n) {
    return String(n).padStart(2, "0");
  }

  function toUtcMs(ts) {
    if (ts === null || ts === undefined || ts === "") return null;
    if (typeof ts === "string" && ts.includes("-") && Number.isNaN(Number(ts))) {
      const d = new Date(ts.endsWith("Z") || /[+-]\d{2}:\d{2}$/.test(ts) ? ts : ts + "Z");
      const ms = d.getTime();
      return Number.isNaN(ms) ? null : ms;
    }
    const n = Number(ts);
    if (!Number.isFinite(n)) return null;
    return n > 1e12 ? n : n * 1000;
  }

  function fmtUtc(ts) {
    const ms = toUtcMs(ts);
    if (ms === null) return "–";
    const d = new Date(ms);
    return (
      `${d.getUTCFullYear()}-${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())} ` +
      `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}:${pad2(d.getUTCSeconds())} UTC`
    );
  }

  function fmtUtcShort(unixSec) {
    const d = new Date(Number(unixSec) * 1000);
    if (Number.isNaN(d.getTime())) return "";
    return (
      `${pad2(d.getUTCDate())}.${pad2(d.getUTCMonth() + 1)} ` +
      `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}`
    );
  }

  function fmt(n, digits) {
    if (n === null || n === undefined || Number.isNaN(Number(n))) return "–";
    return Number(n).toFixed(digits);
  }

  function asNum(v) {
    if (v === null || v === undefined || v === "") return null;
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }

  global.StochUtc = { fmtUtc: fmtUtc, toUtcMs: toUtcMs, fmtUtcShort: fmtUtcShort };

  function ChartModal(opts) {
    this.backdrop = document.getElementById(opts.backdropId || "stochChartModal");
    this.titleEl = document.getElementById(opts.titleId || "stochChartTitle");
    this.subEl = document.getElementById(opts.subId || "stochChartSub");
    this.hostEl = document.getElementById(opts.hostId || "stochChartHost");
    this.closeBtn = document.getElementById(opts.closeId || "stochChartClose");
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

  ChartModal.prototype.open = async function (trade) {
    if (!this.backdrop || !this.hostEl || !global.LightweightCharts) {
      console.warn("Stoch chart modal: missing DOM or LightweightCharts");
      return;
    }
    this._destroyChart();
    this.backdrop.classList.add("open");

    const symbol = String(trade.symbol || "").toUpperCase();
    const direction = String(trade.trade_direction || "").toUpperCase();
    const mode = trade.close_price != null || trade.trade_state ? "Resultat" : "Signal";

    if (this.titleEl) this.titleEl.textContent = `${symbol} · ${mode} · UTC`;
    if (this.subEl) {
      const bits = [
        direction || "–",
        trade.trade_state || trade.signal_state || "",
        trade.is_demo ? "Demo" : "",
      ].filter(Boolean);
      this.subEl.textContent = bits.join(" · ");
    }

    const chart = LightweightCharts.createChart(this.hostEl, {
      layout: {
        background: { color: "#020617" },
        textColor: "#e5e7eb",
      },
      grid: {
        vertLines: { color: "#1f2937" },
        horzLines: { color: "#1f2937" },
      },
      rightPriceScale: {
        borderColor: "#374151",
        autoScale: true,
      },
      localization: {
        locale: "en-GB",
        timeFormatter: (time) => {
          const sec = typeof time === "object" ? null : Number(time);
          if (sec == null || !Number.isFinite(sec)) return "";
          return fmtUtc(sec);
        },
      },
      timeScale: {
        borderColor: "#374151",
        timeVisible: true,
        secondsVisible: false,
        tickMarkFormatter: (time) => fmtUtcShort(time),
      },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    });

    const series = chart.addSeries(LightweightCharts.CandlestickSeries, {
      upColor: "#26a69a",
      downColor: "#ef5350",
      borderVisible: false,
      wickUpColor: "#26a69a",
      wickDownColor: "#ef5350",
      lastValueVisible: false,
      priceLineVisible: false,
      priceFormat: { type: "price", precision: 6, minMove: 0.000001 },
    });

    this.chart = chart;
    this.series = series;

    let candles = [];
    let candleSource = "";
    try {
      const url = trade.ema_flip_research
        ? `/api/stoch/ema-flip-research-klines?signal_id=${encodeURIComponent(trade.signal_id || "")}`
        : trade.pool_research
        ? `/api/stoch/pool-research-klines?signal_id=${encodeURIComponent(trade.signal_id || "")}`
        : `/api/stoch/klines?symbol=${encodeURIComponent(symbol)}&interval=5&limit=300`;
      const res = await fetch(url, { credentials: "include" });
      const data = await res.json();
      candles = Array.isArray(data.candles) ? data.candles : [];
      candleSource = String(data.source || "");
      if ((trade.pool_research || trade.ema_flip_research) && candleSource.indexOf("bybit") >= 0) {
        candles = [];
        candleSource = "rejected_bybit";
      }
    } catch (err) {
      console.warn("Stoch klines fetch failed", err);
    }

    const candleData = candles.map((c) => ({
      time: Number(c.time),
      open: Number(c.open),
      high: Number(c.high),
      low: Number(c.low),
      close: Number(c.close),
    }));

    if (!candleData.length) {
      if (this.subEl) this.subEl.textContent += " · Keine Kerzen verfügbar";
    } else {
      series.setData(candleData);
      chart.timeScale().fitContent();
    }

    const addLine = (price, color, title, dashed) => {
      const p = asNum(price);
      if (p === null || !series) return null;
      const line = series.createPriceLine({
        price: p,
        color,
        lineWidth: 2,
        lineStyle: dashed ? LightweightCharts.LineStyle.Dashed : LightweightCharts.LineStyle.Solid,
        axisLabelVisible: true,
        title,
      });
      this.priceLines.push(line);
      return p;
    };

    const entry = asNum(trade.open_price) ?? asNum(trade.expected_open_price) ?? asNum(trade.entry_price);
    const tp = trade.pool_research ? null : asNum(trade.expected_tp);
    const sl = asNum(trade.expected_sl) ?? asNum(trade.sl_price);
    const closePx = asNum(trade.close_price);
    const tp1 = asNum(trade.tp1_price);
    const tp2 = asNum(trade.tp2_price);

    addLine(entry, "#3b82f6", entry !== null ? `Entry ${fmt(entry, 6)}` : "Entry", false);
    if (trade.ema_flip_research) {
      addLine(trade.ema9, "#f59e0b", "EMA9", false);
      addLine(trade.ema20, "#a855f7", "EMA20", false);
      addLine(sl, "#ef4444", "SL", true);
      const addZone = (cluster, color, title) => {
        if (!cluster) return;
        addLine(cluster.bottom, color, title + " lo", true);
        addLine(cluster.top, color, title + " hi", true);
      };
      (trade.active_upper_pools || []).forEach((c, i) => addZone(c, "rgba(34,197,94,0.35)", "up" + i));
      (trade.active_lower_pools || []).forEach((c, i) => addZone(c, "rgba(59,130,246,0.35)", "dn" + i));
      addZone(trade.protection_pool || trade.sl_cluster, "rgba(239,68,68,0.45)", "protect");
      (trade.ratchet_steps || []).forEach((st, i) => addLine(st.sl_price, "#fb7185", "R" + (i + 1), true));
      const tag = trade.flipped || trade.decision === "FLIPPED" ? "FLIPPED" : trade.decision === "ALIGNED" ? "ALIGNED" : trade.decision;
      this.subEl.textContent +=
        ` · ${tag}` +
        ` · orig ${trade.original_direction || "–"} → ${trade.executed_direction || "–"}` +
        ` · EMA trend ${trade.ema_trend || "–"}` +
        ` · last ${trade.last_confirmed_cross || "–"}` +
        ` · Gross ${fmt(trade.gross_pnl_pct, 2)} Net ${fmt(trade.net_pnl_pct, 2)}` +
        (trade.exit_reason ? ` · Exit ${trade.exit_reason}` : "");
    } else if (trade.pool_research) {
      if (tp1 !== null) {
        const sz = trade.tp1_size != null ? ` ${Math.round(Number(trade.tp1_size) * 100)}%` : "";
        addLine(tp1, "#22c55e", `TP1${sz}`, true);
      }
      if (tp2 !== null) {
        const sz = trade.tp2_size != null ? ` ${Math.round(Number(trade.tp2_size) * 100)}%` : "";
        addLine(tp2, "#16a34a", `TP2${sz}`, true);
      }
      addLine(sl, "#ef4444", "SL", true);
      const addZone = (cluster, color, title) => {
        if (!cluster) return;
        addLine(cluster.bottom, color, title + " lo", true);
        addLine(cluster.top, color, title + " hi", true);
      };
      addZone(trade.sl_cluster, "rgba(239,68,68,0.35)", "SL zone");
      addZone(trade.tp1_cluster, "rgba(34,197,94,0.35)", "TP1 zone");
      addZone(trade.tp2_cluster, "rgba(22,163,74,0.35)", "TP2 zone");
      if (trade.pool_research) {
        const slCl = trade.sl_cluster || {};
        const tp1Cl = trade.tp1_cluster || {};
        const tp2Cl = trade.tp2_cluster || {};
        const wide = trade.sl_too_wide ? " · SL TOO WIDE (>1.5%)" : "";
        this.subEl.textContent +=
          ` · Signal-TF ${trade.signal_timeframe || "–"} · Pool-TF 5m` +
          ` · Snapshot ${trade.snapshot_as_of ? fmtUtc(trade.snapshot_as_of) : "–"}` +
          ` · 5m ${fmtUtc(trade.last_5m_open)}–${fmtUtc(trade.last_5m_close)}` +
          ` · Pools@entry ${trade.entry_pool_count ?? "–"}` +
          ` · SL cluster #${slCl.cluster_id ?? "–"} ${slCl.side || ""} [${fmt(slCl.bottom, 6)}–${fmt(slCl.top, 6)}]` +
          ` dist ${fmt(trade.sl_distance_pct, 2)}%${wide}` +
          ` · TP1 cluster #${tp1Cl.cluster_id ?? "–"}` +
          (trade.tp2_price != null ? ` · TP2 cluster #${tp2Cl.cluster_id ?? "–"}` : "") +
          (trade.exit_time ? ` · Closed ${fmtUtc(trade.exit_time)}` : " · OPEN/no close");
      }
    } else {
      addLine(tp, "#4caf50", "TP", true);
      addLine(sl, "#f44336", "SL", true);
    }
    if (closePx !== null) addLine(closePx, "#ffd700", "Close", false);

    // Candle-Marker am Entry (wie trade_dashboard Open Trade)
    const markers = [];
    const openMs = toUtcMs(trade.open_time) ?? toUtcMs(trade.expected_open_time);
    const openSec = openMs !== null ? Math.floor(openMs / 1000) : null;
    if (candleData.length && entry !== null) {
      let openCandle = null;
      if (openSec !== null) {
        openCandle = candleData.find((c) => c.time >= openSec) || null;
      }
      if (!openCandle) {
        // Fallback: Kerze deren Close dem Entry am nächsten ist
        openCandle = candleData.reduce((best, c) => {
          if (!best) return c;
          return Math.abs(c.close - entry) < Math.abs(best.close - entry) ? c : best;
        }, null);
      }
      if (openCandle) {
        const isLong = direction === "LONG";
        const ezm = !!trade.ezm_research;
        let color = trade.pool_research ? "#3b82f6" : "#808080";
        if (ezm) color = isLong ? "#22c55e" : "#ef4444";
        markers.push({
          time: openCandle.time,
          position: isLong ? "belowBar" : "aboveBar",
          color,
          shape: isLong ? "arrowUp" : "arrowDown",
          text: ezm
            ? `${isLong ? "LONG" : "SHORT"} ${fmt(entry, 6)}`
            : `Entry ${fmt(entry, 6)}`,
        });
      }
    }

    const closeMs = toUtcMs(trade.close_time);
    if (candleData.length && closePx !== null && closeMs !== null) {
      const closeSec = Math.floor(closeMs / 1000);
      const closeCandle = candleData.find((c) => c.time >= closeSec);
      if (closeCandle) {
        markers.push({
          time: closeCandle.time,
          position: "aboveBar",
          color: "#ffd700",
          shape: "circle",
          text: `Close ${fmt(closePx, 6)}`,
        });
      }
    }

    if (trade.pool_research && Array.isArray(trade.legs)) {
      trade.legs.forEach((leg) => {
        const kind = String(leg.kind || "").toUpperCase();
        const tMs = toUtcMs(leg.time);
        if (tMs === null || !candleData.length) return;
        const tSec = Math.floor(tMs / 1000);
        const candle = candleData.find((c) => c.time >= tSec) || candleData[candleData.length - 1];
        let color = "#e879f9";
        if (kind === "TP1" || kind === "TP2") color = "#22c55e";
        if (kind === "SL") color = "#ef4444";
        markers.push({
          time: candle.time,
          position: kind === "SL" ? "aboveBar" : "belowBar",
          color,
          shape: kind === "SL" ? "arrowDown" : "circle",
          text: kind,
        });
      });
    }

    if (markers.length && typeof LightweightCharts.createSeriesMarkers === "function") {
      try {
        LightweightCharts.createSeriesMarkers(series, markers);
      } catch (err) {
        console.warn("Stoch markers failed", err);
      }
    }

    if (candleData.length && entry !== null) {
      const last = candleData[candleData.length - 1].close;
      const levels = [entry, tp, sl, closePx].filter((v) => v !== null);
      const lo = Math.min(...candleData.map((c) => c.low));
      const hi = Math.max(...candleData.map((c) => c.high));
      const span = Math.max(hi - lo, Math.abs(last) * 0.01, 1e-9);
      const far = levels.some((p) => p < lo - 2 * span || p > hi + 2 * span);
      if (far && this.subEl) {
        this.subEl.textContent +=
          " · Hinweis: Entry/TP/SL liegen weit vom aktuellen Kurs (Achsen-Labels rechts)";
      }
    }

    if (this.subEl) {
      const openLabel = openMs !== null ? ` · Open ${fmtUtc(openMs)}` : "";
      if (trade.pool_research) {
        this.subEl.textContent +=
          openLabel +
          ` · Entry ${fmt(entry, 6)} · TP1 ${fmt(tp1, 6)} · TP2 ${fmt(tp2, 6)} · SL ${fmt(sl, 6)}`;
      } else {
        this.subEl.textContent +=
          openLabel +
          ` · Entry ${fmt(entry, 6)} · TP ${fmt(tp, 6)} · SL ${fmt(sl, 6)}` +
          (closePx !== null ? ` · Close ${fmt(closePx, 6)}` : "");
      }
    }

    requestAnimationFrame(() => {
      try {
        chart.applyOptions({
          width: this.hostEl.clientWidth,
          height: this.hostEl.clientHeight,
        });
        if (candleData.length) chart.timeScale().fitContent();
      } catch (_) {}
    });
  };

  global.StochChartModal = ChartModal;
})(window);
