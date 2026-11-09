/**
 * Pure helpers for Research Scanner Pools (COMMON) — pane pick, overlay zones, merge.
 * Node + browser (ResearchScannerCommonHelpers).
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  if (root) {
    root.ResearchScannerCommonHelpers = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  var SCANNER_COMMON_NS = "SCANNER_COMMON";
  var SCANNER_COMMON_TIMELINE_NS = "SCANNER_COMMON_TIMELINE";
  var CAUSAL_LLD_LANDSCAPE_NS = "CAUSAL_LLD_LANDSCAPE";
  var LIVE_MICRO_LLD_V2_NS = "LIVE_MICRO_LLD_V2";
  var LIVE_MICRO_LLD_V2_TFS = { "1m": true, "5m": true, "15m": true, "30m": true, "1h": true, "4h": true };

  function isScannerCommonOverlay(p) {
    return !!(p && p.namespace === SCANNER_COMMON_NS);
  }

  function isScannerCommonTimelineOverlay(p) {
    return !!(p && p.namespace === SCANNER_COMMON_TIMELINE_NS);
  }

  function isCausalLldLandscapeOverlay(p) {
    return !!(p && p.namespace === CAUSAL_LLD_LANDSCAPE_NS);
  }

  function isLiveMicroLldV2Overlay(p) {
    return !!(p && p.namespace === LIVE_MICRO_LLD_V2_NS);
  }

  function isScannerCommonFamilyOverlay(p) {
    return isScannerCommonOverlay(p) || isScannerCommonTimelineOverlay(p);
  }

  function isResearchPoolLayerOverlay(p) {
    return (
      isScannerCommonFamilyOverlay(p) ||
      isCausalLldLandscapeOverlay(p) ||
      isLiveMicroLldV2Overlay(p)
    );
  }

  function stripScannerCommonOverlays(list) {
    return (list || []).filter(function (p) {
      return p && !isScannerCommonFamilyOverlay(p);
    });
  }

  /**
   * Prefer visible loaded 15m pane with a closed bar; else first loaded 15m with closed bar.
   */
  function selectPaneForScannerCommon(paneIds, panes, visibleIdList, lastClosedBarTimeFn) {
    var visible = {};
    (visibleIdList || []).forEach(function (pid) {
      visible[pid] = true;
    });
    var fallback = null;
    for (var i = 0; i < (paneIds || []).length; i += 1) {
      var pid = paneIds[i];
      var p = panes && panes[pid];
      if (!p || p.tf !== "15m") continue;
      var candles = p.pendingData && p.pendingData.candles;
      if (!candles || !candles.length) continue;
      if (!lastClosedBarTimeFn || lastClosedBarTimeFn(p) == null) continue;
      if (visible[pid]) return p;
      if (!fallback) fallback = p;
    }
    return fallback;
  }

  function hexFillFromStyle(style, fallbackColor) {
    if (style && style.fillColor) return style.fillColor;
    var c = (style && style.color) || fallbackColor || "#888888";
    if (c.length === 7) return c + "33";
    return c;
  }

  /**
   * Map backend COMMON rectangle+points to research_trp zone overlays (renderZone).
   */
  function normalizeScannerCommonZone(ov) {
    if (!ov || ov.type !== "zone" || ov.start_timestamp == null) return ov;
    if (ov.bottom_price != null && ov.top_price != null) return ov;
    var lo = ov.lower_price != null ? ov.lower_price : ov.bottom_price;
    var hi = ov.upper_price != null ? ov.upper_price : ov.top_price;
    if (lo == null || hi == null) return ov;
    return Object.assign({}, ov, {
      bottom_price: Number(lo),
      top_price: Number(hi),
    });
  }

  function scannerCommonOverlayToZone(ov) {
    if (!ov) return null;
    if (ov.type === "zone" && ov.start_timestamp != null) {
      return normalizeScannerCommonZone(ov);
    }
    if (ov.type !== "rectangle" || !ov.points || ov.points.length < 2) return null;
    var p0 = ov.points[0];
    var p1 = ov.points[1];
    var t0 = Number(p0.time);
    var t1 = Number(p1.time);
    var pr0 = Number(p0.price);
    var pr1 = Number(p1.price);
    if (!Number.isFinite(t0) || !Number.isFinite(t1) || !Number.isFinite(pr0) || !Number.isFinite(pr1)) {
      return null;
    }
    var style = ov.style || {};
    var color = style.color || "#888888";
    return {
      id: ov.id,
      namespace: ov.namespace || SCANNER_COMMON_NS,
      type: "zone",
      start_timestamp: Math.min(t0, t1),
      end_timestamp: Math.max(t0, t1),
      top_price: Math.max(pr0, pr1),
      bottom_price: Math.min(pr0, pr1),
      style: {
        color: color,
        fillColor: hexFillFromStyle(style, color),
        width: style.lineWidth != null ? style.lineWidth : style.width,
      },
      metadata: Object.assign({}, ov.metadata || {}),
    };
  }

  function convertScannerCommonOverlaysForRender(overlays) {
    var out = [];
    (overlays || []).forEach(function (ov) {
      if (!ov) return;
      if (!isScannerCommonFamilyOverlay(ov)) {
        out.push(ov);
        return;
      }
      var zone = scannerCommonOverlayToZone(ov);
      if (zone) out.push(zone);
    });
    return out;
  }

  function mergeScannerCommonOverlays(base, scannerOverlays) {
    var converted = convertScannerCommonOverlaysForRender(scannerOverlays || []);
    return stripScannerCommonOverlays(base).concat(converted);
  }

  function stripCausalLldLandscapeOverlays(list) {
    return (list || []).filter(function (p) {
      return p && !isCausalLldLandscapeOverlay(p);
    });
  }

  function stripLiveMicroLldV2Overlays(list) {
    return (list || []).filter(function (p) {
      return p && !isLiveMicroLldV2Overlay(p);
    });
  }

  function mergePaneOverlaysPreservingScannerCommon(
    newPaneOverlays,
    previousPendingOverlays,
    keepCommon,
    keepCausal,
    keepLiveMicroV2
  ) {
    var base = newPaneOverlays || [];
    var keepC = !!keepCommon;
    var keepL = !!keepCausal;
    var keepV2 = !!keepLiveMicroV2;
    if (!keepC && !keepL && !keepV2) return base;
    var preserved = (previousPendingOverlays || []).filter(function (p) {
      return (
        (keepC && isScannerCommonFamilyOverlay(p)) ||
        (keepL && isCausalLldLandscapeOverlay(p)) ||
        (keepV2 && isLiveMicroLldV2Overlay(p))
      );
    });
    if (!preserved.length) return base;
    return stripScannerCommonOverlays(
      stripCausalLldLandscapeOverlays(stripLiveMicroLldV2Overlays(base))
    ).concat(preserved);
  }

  function applyCausalLldLandscapeToOverlays(
    pendingOverlays,
    zones,
    fromCloseUnix,
    toCloseUnix
  ) {
    var base = stripCausalLldLandscapeOverlays(pendingOverlays || []);
    var clipped = clipZonesForWindow(zones, fromCloseUnix, toCloseUnix);
    return base.concat(clipped);
  }

  function applyLiveMicroLldV2ToOverlays(
    pendingOverlays,
    zones,
    fromCloseUnix,
    toCloseUnix
  ) {
    var base = stripLiveMicroLldV2Overlays(pendingOverlays || []);
    var clipped = clipZonesForWindow(zones, fromCloseUnix, toCloseUnix);
    return base.concat(clipped);
  }

  function liveMicroLldV2CacheKey(symbol, timeframe, fromIso, toIso, poolDetail, filterKey) {
    return (
      String(symbol || "") +
      "\x1f" +
      String(timeframe || "") +
      "\x1f" +
      "live_micro_lld_v2\x1f" +
      String(fromIso || "") +
      "\x1f" +
      String(toIso || "") +
      "\x1f" +
      String(poolDetail != null ? poolDetail : 5) +
      "\x1f" +
      String(filterKey != null ? filterKey : "-")
    );
  }

  function resolvePaneCloseWindow(pane, tfSec) {
    var candles = pane && pane.pendingData && pane.pendingData.candles;
    if (!candles || !candles.length) return null;
    var sec = tfSec || 900;
    var firstOpen = Number(candles[0].time);
    var lastOpen = Number(candles[candles.length - 1].time);
    if (!Number.isFinite(firstOpen) || !Number.isFinite(lastOpen)) return null;
    return {
      from_close_unix: firstOpen + sec,
      to_close_unix: lastOpen + sec,
    };
  }

  function causalLldLandscapeCacheKey(symbol, timeframe, fromIso, toIso, configVersion) {
    return (
      String(symbol || "") +
      "\x1f" +
      String(timeframe || "") +
      "\x1f" +
      "causal_lld\x1f" +
      String(fromIso || "") +
      "\x1f" +
      String(toIso || "") +
      "\x1f" +
      String(configVersion != null ? configVersion : "-")
    );
  }

  function countRenderableScannerCommon(overlays) {
    var n = 0;
    (overlays || []).forEach(function (ov) {
      if (!isScannerCommonFamilyOverlay(ov)) return;
      if (ov.type === "zone" && ov.start_timestamp != null) n += 1;
      else if (ov.type === "rectangle" && ov.points && ov.points.length >= 2) n += 1;
    });
    return n;
  }

  /**
   * True when incremental poll advanced the last fully closed bar (15m open time).
   */
  function closedBarOpenAdvanced(prevClosedOpen, nextClosedOpen) {
    if (prevClosedOpen == null || nextClosedOpen == null) return false;
    return Number(prevClosedOpen) !== Number(nextClosedOpen);
  }

  /**
   * Whether pollPane should schedule a COMMON refresh (pure gate, no DOM).
   */
  function shouldScheduleScannerCommonPollRefresh(opts) {
    opts = opts || {};
    if (opts.isHistoricalReplay) return false;
    if (!opts.checkboxEnabled) return false;
    if (opts.paneTf !== "15m") return false;
    if (!opts.isCommonTargetPane) return false;
    if (!opts.hasLoadedCandles) return false;
    return closedBarOpenAdvanced(opts.prevClosedOpen, opts.nextClosedOpen);
  }

  /**
   * Coalesce duplicate poll schedules for the same closed-bar open.
   * bucket: { lastRefreshedClosedOpen, pendingClosedOpen, pendingPaneId }
   */
  function coalesceScannerCommonPollSchedule(bucket, paneId, closedOpen) {
    var b = bucket || {};
    var open = closedOpen == null ? null : Number(closedOpen);
    if (open == null || !paneId) {
      return { schedule: false, bucket: b, reason: "no_closed_open" };
    }
    if (b.lastRefreshedClosedOpen != null && Number(b.lastRefreshedClosedOpen) === open) {
      return { schedule: false, bucket: b, reason: "already_refreshed" };
    }
    if (
      b.pendingClosedOpen != null &&
      Number(b.pendingClosedOpen) === open &&
      b.pendingPaneId === paneId
    ) {
      return { schedule: false, bucket: b, reason: "already_pending" };
    }
    b.pendingPaneId = paneId;
    b.pendingClosedOpen = open;
    return { schedule: true, bucket: b, reason: "new_closed_bar" };
  }

  function markScannerCommonPollRefreshed(bucket, closedOpen) {
    var b = bucket || {};
    var open = closedOpen == null ? null : Number(closedOpen);
    b.lastRefreshedClosedOpen = open;
    b.pendingPaneId = null;
    b.pendingClosedOpen = null;
    return b;
  }

  /** Last fully closed bar open time on pane, as of asOfUnix (not wall-clock). */
  function lastClosedBarOpenOnPane(pane, tfSec, asOfUnix) {
    var step = tfSec || 900;
    var asOf = Number(asOfUnix);
    if (!Number.isFinite(asOf)) return null;
    var candles = (pane && pane.pendingData && pane.pendingData.candles) || [];
    for (var i = candles.length - 1; i >= 0; i--) {
      var t = Number(candles[i].time);
      if (t + step <= asOf) return t;
    }
    return null;
  }

  function resolveScannerCommonAsOfUnix(ctx) {
    ctx = ctx || {};
    var tfSec = ctx.tfSec || 900;
    var nowUnix = ctx.nowUnix != null ? Number(ctx.nowUnix) : null;
    if (nowUnix == null || !Number.isFinite(nowUnix)) {
      nowUnix = Math.floor(Date.now() / 1000);
    }
    if (ctx.isHistoricalReplay && ctx.replayTargetTs != null) {
      return Math.floor(Number(ctx.replayTargetTs));
    }
    if (ctx.isHistoricalReplay && ctx.selectedUnix != null) {
      return Math.floor(Number(ctx.selectedUnix));
    }
    if (ctx.historyBlocksLive && ctx.pane) {
      var candles = ctx.pane.pendingData && ctx.pane.pendingData.candles;
      if (candles && candles.length) {
        var lastOpen = Number(candles[candles.length - 1].time);
        if (Number.isFinite(lastOpen)) {
          return Math.min(nowUnix, lastOpen + tfSec);
        }
      }
    }
    return nowUnix;
  }

  function scannerCommonHistoricalMode(ctx) {
    ctx = ctx || {};
    if (ctx.isHistoricalReplay) return true;
    if (ctx.historyBlocksLive) return true;
    return false;
  }

  /**
   * Returns decision_close unix (15m bar close) for COMMON POST.
   */
  function resolveScannerCommonDecisionCloseUnix(ctx) {
    var pane = ctx && ctx.pane;
    if (!pane || pane.tf !== "15m") return null;
    var tfSec = ctx.tfSec || 900;
    var asOf = resolveScannerCommonAsOfUnix(ctx);
    var closedOpen = lastClosedBarOpenOnPane(pane, tfSec, asOf);
    if (closedOpen == null) return null;
    return closedOpen + tfSec;
  }

  /**
   * Timeline request window [from_close, to_close] (inclusive unix seconds).
   */
  function timelineZoneIntersectsWindow(zone, fromCloseUnix, toCloseUnix) {
    if (!zone) return false;
    var from = Number(fromCloseUnix);
    var to = Number(toCloseUnix);
    var start = Number(zone.start_timestamp);
    var end = Number(zone.end_timestamp);
    if (!Number.isFinite(from) || !Number.isFinite(to)) return false;
    if (!Number.isFinite(start) || !Number.isFinite(end)) return false;
    if (end < from) return false;
    if (start > to) return false;
    return true;
  }

  function clipTimelineZoneToWindow(zone, fromCloseUnix, toCloseUnix) {
    if (!zone || !timelineZoneIntersectsWindow(zone, fromCloseUnix, toCloseUnix)) {
      return null;
    }
    var from = Number(fromCloseUnix);
    var to = Number(toCloseUnix);
    var start = Math.max(Number(zone.start_timestamp), from);
    var end = Math.min(Number(zone.end_timestamp), to);
    if (end < start) return null;
    var clipped = normalizeScannerCommonZone(zone);
    if (!clipped) return null;
    return Object.assign({}, clipped, {
      start_timestamp: start,
      end_timestamp: end,
      metadata: Object.assign({}, clipped.metadata || {}, {
        timeline_clipped: start !== Number(zone.start_timestamp) || end !== Number(zone.end_timestamp),
        timeline_window_from: from,
        timeline_window_to: to,
      }),
    });
  }

  function clipZonesForWindow(zones, fromCloseUnix, toCloseUnix) {
    var out = [];
    (zones || []).forEach(function (z) {
      if (!z) return;
      var clipped = clipTimelineZoneToWindow(z, fromCloseUnix, toCloseUnix);
      if (clipped) out.push(clipped);
    });
    return out;
  }

  function prepareTimelineZonesForDisplay(zones, fromCloseUnix, toCloseUnix) {
    var out = [];
    (zones || []).forEach(function (z) {
      if (!z) return;
      if (z.namespace && z.namespace !== SCANNER_COMMON_TIMELINE_NS) return;
      var clipped = clipTimelineZoneToWindow(z, fromCloseUnix, toCloseUnix);
      if (clipped) out.push(clipped);
    });
    return out;
  }

  function applyScannerCommonTimelineToOverlays(
    pendingOverlays,
    timelineZones,
    fromCloseUnix,
    toCloseUnix
  ) {
    var base = stripScannerCommonOverlays(pendingOverlays || []);
    var clipped = prepareTimelineZonesForDisplay(timelineZones, fromCloseUnix, toCloseUnix);
    return mergeScannerCommonOverlays(base, clipped);
  }

  function countScannerCommonTimelineOverlays(overlays) {
    var n = 0;
    (overlays || []).forEach(function (p) {
      if (isScannerCommonTimelineOverlay(p)) n += 1;
    });
    return n;
  }

  function uniqueOverlayIds(overlays) {
    var seen = {};
    (overlays || []).forEach(function (p) {
      if (p && p.id) seen[p.id] = true;
    });
    return Object.keys(seen);
  }

  /**
   * Resolve [from_close, to_close] ISO for timeline POST from pane candles + loaded range.
   */
  function resolveScannerCommonTimelineWindow(ctx) {
    ctx = ctx || {};
    var pane = ctx.pane;
    if (!pane || pane.tf !== "15m") return null;
    var tfSec = ctx.tfSec || 900;
    var candles = (pane.pendingData && pane.pendingData.candles) || [];
    if (!candles.length) return null;
    var toCloseUnix = resolveScannerCommonDecisionCloseUnix(ctx);
    if (toCloseUnix == null) return null;
    var fromHint = ctx.loadedFromUnix;
    var fromCloseUnix = null;
    for (var i = 0; i < candles.length; i += 1) {
      var open = Number(candles[i].time);
      if (!Number.isFinite(open)) continue;
      var close = open + tfSec;
      if (close > toCloseUnix) break;
      if (fromHint != null && close < Number(fromHint)) continue;
      fromCloseUnix = close;
      break;
    }
    if (fromCloseUnix == null) {
      var firstOpen = Number(candles[0].time);
      if (!Number.isFinite(firstOpen)) return null;
      fromCloseUnix = firstOpen + tfSec;
    }
    if (fromCloseUnix > toCloseUnix) return null;
    return {
      from_close_unix: fromCloseUnix,
      to_close_unix: toCloseUnix,
    };
  }

  function scannerCommonTimelineCacheKey(symbol, timeframe, fromIso, toIso, configVersion) {
    return (
      String(symbol || "") +
      "\x1f" +
      String(timeframe || "") +
      "\x1f" +
      "timeline\x1f" +
      String(fromIso || "") +
      "\x1f" +
      String(toIso || "") +
      "\x1f" +
      String(configVersion != null ? configVersion : "-")
    );
  }

  function scannerCommonCacheKey(symbol, timeframe, decisionIso, configVersion) {
    return (
      String(symbol || "") +
      "\x1f" +
      String(timeframe || "") +
      "\x1f" +
      String(decisionIso || "") +
      "\x1f" +
      String(configVersion != null ? configVersion : "-")
    );
  }

  return {
    SCANNER_COMMON_NS: SCANNER_COMMON_NS,
    SCANNER_COMMON_TIMELINE_NS: SCANNER_COMMON_TIMELINE_NS,
    CAUSAL_LLD_LANDSCAPE_NS: CAUSAL_LLD_LANDSCAPE_NS,
    LIVE_MICRO_LLD_V2_NS: LIVE_MICRO_LLD_V2_NS,
    LIVE_MICRO_LLD_V2_TFS: LIVE_MICRO_LLD_V2_TFS,
    isScannerCommonOverlay: isScannerCommonOverlay,
    isScannerCommonTimelineOverlay: isScannerCommonTimelineOverlay,
    isCausalLldLandscapeOverlay: isCausalLldLandscapeOverlay,
    isLiveMicroLldV2Overlay: isLiveMicroLldV2Overlay,
    isScannerCommonFamilyOverlay: isScannerCommonFamilyOverlay,
    isResearchPoolLayerOverlay: isResearchPoolLayerOverlay,
    stripScannerCommonOverlays: stripScannerCommonOverlays,
    stripCausalLldLandscapeOverlays: stripCausalLldLandscapeOverlays,
    stripLiveMicroLldV2Overlays: stripLiveMicroLldV2Overlays,
    normalizeScannerCommonZone: normalizeScannerCommonZone,
    selectPaneForScannerCommon: selectPaneForScannerCommon,
    scannerCommonOverlayToZone: scannerCommonOverlayToZone,
    convertScannerCommonOverlaysForRender: convertScannerCommonOverlaysForRender,
    mergeScannerCommonOverlays: mergeScannerCommonOverlays,
    mergePaneOverlaysPreservingScannerCommon: mergePaneOverlaysPreservingScannerCommon,
    countRenderableScannerCommon: countRenderableScannerCommon,
    closedBarOpenAdvanced: closedBarOpenAdvanced,
    shouldScheduleScannerCommonPollRefresh: shouldScheduleScannerCommonPollRefresh,
    coalesceScannerCommonPollSchedule: coalesceScannerCommonPollSchedule,
    markScannerCommonPollRefreshed: markScannerCommonPollRefreshed,
    lastClosedBarOpenOnPane: lastClosedBarOpenOnPane,
    resolveScannerCommonAsOfUnix: resolveScannerCommonAsOfUnix,
    scannerCommonHistoricalMode: scannerCommonHistoricalMode,
    resolveScannerCommonDecisionCloseUnix: resolveScannerCommonDecisionCloseUnix,
    scannerCommonCacheKey: scannerCommonCacheKey,
    resolveScannerCommonTimelineWindow: resolveScannerCommonTimelineWindow,
    scannerCommonTimelineCacheKey: scannerCommonTimelineCacheKey,
    timelineZoneIntersectsWindow: timelineZoneIntersectsWindow,
    clipTimelineZoneToWindow: clipTimelineZoneToWindow,
    clipZonesForWindow: clipZonesForWindow,
    prepareTimelineZonesForDisplay: prepareTimelineZonesForDisplay,
    applyScannerCommonTimelineToOverlays: applyScannerCommonTimelineToOverlays,
    countScannerCommonTimelineOverlays: countScannerCommonTimelineOverlays,
    uniqueOverlayIds: uniqueOverlayIds,
    applyCausalLldLandscapeToOverlays: applyCausalLldLandscapeToOverlays,
    applyLiveMicroLldV2ToOverlays: applyLiveMicroLldV2ToOverlays,
    causalLldLandscapeCacheKey: causalLldLandscapeCacheKey,
    liveMicroLldV2CacheKey: liveMicroLldV2CacheKey,
    resolvePaneCloseWindow: resolvePaneCloseWindow,
  };
});
