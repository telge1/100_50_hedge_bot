/**
 * Pure Market-Profile LLD overlay helpers (Node + browser).
 * Normalization + missing-endpoint fallback contract. No network/DOM/CH.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  if (root) {
    root.MpLldOverlayHelpers = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  var SLIM_PATH = "/api/research/liquidity-location";
  var PANE_PATH = "/api/research/pane";
  var CONTRACT_VERSION = "lld_overlay_v1";

  function overlayNamespace(p) {
    if (!p) return "";
    if (p.namespace) return String(p.namespace);
    var id = String(p.id || "");
    if (id.indexOf("lld:") === 0 || id.indexOf("lldc:") === 0) return "LLD";
    var meta = p.metadata || {};
    if (meta.source === "drawing" || meta.drawing_id) {
      if (p.type === "position" || meta.drawing_type === "long_position" || meta.drawing_type === "short_position") {
        return "POSITION";
      }
      return "USER_DRAWING";
    }
    return "SYSTEM";
  }

  function isLldOverlay(p) {
    return overlayNamespace(p) === "LLD";
  }

  /**
   * Reference extraction used by applyLldPaneBundle (pane or slim).
   */
  function extractConsumedLldState(body) {
    var next = {};
    var orderedIds = [];
    var list = (body && body.overlays) || [];
    for (var i = 0; i < list.length; i += 1) {
      var p = list[i];
      if (p && p.id && isLldOverlay(p)) {
        var id = String(p.id);
        if (!Object.prototype.hasOwnProperty.call(next, id)) orderedIds.push(id);
        next[id] = p;
      }
    }
    var liquidity = (body && body.liquidity) || null;
    var lldEma = (body && body.lld_ema) || (liquidity && liquidity.ema) || null;
    if (!lldEma) {
      lldEma = { fast: [], slow: [], fast_visible: false, slow_visible: false };
    }
    var clusters = (body && body.clusters) || (liquidity && liquidity.clusters);
    return {
      orderedIds: orderedIds,
      lldPayloads: next,
      lldEma: lldEma,
      clusters: clusters,
      liquidityLocationMode: body && body.liquidity_location_mode,
      liquidityLocationAsOf: body && body.liquidity_location_as_of,
      canonicalSnapshotSha256: body && body.canonical_snapshot_sha256,
      symbol: body && body.symbol,
      timeframe: body && body.timeframe,
      from: body && body.from,
      to: body && body.to,
      contractVersion: body && body.contract_version
    };
  }

  /** Endpoint missing → allow one pane fallback. Auth/logic errors → no fallback. */
  function isMissingEndpointStatus(status) {
    return status === 404 || status === 405;
  }

  function shouldFallbackToPane(status) {
    return isMissingEndpointStatus(status);
  }

  /**
   * Load slim LLD first; on proven missing endpoint, fall back once to pane.
   * opts.fetchJson(url, method, body) → Promise<{ok,status,payload}>
   * opts.isAborted() / opts.isStale() / opts.isToggleOff() gate application.
   */
  function loadLldWithFallback(opts) {
    opts = opts || {};
    var fetchJson = opts.fetchJson;
    var requestBody = opts.requestBody || {};
    var paneBody = opts.paneBody || requestBody;
    var calls = [];

    function record(url) {
      calls.push(url);
    }

    function gateOrNull(result) {
      if (opts.isAborted && opts.isAborted()) return { applied: false, reason: "abort", calls: calls, source: null, body: null };
      if (opts.isStale && opts.isStale()) return { applied: false, reason: "stale", calls: calls, source: null, body: null };
      if (opts.isToggleOff && opts.isToggleOff()) return { applied: false, reason: "toggle_off", calls: calls, source: null, body: null };
      return null;
    }

    record(SLIM_PATH);
    return Promise.resolve(fetchJson(SLIM_PATH, "POST", requestBody)).then(function (slimRes) {
      var blocked = gateOrNull();
      if (blocked) return blocked;
      if (slimRes && slimRes.ok) {
        return {
          applied: true,
          reason: "slim",
          calls: calls,
          source: "slim",
          body: slimRes.payload
        };
      }
      var status = slimRes ? slimRes.status : 0;
      if (!shouldFallbackToPane(status)) {
        var err = new Error(
          (slimRes && slimRes.payload && (slimRes.payload.message || slimRes.payload.error)) ||
            ("lld_overlay_http_" + status)
        );
        err.status = status;
        err.calls = calls;
        throw err;
      }
      record(PANE_PATH);
      return Promise.resolve(fetchJson(PANE_PATH, "POST", paneBody)).then(function (paneRes) {
        var blocked2 = gateOrNull();
        if (blocked2) return blocked2;
        if (!paneRes || !paneRes.ok) {
          var err2 = new Error(
            (paneRes && paneRes.payload && (paneRes.payload.message || paneRes.payload.error)) ||
              ("pane_fallback_http_" + (paneRes && paneRes.status))
          );
          err2.status = paneRes && paneRes.status;
          err2.calls = calls;
          throw err2;
        }
        return {
          applied: true,
          reason: "pane_fallback",
          calls: calls,
          source: "pane_fallback",
          body: paneRes.payload
        };
      });
    });
  }

  return {
    SLIM_PATH: SLIM_PATH,
    PANE_PATH: PANE_PATH,
    CONTRACT_VERSION: CONTRACT_VERSION,
    overlayNamespace: overlayNamespace,
    isLldOverlay: isLldOverlay,
    extractConsumedLldState: extractConsumedLldState,
    isMissingEndpointStatus: isMissingEndpointStatus,
    shouldFallbackToPane: shouldFallbackToPane,
    loadLldWithFallback: loadLldWithFallback
  };
});
