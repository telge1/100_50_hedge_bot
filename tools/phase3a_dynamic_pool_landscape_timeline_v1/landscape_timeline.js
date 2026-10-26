"use strict";
/**
 * Phase 3A — causal 5m PD1 depth-2 landscape timeline (read-only).
 */
const {
  loadAdapter,
  buildDepth2Intervals,
  computeGaps,
  parseIsoMs,
  poolActiveAt,
} = require("../phase1b_depth2_landscape_pilot_v1/analyzer_depth2_landscape.js");
const { matchBand, priceIoU } = require("../phase2c_5m_pd1_depth2_candidate_pool_age_v1/pool_age_enricher.js");

const CONFIG_VERSION = "phase3a_dynamic_pool_landscape_timeline_v1";

function isoFromMs(ms) {
  return new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z");
}

function trackId(iv) {
  const o = (iv.origins || []).slice().sort().join("|");
  return `${iv.side}:${o || iv.bottom_price + "-" + iv.top_price}`;
}

function poolRow(iv, pools, asOfMs) {
  const origins = iv.origins || [];
  const activeOrigins = [];
  for (const pl of pools || []) {
    if (!origins.includes(pl.origin)) continue;
    activeOrigins.push({
      origin: pl.origin,
      active: poolActiveAt(pl, asOfMs),
      bottom: pl.bottom,
      top: pl.top,
    });
  }
  const widthPct =
    iv.bottom_price > 0
      ? ((iv.top_price - iv.bottom_price) / iv.bottom_price) * 100
      : 0;
  return {
    pool_track_id: trackId(iv),
    side: iv.side,
    bottom_price: iv.bottom_price,
    top_price: iv.top_price,
    depth: iv.depth,
    origins,
    pool_ids: iv.pool_ids || [],
    width_pct: widthPct,
    active_origins: activeOrigins,
    all_origins_active: activeOrigins.length
      ? activeOrigins.every((x) => x.active)
      : null,
  };
}

function matchPool(prev, curr) {
  const m = matchBand(
    {
      bottom_price: curr.bottom_price,
      top_price: curr.top_price,
      origins: curr.origins,
    },
    {
      upper_pool_lower: prev.bottom_price,
      upper_pool_upper: prev.top_price,
      upper_pool_origins: prev.origins,
    }
  );
  const iou = priceIoU(
    prev.bottom_price,
    prev.top_price,
    curr.bottom_price,
    curr.top_price
  );
  const shared = (prev.origins || []).filter((o) =>
    (curr.origins || []).includes(o)
  ).length;
  const ok =
    m.ok ||
    (iou >= 0.35 && shared >= 1) ||
    (prev.origins || []).join("|") === (curr.origins || []).join("|");
  const uncertain = ok && m.score < 0.45 && shared === 0;
  return { ok, score: m.score, iou, shared_origin_count: shared, uncertain };
}

function nearestPool(intervals, price, side) {
  const list = (intervals || []).filter((x) => x.side === side);
  let best = null;
  let bestD = Infinity;
  for (const iv of list) {
    const bot = Number(iv.bottom_price);
    const top = Number(iv.top_price);
    let d;
    if (price >= bot && price <= top) d = 0;
    else if (price > top) d = price - top;
    else d = bot - price;
    if (d < bestD) {
      bestD = d;
      best = iv;
    }
  }
  return best
    ? {
        bottom_price: best.bottom_price,
        top_price: best.top_price,
        depth: best.depth,
        distance_abs: bestD,
        distance_pct: (bestD / price) * 100,
        origins: best.origins,
      }
    : null;
}

function priceReachedPool(candle, pool) {
  if (!pool) return false;
  const bot = Number(pool.bottom_price);
  const top = Number(pool.top_price);
  return candle.h >= bot && candle.l <= top;
}

function emaAt(closes, period) {
  if (closes.length < period) return null;
  const k = 2 / (period + 1);
  let prev = closes.slice(0, period).reduce((a, b) => a + b, 0) / period;
  for (let i = period; i < closes.length; i++) {
    prev = closes[i] * k + prev * (1 - k);
  }
  return prev;
}

function emaSlopes(closes) {
  const periods = [9, 20, 59, 200];
  const out = {};
  for (const p of periods) {
    if (closes.length < p + 3) {
      out[`ema${p}`] = null;
      out[`ema${p}_slope_3bar_pct`] = null;
      continue;
    }
    const series = [];
    for (let end = p; end <= closes.length; end++) {
      series.push(emaAt(closes.slice(0, end), p));
    }
    const v0 = series[series.length - 3];
    const v1 = series[series.length - 1];
    out[`ema${p}`] = v1;
    out[`ema${p}_slope_3bar_pct`] =
      v0 && v1 ? ((v1 - v0) / v0) * 100 : null;
  }
  return out;
}

function diffPoolEvents(prev, curr, candle, reachedSet) {
  const events = [];
  const prevBySide = { upper: [], lower: [] };
  const currBySide = { upper: [], lower: [] };
  (prev || []).forEach((p) => prevBySide[p.side].push(p));
  (curr || []).forEach((p) => currBySide[p.side].push(p));

  for (const side of ["upper", "lower"]) {
    const usedPrev = new Set();
    for (const c of currBySide[side]) {
      let best = null;
      for (const p of prevBySide[side]) {
        if (usedPrev.has(p.pool_track_id)) continue;
        const m = matchPool(p, c);
        if (!m.ok) continue;
        if (!best || m.score > best.m.score) best = { p, m };
      }
      if (!best) {
        events.push({
          event_type: "POOL_NEW",
          side,
          pool_track_id: c.pool_track_id,
          bottom_price: c.bottom_price,
          top_price: c.top_price,
          depth: c.depth,
          origins: c.origins,
          identity_confidence: "HIGH",
        });
      } else {
        usedPrev.add(best.p.pool_track_id);
        const p = best.p;
        const types = ["POOL_CONTINUED"];
        if (
          c.bottom_price < p.bottom_price - 1e-12 ||
          c.top_price > p.top_price + 1e-12
        ) {
          types.push("POOL_EXPANDED");
        }
        if (
          c.bottom_price > p.bottom_price + 1e-12 ||
          c.top_price < p.top_price - 1e-12
        ) {
          types.push("POOL_CONTRACTED");
        }
        if (c.depth > p.depth) types.push("POOL_DENSITY_INCREASED");
        if (c.depth < p.depth) types.push("POOL_DENSITY_DECREASED");
        for (const et of types) {
          events.push({
            event_type: et,
            side,
            pool_track_id: c.pool_track_id,
            prev_pool_track_id: p.pool_track_id,
            bottom_price: c.bottom_price,
            top_price: c.top_price,
            prev_bottom_price: p.bottom_price,
            prev_top_price: p.top_price,
            depth: c.depth,
            prev_depth: p.depth,
            origins: c.origins,
            identity_confidence: best.m.uncertain ? "NOT_VERIFIED" : "HIGH",
            match_score: best.m.score,
          });
        }
      }
      const key = c.pool_track_id;
      if (priceReachedPool(candle, c) && !reachedSet.has(key)) {
        reachedSet.add(key);
        events.push({
          event_type: "POOL_REACHED_BY_PRICE",
          side,
          pool_track_id: key,
          bottom_price: c.bottom_price,
          top_price: c.top_price,
          depth: c.depth,
          origins: c.origins,
          candle_close: candle.c,
          candle_high: candle.h,
          candle_low: candle.l,
        });
      }
    }
    for (const p of prevBySide[side]) {
      if (usedPrev.has(p.pool_track_id)) continue;
      events.push({
        event_type: "POOL_DISAPPEARED",
        side,
        pool_track_id: p.pool_track_id,
        bottom_price: p.bottom_price,
        top_price: p.top_price,
        depth: p.depth,
        origins: p.origins,
        identity_confidence: "HIGH",
        note: "Absent from depth-2 display_intervals at causal as-of (not inferred from price)",
      });
    }
  }
  return events;
}

function buildTimeline(input) {
  const cfg = { ...(input.config || {}) };
  const slotMin = cfg.slot_minutes || 5;
  const largeGapPct = cfg.min_pool_gap_pct != null ? cfg.min_pool_gap_pct : 0.5;
  const highlightPct = cfg.large_gap_highlight_pct != null ? cfg.large_gap_highlight_pct : 1.0;
  const pools = input.pools || [];
  const prices = (input.prices || [])
    .slice()
    .sort((a, b) => parseIsoMs(a.t) - parseIsoMs(b.t));
  const visFrom = parseIsoMs(cfg.visible_from_utc);
  const visTo = parseIsoMs(cfg.visible_to_utc);
  const { AD } = loadAdapter(input.adapter_path);

  const closesByTime = new Map();
  prices.forEach((r) => closesByTime.set(r.t, Number(r.c)));

  const snapshots = [];
  const lifecycleEvents = [];
  const gapRows = [];
  let prevPools = [];
  const reachedSet = new Set();

  for (const row of prices) {
    const tMs = parseIsoMs(row.t);
    if (tMs < visFrom || tMs > visTo) continue;
    const asOf = row.t;
    const candle = {
      t: row.t,
      o: Number(row.o),
      h: Number(row.h),
      l: Number(row.l),
      c: Number(row.c),
    };
    const landscape = buildDepth2Intervals(AD, pools, asOf, slotMin);
    const intervals = landscape.display_intervals || [];
    const lowers = intervals.filter((x) => x.side === "lower");
    const uppers = intervals.filter((x) => x.side === "upper");
    const gapL = computeGaps(lowers, "lower", largeGapPct);
    const gapU = computeGaps(uppers, "upper", largeGapPct);

    const asOfMs = tMs;
    const poolRows = intervals.map((iv) => poolRow(iv, pools, asOfMs));
    const prevPoolRows = prevPools;

    const events = diffPoolEvents(prevPoolRows, poolRows, candle, reachedSet);
    events.forEach((ev) => {
      lifecycleEvents.push({
        ...ev,
        candle_close_utc: asOf,
        causal_as_of_utc: asOf,
      });
    });

    const allGaps = [...gapL.gaps, ...gapU.gaps];
    allGaps.forEach((g) => {
      gapRows.push({
        candle_close_utc: asOf,
        side: g.side,
        gap_bottom_price: g.gap_bottom_price,
        gap_top_price: g.gap_top_price,
        gap_pct: g.gap_pct,
        large_gap_ge_min: g.large_gap,
        large_gap_ge_1pct: g.gap_pct >= highlightPct,
        previous_union_bottom: g.previous_union_bottom,
        previous_union_top: g.previous_union_top,
        next_union_bottom: g.next_union_bottom,
        next_union_top: g.next_union_top,
      });
    });

    const closes = [];
    for (const r of prices) {
      if (parseIsoMs(r.t) <= tMs) closes.push(Number(r.c));
    }
    const ema = emaSlopes(closes);

    const snap = {
      version: CONFIG_VERSION,
      candle_close_utc: asOf,
      causal_as_of_utc: asOf,
      ohlc: candle,
      ema,
      nearest_upper: nearestPool(poolRows, candle.c, "upper"),
      nearest_lower: nearestPool(poolRows, candle.c, "lower"),
      upper_pools: poolRows.filter((p) => p.side === "upper"),
      lower_pools: poolRows.filter((p) => p.side === "lower"),
      upper_pool_count: uppers.length,
      lower_pool_count: lowers.length,
      gaps_upper: gapU.gaps,
      gaps_lower: gapL.gaps,
      parity_ok: landscape.parity?.ok,
      lifecycle_events_this_bar: events,
    };
    snapshots.push(snap);
    prevPools = poolRows;
  }

  return {
    config: cfg,
    adapter_path: input.adapter_path,
    snapshot_count: snapshots.length,
    snapshots,
    lifecycle_events: lifecycleEvents,
    gap_timeline: gapRows,
  };
}

if (require.main === module) {
  const chunks = [];
  process.stdin.on("data", (c) => chunks.push(c));
  process.stdin.on("end", () => {
    try {
      const input = JSON.parse(Buffer.concat(chunks).toString("utf8"));
      process.stdout.write(JSON.stringify(buildTimeline(input)));
    } catch (e) {
      process.stderr.write(String(e.stack || e));
      process.exit(1);
    }
  });
}

module.exports = { buildTimeline, trackId, nearestPool, CONFIG_VERSION };
