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

function armWithTarget(opts) {
  let bp = H.createBreakpointState(opts);
  const wall = (opts.lockWallId
    ? (bp.targetCandidates || []).find((w) => w.id === opts.lockWallId)
    : (bp.targetCandidates || [])[0]) || opts.lockWall;
  const locked = H.lockManualTarget(bp, wall, { candidates: bp.targetCandidates || [] });
  assert.equal(locked.ok, true, locked.reason);
  return locked.state;
}

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

  it("BP place does not auto-lock or arm", () => {
    const bp = H.createBreakpointState({
      symbol: "BTCUSDT",
      price: 101050,
      refPrice: 101000,
      walls: ASK_WALLS
    });
    assert.equal(bp.targetWall, null);
    assert.equal(bp.status, "TARGET_CANDIDATES_READY");
    assert.ok(bp.targetCandidates.length >= 1);
    assert.equal(H.evaluateTrigger(bp, 101060).triggered, false);
    assert.equal(H.evaluateTrigger(bp, 101060).reason, "not_armed");
  });

  it("rearm after move clears triggered and requires new manual target", () => {
    let bp = armWithTarget({
      symbol: "BTCUSDT",
      price: 101050,
      refPrice: 101000,
      walls: ASK_WALLS,
      lockWallId: "a1"
    });
    bp = H.applyTrigger(bp, { triggered: true, atMs: 1 });
    assert.equal(bp.triggered, true);
    bp = H.rearmAfterMove(bp, 101060, { refPrice: 101000, walls: ASK_WALLS });
    assert.equal(bp.triggered, false);
    assert.equal(bp.price, 101060);
    assert.equal(bp.targetWall, null);
    assert.ok(["TARGET_CANDIDATES_READY", "BP_SET_WAITING_TARGET"].includes(bp.status));
  });
});

describe("Trigger semantics", () => {
  it("upper BP triggers from below once only when armed + locked", () => {
    let bp = armWithTarget({
      symbol: "BTCUSDT",
      price: 101050,
      refPrice: 101000,
      walls: ASK_WALLS,
      lockWallId: "a1"
    });
    assert.equal(bp.status, "ARMED");
    assert.equal(H.evaluateTrigger(bp, 101040).triggered, false);
    const t1 = H.evaluateTrigger(bp, 101050);
    assert.equal(t1.triggered, true);
    bp = H.applyTrigger(bp, t1);
    assert.equal(H.evaluateTrigger(bp, 101060).triggered, false);
  });

  it("lower BP triggers from above once only when armed + locked", () => {
    let bp = armWithTarget({
      symbol: "BTCUSDT",
      price: 100950,
      refPrice: 101000,
      walls: ASK_WALLS,
      lockWallId: "b1"
    });
    assert.equal(H.evaluateTrigger(bp, 100960).triggered, false);
    const t1 = H.evaluateTrigger(bp, 100950);
    assert.equal(t1.triggered, true);
    bp = H.applyTrigger(bp, t1);
    assert.equal(H.evaluateTrigger(bp, 100900).triggered, false);
  });

  it("resize/zoom does not invent a trigger without price cross", () => {
    const bp = armWithTarget({
      symbol: "BTCUSDT",
      price: 101050,
      refPrice: 101000,
      walls: ASK_WALLS,
      lockWallId: "a1"
    });
    assert.equal(H.evaluateTrigger(bp, 101000).triggered, false);
  });
});

describe("Manual target wall selection", () => {
  it("lists only Major/Q95 ASK candidates above BP", () => {
    const listed = H.listTargetCandidates({
      breakpoint: 101050,
      refPrice: 101000,
      walls: ASK_WALLS
    });
    assert.equal(listed.status, "TARGET_CANDIDATES_READY");
    assert.equal(listed.side, "ASK");
    assert.ok(listed.candidates.every((c) => c.major === true));
    assert.ok(listed.candidates.every((c) => c.percentile >= 95));
    assert.ok(listed.candidates.some((c) => c.id === "a1"));
    assert.ok(!listed.candidates.some((c) => c.id === "a2"));
    assert.ok(!listed.candidates.some((c) => c.side === "BID"));
  });

  it("lists BID Q95 candidates below BP without locking", () => {
    const listed = H.listTargetCandidates({
      breakpoint: 100900,
      refPrice: 101000,
      walls: ASK_WALLS
    });
    assert.equal(listed.status, "TARGET_CANDIDATES_READY");
    assert.equal(listed.side, "BID");
    assert.equal(listed.candidates[0].id, "b1");
    assert.equal(listed.candidates[0].major, true);
  });

  it("rejects tiny non-Q95 walls even when nearer to BP", () => {
    const walls = [
      { id: "tiny", side: "ASK", price: 101060, qty: 1, notional: 1e5 },
      { id: "mid", side: "ASK", price: 101070, qty: 20, notional: 2e6 },
      { id: "big", side: "ASK", price: 101080, qty: 100, notional: 1e7 },
      { id: "big2", side: "ASK", price: 101090, qty: 90, notional: 9e6 },
      { id: "big3", side: "ASK", price: 101100, qty: 80, notional: 8e6 },
      { id: "big4", side: "ASK", price: 101110, qty: 70, notional: 7e6 },
      { id: "big5", side: "ASK", price: 101120, qty: 60, notional: 6e6 },
      { id: "big6", side: "ASK", price: 101130, qty: 55, notional: 5.5e6 },
      { id: "big7", side: "ASK", price: 101140, qty: 52, notional: 5.2e6 },
      { id: "big8", side: "ASK", price: 101150, qty: 51, notional: 5.1e6 },
      { id: "big9", side: "ASK", price: 101160, qty: 50, notional: 5.0e6 },
      { id: "big10", side: "ASK", price: 101170, qty: 49, notional: 4.9e6 },
      { id: "big11", side: "ASK", price: 101180, qty: 48, notional: 4.8e6 },
      { id: "big12", side: "ASK", price: 101190, qty: 47, notional: 4.7e6 },
      { id: "big13", side: "ASK", price: 101200, qty: 46, notional: 4.6e6 },
      { id: "big14", side: "ASK", price: 101210, qty: 45, notional: 4.5e6 },
      { id: "big15", side: "ASK", price: 101220, qty: 44, notional: 4.4e6 },
      { id: "big16", side: "ASK", price: 101230, qty: 43, notional: 4.3e6 },
      { id: "big17", side: "ASK", price: 101240, qty: 42, notional: 4.2e6 },
      { id: "big18", side: "ASK", price: 101250, qty: 41, notional: 4.1e6 }
    ];
    const listed = H.listTargetCandidates({
      breakpoint: 101050,
      refPrice: 101000,
      walls: walls
    });
    assert.ok(!listed.candidates.some((c) => c.id === "tiny"));
    assert.ok(!listed.candidates.some((c) => c.id === "mid"));
    assert.ok(listed.candidates.every((c) => c.percentile >= 95 && c.major === true));
    assert.ok(listed.candidates.some((c) => c.id === "big"));
  });

  it("never invents major=true for ordinary OBP bars", () => {
    const walls = [
      { id: "x1", side: "ASK", price: 101100, qty: 10, notional: 1e6 },
      { id: "x2", side: "ASK", price: 101110, qty: 11, notional: 1.1e6 },
      { id: "x3", side: "ASK", price: 101120, qty: 12, notional: 1.2e6 }
    ];
    const classified = H.classifyMajorWalls(walls);
    assert.ok(classified.every((c) => c.major === false || c.percentile >= 95));
    assert.equal(classified.filter((c) => c.major).length <= 1, true);
    assert.ok(classified.every((c) => c.majorRule !== "invented"));
  });

  it("matchWallAtPrice only hits existing major candidates", () => {
    const listed = H.listTargetCandidates({
      breakpoint: 101050,
      refPrice: 101000,
      walls: ASK_WALLS
    });
    const hit = H.matchWallAtPrice(listed.candidates, 101100, 0.1, 5);
    assert.equal(hit.ok, true);
    assert.equal(hit.wall.id, "a1");
    const miss = H.matchWallAtPrice(listed.candidates, 101200, 0.1, 5);
    assert.equal(miss.ok, false);
  });

  it("lockManualTarget arms only after explicit candidate lock", () => {
    const bp = H.createBreakpointState({
      symbol: "BTCUSDT",
      price: 101050,
      refPrice: 101000,
      walls: ASK_WALLS
    });
    const bad = H.lockManualTarget(bp, { id: "invented", side: "ASK", price: 101111, qty: 9 }, {
      candidates: bp.targetCandidates
    });
    assert.equal(bad.ok, false);
    const good = H.lockManualTarget(bp, null, {
      price: 101100,
      candidates: bp.targetCandidates
    });
    assert.equal(good.ok, true);
    assert.equal(good.state.status, "ARMED");
    assert.equal(good.state.targetStatus, "TARGET_LOCKED");
    assert.equal(good.state.targetWall.id, "a1");
    assert.equal(good.state.targetWall.major, true);
  });

  it("diagnostic selectTargetWall still picks nearest major", () => {
    const r = H.selectTargetWall({
      breakpoint: 101050,
      refPrice: 101000,
      walls: ASK_WALLS
    });
    assert.equal(r.status, "TARGET_LOCKED");
    assert.equal(r.wall.id, "a1");
    assert.equal(r.reason, "nearest_major");
  });

  it("diagnostic selectTargetWall ignores tiny non-major peers", () => {
    const walls = [
      { id: "tiny", side: "ASK", price: 101060, qty: 1, notional: 1e5 },
      { id: "big", side: "ASK", price: 101080, qty: 100, notional: 1e7 },
      { id: "mid", side: "ASK", price: 101070, qty: 20, notional: 2e6 }
    ];
    const r = H.selectTargetWall({ breakpoint: 101050, refPrice: 101000, walls: walls });
    assert.equal(r.status, "TARGET_LOCKED");
    assert.equal(r.wall.id, "big");
  });

  it("returns NO_TARGET_WALL when empty", () => {
    const r = H.selectTargetWall({ breakpoint: 101050, refPrice: 101000, walls: [] });
    assert.equal(r.status, "NO_TARGET_WALL");
    const listed = H.listTargetCandidates({ breakpoint: 101050, refPrice: 101000, walls: [] });
    assert.equal(listed.status, "NO_TARGET_WALL");
  });

  it("flags AMBIGUOUS_TARGET when two nearest peers tie", () => {
    const walls = [
      { id: "x", side: "ASK", price: 101100, qty: 50, notional: 5e6, major: true },
      { id: "y", side: "ASK", price: 101100, qty: 51, notional: 5.1e6, major: true }
    ];
    const r = H.selectTargetWall({ breakpoint: 101050, refPrice: 101000, walls: walls });
    assert.equal(r.status, "AMBIGUOUS_TARGET");
  });

  it("labels cover manual-target states", () => {
    assert.equal(H.decisionLabel("TARGET_CANDIDATES_READY"), "BP SET · SELECT TARGET");
    assert.equal(H.decisionLabel("BP_SET_WAITING_TARGET"), "BP SET · SELECT TARGET");
    assert.equal(H.decisionLabel("TARGET_LOCKED"), "TARGET LOCKED");
    assert.equal(H.decisionLabel("ARMED"), "ARMED");
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
    assert.equal(typeof H.lockManualTarget, "function");
    assert.equal(typeof H.listTargetCandidates, "function");
  });
});
