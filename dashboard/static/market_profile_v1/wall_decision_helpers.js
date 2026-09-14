/**
 * Manual Wall Breakpoint + Live Wall Decision V1 — pure helpers (Node + browser).
 * No network, no DOM, no ClickHouse. Thresholds marked V1_PROVISIONAL.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  if (root) {
    root.MpWallDecisionHelpers = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  var STORAGE_KEY = "mp_v1_wall_decision";
  var PANEL_POS_KEY = "mp_v1_wall_decision_panel_pos";
  var RULE_VERSION = "wall_decision_v1_provisional";

  /** @type {Readonly<Record<string, number>>} */
  var V1_PROVISIONAL = Object.freeze({
    wall_consume_pct: 0.65,
    trade_explained_pct: 0.6,
    aggressor_share: 0.65,
    acceptance_sec: 15,
    replenish_max_pct: 0.25,
    hysteresis_ticks: 2,
    major_wall_median_mult: 3.0
  });

  var STATES = Object.freeze([
    "DISARMED",
    "ARMED",
    "NO_TARGET_WALL",
    "TARGET_LOCKED",
    "TRIGGERED",
    "WALL_ATTACK",
    "WALL_CONSUMED",
    "WALL_PULLED",
    "WALL_REPLENISHED",
    "ABSORPTION",
    "BREAKOUT_PENDING",
    "REJECTION_PENDING",
    "ACCEPTED_ABOVE",
    "ACCEPTED_BELOW",
    "LONG_READY",
    "SHORT_READY",
    "NO_TRADE",
    "WALL_LOST",
    "DATA_GAP",
    "STALE_DATA",
    "CANCELLED",
    "EXPIRED"
  ]);

  var TERMINAL_NO_TRADE = Object.freeze({
    LONG_READY: false,
    SHORT_READY: false,
    DATA_GAP: true,
    STALE_DATA: true,
    WALL_LOST: true,
    NO_TRADE: true,
    CANCELLED: true,
    EXPIRED: true
  });

  function num(v) {
    var n = Number(v);
    return Number.isFinite(n) ? n : null;
  }

  function roundToTick(price, tickSize) {
    var p = num(price);
    var t = num(tickSize);
    if (p == null) return null;
    if (t == null || t <= 0) return p;
    var rounded = Math.round(p / t) * t;
    var decimals = String(t).includes(".") ? String(t).split(".")[1].length : 0;
    return Number(rounded.toFixed(Math.min(decimals, 8)));
  }

  function defaultTickSize(symbol) {
    var s = String(symbol || "").toUpperCase();
    if (s.indexOf("BTC") === 0) return 0.1;
    if (s.indexOf("ETH") === 0) return 0.01;
    if (s.indexOf("XAUT") === 0) return 0.01;
    if (s.indexOf("DOGE") === 0) return 0.00001;
    return 0.01;
  }

  function attackDirection(breakpoint, refPrice) {
    var bp = num(breakpoint);
    var px = num(refPrice);
    if (bp == null || px == null) return null;
    if (bp > px) return "UP_ASK";
    if (bp < px) return "DOWN_BID";
    return null;
  }

  function expectedWallSide(direction) {
    if (direction === "UP_ASK") return "ASK";
    if (direction === "DOWN_BID") return "BID";
    return null;
  }

  /**
   * Select next relevant wall behind breakpoint in attack direction.
   * walls: [{id, side:'BID'|'ASK', price, qty, notional, major?:boolean}]
   */
  function selectTargetWall(opts) {
    opts = opts || {};
    var bp = num(opts.breakpoint);
    var direction = opts.direction || attackDirection(bp, opts.refPrice);
    var side = expectedWallSide(direction);
    var walls = Array.isArray(opts.walls) ? opts.walls : [];
    var mult = num(opts.medianMult) != null ? opts.medianMult : V1_PROVISIONAL.major_wall_median_mult;

    if (!side || bp == null) {
      return { status: "NO_TARGET_WALL", wall: null, reason: "no_direction" };
    }

    var qtys = walls.map(function (w) { return num(w.qty); }).filter(function (q) { return q != null && q > 0; });
    var median = null;
    if (qtys.length) {
      qtys.sort(function (a, b) { return a - b; });
      var mid = Math.floor(qtys.length / 2);
      median = qtys.length % 2 ? qtys[mid] : (qtys[mid - 1] + qtys[mid]) / 2;
    }

    var candidates = walls.filter(function (w) {
      if (!w || String(w.side).toUpperCase() !== side) return false;
      var p = num(w.price);
      if (p == null) return false;
      if (direction === "UP_ASK" && !(p >= bp)) return false;
      if (direction === "DOWN_BID" && !(p <= bp)) return false;
      if (w.major === true) return true;
      if (median == null || median <= 0) return true;
      var q = num(w.qty);
      return q != null && q >= median * mult;
    });

    if (!candidates.length) {
      return { status: "NO_TARGET_WALL", wall: null, reason: "no_relevant_wall" };
    }

    candidates.sort(function (a, b) {
      var pa = Math.abs(num(a.price) - bp);
      var pb = Math.abs(num(b.price) - bp);
      if (pa !== pb) return pa - pb;
      return (num(b.qty) || 0) - (num(a.qty) || 0);
    });

    var best = candidates[0];
    var second = candidates[1];
    if (second) {
      var d0 = Math.abs(num(best.price) - bp);
      var d1 = Math.abs(num(second.price) - bp);
      var q0 = num(best.qty) || 0;
      var q1 = num(second.qty) || 0;
      if (d0 === d1 && Math.abs(q0 - q1) / Math.max(q0, q1, 1) < 0.05) {
        return {
          status: "AMBIGUOUS_TARGET",
          wall: best,
          candidates: candidates.slice(0, 3),
          reason: "ambiguous_nearest"
        };
      }
    }

    return {
      status: "TARGET_LOCKED",
      wall: {
        id: best.id != null ? String(best.id) : side + ":" + String(best.price),
        side: side,
        price: num(best.price),
        qty: num(best.qty),
        notional: num(best.notional),
        zone_lo: num(best.zone_lo) != null ? num(best.zone_lo) : num(best.price),
        zone_hi: num(best.zone_hi) != null ? num(best.zone_hi) : num(best.price)
      },
      reason: "nearest_major"
    };
  }

  function createSessionId(symbol, tsNs) {
    var t = tsNs != null ? String(tsNs) : String(Date.now()) + "000000";
    return "wd1_" + String(symbol || "UNK").toUpperCase() + "_" + t;
  }

  function createBreakpointState(opts) {
    opts = opts || {};
    var symbol = String(opts.symbol || "").toUpperCase();
    var tick = num(opts.tickSize) != null ? num(opts.tickSize) : defaultTickSize(symbol);
    var price = roundToTick(opts.price, tick);
    var ref = num(opts.refPrice);
    var direction = attackDirection(price, ref);
    var now = opts.nowMs != null ? Number(opts.nowMs) : Date.now();
    var target = selectTargetWall({
      breakpoint: price,
      refPrice: ref,
      direction: direction,
      walls: opts.walls || [],
      medianMult: opts.medianMult
    });
    var status =
      target.status === "NO_TARGET_WALL"
        ? "NO_TARGET_WALL"
        : target.status === "AMBIGUOUS_TARGET"
          ? "ARMED"
          : "TARGET_LOCKED";
    return {
      symbol: symbol,
      price: price,
      tickSize: tick,
      direction: direction,
      wallSide: expectedWallSide(direction),
      status: status,
      targetStatus: target.status,
      targetWall: target.wall,
      ambiguous: target.status === "AMBIGUOUS_TARGET",
      createdAtMs: now,
      updatedAtMs: now,
      armedCycleId: createSessionId(symbol, now * 1e6),
      triggered: false,
      sessionId: null
    };
  }

  /**
   * Directional trigger with one-shot per armed cycle + hysteresis.
   */
  function evaluateTrigger(bpState, livePrice, opts) {
    opts = opts || {};
    if (!bpState || bpState.triggered) {
      return { triggered: false, reason: "already_triggered_or_missing" };
    }
    if (bpState.status !== "ARMED" && bpState.status !== "TARGET_LOCKED" && bpState.status !== "NO_TARGET_WALL") {
      return { triggered: false, reason: "not_armed" };
    }
    var px = num(livePrice);
    var line = num(bpState.price);
    var tick = num(bpState.tickSize) || defaultTickSize(bpState.symbol);
    var hyst = (num(opts.hysteresisTicks) != null ? num(opts.hysteresisTicks) : V1_PROVISIONAL.hysteresis_ticks) * tick;
    if (px == null || line == null) return { triggered: false, reason: "bad_price" };

    var hit = false;
    if (bpState.direction === "UP_ASK") {
      hit = px + 1e-12 >= line;
    } else if (bpState.direction === "DOWN_BID") {
      hit = px - 1e-12 <= line;
    } else {
      return { triggered: false, reason: "no_direction" };
    }
    if (!hit) return { triggered: false, reason: "not_crossed" };

    return {
      triggered: true,
      reason: "price_crossed",
      price: px,
      hysteresis: hyst,
      atMs: opts.nowMs != null ? Number(opts.nowMs) : Date.now()
    };
  }

  function applyTrigger(bpState, triggerResult) {
    if (!bpState || !triggerResult || !triggerResult.triggered) return bpState;
    var next = Object.assign({}, bpState);
    next.triggered = true;
    next.status = "TRIGGERED";
    next.triggeredAtMs = triggerResult.atMs;
    next.sessionId = createSessionId(bpState.symbol, triggerResult.atMs * 1e6);
    next.updatedAtMs = triggerResult.atMs;
    return next;
  }

  function rearmAfterMove(bpState, newPrice, opts) {
    opts = opts || {};
    var tick = num(bpState && bpState.tickSize) || defaultTickSize(opts.symbol || (bpState && bpState.symbol));
    var price = roundToTick(newPrice, tick);
    var base = Object.assign({}, bpState || {}, {
      price: price,
      tickSize: tick,
      triggered: false,
      sessionId: null,
      triggeredAtMs: null,
      updatedAtMs: opts.nowMs != null ? Number(opts.nowMs) : Date.now()
    });
    var direction = attackDirection(price, opts.refPrice);
    base.direction = direction;
    base.wallSide = expectedWallSide(direction);
    base.armedCycleId = createSessionId(base.symbol, base.updatedAtMs * 1e6);
    var target = selectTargetWall({
      breakpoint: price,
      refPrice: opts.refPrice,
      direction: direction,
      walls: opts.walls || []
    });
    base.targetStatus = target.status;
    base.targetWall = target.wall;
    base.ambiguous = target.status === "AMBIGUOUS_TARGET";
    if (target.status === "NO_TARGET_WALL") {
      base.status = "NO_TARGET_WALL";
    } else if (target.status === "TARGET_LOCKED") {
      base.status = "TARGET_LOCKED";
    } else {
      base.status = "ARMED";
    }
    return base;
  }

  /**
   * Metrics-driven state transition (ASK path; BID mirrored).
   * metrics: {
   *   wallSide, wallReducePct, tradeExplainedPct, replenishPct,
   *   aggressorBuyShare, aggressorSellShare, priceResponseBps,
   *   acceptedAboveSec, acceptedBelowSec, dataGap, stale, wallLost,
   *   incompleteTrades, epochBoundary
   * }
   */
  function transitionDecision(prevState, metrics, thresholds) {
    metrics = metrics || {};
    var th = Object.assign({}, V1_PROVISIONAL, thresholds || {});
    var side = String(metrics.wallSide || "").toUpperCase();
    var reasons = [];

    if (metrics.dataGap) {
      return { state: "DATA_GAP", reasons: ["DATA_GAP"], tradeReady: false };
    }
    if (metrics.stale) {
      return { state: "STALE_DATA", reasons: ["STALE_DATA"], tradeReady: false };
    }
    if (metrics.wallLost) {
      return { state: "WALL_LOST", reasons: ["WALL_LOST"], tradeReady: false };
    }
    if (metrics.incompleteTrades || metrics.epochBoundary) {
      return {
        state: "NO_TRADE",
        reasons: metrics.epochBoundary ? ["EPOCH_BOUNDARY"] : ["INCOMPLETE_PUBLIC_TRADES"],
        tradeReady: false
      };
    }

    var reduce = num(metrics.wallReducePct);
    var explained = num(metrics.tradeExplainedPct);
    var replenish = num(metrics.replenishPct);
    var buyShare = num(metrics.aggressorBuyShare);
    var sellShare = num(metrics.aggressorSellShare);
    var resp = num(metrics.priceResponseBps);
    var accAbove = num(metrics.acceptedAboveSec) || 0;
    var accBelow = num(metrics.acceptedBelowSec) || 0;

    if (reduce != null && explained != null && reduce >= th.wall_consume_pct && explained < th.trade_explained_pct) {
      reasons.push("WALL_PULLED");
      return { state: "WALL_PULLED", reasons: reasons, tradeReady: false };
    }

    if (replenish != null && replenish > th.replenish_max_pct && reduce != null && reduce >= 0.3) {
      reasons.push("WALL_REPLENISHED");
      if (side === "ASK" && buyShare != null && buyShare >= th.aggressor_share && resp != null && Math.abs(resp) < 3) {
        reasons.push("BUY_AGGRESSION_HIGH", "PRICE_RESPONSE_LOW");
        if (accBelow >= th.acceptance_sec) {
          reasons.push("REJECTED_BELOW");
          return { state: "SHORT_READY", reasons: reasons, tradeReady: true };
        }
        return { state: "REJECTION_PENDING", reasons: reasons, tradeReady: false };
      }
      if (side === "BID" && sellShare != null && sellShare >= th.aggressor_share && resp != null && Math.abs(resp) < 3) {
        reasons.push("SELL_AGGRESSION_HIGH", "PRICE_RESPONSE_LOW");
        if (accAbove >= th.acceptance_sec) {
          reasons.push("REJECTED_ABOVE");
          return { state: "LONG_READY", reasons: reasons, tradeReady: true };
        }
        return { state: "REJECTION_PENDING", reasons: reasons, tradeReady: false };
      }
      return { state: "WALL_REPLENISHED", reasons: reasons, tradeReady: false };
    }

    if (
      buyShare != null &&
      buyShare >= th.aggressor_share &&
      resp != null &&
      Math.abs(resp) < 2 &&
      (explained == null || explained >= th.trade_explained_pct)
    ) {
      reasons.push("ABSORPTION", "HIGH_AGGRESSION_LOW_RESPONSE");
      return { state: "ABSORPTION", reasons: reasons, tradeReady: false };
    }

    if (reduce != null && reduce >= th.wall_consume_pct && explained != null && explained >= th.trade_explained_pct) {
      reasons.push(side === "ASK" ? "ASK_CONSUMED_BY_TRADES" : "BID_CONSUMED_BY_TRADES");
      if (replenish != null && replenish <= th.replenish_max_pct) {
        reasons.push("LOW_REPLENISHMENT");
      }
      if (side === "ASK") {
        if (accAbove >= th.acceptance_sec) {
          reasons.push("ACCEPTED_ABOVE_" + th.acceptance_sec + "S");
          if (metrics.retestHeld) reasons.push("RETEST_HELD");
          return { state: "LONG_READY", reasons: reasons, tradeReady: true };
        }
        return { state: "BREAKOUT_PENDING", reasons: reasons.concat(["WAITING_ACCEPTANCE"]), tradeReady: false };
      }
      if (side === "BID") {
        if (accBelow >= th.acceptance_sec) {
          reasons.push("ACCEPTED_BELOW_" + th.acceptance_sec + "S");
          if (metrics.retestHeld) reasons.push("RETEST_HELD");
          return { state: "SHORT_READY", reasons: reasons, tradeReady: true };
        }
        return { state: "BREAKOUT_PENDING", reasons: reasons.concat(["WAITING_ACCEPTANCE"]), tradeReady: false };
      }
      return { state: "WALL_CONSUMED", reasons: reasons, tradeReady: false };
    }

    if (prevState === "TRIGGERED" || prevState === "WALL_ATTACK" || !prevState) {
      return { state: "WALL_ATTACK", reasons: ["ANALYSING"], tradeReady: false };
    }
    return { state: prevState || "WALL_ATTACK", reasons: reasons.length ? reasons : ["ANALYSING"], tradeReady: false };
  }

  function decisionTone(state) {
    if (state === "ARMED" || state === "TARGET_LOCKED" || state === "NO_TARGET_WALL") return "armed";
    if (state === "TRIGGERED" || state === "WALL_ATTACK" || state === "BREAKOUT_PENDING" || state === "REJECTION_PENDING" || state === "ABSORPTION") {
      return "analyse";
    }
    if (state === "LONG_READY") return "long";
    if (state === "SHORT_READY") return "short";
    if (state === "WALL_LOST") return "wall-lost";
    if (state === "DATA_GAP" || state === "STALE_DATA") return "data-bad";
    return "neutral";
  }

  function decisionLabel(state) {
    var map = {
      ARMED: "ARMED",
      TARGET_LOCKED: "TARGET LOCKED",
      NO_TARGET_WALL: "NO TARGET WALL",
      TRIGGERED: "TRIGGERED",
      WALL_ATTACK: "ANALYSE LÄUFT",
      BREAKOUT_PENDING: "ANALYSE LÄUFT",
      REJECTION_PENDING: "ANALYSE LÄUFT",
      ABSORPTION: "ANALYSE LÄUFT",
      WALL_CONSUMED: "ANALYSE LÄUFT",
      WALL_PULLED: "NO TRADE",
      WALL_REPLENISHED: "ANALYSE LÄUFT",
      LONG_READY: "LONG READY",
      SHORT_READY: "SHORT READY",
      NO_TRADE: "NO TRADE",
      WALL_LOST: "WALL LOST",
      DATA_GAP: "DATA GAP",
      STALE_DATA: "STALE DATA",
      CANCELLED: "CANCELLED",
      EXPIRED: "EXPIRED",
      DISARMED: "DISARMED"
    };
    return map[state] || state || "N/A";
  }

  function loadStore(raw) {
    if (!raw) return { bySymbol: {}, version: 1 };
    try {
      var obj = typeof raw === "string" ? JSON.parse(raw) : raw;
      if (!obj || typeof obj !== "object") return { bySymbol: {}, version: 1 };
      if (!obj.bySymbol || typeof obj.bySymbol !== "object") obj.bySymbol = {};
      return obj;
    } catch (e) {
      return { bySymbol: {}, version: 1 };
    }
  }

  function saveSymbolBreakpoint(store, symbol, bpState) {
    var next = loadStore(store);
    var sym = String(symbol || "").toUpperCase();
    if (!sym) return next;
    if (!bpState) {
      delete next.bySymbol[sym];
    } else {
      next.bySymbol[sym] = {
        price: bpState.price,
        tickSize: bpState.tickSize,
        status: bpState.status,
        direction: bpState.direction,
        wallSide: bpState.wallSide,
        targetWall: bpState.targetWall,
        targetStatus: bpState.targetStatus,
        createdAtMs: bpState.createdAtMs,
        updatedAtMs: bpState.updatedAtMs,
        armedCycleId: bpState.armedCycleId
      };
    }
    next.version = 1;
    return next;
  }

  function blocksTradeReady(state) {
    return !!TERMINAL_NO_TRADE[state] || state === "WALL_PULLED" || state === "ABSORPTION";
  }

  return {
    STORAGE_KEY: STORAGE_KEY,
    PANEL_POS_KEY: PANEL_POS_KEY,
    RULE_VERSION: RULE_VERSION,
    V1_PROVISIONAL: V1_PROVISIONAL,
    STATES: STATES,
    num: num,
    roundToTick: roundToTick,
    defaultTickSize: defaultTickSize,
    attackDirection: attackDirection,
    expectedWallSide: expectedWallSide,
    selectTargetWall: selectTargetWall,
    createSessionId: createSessionId,
    createBreakpointState: createBreakpointState,
    evaluateTrigger: evaluateTrigger,
    applyTrigger: applyTrigger,
    rearmAfterMove: rearmAfterMove,
    transitionDecision: transitionDecision,
    decisionTone: decisionTone,
    decisionLabel: decisionLabel,
    loadStore: loadStore,
    saveSymbolBreakpoint: saveSymbolBreakpoint,
    blocksTradeReady: blocksTradeReady
  };
});
