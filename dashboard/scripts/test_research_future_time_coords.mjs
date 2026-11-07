#!/usr/bin/env node
/**
 * Regression: future bar indices map to UTC beyond last candle (no clamp to last bar, not epoch 0).
 * Logic mirrors chart.js unixFromLogical / logicalFromUnix / seriesBarSec.
 */
import assert from "node:assert/strict";

function estimateBarSec(candles) {
  if (candles.length < 2) return 60;
  const d = Number(candles[candles.length - 1].time) - Number(candles[candles.length - 2].time);
  return Number.isFinite(d) && d > 0 ? d : 60;
}

function seriesBarSec(candles) {
  const sec = estimateBarSec(candles);
  return sec > 0 ? sec : 300;
}

function logicalFromUnix(unix, candles) {
  const t = Number(unix);
  const lastIdx = candles.length - 1;
  const barSec = seriesBarSec(candles);
  const firstT = Number(candles[0].time);
  const lastT = Number(candles[lastIdx].time);
  if (t >= lastT) return lastIdx + (t - lastT) / barSec;
  if (t <= firstT) return (t - firstT) / barSec;
  return null;
}

function unixFromLogical(logical, candles) {
  const L = Number(logical);
  const lastIdx = candles.length - 1;
  const barSec = seriesBarSec(candles);
  const firstT = Number(candles[0].time);
  const lastT = Number(candles[lastIdx].time);
  if (L >= lastIdx) return lastT + (L - lastIdx) * barSec;
  if (L <= 0) return firstT + L * barSec;
  return null;
}

const candles = [
  { time: 1000000 },
  { time: 1000300 },
  { time: 1000600 },
];
const barSec = seriesBarSec(candles);
assert.equal(barSec, 300);

const lastT = 1000600;
const future = lastT + 5 * 3600;
const logical = logicalFromUnix(future, candles);
assert.ok(logical > 2, "future logical beyond last index");
const back = unixFromLogical(logical, candles);
assert.equal(back, future, "round-trip future unix");

const oldClampWouldBe = lastT;
assert.notEqual(back, oldClampWouldBe, "must not clamp to last candle");

assert.ok(back > 1e6, "not unix epoch 0");

const logicalRight = candles.length - 1 + 60;
const t60 = unixFromLogical(logicalRight, candles);
assert.equal(t60, lastT + 60 * barSec, "60 bars into kerzenfreier Bereich");

console.log("test_research_future_time_coords.mjs: all assertions passed");
