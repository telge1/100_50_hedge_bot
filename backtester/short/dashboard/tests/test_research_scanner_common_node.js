/**
 * Offline tests for Research Scanner Pools (COMMON) helpers.
 */
"use strict";

const assert = require("node:assert/strict");
const path = require("path");
const h = require(path.join(
  __dirname,
  "..",
  "static",
  "js",
  "research",
  "research_scanner_common_helpers.js"
));

const TF_SEC_15 = 900;

function candle(open, opts) {
  const o = opts || {};
  return {
    time: open,
    open: o.open != null ? o.open : 1,
    high: o.high != null ? o.high : 2,
    low: o.low != null ? o.low : 0.5,
    close: o.close != null ? o.close : 1.5,
    volume: 10,
  };
}

function lastClosedBarTime(pane) {
  const step = TF_SEC_15;
  const now = Math.floor(Date.now() / 1000);
  const candles = (pane.pendingData && pane.pendingData.candles) || [];
  for (let i = candles.length - 1; i >= 0; i--) {
    const t = Number(candles[i].time);
    if (t + step <= now) return t;
  }
  return null;
}

function makePane(id, tf, candleList) {
  return {
    id,
    tf,
    pendingData: candleList ? { candles: candleList } : null,
  };
}

function test_visible_15m_pane0_not_hidden_pane2() {
  const now = Math.floor(Date.now() / 1000);
  const closedOpen = now - 3 * TF_SEC_15;
  const formingOpen = now - TF_SEC_15;
  const loaded = [
    candle(closedOpen - TF_SEC_15),
    candle(closedOpen),
    candle(formingOpen),
  ];
  const panes = {
    "pane-0": makePane("pane-0", "15m", loaded),
    "pane-2": makePane("pane-2", "15m", null),
  };
  const picked = h.selectPaneForScannerCommon(
    ["pane-0", "pane-1", "pane-2", "pane-3"],
    panes,
    ["pane-0"],
    lastClosedBarTime
  );
  assert.strictEqual(picked.id, "pane-0");
}

function test_visible_pane1_15m_layout2() {
  const now = Math.floor(Date.now() / 1000);
  const closedOpen = now - 2 * TF_SEC_15;
  const loaded = [candle(closedOpen - TF_SEC_15), candle(closedOpen)];
  const panes = {
    "pane-0": makePane("pane-0", "1m", [candle(closedOpen)]),
    "pane-1": makePane("pane-1", "15m", loaded),
  };
  const picked = h.selectPaneForScannerCommon(
    ["pane-0", "pane-1", "pane-2", "pane-3"],
    panes,
    ["pane-0", "pane-1"],
    lastClosedBarTime
  );
  assert.strictEqual(picked.id, "pane-1");
}

function test_no_loaded_15m_returns_null() {
  const panes = {
    "pane-0": makePane("pane-0", "1m", [candle(1)]),
    "pane-2": makePane("pane-2", "15m", null),
  };
  const picked = h.selectPaneForScannerCommon(
    ["pane-0", "pane-1", "pane-2", "pane-3"],
    panes,
    ["pane-0"],
    lastClosedBarTime
  );
  assert.strictEqual(picked, null);
}

function test_rectangle_to_zone() {
  const rect = {
    id: "scanner-common:pool-1",
    namespace: "SCANNER_COMMON",
    type: "rectangle",
    points: [
      { time: 100, price: 0.023167 },
      { time: 200, price: 0.023239 },
    ],
    style: { color: "#1e88e5", fillColor: "#1e88e533", lineWidth: 2 },
    metadata: { pool_id: "pool-1", direction: "support", side: "lower" },
  };
  const zone = h.scannerCommonOverlayToZone(rect);
  assert.strictEqual(zone.type, "zone");
  assert.strictEqual(zone.namespace, "SCANNER_COMMON");
  assert.strictEqual(zone.start_timestamp, 100);
  assert.strictEqual(zone.end_timestamp, 200);
  assert.strictEqual(zone.bottom_price, 0.023167);
  assert.strictEqual(zone.top_price, 0.023239);
  assert.strictEqual(zone.metadata.pool_id, "pool-1");
  assert.strictEqual(zone.style.color, "#1e88e5");
}

function test_many_pools_convert() {
  const rects = [];
  for (let i = 0; i < 184; i += 1) {
    rects.push({
      id: "scanner-common:p" + i,
      namespace: "SCANNER_COMMON",
      type: "rectangle",
      points: [{ time: 1, price: 0.02 + i * 0.00001 }, { time: 2, price: 0.03 }],
      style: { color: "#fb8c00" },
      metadata: { pool_id: "p" + i },
    });
  }
  const zones = h.convertScannerCommonOverlaysForRender(rects);
  assert.strictEqual(zones.length, 184);
  assert.strictEqual(h.countRenderableScannerCommon(zones), 184);
}

function test_merge_pane_preserves_common() {
  const common = [
    {
      id: "scanner-common:x",
      namespace: "SCANNER_COMMON",
      type: "zone",
      start_timestamp: 1,
      end_timestamp: 2,
      top_price: 2,
      bottom_price: 1,
    },
  ];
  const lld = [{ id: "lld:1", namespace: "LLD", type: "zone", start_timestamp: 1 }];
  const merged = h.mergePaneOverlaysPreservingScannerCommon(lld, common, true);
  assert.strictEqual(merged.length, 2);
  assert.ok(merged.some((p) => p.id === "scanner-common:x"));
}

function test_strip_on_checkbox_off() {
  const list = [
    { id: "lld:1", namespace: "LLD" },
    { id: "scanner-common:a", namespace: "SCANNER_COMMON" },
  ];
  const stripped = h.stripScannerCommonOverlays(list);
  assert.strictEqual(stripped.length, 1);
  assert.strictEqual(stripped[0].id, "lld:1");
}

function test_replay_decision_normalizes_to_15m_close() {
  const t1145 = Math.floor(Date.parse("2026-09-23T11:45:00Z") / 1000);
  const t1200open = Math.floor(Date.parse("2026-09-23T12:00:00Z") / 1000);
  const t1210 = Math.floor(Date.parse("2026-09-23T12:10:00Z") / 1000);
  const pane = makePane("pane-0", "15m", [
    candle(t1145 - TF_SEC_15),
    candle(t1145),
    candle(t1200open),
  ]);
  const ctx = {
    pane,
    tfSec: TF_SEC_15,
    isHistoricalReplay: true,
    replayTargetTs: t1210,
    selectedUnix: null,
    historyBlocksLive: false,
    nowUnix: Math.floor(Date.parse("2026-10-07T10:00:00Z") / 1000),
  };
  const closeUnix = h.resolveScannerCommonDecisionCloseUnix(ctx);
  assert.strictEqual(closeUnix, t1145 + TF_SEC_15);
}

function test_live_uses_now_not_future_forming() {
  const now = Math.floor(Date.parse("2026-10-07T10:05:00Z") / 1000);
  const closedOpen = now - 2 * TF_SEC_15;
  const formingOpen = now - 300;
  const pane = makePane("pane-0", "15m", [
    candle(closedOpen - TF_SEC_15),
    candle(closedOpen),
    candle(formingOpen),
  ]);
  const ctx = {
    pane,
    tfSec: TF_SEC_15,
    isHistoricalReplay: false,
    replayTargetTs: null,
    historyBlocksLive: false,
    nowUnix: now,
  };
  const closeUnix = h.resolveScannerCommonDecisionCloseUnix(ctx);
  assert.strictEqual(closeUnix, closedOpen + TF_SEC_15);
}

function test_poll_skipped_in_replay() {
  assert.strictEqual(
    h.shouldScheduleScannerCommonPollRefresh({
      isHistoricalReplay: true,
      checkboxEnabled: true,
      paneTf: "15m",
      isCommonTargetPane: true,
      hasLoadedCandles: true,
      prevClosedOpen: 1,
      nextClosedOpen: 2,
    }),
    false
  );
}

function test_cache_key_stable() {
  const k1 = h.scannerCommonCacheKey("TUTUSDT", "15m", "2026-09-23T12:00:00+00:00", 1);
  const k2 = h.scannerCommonCacheKey("TUTUSDT", "15m", "2026-09-23T12:00:00+00:00", 1);
  assert.strictEqual(k1, k2);
  assert.notStrictEqual(
    k1,
    h.scannerCommonCacheKey("TUTUSDT", "15m", "2026-09-24T12:00:00+00:00", 1)
  );
}

function test_poll_no_refresh_without_closed_bar_change() {
  assert.strictEqual(
    h.shouldScheduleScannerCommonPollRefresh({
      checkboxEnabled: true,
      paneTf: "15m",
      isCommonTargetPane: true,
      hasLoadedCandles: true,
      prevClosedOpen: 1000,
      nextClosedOpen: 1000,
    }),
    false
  );
}

function test_poll_refresh_on_new_closed_bar() {
  assert.strictEqual(
    h.shouldScheduleScannerCommonPollRefresh({
      checkboxEnabled: true,
      paneTf: "15m",
      isCommonTargetPane: true,
      hasLoadedCandles: true,
      prevClosedOpen: 1000,
      nextClosedOpen: 1900,
    }),
    true
  );
}

function test_poll_no_refresh_checkbox_off() {
  assert.strictEqual(
    h.shouldScheduleScannerCommonPollRefresh({
      checkboxEnabled: false,
      paneTf: "15m",
      isCommonTargetPane: true,
      hasLoadedCandles: true,
      prevClosedOpen: 1000,
      nextClosedOpen: 1900,
    }),
    false
  );
}

function test_poll_no_refresh_wrong_tf() {
  assert.strictEqual(
    h.shouldScheduleScannerCommonPollRefresh({
      checkboxEnabled: true,
      paneTf: "5m",
      isCommonTargetPane: true,
      hasLoadedCandles: true,
      prevClosedOpen: 1000,
      nextClosedOpen: 1900,
    }),
    false
  );
}

function test_coalesce_single_schedule_per_closed_open() {
  let bucket = {};
  const a = h.coalesceScannerCommonPollSchedule(bucket, "pane-0", 2000);
  assert.strictEqual(a.schedule, true);
  const b = h.coalesceScannerCommonPollSchedule(a.bucket, "pane-0", 2000);
  assert.strictEqual(b.schedule, false);
  assert.strictEqual(b.reason, "already_pending");
}

function test_coalesce_after_refresh_skips_same_bar() {
  let bucket = h.markScannerCommonPollRefreshed({}, 2000);
  const r = h.coalesceScannerCommonPollSchedule(bucket, "pane-0", 2000);
  assert.strictEqual(r.schedule, false);
  assert.strictEqual(r.reason, "already_refreshed");
}

function test_decision_close_from_closed_open() {
  const open = 1000;
  const close = open + TF_SEC_15;
  assert.strictEqual(close, open + 900);
}

function test_zone_end_matches_decision_close_unix() {
  const decisionClose = 2000;
  const rect = {
    namespace: "SCANNER_COMMON",
    type: "rectangle",
    points: [{ time: 100, price: 1 }, { time: decisionClose, price: 2 }],
    style: { color: "#1e88e5" },
    metadata: { pool_id: "p1" },
  };
  const zone = h.scannerCommonOverlayToZone(rect);
  assert.strictEqual(zone.end_timestamp, decisionClose);
}

const TIMELINE_NS = h.SCANNER_COMMON_TIMELINE_NS;

function tsUtc(iso) {
  return Math.floor(Date.parse(iso) / 1000);
}

function timelineZone(id, startIso, endIso, prices) {
  const pr = prices || { lo: 0.024, hi: 0.026 };
  const start = tsUtc(startIso);
  const end = tsUtc(endIso);
  return {
    id: id,
    pool_id: id,
    segment_index: 0,
    namespace: TIMELINE_NS,
    type: "zone",
    start_timestamp: start,
    end_timestamp: end,
    lower_price: pr.lo,
    upper_price: pr.hi,
    bottom_price: pr.lo,
    top_price: pr.hi,
    metadata: { source: "scanner_common_timeline" },
  };
}

function sep2026Candles() {
  const start = tsUtc("2026-09-22T00:00:00Z");
  const out = [];
  for (let i = 0; i < 96 * 4; i += 1) {
    out.push(candle(start + i * TF_SEC_15));
  }
  return out;
}

function test_timeline_window_clip_full_and_partial() {
  const from = tsUtc("2026-09-23T12:00:00Z");
  const to = tsUtc("2026-09-24T12:00:00Z");
  const zone = timelineZone(
    "clip-full",
    "2026-09-22T00:15:00Z",
    "2026-09-24T23:45:00Z"
  );
  const clipped = h.clipTimelineZoneToWindow(zone, from, to);
  assert.ok(clipped);
  assert.strictEqual(clipped.start_timestamp, from);
  assert.strictEqual(clipped.end_timestamp, to);

  const left = timelineZone(
    "clip-left",
    "2026-09-22T00:15:00Z",
    "2026-09-23T18:00:00Z"
  );
  const clipLeft = h.clipTimelineZoneToWindow(left, from, to);
  assert.ok(clipLeft);
  assert.strictEqual(clipLeft.start_timestamp, from);
  assert.strictEqual(clipLeft.end_timestamp, left.end_timestamp);

  const right = timelineZone(
    "clip-right",
    "2026-09-24T06:00:00Z",
    "2026-09-24T23:45:00Z"
  );
  const clipRight = h.clipTimelineZoneToWindow(right, from, to);
  assert.ok(clipRight);
  assert.strictEqual(clipRight.start_timestamp, right.start_timestamp);
  assert.strictEqual(clipRight.end_timestamp, to);
}

function test_timeline_outside_window_not_rendered() {
  const from = tsUtc("2026-09-23T12:00:00Z");
  const to = tsUtc("2026-09-24T12:00:00Z");
  const before = timelineZone(
    "before",
    "2026-09-22T00:15:00Z",
    "2026-09-23T00:15:00Z"
  );
  assert.strictEqual(h.clipTimelineZoneToWindow(before, from, to), null);
  assert.strictEqual(
    h.prepareTimelineZonesForDisplay([before], from, to).length,
    0
  );

  const after = timelineZone(
    "after",
    "2026-09-24T18:00:00Z",
    "2026-09-24T23:45:00Z"
  );
  assert.strictEqual(h.clipTimelineZoneToWindow(after, from, to), null);
  assert.strictEqual(h.prepareTimelineZonesForDisplay([after], from, to).length, 0);
}

function test_timeline_window_switch_replaces_overlays() {
  const fromA = tsUtc("2026-09-22T00:15:00Z");
  const toA = tsUtc("2026-09-23T12:00:00Z");
  const fromB = tsUtc("2026-09-23T12:00:00Z");
  const toB = tsUtc("2026-09-24T23:45:00Z");
  const zoneA = timelineZone(
    "window-a-only",
    "2026-09-22T00:15:00Z",
    "2026-09-23T06:00:00Z"
  );
  const zoneB = timelineZone(
    "window-b-only",
    "2026-09-23T18:00:00Z",
    "2026-09-24T12:00:00Z"
  );
  let overlays = [{ id: "lld:1", namespace: "LLD", type: "zone" }];
  overlays = h.applyScannerCommonTimelineToOverlays(overlays, [zoneA], fromA, toA);
  assert.ok(overlays.some((p) => p.id === "window-a-only"));
  overlays = h.applyScannerCommonTimelineToOverlays(overlays, [zoneB], fromB, toB);
  assert.ok(!overlays.some((p) => p.id === "window-a-only"));
  assert.ok(overlays.some((p) => p.id === "window-b-only"));
  assert.strictEqual(h.countScannerCommonTimelineOverlays(overlays), 1);
  assert.strictEqual(h.uniqueOverlayIds(overlays).length, overlays.length);
}

function test_timeline_replay_shrink_caps_visible_end() {
  const pane = makePane("pane-0", "15m", sep2026Candles());
  const nowUnix = tsUtc("2026-10-07T10:00:00Z");
  const replayWide = tsUtc("2026-09-24T21:10:00Z");
  const replayNarrow = tsUtc("2026-09-23T12:10:00Z");
  const wideCtx = {
    pane,
    tfSec: TF_SEC_15,
    isHistoricalReplay: true,
    replayTargetTs: replayWide,
    historyBlocksLive: false,
    nowUnix,
  };
  const narrowCtx = Object.assign({}, wideCtx, { replayTargetTs: replayNarrow });
  const winWide = h.resolveScannerCommonTimelineWindow(wideCtx);
  const winNarrow = h.resolveScannerCommonTimelineWindow(narrowCtx);
  assert.ok(winWide.to_close_unix > winNarrow.to_close_unix);

  const zones = [
    timelineZone("long-seg", "2026-09-22T00:15:00Z", "2026-09-24T23:45:00Z"),
  ];
  const narrowDisplay = h.prepareTimelineZonesForDisplay(
    zones,
    winNarrow.from_close_unix,
    winNarrow.to_close_unix
  );
  assert.strictEqual(narrowDisplay.length, 1);
  assert.ok(narrowDisplay[0].end_timestamp <= winNarrow.to_close_unix);
  assert.ok(narrowDisplay[0].end_timestamp < winWide.to_close_unix);
}

function test_timeline_return_to_live_replaces_hist_window() {
  const pane = makePane("pane-0", "15m", sep2026Candles());
  const nowUnix = tsUtc("2026-10-07T10:05:00Z");
  const histCtx = {
    pane,
    tfSec: TF_SEC_15,
    isHistoricalReplay: true,
    replayTargetTs: tsUtc("2026-09-23T12:10:00Z"),
    historyBlocksLive: true,
    nowUnix,
  };
  const liveCtx = {
    pane,
    tfSec: TF_SEC_15,
    isHistoricalReplay: false,
    replayTargetTs: null,
    historyBlocksLive: false,
    nowUnix,
  };
  const winHist = h.resolveScannerCommonTimelineWindow(histCtx);
  const winLive = h.resolveScannerCommonTimelineWindow(liveCtx);
  assert.ok(winLive.to_close_unix > winHist.to_close_unix);

  const zones = [
    timelineZone("live-replace", "2026-09-22T00:15:00Z", "2026-09-24T23:45:00Z"),
  ];
  let overlays = h.applyScannerCommonTimelineToOverlays(
    [],
    zones,
    winHist.from_close_unix,
    winHist.to_close_unix
  );
  const histEnd = overlays.find((p) => p.id === "live-replace").end_timestamp;
  overlays = h.applyScannerCommonTimelineToOverlays(
    overlays,
    zones,
    winLive.from_close_unix,
    winLive.to_close_unix
  );
  const liveZone = overlays.find((p) => p.id === "live-replace");
  assert.ok(liveZone);
  assert.ok(liveZone.end_timestamp <= winLive.to_close_unix);
  assert.ok(liveZone.end_timestamp >= histEnd);
  assert.strictEqual(h.countScannerCommonTimelineOverlays(overlays), 1);
}

function test_timeline_cache_hash_and_overlay_dedup() {
  const from = tsUtc("2026-09-23T00:15:00Z");
  const to = tsUtc("2026-09-24T12:00:00Z");
  const fromIso = "2026-09-23T00:15:00+00:00";
  const toIso = "2026-09-24T12:00:00+00:00";
  const k1 = h.scannerCommonTimelineCacheKey("TUTUSDT", "15m", fromIso, toIso, 1);
  const k2 = h.scannerCommonTimelineCacheKey("TUTUSDT", "15m", fromIso, toIso, 1);
  assert.strictEqual(k1, k2);

  const payloadZones = [
    timelineZone("dedup-a", "2026-09-23T06:00:00Z", "2026-09-23T18:00:00Z"),
    timelineZone("dedup-b", "2026-09-23T20:00:00Z", "2026-09-24T06:00:00Z"),
  ];
  const segmentsHash = "abc123segments";
  let overlays = [];
  overlays = h.applyScannerCommonTimelineToOverlays(overlays, payloadZones, from, to);
  overlays = h.applyScannerCommonTimelineToOverlays(overlays, payloadZones, from, to);
  assert.strictEqual(h.countScannerCommonTimelineOverlays(overlays), 2);
  assert.strictEqual(h.uniqueOverlayIds(overlays).length, overlays.length);

  const cache = {};
  cache[k1] = { segments_hash: segmentsHash, zones: payloadZones };
  const body1 = cache[k1];
  const body2 = cache[k2];
  assert.strictEqual(body1.segments_hash, body2.segments_hash);
  assert.strictEqual(body1.zones.length, body2.zones.length);
}

function test_forming_not_used_for_decision_iso_logic() {
  const now = Math.floor(Date.now() / 1000);
  const closedOpen = now - 2 * TF_SEC_15;
  const formingOpen = now - 300;
  const pane = makePane("pane-0", "15m", [
    candle(closedOpen - TF_SEC_15),
    candle(closedOpen),
    candle(formingOpen),
  ]);
  const closed = lastClosedBarTime(pane);
  assert.strictEqual(closed, closedOpen);
  assert.notStrictEqual(closed, formingOpen);
}

function v2Zone(side, strengthClass, meta) {
  return {
    id: "live-micro-v2:test",
    namespace: h.LIVE_MICRO_LLD_V2_NS,
    type: "zone",
    side: side,
    strength_class: strengthClass,
    start_timestamp: 1,
    end_timestamp: 2,
    top_price: 2,
    bottom_price: 1,
    style: { color: "#ef5350", fillColor: "#2196f333", lineWidth: 2 },
    metadata: meta || {},
  };
}

function test_live_micro_lld_v2_strength_fill_and_side_border() {
  const weakUpper = h.normalizeLiveMicroLldV2ZoneForRender(v2Zone("upper", "Weak"));
  assert.strictEqual(weakUpper.style.color, h.LIVE_MICRO_LLD_V2_STRENGTH_RGB.Weak);
  assert.strictEqual(weakUpper.border_color, h.liveMicroLldV2ZoneBorderColor("upper"));
  assert.strictEqual(weakUpper.opacity, 0.65);

  const weakLower = h.normalizeLiveMicroLldV2ZoneForRender(v2Zone("lower", "Weak"));
  assert.strictEqual(weakLower.style.color, weakUpper.style.color);
  assert.strictEqual(weakLower.border_color, h.liveMicroLldV2ZoneBorderColor("lower"));

  const strongLower = h.normalizeLiveMicroLldV2ZoneForRender(v2Zone("lower", "Strong"));
  assert.strictEqual(strongLower.style.color, h.LIVE_MICRO_LLD_V2_STRENGTH_RGB.Strong);
  assert.strictEqual(strongLower.border_color, h.liveMicroLldV2ZoneBorderColor("lower"));
  assert.strictEqual(strongLower.opacity, 0.75);

  ["Weak", "Medium", "Strong", "Very Strong", "Extreme"].forEach(function (sc) {
    const z = h.normalizeLiveMicroLldV2ZoneForRender(v2Zone("upper", sc));
    assert.strictEqual(z.style.color, h.LIVE_MICRO_LLD_V2_STRENGTH_RGB[sc]);
    assert.ok(z.opacity > 0 && z.opacity < 1);
  });

  const microLive = h.normalizeLiveMicroLldV2ZoneForRender(
    v2Zone("upper", "Medium", { pool_type: "micro", early_timing: 0 })
  );
  assert.strictEqual(microLive.opacity, 0.25);

  const microT1 = h.normalizeLiveMicroLldV2ZoneForRender(
    v2Zone("upper", "Medium", { pool_type: "micro", early_timing: 1 })
  );
  assert.strictEqual(microT1.opacity, 0.7);
}

function test_live_micro_lld_v2_apply_overlays_styles_zones() {
  const zones = [v2Zone("upper", "Extreme")];
  const out = h.applyLiveMicroLldV2ToOverlays([], zones, 0, 10);
  assert.strictEqual(out.length, 1);
  assert.strictEqual(out[0].style.color, h.LIVE_MICRO_LLD_V2_STRENGTH_RGB.Extreme);
  assert.strictEqual(out[0].border_color, h.liveMicroLldV2ZoneBorderColor("upper"));
}

function test_live_micro_lld_v2_window_capped_to_weeks() {
  const sec = 900;
  const toOpen = 1_700_000_000;
  const barsPerWeek = (7 * 86400) / sec;
  const fromOpen = toOpen - Math.floor(52 * barsPerWeek) * sec;
  const pane = {
    pendingData: {
      candles: [
        { time: fromOpen },
        { time: toOpen },
      ],
    },
  };
  const win = h.resolveLiveMicroLldV2CloseWindow(pane, sec, 12);
  assert.ok(win);
  assert.strictEqual(win.to_close_unix, toOpen + sec);
  const span = win.to_close_unix - win.from_close_unix;
  const want = 12 * 7 * 86400;
  assert.ok(Math.abs(span - want) <= sec);
}

function test_live_micro_lld_v2_strength_filter_class_names() {
  assert.strictEqual(h.normalizeLiveMicroLldV2StrengthClass("Weak"), "Weak");
  assert.strictEqual(h.normalizeLiveMicroLldV2StrengthClass("Very Strong"), "Very Strong");
  assert.strictEqual(h.normalizeLiveMicroLldV2StrengthClass("VS"), "Very Strong");
  assert.strictEqual(h.normalizeLiveMicroLldV2StrengthClass("Md"), "Medium");
}

function test_live_micro_lld_v2_replay_request_uses_decision_not_pane_end() {
  const t1145 = Math.floor(Date.parse("2026-09-23T11:45:00Z") / 1000);
  const t1200open = Math.floor(Date.parse("2026-09-23T12:00:00Z") / 1000);
  const t1210 = Math.floor(Date.parse("2026-09-23T12:10:00Z") / 1000);
  const paneEndOpen = Math.floor(Date.parse("2026-10-07T18:45:00Z") / 1000);
  const pane = makePane("pane-2", "15m", [
    candle(t1145 - TF_SEC_15),
    candle(t1145),
    candle(t1200open),
    candle(paneEndOpen),
  ]);
  const ctx = {
    pane,
    tfSec: TF_SEC_15,
    isHistoricalReplay: true,
    replayTargetTs: t1210,
    selectedUnix: null,
    historyBlocksLive: false,
    nowUnix: Math.floor(Date.parse("2026-10-07T19:00:00Z") / 1000),
  };
  const win = h.resolveLiveMicroLldV2RequestWindow(pane, TF_SEC_15, 12, ctx);
  assert.ok(win);
  assert.strictEqual(win.to_close_unix, t1145 + TF_SEC_15);
  assert.notStrictEqual(win.to_close_unix, paneEndOpen + TF_SEC_15);
}

function test_live_micro_lld_v2_live_request_uses_last_closed_bar() {
  const now = Math.floor(Date.parse("2026-10-07T10:05:00Z") / 1000);
  const closedOpen = now - 2 * TF_SEC_15;
  const formingOpen = now - 300;
  const pane = makePane("pane-2", "15m", [
    candle(closedOpen - TF_SEC_15),
    candle(closedOpen),
    candle(formingOpen),
  ]);
  const ctx = {
    pane,
    tfSec: TF_SEC_15,
    isHistoricalReplay: false,
    replayTargetTs: null,
    historyBlocksLive: false,
    nowUnix: now,
  };
  const win = h.resolveLiveMicroLldV2RequestWindow(pane, TF_SEC_15, 12, ctx);
  assert.ok(win);
  assert.strictEqual(win.to_close_unix, closedOpen + TF_SEC_15);
}

function test_micro_v2_zone_usable_at() {
  const T = 1_000_000;
  const z = {
    start_timestamp: T - 900,
    end_timestamp: T + 900,
  };
  assert.strictEqual(h.microV2ZoneUsableAt(z, T), true);
  assert.strictEqual(h.microV2ZoneUsableAt(z, T - 901), false);
  const broken = { start_timestamp: T - 3600, end_timestamp: T };
  assert.strictEqual(h.microV2ZoneUsableAt(broken, T), false);
  const future = { start_timestamp: T + 60, end_timestamp: T + 3600 };
  assert.strictEqual(h.microV2ZoneUsableAt(future, T), false);
}

function test_live_micro_lld_v2_ch_test_cache_isolated_from_v2() {
  const kV2 = h.liveMicroLldV2CacheKey("TUTUSDT", "5m", "a", "b", 10, "{}");
  const kCh = h.liveMicroLldV2ChTestCacheKey("TUTUSDT", "5m", "a", "b", 10, "{}");
  assert.notStrictEqual(kV2, kCh);
}

function test_live_micro_lld_v2_ch_test_overlay_namespace_and_dashed() {
  const zone = {
    namespace: h.LIVE_MICRO_LLD_V2_CH_TEST_NS,
    id: "live-micro-v2-ch-test:pid",
    side: "upper",
    strength_class: "Weak",
    start_timestamp: 1_100_000,
    end_timestamp: 1_500_000,
    bottom_price: 1,
    top_price: 2,
    style: { lineWidth: 2 },
  };
  const out = h.normalizeLiveMicroLldV2ChTestZoneForRender(zone);
  assert.strictEqual(out.namespace, h.LIVE_MICRO_LLD_V2_CH_TEST_NS);
  assert.strictEqual(out.border_style, "dashed");
  const merged = h.applyLiveMicroLldV2ChTestToOverlays(
    [Object.assign(v2Zone("lower", "Medium"), { namespace: h.LIVE_MICRO_LLD_V2_NS })],
    [zone],
    1_000_000,
    2_000_000
  );
  assert.strictEqual(h.countLiveMicroLldV2Overlays(merged), 1);
  assert.strictEqual(h.countLiveMicroLldV2ChTestOverlays(merged), 1);
}

function test_live_micro_lld_v2_ch_test_coverage_clip() {
  const inside = h.clipUnixToChTestCoverage(
    h.LIVE_MICRO_LLD_V2_CH_TEST_COV_FROM_UNIX + 3600,
    h.LIVE_MICRO_LLD_V2_CH_TEST_COV_TO_UNIX - 3600
  );
  assert.ok(inside);
  const clippedFull = h.clipUnixToChTestCoverage(
    h.LIVE_MICRO_LLD_V2_CH_TEST_COV_FROM_UNIX - 86400,
    h.LIVE_MICRO_LLD_V2_CH_TEST_COV_TO_UNIX + 86400
  );
  assert.ok(clippedFull);
  assert.strictEqual(clippedFull.from_close_unix, h.LIVE_MICRO_LLD_V2_CH_TEST_COV_FROM_UNIX);
  assert.strictEqual(clippedFull.to_close_unix, h.LIVE_MICRO_LLD_V2_CH_TEST_COV_TO_UNIX);
  const afterRun = h.clipUnixToChTestCoverage(
    h.LIVE_MICRO_LLD_V2_CH_TEST_COV_TO_UNIX + 3600,
    h.LIVE_MICRO_LLD_V2_CH_TEST_COV_TO_UNIX + 7200
  );
  assert.strictEqual(afterRun, null);
}

function test_live_micro_lld_v2_ch_test_comparison_window_uses_fixed_from() {
  const pane = {
    pendingData: { candles: [{ time: 1_789_000_000 }, { time: 1_789_100_000 }] },
  };
  const ctx = {
    pane: pane,
    tfSec: 300,
    isHistoricalReplay: true,
    replayTargetTs: h.LIVE_MICRO_LLD_V2_CH_TEST_COV_TO_UNIX - 3600,
    selectedUnix: null,
    historyBlocksLive: true,
    nowUnix: Math.floor(Date.now() / 1000),
  };
  const win = h.resolveLiveMicroLldV2ChTestComparisonWindow(ctx);
  assert.ok(win);
  assert.strictEqual(win.from_close_unix, h.LIVE_MICRO_LLD_V2_CH_TEST_COV_FROM_UNIX);
  assert.ok(win.to_close_unix <= h.LIVE_MICRO_LLD_V2_CH_TEST_COV_TO_UNIX);
}

function test_historical_lmv2_viewport_uses_timescale_before_first_candle() {
  const sec = TF_SEC_15;
  const scaleFrom = Math.floor(Date.parse("2026-09-26T00:00:00Z") / 1000);
  const scaleTo = Math.floor(Date.parse("2026-09-30T23:59:00Z") / 1000);
  const firstCandle = Math.floor(Date.parse("2026-09-28T00:00:00Z") / 1000);
  const lastCandle = Math.floor(Date.parse("2026-09-30T22:45:00Z") / 1000);
  const asOf = Math.floor(Date.parse("2026-09-30T23:45:00Z") / 1000);
  const ctx = {
    isHistoricalReplay: true,
    replayTargetTs: asOf,
    tfSec: sec,
    historyBlocksLive: true,
  };
  const chartApi = {
    getVisibleTimeRange: function () {
      return { from: scaleFrom, to: scaleTo, firstCandle: firstCandle, lastCandle: lastCandle };
    },
  };
  const vp = h.resolveHistoricalLmv2ViewportCloseWindow({}, sec, ctx, chartApi);
  assert.ok(vp);
  assert.strictEqual(vp.viewport_after_replay_cutoff, false);
  const sep26Close = Math.floor(Date.parse("2026-09-26T00:15:00Z") / 1000);
  assert.ok(vp.view_from_close_unix <= sep26Close);
  const chunks = h.planHistoricalLmv2Chunks(vp.view_from_close_unix, vp.view_to_close_unix, asOf, sec, 1);
  const cov = h.historicalLmv2CloseCoverage(
    chunks,
    vp.display_from_close_unix,
    Math.min(vp.display_to_close_unix, asOf),
    sec
  );
  assert.strictEqual(cov.complete, true);
  assert.strictEqual(vp.display_from_close_unix, vp.view_from_close_unix);
}

function test_historical_lmv2_viewport_replay_cutoff() {
  const sec = TF_SEC_15;
  const sepOpen = Math.floor(Date.parse("2026-09-29T00:00:00Z") / 1000);
  const augReplay = Math.floor(Date.parse("2026-08-26T12:00:00Z") / 1000);
  const ctx = {
    isHistoricalReplay: true,
    replayTargetTs: augReplay,
    tfSec: sec,
  };
  const chartApi = {
    getVisibleTimeRange: function () {
      return { firstCandle: sepOpen, lastCandle: sepOpen + 2 * sec };
    },
  };
  const vp = h.resolveHistoricalLmv2ViewportCloseWindow({}, sec, ctx, chartApi);
  assert.ok(vp);
  assert.strictEqual(vp.viewport_after_replay_cutoff, true);
  assert.ok(vp.as_of_close_unix < vp.view_from_close_unix);
}

function test_historical_lmv2_plan_chunks_sep_pan_sequence() {
  const sec = TF_SEC_15;
  const asOf = h.HISTORICAL_LMV2_PILOT_TO_UNIX;
  const sep20 = Math.floor(Date.parse("2026-09-20T00:00:00Z") / 1000);
  const sep19 = sep20 - 86400;
  const sep18 = sep19 - 86400;
  const c20 = h.planHistoricalLmv2Chunks(sep20, sep20 + 86400, asOf, sec, 1);
  const c19 = h.planHistoricalLmv2Chunks(sep19, sep19 + 86400, asOf, sec, 1);
  const c18 = h.planHistoricalLmv2Chunks(sep18, sep18 + 86400, asOf, sec, 1);
  assert.ok(c20.length >= 1);
  assert.ok(c19.length >= 1);
  assert.ok(c18.length >= 1);
  const mergedLists = [];
  [c20, c19, c18].forEach(function (chunks) {
    chunks.forEach(function (ch) {
      mergedLists.push([
        {
          namespace: h.HISTORICAL_LMV2_NS,
          id: "pool-" + ch.from_close_unix,
          pool_id: "pool-" + ch.from_close_unix,
          start_timestamp: ch.from_close_unix,
          end_timestamp: ch.to_close_unix,
        },
      ]);
    });
  });
  const merged = h.mergeHistoricalLmv2Zones(mergedLists);
  assert.ok(merged.length >= 3);
  const ids = {};
  merged.forEach(function (z) {
    ids[z.pool_id] = (ids[z.pool_id] || 0) + 1;
  });
  assert.ok(Object.keys(ids).length >= 3);
}

function test_historical_lmv2_merge_preserves_identity_across_chunks() {
  const sec = TF_SEC_15;
  const z1 = {
    namespace: h.HISTORICAL_LMV2_NS,
    id: "p1",
    pool_id: "p1",
    start_timestamp: 1_000_000,
    end_timestamp: 1_000_000 + sec * 10,
  };
  const z2 = {
    namespace: h.HISTORICAL_LMV2_NS,
    id: "p1",
    pool_id: "p1",
    start_timestamp: z1.end_timestamp,
    end_timestamp: 1_000_000 + sec * 20,
  };
  const merged = h.mergeHistoricalLmv2Zones([[z1], [z2]]);
  assert.strictEqual(merged.length, 1);
  assert.strictEqual(merged[0].pool_id, "p1");
  assert.strictEqual(merged[0].end_timestamp, z2.end_timestamp);
}

function test_historical_lmv2_chunk_cache_gather_and_lru() {
  const store = { chunks: {}, order: [] };
  const asOfIso = "2026-09-30T23:45:00Z";
  const layersKey = "A";
  const span = (h.HISTORICAL_LMV2_MAX_CLOSES_PER_REQUEST - 1) * h.HISTORICAL_LMV2_TF_SEC;
  let start = h.HISTORICAL_LMV2_PILOT_FROM_UNIX;
  const planned = [];
  for (let i = 0; i < h.HISTORICAL_LMV2_CHUNK_CACHE_MAX + 2; i += 1) {
    const ch = { from_close_unix: start, to_close_unix: start + span };
    planned.push(ch);
    const fromIso = h.historicalCloseUnixToIsoZ(ch.from_close_unix);
    const toIso = h.historicalCloseUnixToIsoZ(ch.to_close_unix);
    const key = h.historicalLmv2ChunkCacheKey("DOGEUSDT", "15m", fromIso, toIso, asOfIso, layersKey);
    h.touchHistoricalLmv2ChunkCache(store, key, {
      status: "ok",
      body: { zones: [{ namespace: h.HISTORICAL_LMV2_NS, id: "z" + i, start_timestamp: start, end_timestamp: start + span }] },
    });
    start += span + h.HISTORICAL_LMV2_TF_SEC;
  }
  assert.ok(Object.keys(store.chunks).length <= h.HISTORICAL_LMV2_CHUNK_CACHE_MAX);
  assert.ok(store.order.length <= h.HISTORICAL_LMV2_CHUNK_CACHE_MAX);
  const tail = planned[planned.length - 1];
  const g = h.gatherHistoricalLmv2ZonePartsFromPlannedChunks(
    store,
    "DOGEUSDT",
    "15m",
    asOfIso,
    layersKey,
    [tail]
  );
  assert.strictEqual(g.stats.cache_hits, 1);
  assert.strictEqual(g.zone_parts[0].length, 1);
}

function test_static_timeline_preload_7day_three_windows() {
  const from = Math.floor(Date.parse(h.PHASE5P_PILOT_FROM_ISO) / 1000);
  const to = Math.floor(Date.parse(h.PHASE5P_PILOT_TO_ISO) / 1000);
  const v = h.validateStaticTimelinePreloadRange(from, to);
  assert.strictEqual(v.ok, true);
  assert.strictEqual(v.close_count, h.PHASE5P_PILOT_MAX_CLOSES);
  const wins = h.planStaticTimelinePreloadWindows(from, to);
  assert.strictEqual(wins.length, 3);
  const cov = h.historicalLmv2CloseCoverage(wins, from, to, h.HISTORICAL_LMV2_TF_SEC);
  assert.strictEqual(cov.complete, true);
  assert.strictEqual(cov.required_count, 672);
}

function test_static_timeline_lazy_visible_not_full_selected() {
  const selFrom = Math.floor(Date.parse("2026-08-15T00:00:00Z") / 1000);
  const selTo = Math.floor(Date.parse("2026-09-17T23:45:00Z") / 1000);
  const visFrom = Math.floor(Date.parse("2026-08-15T00:00:00Z") / 1000);
  const visTo = Math.floor(Date.parse("2026-08-17T23:45:00Z") / 1000);
  const selected = { from_close_unix: selFrom, to_close_unix: selTo };
  const full = h.planStaticTimelineWindows(selFrom, selTo);
  const lazy = h.planStaticTimelineLoadForVisible(visFrom, visTo, selected, 1);
  assert.ok(full.length > lazy.windows.length);
  assert.ok(lazy.windows.length >= 1);
  assert.ok(lazy.windows.length <= 3);
}

function test_historical_lmv2_aug_sep_boundary_chunks() {
  const sec = TF_SEC_15;
  const asOf = h.HISTORICAL_LMV2_PILOT_TO_UNIX;
  const aug31 = Math.floor(Date.parse("2026-08-31T12:00:00Z") / 1000);
  const sep01 = Math.floor(Date.parse("2026-09-01T12:00:00Z") / 1000);
  const cAug = h.planHistoricalLmv2Chunks(aug31, aug31 + 86400, asOf, sec, 1);
  const cSep = h.planHistoricalLmv2Chunks(sep01, sep01 + 86400, asOf, sec, 1);
  assert.ok(cAug.length >= 1);
  assert.ok(cSep.length >= 1);
  assert.ok(cAug[0].from_close_unix >= h.HISTORICAL_LMV2_PILOT_FROM_UNIX);
  assert.ok(cSep[cSep.length - 1].to_close_unix <= asOf);
}

function test_live_micro_lld_v2_chart_viewport_filter() {
  const many = [];
  for (let i = 0; i < 1200; i += 1) {
    many.push(
      Object.assign(v2Zone("upper", "Weak"), {
        id: "live-micro-v2:test:" + i,
        start_timestamp: 1_000_000 + i * 100,
        end_timestamp: 1_000_500 + i * 100,
        bottom_price: 0.01 + i * 0.00001,
        top_price: 0.0105 + i * 0.00001,
      })
    );
  }
  const pane = { pendingData: { candles: [{ time: 1_050_000 }, { time: 1_060_000 }] } };
  const chartApi = {
    getVisibleTimeRange: function () {
      return { from: 1_055_000, to: 1_058_000 };
    },
    getVisiblePriceRange: function () {
      return { from: 0.015, to: 0.02 };
    },
  };
  const filtered = h.filterOverlaysForChartRender(many, pane, chartApi);
  assert.ok(filtered.length <= h.LIVE_MICRO_LLD_V2_MAX_CHART_ZONES);
  assert.ok(filtered.length < many.length);
  assert.ok(h.liveMicroLldV2NeedsViewportChartSync(many));
  assert.strictEqual(h.countLiveMicroLldV2Overlays(many), 1200);
}

const tests = [
  test_visible_15m_pane0_not_hidden_pane2,
  test_visible_pane1_15m_layout2,
  test_no_loaded_15m_returns_null,
  test_rectangle_to_zone,
  test_many_pools_convert,
  test_merge_pane_preserves_common,
  test_strip_on_checkbox_off,
  test_replay_decision_normalizes_to_15m_close,
  test_live_uses_now_not_future_forming,
  test_poll_skipped_in_replay,
  test_cache_key_stable,
  test_poll_no_refresh_without_closed_bar_change,
  test_poll_refresh_on_new_closed_bar,
  test_poll_no_refresh_checkbox_off,
  test_poll_no_refresh_wrong_tf,
  test_coalesce_single_schedule_per_closed_open,
  test_coalesce_after_refresh_skips_same_bar,
  test_decision_close_from_closed_open,
  test_zone_end_matches_decision_close_unix,
  test_forming_not_used_for_decision_iso_logic,
  test_timeline_window_clip_full_and_partial,
  test_timeline_outside_window_not_rendered,
  test_timeline_window_switch_replaces_overlays,
  test_timeline_replay_shrink_caps_visible_end,
  test_timeline_return_to_live_replaces_hist_window,
  test_timeline_cache_hash_and_overlay_dedup,
  test_live_micro_lld_v2_window_capped_to_weeks,
  test_live_micro_lld_v2_strength_fill_and_side_border,
  test_live_micro_lld_v2_apply_overlays_styles_zones,
  test_live_micro_lld_v2_replay_request_uses_decision_not_pane_end,
  test_live_micro_lld_v2_live_request_uses_last_closed_bar,
  test_micro_v2_zone_usable_at,
  test_live_micro_lld_v2_chart_viewport_filter,
  test_live_micro_lld_v2_strength_filter_class_names,
  test_live_micro_lld_v2_ch_test_cache_isolated_from_v2,
  test_live_micro_lld_v2_ch_test_overlay_namespace_and_dashed,
  test_live_micro_lld_v2_ch_test_coverage_clip,
  test_live_micro_lld_v2_ch_test_comparison_window_uses_fixed_from,
  test_historical_lmv2_viewport_uses_timescale_before_first_candle,
  test_historical_lmv2_viewport_replay_cutoff,
  test_historical_lmv2_plan_chunks_sep_pan_sequence,
  test_historical_lmv2_merge_preserves_identity_across_chunks,
  test_historical_lmv2_chunk_cache_gather_and_lru,
  test_static_timeline_preload_7day_three_windows,
  test_static_timeline_lazy_visible_not_full_selected,
  test_historical_lmv2_aug_sep_boundary_chunks,
];

let failed = 0;
for (const t of tests) {
  try {
    t();
    console.log("ok", t.name);
  } catch (err) {
    failed += 1;
    console.error("FAIL", t.name, err);
  }
}
process.exit(failed ? 1 : 0);
