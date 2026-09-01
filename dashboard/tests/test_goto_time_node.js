/**
 * Node tests for ResearchGotoTime pure helpers.
 */
const assert = require("assert");
const path = require("path");
const gt = require(path.join(__dirname, "..", "static", "js", "research", "goto_time.js"));

const EXP04 = "2026-08-26T11:34:51Z";
const EXP04_UNIX = Math.floor(Date.parse(EXP04) / 1000);

function test_seconds_preserved() {
  assert.strictEqual(gt.parseGotoUtcToUnix("2026-08-26 11:34:51"), EXP04_UNIX);
  assert.strictEqual(gt.parseGotoUtcToUnix("2026-08-26T11:34:51Z"), EXP04_UNIX);
  assert.strictEqual(gt.parseGotoUtcToUnix("26.08.2026, 11:34:51"), EXP04_UNIX);
  assert.strictEqual(gt.unixToIsoZ(EXP04_UNIX), EXP04);
  // Must not silently floor to :00 or :34
  assert.notStrictEqual(gt.unixToIsoZ(EXP04_UNIX), "2026-08-26T11:30:00Z");
  assert.notStrictEqual(gt.unixToIsoZ(EXP04_UNIX), "2026-08-26T11:34:00Z");
}

function test_minute_form_defaults_seconds_zero() {
  const u = gt.parseGotoUtcToUnix("2026-08-26T11:30");
  assert.strictEqual(gt.unixToIsoZ(u), "2026-08-26T11:30:00Z");
}

function test_goto_asof_and_window_same_utc() {
  const win = gt.gotoLoadWindow(EXP04_UNIX);
  assert.strictEqual(win.goto_ts_utc, EXP04_UNIX);
  assert.strictEqual(win.as_of_iso, EXP04);
  assert.strictEqual(win.to - win.from, 2 * gt.GOTO_VIEW_HALF_SEC);
  assert.ok(gt.visibleRangeContains(win.from, win.to, EXP04_UNIX));
  assert.strictEqual(win.from, EXP04_UNIX - 4 * 3600);
  assert.strictEqual(win.to, EXP04_UNIX + 4 * 3600);
}

function test_not_local_tz() {
  // Without Z, parser requires explicit UTC construction — bare local rejection
  // Date-only without time is rejected.
  assert.strictEqual(gt.parseGotoUtcToUnix("not-a-time"), null);
  // Explicit Z and space form both UTC
  const a = gt.parseGotoUtcToUnix("2026-08-26T11:34:51Z");
  const b = gt.parseGotoUtcToUnix("2026-08-26 11:34:51 UTC");
  assert.strictEqual(a, b);
}

function test_source_invariants() {
  const fs = require("fs");
  const js = fs.readFileSync(
    path.join(__dirname, "..", "static", "js", "research", "research_charts.js"),
    "utf8"
  );
  assert.ok(js.includes("goto_ts_utc") || js.includes("gotoTsUtc"));
  assert.ok(js.includes('sourceAction: "go-to"'));
  assert.ok(!js.includes('sourceAction: "go-to-lld-asof"'), "old force-reset LLD path must be gone");
  assert.ok(js.includes("jumpToUnix: goto_ts_utc") || js.includes("jumpToUnix: goto_ts"));
  // After GO TO, must not force resetView without jump guard
  assert.ok(js.includes('opts.sourceAction) !== "go-to"') || js.includes('!== "go-to"'));
}

test_seconds_preserved();
test_minute_form_defaults_seconds_zero();
test_goto_asof_and_window_same_utc();
test_not_local_tz();
test_source_invariants();
console.log("goto_time_node_tests_ok");
