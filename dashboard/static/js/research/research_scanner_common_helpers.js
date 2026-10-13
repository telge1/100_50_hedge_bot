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
  var HISTORICAL_LMV2_NS = "HISTORICAL_LMV2_POOLS";
  var HISTORICAL_LMV2_PILOT_FROM_UNIX = Math.floor(
    Date.parse("2026-08-01T00:00:00.000Z") / 1000
  );
  var HISTORICAL_LMV2_PILOT_TO_UNIX = Math.floor(
    Date.parse("2026-09-30T23:45:00.000Z") / 1000
  );
  var HISTORICAL_LMV2_TF_SEC = 900;
  var HISTORICAL_LMV2_MAX_CLOSES_PER_REQUEST = 96;
  var HISTORICAL_LMV2_CHUNK_PREFETCH = 1;
  var HISTORICAL_LMV2_CHUNK_CACHE_MAX = 32;
  var HISTORICAL_LMV2_SUPPORTED_PD = [1, 5, 10];
  var HISTORICAL_LMV2_PD_REGISTRY = {
    1: {
      pipeline_line_id: "historical_mixed_frozen_doge_apr_sep_2026_v1_15mpd1_v1",
      config_hash: "4aeb36fe22ef3ab24fc65c8e7bc33b6ca499dd51759d07936c96fee781c641c9",
    },
    5: {
      pipeline_line_id: "historical_mixed_frozen_doge_apr_sep_2026_v1_15mpd5_v1",
      config_hash: "533e04c7cd0e1c93540ed9466a2a5a2eb626f82b828b9a45d1da234703ebd03c",
    },
    10: {
      pipeline_line_id: "historical_mixed_frozen_doge_apr_sep_2026_v1_15mpd10_v1",
      config_hash: "bd9ac4f30e177321b688f5adb0fe2681ebb4966d51701e3b323498bfcd0d06c2",
    },
  };
  var HISTORICAL_LMV2_PIPELINE_LINE = HISTORICAL_LMV2_PD_REGISTRY[10].pipeline_line_id;

  function historicalLmv2PdRegistryEntry(poolDetail) {
    var pd = Number(poolDetail);
    return HISTORICAL_LMV2_PD_REGISTRY[pd] || null;
  }

  function historicalLmv2CacheScopeKey(poolDetail) {
    var ent = historicalLmv2PdRegistryEntry(poolDetail == null ? 10 : poolDetail);
    if (!ent) {
      return "pd_invalid";
    }
    return (
      "pd" +
      String(poolDetail == null ? 10 : poolDetail) +
      "\x1f" +
      ent.pipeline_line_id +
      "\x1f" +
      ent.config_hash
    );
  }
  var RESEARCH_STATIC_TIMELINE_IMPORT_FROM_ISO = "2026-04-01T00:00:00Z";
  var RESEARCH_STATIC_TIMELINE_IMPORT_TO_ISO = "2026-09-30T23:45:00Z";
  var RESEARCH_STATIC_TIMELINE_FROM_UNIX = Math.floor(
    Date.parse(RESEARCH_STATIC_TIMELINE_IMPORT_FROM_ISO) / 1000
  );
  var RESEARCH_STATIC_TIMELINE_TO_UNIX = Math.floor(
    Date.parse(RESEARCH_STATIC_TIMELINE_IMPORT_TO_ISO) / 1000
  );
  var STATIC_TIMELINE_MAX_CLOSES = HISTORICAL_LMV2_MAX_CLOSES_PER_REQUEST * 3;
  var STATIC_TIMELINE_MAX_SPAN_UNIX = (STATIC_TIMELINE_MAX_CLOSES - 1) * HISTORICAL_LMV2_TF_SEC;
  var STATIC_TIMELINE_CACHE_MAX = 16;
  var STATIC_TIMELINE_PREFETCH_WINDOWS = 1;
  var STATIC_TIMELINE_PRELOAD_READER_VERSION = "historical_pool_state_v1";
  var PHASE5P_PILOT_MAX_CLOSES = 672;
  var PHASE5P_PILOT_FROM_ISO = "2026-09-01T00:00:00Z";
  var PHASE5P_PILOT_TO_ISO = "2026-09-07T23:45:00Z";
  var PHASE5M_STATIC_TIMELINE_FROM_ISO = "2026-09-27T00:00:00Z";
  var PHASE5M_STATIC_TIMELINE_TO_ISO = "2026-09-29T23:45:00Z";
  var PHASE5M_STATIC_TIMELINE_FROM_UNIX = Math.floor(
    Date.parse(PHASE5M_STATIC_TIMELINE_FROM_ISO) / 1000
  );
  var PHASE5M_STATIC_TIMELINE_TO_UNIX = Math.floor(
    Date.parse(PHASE5M_STATIC_TIMELINE_TO_ISO) / 1000
  );
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

  function isHistoricalLmv2Overlay(p) {
    return !!(p && p.namespace === HISTORICAL_LMV2_NS);
  }

  function isLiveMicroPoolOverlay(p) {
    return (
      isLiveMicroLldV2Overlay(p) ||
      isLiveMicroLldV2ChTestOverlay(p) ||
      isHistoricalLmv2Overlay(p)
    );
  }

  function isScannerCommonFamilyOverlay(p) {
    return isScannerCommonOverlay(p) || isScannerCommonTimelineOverlay(p);
  }

  function isResearchPoolLayerOverlay(p) {
    return (
      isScannerCommonFamilyOverlay(p) ||
      isCausalLldLandscapeOverlay(p) ||
      isLiveMicroLldV2Overlay(p) ||
      isLiveMicroLldV2ChTestOverlay(p) ||
      isHistoricalLmv2Overlay(p)
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

  function stripHistoricalLmv2Overlays(list) {
    return (list || []).filter(function (p) {
      return p && !isHistoricalLmv2Overlay(p);
    });
  }

  function countHistoricalLmv2Overlays(list) {
    var n = 0;
    (list || []).forEach(function (p) {
      if (isHistoricalLmv2Overlay(p)) n += 1;
    });
    return n;
  }

  function mergePaneOverlaysPreservingScannerCommon(
    newPaneOverlays,
    previousPendingOverlays,
    keepCommon,
    keepCausal,
    keepLiveMicroV2,
    keepLiveMicroChTest,
    keepHistoricalLmv2
  ) {
    var base = newPaneOverlays || [];
    var keepC = !!keepCommon;
    var keepL = !!keepCausal;
    var keepV2 = !!keepLiveMicroV2;
    var keepCh = !!keepLiveMicroChTest;
    var keepHist = !!keepHistoricalLmv2;
    if (!keepC && !keepL && !keepV2 && !keepCh && !keepHist) return base;
    var preserved = (previousPendingOverlays || []).filter(function (p) {
      return (
        (keepC && isScannerCommonFamilyOverlay(p)) ||
        (keepL && isCausalLldLandscapeOverlay(p)) ||
        (keepV2 && isLiveMicroLldV2Overlay(p)) ||
        (keepCh && isLiveMicroLldV2ChTestOverlay(p)) ||
        (keepHist && isHistoricalLmv2Overlay(p))
      );
    });
    if (!preserved.length) return base;
    return stripScannerCommonOverlays(
      stripCausalLldLandscapeOverlays(
        stripHistoricalLmv2Overlays(
          stripLiveMicroLldV2ChTestOverlays(stripLiveMicroLldV2Overlays(base))
        )
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

  function clipUnixToHistoricalPilot(fromUnix, toUnix) {
    var covFrom = HISTORICAL_LMV2_PILOT_FROM_UNIX;
    var covTo = HISTORICAL_LMV2_PILOT_TO_UNIX;
    var from = Math.max(Math.floor(Number(fromUnix) || 0), covFrom);
    var to = Math.min(Math.floor(Number(toUnix) || 0), covTo);
    if (to < covFrom || from > covTo || from > to) return null;
    return { from_close_unix: from, to_close_unix: to };
  }

  function historicalDecisionCloseAtOrBefore(asOfUnix, tfSec) {
    var step = tfSec || HISTORICAL_LMV2_TF_SEC;
    var u = Math.floor(Number(asOfUnix));
    if (!Number.isFinite(u)) return null;
    var mod = ((u % step) + step) % step;
    var close = u - mod;
    return close;
  }

  function resolveHistoricalLmv2AsOfCloseUnix(ctx) {
    ctx = ctx || {};
    var sec = ctx.tfSec || HISTORICAL_LMV2_TF_SEC;
    if (ctx.isHistoricalReplay && ctx.replayTargetTs != null) {
      return historicalDecisionCloseAtOrBefore(ctx.replayTargetTs, sec);
    }
    var dc = resolveLiveMicroLldV2DecisionCloseUnix(ctx);
    if (dc != null) return dc;
    return null;
  }

  function resolveHistoricalLmv2RequestWindow(pane, tfSec, ctx) {
    ctx = ctx || {};
    var sec = tfSec || HISTORICAL_LMV2_TF_SEC;
    var maxCloses = HISTORICAL_LMV2_MAX_CLOSES_PER_REQUEST;
    var decisionClose = resolveHistoricalLmv2AsOfCloseUnix(ctx);
    if (decisionClose == null) return null;
    var toClose = Math.min(Math.floor(Number(decisionClose)), HISTORICAL_LMV2_PILOT_TO_UNIX);
    toClose = Math.max(toClose, HISTORICAL_LMV2_PILOT_FROM_UNIX);
    var spanSec = (maxCloses - 1) * sec;
    var fromClose = toClose - spanSec;
    fromClose = Math.max(fromClose, HISTORICAL_LMV2_PILOT_FROM_UNIX);
    var paneBase = resolvePaneCloseWindow(pane, sec);
    if (paneBase && Number.isFinite(paneBase.from_close_unix) && Number.isFinite(paneBase.to_close_unix)) {
      var paneFrom = Math.max(Math.floor(paneBase.from_close_unix), HISTORICAL_LMV2_PILOT_FROM_UNIX);
      var paneTo = Math.min(Math.floor(paneBase.to_close_unix), toClose);
      if (paneTo >= paneFrom && paneFrom >= fromClose && paneTo <= toClose) {
        var closeCount = Math.floor((paneTo - paneFrom) / sec) + 1;
        if (closeCount >= 1 && closeCount <= maxCloses) {
          fromClose = paneFrom;
          toClose = paneTo;
        }
      }
    }
    return clipUnixToHistoricalPilot(fromClose, toClose);
  }

  function resolveHistoricalLmv2ViewportCloseWindow(pane, tfSec, ctx, chartApi) {
    var sec = tfSec || HISTORICAL_LMV2_TF_SEC;
    var asOf = resolveHistoricalLmv2AsOfCloseUnix(ctx || {});
    if (asOf == null) return null;
    asOf = Math.min(Math.floor(Number(asOf)), HISTORICAL_LMV2_PILOT_TO_UNIX);
    asOf = Math.max(asOf, HISTORICAL_LMV2_PILOT_FROM_UNIX);
    var viewFrom = null;
    var viewTo = null;
    if (chartApi && chartApi.getVisibleTimeRange) {
      var tr = chartApi.getVisibleTimeRange();
      if (tr && tr.from != null && tr.to != null) {
        var scaleFrom = Math.floor(Number(tr.from));
        var scaleTo = Math.floor(Number(tr.to));
        var scaleFromClose = scaleFrom + sec;
        var scaleToClose = historicalDecisionCloseAtOrBefore(scaleTo, sec);
        var firstCandle = tr.firstCandle != null ? Math.floor(Number(tr.firstCandle)) : null;
        var lastCandle = tr.lastCandle != null ? Math.floor(Number(tr.lastCandle)) : null;
        var candleFromClose = firstCandle != null ? firstCandle + sec : scaleFromClose;
        var candleToClose = lastCandle != null ? lastCandle + sec : scaleToClose;
        viewFrom = Math.min(candleFromClose, scaleFromClose);
        viewTo = Math.max(candleToClose, scaleToClose);
      } else if (tr && tr.firstCandle != null && tr.lastCandle != null) {
        viewFrom = Math.floor(Number(tr.firstCandle)) + sec;
        viewTo = Math.floor(Number(tr.lastCandle)) + sec;
      }
    }
    if (viewFrom == null || viewTo == null) {
      var fallback = resolveHistoricalLmv2RequestWindow(pane, sec, ctx);
      if (!fallback) return null;
      return {
        view_from_close_unix: fallback.from_close_unix,
        view_to_close_unix: fallback.to_close_unix,
        as_of_close_unix: asOf,
      };
    }
    var visibleFrom = viewFrom;
    var visibleTo = viewTo;
    viewTo = Math.min(viewTo, asOf);
    viewFrom = Math.max(viewFrom, HISTORICAL_LMV2_PILOT_FROM_UNIX);
    if (visibleFrom > asOf || viewTo < viewFrom) {
      return {
        view_from_close_unix: visibleFrom,
        view_to_close_unix: visibleTo,
        as_of_close_unix: asOf,
        viewport_after_replay_cutoff: true,
      };
    }
    return {
      view_from_close_unix: viewFrom,
      view_to_close_unix: viewTo,
      display_from_close_unix: viewFrom,
      display_to_close_unix: Math.min(visibleTo, asOf),
      as_of_close_unix: asOf,
      viewport_after_replay_cutoff: false,
    };
  }

  function clampCloseUnixToStaticImport(unix) {
    var u = Math.floor(Number(unix));
    if (!Number.isFinite(u)) return RESEARCH_STATIC_TIMELINE_FROM_UNIX;
    return Math.min(
      Math.max(u, RESEARCH_STATIC_TIMELINE_FROM_UNIX),
      RESEARCH_STATIC_TIMELINE_TO_UNIX
    );
  }

  function planStaticTimelineWindows(fromCloseUnix, toCloseUnix) {
    var sec = HISTORICAL_LMV2_TF_SEC;
    var span = STATIC_TIMELINE_MAX_SPAN_UNIX;
    var from = clampCloseUnixToStaticImport(fromCloseUnix);
    var to = clampCloseUnixToStaticImport(toCloseUnix);
    if (to < from) return [];
    var out = [];
    var start = from;
    while (start <= to) {
      var end = Math.min(start + span, to);
      out.push({ from_close_unix: start, to_close_unix: end });
      if (end >= to) break;
      start = end + sec;
    }
    return out;
  }

  function parseStaticTimelineUtcInput(text) {
    var raw = String(text || "").trim();
    if (!raw) return null;
    var iso = raw.indexOf("T") >= 0 ? raw : raw.replace(" ", "T");
    if (!/Z$/i.test(iso)) iso = iso.replace(/\s*UTC\s*$/i, "") + "Z";
    var ms = Date.parse(iso);
    if (!Number.isFinite(ms)) return null;
    return Math.floor(ms / 1000);
  }

  function parseStaticTimelineSelectedRangeFromInputs() {
    if (typeof document === "undefined") return null;
    var fromEl = document.getElementById("researchStaticTimelineFrom");
    var toEl = document.getElementById("researchStaticTimelineTo");
    if (!fromEl || !toEl) return null;
    var manualFrom = parseStaticTimelineUtcInput(fromEl.value);
    var manualTo = parseStaticTimelineUtcInput(toEl.value);
    if (manualFrom == null || manualTo == null || manualTo < manualFrom) return null;
    return {
      from_close_unix: clampCloseUnixToStaticImport(manualFrom),
      to_close_unix: clampCloseUnixToStaticImport(manualTo),
    };
  }

  function intersectCloseRanges(aFrom, aTo, bFrom, bTo) {
    var from = Math.max(Math.floor(Number(aFrom)), Math.floor(Number(bFrom)));
    var to = Math.min(Math.floor(Number(aTo)), Math.floor(Number(bTo)));
    if (to < from) return null;
    return { from_close_unix: from, to_close_unix: to };
  }

  function resolveStaticTimelineVisibleCloseRange(chartApi, tfSec, selectedRange) {
    var sec = tfSec || HISTORICAL_LMV2_TF_SEC;
    var selFrom = selectedRange
      ? selectedRange.from_close_unix
      : RESEARCH_STATIC_TIMELINE_FROM_UNIX;
    var selTo = selectedRange
      ? selectedRange.to_close_unix
      : RESEARCH_STATIC_TIMELINE_TO_UNIX;
    var viewFrom = null;
    var viewTo = null;
    if (chartApi && chartApi.getVisibleTimeRange) {
      var tr = chartApi.getVisibleTimeRange();
      if (tr && tr.from != null && tr.to != null) {
        viewFrom = Math.floor(Number(tr.from)) + sec;
        viewTo = historicalDecisionCloseAtOrBefore(Math.floor(Number(tr.to)), sec);
      }
    }
    if (viewFrom == null || viewTo == null) {
      viewFrom = selFrom;
      viewTo = Math.min(selFrom + STATIC_TIMELINE_MAX_SPAN_UNIX, selTo);
    }
    var vis = intersectCloseRanges(viewFrom, viewTo, selFrom, selTo);
    if (!vis) {
      vis = { from_close_unix: selFrom, to_close_unix: Math.min(selFrom + STATIC_TIMELINE_MAX_SPAN_UNIX, selTo) };
    }
    return vis;
  }

  function expandStaticTimelineLoadRange(visibleFrom, visibleTo, selectedRange, prefetchWindows) {
    var span = STATIC_TIMELINE_MAX_SPAN_UNIX;
    var sec = HISTORICAL_LMV2_TF_SEC;
    var pf = prefetchWindows == null ? STATIC_TIMELINE_PREFETCH_WINDOWS : prefetchWindows;
    var selFrom = selectedRange
      ? selectedRange.from_close_unix
      : RESEARCH_STATIC_TIMELINE_FROM_UNIX;
    var selTo = selectedRange
      ? selectedRange.to_close_unix
      : RESEARCH_STATIC_TIMELINE_TO_UNIX;
    var from = Math.max(selFrom, Math.floor(Number(visibleFrom)) - pf * span);
    var to = Math.min(selTo, Math.floor(Number(visibleTo)) + pf * span);
    if (to < from) to = from;
    return { from_close_unix: from, to_close_unix: to };
  }

  function planStaticTimelineLoadForVisible(visibleFrom, visibleTo, selectedRange, prefetchWindows) {
    var expanded = expandStaticTimelineLoadRange(visibleFrom, visibleTo, selectedRange, prefetchWindows);
    var windows = planStaticTimelineWindows(expanded.from_close_unix, expanded.to_close_unix);
    return {
      load_from_close_unix: expanded.from_close_unix,
      load_to_close_unix: expanded.to_close_unix,
      windows: windows,
      visible_from_close_unix: Math.floor(Number(visibleFrom)),
      visible_to_close_unix: Math.floor(Number(visibleTo)),
    };
  }

  function gatherStaticTimelineZoneParts(cacheState, symbol, timeframe, windows, poolDetail) {
    var parts = [];
    (windows || []).forEach(function (w) {
      var fromIso = historicalCloseUnixToIsoZ(w.from_close_unix);
      var toIso = historicalCloseUnixToIsoZ(w.to_close_unix);
      var key = staticTimelineCacheKey(symbol, timeframe, fromIso, toIso, poolDetail);
      var ent = cacheState && cacheState.windows ? cacheState.windows[key] : null;
      if (ent && ent.status === "ok" && ent.body && ent.body.zones) {
        parts.push(ent.body.zones);
      }
    });
    return parts;
  }

  function staticTimelineWindowCoverage(
    windows,
    cacheState,
    symbol,
    timeframe,
    fromCloseUnix,
    toCloseUnix,
    poolDetail
  ) {
    var closeCov = historicalLmv2CloseCoverage(
      windows,
      fromCloseUnix,
      toCloseUnix,
      HISTORICAL_LMV2_TF_SEC
    );
    var winCov = staticTimelineLoadCoverage(windows, cacheState, symbol, timeframe, poolDetail);
    return {
      closes: closeCov,
      windows: winCov,
      complete: closeCov.complete && winCov.complete,
    };
  }

  function resolveStaticTimelineDesiredWindow(pane, tfSec, chartApi, ctx) {
    var selected = (ctx && ctx.selected_range) || null;
    var visible = resolveStaticTimelineVisibleCloseRange(chartApi, tfSec, selected);
    return {
      selected_range: selected,
      visible_from_close_unix: visible.from_close_unix,
      visible_to_close_unix: visible.to_close_unix,
      source: selected ? "selected_plus_visible" : "visible_only",
    };
  }

  function staticTimelineCacheKey(symbol, timeframe, fromIso, toIso, poolDetail) {
    return (
      String(symbol || "") +
      "\x1f" +
      String(timeframe || "") +
      "\x1f" +
      historicalLmv2CacheScopeKey(poolDetail == null ? 10 : poolDetail) +
      "\x1fstatic\x1f" +
      String(fromIso || "") +
      "\x1f" +
      String(toIso || "")
    );
  }

  function staticTimelinePreloadBundleKey(symbol, timeframe, fromUnix, toUnix, poolDetail) {
    return (
      String(symbol || "") +
      "\x1f" +
      String(timeframe || "") +
      "\x1f" +
      historicalLmv2CacheScopeKey(poolDetail == null ? 10 : poolDetail) +
      "\x1fpreload\x1f" +
      STATIC_TIMELINE_PRELOAD_READER_VERSION +
      "\x1f" +
      String(fromUnix) +
      "\x1f" +
      String(toUnix)
    );
  }

  function staticTimelineCloseCount(fromUnix, toUnix) {
    var sec = HISTORICAL_LMV2_TF_SEC;
    var from = Math.floor(Number(fromUnix));
    var to = Math.floor(Number(toUnix));
    if (!Number.isFinite(from) || !Number.isFinite(to) || to < from) return 0;
    return Math.floor((to - from) / sec) + 1;
  }

  function validateStaticTimelinePreloadRange(fromUnix, toUnix) {
    var from = clampCloseUnixToStaticImport(fromUnix);
    var to = clampCloseUnixToStaticImport(toUnix);
    if (to < from) return { ok: false, reason: "to before from" };
    var closes = staticTimelineCloseCount(from, to);
    if (closes > PHASE5P_PILOT_MAX_CLOSES) {
      return {
        ok: false,
        reason: "pilot max " + PHASE5P_PILOT_MAX_CLOSES + " closes (7 days @ 15m)",
        close_count: closes,
      };
    }
    return { ok: true, from_close_unix: from, to_close_unix: to, close_count: closes };
  }

  function planStaticTimelinePreloadWindows(fromUnix, toUnix) {
    return planStaticTimelineWindows(fromUnix, toUnix);
  }

  function touchStaticTimelineCache(cacheState, key, entry, opts) {
    if (!cacheState || !key) return;
    if (!cacheState.windows) cacheState.windows = {};
    if (!cacheState.order) cacheState.order = [];
    var pin = (opts && opts.pinKeys) || null;
    cacheState.windows[key] = entry;
    var order = cacheState.order.filter(function (k) { return k !== key; });
    order.push(key);
    var max = (opts && opts.maxEntries) || STATIC_TIMELINE_CACHE_MAX;
    while (order.length > max) {
      var dropIdx = -1;
      for (var di = 0; di < order.length; di += 1) {
        var cand = order[di];
        if (!cand || cand === key) continue;
        if (pin && pin[cand]) continue;
        dropIdx = di;
        break;
      }
      if (dropIdx < 0) break;
      var drop = order.splice(dropIdx, 1)[0];
      if (drop && cacheState.windows[drop]) {
        if (opts && opts.onEvict) opts.onEvict(drop);
        delete cacheState.windows[drop];
      }
    }
    cacheState.order = order;
  }

  function staticTimelinePinKeysFromWindows(symbol, timeframe, windows, poolDetail) {
    var sym = symbol || "DOGEUSDT";
    var tf = timeframe || "15m";
    var pin = {};
    (windows || []).forEach(function (w) {
      var fromIso = historicalCloseUnixToIsoZ(w.from_close_unix);
      var toIso = historicalCloseUnixToIsoZ(w.to_close_unix);
      pin[staticTimelineCacheKey(sym, tf, fromIso, toIso, poolDetail)] = true;
    });
    return pin;
  }

  function assessStaticTimelinePreloadBundle(
    cacheState,
    symbol,
    timeframe,
    fromUnix,
    toUnix,
    windows,
    poolDetail
  ) {
    var sym = symbol || "DOGEUSDT";
    var tf = timeframe || "15m";
    var from = Math.floor(Number(fromUnix));
    var to = Math.floor(Number(toUnix));
    var plan = windows || planStaticTimelinePreloadWindows(from, to);
    var cov = staticTimelineWindowCoverage(plan, cacheState, sym, tf, from, to, poolDetail);
    var parts = gatherStaticTimelineZoneParts(cacheState, sym, tf, plan, poolDetail);
    var merged = mergeHistoricalLmv2Zones(parts);
    return {
      windows: plan,
      coverage: cov,
      merged_zones: merged,
      segment_count: merged.length,
      pool_count: merged.reduce(function (acc, z) {
        if (z && z.pool_id) acc[z.pool_id] = true;
        return acc;
      }, {}),
      ready: cov.complete,
    };
  }

  function staticTimelineLoadCoverage(plannedWindows, cacheState, symbol, timeframe, poolDetail) {
    var sym = symbol || "DOGEUSDT";
    var tf = timeframe || "15m";
    var loaded = 0;
    var missing = 0;
    (plannedWindows || []).forEach(function (w) {
      var fromIso = historicalCloseUnixToIsoZ(w.from_close_unix);
      var toIso = historicalCloseUnixToIsoZ(w.to_close_unix);
      var key = staticTimelineCacheKey(sym, tf, fromIso, toIso, poolDetail);
      var ent = cacheState && cacheState.windows ? cacheState.windows[key] : null;
      if (ent && ent.status === "ok" && ent.body) loaded += 1;
      else missing += 1;
    });
    return {
      windows_planned: (plannedWindows || []).length,
      windows_loaded: loaded,
      windows_missing: missing,
      complete: missing === 0 && (plannedWindows || []).length > 0,
    };
  }

  function planHistoricalLmv2Chunks(viewFromClose, viewToClose, asOfClose, tfSec, prefetchChunks) {
    var sec = tfSec || HISTORICAL_LMV2_TF_SEC;
    var maxCloses = HISTORICAL_LMV2_MAX_CLOSES_PER_REQUEST;
    var span = (maxCloses - 1) * sec;
    var prefetch = prefetchChunks == null ? HISTORICAL_LMV2_CHUNK_PREFETCH : prefetchChunks;
    var asOf = Math.min(Math.floor(Number(asOfClose)), HISTORICAL_LMV2_PILOT_TO_UNIX);
    var viewTo = Math.min(Math.floor(Number(viewToClose)), asOf);
    var viewFrom = Math.max(Math.floor(Number(viewFromClose)), HISTORICAL_LMV2_PILOT_FROM_UNIX);
    if (viewTo < viewFrom) return [];
    var loadFrom = viewFrom;
    var loadTo = viewTo;
    if (prefetch > 0) {
      loadFrom = Math.max(HISTORICAL_LMV2_PILOT_FROM_UNIX, viewFrom - prefetch * span);
      loadTo = Math.min(viewTo + prefetch * span, asOf, HISTORICAL_LMV2_PILOT_TO_UNIX);
    }
    var chunks = [];
    var start = loadFrom;
    while (start <= loadTo) {
      var end = Math.min(start + span, loadTo);
      chunks.push({ from_close_unix: start, to_close_unix: end });
      if (end >= loadTo) break;
      start = end + sec;
    }
    return chunks;
  }

  function historicalLmv2CloseCoverage(chunks, fromCloseUnix, toCloseUnix, tfSec) {
    var sec = tfSec || HISTORICAL_LMV2_TF_SEC;
    var from = Math.floor(Number(fromCloseUnix));
    var to = Math.floor(Number(toCloseUnix));
    if (!Number.isFinite(from) || !Number.isFinite(to) || to < from) {
      return { complete: false, missing_count: 0, covered_count: 0, required_count: 0 };
    }
    var covered = {};
    (chunks || []).forEach(function (ch) {
      if (!ch) return;
      var t = Math.floor(Number(ch.from_close_unix));
      var end = Math.floor(Number(ch.to_close_unix));
      while (t <= end) {
        covered[t] = true;
        t += sec;
      }
    });
    var missing = 0;
    var required = 0;
    for (var c = from; c <= to; c += sec) {
      required += 1;
      if (!covered[c]) missing += 1;
    }
    return {
      complete: missing === 0,
      missing_count: missing,
      covered_count: required - missing,
      required_count: required,
    };
  }

  function historicalLmv2ChunkCacheKey(
    symbol,
    timeframe,
    fromIso,
    toIso,
    asOfIso,
    layersKey,
    poolDetail
  ) {
    return (
      String(symbol || "") +
      "\x1f" +
      String(timeframe || "") +
      "\x1f" +
      historicalLmv2CacheScopeKey(poolDetail == null ? 10 : poolDetail) +
      "\x1fchunk\x1f" +
      String(fromIso || "") +
      "\x1f" +
      String(toIso || "") +
      "\x1f" +
      String(asOfIso || "") +
      "\x1f" +
      String(layersKey || "-")
    );
  }

  function mergeHistoricalLmv2Zones(zoneLists) {
    var byKey = {};
    (zoneLists || []).forEach(function (zones) {
      (zones || []).forEach(function (z) {
        if (!z || z.namespace !== HISTORICAL_LMV2_NS) return;
        var baseId = String(z.id || z.pool_id || "");
        if (!baseId) return;
        var prev = byKey[baseId];
        if (!prev) {
          byKey[baseId] = z;
          return;
        }
        var gap = Number(z.start_timestamp) - Number(prev.end_timestamp);
        if (Number.isFinite(gap) && gap <= (HISTORICAL_LMV2_TF_SEC + 1)) {
          prev.end_timestamp = Math.max(Number(prev.end_timestamp), Number(z.end_timestamp));
          prev.start_timestamp = Math.min(Number(prev.start_timestamp), Number(z.start_timestamp));
          if (z.metadata && !z.metadata.right_censored) {
            if (!prev.metadata) prev.metadata = {};
            prev.metadata.right_censored = false;
          }
        } else {
          var altKey = baseId + "@" + String(z.start_timestamp);
          byKey[altKey] = z;
        }
      });
    });
    return Object.keys(byKey).map(function (k) { return byKey[k]; });
  }

  function touchHistoricalLmv2ChunkCache(cacheState, key, entry) {
    if (!cacheState || !key) return;
    if (!cacheState.chunks) cacheState.chunks = {};
    if (!cacheState.order) cacheState.order = [];
    cacheState.chunks[key] = entry;
    var order = cacheState.order.filter(function (k) { return k !== key; });
    order.push(key);
    while (order.length > HISTORICAL_LMV2_CHUNK_CACHE_MAX) {
      var drop = order.shift();
      if (drop && cacheState.chunks[drop]) delete cacheState.chunks[drop];
    }
    cacheState.order = order;
  }

  function historicalCloseUnixToIsoZ(unix) {
    var u = Math.floor(Number(unix));
    if (!Number.isFinite(u)) return "";
    return new Date(u * 1000).toISOString().replace(".000Z", "Z");
  }

  function lookupHistoricalLmv2ChunkCacheEntry(
    chunkStore,
    symbol,
    timeframe,
    fromCloseUnix,
    toCloseUnix,
    asOfIso,
    layersKey
  ) {
    var fromIso = historicalCloseUnixToIsoZ(fromCloseUnix);
    var toIso = historicalCloseUnixToIsoZ(toCloseUnix);
    var key = historicalLmv2ChunkCacheKey(symbol, timeframe, fromIso, toIso, asOfIso, layersKey);
    var entry = chunkStore && chunkStore.chunks ? chunkStore.chunks[key] : null;
    return { cache_key: key, entry: entry };
  }

  function gatherHistoricalLmv2ZonePartsFromPlannedChunks(
    chunkStore,
    symbol,
    timeframe,
    asOfIso,
    layersKey,
    chunks
  ) {
    var parts = [];
    var stats = { cache_hits: 0, cache_misses: 0, cache_errors: 0 };
    (chunks || []).forEach(function (ch) {
      if (!ch) return;
      var lu = lookupHistoricalLmv2ChunkCacheEntry(
        chunkStore,
        symbol,
        timeframe,
        ch.from_close_unix,
        ch.to_close_unix,
        asOfIso,
        layersKey
      );
      var entry = lu.entry;
      if (entry && entry.status === "ok" && entry.body) {
        parts.push(entry.body.zones || []);
        stats.cache_hits += 1;
      } else if (entry && entry.status === "error") {
        stats.cache_errors += 1;
      } else {
        stats.cache_misses += 1;
      }
    });
    return { zone_parts: parts, stats: stats };
  }

  function normalizeHistoricalLmv2ZoneForRender(zone) {
    if (!zone || zone.namespace !== HISTORICAL_LMV2_NS) return zone;
    var meta = zone.metadata || {};
    var asLive = normalizeLiveMicroLldV2ZoneForRender(
      Object.assign({}, zone, { namespace: LIVE_MICRO_LLD_V2_NS })
    );
    var border = "dotted";
    if (meta.layer === "remaining") {
      border = "dashed";
    }
    return Object.assign({}, asLive, {
      namespace: HISTORICAL_LMV2_NS,
      id: zone.id,
      border_style: border,
      metadata: Object.assign({}, meta, {
        visibility_mode: "RESEARCH_HISTORICAL",
        live_availability: "NOT_VERIFIED",
      }),
    });
  }

  function cullHistoricalZonesForRender(zones, pane) {
    var list = zones || [];
    var active = [];
    var remaining = [];
    list.forEach(function (z) {
      if (!z) return;
      if (z.metadata && z.metadata.layer === "remaining") remaining.push(z);
      else active.push(z);
    });
    var maxActive = LIVE_MICRO_LLD_V2_MAX_CHART_ZONES;
    if (active.length > maxActive) {
      active = active.slice(0, maxActive);
    }
    var remCap = Math.max(200, LIVE_MICRO_LLD_V2_MAX_CHART_ZONES * 2);
    if (remaining.length > 0 && remaining[0].metadata && remaining[0].metadata.visibility_mode === "RESEARCH_HISTORICAL") {
      remCap = 8000;
    }
    if (remaining.length > remCap) {
      remaining = remaining.slice(0, remCap);
      remaining.forEach(function (z) {
        if (!z.metadata) z.metadata = {};
        z.metadata.display_viewport_culled = true;
      });
    }
    return active.concat(remaining);
  }

  function applyHistoricalLmv2ToOverlays(
    pendingOverlays,
    zones,
    fromCloseUnix,
    toCloseUnix
  ) {
    var base = stripHistoricalLmv2Overlays(pendingOverlays || []);
    var clipped = clipZonesForWindow(zones, fromCloseUnix, toCloseUnix);
    var culled = cullHistoricalZonesForRender(clipped);
    var styled = (culled || []).map(normalizeHistoricalLmv2ZoneForRender);
    return base.concat(styled);
  }

  function historicalLmv2CacheKey(symbol, timeframe, fromIso, toIso, asOfIso, layersKey) {
    return (
      String(symbol || "") +
      "\x1f" +
      String(timeframe || "") +
      "\x1fhistorical_lmv2\x1f" +
      String(fromIso || "") +
      "\x1f" +
      String(toIso || "") +
      "\x1f" +
      String(asOfIso || "") +
      "\x1f" +
      String(layersKey || "-")
    );
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
    HISTORICAL_LMV2_NS: HISTORICAL_LMV2_NS,
    HISTORICAL_LMV2_PILOT_FROM_UNIX: HISTORICAL_LMV2_PILOT_FROM_UNIX,
    HISTORICAL_LMV2_PILOT_TO_UNIX: HISTORICAL_LMV2_PILOT_TO_UNIX,
    HISTORICAL_LMV2_MAX_CLOSES_PER_REQUEST: HISTORICAL_LMV2_MAX_CLOSES_PER_REQUEST,
    HISTORICAL_LMV2_CHUNK_PREFETCH: HISTORICAL_LMV2_CHUNK_PREFETCH,
    HISTORICAL_LMV2_CHUNK_CACHE_MAX: HISTORICAL_LMV2_CHUNK_CACHE_MAX,
    HISTORICAL_LMV2_PIPELINE_LINE: HISTORICAL_LMV2_PIPELINE_LINE,
    HISTORICAL_LMV2_PD_REGISTRY: HISTORICAL_LMV2_PD_REGISTRY,
    HISTORICAL_LMV2_SUPPORTED_PD: HISTORICAL_LMV2_SUPPORTED_PD,
    historicalLmv2PdRegistryEntry: historicalLmv2PdRegistryEntry,
    historicalLmv2CacheScopeKey: historicalLmv2CacheScopeKey,
    RESEARCH_STATIC_TIMELINE_IMPORT_FROM_ISO: RESEARCH_STATIC_TIMELINE_IMPORT_FROM_ISO,
    RESEARCH_STATIC_TIMELINE_IMPORT_TO_ISO: RESEARCH_STATIC_TIMELINE_IMPORT_TO_ISO,
    RESEARCH_STATIC_TIMELINE_FROM_UNIX: RESEARCH_STATIC_TIMELINE_FROM_UNIX,
    RESEARCH_STATIC_TIMELINE_TO_UNIX: RESEARCH_STATIC_TIMELINE_TO_UNIX,
    STATIC_TIMELINE_MAX_CLOSES: STATIC_TIMELINE_MAX_CLOSES,
    STATIC_TIMELINE_MAX_SPAN_UNIX: STATIC_TIMELINE_MAX_SPAN_UNIX,
    planStaticTimelineWindows: planStaticTimelineWindows,
    planStaticTimelineLoadForVisible: planStaticTimelineLoadForVisible,
    resolveStaticTimelineDesiredWindow: resolveStaticTimelineDesiredWindow,
    resolveStaticTimelineVisibleCloseRange: resolveStaticTimelineVisibleCloseRange,
    parseStaticTimelineSelectedRangeFromInputs: parseStaticTimelineSelectedRangeFromInputs,
    gatherStaticTimelineZoneParts: gatherStaticTimelineZoneParts,
    staticTimelineWindowCoverage: staticTimelineWindowCoverage,
    staticTimelineCacheKey: staticTimelineCacheKey,
    touchStaticTimelineCache: touchStaticTimelineCache,
    staticTimelineLoadCoverage: staticTimelineLoadCoverage,
    parseStaticTimelineUtcInput: parseStaticTimelineUtcInput,
    clampCloseUnixToStaticImport: clampCloseUnixToStaticImport,
    STATIC_TIMELINE_PREFETCH_WINDOWS: STATIC_TIMELINE_PREFETCH_WINDOWS,
    staticTimelinePreloadBundleKey: staticTimelinePreloadBundleKey,
    staticTimelineCloseCount: staticTimelineCloseCount,
    validateStaticTimelinePreloadRange: validateStaticTimelinePreloadRange,
    planStaticTimelinePreloadWindows: planStaticTimelinePreloadWindows,
    staticTimelinePinKeysFromWindows: staticTimelinePinKeysFromWindows,
    assessStaticTimelinePreloadBundle: assessStaticTimelinePreloadBundle,
    STATIC_TIMELINE_PRELOAD_READER_VERSION: STATIC_TIMELINE_PRELOAD_READER_VERSION,
    PHASE5P_PILOT_MAX_CLOSES: PHASE5P_PILOT_MAX_CLOSES,
    PHASE5P_PILOT_FROM_ISO: PHASE5P_PILOT_FROM_ISO,
    PHASE5P_PILOT_TO_ISO: PHASE5P_PILOT_TO_ISO,
    PHASE5M_STATIC_TIMELINE_FROM_ISO: PHASE5M_STATIC_TIMELINE_FROM_ISO,
    PHASE5M_STATIC_TIMELINE_TO_ISO: PHASE5M_STATIC_TIMELINE_TO_ISO,
    PHASE5M_STATIC_TIMELINE_FROM_UNIX: PHASE5M_STATIC_TIMELINE_FROM_UNIX,
    PHASE5M_STATIC_TIMELINE_TO_UNIX: PHASE5M_STATIC_TIMELINE_TO_UNIX,
    LIVE_MICRO_LLD_V2_CH_TEST_RUN_ID: LIVE_MICRO_LLD_V2_CH_TEST_RUN_ID,
    LIVE_MICRO_LLD_V2_CH_TEST_COV_FROM_UNIX: LIVE_MICRO_LLD_V2_CH_TEST_COV_FROM_UNIX,
    LIVE_MICRO_LLD_V2_CH_TEST_COV_TO_UNIX: LIVE_MICRO_LLD_V2_CH_TEST_COV_TO_UNIX,
    LIVE_MICRO_LLD_V2_TFS: LIVE_MICRO_LLD_V2_TFS,
    isScannerCommonOverlay: isScannerCommonOverlay,
    isScannerCommonTimelineOverlay: isScannerCommonTimelineOverlay,
    isCausalLldLandscapeOverlay: isCausalLldLandscapeOverlay,
    isLiveMicroLldV2Overlay: isLiveMicroLldV2Overlay,
    isLiveMicroLldV2ChTestOverlay: isLiveMicroLldV2ChTestOverlay,
    isHistoricalLmv2Overlay: isHistoricalLmv2Overlay,
    isScannerCommonFamilyOverlay: isScannerCommonFamilyOverlay,
    isResearchPoolLayerOverlay: isResearchPoolLayerOverlay,
    stripScannerCommonOverlays: stripScannerCommonOverlays,
    stripCausalLldLandscapeOverlays: stripCausalLldLandscapeOverlays,
    stripLiveMicroLldV2Overlays: stripLiveMicroLldV2Overlays,
    stripLiveMicroLldV2ChTestOverlays: stripLiveMicroLldV2ChTestOverlays,
    stripHistoricalLmv2Overlays: stripHistoricalLmv2Overlays,
    countHistoricalLmv2Overlays: countHistoricalLmv2Overlays,
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
    applyHistoricalLmv2ToOverlays: applyHistoricalLmv2ToOverlays,
    clipUnixToHistoricalPilot: clipUnixToHistoricalPilot,
    resolveHistoricalLmv2RequestWindow: resolveHistoricalLmv2RequestWindow,
    resolveHistoricalLmv2ViewportCloseWindow: resolveHistoricalLmv2ViewportCloseWindow,
    planHistoricalLmv2Chunks: planHistoricalLmv2Chunks,
    historicalLmv2CloseCoverage: historicalLmv2CloseCoverage,
    resolveHistoricalLmv2AsOfCloseUnix: resolveHistoricalLmv2AsOfCloseUnix,
    historicalDecisionCloseAtOrBefore: historicalDecisionCloseAtOrBefore,
    historicalLmv2CacheKey: historicalLmv2CacheKey,
    historicalLmv2ChunkCacheKey: historicalLmv2ChunkCacheKey,
    mergeHistoricalLmv2Zones: mergeHistoricalLmv2Zones,
    historicalCloseUnixToIsoZ: historicalCloseUnixToIsoZ,
    lookupHistoricalLmv2ChunkCacheEntry: lookupHistoricalLmv2ChunkCacheEntry,
    gatherHistoricalLmv2ZonePartsFromPlannedChunks: gatherHistoricalLmv2ZonePartsFromPlannedChunks,
    touchHistoricalLmv2ChunkCache: touchHistoricalLmv2ChunkCache,
    normalizeHistoricalLmv2ZoneForRender: normalizeHistoricalLmv2ZoneForRender,
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
