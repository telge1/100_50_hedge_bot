/** Parity: poolActiveLookup vs legacy poolActiveAt in full zone output. */
"use strict";
const fs = require("fs");
const vm = require("vm");
const path = require("path");
const poolsPath = process.argv[2];
const codeLegacy = fs
  .readFileSync(path.join(__dirname, "static/js/research/pd1_exact_density_trp_adapter.js"), "utf8")
  .replace("function buildPoolActiveLookup", "function buildPoolActiveLookup_DISABLED");
const codeCurrent = fs.readFileSync(
  path.join(__dirname, "static/js/research/pd1_exact_density_trp_adapter.js"),
  "utf8"
);
function run(src, pools) {
  const sandbox = { console, Math, Date, Number, Map, Set, parseInt, parseFloat, JSON };
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox);
  const AD = sandbox.PD1ExactDensityTrpAdapter;
  const t0 = Date.parse("2026-08-14T12:00:00Z");
  const t1 = Date.parse("2026-08-15T12:00:00Z");
  const yMin = Math.min(...pools.map((p) => p.bottom)) * 0.9995;
  const yMax = Math.max(...pools.map((p) => p.top)) * 1.0005;
  return AD.poolsToExactOriginZones(pools, t0, t1, yMin, yMax, {
    slotMinutes: 60,
    sweepT0Ms: t0,
    sweepT1Ms: t1,
  });
}
const pools = JSON.parse(fs.readFileSync(poolsPath, "utf8"));
function sig(z) {
  return z
    .map((x) =>
      [x.side, x.start_timestamp, x.end_timestamp, x.bottom_price, x.top_price, x.metadata.depth].join("|")
    )
    .sort()
    .join("\n");
}
// legacy: disable lookup branch by patching computeExactBands to not pass lookup
const legacyCode = codeCurrent.replace(
  "const activeLookup = buildPoolActiveLookup(pools);",
  "const activeLookup = null;"
);
const a = run(legacyCode, pools);
const b = run(codeCurrent, pools);
const ok = sig(a) === sig(b);
console.log(JSON.stringify({ parity: ok ? "PASS" : "FAIL", zones: a.length }));
process.exit(ok ? 0 : 1);
