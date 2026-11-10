/**
 * Parity: store raw zones once (fixed sweep); cull-only path matches full recompute.
 */
"use strict";
const fs = require("fs");
const vm = require("vm");
const path = require("path");

const pools = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const code = fs.readFileSync(
  path.join(__dirname, "static/js/research/pd1_exact_density_trp_adapter.js"),
  "utf8"
);
const sandbox = { console, Math, Date, Number, Map, Set, parseInt, parseFloat, JSON, performance };
vm.createContext(sandbox);
vm.runInContext(code, sandbox);
const AD = sandbox.PD1ExactDensityTrpAdapter;

const pad = 2 * 3600 * 1000;
const hitFrom = Date.parse("2026-08-14T12:00:00Z");
const hitTo = Date.parse("2026-08-15T12:00:00Z");
const loadFrom = hitFrom - 36 * 3600000;
const loadTo = hitTo + 6 * 3600000 + 36 * 3600000;
const yMin = Math.min(...pools.map((p) => p.bottom)) * 0.9995;
const yMax = Math.max(...pools.map((p) => p.top)) * 1.0005;
const sweepOpt = {
  slotMinutes: 60,
  sweepT0Ms: hitFrom - pad,
  sweepT1Ms: hitTo + pad,
};

function sig(segs) {
  return (segs || [])
    .map((s) =>
      [
        s.side,
        s.start_timestamp,
        s.end_timestamp,
        s.bottom_price.toFixed(8),
        s.top_price.toFixed(8),
        s.metadata && s.metadata.depth,
      ].join("|")
    )
    .sort()
    .join("\n");
}

const rawZones = AD.poolsToExactOriginZones(pools, loadFrom, loadTo, yMin, yMax, sweepOpt);
const vpBounds = AD.computeViewportCullBounds(
  Math.floor(hitFrom / 1000),
  Math.floor(hitTo / 1000),
  yMin,
  yMax
);
const viaCache = AD.mergeExactOriginSegments(AD.cullZonesToViewport(rawZones, vpBounds));
const viaFull = AD.mergeExactOriginSegments(
  AD.cullZonesToViewport(
    AD.poolsToExactOriginZones(pools, loadFrom, loadTo, yMin, yMax, sweepOpt),
    vpBounds
  )
);
const parity = sig(viaCache) === sig(viaFull);
console.log(JSON.stringify({ parity: parity ? "PASS" : "FAIL", segments: viaCache.length }));
process.exit(parity ? 0 : 1);
