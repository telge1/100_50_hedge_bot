"use strict";
/**
 * Merge-gap regression harness. Reads the production adapter.
 * The legacy join (end >= start - 300) exists only here as the pre-fix oracle.
 */
const fs = require("fs");
const vm = require("vm");
const crypto = require("crypto");

const ADAPTER =
  "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard/static/js/research/pd1_exact_density_trp_adapter.js";

const ctx = { window: {}, console, Date, performance: { now: () => 0 } };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(ADAPTER, "utf8"), ctx);
const AD = ctx.window.PD1ExactDensityTrpAdapter;

const STEP_MS = 5 * 60 * 1000;
const SWEEP_PAD_MS = 2 * 3600 * 1000;

function floor5m(ms) {
  return Math.floor(ms / STEP_MS) * STEP_MS;
}
function ceil5m(ms) {
  return Math.ceil(ms / STEP_MS) * STEP_MS;
}

function legacyMerge(zones) {
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

function col(start, depth, width) {
  return {
    side: "lower",
    bottom_price: 0.1,
    top_price: 0.2,
    start_timestamp: start,
    end_timestamp: start + width,
    opacity: 0.35,
    style: { color: "#1a237e" },
    metadata: { depth: depth },
  };
}

function span(seg) {
  return {
    depth: seg.metadata.depth,
    start: seg.start_timestamp,
    end: seg.end_timestamp,
  };
}

function synthetic() {
  const gap5 = [col(0, 5, 300), col(300, 1, 300), col(600, 5, 300)];
  const prodMerged = AD.mergeExactOriginSegments(gap5);
  const prodMin3 = AD.filterSegmentsByMinOrigins(prodMerged, 3);
  const legacyMin3 = AD.filterSegmentsByMinOrigins(legacyMerge(gap5), 3);
  const touch = AD.mergeExactOriginSegments([col(0, 5, 300), col(300, 5, 300)]);
  const overlap = AD.mergeExactOriginSegments([col(0, 5, 400), col(300, 5, 300)]);
  const gap15 = [col(0, 5, 900), col(900, 1, 900), col(1800, 5, 900)];
  const prod15 = AD.filterSegmentsByMinOrigins(AD.mergeExactOriginSegments(gap15), 3);
  const legacy15 = AD.filterSegmentsByMinOrigins(legacyMerge(gap15), 3);
  return {
    five_m_gap_production_min3: prodMin3.map(span),
    five_m_gap_legacy_min3: legacyMin3.map(span),
    touching: touch.map(span),
    overlap: overlap.map(span),
    fifteen_m_gap_production_min3: prod15.map(span),
    fifteen_m_gap_legacy_min3: legacy15.map(span),
  };
}

function groupKey(z) {
  const d = z.metadata ? z.metadata.depth : 0;
  const c = z.style ? z.style.color : "";
  return [z.side, z.bottom_price, z.top_price, d, c, z.opacity].join("|");
}

function bridgeCount(zones) {
  const groups = new Map();
  for (const z of zones) {
    const k = groupKey(z);
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(z);
  }
  let bridge = 0;
  for (const arr of groups.values()) {
    arr.sort((a, b) => a.start_timestamp - b.start_timestamp);
    for (let i = 1; i < arr.length; i++) {
      const gap = arr[i].start_timestamp - arr[i - 1].end_timestamp;
      if (gap > 0 && gap <= 300) bridge += 1;
    }
  }
  return bridge;
}

function paintedHoles(merged, inputs) {
  const byKey = new Map();
  for (const z of inputs) {
    const k = groupKey(z);
    if (!byKey.has(k)) byKey.set(k, []);
    byKey.get(k).push(z);
  }
  let holes = 0;
  for (const seg of merged) {
    const pieces = (byKey.get(groupKey(seg)) || [])
      .filter(
        (z) => z.end_timestamp > seg.start_timestamp && z.start_timestamp < seg.end_timestamp
      )
      .sort((a, b) => a.start_timestamp - b.start_timestamp);
    if (!pieces.length || pieces[0].start_timestamp > seg.start_timestamp) {
      holes += 1;
      continue;
    }
    let cursor = pieces[0].end_timestamp;
    let hole = pieces[pieces.length - 1].end_timestamp < seg.end_timestamp;
    for (let i = 1; i < pieces.length; i++) {
      if (pieces[i].start_timestamp > cursor) hole = true;
      cursor = Math.max(cursor, pieces[i].end_timestamp);
    }
    if (hole) holes += 1;
  }
  return holes;
}

function geomKey(seg) {
  const d = seg.metadata && seg.metadata.depth != null ? seg.metadata.depth : 0;
  const c = (seg.style && seg.style.color) || "";
  return [seg.side, seg.start_timestamp, seg.end_timestamp, seg.bottom_price, seg.top_price, d, c, seg.opacity].join("|");
}

function fingerprint(segments) {
  const keys = segments.map(geomKey).sort();
  return crypto.createHash("sha256").update(keys.join("\n")).digest("hex");
}

function sideSplit(segments) {
  return {
    upper: segments.filter((s) => s.side === "upper").length,
    lower: segments.filter((s) => s.side === "lower").length,
  };
}

function filterTable(merged) {
  const mins = [1, 2, 3, 5, 10];
  const out = {};
  mins.forEach((n) => {
    const rows = AD.filterSegmentsByMinOrigins(merged, n);
    const split = sideSplit(rows);
    out[String(n)] = { n: rows.length, upper: split.upper, lower: split.lower, sha256: fingerprint(rows) };
  });
  return out;
}

function summarize(label, pools, slotMin, fromUtc, toUtc, loUtc, hiUtc) {
  const fromMs = Date.parse(fromUtc);
  const toMs = Date.parse(toUtc);
  const loMs = Date.parse(loUtc);
  const hiMs = Date.parse(hiUtc);
  let sweep0 = Math.max(floor5m(fromMs - SWEEP_PAD_MS), floor5m(loMs));
  let sweep1 = Math.min(ceil5m(toMs + SWEEP_PAD_MS), ceil5m(hiMs));
  const loadSpan = AD.computeDensityLoadSpanMs(fromMs, toMs, loMs, hiMs);
  const ext = pools.length
    ? {
        yMin: Math.min(...pools.map((p) => p.bottom)) * 0.9995,
        yMax: Math.max(...pools.map((p) => p.top)) * 1.0005,
      }
    : { yMin: 0, yMax: 1 };
  const raw = AD.poolsToExactOriginZones(pools, loadSpan.fromMs, loadSpan.toMs, ext.yMin, ext.yMax, {
    sweepT0Ms: sweep0,
    sweepT1Ms: sweep1,
    slotMinutes: slotMin,
  });
  const vp = AD.computeViewportCullBounds(fromMs / 1000, toMs / 1000, ext.yMin, ext.yMax);
  const culled = AD.cullZonesToViewport(raw, vp);
  const legacy = legacyMerge(culled);
  const production = AD.mergeExactOriginSegments(culled);
  const origins = new Set(pools.map((p) => p.origin).filter(Boolean));
  return {
    label: label,
    slot: slotMin,
    pools: pools.length,
    origins: origins.size,
    pools_missing_origin: pools.filter((p) => !p.origin).length,
    raw: raw.length,
    culled: culled.length,
    bridges: bridgeCount(culled),
    legacy: {
      merged: legacy.length,
      holes: paintedHoles(legacy, culled),
      sha256: fingerprint(legacy),
      filters: filterTable(legacy),
    },
    production: {
      merged: production.length,
      holes: paintedHoles(production, culled),
      sha256: fingerprint(production),
      filters: filterTable(production),
    },
  };
}

function keysOf(merged, minDepth) {
  return new Set(AD.filterSegmentsByMinOrigins(merged, minDepth).map(geomKey));
}

function pilot(data) {
  const fromUtc = data.from_utc;
  const toUtc = data.to_utc;
  const loUtc = "2026-09-09T00:00:00Z";
  const hiUtc = "2026-09-17T23:55:00Z";
  const singles = {};
  [1, 5, 10].forEach((pd) => {
    singles[pd] = summarize("5m_pd" + pd, data.pds[String(pd)].pools, 5, fromUtc, toUtc, loUtc, hiUtc);
  });
  const fromMs = Date.parse(fromUtc);
  const toMs = Date.parse(toUtc);
  const loMs = Date.parse(loUtc);
  const hiMs = Date.parse(hiUtc);
  let sweep0 = Math.max(floor5m(fromMs - SWEEP_PAD_MS), floor5m(loMs));
  let sweep1 = Math.min(ceil5m(toMs + SWEEP_PAD_MS), ceil5m(hiMs));
  const loadSpan = AD.computeDensityLoadSpanMs(fromMs, toMs, loMs, hiMs);
  const allPools = []
    .concat(data.pds["1"].pools, data.pds["5"].pools, data.pds["10"].pools);
  const ext = {
    yMin: Math.min(...allPools.map((p) => p.bottom)) * 0.9995,
    yMax: Math.max(...allPools.map((p) => p.top)) * 1.0005,
  };
  const perMerged = {};
  [1, 5, 10].forEach((pd) => {
    const pools = data.pds[String(pd)].pools;
    const raw = AD.poolsToExactOriginZones(pools, loadSpan.fromMs, loadSpan.toMs, ext.yMin, ext.yMax, {
      sweepT0Ms: sweep0,
      sweepT1Ms: sweep1,
      slotMinutes: 5,
    });
    const vp = AD.computeViewportCullBounds(fromMs / 1000, toMs / 1000, ext.yMin, ext.yMax);
    perMerged[pd] = AD.mergeExactOriginSegments(AD.cullZonesToViewport(raw, vp));
  });
  const concat = perMerged[1].concat(perMerged[5], perMerged[10]);
  const cross = AD.mergeExactOriginSegmentLists(perMerged[1], perMerged[5].concat(perMerged[10]));
  const inclusion = {};
  [1, 2, 3, 5, 10].forEach((minDepth) => {
    const multi = keysOf(concat, minDepth);
    const row = { multi: multi.size };
    [1, 5, 10].forEach((pd) => {
      const single = keysOf(perMerged[pd], minDepth);
      let missing = 0;
      single.forEach((k) => {
        if (!multi.has(k)) missing += 1;
      });
      row["pd" + pd] = { n: single.size, missing: missing };
    });
    inclusion[String(minDepth)] = row;
  });
  return {
    singles: singles,
    multi: {
      concat: concat.length,
      sum_singles: perMerged[1].length + perMerged[5].length + perMerged[10].length,
      cross_merge_count: cross.length,
      cross_merge_smaller: cross.length < concat.length,
      inclusion: inclusion,
      filters: filterTable(concat),
    },
  };
}

const mode = process.argv[2];
if (mode === "synthetic") {
  process.stdout.write(JSON.stringify(synthetic()));
} else if (mode === "pilot") {
  const data = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
  process.stdout.write(JSON.stringify(pilot(data)));
} else if (mode === "window") {
  const job = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
  process.stdout.write(
    JSON.stringify(
      summarize(job.label, job.pools, job.slot, job.from_utc, job.to_utc, job.lo_utc, job.hi_utc)
    )
  );
} else {
  throw new Error("mode");
}
