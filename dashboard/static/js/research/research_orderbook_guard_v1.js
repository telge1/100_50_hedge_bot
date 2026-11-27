/**
 * Orderbook request guard. Middleware 429 is not collector capacity.
 * A rate-limit miss keeps the last painted book.
 */
(function (root) {
  "use strict";

  var CLEAR_STATES = {
    NOT_PILOT: 1,
    OFFLINE: 1,
    DISABLED: 1,
    CAPACITY: 1,
  };

  function classifyOb1000Error(err) {
    if (!err) return null;
    var msg = String(err.message || err);
    var api = err.apiError != null ? String(err.apiError) : "";
    var blob = msg + " " + api;
    if (
      blob.indexOf("symbol_not_in_pilot") >= 0 ||
      blob.indexOf("invalid_symbol_syntax") >= 0 ||
      blob.indexOf("invalid_symbol") >= 0
    ) {
      return { code: "symbol_not_in_pilot", status: 400 };
    }
    if (blob.indexOf("unknown_lease") >= 0 || blob.indexOf("no_active_lease") >= 0) {
      return { code: "unknown_lease", status: 409 };
    }
    if (blob.indexOf("collector_unavailable") >= 0 || blob.indexOf("503") >= 0) {
      return { code: "collector_unavailable", status: 503 };
    }
    if (blob.indexOf("disabled") >= 0) return { code: "disabled", status: 503 };
    if (blob.indexOf("capacity_reached") >= 0) {
      return { code: "capacity_reached", status: 429 };
    }
    if (blob.indexOf("Too many concurrent requests") >= 0 || blob.indexOf("429") >= 0) {
      return { code: "rate_limited", status: 429 };
    }
    return { code: "error", message: msg };
  }

  function clearsOrderbook(uiState) {
    return !!CLEAR_STATES[String(uiState || "")];
  }

  function staggerOffsetMs(index, count, intervalMs) {
    var n = Math.max(1, count | 0);
    var i = Math.max(0, index | 0);
    var interval = intervalMs > 0 ? intervalMs : 2000;
    if (n <= 1) return 0;
    return Math.round((i * interval) / n);
  }

  function viewportSkipsOrderbookFetch(snap, now, intervalMs) {
    if (!snap) return false;
    if (snap.inflight || snap.timerPending) return true;
    if (snap.lastOkAt != null && (now - snap.lastOkAt) < intervalMs) return true;
    return false;
  }

  root.ResearchOrderbookGuard = {
    classifyOb1000Error: classifyOb1000Error,
    clearsOrderbook: clearsOrderbook,
    staggerOffsetMs: staggerOffsetMs,
    viewportSkipsOrderbookFetch: viewportSkipsOrderbookFetch,
  };
})(typeof window !== "undefined" ? window : globalThis);
