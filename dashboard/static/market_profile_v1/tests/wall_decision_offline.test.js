/**
 * Wall Decision V1 — offline unit tests (no network, no orders).
 */
"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");

const H = require(path.join(__dirname, "..", "wall_decision_helpers.js"));

const ASK_WALLS = [
  { id: "a1", side: "ASK", price: 101100, qty: 50, notional: 5e6, major: true },
  { id: "a2", side: "ASK", price: 101200, qty: 5, notional: 5e5 },
  { id: "b1", side: "BID", price: 100800, qty: 40, notional: 4e6, major: true }
];

describe("Breakpoint helpers", () => {
  it("rounds to tick size", () => {
    assert.equal(H.roundToTick(101123.37, 0.1), 101123.4);
    assert.equal(H.roundToTick(0.123456, 0.00001), 0.12346);
  });

  it("maps direction from price vs ref", () => {
    assert.equal(H.attackDirection(101100, 101000), "UP_ASK");
    assert.equal(H.attackDirection(100900, 101000), "DOWN_BID");
    assert.equal(H.attackDirection(101000, 101000), null);
  });

  it("creates per-symbol store entries independently", () => {
    let store = H.loadStore(null);
    const btc = H.createBreakpointState({
      symbol: "BTCUSDT",
      price: 101050,
      refPrice: 101000,
      walls: ASK_WALLS
    });
    const doge = H.createBreakpointState({
      symbol: "DOGEUSDT",
      price: 0.12,
      refPrice: 0.11,
      walls: [{ id: "d1", side: "ASK", price: 0.13, qty: 1e6, major: true }]
    });
    store = H.saveSymbolBreakpoint(store, "BTCUSDT", btc);
    store = H.saveSymbolBreakpoint(store, "DOGEUSDT", doge);
    assert.equal(store.bySymbol.BTCUSDT.price, btc.price);
    assert.equal(store.bySymbol.DOGEUSDT.price, doge.price);
    store = H.saveSymbolBreakpoint(store, "BTCUSDT", null);
    assert.equal(store.bySymbol.BTCUSDT, undefined);
    assert.ok(store.bySymbol.DOGEUSDT);
  });

  it("timeframe-agnostic: price persists irrespective of tf field", () => {
    const bp = H.createBreakpointState({
      symbol: "BTCUSDT",
      price: 101050.04,
      tickSize: 0.1,
      refPrice: 101000,
      walls: ASK_WALLS
    });
    assert.equal(bp.price, 101050);
    const store = H.saveSymbolBreakpoint(H.loadStore(null), "BTCUSDT", bp);
    assert.equal(store.bySymbol.BTCUSDT.price, 101050);
  });

  it("rearm after move clears triggered", () => {
    let bp = H.createBreakpointState({
      symbol: "BTCUSDT",
      price: 101050,
      refPrice: 101000,
      walls: ASK_WALLS
    });
    bp = H.applyTrigger(bp, { triggered: true, atMs: 1 });
    assert.equal(bp.triggered, true);
    bp = H.rearmAfterMove(bp, 101060, { refPrice: 101000, walls: ASK_WALLS });
    assert.equal(bp.triggered, false);
    assert.equal(bp.price, 101060);
    assert.ok(["ARMED", "TARGET_LOCKED", "NO_TARGET_WALL"].includes(bp.status));
  });
});

describe("Trigger semantics", () => {
  it("upper BP triggers from below once", () => {
    let bp = H.createBreakpointState({
      symbol: "BTCUSDT",
      price: 101050,
      refPrice: 101000,
      walls: ASK_WALLS
    });
    assert.equal(H.evaluateTrigger(bp, 101040).triggered, false);
    const t1 = H.evaluateTrigger(bp, 101050);
    assert.equal(t1.triggered, true);
    bp = H.applyTrigger(bp, t1);
    assert.equal(H.evaluateTrigger(bp, 101060).triggered, false);
  });

  it("lower BP triggers from above once", () => {
    let bp = H.createBreakpointState({
      symbol: "BTCUSDT",
      price: 100950,
      refPrice: 101000,
      walls: ASK_WALLS
    });
    assert.equal(H.evaluateTrigger(bp, 100960).triggered, false);
    const t1 = H.evaluateTrigger(bp, 100950);
    assert.equal(t1.triggered, true);
    bp = H.applyTrigger(bp, t1);
    assert.equal(H.evaluateTrigger(bp, 100900).triggered, false);
  });

  it("resize/zoom does not invent a trigger without price cross", () => {
    const bp = H.createBreakpointState({
      symbol: "BTCUSDT",
      price: 101050,
      refPrice: 101000,
      walls: ASK_WALLS
    });
    assert.equal(H.evaluateTrigger(bp, 101000).triggered, false);
  });
});

describe("Target wall selection", () => {
  it("locks nearest major ASK above BP", () => {
    const r = H.selectTargetWall({
      breakpoint: 101050,
      refPrice: 101000,
      walls: ASK_WALLS
    });
    assert.equal(r.status, "TARGET_LOCKED");
    assert.equal(r.wall.id, "a1");
    assert.equal(r.wall.side, "ASK");
  });

  it("locks nearest major BID below BP", () => {
    const r = H.selectTargetWall({
      breakpoint: 100900,
      refPrice: 101000,
      walls: ASK_WALLS
    });
    assert.equal(r.status, "TARGET_LOCKED");
    assert.equal(r.wall.id, "b1");
    assert.equal(r.wall.side, "BID");
  });

  it("ignores small non-major walls when median filter applies", () => {
    const walls = [
      { id: "tiny", side: "ASK", price: 101060, qty: 1 },
      { id: "big", side: "ASK", price: 101080, qty: 100 },
      { id: "mid", side: "ASK", price: 101070, qty: 20 }
    ];
    const r = H.selectTargetWall({ breakpoint: 101050, refPrice: 101000, walls: walls });
    assert.equal(r.status, "TARGET_LOCKED");
    assert.ok(r.wall.qty >= 20);
  });

  it("returns NO_TARGET_WALL when empty", () => {
    const r = H.selectTargetWall({ breakpoint: 101050, refPrice: 101000, walls: [] });
    assert.equal(r.status, "NO_TARGET_WALL");
  });

  it("flags AMBIGUOUS_TARGET when two nearest peers tie", () => {
    const walls = [
      { id: "x", side: "ASK", price: 101100, qty: 50, major: true },
      { id: "y", side: "ASK", price: 101100, qty: 51, major: true }
    ];
    const r = H.selectTargetWall({ breakpoint: 101050, refPrice: 101000, walls: walls });
    assert.equal(r.status, "AMBIGUOUS_TARGET");
  });
});

describe("State machine", () => {
  it("ASK consumed + acceptance → LONG_READY", () => {
    const d = H.transitionDecision("TRIGGERED", {
      wallSide: "ASK",
      wallReducePct: 0.7,
      tradeExplainedPct: 0.7,
      replenishPct: 0.1,
      acceptedAboveSec: 15,
      retestHeld: true
    });
    assert.equal(d.state, "LONG_READY");
    assert.ok(d.reasons.includes("ASK_CONSUMED_BY_TRADES"));
    assert.ok(d.reasons.includes("ACCEPTED_ABOVE_15S"));
  });

  it("ASK absorption + replenish + reject → SHORT_READY", () => {
    const d = H.transitionDecision("WALL_ATTACK", {
      wallSide: "ASK",
      wallReducePct: 0.4,
      tradeExplainedPct: 0.7,
      replenishPct: 0.5,
      aggressorBuyShare: 0.8,
      priceResponseBps: 1,
      acceptedBelowSec: 15
    });
    assert.equal(d.state, "SHORT_READY");
    assert.ok(d.reasons.includes("BUY_AGGRESSION_HIGH"));
  });

  it("BID consumed + acceptance below → SHORT_READY", () => {
    const d = H.transitionDecision("TRIGGERED", {
      wallSide: "BID",
      wallReducePct: 0.7,
      tradeExplainedPct: 0.7,
      replenishPct: 0.1,
      acceptedBelowSec: 15
    });
    assert.equal(d.state, "SHORT_READY");
  });

  it("BID absorption + reject above → LONG_READY", () => {
    const d = H.transitionDecision("WALL_ATTACK", {
      wallSide: "BID",
      wallReducePct: 0.4,
      tradeExplainedPct: 0.7,
      replenishPct: 0.5,
      aggressorSellShare: 0.8,
      priceResponseBps: 1,
      acceptedAboveSec: 15
    });
    assert.equal(d.state, "LONG_READY");
  });

  it("pull without acceptance → WALL_PULLED / no trade ready", () => {
    const d = H.transitionDecision("TRIGGERED", {
      wallSide: "ASK",
      wallReducePct: 0.7,
      tradeExplainedPct: 0.2
    });
    assert.equal(d.state, "WALL_PULLED");
    assert.equal(d.tradeReady, false);
  });

  it("high aggression low response → ABSORPTION", () => {
    const d = H.transitionDecision("WALL_ATTACK", {
      wallSide: "ASK",
      aggressorBuyShare: 0.8,
      priceResponseBps: 0.5,
      tradeExplainedPct: 0.7
    });
    assert.equal(d.state, "ABSORPTION");
  });

  it("data gap / stale / epoch / incomplete trades block ready", () => {
    assert.equal(H.transitionDecision("TRIGGERED", { dataGap: true }).state, "DATA_GAP");
    assert.equal(H.transitionDecision("TRIGGERED", { stale: true }).state, "STALE_DATA");
    assert.equal(H.transitionDecision("TRIGGERED", { wallLost: true }).state, "WALL_LOST");
    assert.equal(H.transitionDecision("TRIGGERED", { incompleteTrades: true }).state, "NO_TRADE");
    assert.equal(H.transitionDecision("TRIGGERED", { epochBoundary: true }).state, "NO_TRADE");
  });

  it("no wall reduction + explained 0 is not incomplete trades", () => {
    // Session wd1 bug: UI forced incompleteTrades from DATA_UNAVAILABLE explained.
    // After fix, server sends explained=0 / incomplete=false → stay analysing path.
    const d = H.transitionDecision("WALL_ATTACK", {
      wallSide: "BID",
      wallReducePct: 0,
      tradeExplainedPct: 0,
      pullPct: 0,
      replenishPct: 3.0,
      incompleteTrades: false,
      wallLost: false
    });
    assert.notEqual(d.state, "NO_TRADE");
    assert.ok(!d.reasons.includes("INCOMPLETE_PUBLIC_TRADES"));
  });

  it("thresholds are marked V1_PROVISIONAL", () => {
    assert.equal(H.V1_PROVISIONAL.wall_consume_pct, 0.65);
    assert.ok(H.RULE_VERSION.includes("provisional"));
  });

  it("decision labels include text not only tone", () => {
    assert.equal(H.decisionLabel("LONG_READY"), "LONG READY");
    assert.equal(H.decisionTone("LONG_READY"), "long");
    assert.equal(H.decisionLabel("DATA_GAP"), "DATA GAP");
  });
});

describe("Panel contract helpers", () => {
  it("exports storage keys for panel position", () => {
    assert.equal(typeof H.PANEL_POS_KEY, "string");
    assert.equal(typeof H.STORAGE_KEY, "string");
  });
});
