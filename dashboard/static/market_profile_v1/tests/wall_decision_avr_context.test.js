/**
 * Wall Decision AVR context — offline unit tests (no network).
 */
"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");
const fs = require("fs");
const vm = require("vm");

function loadAvrContext() {
  const code = fs.readFileSync(
    path.join(__dirname, "..", "wall_decision_avr_context.js"),
    "utf8"
  );
  const sandbox = {
    window: {},
    globalThis: {},
    Date,
    Math,
    Number,
    String,
    Array,
    Object,
    URLSearchParams,
    setInterval: () => 1,
    clearInterval: () => {},
    fetch: () => Promise.reject(new Error("no network in unit test")),
    CustomEvent: function CustomEvent() {},
    console
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.runInNewContext(code, sandbox, { filename: "wall_decision_avr_context.js" });
  return sandbox.MpWallDecisionAvrContext;
}

describe("Wall Decision AVR context", () => {
  it("maps candle.avr without recomputing state", () => {
    const A = loadAvrContext();
    const out = A.buildFromCandle(
      {
        time: 1789380000,
        avr: {
          dominant_state: "SELLER_CONTROL",
          final_state: "SELLER_CONTROL",
          total_buy_notional: 100,
          total_sell_notional: 400,
          provisional: false,
          trade_price: { move_bps: -12.5 },
          evidence: [
            {
              available_at: 1789380300,
              price_progress_bps: -11,
              directional_velocity_bps_s_down: 0.4,
              directional_velocity_bps_s_up: 0,
              impact_efficiency_bps_per_million_sell: 22
            }
          ]
        }
      },
      "BTCUSDT"
    );
    assert.equal(out.available, true);
    assert.equal(out.state, "SELLER_CONTROL");
    assert.equal(out.source, "footprint_avr_v1");
    assert.equal(out.buy_notional, 100);
    assert.equal(out.sell_notional, 400);
    assert.ok(Math.abs(out.imbalance - (100 - 400) / 500) < 1e-9);
    assert.equal(out.response_bps, -11);
    assert.equal(out.efficiency_bps_per_million, 22);
    assert.equal(out.down_velocity_bps_per_s, 0.4);
    assert.equal(out.proxy, false);
    assert.equal(out.event_time_ns, 1789380000 * 1e9);
    assert.equal(out.available_at_ns, 1789380300 * 1e9);
  });

  it("marks vacuum states as proxy", () => {
    const A = loadAvrContext();
    const out = A.buildFromCandle({
      time: 1,
      avr: { dominant_state: "VACUUM_DOWN_PROXY", final_state: "VACUUM_DOWN_PROXY" }
    });
    assert.equal(out.available, true);
    assert.equal(out.proxy, true);
  });

  it("returns unavailable when avr missing", () => {
    const A = loadAvrContext();
    const out = A.buildFromCandle({ time: 1, avr: null });
    assert.equal(out.available, false);
    assert.equal(out.state, null);
  });

  it("publishes into window.__mpWallDecisionContext.avr", () => {
    const A = loadAvrContext();
    A.publishFromCandle({
      time: 10,
      avr: { final_state: "BUYER_CONTROL", dominant_state: "BUYER_CONTROL" }
    });
    const got = A.readStateName();
    assert.equal(got.status, "ok");
    assert.equal(got.value, "BUYER_CONTROL");
    assert.equal(A.getAvr().available, true);
  });
});
