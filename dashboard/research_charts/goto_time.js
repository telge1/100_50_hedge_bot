/**
 * Pure GO-TO / as-of time helpers for Research Charts.
 * Same UTC instant drives liquidity_location_as_of, candle window, and chart focus.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.ResearchGotoTime = factory();
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  /** Visible/load half-window around GO TO target (seconds). */
  var GOTO_VIEW_HALF_SEC = 4 * 3600;

  function pad2(n) {
    return String(n).padStart(2, "0");
  }

  /**
   * Parse a GO-TO input as UTC unix seconds.
   * Accepts:
   * - 2026-08-26T11:34:51 / 2026-08-26T11:34:51Z / 2026-08-26 11:34:51
   * - datetime-local minute form 2026-08-26T11:34 (seconds default 00)
   * - 26.08.2026, 11:34:51 / 26.08.2026 11:34:51
   * Never interprets bare datetime as browser-local.
   */
  function parseGotoUtcToUnix(raw) {
    if (raw == null) return null;
    var s = String(raw).trim();
    if (!s) return null;

    var de = s.match(
      /^(\d{1,2})\.(\d{1,2})\.(\d{4})(?:[,\s]+|T)(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(?:UTC|Z)?$/i
    );
    if (de) {
      s =
        de[3] +
        "-" +
        pad2(de[2]) +
        "-" +
        pad2(de[1]) +
        "T" +
        pad2(de[4]) +
        ":" +
        de[5] +
        ":" +
        pad2(de[6] != null ? de[6] : "0") +
        "Z";
    } else {
      s = s.replace(/\s+UTC$/i, "").replace(/\s+Z$/i, "Z");
      s = s.replace(" ", "T");
      if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(s)) s += ":00";
      if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/.test(s)) s += "Z";
      if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+$/.test(s)) s += "Z";
    }

    if (!/[zZ]|[+-]\d{2}:?\d{2}$/.test(s)) return null;
    var ms = Date.parse(s);
    if (!Number.isFinite(ms)) return null;
    return Math.floor(ms / 1000);
  }

  /** ISO-8601 UTC with seconds, no millis: 2026-08-26T11:34:51Z */
  function unixToIsoZ(unix) {
    var n = Number(unix);
    if (!Number.isFinite(n)) return null;
    return new Date(n * 1000).toISOString().replace(/\.\d{3}Z$/, "Z");
  }

  function fmtUtcSeconds(unix) {
    if (unix == null || !Number.isFinite(Number(unix))) return "—";
    var d = new Date(Number(unix) * 1000);
    return (
      d.getUTCFullYear() +
      "-" +
      pad2(d.getUTCMonth() + 1) +
      "-" +
      pad2(d.getUTCDate()) +
      " " +
      pad2(d.getUTCHours()) +
      ":" +
      pad2(d.getUTCMinutes()) +
      ":" +
      pad2(d.getUTCSeconds()) +
      " UTC"
    );
  }

  /**
   * Deterministic candle load + visible pad around goto_ts_utc.
   * LLD as-of stays exactly at goto_ts; candles may extend past as_of for orientation.
   */
  function gotoLoadWindow(gotoTs, halfSec) {
    var ts = Math.floor(Number(gotoTs));
    var half = halfSec != null ? Math.floor(Number(halfSec)) : GOTO_VIEW_HALF_SEC;
    if (!Number.isFinite(ts) || !Number.isFinite(half) || half <= 0) return null;
    return {
      goto_ts_utc: ts,
      from: ts - half,
      to: ts + half,
      viewPad: half,
      as_of_iso: unixToIsoZ(ts),
    };
  }

  function visibleRangeContains(from, to, target) {
    var a = Number(from);
    var b = Number(to);
    var t = Number(target);
    if (![a, b, t].every(Number.isFinite)) return false;
    return a <= t && t <= b;
  }

  return {
    GOTO_VIEW_HALF_SEC: GOTO_VIEW_HALF_SEC,
    parseGotoUtcToUnix: parseGotoUtcToUnix,
    unixToIsoZ: unixToIsoZ,
    fmtUtcSeconds: fmtUtcSeconds,
    gotoLoadWindow: gotoLoadWindow,
    visibleRangeContains: visibleRangeContains,
  };
});
