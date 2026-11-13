/**
 * DOGE-LIVE-P5I — Exact Origin E2E trace (default OFF).
 * Enable: localStorage DOGE_EXACT_ORIGIN_TRACE=1 or window.DOGE_EXACT_ORIGIN_TRACE=1
 * Disable: localStorage.removeItem('DOGE_EXACT_ORIGIN_TRACE')
 */
(function () {
  "use strict";

  var LS_KEY = "DOGE_EXACT_ORIGIN_TRACE";
  var seq = 0;

  function enabled() {
    try {
      if (typeof window !== "undefined" && window.DOGE_EXACT_ORIGIN_TRACE === 1) return true;
      return localStorage.getItem(LS_KEY) === "1";
    } catch (e) {
      return false;
    }
  }

  function isoFromUnix(unix) {
    var u = Math.floor(Number(unix));
    if (!Number.isFinite(u)) return "?";
    try {
      return new Date(u * 1000).toISOString().replace(".000", "");
    } catch (e2) {
      return String(u);
    }
  }

  function isoFromMs(ms) {
    var m = Number(ms);
    if (!Number.isFinite(m)) return "?";
    try {
      return new Date(m).toISOString().replace(".000", "");
    } catch (e) {
      return String(m);
    }
  }

  function shortIso(iso) {
    if (!iso || iso === "?") return "?";
    return String(iso).slice(0, 10);
  }

  function newTraceId() {
    seq += 1;
    return "eo" + Date.now().toString(36) + "-" + seq.toString(36);
  }

  function compactObj(obj) {
    if (!obj) return "";
    try {
      return JSON.stringify(obj);
    } catch (e) {
      return String(obj);
    }
  }

  function logLine(parts) {
    if (!enabled()) return;
    console.log("TRACE | " + parts);
  }

  function event(traceId, stage, fields) {
    if (!enabled()) return;
    var tid = traceId ? "id=" + traceId + " | " : "";
    var f = fields || {};
    var bits = [tid + "stage=" + stage];
    Object.keys(f).forEach(function (k) {
      var v = f[k];
      if (v === undefined) return;
      if (v === null) bits.push(k + "=null");
      else if (typeof v === "object") bits.push(k + "=" + compactObj(v));
      else bits.push(k + "=" + String(v));
    });
    logLine(bits.join(" | "));
  }

  function activationHelp() {
    if (!enabled()) return;
    logLine(
      "trace=ON | P5K: cases A=30m/5m B=30m/30m C=5m/5m | filter console TRACE | disable: localStorage.removeItem('" +
        LS_KEY +
        "')"
    );
  }

  /** P5K: keep chart/pool cases disjoint in logs. */
  function p5kCaseLabel(chartTf, poolTf) {
    var c = String(chartTf || "?");
    var p = String(poolTf || "?");
    if (c === "5m" && p === "5m") return "C_REF_5m5m";
    if (c === "30m" && p === "5m") return "A_30m_chart_5m_pool";
    if (c === "30m" && p === "30m") return "B_30m_chart_30m_pool";
    return "OTHER_" + c + "_" + p;
  }

  function isAprSepStaticBound(iso) {
    if (!iso) return false;
    var s = String(iso);
    return s.indexOf("2026-04-01") === 0 || s.indexOf("2026-09-30") === 0;
  }

  window.DogeExactOriginTrace = {
    LS_KEY: LS_KEY,
    enabled: enabled,
    newTraceId: newTraceId,
    event: event,
    logLine: logLine,
    isoFromUnix: isoFromUnix,
    isoFromMs: isoFromMs,
    shortIso: shortIso,
    activationHelp: activationHelp,
    p5kCaseLabel: p5kCaseLabel,
    isAprSepStaticBound: isAprSepStaticBound,
  };
})();
