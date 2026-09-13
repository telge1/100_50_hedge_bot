/**
 * Deterministic offline hotpath tests for Market Profile frontend.
 * No network, no ClickHouse, no dashboard.app import.
 */
"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");

const helpers = require(path.join(
  __dirname,
  "..",
  "hotpath_helpers.js"
));

describe("MpHotpathHelpers forming signature", () => {
  it("treats identical OHLC as no-apply", () => {
    const bar = { time: 100, open: 1, high: 2, low: 0.5, close: 1.5, volume: 10 };
    const a = helpers.shouldApplyForming("", bar);
    assert.equal(a.apply, true);
    const b = helpers.shouldApplyForming(a.signature, Object.assign({}, bar));
    assert.equal(b.apply, false);
  });

  it("applies when only close changes", () => {
    const bar = { time: 100, open: 1, high: 2, low: 0.5, close: 1.5 };
    const a = helpers.shouldApplyForming("", bar);
    const b = helpers.shouldApplyForming(a.signature, Object.assign({}, bar, { close: 1.6 }));
    assert.equal(b.apply, true);
  });

  it("applies when only high/low/volume/time changes", () => {
    const base = { time: 100, open: 1, high: 2, low: 0.5, close: 1.5, volume: 10 };
    const sig = helpers.formingConsumedSignature(base);
    assert.equal(helpers.shouldApplyForming(sig, Object.assign({}, base, { high: 3 })).apply, true);
    assert.equal(helpers.shouldApplyForming(sig, Object.assign({}, base, { low: 0.1 })).apply, true);
    assert.equal(helpers.shouldApplyForming(sig, Object.assign({}, base, { volume: 11 })).apply, true);
    assert.equal(helpers.shouldApplyForming(sig, Object.assign({}, base, { time: 200 })).apply, true);
  });
});

describe("forming poll controller", () => {
  it("targets ~60 fetches/min vs baseline ~240", () => {
    assert.equal(helpers.formingFetchesPerMinute(helpers.FORMING_MS_BASELINE), 240);
    assert.equal(helpers.formingFetchesPerMinute(helpers.FORMING_MS), 60);
  });

  it("skips identical forming bars after first apply", async () => {
    const bars = [];
    const tick = () => new Promise((r) => setTimeout(r, 0));
    const ctrl = helpers.createFormingPollController({
      intervalMs: 1000,
      isHidden: () => false,
      setInterval: () => ({ alive: true }),
      clearInterval: () => {},
      fetchForming: async () => ({ time: 1, open: 1, high: 1, low: 1, close: 1 }),
      onBar: (b) => bars.push(b)
    });
    ctrl.start();
    await tick();
    assert.equal(ctrl.getStats().applied, 1);
    for (let i = 0; i < 10; i += 1) {
      ctrl.poll(ctrl.getGen());
      await tick();
    }
    const st = ctrl.getStats();
    assert.equal(st.applied, 1);
    assert.ok(st.skippedSame >= 10, JSON.stringify(st));
    assert.equal(bars.length, 1);
    ctrl.stop();
  });

  it("never runs more than one inflight forming fetch", async () => {
    let resolveFetch;
    const pending = new Promise((r) => { resolveFetch = r; });
    let calls = 0;
    const ctrl = helpers.createFormingPollController({
      intervalMs: 1000,
      setInterval: () => ({ alive: true }),
      clearInterval: () => {},
      fetchForming: () => {
        calls += 1;
        return pending.then(() => ({ time: 1, open: 1, high: 1, low: 1, close: 1 }));
      },
      onBar: () => {}
    });
    ctrl.start();
    ctrl.poll(ctrl.getGen());
    ctrl.poll(ctrl.getGen());
    await Promise.resolve();
    assert.equal(calls, 1);
    assert.equal(ctrl.getStats().skippedInflight, 2);
    resolveFetch();
    await pending;
    await Promise.resolve();
    ctrl.stop();
  });

  it("drops stale responses after generation bump (symbol/TF change)", async () => {
    let resolveFetch;
    const pending = new Promise((r) => { resolveFetch = r; });
    const applied = [];
    const ctrl = helpers.createFormingPollController({
      intervalMs: 1000,
      setInterval: () => ({ alive: true }),
      clearInterval: () => {},
      fetchForming: () => pending.then(() => ({ time: 9, open: 1, high: 1, low: 1, close: 9 })),
      onBar: (b) => applied.push(b)
    });
    ctrl.start();
    const gen = ctrl.getGen();
    await Promise.resolve();
    ctrl.stop(); // bump gen mid-flight
    resolveFetch();
    await pending;
    await Promise.resolve();
    await Promise.resolve();
    assert.equal(applied.length, 0);
    assert.ok(ctrl.getStats().skippedStale >= 1 || ctrl.getGen() !== gen);
    ctrl.stop();
  });

  it("pauses on hidden and resumes with a single chain", async () => {
    let hidden = false;
    const timers = [];
    const ctrl = helpers.createFormingPollController({
      intervalMs: 1000,
      isHidden: () => hidden,
      setInterval: (fn, ms) => {
        const id = { fn, ms, alive: true };
        timers.push(id);
        return id;
      },
      clearInterval: (id) => {
        if (id) id.alive = false;
      },
      fetchForming: async () => ({ time: 1, open: 1, high: 1, low: 1, close: 1 }),
      onBar: () => {}
    });
    ctrl.start();
    await Promise.resolve();
    assert.equal(ctrl.hasTimer(), true);
    hidden = true;
    ctrl.onVisibilityChange();
    assert.equal(ctrl.hasTimer(), false);
    hidden = false;
    ctrl.resetStats();
    ctrl.onVisibilityChange();
    await Promise.resolve();
    await Promise.resolve();
    assert.equal(ctrl.hasTimer(), true);
    assert.equal(ctrl.getStats().fetches, 1);
    const alive = timers.filter((t) => t.alive);
    assert.equal(alive.length, 1);
    ctrl.stop();
  });
});

describe("rAF draw coalescer", () => {
  it("collapses four schedules in one frame into one run", () => {
    const queue = [];
    const coalescer = helpers.createRafCoalescer(
      (cb) => {
        queue.push(cb);
        return queue.length;
      },
      () => { queue.length = 0; }
    );
    let runs = 0;
    coalescer.schedule(() => { runs += 1; });
    coalescer.schedule(() => { runs += 1; });
    coalescer.schedule(() => { runs += 1; });
    coalescer.schedule(() => { runs += 1; });
    assert.equal(queue.length, 1);
    queue[0]();
    assert.equal(runs, 1);
  });
});

describe("request dedupe", () => {
  it("blocks identical inflight keys and allows after end", () => {
    const d = helpers.createInflightDedupe();
    const key = helpers.requestKey({
      layer: "lld",
      symbol: "BTCUSDT",
      timeframe: "15m",
      start: 1,
      end: 2,
      generation: 3
    });
    assert.equal(d.begin(key), true);
    assert.equal(d.begin(key), false);
    d.end(key);
    assert.equal(d.begin(key), true);
  });

  it("does not conflate different symbols/ranges", () => {
    const d = helpers.createInflightDedupe();
    const a = helpers.requestKey({ layer: "oi", symbol: "BTCUSDT", start: 1, end: 2, generation: 1 });
    const b = helpers.requestKey({ layer: "oi", symbol: "ETHUSDT", start: 1, end: 2, generation: 1 });
    assert.equal(d.begin(a), true);
    assert.equal(d.begin(b), true);
  });
});

describe("production constants wired", () => {
  it("app.js uses FORMING_MS 1000 and helpers", () => {
    const fs = require("fs");
    const app = fs.readFileSync(
      path.join(__dirname, "..", "app.js"),
      "utf8"
    );
    assert.match(app, /FORMING_MS\s*=\s*\(_hp\s*&&\s*_hp\.FORMING_MS\)\s*\|\|\s*1000/);
    assert.match(app, /pauseLivePoll/);
    assert.match(app, /shouldApplyForming/);
    assert.match(app, /second setData with the same candles/);
    assert.doesNotMatch(app, /FORMING_MS\s*=\s*250\b/);
    // Second setData path removed
    assert.doesNotMatch(app, /applyPayloadToChart\(s,\s*\{\s*preserveView:\s*true\s*\}\)/);
  });

  it("chart.js bootstraps helpers and coalesces layoutOverlays", () => {
    const fs = require("fs");
    const chart = fs.readFileSync(
      path.join(__dirname, "..", "..", "research_trp", "chart.js"),
      "utf8"
    );
    assert.match(chart, /MpHotpathHelpers bootstrap/);
    assert.match(chart, /_overlayLayoutCoalesce/);
    assert.match(chart, /function layoutOverlaysNow/);
  });
});
