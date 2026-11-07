#!/usr/bin/env node
/**
 * Regression: overlayFingerprintForSync must detect line selection/geometry changes.
 * Logic must match research_charts.js overlayFingerprintForSync.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const hostJs = join(root, "static/js/research/research_charts.js");
const src = readFileSync(hostJs, "utf8");
assert.match(src, /function overlayFingerprintForSync\(p\)/, "host must define overlayFingerprintForSync");
assert.match(
  src,
  /if \(p\.type === "line"\)[\s\S]*?meta\.selected[\s\S]*?p\.style\.width/,
  "line fingerprint must include selected and style.width"
);

function overlayFingerprintForSync(p) {
  if (!p) return "";
  if (p.type === "zone") {
    return [
      p.type || "",
      p.start_timestamp != null ? p.start_timestamp : "",
      p.end_timestamp != null ? p.end_timestamp : "",
      p.top_price != null ? p.top_price : "",
      p.bottom_price != null ? p.bottom_price : "",
      p.opacity != null ? p.opacity : "",
      p.border_color || "",
      (p.style && p.style.color) || "",
      p.visible === false ? "0" : "1",
    ].join("|");
  }
  if (p.type === "line") {
    const meta = p.metadata || {};
    const ext = p.extend_right || meta.extend_right ? "1" : "0";
    return [
      p.type || "",
      p.kind || "",
      p.start_timestamp != null ? p.start_timestamp : "",
      p.start_price != null ? p.start_price : "",
      p.end_timestamp != null ? p.end_timestamp : "",
      p.end_price != null ? p.end_price : "",
      p.timestamp != null ? p.timestamp : "",
      p.price != null ? p.price : "",
      meta.selected ? "1" : "0",
      ext,
      p.style && p.style.width != null ? p.style.width : "",
      (p.style && p.style.color) || "",
      p.visible === false ? "0" : "1",
    ].join("|");
  }
  return [
    p.type || "",
    p.shape || "",
    p.timestamp || "",
    p.price || "",
    p.text || "",
    (p.style && p.style.color) || "",
    p.visible === false ? "0" : "1",
  ].join("|");
}

function needsUpdate(prev, next) {
  return overlayFingerprintForSync(prev) !== overlayFingerprintForSync(next);
}

const baseTrend = {
  id: "d-test:line",
  type: "line",
  kind: "segment",
  start_timestamp: 100,
  start_price: 1.1,
  end_timestamp: 200,
  end_price: 1.2,
  style: { color: "#29f536", width: 1.5 },
  metadata: { drawing_type: "trend", drawing_id: "d-test", selected: false },
  visible: true,
};

assert.equal(needsUpdate(baseTrend, { ...baseTrend }), false, "identical line → no update");

const selected = {
  ...baseTrend,
  metadata: { ...baseTrend.metadata, selected: true },
  style: { ...baseTrend.style, width: 2 },
};
assert.equal(needsUpdate(baseTrend, selected), true, "selected false→true");
assert.equal(needsUpdate(selected, baseTrend), true, "selected true→false");

const anchor1 = { ...baseTrend, start_timestamp: 110 };
assert.equal(needsUpdate(baseTrend, anchor1), true, "first anchor move");

const anchor2 = { ...baseTrend, end_price: 1.25 };
assert.equal(needsUpdate(baseTrend, anchor2), true, "second anchor move");

const rayOn = { ...baseTrend, metadata: { ...baseTrend.metadata, extend_right: true } };
assert.equal(needsUpdate(baseTrend, rayOn), true, "extend_right toggle");
const rayOff = { ...rayOn, metadata: { ...rayOn.metadata, extend_right: false } };
assert.equal(needsUpdate(rayOn, rayOff), true, "extend_right off");

const marker = {
  id: "m1",
  type: "marker",
  timestamp: 100,
  price: 1,
  text: "x",
  style: { color: "#fff" },
  visible: true,
};
assert.equal(needsUpdate(marker, { ...marker }), false, "marker unchanged");

const zone = {
  id: "z1",
  type: "zone",
  start_timestamp: 1,
  end_timestamp: 2,
  top_price: 2,
  bottom_price: 1,
  style: { color: "#fff" },
  visible: true,
};
assert.equal(needsUpdate(zone, { ...zone }), false, "zone unchanged");

console.log("test_research_overlay_fp.mjs: all assertions passed");
