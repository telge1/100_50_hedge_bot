#!/usr/bin/env node
/**
 * Regression tests for navPaneLoadRangeForFreeNav null-center guard (epoch-0 bug).
 * Logic mirrors patched research_charts.js — update if that function changes.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const jsPath = join(root, "static/js/research/research_charts.js");
const src = readFileSync(jsPath, "utf8");

assert.match(src, /function navPaneLoadRangeForFreeNav\(centerTs\)\s*\{[\s\S]*?if \(centerTs == null\) return \{ from: null, to: null \}/,
  "navPaneLoadRangeForFreeNav must reject null/undefined before Number()");
assert.match(src, /function gotoNavCandleLoadRange\(gotoTs, win\)\s*\{[\s\S]*?if \(gotoTs == null\) return \{ from: null, to: null \}/,
  "gotoNavCandleLoadRange must reject null before Number()");
assert.match(src, /if \(center == null\) \{\s*loadPane\(pid, \{ force: true, sourceAction: "tf-change" \}\)/,
  "TF change must fall back to standard loadPane when no GO-TO nav center");

const HISTORY_SPAN_DAYS = { "7d": 7, "30d": 30, "90d": 90 };

function historySpanSeconds(history) {
  const p = history.preset || "30d";
  if (p === "custom") {
    if (history.customFrom != null && history.customTo != null && history.customTo > history.customFrom) {
      return history.customTo - history.customFrom;
    }
  }
  const days = HISTORY_SPAN_DAYS[p];
  return (days || 30) * 86400;
}

function navPaneLoadRangeForFreeNav(centerTs, history, nowSec) {
  if (centerTs == null) return { from: null, to: null };
  const center = Math.floor(Number(centerTs));
  if (!Number.isFinite(center)) return { from: null, to: null };
  let spanSec = historySpanSeconds(history);
  if (!Number.isFinite(spanSec) || spanSec < 86400) spanSec = 30 * 86400;
  const half = Math.ceil(spanSec / 2);
  let from = center - half;
  let to = Math.min(center + half, nowSec);
  if (!Number.isFinite(from) || !Number.isFinite(to) || to <= from) {
    return { from: null, to: null };
  }
  return { from: Math.floor(from), to: Math.floor(to) };
}

function gotoNavCandleLoadRange(gotoTs) {
  if (gotoTs == null) return { from: null, to: null };
  const ts = Math.floor(Number(gotoTs));
  if (!Number.isFinite(ts)) return { from: null, to: null };
  const span = 45 * 86400;
  return { from: ts - span, to: ts + span };
}

const customHistory = {
  preset: "custom",
  customFrom: 0,
  customTo: 20041200,
};

// Bug window: null center + ~232d span used to yield ±10020600
for (const bad of [null, undefined]) {
  const r = navPaneLoadRangeForFreeNav(bad, customHistory, 1_800_000_000);
  assert.equal(r.from, null);
  assert.equal(r.to, null);
  assert.notEqual(r.from, -10020600);
}

assert.deepEqual(gotoNavCandleLoadRange(null), { from: null, to: null });
assert.deepEqual(gotoNavCandleLoadRange(undefined), { from: null, to: null });

const sep26_2026 = Math.floor(Date.parse("2026-09-26T12:00:00Z") / 1000);
const nowSec = Math.floor(Date.parse("2026-10-19T13:00:00Z") / 1000);
const ok = navPaneLoadRangeForFreeNav(sep26_2026, { preset: "30d" }, nowSec);
assert.ok(ok.from > 1_600_000_000, "from should be modern unix");
assert.ok(ok.to > 1_600_000_000, "to should be modern unix");
assert.ok(ok.from < sep26_2026 && ok.to >= sep26_2026 - 86400);

const gotoOk = gotoNavCandleLoadRange(sep26_2026);
assert.ok(gotoOk.from > 1_600_000_000);

console.log("test_research_nav_epoch_fix.mjs: all assertions passed");
