/**
 * DOGE-LIVE-P7 — Candle / forming-bar live trace (default OFF).
 * Enable: localStorage DOGE_CANDLE_LIVE_TRACE=1
 */
(function () {
  "use strict";

  var LS_KEY = "DOGE_CANDLE_LIVE_TRACE";
  var BOOT_LS_KEY = "RESEARCH_BOOT_TRACE";

  function enabled() {
    try {
      if (typeof window !== "undefined" && window.DOGE_CANDLE_LIVE_TRACE === 1) return true;
      if (localStorage.getItem(BOOT_LS_KEY) === "1") return true;
      return localStorage.getItem(LS_KEY) === "1";
    } catch (e) {
      return false;
    }
  }

  function isoFromUnix(unix) {
    var u = Math.floor(Number(unix));
    if (!Number.isFinite(u)) return null;
    try {
      return new Date(u * 1000).toISOString().replace(".000", "");
    } catch (e2) {
      return null;
    }
  }

  function event(stage, fields) {
    if (!enabled()) return;
    var f = fields || {};
    var bits = ["CANDLE_TRACE", "stage=" + stage];
    Object.keys(f).forEach(function (k) {
      var v = f[k];
      if (v === undefined) return;
      bits.push(k + "=" + (v === null ? "null" : String(v)));
    });
    try {
      console.log(bits.join(" | "));
    } catch (e) { /* ignore */ }
  }

  window.DogeCandleLiveTrace = {
    enabled: enabled,
    event: event,
    isoFromUnix: isoFromUnix,
    LS_KEY: LS_KEY,
  };
})();
