/**
 * Authoritative AVR context for Wall Decision V1.
 *
 * Reuses the existing footprint AVR-V1 API (/api/footprint-candles → candle.avr)
 * without depending on the visual Footprint layer or the chart candle timeframe.
 *
 * Native AVR resolution is fixed by the engine: BTCUSDT / 5m / DISPLAY.
 */
(function (global) {
  "use strict";

  var SOURCE = "footprint_avr_v1";
  var SYMBOL = "BTCUSDT";
  var TIMEFRAME = "5m";
  var MODE = "DISPLAY";
  var BUCKET_STEP = "5";
  var LOOKBACK_S = 20 * 60; /* ~4 closed 5m candles + forming */
  var POLL_MS = 5000;

  var ctx = global.__mpWallDecisionContext || (global.__mpWallDecisionContext = {});
  if (!ctx.avr) {
    ctx.avr = {
      symbol: null,
      state: null,
      event_time_ns: null,
      available_at_ns: null,
      age_ms: null,
      buy_notional: null,
      sell_notional: null,
      imbalance: null,
      response_bps: null,
      efficiency_bps_per_million: null,
      up_velocity_bps_per_s: null,
      down_velocity_bps_per_s: null,
      proxy: false,
      source: SOURCE,
      available: false,
      reason: "not_started"
    };
  }

  var timer = null;
  var inflight = null;
  var lastFetchAtMs = 0;

  function nowMs() {
    return Date.now();
  }

  function toNsFromUnixSec(sec) {
    var n = Number(sec);
    if (!Number.isFinite(n)) return null;
    return Math.round(n * 1e9);
  }

  function toNsFromMs(ms) {
    var n = Number(ms);
    if (!Number.isFinite(n)) return null;
    return Math.round(n * 1e6);
  }

  function pickState(avr) {
    if (!avr || typeof avr !== "object") return null;
    var name = avr.final_state || avr.dominant_state || null;
    return name != null && String(name) !== "" ? String(name) : null;
  }

  function evidenceFrom(avr) {
    if (!avr) return {};
    var ev = avr.evidence;
    if (Array.isArray(ev) && ev.length) return ev[0] || {};
    if (ev && typeof ev === "object") return ev;
    var panel = avr.panel || {};
    return {
      available_at: panel.available_at,
      price_progress_bps: panel.price_progress_bps,
      impact_efficiency_bps_per_million_sell: null,
      impact_efficiency_bps_per_million_buy: null
    };
  }

  function buildUnavailable(reason, symbol) {
    return {
      symbol: symbol || null,
      state: null,
      event_time_ns: null,
      available_at_ns: null,
      age_ms: null,
      buy_notional: null,
      sell_notional: null,
      imbalance: null,
      response_bps: null,
      efficiency_bps_per_million: null,
      up_velocity_bps_per_s: null,
      down_velocity_bps_per_s: null,
      proxy: false,
      source: SOURCE,
      available: false,
      reason: reason || "DATA_UNAVAILABLE"
    };
  }

  /**
   * Map an existing candle.avr payload (server AVR-V1) into the WD context contract.
   * Does not recompute AVR.
   */
  function buildFromCandle(candle, symbol) {
    var avr = candle && candle.avr;
    var stateName = pickState(avr);
    if (!stateName) return buildUnavailable("missing_avr_state", symbol || SYMBOL);

    var ev = evidenceFrom(avr);
    var candleStart = Number(candle.time != null ? candle.time : candle.open_time);
    var availableAtSec =
      ev.available_at != null
        ? Number(ev.available_at)
        : Number.isFinite(candleStart)
          ? candleStart + 300
          : null;
    var recvMs = nowMs();
    var buy = avr.total_buy_notional != null ? Number(avr.total_buy_notional) : null;
    var sell = avr.total_sell_notional != null ? Number(avr.total_sell_notional) : null;
    var total = (buy || 0) + (sell || 0);
    var imbalance =
      total > 0 && buy != null && sell != null ? (buy - sell) / total : null;
    var tradeMove =
      avr.trade_price && avr.trade_price.move_bps != null
        ? Number(avr.trade_price.move_bps)
        : null;
    var responseBps =
      ev.price_progress_bps != null ? Number(ev.price_progress_bps) : tradeMove;
    var effSell = ev.impact_efficiency_bps_per_million_sell;
    var effBuy = ev.impact_efficiency_bps_per_million_buy;
    var eff =
      effSell != null || effBuy != null
        ? Number(effSell != null ? effSell : effBuy)
        : null;
    var upVel = ev.directional_velocity_bps_s_up;
    var downVel = ev.directional_velocity_bps_s_down;
    if (upVel == null && ev.price_velocity_bps_per_second != null) {
      var v = Number(ev.price_velocity_bps_per_second);
      upVel = v > 0 ? v : 0;
      downVel = v < 0 ? -v : 0;
    }
    var proxy = /VACUUM/i.test(stateName) || !!avr.proxy;

    var availableAtNs = toNsFromUnixSec(availableAtSec);
    var ageMs =
      availableAtSec != null && Number.isFinite(availableAtSec)
        ? Math.max(0, recvMs - availableAtSec * 1000)
        : null;

    return {
      symbol: String(symbol || SYMBOL).toUpperCase(),
      state: stateName,
      event_time_ns: toNsFromUnixSec(candleStart),
      available_at_ns: availableAtNs != null ? availableAtNs : toNsFromMs(recvMs),
      age_ms: ageMs,
      buy_notional: buy,
      sell_notional: sell,
      imbalance: imbalance,
      response_bps: responseBps,
      efficiency_bps_per_million: eff,
      up_velocity_bps_per_s: upVel != null ? Number(upVel) : null,
      down_velocity_bps_per_s: downVel != null ? Number(downVel) : null,
      proxy: proxy,
      source: SOURCE,
      available: true,
      reason: null,
      timeframe: TIMEFRAME,
      provisional: !!avr.provisional,
      dominant_state: avr.dominant_state || null,
      final_state: avr.final_state || null,
      coverage_status: avr.coverage_status || null,
      verification: avr.verification || null
    };
  }

  function publish(avrObj) {
    ctx.avr = avrObj;
    try {
      global.dispatchEvent(
        new CustomEvent("mp-wall-decision-avr", { detail: { avr: avrObj } })
      );
    } catch (e) {
      /* ignore */
    }
    return avrObj;
  }

  function latestCandleWithAvr(candles) {
    if (!candles || !candles.length) return null;
    for (var i = candles.length - 1; i >= 0; i -= 1) {
      if (candles[i] && candles[i].avr && pickState(candles[i].avr)) return candles[i];
    }
    return null;
  }

  /** Prefer live Footprint store when it already holds AVR for BTCUSDT/5m. */
  function tryPublishFromFootprintStore() {
    try {
      var FC = global.FootprintCandles;
      var st = FC && FC._state;
      if (!st) return false;
      var candles =
        (st.payload && st.payload.candles) ||
        st.historyCandles ||
        null;
      var candle = latestCandleWithAvr(candles);
      if (!candle) return false;
      var sym = String(st.symbol || SYMBOL).toUpperCase();
      if (sym !== SYMBOL) return false;
      publish(buildFromCandle(candle, sym));
      return true;
    } catch (e) {
      return false;
    }
  }

  function fetchSnapshot() {
    if (inflight) return inflight;
    var end = Math.floor(nowMs() / 1000);
    var start = end - LOOKBACK_S;
    var qs = new URLSearchParams({
      symbol: SYMBOL,
      timeframe: TIMEFRAME,
      mode: MODE,
      bucket_step: BUCKET_STEP,
      from: String(start),
      to: String(end)
    });
    lastFetchAtMs = nowMs();
    inflight = fetch("/api/footprint-candles?" + qs.toString(), {
      credentials: "same-origin"
    })
      .then(function (res) {
        return res.json().then(function (body) {
          return { ok: res.ok, body: body };
        });
      })
      .then(function (out) {
        if (!out.ok || !out.body || out.body.success !== true) {
          publish(
            buildUnavailable(
              (out.body && (out.body.error || out.body.message)) || "avr_http_failed",
              SYMBOL
            )
          );
          return ctx.avr;
        }
        var candle = latestCandleWithAvr(out.body.candles || []);
        if (!candle) {
          publish(buildUnavailable("no_avr_candle", SYMBOL));
          return ctx.avr;
        }
        publish(buildFromCandle(candle, out.body.symbol || SYMBOL));
        return ctx.avr;
      })
      .catch(function () {
        publish(buildUnavailable("avr_network_error", SYMBOL));
        return ctx.avr;
      })
      .then(function (v) {
        inflight = null;
        return v;
      });
    return inflight;
  }

  function refresh() {
    if (tryPublishFromFootprintStore()) {
      /* Still refresh via API periodically so chart TF / disabled footprint
         cannot starve Wall Decision. Prefer store if fresher than 15s. */
      var age = ctx.avr && ctx.avr.age_ms;
      if (age != null && age < 15000 && nowMs() - lastFetchAtMs < 15000) {
        return Promise.resolve(ctx.avr);
      }
    }
    return fetchSnapshot();
  }

  function start(opts) {
    opts = opts || {};
    var interval = opts.intervalMs != null ? Number(opts.intervalMs) : POLL_MS;
    stop();
    refresh();
    timer = setInterval(function () {
      refresh();
    }, Math.max(2000, interval || POLL_MS));
  }

  function stop() {
    if (timer) {
      clearInterval(timer);
      timer = null;
    }
  }

  function getAvr() {
    return ctx.avr;
  }

  function readStateName() {
    var a = ctx.avr;
    if (a && a.available && a.state) return { value: a.state, status: "ok", avr: a };
    return {
      value: null,
      status: "DATA_UNAVAILABLE",
      reason: (a && a.reason) || "DATA_UNAVAILABLE",
      avr: a || null
    };
  }

  global.MpWallDecisionAvrContext = {
    SOURCE: SOURCE,
    SYMBOL: SYMBOL,
    TIMEFRAME: TIMEFRAME,
    buildFromCandle: buildFromCandle,
    buildUnavailable: buildUnavailable,
    publish: publish,
    publishFromCandle: function (candle, symbol) {
      return publish(buildFromCandle(candle, symbol));
    },
    refresh: refresh,
    fetchSnapshot: fetchSnapshot,
    tryPublishFromFootprintStore: tryPublishFromFootprintStore,
    start: start,
    stop: stop,
    getAvr: getAvr,
    readStateName: readStateName,
    _ctx: ctx
  };
})(typeof window !== "undefined" ? window : globalThis);
