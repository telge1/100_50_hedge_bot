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
    major_wall_median_mult: 3.0,
    major_wall_q_percentile: 95
  });

  var STATES = Object.freeze([
    "DISARMED",
    "BP_SET_WAITING_TARGET",
    "TARGET_CANDIDATES_READY",
    "TARGET_LOCKED",
    "ARMED",
    "MISSED_TRIGGER",
    "TARGET_LOST_BEFORE_TRIGGER",
    "TARGET_MOVED",
    "TARGET_INVALID",
    "NO_TARGET_WALL",
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

  var WALL_PICK_TOL_TICKS = 5;

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

  function wallNotional(w) {
    if (!w) return null;
    var n = num(w.notional);
    if (n != null && n > 0) return n;
    var v = num(w.value);
    if (v != null && v > 0) return v;
    var p = num(w.price);
    var q = num(w.qty);
    if (p != null && q != null && q > 0) return p * q;
    return null;
  }

  function percentileRank(sortedAsc, value) {
    var v = num(value);
    if (v == null || !sortedAsc || !sortedAsc.length) return null;
    var le = 0;
    for (var i = 0; i < sortedAsc.length; i += 1) {
      if (sortedAsc[i] <= v) le += 1;
    }
    return (le / sortedAsc.length) * 100;
  }

  /**
   * Annotate walls with major/Q95 proof. Never invents major=true.
   * Major if explicit major OR source percentile>=Q OR notional percentile>=Q (per side).
   */
  function classifyMajorWalls(walls, opts) {
    opts = opts || {};
    var qGate =
      num(opts.qPercentile) != null
        ? num(opts.qPercentile)
        : V1_PROVISIONAL.major_wall_q_percentile;
    var mult =
      num(opts.medianMult) != null
        ? num(opts.medianMult)
        : V1_PROVISIONAL.major_wall_median_mult;
    var nowMs = opts.nowMs != null ? Number(opts.nowMs) : Date.now();
    var list = Array.isArray(walls) ? walls : [];

    var bySide = { BID: [], ASK: [] };
    for (var i = 0; i < list.length; i += 1) {
      var w0 = list[i];
      if (!w0) continue;
      var side0 = String(w0.side || "").toUpperCase();
      if (side0 !== "BID" && side0 !== "ASK") continue;
      var n0 = wallNotional(w0);
      if (n0 == null || n0 <= 0) continue;
      bySide[side0].push(n0);
    }
    bySide.BID.sort(function (a, b) { return a - b; });
    bySide.ASK.sort(function (a, b) { return a - b; });

    var qtysAll = list
      .map(function (w) { return num(w && w.qty); })
      .filter(function (q) { return q != null && q > 0; })
      .sort(function (a, b) { return a - b; });
    var medianQty = null;
    if (qtysAll.length) {
      var mid = Math.floor(qtysAll.length / 2);
      medianQty =
        qtysAll.length % 2
          ? qtysAll[mid]
          : (qtysAll[mid - 1] + qtysAll[mid]) / 2;
    }

    return list.map(function (w, idx) {
      if (!w) return null;
      var side = String(w.side || "").toUpperCase();
      var notional = wallNotional(w);
      var qty = num(w.qty);
      var price = num(w.price);
      var srcPct = num(w.percentile);
      var ratio = num(w.wall_ratio != null ? w.wall_ratio : w.ratio);
      var snapTs = num(w.timestamp != null ? w.timestamp : w.snapshot_ts);
      var snapMs =
        snapTs == null
          ? null
          : snapTs > 1e12
            ? snapTs
            : snapTs * 1000;
      var ageMs = snapMs != null ? Math.max(0, nowMs - snapMs) : null;
      var pct =
        notional != null && bySide[side]
          ? percentileRank(bySide[side], notional)
          : null;

      var majorRule = null;
      var isMajor = false;
      if (w.major === true) {
        isMajor = true;
        majorRule = "explicit_major";
      } else if (srcPct != null && srcPct >= qGate) {
        isMajor = true;
        majorRule = "source_percentile_q" + qGate;
      } else if (pct != null && pct + 1e-9 >= qGate) {
        isMajor = true;
        majorRule = "notional_q" + qGate;
      }

      var passesMedianMult =
        medianQty != null &&
        medianQty > 0 &&
        qty != null &&
        qty >= medianQty * mult;

      return {
        id: w.id != null ? String(w.id) : side + ":" + String(price) + ":" + idx,
        symbol: w.symbol != null ? String(w.symbol).toUpperCase() : null,
        side: side,
        price: price,
        qty: qty,
        notional: notional,
        zone_lo: num(w.zone_lo) != null ? num(w.zone_lo) : price,
        zone_hi: num(w.zone_hi) != null ? num(w.zone_hi) : price,
        wall_ratio: ratio,
        percentile: pct,
        source_percentile: srcPct,
        major: isMajor,
        majorRule: majorRule,
        passes_median_mult: !!passesMedianMult,
        snapshot_ts: snapTs,
        snapshot_ms: snapMs,
        data_age_ms: ageMs,
        relevant: isMajor
      };
    }).filter(Boolean);
  }

  /**
   * Selectable Major/Q95 walls in attack direction only (no auto-lock).
   */
  function listTargetCandidates(opts) {
    opts = opts || {};
    var bp = num(opts.breakpoint);
    var direction = opts.direction || attackDirection(bp, opts.refPrice);
    var side = expectedWallSide(direction);
    var walls = Array.isArray(opts.walls) ? opts.walls : [];
    var qGate =
      num(opts.qPercentile) != null
        ? num(opts.qPercentile)
        : V1_PROVISIONAL.major_wall_q_percentile;

    if (!side || bp == null) {
      return {
        status: "NO_TARGET_WALL",
        candidates: [],
        direction: direction,
        side: side,
        reason: "no_direction",
        qPercentile: qGate
      };
    }

    var classified = classifyMajorWalls(walls, {
      qPercentile: qGate,
      medianMult: opts.medianMult,
      nowMs: opts.nowMs
    });

    var candidates = classified.filter(function (w) {
      if (!w || w.side !== side) return false;
      if (!w.major) return false;
      var p = num(w.price);
      if (p == null) return false;
      if (direction === "UP_ASK" && !(p >= bp)) return false;
      if (direction === "DOWN_BID" && !(p <= bp)) return false;
      var q = num(w.qty);
      return q != null && q > 0;
    }).map(function (w) {
      return Object.assign({}, w, {
        bp_direction: direction,
        relative_to_bp:
          direction === "UP_ASK"
            ? "ask_at_or_above_bp"
            : direction === "DOWN_BID"
              ? "bid_at_or_below_bp"
              : null
      });
    });

    candidates.sort(function (a, b) {
      var pa = Math.abs(num(a.price) - bp);
      var pb = Math.abs(num(b.price) - bp);
      if (pa !== pb) return pa - pb;
      return (num(b.notional) || 0) - (num(a.notional) || 0);
    });

    return {
      status: candidates.length ? "TARGET_CANDIDATES_READY" : "NO_TARGET_WALL",
      candidates: candidates,
      direction: direction,
      side: side,
      reason: candidates.length ? "major_q95_candidates_ready" : "no_major_q95_wall",
      qPercentile: qGate,
      universe_count: classified.length,
      major_count: classified.filter(function (w) { return w.major; }).length
    };
  }

  function matchWallAtPrice(candidates, price, tickSize, tolTicks) {
    var px = num(price);
    var tick = num(tickSize) || 0.1;
    var tol = (num(tolTicks) != null ? num(tolTicks) : WALL_PICK_TOL_TICKS) * tick;
    var list = Array.isArray(candidates) ? candidates : [];
    if (px == null || !list.length) {
      return { ok: false, wall: null, reason: "no_candidates" };
    }
    var hits = [];
    for (var i = 0; i < list.length; i += 1) {
      var w = list[i];
      if (!w || !w.major) continue;
      var lo = num(w.zone_lo) != null ? num(w.zone_lo) : num(w.price);
      var hi = num(w.zone_hi) != null ? num(w.zone_hi) : num(w.price);
      if (lo == null || hi == null) continue;
      if (lo > hi) {
        var tmp = lo;
        lo = hi;
        hi = tmp;
      }
      var inZone = px >= lo - tol && px <= hi + tol;
      var nearPrice = Math.abs(num(w.price) - px) <= tol;
      if (!inZone && !nearPrice) continue;
      hits.push({
        wall: w,
        dist: Math.min(Math.abs(num(w.price) - px), Math.abs(((lo + hi) / 2) - px))
      });
    }
    if (!hits.length) {
      return { ok: false, wall: null, reason: "no_major_wall_at_price" };
    }
    hits.sort(function (a, b) {
      if (a.dist !== b.dist) return a.dist - b.dist;
      return (num(b.wall.notional) || 0) - (num(a.wall.notional) || 0);
    });
    return { ok: true, wall: hits[0].wall, reason: "matched_major_wall", hits: hits.length };
  }

  function normalizeWall(wall, side) {
    if (!wall) return null;
    var s = String(side || wall.side || "").toUpperCase();
    return {
      id: wall.id != null ? String(wall.id) : s + ":" + String(wall.price),
      side: s,
      price: num(wall.price),
      qty: num(wall.qty),
      notional: wallNotional(wall),
      zone_lo: num(wall.zone_lo) != null ? num(wall.zone_lo) : num(wall.price),
      zone_hi: num(wall.zone_hi) != null ? num(wall.zone_hi) : num(wall.price),
      major: wall.major === true,
      majorRule: wall.majorRule || null,
      percentile: num(wall.percentile),
      snapshot_ts: num(wall.snapshot_ts != null ? wall.snapshot_ts : wall.timestamp),
      relative_to_bp: wall.relative_to_bp || null
    };
  }

  /**
   * Diagnostics / fixtures only — live UI uses lockManualTarget.
   */
  function selectTargetWall(opts) {
    var listed = listTargetCandidates(opts);
    if (!listed.candidates.length) {
      return { status: "NO_TARGET_WALL", wall: null, reason: listed.reason || "no_major_q95_wall" };
    }
    var best = listed.candidates[0];
    var second = listed.candidates[1];
    var bp = num(opts && opts.breakpoint);
    if (second && bp != null) {
      var d0 = Math.abs(num(best.price) - bp);
      var d1 = Math.abs(num(second.price) - bp);
      var q0 = num(best.notional) || 0;
      var q1 = num(second.notional) || 0;
      if (d0 === d1 && Math.abs(q0 - q1) / Math.max(q0, q1, 1) < 0.05) {
        return {
          status: "AMBIGUOUS_TARGET",
          wall: normalizeWall(best, listed.side),
          candidates: listed.candidates.slice(0, 3),
          reason: "ambiguous_nearest"
        };
      }
    }
    return {
      status: "TARGET_LOCKED",
      wall: normalizeWall(best, listed.side),
      candidates: listed.candidates,
      reason: "nearest_major"
    };
  }

  function createSessionId(symbol, tsNs) {
    var t = tsNs != null ? String(tsNs) : String(Date.now()) + "000000";
    return "wd1_" + String(symbol || "UNK").toUpperCase() + "_" + t;
  }

  /** Place BP only — never auto-locks a target. */
  function createBreakpointState(opts) {
    opts = opts || {};
    var symbol = String(opts.symbol || "").toUpperCase();
    var tick = num(opts.tickSize) != null ? num(opts.tickSize) : defaultTickSize(symbol);
    var price = roundToTick(opts.price, tick);
    var ref = num(opts.refPrice);
    var direction = attackDirection(price, ref);
    var now = opts.nowMs != null ? Number(opts.nowMs) : Date.now();
    var listed = listTargetCandidates({
      breakpoint: price,
      refPrice: ref,
      direction: direction,
      walls: opts.walls || [],
      medianMult: opts.medianMult
    });
    var status =
      listed.status === "TARGET_CANDIDATES_READY"
        ? "TARGET_CANDIDATES_READY"
        : "BP_SET_WAITING_TARGET";
    if (!direction) status = "BP_SET_WAITING_TARGET";
    return {
      symbol: symbol,
      price: price,
      tickSize: tick,
      direction: direction,
      wallSide: expectedWallSide(direction),
      status: status,
      targetStatus: listed.status,
      targetWall: null,
      targetCandidates: listed.candidates || [],
      ambiguous: false,
      createdAtMs: now,
      updatedAtMs: now,
      armedCycleId: createSessionId(symbol, now * 1e6),
      triggered: false,
      sessionId: null,
      manualTarget: true
    };
  }

  function lockManualTarget(bpState, wall, opts) {
    opts = opts || {};
    if (!bpState) return { ok: false, state: null, reason: "missing_bp" };
    var side = expectedWallSide(bpState.direction);
    var candidates = Array.isArray(opts.candidates)
      ? opts.candidates
      : bpState.targetCandidates || [];
    var picked = wall;
    if (!picked && opts.price != null) {
      var match = matchWallAtPrice(candidates, opts.price, bpState.tickSize, opts.tolTicks);
      if (!match.ok) return { ok: false, state: bpState, reason: match.reason };
      picked = match.wall;
    }
    if (!picked) return { ok: false, state: bpState, reason: "no_wall" };
    var norm = normalizeWall(picked, side || picked.side);
    if (!norm || !side || String(norm.side).toUpperCase() !== side) {
      return { ok: false, state: bpState, reason: "wrong_side" };
    }
    if (norm.major !== true && !(picked && picked.major === true)) {
      return { ok: false, state: bpState, reason: "not_major_q95" };
    }
    var allowed = false;
    for (var i = 0; i < candidates.length; i += 1) {
      var c = candidates[i];
      if (!c) continue;
      if (c.id != null && norm.id != null && String(c.id) === String(norm.id)) {
        allowed = true;
        break;
      }
      if (String(c.side).toUpperCase() === String(norm.side).toUpperCase() && num(c.price) === num(norm.price)) {
        allowed = true;
        break;
      }
    }
    if (!allowed) {
      return { ok: false, state: bpState, reason: "wall_not_in_candidates" };
    }
    var now = opts.nowMs != null ? Number(opts.nowMs) : Date.now();
    var next = Object.assign({}, bpState, {
      targetWall: norm,
      targetStatus: "TARGET_LOCKED",
      status: "ARMED",
      updatedAtMs: now,
      ambiguous: false
    });
    return { ok: true, state: next, reason: "manual_lock" };
  }

  function evaluateTrigger(bpState, livePrice, opts) {
    opts = opts || {};
    if (!bpState || bpState.triggered) {
      return { triggered: false, reason: "already_triggered_or_missing" };
    }
    if (bpState.status !== "ARMED" || !bpState.targetWall) {
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
    return createBreakpointState({
      symbol: (bpState && bpState.symbol) || opts.symbol,
      price: price,
      tickSize: tick,
      refPrice: opts.refPrice,
      walls: opts.walls || [],
      medianMult: opts.medianMult,
      nowMs: opts.nowMs
    });
  }

  function refreshTargetCandidates(bpState, walls, opts) {
    opts = opts || {};
    if (!bpState) return bpState;
    var listed = listTargetCandidates({
      breakpoint: bpState.price,
      refPrice: opts.refPrice,
      direction: bpState.direction,
      walls: walls || [],
      medianMult: opts.medianMult
    });
    var next = Object.assign({}, bpState, {
      targetCandidates: listed.candidates || [],
      updatedAtMs: opts.nowMs != null ? Number(opts.nowMs) : Date.now()
    });
    if (bpState.targetWall && bpState.status === "ARMED") {
      var still = false;
      for (var i = 0; i < listed.candidates.length; i += 1) {
        var c = listed.candidates[i];
        if (c && String(c.id) === String(bpState.targetWall.id)) {
          still = true;
          next.targetWall = normalizeWall(c, bpState.wallSide);
          break;
        }
      }
      if (!still) {
        next.status = "TARGET_LOST_BEFORE_TRIGGER";
        next.targetStatus = "TARGET_LOST_BEFORE_TRIGGER";
        next.targetWall = null;
      }
      return next;
    }
    if (!bpState.targetWall) {
      next.targetStatus = listed.status;
      next.status =
        listed.status === "TARGET_CANDIDATES_READY"
          ? "TARGET_CANDIDATES_READY"
          : "BP_SET_WAITING_TARGET";
    }
    return next;
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
    if (
      state === "ARMED" ||
      state === "TARGET_LOCKED" ||
      state === "TARGET_CANDIDATES_READY" ||
      state === "BP_SET_WAITING_TARGET"
    ) {
      return "armed";
    }
    if (
      state === "NO_TARGET_WALL" ||
      state === "TARGET_LOST_BEFORE_TRIGGER" ||
      state === "TARGET_MOVED" ||
      state === "TARGET_INVALID" ||
      state === "MISSED_TRIGGER"
    ) {
      return "neutral";
    }
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
      TARGET_CANDIDATES_READY: "BP SET · SELECT TARGET",
      BP_SET_WAITING_TARGET: "BP SET · SELECT TARGET",
      NO_TARGET_WALL: "NO TARGET WALL",
      TARGET_LOST_BEFORE_TRIGGER: "TARGET LOST",
      TARGET_MOVED: "TARGET MOVED",
      TARGET_INVALID: "TARGET INVALID",
      MISSED_TRIGGER: "MISSED TRIGGER",
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
        targetCandidates: bpState.targetCandidates || [],
        createdAtMs: bpState.createdAtMs,
        updatedAtMs: bpState.updatedAtMs,
        armedCycleId: bpState.armedCycleId,
        manualTarget: true
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
    classifyMajorWalls: classifyMajorWalls,
    listTargetCandidates: listTargetCandidates,
    matchWallAtPrice: matchWallAtPrice,
    lockManualTarget: lockManualTarget,
    refreshTargetCandidates: refreshTargetCandidates,
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
