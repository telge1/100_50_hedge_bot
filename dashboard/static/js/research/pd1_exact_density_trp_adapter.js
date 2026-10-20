/**
 * Exact Origin density → research_trp zone overlays (render-only adapter).
 * Same band sweep as canvas landscape viewer; no SSOT / analyze_side changes.
 */
(function (root) {
  "use strict";

  const NS = "PD1_EXACT_ORIGIN_DENSITY_V1";
  const STEP_MS = 5 * 60 * 1000;
  const LOWER_RGB = ["#1a237e", "#3949ab", "#5c6bc0", "#7986cb", "#9fa8da"];
  const UPPER_RGB = ["#4a148c", "#6a1b9a", "#8e24aa", "#ab47bc", "#ce93d8"];

  function parseIso(s) {
    return new Date(String(s).replace("Z", "+00:00")).getTime();
  }

  function causalDecisionCloseMs(t) {
    const d = new Date(t);
    const slot = Math.floor(d.getUTCMinutes() / 15) * 15;
    return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), d.getUTCHours(), slot, 0, 0);
  }

  function poolActiveAt(pl, t) {
    for (const seg of pl.segments || []) {
      const a = parseIso(seg[0]);
      const b = parseIso(seg[1]);
      if (t >= a && t <= b) return true;
    }
    return false;
  }

  function computeExactBandsForSide(pools, t, side, yMin, yMax) {
    const active = (pools || []).filter((pl) => poolActiveAt(pl, t) && pl.side === side);
    if (!active.length) return [];
    const events = [];
    for (const pl of active) {
      const lo = Math.max(yMin, pl.bottom);
      const hi = Math.min(yMax, pl.top);
      if (hi < lo) continue;
      events.push({ price: lo, delta: 1, pid: pl.pool_id, origin: pl.origin });
      events.push({ price: hi, delta: -1, pid: pl.pool_id, origin: pl.origin });
    }
    if (!events.length) return [];
    events.sort((a, b) => (a.price === b.price ? b.delta - a.delta : a.price - b.price));
    const activePools = new Map();
    const bands = [];
    let prevPrice = null;
    let prevDepth = 0;
    const depthNow = () => new Set(activePools.values()).size;
    for (const ev of events) {
      if (prevPrice !== null && ev.price > prevPrice && prevDepth > 0) {
        bands.push({ y0: prevPrice, y1: ev.price, depth: prevDepth });
      }
      if (ev.delta > 0) activePools.set(ev.pid, ev.origin);
      else activePools.delete(ev.pid);
      prevDepth = depthNow();
      prevPrice = ev.price;
    }
    const merged = [];
    for (const b of bands) {
      const last = merged[merged.length - 1];
      if (last && last.depth === b.depth && Math.abs(b.y0 - last.y1) < 1e-15) last.y1 = b.y1;
      else merged.push({ ...b });
    }
    return merged;
  }

  function densityBucket(n) {
    if (n <= 0) return -1;
    if (n === 1) return 0;
    if (n <= 3) return 1;
    if (n <= 5) return 2;
    if (n <= 7) return 3;
    return 4;
  }

  function fillRgb(side, bucket) {
    const pal = side === "upper" ? UPPER_RGB : LOWER_RGB;
    return pal[Math.max(0, Math.min(4, bucket))];
  }

  function opacityForDepth(bucket) {
    return [0.35, 0.45, 0.55, 0.65, 0.78][Math.max(0, Math.min(4, bucket))];
  }

  /**
   * @param {object[]} pools landscape window pools
   * @param {number} viewT0Ms API/context lower bound (ms)
   * @param {number} viewT1Ms API/context upper bound (ms)
   * @param {number} yMin
   * @param {number} yMax
   * @param {{ sweepT0Ms?: number, sweepT1Ms?: number }} [opt] render sweep (visible + pad); defaults to full view span
   */
  function poolsToExactOriginZones(pools, viewT0Ms, viewT1Ms, yMin, yMax, opt) {
    const zones = [];
    const sweep0 = opt && opt.sweepT0Ms != null ? opt.sweepT0Ms : viewT0Ms;
    const sweep1 = opt && opt.sweepT1Ms != null ? opt.sweepT1Ms : viewT1Ms;
    const tStart = Math.floor(sweep0 / STEP_MS) * STEP_MS;
    const loopEnd = sweep1;
    const runs = [];
    let run = null;
    for (let t = tStart; t <= loopEnd; t += STEP_MS) {
      const dc = causalDecisionCloseMs(t);
      if (run && run.dc === dc) {
        run.tEnd = t;
      } else {
        if (run) runs.push(run);
        run = { dc, t0: t, tEnd: t };
      }
    }
    if (run) runs.push(run);
    runs.forEach((col, ci) => {
      const tDraw = col.t0;
      const tEndMs = col.tEnd + STEP_MS;
      const t0s = Math.floor(col.t0 / 1000);
      const t1s = Math.floor(tEndMs / 1000);
      ["lower", "upper"].forEach((side) => {
        const bands = computeExactBandsForSide(pools, tDraw, side, yMin, yMax);
        bands.forEach((b, bi) => {
          const bucket = densityBucket(b.depth);
          if (bucket < 0) return;
          const id = "pd1-eod-" + col.dc + "-" + side + "-" + ci + "-" + bi;
          const fracX = side === "lower" ? 0 : 0.5;
          zones.push({
            id: id,
            type: "zone",
            namespace: NS,
            side: side,
            start_timestamp: t0s,
            end_timestamp: t1s,
            top_price: b.y1,
            bottom_price: b.y0,
            opacity: opacityForDepth(bucket),
            shape: "rectangle",
            metadata: { depth: b.depth, decision_close_ms: col.dc, column_frac_x: fracX },
            style: { color: fillRgb(side, bucket) },
            border_width: 0,
          });
        });
      });
    });
    return zones;
  }

  function landscapePricesToCandles(prices) {
    return (prices || []).map((b) => ({
      time: Math.floor(parseIso(b.t) / 1000),
      open: b.o,
      high: b.h,
      low: b.l,
      close: b.c,
    }));
  }

  function zoneIntersectsViewport(z, tLo, tHi, pLo, pHi) {
    const zs = Number(z.start_timestamp);
    const ze = Number(z.end_timestamp != null ? z.end_timestamp : zs);
    const zb = Number(z.bottom_price);
    const zt = Number(z.top_price);
    if (![zs, ze, zb, zt].every(Number.isFinite)) return false;
    const t0 = Math.min(zs, ze);
    const t1 = Math.max(zs, ze);
    const p0 = Math.min(zb, zt);
    const p1 = Math.max(zb, zt);
    return t1 >= tLo && t0 <= tHi && p1 >= pLo && p0 <= pHi;
  }

  function computeViewportCullBounds(viewFromSec, viewToSec, yMin, yMax) {
    const tPad = Math.max(86400, (viewToSec - viewFromSec) * 0.08);
    const timeLo = viewFromSec - tPad;
    const timeHi = viewToSec + tPad;
    const pPad = Math.max((yMax - yMin) * 0.12, (yMax + yMin) * 0.0005);
    return {
      timeLo: timeLo,
      timeHi: timeHi,
      priceLo: yMin - pPad,
      priceHi: yMax + pPad,
    };
  }

  function cullZonesToViewport(zones, bounds) {
    if (!bounds) return zones || [];
    const tLo = bounds.timeLo;
    const tHi = bounds.timeHi;
    const pLo = bounds.priceLo;
    const pHi = bounds.priceHi;
    return (zones || []).filter((z) => zoneIntersectsViewport(z, tLo, tHi, pLo, pHi));
  }

  /** Lossless merge of time-adjacent identical bands (Option B / canvas path). */
  function mergeExactOriginSegments(zones) {
    const sorted = (zones || []).slice().sort((a, b) => {
      if (a.side !== b.side) return a.side < b.side ? -1 : 1;
      if (a.bottom_price !== b.bottom_price) return a.bottom_price - b.bottom_price;
      if (a.top_price !== b.top_price) return a.top_price - b.top_price;
      const da = (a.metadata && a.metadata.depth) || 0;
      const db = (b.metadata && b.metadata.depth) || 0;
      if (da !== db) return da - db;
      const ca = (a.style && a.style.color) || "";
      const cb = (b.style && b.style.color) || "";
      if (ca !== cb) return ca < cb ? -1 : 1;
      if (a.opacity !== b.opacity) return a.opacity - b.opacity;
      return a.start_timestamp - b.start_timestamp;
    });
    const out = [];
    for (const z of sorted) {
      const last = out[out.length - 1];
      const zDepth = (z.metadata && z.metadata.depth) || 0;
      const lDepth = last && last.metadata ? last.metadata.depth : 0;
      const zColor = (z.style && z.style.color) || "";
      const lColor = last && last.style ? last.style.color : "";
      if (
        last &&
        last.side === z.side &&
        last.bottom_price === z.bottom_price &&
        last.top_price === z.top_price &&
        lDepth === zDepth &&
        lColor === zColor &&
        last.opacity === z.opacity &&
        last.end_timestamp >= z.start_timestamp - 300
      ) {
        last.end_timestamp = Math.max(last.end_timestamp, z.end_timestamp);
      } else {
        out.push({
          ...z,
          metadata: z.metadata ? { ...z.metadata } : {},
          style: z.style ? { ...z.style } : {},
        });
      }
    }
    return out;
  }

  const EOD_CANVAS_MAX_SEGMENTS = 8000;
  const EOD_MAX_API_VISIBLE_MS = 72 * 3600 * 1000;
  /** Inclusive 5m endpoints per request (864 bars); naive 72h wall clock → 865 steps. */
  const EOD_MAX_5M_STEPS = 864;
  const EOD_MAX_API_CHUNK_MS = (EOD_MAX_5M_STEPS - 1) * STEP_MS;
  const EOD_API_TAIL_PAD_MS = 6 * 3600 * 1000;
  const EOD_WINDOW_CHUNK_CACHE_MAX = 32;
  const EOD_DEFAULT_PREFETCH_MS = 36 * 3600 * 1000;
  const EOD_FIXED_TILE_HOURS = 48;

  function floor5mMs(ms) {
    return Math.floor(ms / STEP_MS) * STEP_MS;
  }

  function ceil5mMs(ms) {
    return Math.ceil(ms / STEP_MS) * STEP_MS;
  }

  function exactOriginWindowCacheKey(fromMs, toMs) {
    return "pd1-eod-win\x1f" + String(fromMs) + "\x1f" + String(toMs);
  }

  function exactOriginFixedTileCacheKey(gridStartMs, tileHours) {
    return "pd1-eod-tile\x1f" + String(tileHours) + "h\x1f" + String(gridStartMs);
  }

  function exactOriginCacheKeyForWindow(w) {
    if (w && w.cacheKey) return w.cacheKey;
    return exactOriginWindowCacheKey(w.fromMs, w.toMs);
  }

  function visible5mStepsInclusive(fromMs, toMs) {
    return Math.floor((toMs - fromMs) / STEP_MS) + 1;
  }

  function fixedTileSpec(tileHours) {
    const tileMs = tileHours * 3600 * 1000;
    const steps = tileMs / STEP_MS;
    const lastOffset = (steps - 1) * STEP_MS;
    return { tileHours: tileHours, tileMs: tileMs, steps: steps, lastOffset: lastOffset };
  }

  function touchExactOriginWindowCache(store, key, entry) {
    if (!store || !key) return;
    if (!store.chunks) store.chunks = {};
    if (!store.order) store.order = [];
    store.chunks[key] = entry;
    const order = store.order.filter(function (k) {
      return k !== key;
    });
    order.push(key);
    while (order.length > EOD_WINDOW_CHUNK_CACHE_MAX) {
      const drop = order.shift();
      if (drop && store.chunks[drop]) delete store.chunks[drop];
    }
    store.order = order;
  }

  /** Sliding contiguous chunks (≤864 inclusive 5m steps each), 5m-aligned. */
  function planExactOriginApiWindows(loadFromMs, loadToMs, loMs, hiMs) {
    let from = floor5mMs(Math.max(loadFromMs, loMs));
    let to = ceil5mMs(Math.min(loadToMs, hiMs));
    if (from >= to) return [];
    const out = [];
    let cur = from;
    while (cur < to) {
      let end = Math.min(cur + EOD_MAX_API_CHUNK_MS, to);
      if (end <= cur) break;
      out.push({ fromMs: cur, toMs: end, planner: "sliding" });
      if (end >= to) break;
      cur = end;
    }
    return out;
  }

  /** UTC fixed tiles (default 48h) intersecting load span; stable grid cache keys. */
  function planExactOriginFixedTiles(loadFromMs, loadToMs, loMs, hiMs, tileHours) {
    const hours = tileHours == null ? EOD_FIXED_TILE_HOURS : tileHours;
    const spec = fixedTileSpec(hours);
    const grid = spec.tileMs;
    const spanFrom = floor5mMs(Math.max(loadFromMs, loMs));
    const spanTo = ceil5mMs(Math.min(loadToMs, hiMs));
    if (spanFrom >= spanTo) return [];
    const t0 = Math.floor(spanFrom / grid) * grid;
    const out = [];
    for (let start = t0; start < spanTo; start += grid) {
      const fromMs = Math.max(start, floor5mMs(loMs));
      const toMs = Math.min(start + spec.lastOffset, ceil5mMs(hiMs));
      if (fromMs > toMs) continue;
      if (visible5mStepsInclusive(fromMs, toMs) > EOD_MAX_5M_STEPS) continue;
      out.push({
        fromMs: fromMs,
        toMs: toMs,
        gridStartMs: start,
        tileHours: hours,
        planner: "fixed48h",
        cacheKey: exactOriginFixedTileCacheKey(start, hours),
      });
    }
    return out;
  }

  function planExactOriginLoadWindows(loadFromMs, loadToMs, loMs, hiMs, opt) {
    const mode = opt && opt.mode ? opt.mode : "sliding";
    if (mode === "fixed48h") {
      return planExactOriginFixedTiles(loadFromMs, loadToMs, loMs, hiMs, EOD_FIXED_TILE_HOURS);
    }
    return planExactOriginApiWindows(loadFromMs, loadToMs, loMs, hiMs);
  }

  function computeDensityLoadSpanMs(hitFromMs, hitToMs, loMs, hiMs, prefetchMs) {
    const pad = prefetchMs == null ? EOD_DEFAULT_PREFETCH_MS : prefetchMs;
    let from = floor5mMs(hitFromMs - pad);
    let to = ceil5mMs(hitToMs + EOD_API_TAIL_PAD_MS + pad);
    from = Math.max(floor5mMs(loMs), from);
    to = Math.min(ceil5mMs(hiMs), to);
    return { fromMs: from, toMs: to };
  }

  function windowOverlapsSpan(win, spanFromMs, spanToMs) {
    return win.toMs > spanFromMs && win.fromMs < spanToMs;
  }

  function mergeDensityPools(poolLists) {
    const byId = new Map();
    (poolLists || []).forEach(function (list) {
      (list || []).forEach(function (p) {
        if (!p || p.pool_id == null) return;
        const id = String(p.pool_id);
        const prev = byId.get(id);
        if (!prev) {
          byId.set(id, {
            ...p,
            segments: (p.segments || []).map(function (s) {
              return [s[0], s[1]];
            }),
          });
          return;
        }
        const sig = new Set(
          (prev.segments || []).map(function (s) {
            return String(s[0]) + "|" + String(s[1]);
          })
        );
        (p.segments || []).forEach(function (s) {
          const k = String(s[0]) + "|" + String(s[1]);
          if (!sig.has(k)) {
            prev.segments.push([s[0], s[1]]);
            sig.add(k);
          }
        });
        if (p.top > prev.top) prev.top = p.top;
        if (p.bottom < prev.bottom) prev.bottom = p.bottom;
      });
    });
    return Array.from(byId.values());
  }

  function mergeExactOriginSegmentLists(a, b) {
    return mergeExactOriginSegments((a || []).concat(b || []));
  }

  function exactOriginWindowsFullyCached(store, planned) {
    if (!store || !planned || !planned.length) return false;
    return planned.every(function (w) {
      const key = exactOriginCacheKeyForWindow(w);
      const ent = store.chunks[key];
      return ent && ent.status === "ok" && ent.pools;
    });
  }

  root.PD1ExactDensityTrpAdapter = {
    NS: NS,
    EOD_CANVAS_MAX_SEGMENTS: EOD_CANVAS_MAX_SEGMENTS,
    poolsToExactOriginZones: poolsToExactOriginZones,
    landscapePricesToCandles: landscapePricesToCandles,
    causalDecisionCloseMs: causalDecisionCloseMs,
    zoneIntersectsViewport: zoneIntersectsViewport,
    computeViewportCullBounds: computeViewportCullBounds,
    cullZonesToViewport: cullZonesToViewport,
    mergeExactOriginSegments: mergeExactOriginSegments,
    mergeExactOriginSegmentLists: mergeExactOriginSegmentLists,
    planExactOriginApiWindows: planExactOriginApiWindows,
    planExactOriginFixedTiles: planExactOriginFixedTiles,
    planExactOriginLoadWindows: planExactOriginLoadWindows,
    computeDensityLoadSpanMs: computeDensityLoadSpanMs,
    exactOriginWindowCacheKey: exactOriginWindowCacheKey,
    exactOriginFixedTileCacheKey: exactOriginFixedTileCacheKey,
    exactOriginCacheKeyForWindow: exactOriginCacheKeyForWindow,
    visible5mStepsInclusive: visible5mStepsInclusive,
    EOD_MAX_5M_STEPS: EOD_MAX_5M_STEPS,
    EOD_MAX_API_CHUNK_MS: EOD_MAX_API_CHUNK_MS,
    EOD_FIXED_TILE_HOURS: EOD_FIXED_TILE_HOURS,
    touchExactOriginWindowCache: touchExactOriginWindowCache,
    mergeDensityPools: mergeDensityPools,
    windowOverlapsSpan: windowOverlapsSpan,
    exactOriginWindowsFullyCached: exactOriginWindowsFullyCached,
    EOD_MAX_API_VISIBLE_MS: EOD_MAX_API_VISIBLE_MS,
    EOD_DEFAULT_PREFETCH_MS: EOD_DEFAULT_PREFETCH_MS,
    EOD_WINDOW_CHUNK_CACHE_MAX: EOD_WINDOW_CHUNK_CACHE_MAX,
    densityBucket: densityBucket,
    fillRgb: fillRgb,
    opacityForDepth: opacityForDepth,
  };
})(typeof window !== "undefined" ? window : globalThis);
