"use strict";

const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const path = __dirname + "/research_orderbook_guard_v1.js";
const ctx = { window: {}, console };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path, "utf8"), ctx);
const G = ctx.window.ResearchOrderbookGuard;

function err(message, apiError) {
  const e = new Error(message);
  if (apiError != null) e.apiError = apiError;
  return e;
}

const middleware = G.classifyOb1000Error(err("429 Too many concurrent requests (/api/research/ob1000-levels)", "Too many concurrent requests"));
assert.strictEqual(middleware.code, "rate_limited");
assert.strictEqual(G.clearsOrderbook("RATE_LIMITED"), false);

const capacity = G.classifyOb1000Error(err("429 capacity_reached", "capacity_reached"));
assert.strictEqual(capacity.code, "capacity_reached");
assert.strictEqual(G.clearsOrderbook("CAPACITY"), true);
assert.strictEqual(G.clearsOrderbook("OFFLINE"), true);
assert.strictEqual(G.clearsOrderbook("LIVE"), false);

const offsets = [0, 1, 2, 3].map((i) => G.staggerOffsetMs(i, 4, 2000));
assert.deepStrictEqual(offsets, [0, 500, 1000, 1500]);
assert.strictEqual(G.staggerOffsetMs(0, 1, 2000), 0);

assert.strictEqual(G.viewportSkipsOrderbookFetch({ inflight: true }, 1000, 2000), true);
assert.strictEqual(G.viewportSkipsOrderbookFetch({ timerPending: true }, 1000, 2000), true);
assert.strictEqual(G.viewportSkipsOrderbookFetch({ lastOkAt: 500 }, 1000, 2000), true);
assert.strictEqual(G.viewportSkipsOrderbookFetch({ lastOkAt: 500 }, 3000, 2000), false);

const charts = fs.readFileSync(__dirname + "/research_charts.js", "utf8");
assert.ok(charts.indexOf("fetchObLevelsShared") >= 0);
assert.ok(charts.indexOf('reason: "viewport"') >= 0);
assert.ok(charts.indexOf("scheduleVisibleOrderbookLevelsStaggered") >= 0);
assert.ok(charts.indexOf("MAX_CONCURRENT_REQUESTS") < 0);

console.log("orderbook_guard_ok");
