#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const jsPath = path.join(__dirname, "..", "chart_time_utc.js");
const code = fs.readFileSync(jsPath, "utf8");
const sandbox = { window: {}, globalThis: {} };
sandbox.globalThis = sandbox.window;
vm.runInNewContext(code, sandbox);
const fmt = sandbox.window.ResearchChartTimeUtc.formatCrosshairTimeUtc;

function assert(cond, msg) {
  if (!cond) {
    console.error("FAIL:", msg);
    process.exit(1);
  }
}

const t1 = Date.UTC(2026, 8, 1, 0, 0, 0) / 1000;
assert(fmt(t1) === "2026-09-01 00:00 UTC", "midnight UTC");

const t2 = Date.UTC(2026, 8, 1, 12, 35, 0) / 1000;
assert(fmt(t2) === "2026-09-01 12:35 UTC", "5m candle open");

const t3 = Date.UTC(2026, 7, 26, 15, 10, 0) / 1000;
assert(fmt(t3) === "2026-08-26 15:10 UTC", "reference minute");

assert(fmt("bad") === "", "invalid input");

console.log("OK test_chart_crosshair_time_utc");
