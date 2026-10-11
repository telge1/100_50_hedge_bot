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
  var LIVE_MICRO_LLD_V2_CH_TEST_NS = "LIVE_MICRO_LLD_V2_CH_TEST";
  var LIVE_MICRO_LLD_V2_TFS = { "1m": true, "5m": true, "15m": true, "30m": true, "1h": true, "4h": true };
  var LIVE_MICRO_LLD_V2_CH_TEST_RUN_ID = "de5f74f6-236f-4dc1-81df-94e15dec1b83";
  var LIVE_MICRO_LLD_V2_CH_TEST_COV_FROM_UNIX = Math.floor(
    Date.parse("2026-09-08T11:20:00.000Z") / 1000
  );
  var LIVE_MICRO_LLD_V2_CH_TEST_COV_TO_UNIX = Math.floor(
    Date.parse("2026-10-08T11:20:00.000Z") / 1000
  );

  /** Pine pool_ralf strength fill (RGB) + transparency → chart fill opacity. */
  var LIVE_MICRO_LLD_V2_STRENGTH_RGB = {
    Weak: "#2196f3",
    Medium: "#8bc34a",
    Strong: "#ffeb3b",
    "Very Strong": "#ff9800",
    Extreme: "#f44336",
  };
  var LIVE_MICRO_LLD_V2_STRENGTH_TRANSPARENCY = {
    Weak: 35,
    Medium: 30,
    Strong: 25,
    "Very Strong": 20,
    Extreme: 15,
  };
  var LIVE_MICRO_LLD_V2_BORDER_UPPER = "#ef5350";
  var LIVE_MICRO_LLD_V2_BORDER_LOWER = "#00bcd4";
  var LIVE_MICRO_LLD_V2_MICRO_LIVE_TRANSPARENCY = 75;
  /** Max V2 zone DOM nodes on chart — full list stays in pendingOverlays for merge/hint. */
  var LIVE_MICRO_LLD_V2_MAX_CHART_ZONES = 900;

  function normalizeLiveMicroLldV2StrengthClass(sc) {
    var s = String(sc || "").trim();
    if (!s) return "Weak";
    if (LIVE_MICRO_LLD_V2_STRENGTH_RGB[s]) return s;
    var low = s.toLowerCase();
    if (low === "md" || low === "medium") return "Medium";
    if (low === "vs" || low === "very strong" || low === "very_strong") return "Very Strong";
    if (low === "ex" || low === "extreme") return "Extreme";
    if (low === "weak") return "Weak";
    if (low === "strong") return "Strong";
    return s;
  }

  function liveMicroLldV2ZoneFillOpacity(strengthClass, microLive) {
    var sc = normalizeLiveMicroLldV2StrengthClass(strengthClass);
    var t = LIVE_MICRO_LLD_V2_STRENGTH_TRANSPARENCY[sc];
    if (t == null) t = 35;
    if (microLive) t = LIVE_MICRO_LLD_V2_MICRO_LIVE_TRANSPARENCY;
    return (100 - t) / 100;
  }

  function liveMicroLldV2ZoneBorderColor(side) {
    return String(side || "").toLowerCase() === "upper"
      ? LIVE_MICRO_LLD_V2_BORDER_UPPER
      : LIVE_MICRO_LLD_V2_BORDER_LOWER;
  }

  /**
   * Map API zone → research_trp renderZone: fill from strength_class, border from side.
   */
  function normalizeLiveMicroLldV2ZoneForRender(zone) {
    if (!zone || zone.namespace !== LIVE_MICRO_LLD_V2_NS) return zone;
    var meta = zone.metadata || {};
    var sc = normalizeLiveMicroLldV2StrengthClass(
      zone.strength_class != null ? zone.strength_class : meta.strength_class
    );
    var microLive =
      String(meta.pool_type || zone.pool_type || "") === "micro" &&
      Number(meta.early_timing != null ? meta.early_timing : zone.early_timing) === 0;
    var fillRgb = LIVE_MICRO_LLD_V2_STRENGTH_RGB[sc] || "#888888";
    var opacity = liveMicroLldV2ZoneFillOpacity(sc, microLive);
    var borderWidth = (zone.style && zone.style.lineWidth != null) ? zone.style.lineWidth : 2;
    if (meta.layer === "remaining") {
      opacity = 0.55;
      borderWidth = 1;
    } else if (meta.remaining_mode) {
      opacity = 0;
      borderWidth = 1;
    }
    var border = liveMicroLldV2ZoneBorderColor(zone.side);
    var style = Object.assign({}, zone.style || {}, {
      color: fillRgb,
      fillColor: fillRgb,
    });
    return Object.assign({}, zone, {
      strength_class: sc,
      style: style,
      border_color: border,
      border_width: borderWidth,
      opacity: opacity,
      metadata: Object.assign({}, meta, { strength_class: sc }),
    });
  }

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

  function isLiveMicroLldV2ChTestOverlay(p) {
    return !!(p && p.namespace === LIVE_MICRO_LLD_V2_CH_TEST_NS);
  }

  function isLiveMicroPoolOverlay(p) {
    return isLiveMicroLldV2Overlay(p) || isLiveMicroLldV2ChTestOverlay(p);
  }

  function isScannerCommonFamilyOverlay(p) {
    return isScannerCommonOverlay(p) || isScannerCommonTimelineOverlay(p);
  }

  function isResearchPoolLayerOverlay(p) {
    return (
      isScannerCommonFamilyOverlay(p) ||
      isCausalLldLandscapeOverlay(p) ||
      isLiveMicroLldV2Overlay(p) ||
      isLiveMicroLldV2ChTestOverlay(p)
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

  function stripLiveMicroLldV2ChTestOverlays(list) {
    return (list || []).filter(function (p) {
      return p && !isLiveMicroLldV2ChTestOverlay(p);
    });
  }

  function mergePaneOverlaysPreservingScannerCommon(
    newPaneOverlays,
    previousPendingOverlays,
    keepCommon,
    keepCausal,
    keepLiveMicroV2,
    keepLiveMicroChTest
  ) {
    var base = newPaneOverlays || [];
    var keepC = !!keepCommon;
    var keepL = !!keepCausal;
    var keepV2 = !!keepLiveMicroV2;
    var keepCh = !!keepLiveMicroChTest;
    if (!keepC && !keepL && !keepV2 && !keepCh) return base;
    var preserved = (previousPendingOverlays || []).filter(function (p) {
      return (
        (keepC && isScannerCommonFamilyOverlay(p)) ||
        (keepL && isCausalLldLandscapeOverlay(p)) ||
        (keepV2 && isLiveMicroLldV2Overlay(p)) ||
        (keepCh && isLiveMicroLldV2ChTestOverlay(p))
      );
    });
    if (!preserved.length) return base;
    return stripScannerCommonOverlays(
      stripCausalLldLandscapeOverlays(
        stripLiveMicroLldV2ChTestOverlays(stripLiveMicroLldV2Overlays(base))
      )
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
    var styled = (clipped || []).map(normalizeLiveMicroLldV2ZoneForRender);
    return base.concat(styled);
  }

  function normalizeLiveMicroLldV2ChTestZoneForRender(zone) {
    if (!zone || zone.namespace !== LIVE_MICRO_LLD_V2_CH_TEST_NS) return zone;
    var base = normalizeLiveMicroLldV2ZoneForRender(
      Object.assign({}, zone, { namespace: LIVE_MICRO_LLD_V2_NS })
    );
    return Object.assign({}, base, {
      namespace: LIVE_MICRO_LLD_V2_CH_TEST_NS,
      id: zone.id,
      border_style: "dashed",
    });
  }

  function applyLiveMicroLldV2ChTestToOverlays(
    pendingOverlays,
    zones,
    fromCloseUnix,
    toCloseUnix
  ) {
    var base = stripLiveMicroLldV2ChTestOverlays(pendingOverlays || []);
    var clipped = clipZonesForWindow(zones, fromCloseUnix, toCloseUnix);
    var styled = (clipped || []).map(normalizeLiveMicroLldV2ChTestZoneForRender);
    return base.concat(styled);
  }

  function zoneIntersectsTimeAndPrice(zone, timeLo, timeHi, priceLo, priceHi) {
    if (!zone) return false;
    var a = zone.start_timestamp != null ? Number(zone.start_timestamp) : null;
    var c = zone.end_timestamp != null ? Number(zone.end_timestamp) : a;
    if (a == null || !Number.isFinite(a)) return true;
    if (!Number.isFinite(c)) c = a;
    if (timeLo != null && timeHi != null && Number.isFinite(timeLo) && Number.isFinite(timeHi)) {
      if (c < timeLo || a > timeHi) return false;
    }
    var bot = zone.bottom_price != null ? Number(zone.bottom_price) : null;
    var top = zone.top_price != null ? Number(zone.top_price) : null;
    if (bot == null || top == null || !Number.isFinite(bot) || !Number.isFinite(top)) {
      return true;
    }
    if (priceLo != null && priceHi != null && Number.isFinite(priceLo) && Number.isFinite(priceHi)) {
      if (top < priceLo || bot > priceHi) return false;
    }
    return true;
  }

  function resolveChartViewportForPoolLayers(pane, chartApi) {
    var timeLo = null;
    var timeHi = null;
    var priceLo = null;
    var priceHi = null;
    if (chartApi) {
      if (chartApi.getVisibleTimeRange) {
        var tr = chartApi.getVisibleTimeRange();
        if (tr && tr.from != null && tr.to != null) {
          timeLo = Number(tr.from);
          timeHi = Number(tr.to);
        }
      }
      if (chartApi.getVisiblePriceRange) {
        var pr = chartApi.getVisiblePriceRange();
        if (pr && pr.from != null && pr.to != null) {
          priceLo = Number(pr.from);
          priceHi = Number(pr.to);
        }
      }
    }
    if (timeLo == null || timeHi == null) {
      var candles = pane && pane.pendingData && pane.pendingData.candles;
      if (candles && candles.length) {
        timeLo = Number(candles[0].time);
        timeHi = Number(candles[candles.length - 1].time);
      }
    }
    if (timeLo != null && timeHi != null && Number.isFinite(timeLo) && Number.isFinite(timeHi)) {
      var tPad = Math.max(86400, (timeHi - timeLo) * 0.08);
      timeLo -= tPad;
      timeHi += tPad;
    }
    if (priceLo != null && priceHi != null && Number.isFinite(priceLo) && Number.isFinite(priceHi)) {
      var pSpan = priceHi - priceLo;
      var pPad = Math.max(pSpan * 0.12, (priceHi + priceLo) * 0.0005);
      priceLo -= pPad;
      priceHi += pPad;
    }
    return {
      timeLo: timeLo,
      timeHi: timeHi,
      priceLo: priceLo,
      priceHi: priceHi,
    };
  }

  /**
   * pendingOverlays may hold 5k+ V2 zones; chart DOM only gets viewport subset.
   */
  function cullMicroPoolZonesForChart(v2, pane, chartApi) {
    if (!v2.length) return v2;
    if (v2.length <= LIVE_MICRO_LLD_V2_MAX_CHART_ZONES) return v2;
    var vp = resolveChartViewportForPoolLayers(pane, chartApi);
    var picked = v2.filter(function (z) {
      return zoneIntersectsTimeAndPrice(
        z,
        vp.timeLo,
        vp.timeHi,
        vp.priceLo,
        vp.priceHi
      );
    });
    if (!picked.length) {
      picked = v2.slice(-LIVE_MICRO_LLD_V2_MAX_CHART_ZONES);
    } else if (picked.length > LIVE_MICRO_LLD_V2_MAX_CHART_ZONES) {
      picked = picked.slice(0, LIVE_MICRO_LLD_V2_MAX_CHART_ZONES);
    }
    return picked;
  }

  function filterOverlaysForChartRender(overlays, pane, chartApi) {
    var list = overlays || [];
    var v2 = [];
    var chTest = [];
    var rest = [];
    list.forEach(function (p) {
      if (!p) return;
      if (isLiveMicroLldV2Overlay(p)) v2.push(p);
      else if (isLiveMicroLldV2ChTestOverlay(p)) chTest.push(p);
      else rest.push(p);
    });
    if (!v2.length && !chTest.length) return list;
    return rest
      .concat(cullMicroPoolZonesForChart(v2, pane, chartApi))
      .concat(cullMicroPoolZonesForChart(chTest, pane, chartApi));
  }

  function countLiveMicroLldV2Overlays(overlays) {
    var n = 0;
    (overlays || []).forEach(function (p) {
      if (isLiveMicroLldV2Overlay(p)) n += 1;
    });
    return n;
  }

  function countLiveMicroLldV2ChTestOverlays(overlays) {
    var n = 0;
    (overlays || []).forEach(function (p) {
      if (isLiveMicroLldV2ChTestOverlay(p)) n += 1;
    });
    return n;
  }

  function liveMicroLldV2NeedsViewportChartSync(overlays) {
    return (
      countLiveMicroLldV2Overlays(overlays) > LIVE_MICRO_LLD_V2_MAX_CHART_ZONES ||
      countLiveMicroLldV2ChTestOverlays(overlays) > LIVE_MICRO_LLD_V2_MAX_CHART_ZONES
    );
  }

  function liveMicroLldV2ChTestCacheKey(symbol, timeframe, fromIso, toIso, poolDetail, filterKey) {
    return (
      String(symbol || "") +
      "\x1f" +
      String(timeframe || "") +
      "\x1f" +
      "live_micro_lld_v2_ch_test\x1f" +
      "pd" +
      String(poolDetail != null ? poolDetail : "?") +
      "\x1f" +
      String(fromIso || "") +
      "\x1f" +
      String(toIso || "") +
      "\x1f" +
      String(filterKey != null ? filterKey : "-")
    );
  }

  function clipUnixToChTestCoverage(fromUnix, toUnix) {
    var covFrom = LIVE_MICRO_LLD_V2_CH_TEST_COV_FROM_UNIX;
    var covTo = LIVE_MICRO_LLD_V2_CH_TEST_COV_TO_UNIX;
    var from = Math.max(Math.floor(Number(fromUnix) || 0), covFrom);
    var to = Math.min(Math.floor(Number(toUnix) || 0), covTo);
    if (to < covFrom || from > covTo || from > to) return null;
    return { from_close_unix: from, to_close_unix: to };
  }

  /**
   * CH test + engine comparison: fixed Phase-1 coverage from, decision to (clipped to run end).
   */
  function resolveLiveMicroLldV2ChTestComparisonWindow(ctx) {
    ctx = ctx || {};
    var decisionClose = resolveLiveMicroLldV2DecisionCloseUnix(ctx);
    if (decisionClose == null) return null;
    var toClose = Math.min(
      Math.floor(Number(decisionClose)),
      LIVE_MICRO_LLD_V2_CH_TEST_COV_TO_UNIX
    );
    return clipUnixToChTestCoverage(LIVE_MICRO_LLD_V2_CH_TEST_COV_FROM_UNIX, toClose);
  }

  function resolveLiveMicroLldV2ChTestRequestWindow(pane, tfSec, maxWeeks, ctx, compareMode) {
    if (compareMode) {
      return resolveLiveMicroLldV2ChTestComparisonWindow(ctx);
    }
    ctx = ctx || {};
    var sec = tfSec || 300;
    var weeks = Math.max(1, Math.min(52, Number(maxWeeks) || 12));
    var span = weeks * 7 * 86400;
    var decisionClose = resolveLiveMicroLldV2DecisionCloseUnix(ctx);
    if (decisionClose == null) return null;
    var toClose = Math.min(
      Math.floor(Number(decisionClose)),
      LIVE_MICRO_LLD_V2_CH_TEST_COV_TO_UNIX
    );
    var fromClose = toClose - span;
    var paneBase = resolvePaneCloseWindow(pane, sec);
    if (paneBase && Number.isFinite(paneBase.from_close_unix)) {
      fromClose = Math.max(fromClose, Number(paneBase.from_close_unix));
    }
    return clipUnixToChTestCoverage(fromClose, toClose);
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

  /**
   * V2 engine window: last N weeks ending at pane close (not full loaded history).
   */
  function resolveLiveMicroLldV2CloseWindow(pane, tfSec, maxWeeks) {
    var base = resolvePaneCloseWindow(pane, tfSec);
    if (!base) return null;
    var weeks = Math.max(1, Math.min(52, Number(maxWeeks) || 12));
    var span = weeks * 7 * 86400;
    var toClose = Number(base.to_close_unix);
    var fromClose = Number(base.from_close_unix);
    if (!Number.isFinite(toClose) || !Number.isFinite(fromClose)) return null;
    var minFrom = toClose - span;
    if (fromClose < minFrom) fromClose = minFrom;
    return {
      from_close_unix: Math.floor(fromClose),
      to_close_unix: Math.floor(toClose),
      history_weeks: weeks,
    };
  }

  /** As-of instant for Micro V2 (replay target, selected bar, live now, pinned history). */
  function resolveLiveMicroLldV2AsOfUnix(ctx) {
    return resolveScannerCommonAsOfUnix(ctx);
  }

  /**
   * Bar close unix for Micro V2 API to_close (no forming bar).
   */
  function resolveLiveMicroLldV2DecisionCloseUnix(ctx) {
    var pane = ctx && ctx.pane;
    if (!pane || !pane.pendingData || !pane.pendingData.candles || !pane.pendingData.candles.length) {
      return null;
    }
    var tfSec = ctx.tfSec || 900;
    var asOf = resolveLiveMicroLldV2AsOfUnix(ctx);
    var closedOpen = lastClosedBarOpenOnPane(pane, tfSec, asOf);
    if (closedOpen == null) return null;
    return closedOpen + tfSec;
  }

  /**
   * Request window for POST live-micro-lld-v2.
   * Replay/history: to_close = decision close at T (not pane end).
   * Live: to_close = last closed bar on pane as of now.
   */
  function resolveLiveMicroLldV2RequestWindow(pane, tfSec, maxWeeks, ctx) {
    ctx = ctx || {};
    var sec = tfSec || 900;
    var weeks = Math.max(1, Math.min(52, Number(maxWeeks) || 12));
    var span = weeks * 7 * 86400;
    var decisionClose = resolveLiveMicroLldV2DecisionCloseUnix(ctx);
    if (decisionClose == null) {
      return resolveLiveMicroLldV2CloseWindow(pane, sec, weeks);
    }
    var toClose = Math.floor(Number(decisionClose));
    var fromClose = toClose - span;
    var paneBase = resolvePaneCloseWindow(pane, sec);
    if (paneBase && Number.isFinite(paneBase.from_close_unix)) {
      fromClose = Math.max(fromClose, Number(paneBase.from_close_unix));
    }
    if (!ctx.isHistoricalReplay && !ctx.historyBlocksLive && paneBase) {
      var paneTo = Number(paneBase.to_close_unix);
      if (Number.isFinite(paneTo) && toClose > paneTo) {
        toClose = paneTo;
      }
    }
    return {
      from_close_unix: Math.floor(fromClose),
      to_close_unix: toClose,
      history_weeks: weeks,
      decision_close_unix: toClose,
    };
  }

  /**
   * Signal-review helper: pool usable at decision close T (not for render culling).
   */
  function microV2ZoneUsableAt(zone, decisionCloseUnix) {
    if (!zone || decisionCloseUnix == null) return false;
    var T = Number(decisionCloseUnix);
    if (!Number.isFinite(T)) return false;
    var start =
      zone.start_timestamp != null
        ? Number(zone.start_timestamp)
        : zone.known_at != null
          ? Math.floor(Date.parse(String(zone.known_at)) / 1000)
          : null;
    if (start == null || !Number.isFinite(start) || start > T) return false;
    var end = zone.end_timestamp != null ? Number(zone.end_timestamp) : null;
    if (end != null && Number.isFinite(end) && end <= T) return false;
    return true;
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
    LIVE_MICRO_LLD_V2_CH_TEST_NS: LIVE_MICRO_LLD_V2_CH_TEST_NS,
    LIVE_MICRO_LLD_V2_CH_TEST_RUN_ID: LIVE_MICRO_LLD_V2_CH_TEST_RUN_ID,
    LIVE_MICRO_LLD_V2_CH_TEST_COV_FROM_UNIX: LIVE_MICRO_LLD_V2_CH_TEST_COV_FROM_UNIX,
    LIVE_MICRO_LLD_V2_CH_TEST_COV_TO_UNIX: LIVE_MICRO_LLD_V2_CH_TEST_COV_TO_UNIX,
    LIVE_MICRO_LLD_V2_TFS: LIVE_MICRO_LLD_V2_TFS,
    isScannerCommonOverlay: isScannerCommonOverlay,
    isScannerCommonTimelineOverlay: isScannerCommonTimelineOverlay,
    isCausalLldLandscapeOverlay: isCausalLldLandscapeOverlay,
    isLiveMicroLldV2Overlay: isLiveMicroLldV2Overlay,
    isLiveMicroLldV2ChTestOverlay: isLiveMicroLldV2ChTestOverlay,
    isScannerCommonFamilyOverlay: isScannerCommonFamilyOverlay,
    isResearchPoolLayerOverlay: isResearchPoolLayerOverlay,
    stripScannerCommonOverlays: stripScannerCommonOverlays,
    stripCausalLldLandscapeOverlays: stripCausalLldLandscapeOverlays,
    stripLiveMicroLldV2Overlays: stripLiveMicroLldV2Overlays,
    stripLiveMicroLldV2ChTestOverlays: stripLiveMicroLldV2ChTestOverlays,
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
    applyLiveMicroLldV2ChTestToOverlays: applyLiveMicroLldV2ChTestToOverlays,
    filterOverlaysForChartRender: filterOverlaysForChartRender,
    countLiveMicroLldV2Overlays: countLiveMicroLldV2Overlays,
    countLiveMicroLldV2ChTestOverlays: countLiveMicroLldV2ChTestOverlays,
    liveMicroLldV2NeedsViewportChartSync: liveMicroLldV2NeedsViewportChartSync,
    zoneIntersectsTimeAndPrice: zoneIntersectsTimeAndPrice,
    LIVE_MICRO_LLD_V2_MAX_CHART_ZONES: LIVE_MICRO_LLD_V2_MAX_CHART_ZONES,
    causalLldLandscapeCacheKey: causalLldLandscapeCacheKey,
    liveMicroLldV2CacheKey: liveMicroLldV2CacheKey,
    liveMicroLldV2ChTestCacheKey: liveMicroLldV2ChTestCacheKey,
    resolveLiveMicroLldV2ChTestRequestWindow: resolveLiveMicroLldV2ChTestRequestWindow,
    resolveLiveMicroLldV2ChTestComparisonWindow: resolveLiveMicroLldV2ChTestComparisonWindow,
    clipUnixToChTestCoverage: clipUnixToChTestCoverage,
    resolvePaneCloseWindow: resolvePaneCloseWindow,
    resolveLiveMicroLldV2CloseWindow: resolveLiveMicroLldV2CloseWindow,
    resolveLiveMicroLldV2AsOfUnix: resolveLiveMicroLldV2AsOfUnix,
    resolveLiveMicroLldV2DecisionCloseUnix: resolveLiveMicroLldV2DecisionCloseUnix,
    resolveLiveMicroLldV2RequestWindow: resolveLiveMicroLldV2RequestWindow,
    microV2ZoneUsableAt: microV2ZoneUsableAt,
    normalizeLiveMicroLldV2StrengthClass: normalizeLiveMicroLldV2StrengthClass,
    normalizeLiveMicroLldV2ZoneForRender: normalizeLiveMicroLldV2ZoneForRender,
    normalizeLiveMicroLldV2ChTestZoneForRender: normalizeLiveMicroLldV2ChTestZoneForRender,
    liveMicroLldV2ZoneFillOpacity: liveMicroLldV2ZoneFillOpacity,
    liveMicroLldV2ZoneBorderColor: liveMicroLldV2ZoneBorderColor,
    LIVE_MICRO_LLD_V2_STRENGTH_RGB: LIVE_MICRO_LLD_V2_STRENGTH_RGB,
  };
});
