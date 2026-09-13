/**
 * Offline LLD slim overlay + fallback contract tests.
 * No network, no ClickHouse, no dashboard.app.
 */
"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");
const fs = require("fs");

const helpers = require(path.join(__dirname, "..", "lld_overlay_helpers.js"));

function zone(id, direction) {
  return {
    id,
    namespace: "LLD",
    type: "rectangle",
    points: [
      { time: 1, price: 100 },
      { time: 2, price: 101 }
    ],
    metadata: { direction: direction || "support", status: "active" }
  };
}

function paneFixture() {
  return {
    success: true,
    symbol: "BTCUSDT",
    timeframe: "15m",
    from: 1,
    to: 2,
    candles: Array.from({ length: 50 }, (_, i) => ({
      time: i,
      open: 1,
      high: 2,
      low: 0.5,
      close: 1.5,
      volume: 1
    })),
    ema: { series: [{ length: 9, values: [1, 2] }] },
    stochastic: { enabled: false },
    open_interest: { enabled: false },
    overlays: [
      {
        id: "draw:1",
        namespace: "USER_DRAWING",
        metadata: { source: "drawing", drawing_id: "draw:1" }
      },
      zone("lld:1", "support"),
      zone("lld:2", "resistance")
    ],
    lld_ema: { fast: [{ time: 1, value: 1 }], slow: [], fast_visible: true, slow_visible: false },
    clusters: { "3": 1, "4-5": 0, "6+": 0 },
    liquidity: {
      overlays: [zone("lld:1"), zone("lld:2")],
      ema: { fast: [{ time: 1, value: 1 }], slow: [], fast_visible: true, slow_visible: false },
      clusters: { "3": 1, "4-5": 0, "6+": 0 },
      liquidity_location: { mode: "live", liquidity_location_as_of: null }
    },
    liquidity_location_mode: "live",
    liquidity_location_as_of: null,
    canonical_snapshot_sha256: "abc"
  };
}

function slimFixture(pane) {
  const overlays = (pane.overlays || []).filter((p) => helpers.isLldOverlay(p));
  return {
    contract_version: helpers.CONTRACT_VERSION,
    success: true,
    symbol: pane.symbol,
    timeframe: pane.timeframe,
    from: pane.from,
    to: pane.to,
    overlays,
    lld_ema: pane.lld_ema,
    clusters: pane.clusters,
    liquidity: pane.liquidity,
    liquidity_location_mode: pane.liquidity_location_mode,
    liquidity_location_as_of: pane.liquidity_location_as_of,
    canonical_snapshot_sha256: pane.canonical_snapshot_sha256
  };
}

function coreState(s) {
  return {
    orderedIds: s.orderedIds,
    lldPayloads: s.lldPayloads,
    lldEma: s.lldEma,
    clusters: s.clusters,
    liquidityLocationMode: s.liquidityLocationMode,
    liquidityLocationAsOf: s.liquidityLocationAsOf,
    canonicalSnapshotSha256: s.canonicalSnapshotSha256,
    symbol: s.symbol,
    timeframe: s.timeframe,
    from: s.from,
    to: s.to
  };
}

describe("LLD extractConsumedLldState parity", () => {
  it("pane and slim normalize to the same internal state", () => {
    const pane = paneFixture();
    const slim = slimFixture(pane);
    assert.deepEqual(
      coreState(helpers.extractConsumedLldState(pane)),
      coreState(helpers.extractConsumedLldState(slim))
    );
  });

  it("ignores drawings and keeps LLD order", () => {
    const pane = paneFixture();
    const st = helpers.extractConsumedLldState(pane);
    assert.deepEqual(st.orderedIds, ["lld:1", "lld:2"]);
    assert.equal(Object.keys(st.lldPayloads).length, 2);
  });
});

describe("fallback status contract", () => {
  it("404 and 405 allow fallback", () => {
    assert.equal(helpers.shouldFallbackToPane(404), true);
    assert.equal(helpers.shouldFallbackToPane(405), true);
  });

  for (const status of [401, 403, 422, 429, 500, 502, 503]) {
    it(`status ${status} does not fallback`, () => {
      assert.equal(helpers.shouldFallbackToPane(status), false);
    });
  }
});

describe("loadLldWithFallback", () => {
  it("uses slim only when available (new FE + new BE)", async () => {
    const pane = paneFixture();
    const slim = slimFixture(pane);
    const calls = [];
    const result = await helpers.loadLldWithFallback({
      requestBody: { symbol: "BTCUSDT" },
      paneBody: { symbol: "BTCUSDT", ema: {} },
      fetchJson: async (url) => {
        calls.push(url);
        if (url === helpers.SLIM_PATH) return { ok: true, status: 200, payload: slim };
        throw new Error("pane should not be called");
      }
    });
    assert.equal(result.source, "slim");
    assert.deepEqual(calls, [helpers.SLIM_PATH]);
    assert.equal(result.calls.filter((u) => u === helpers.PANE_PATH).length, 0);
  });

  it("falls back exactly once on 404 (new FE + old BE)", async () => {
    const pane = paneFixture();
    const calls = [];
    const result = await helpers.loadLldWithFallback({
      requestBody: { symbol: "BTCUSDT" },
      paneBody: { symbol: "BTCUSDT" },
      fetchJson: async (url) => {
        calls.push(url);
        if (url === helpers.SLIM_PATH) return { ok: false, status: 404, payload: { error: "not_found" } };
        return { ok: true, status: 200, payload: pane };
      }
    });
    assert.equal(result.source, "pane_fallback");
    assert.deepEqual(calls, [helpers.SLIM_PATH, helpers.PANE_PATH]);
  });

  it("falls back on 405", async () => {
    const pane = paneFixture();
    const result = await helpers.loadLldWithFallback({
      fetchJson: async (url) => {
        if (url === helpers.SLIM_PATH) return { ok: false, status: 405, payload: null };
        return { ok: true, status: 200, payload: pane };
      }
    });
    assert.equal(result.source, "pane_fallback");
  });

  for (const status of [401, 403, 422, 429, 500]) {
    it(`does not fallback on ${status}`, async () => {
      let paneCalls = 0;
      await assert.rejects(() =>
        helpers.loadLldWithFallback({
          fetchJson: async (url) => {
            if (url === helpers.SLIM_PATH) return { ok: false, status, payload: { error: "x" } };
            paneCalls += 1;
            return { ok: true, status: 200, payload: {} };
          }
        })
      );
      assert.equal(paneCalls, 0);
    });
  }

  it("does not fallback on timeout/network rejection", async () => {
    let paneCalls = 0;
    await assert.rejects(
      () =>
        helpers.loadLldWithFallback({
          fetchJson: async (url) => {
            if (url === helpers.SLIM_PATH) throw new Error("timeout");
            paneCalls += 1;
            return { ok: true, status: 200, payload: {} };
          }
        }),
      /timeout/
    );
    assert.equal(paneCalls, 0);
  });

  it("toggle-off before slim apply discards body", async () => {
    const slim = slimFixture(paneFixture());
    const result = await helpers.loadLldWithFallback({
      fetchJson: async () => ({ ok: true, status: 200, payload: slim }),
      isToggleOff: () => true
    });
    assert.equal(result.applied, false);
    assert.equal(result.reason, "toggle_off");
  });

  it("toggle-off before pane fallback discards body", async () => {
    let toggled = false;
    const pane = paneFixture();
    const result = await helpers.loadLldWithFallback({
      fetchJson: async (url) => {
        if (url === helpers.SLIM_PATH) {
          toggled = true;
          return { ok: false, status: 404, payload: null };
        }
        return { ok: true, status: 200, payload: pane };
      },
      isToggleOff: () => toggled
    });
    assert.equal(result.applied, false);
    assert.equal(result.reason, "toggle_off");
  });

  it("stale generation discards slim", async () => {
    const slim = slimFixture(paneFixture());
    const result = await helpers.loadLldWithFallback({
      fetchJson: async () => ({ ok: true, status: 200, payload: slim }),
      isStale: () => true
    });
    assert.equal(result.applied, false);
    assert.equal(result.reason, "stale");
  });

  it("abort discards without pane fallback after slim success path gate", async () => {
    const slim = slimFixture(paneFixture());
    const result = await helpers.loadLldWithFallback({
      fetchJson: async () => ({ ok: true, status: 200, payload: slim }),
      isAborted: () => true
    });
    assert.equal(result.applied, false);
    assert.equal(result.reason, "abort");
  });
});

describe("compatibility matrix (offline)", () => {
  it("old FE + old BE: pane path only", () => {
    // Old FE called pane directly; helper not involved. Documented by PANE_PATH presence.
    assert.equal(helpers.PANE_PATH, "/api/research/pane");
  });

  it("old FE + new BE: pane still valid contract", () => {
    const pane = paneFixture();
    const st = helpers.extractConsumedLldState(pane);
    assert.ok(st.orderedIds.length >= 1);
  });

  it("new FE + old BE: one pane fallback", async () => {
    const pane = paneFixture();
    const result = await helpers.loadLldWithFallback({
      fetchJson: async (url) =>
        url === helpers.SLIM_PATH
          ? { ok: false, status: 404, payload: null }
          : { ok: true, status: 200, payload: pane }
    });
    assert.equal(result.source, "pane_fallback");
    assert.equal(result.calls.filter((u) => u === helpers.PANE_PATH).length, 1);
  });

  it("new FE + new BE: no pane call", async () => {
    const slim = slimFixture(paneFixture());
    const result = await helpers.loadLldWithFallback({
      fetchJson: async (url) => {
        assert.equal(url, helpers.SLIM_PATH);
        return { ok: true, status: 200, payload: slim };
      }
    });
    assert.equal(result.source, "slim");
  });
});

describe("app.js wiring", () => {
  it("bootstraps helpers and uses slim path", () => {
    const app = fs.readFileSync(path.join(__dirname, "..", "app.js"), "utf8");
    assert.match(app, /MpLldOverlayHelpers bootstrap/);
    assert.match(app, /loadLiquidityLocationOverlay/);
    assert.match(app, /\/api\/research\/liquidity-location/);
    assert.match(app, /loadLldWithFallback/);
  });
});
