/**
 * Static checks: replay bounce-back root causes addressed in source.
 */
const assert = require("assert");
const fs = require("fs");
const path = require("path");

const js = fs.readFileSync(
  path.join(__dirname, "..", "static", "js", "research", "research_charts.js"),
  "utf8"
);
const chart = fs.readFileSync(
  path.join(__dirname, "..", "static", "research_trp", "chart.js"),
  "utf8"
);

// Root cause A: setEmaOverlays -> applyDefaultView when followLive (fixed via replay lock + stopPoll)
assert.ok(js.includes("if (isHistoricalReplay()) return"), "poll skipped in replay");
assert.ok(js.includes("stopPoll()"), "poll stopped on enter replay");
assert.ok(chart.includes("if (followLive && !replayViewLock)"), "EMA no default view when replay locked");
assert.ok(chart.includes("if (followLive && !replayViewLock) stickToLiveEdge"), "no stickToLive in replay");

// Root cause B: pollPane setData preserveView + stickToLiveEdge
assert.ok(js.includes("skipDefaultView: isHistoricalReplay()"), "poll setData skip default in replay");

// Stale generation
assert.ok(js.includes("reqReplayGen !== state.replayGen"), "stale replay responses discarded");

// TF change preserves replay
assert.ok(js.includes("tf-change-replay"), "timeframe change replay path");

console.log("replay_view_lock_node_ok");
