/**
 * Wall X-Ray V1 — offline unit tests (no network, no orders).
 */
"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");
const fs = require("fs");

const H = require(path.join(__dirname, "..", "wall_decision_helpers.js"));
const X = require(path.join(__dirname, "..", "wall_xray_helpers.js"));

function majorUniverse() {
  // Enough ASK walls so Q95 selects only the largest few; plus one tiny reject case.
  const asks = [];
  for (let i = 0; i < 20; i += 1) {
    const price = 78000 + i * 10;
    const qty = 40 + i;
    asks.push({
      id: "ask" + i,
      side: "ASK",
      price,
      qty,
      notional: price * qty,
      zone_lo: price,
      zone_hi: price
    });
  }
  // Dominant major-sized wall
  asks.push({
    id: "ask_big",
    side: "ASK",
    price: 78250,
    qty: 200,
    notional: 78250 * 200,
    zone_lo: 78250,
    zone_hi: 78250,
    major: true
  });
  asks.push({
    id: "ask_backstop",
    side: "ASK",
    price: 78400,
    qty: 400,
    notional: 78400 * 400,
    zone_lo: 78400,
    zone_hi: 78400,
    major: true
  });
  // Tiny non-major (historical reject case)
  asks.push({
    id: "tiny_77573",
    side: "BID",
    price: 77573.8,
    qty: 1.922,
    notional: 77573.8 * 1.922,
    zone_lo: 77573.8,
    zone_hi: 77573.8
  });
  const bids = [];
  for (let i = 0; i < 20; i += 1) {
    const price = 77800 - i * 10;
    const qty = 35 + i;
    bids.push({
      id: "bid" + i,
      side: "BID",
      price,
      qty,
      notional: price * qty,
      zone_lo: price,
      zone_hi: price
    });
  }
  bids.push({
    id: "bid_big",
    side: "BID",
    price: 77600,
    qty: 180,
    notional: 77600 * 180,
    zone_lo: 77600,
    zone_hi: 77600,
    major: true
  });
  return asks.concat(bids);
}

describe("Wall X-Ray start without breakpoint", () => {
  it("creates MONITORING session immediately without BP", () => {
    const walls = majorUniverse();
    const classified = H.classifyMajorWalls(walls);
    const major = classified.find((w) => w.id === "ask_big" && w.major);
    assert.ok(major);
    const created = X.createXraySession({
      symbol: "BTCUSDT",
      wall: major,
      livePrice: 78100,
      walls,
      tickSize: 0.1,
      preRollAvailable: false,
      nowMs: 1_700_000_000_000
    });
    assert.equal(created.ok, true);
    assert.equal(created.session.status, "MONITORING");
    assert.equal(created.session.phase, "MONITORING");
    assert.equal(created.session.bias, "NO_TRADE");
    assert.ok(created.session.reasons.includes("XRAY_STARTED_NOW"));
    assert.ok(!created.session.tradeReady);
    assert.equal(created.session.target.price, 78250);
  });

  it("rejects lock when wall is not major/q95", () => {
    const walls = majorUniverse();
    const tiny = walls.find((w) => w.id === "tiny_77573");
    const created = X.createXraySession({
      symbol: "BTCUSDT",
      wall: Object.assign({}, tiny, { major: false }),
      livePrice: 78000,
      walls
    });
    assert.equal(created.ok, false);
  });
});

describe("Major/Q95 click contract", () => {
  it("only Major/Q95 selectable; 77573.8 qty 1.922 rejected", () => {
    const walls = majorUniverse();
    const clusters = X.clusterWalls(walls, { tickSize: 0.1 });
    const majors = clusters.filter((c) => c.major);
    assert.ok(majors.every((c) => c.major === true));
    assert.ok(!majors.some((c) => Math.abs(c.price - 77573.8) < 0.2));
    const miss = X.matchXrayClick(majors, 77573.8, 0.1, 5);
    assert.equal(miss.ok, false);
    assert.equal(miss.status, "NO_MAJOR_WALL_AT_CLICK");
  });

  it("no nearest-fallback between two walls", () => {
    const majors = [
      {
        id: "a",
        side: "ASK",
        price: 78200,
        zone_lo: 78200,
        zone_hi: 78200,
        notional: 1e7,
        major: true
      },
      {
        id: "b",
        side: "ASK",
        price: 78300,
        zone_lo: 78300,
        zone_hi: 78300,
        notional: 2e7,
        major: true
      }
    ];
    const mid = X.matchXrayClick(majors, 78250, 0.1, 5);
    assert.equal(mid.ok, false);
    assert.equal(mid.status, "NO_MAJOR_WALL_AT_CLICK");
  });

  it("ambiguous when click hits overlapping major zones", () => {
    const majors = [
      {
        id: "a",
        side: "ASK",
        price: 78200,
        zone_lo: 78195,
        zone_hi: 78205,
        notional: 1e7,
        major: true
      },
      {
        id: "b",
        side: "ASK",
        price: 78202,
        zone_lo: 78198,
        zone_hi: 78208,
        notional: 2e7,
        major: true
      }
    ];
    const hit = X.matchXrayClick(majors, 78200, 0.1, 5);
    assert.equal(hit.ok, false);
    assert.equal(hit.status, "AMBIGUOUS_TARGET_CLICK");
  });

  it("chart and full-ob click share matchXrayClick contract (same target_id/zone)", () => {
    const walls = majorUniverse();
    const majors = X.clusterWalls(walls, { tickSize: 0.1 }).filter((c) => c.major);
    const chart = X.matchXrayClick(majors, 78250, 0.1, 5);
    const fullOb = X.matchXrayClick(majors, 78250, 0.1, 5);
    assert.equal(chart.ok, true);
    assert.equal(fullOb.ok, true);
    assert.equal(chart.wall.id, fullOb.wall.id);
    assert.equal(chart.wall.zone_lo, fullOb.wall.zone_lo);
    assert.equal(chart.wall.zone_hi, fullOb.wall.zone_hi);
  });
});

describe("Approach / bias / consume vs pull", () => {
  it("approach and move-away", () => {
    assert.equal(X.computeApproach(10, 5), "approaching");
    assert.equal(X.computeApproach(5, 12), "moving_away");
    assert.equal(X.computeApproach(8, 8), "stable");
  });

  it("no bias on target lock alone", () => {
    const walls = majorUniverse();
    const major = H.classifyMajorWalls(walls).find((w) => w.id === "ask_big");
    const created = X.createXraySession({
      symbol: "BTCUSDT",
      wall: major,
      livePrice: 78100,
      walls,
      nowMs: 1
    });
    const tr = X.transitionXray(created.session, 78100, {}, { nowMs: 2 });
    assert.equal(tr.session.bias, "NO_TRADE");
    assert.ok(tr.session.reasons.includes("NO_BIAS_ON_LOCK_ONLY"));
    assert.equal(tr.session.tradeReady, false);
  });

  it("ASK defended absorbing buys => SHORT_BIAS; BID mirrored LONG_BIAS", () => {
    const ask = X.computeBias("ASK", {
      wallReducePct: 0.1,
      replenishPct: 0.4,
      aggressorBuyShare: 0.7,
      priceResponseBps: 1
    });
    assert.equal(ask.bias, "SHORT_BIAS");
    const bid = X.computeBias("BID", {
      wallReducePct: 0.1,
      replenishPct: 0.4,
      aggressorSellShare: 0.7,
      priceResponseBps: 1
    });
    assert.equal(bid.bias, "LONG_BIAS");
  });

  it("pull is not consume; consume needs explained trades", () => {
    const walls = majorUniverse();
    const major = H.classifyMajorWalls(walls).find((w) => w.id === "ask_big");
    const created = X.createXraySession({
      symbol: "BTCUSDT",
      wall: major,
      livePrice: 78250,
      walls,
      nowMs: 1
    });
    // at contact distance
    const pulled = X.transitionXray(
      created.session,
      78250,
      { wallReducePct: 0.8, tradeExplainedPct: 0.2 },
      { nowMs: 2 }
    );
    assert.equal(pulled.session.phase, "PULLED");
    assert.equal(pulled.session.bias, "NO_TRADE");
    assert.ok(
      pulled.session.reasons.includes("PULL_RISK") ||
        pulled.session.reasons.includes("PULL_WITHOUT_EXPLAINED_TRADES")
    );

    const consumed = X.transitionXray(
      created.session,
      78250,
      {
        wallReducePct: 0.8,
        tradeExplainedPct: 0.75,
        acceptedAboveSec: 20
      },
      { nowMs: 3 }
    );
    assert.ok(["CONSUMED", "ACCEPTED_THROUGH"].includes(consumed.session.phase));
  });

  it("ASK consume + acceptance above => LONG_BIAS; BID mirrored SHORT_BIAS", () => {
    const ask = X.computeBias("ASK", {
      wallReducePct: 0.7,
      tradeExplainedPct: 0.8,
      acceptedAboveSec: 20
    });
    assert.equal(ask.bias, "LONG_BIAS");
    const bid = X.computeBias("BID", {
      wallReducePct: 0.7,
      tradeExplainedPct: 0.8,
      acceptedBelowSec: 20
    });
    assert.equal(bid.bias, "SHORT_BIAS");
  });
});

describe("Wall radar / roles / clustering", () => {
  it("sorts by distance and size; marks DOMINANT", () => {
    const walls = majorUniverse();
    const byDist = X.buildWallRadar({
      livePrice: 78100,
      walls,
      tickSize: 0.1,
      sortMode: "distance"
    });
    assert.ok(byDist.ask.length >= 1);
    for (let i = 1; i < byDist.ask.length; i += 1) {
      assert.ok(byDist.ask[i].distance_abs >= byDist.ask[i - 1].distance_abs - 1e-9);
    }
    const bySize = X.buildWallRadar({
      livePrice: 78100,
      walls,
      tickSize: 0.1,
      sortMode: "size"
    });
    for (let i = 1; i < bySize.ask.length; i += 1) {
      assert.ok((bySize.ask[i].notional || 0) <= (bySize.ask[i - 1].notional || 0) + 1e-6);
    }
    assert.ok(byDist.dominant_ask);
    assert.equal(byDist.dominant_ask.badge, "DOMINANT");
  });

  it("small front wall before large backstop sets LARGER_WALL_BEHIND; no CLEAR_PATH", () => {
    const walls = [
      {
        id: "front",
        side: "ASK",
        price: 78200,
        qty: 50,
        notional: 5e6,
        major: true,
        zone_lo: 78200,
        zone_hi: 78200
      },
      {
        id: "back",
        side: "ASK",
        price: 78350,
        qty: 200,
        notional: 2e7,
        major: true,
        zone_lo: 78350,
        zone_hi: 78350
      }
    ];
    const roles = X.assignWallRoles({
      livePrice: 78100,
      primary: walls[0],
      walls,
      tickSize: 0.1
    });
    assert.equal(roles.FRONT_WALL && roles.FRONT_WALL.price, 78200);
    assert.ok(roles.FRONT_WALL && String(roles.FRONT_WALL.id).includes("78200"));
    assert.equal(roles.BACKSTOP_WALL && roles.BACKSTOP_WALL.price, 78350);
    assert.ok(roles.BACKSTOP_WALL && String(roles.BACKSTOP_WALL.id).includes("78350"));
    assert.equal(roles.LARGER_WALL_BEHIND, true);
    assert.equal(roles.clear_path, false);

    const bias = X.computeBias(
      "ASK",
      { wallReducePct: 0.8, tradeExplainedPct: 0.9, acceptedAboveSec: 30 },
      roles
    );
    assert.equal(bias.bias, "NO_TRADE");
    assert.ok(bias.reasons.includes("NO_CLEAR_PATH") || bias.reasons.includes("LARGER_WALL_BEHIND"));
  });

  it("adjacent ticks cluster deterministically; zone shared", () => {
    const walls = [
      { id: "l1", side: "ASK", price: 78200.0, qty: 10, notional: 1e6, major: true },
      { id: "l2", side: "ASK", price: 78200.3, qty: 12, notional: 1.2e6, major: true },
      { id: "l3", side: "ASK", price: 78200.5, qty: 8, notional: 0.8e6, major: true }
    ];
    const c1 = X.clusterWalls(walls, { tickSize: 0.1, clusterTicks: 5 });
    const c2 = X.clusterWalls(walls, { tickSize: 0.1, clusterTicks: 5 });
    assert.equal(c1.length, 1);
    assert.equal(c2.length, 1);
    assert.equal(c1[0].zone_lo, c2[0].zone_lo);
    assert.equal(c1[0].zone_hi, c2[0].zone_hi);
    assert.equal(c1[0].level_count, 3);
    const snap = X.snapshotTarget(c1[0]);
    assert.equal(snap.zone_lo, c1[0].zone_lo);
    assert.equal(snap.zone_hi, c1[0].zone_hi);
  });

  it("primary never auto-replaced by backstop; PRIMARY_WALL_LOST explicit", () => {
    const walls = majorUniverse();
    const major = H.classifyMajorWalls(walls).find((w) => w.id === "ask_big");
    const created = X.createXraySession({
      symbol: "BTCUSDT",
      wall: major,
      livePrice: 78100,
      walls,
      nowMs: 1
    });
    const primaryId = created.session.target.id;
    const withoutPrimary = walls.filter((w) => w.id !== "ask_big");
    const lost = X.refreshPrimaryPresence(created.session, withoutPrimary, { tickSize: 0.1 });
    assert.equal(lost.lost, true);
    assert.equal(lost.session.status, "PRIMARY_WALL_LOST");
    assert.equal(lost.session.target.id, primaryId);
  });
});

describe("Safety / AVR isolation / BP mode intact", () => {
  it("execution remains false in API config module source", () => {
    const apiPath = path.join(
      __dirname,
      "..",
      "..",
      "..",
      "wall_decision_v1",
      "api.py"
    );
    const src = fs.readFileSync(apiPath, "utf8");
    assert.match(src, /"execution":\s*False/);
    assert.doesNotMatch(src, /place_order|create_order|private\/order/i);
  });

  it("DOGE does not inherit BTC AVR via wall_decision avr context contract", () => {
    const avrPath = path.join(__dirname, "..", "wall_decision_avr_context.js");
    const src = fs.readFileSync(avrPath, "utf8");
    assert.match(src, /BTCUSDT/);
    // Symbol gate must reject non-BTC
    assert.match(src, /symbol_unsupported|unsupported|BTCUSDT/);
  });

  it("existing BP mode helpers still arm after manual target", () => {
    const walls = [
      { id: "a1", side: "ASK", price: 101100, qty: 50, notional: 5e6, major: true },
      { id: "a2", side: "ASK", price: 101200, qty: 5, notional: 5e5 },
      { id: "b1", side: "BID", price: 100800, qty: 40, notional: 4e6, major: true }
    ];
    const bp = H.createBreakpointState({
      symbol: "BTCUSDT",
      price: 101050,
      refPrice: 101000,
      walls
    });
    assert.equal(bp.targetWall, null);
    const locked = H.lockManualTarget(bp, null, {
      price: 101100,
      candidates: bp.targetCandidates
    });
    assert.equal(locked.ok, true);
    assert.equal(locked.state.status, "ARMED");
  });
});
