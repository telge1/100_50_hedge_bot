"use strict";
/**
 * Phase 1B — read-only Depth-2 landscape + gaps using production PD1ExactDensityTrpAdapter.
 * Origin enrichment is analyzer-only (does not change chart geometry).
 */
const fs = require("fs");
const vm = require("vm");
const path = require("path");

const DEFAULT_ADAPTER = path.resolve(
  __dirname,
  "../../../spread_recovery_hedge_short_dev/dashboard/static/js/research/pd1_exact_density_trp_adapter.js"
);
const FALLBACK_ADAPTER = path.resolve(
  __dirname,
  "../../backtester/short/dashboard/research_charts/live_micro_lld_v2_ch_test/historical_json_import/phase6xt_reuse_research_poc/pd1_exact_density_trp_adapter.js"
);

function loadAdapter(adapterPath) {
  let p = adapterPath || process.env.PHASE1B_DENSITY_ADAPTER || DEFAULT_ADAPTER;
  if (!fs.existsSync(p) && fs.existsSync(FALLBACK_ADAPTER)) p = FALLBACK_ADAPTER;
  const ctx = { window: {}, console, Date, performance: { now: () => 0 } };
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(p, "utf8"), ctx);
  const AD = ctx.window.PD1ExactDensityTrpAdapter;
  if (!AD) throw new Error("PD1ExactDensityTrpAdapter missing: " + p);
  return { AD, adapterPath: p };
}

function parseIsoMs(s) {
  return new Date(String(s).replace("Z", "+00:00")).getTime();
}

function poolActiveAt(pl, tMs) {
  for (const seg of pl.segments || []) {
    const a = parseIsoMs(seg[0]);
    const b = parseIsoMs(seg[1]);
    if (tMs >= a && tMs <= b) return true;
  }
  return false;
}

/** Mirror adapter poolActiveAt + sweep; attach origin set per band (forensic). */
function computeExactBandsForSideWithOrigins(pools, tMs, side, yMin, yMax) {
  const active = (pools || []).filter((pl) => poolActiveAt(pl, tMs) && pl.side === side);
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
  let prevOrigins = [];
  const depthNow = () => new Set(activePools.values()).size;
  const originsNow = () => Array.from(new Set(activePools.values())).sort();
  for (const ev of events) {
    if (prevPrice !== null && ev.price > prevPrice && prevDepth > 0) {
      bands.push({
        y0: prevPrice,
        y1: ev.price,
        depth: prevDepth,
        origins: prevOrigins.slice(),
      });
    }
    if (ev.delta > 0) activePools.set(ev.pid, ev.origin);
    else activePools.delete(ev.pid);
    prevDepth = depthNow();
    prevOrigins = originsNow();
    prevPrice = ev.price;
  }
  const merged = [];
  for (const b of bands) {
    const last = merged[merged.length - 1];
    // Match production adapter: merge adjacent equal depth only (not origin set).
    if (last && last.depth === b.depth && Math.abs(b.y0 - last.y1) < 1e-15) {
      last.y1 = b.y1;
      b.origins.forEach((o) => {
        if (last.origins.indexOf(o) < 0) last.origins.push(o);
      });
      last.origins.sort();
    } else {
      merged.push({ ...b, origins: b.origins.slice() });
    }
  }
  return merged;
}

function arraysEqual(a, b) {
  if (!a || !b || a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

function priceExtents(pools) {
  let yMin = Infinity;
  let yMax = -Infinity;
  (pools || []).forEach((p) => {
    if (p.bottom != null) yMin = Math.min(yMin, Number(p.bottom));
    if (p.top != null) yMax = Math.max(yMax, Number(p.top));
  });
  if (!Number.isFinite(yMin) || !Number.isFinite(yMax)) {
    return { yMin: 0, yMax: 1 };
  }
  return {
    yMin: yMin * 0.9995,
    yMax: yMax * 1.0005,
  };
}

function bandsToZones(AD, bands, side, t0s, t1s, slotMin) {
  const zones = [];
  bands.forEach((b, bi) => {
    const bucket = AD.densityBucket(b.depth);
    if (bucket < 0) return;
    zones.push({
      id: "phase1b-" + side + "-" + bi,
      type: "zone",
      namespace: AD.NS,
      side: side,
      start_timestamp: t0s,
      end_timestamp: t1s,
      top_price: b.y1,
      bottom_price: b.y0,
      opacity: AD.opacityForDepth(bucket),
      shape: "rectangle",
      metadata: {
        depth: b.depth,
        origins: b.origins,
        decision_close_ms: null,
      },
      style: { color: AD.fillRgb(side, bucket) },
      border_width: 0,
    });
  });
  return zones;
}

function chartGeometryAtAsOf(AD, pools, asOfMs, slotMinutes) {
  const ext = priceExtents(pools);
  const tFloor = Math.floor(asOfMs / (5 * 60 * 1000)) * (5 * 60 * 1000);
  const sweep0 = tFloor;
  const sweep1 = tFloor;
  const zones = AD.poolsToExactOriginZones(
    pools,
    sweep0,
    sweep1 + 5 * 60 * 1000,
    ext.yMin,
    ext.yMax,
    {
      sweepT0Ms: sweep0,
      sweepT1Ms: sweep1,
      slotMinutes: slotMinutes,
    }
  );
  const culled = zones;
  const merged = AD.mergeExactOriginSegments(culled);
  const depth1 = AD.filterSegmentsByMinOrigins(merged, 1);
  const depth2 = AD.filterSegmentsByMinOrigins(merged, 2);
  return { zonesRaw: zones, merged, depth1, depth2, ext, tFloor };
}

function enrichOriginsFromBands(band, pools, tMs, side) {
  const lo = band.bottom_price;
  const hi = band.top_price;
  const origins = new Set();
  const poolIds = [];
  (pools || []).forEach((pl) => {
    if (pl.side !== side || !poolActiveAt(pl, tMs)) return;
    const pb = Number(pl.bottom);
    const pt = Number(pl.top);
    if (pt < lo || pb > hi) return;
    if (pb <= hi && pt >= lo) {
      origins.add(pl.origin);
      poolIds.push(pl.pool_id);
    }
  });
  return {
    origins: Array.from(origins).sort(),
    pool_ids: poolIds.sort(),
  };
}

function buildDepth2Intervals(AD, pools, asOfIso, slotMinutes) {
  const asOfMs = parseIsoMs(asOfIso);
  const ext = priceExtents(pools);
  const dcMs = AD.causalDecisionCloseMs(asOfMs, slotMinutes);
  const t0s = Math.floor(asOfMs / 1000);
  const t1s = t0s + slotMinutes * 60;

  const chart = chartGeometryAtAsOf(AD, pools, asOfMs, slotMinutes);

  const forensic = { lower: [], upper: [] };
  ["lower", "upper"].forEach((side) => {
    const bands = computeExactBandsForSideWithOrigins(
      pools,
      asOfMs,
      side,
      ext.yMin,
      ext.yMax
    );
    bands.forEach((b, i) => {
      if (b.depth < 2) return;
      forensic[side].push({
        side,
        bottom_price: b.y0,
        top_price: b.y1,
        depth: b.depth,
        origins: b.origins,
        as_of_utc: asOfIso,
        source_timeframe: "5m",
        source_pool_detail: 1,
        band_index: i,
        provenance: "sweep_line_forensic",
      });
    });
  });

  const display = [];
  chart.depth2.forEach((z) => {
    const en = enrichOriginsFromBands(z, pools, asOfMs, z.side);
    display.push({
      side: z.side,
      bottom_price: z.bottom_price,
      top_price: z.top_price,
      depth: z.metadata.depth,
      origins: en.origins,
      pool_ids: en.pool_ids,
      start_timestamp: z.start_timestamp,
      end_timestamp: z.end_timestamp,
      as_of_utc: asOfIso,
      source_timeframe: "5m",
      source_pool_detail: 1,
      provenance: "chart_path_merge_filter_min2",
      metadata_origins_sweep: z.metadata.origins || null,
    });
  });

  return {
    as_of_utc: asOfIso,
    as_of_ms: asOfMs,
    decision_close_ms: dcMs,
    slot_minutes: slotMinutes,
    price_extents: ext,
    forensic_bands: forensic,
    chart_depth2_zones: chart.depth2,
    display_intervals: display,
    parity: compareParity(forensic, chart.depth2),
  };
}

function zoneKey(z) {
  return [
    z.side,
    z.bottom_price,
    z.top_price,
    (z.metadata && z.metadata.depth) || 0,
  ].join("|");
}

function compareParity(forensic, chartZones) {
  const fSet = new Map();
  ["lower", "upper"].forEach((side) => {
    (forensic[side] || []).forEach((b) => {
      fSet.set(
        [side, b.bottom_price, b.top_price, b.depth].join("|"),
        b
      );
    });
  });
  const cSet = new Map();
  (chartZones || []).forEach((z) => {
    cSet.set(zoneKey(z), z);
  });
  const missingInChart = [];
  fSet.forEach((v, k) => {
    if (!cSet.has(k)) missingInChart.push(k);
  });
  const missingInForensic = [];
  cSet.forEach((v, k) => {
    if (!fSet.has(k)) missingInForensic.push(k);
  });
  return {
    ok: missingInChart.length === 0 && missingInForensic.length === 0,
    forensic_count: fSet.size,
    chart_count: cSet.size,
    missing_in_chart: missingInChart,
    missing_in_forensic: missingInForensic,
  };
}

function unionIntervalsForGaps(intervals) {
  const sorted = (intervals || [])
    .slice()
    .sort((a, b) => a.bottom_price - b.bottom_price);
  const out = [];
  for (const iv of sorted) {
    const lo = Number(iv.bottom_price);
    const hi = Number(iv.top_price);
    if (!out.length) {
      out.push({
        bottom_price: lo,
        top_price: hi,
        origins: new Set(iv.origins || []),
      });
      continue;
    }
    const last = out[out.length - 1];
    if (lo <= last.top_price + 1e-15) {
      last.top_price = Math.max(last.top_price, hi);
      (iv.origins || []).forEach((o) => last.origins.add(o));
    } else {
      out.push({
        bottom_price: lo,
        top_price: hi,
        origins: new Set(iv.origins || []),
      });
    }
  }
  return out.map((u) => ({
    bottom_price: u.bottom_price,
    top_price: u.top_price,
    origins: Array.from(u.origins).sort(),
  }));
}

function computeGaps(intervals, side, largeGapPct) {
  const united = unionIntervalsForGaps(intervals);
  const gaps = [];
  for (let i = 0; i < united.length - 1; i++) {
    const prev = united[i];
    const next = united[i + 1];
    const gapLo = prev.top_price;
    const gapHi = next.bottom_price;
    if (gapHi <= gapLo + 1e-15) continue;
    const gapPct = ((gapHi - gapLo) / gapLo) * 100;
    gaps.push({
      side,
      gap_bottom_price: gapLo,
      gap_top_price: gapHi,
      gap_pct: gapPct,
      large_gap: gapPct >= largeGapPct,
      previous_union_bottom: prev.bottom_price,
      previous_union_top: prev.top_price,
      next_union_bottom: next.bottom_price,
      next_union_top: next.top_price,
      previous_origins: prev.origins,
      next_origins: next.origins,
      gap_contract:
        "gap_pct = (next_lower_edge - previous_upper_edge) / previous_upper_edge * 100",
    });
  }
  return { united_intervals: united, gaps };
}

function analyzeWindowPayload(payload, options) {
  const AD = options.AD;
  const asOfIso = options.as_of_utc;
  const slotMinutes = options.slot_minutes || 5;
  const largeGapPct = options.large_gap_pct != null ? options.large_gap_pct : 0.5;
  const refPrice = options.reference_price;

  const pools = payload.pools || [];
  const landscape = buildDepth2Intervals(AD, pools, asOfIso, slotMinutes);
  const intervals = landscape.display_intervals;

  const lowerIv = intervals.filter((x) => x.side === "lower");
  const upperIv = intervals.filter((x) => x.side === "upper");
  const gapLower = computeGaps(lowerIv, "lower", largeGapPct);
  const gapUpper = computeGaps(upperIv, "upper", largeGapPct);

  let ref = refPrice != null ? Number(refPrice) : null;
  if (ref == null && payload.prices && payload.prices.length) {
    const asMs = parseIsoMs(asOfIso);
    let best = null;
    let bestD = Infinity;
    payload.prices.forEach((row) => {
      const t = parseIsoMs(row.t);
      const d = Math.abs(t - asMs);
      if (d < bestD) {
        bestD = d;
        best = row;
      }
    });
    if (best) ref = Number(best.c);
  }

  function rankGaps(gapPack, side) {
    return gapPack.gaps
      .map((g) => ({
        ...g,
        distance_from_ref_bottom: ref != null ? ref - g.gap_top_price : null,
        distance_from_ref_top: ref != null ? g.gap_bottom_price - ref : null,
      }))
      .sort((a, b) => a.gap_bottom_price - b.gap_bottom_price);
  }

  return {
    config: {
      symbol: "DOGEUSDT",
      timeframe: "5m",
      pool_detail: 1,
      min_depth: 2,
      large_gap_threshold_pct: largeGapPct,
      as_of_utc: asOfIso,
    },
    window: payload.window || null,
    provenance: payload.provenance || null,
    reference_price: ref,
    landscape,
    gaps: {
      lower: rankGaps(gapLower, "lower"),
      upper: rankGaps(gapUpper, "upper"),
      united_lower: gapLower.united_intervals,
      united_upper: gapUpper.united_intervals,
    },
    depth1_visible_in_chart_path: (landscape.chart_depth2_zones || []).length === 0
      ? []
      : null,
  };
}

function runFromStdin() {
  const chunks = [];
  process.stdin.on("data", (c) => chunks.push(c));
  process.stdin.on("end", () => {
    try {
      const input = JSON.parse(Buffer.concat(chunks).toString("utf8"));
      const { AD, adapterPath } = loadAdapter(input.adapter_path);
      const out = analyzeWindowPayload(input.window_payload, {
        AD,
        as_of_utc: input.as_of_utc,
        slot_minutes: input.slot_minutes || 5,
        large_gap_pct: input.large_gap_pct != null ? input.large_gap_pct : 0.5,
        reference_price: input.reference_price,
      });
      out.adapter_path = adapterPath;
      process.stdout.write(JSON.stringify(out, null, 0));
    } catch (err) {
      process.stderr.write(String(err.stack || err));
      process.exit(1);
    }
  });
}

if (require.main === module) {
  runFromStdin();
}

module.exports = {
  loadAdapter,
  analyzeWindowPayload,
  buildDepth2Intervals,
  chartGeometryAtAsOf,
  computeGaps,
  computeExactBandsForSideWithOrigins,
  poolActiveAt,
  parseIsoMs,
  enrichOriginsFromBands,
};
