/**
 * CLI: node test_pd1_eod_density_render_parity_v1.js <pools.json> <timeframe>
 * 5m grid is authoritative; hourly sweep shortcut must NOT match for 1h pools.
 */
"use strict";
const fs = require("fs");
const vm = require("vm");
const path = require("path");

const poolsPath = process.argv[2];
const tf = process.argv[3] || "1h";
const adapterPath = path.join(__dirname, "static/js/research/pd1_exact_density_trp_adapter.js");
const code = fs.readFileSync(adapterPath, "utf8");
const pools = JSON.parse(fs.readFileSync(poolsPath, "utf8"));
if (!pools.length) {
  console.log(JSON.stringify({ parity: "SKIP", reason: "no pools" }));
  process.exit(0);
}
const t0 = Date.parse("2026-08-14T18:00:00Z");
const t1 = Date.parse("2026-08-15T18:00:00Z");
const yMin = Math.min(...pools.map((p) => p.bottom)) * 0.9995;
const yMax = Math.max(...pools.map((p) => p.top)) * 1.0005;
const slotMin = tf === "1h" ? 60 : tf === "5m" ? 5 : 15;
const opt = { slotMinutes: slotMin, sweepT0Ms: t0, sweepT1Ms: t1 };

function runAdapter(src) {
  const sandbox = { console, Math, Date, Number, Map, Set, parseInt, parseFloat, JSON };
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox);
  return sandbox.PD1ExactDensityTrpAdapter.poolsToExactOriginZones(pools, t0, t1, yMin, yMax, opt);
}

function sig(zones) {
  return zones
    .map((z) =>
      [
        z.side,
        z.start_timestamp,
        z.end_timestamp,
        z.bottom_price.toFixed(8),
        z.top_price.toFixed(8),
        z.metadata.depth,
      ].join("|")
    )
    .sort()
    .join("\n");
}

const z5mGrid = runAdapter(code);
const z5mGrid2 = runAdapter(code);
const stable = sig(z5mGrid) === sig(z5mGrid2);
const hourlyCode = code.replace(
  "for (let t = tStart; t <= loopEnd; t += STEP_MS)",
  "for (let t = tStart; t <= loopEnd; t += (slotMin >= 60 ? 3600000 : STEP_MS))"
);
const zHourly = runAdapter(hourlyCode);
let parity = stable && z5mGrid.length > 0;
if (slotMin >= 60) {
  parity = parity && sig(z5mGrid) !== sig(zHourly);
}
console.log(
  JSON.stringify({
    parity: parity ? "PASS" : "FAIL",
    timeframe: tf,
    zones: z5mGrid.length,
    slotMin: slotMin,
    hourlyShortcutMatches5mGrid: slotMin >= 60 ? sig(z5mGrid) === sig(zHourly) : null,
  })
);
process.exit(parity ? 0 : 1);
