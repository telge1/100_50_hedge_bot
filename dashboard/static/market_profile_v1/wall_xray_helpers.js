/**
 * Manual Wall X-Ray V1 — pure helpers (Node + browser).
 * Reuses Major/Q95 classification from MpWallDecisionHelpers.
 * No network, no DOM, no orders.
 */
(function (root, factory) {
  var api = factory(root && root.MpWallDecisionHelpers);
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  if (root) {
    root.MpWallXrayHelpers = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function (WD) {
  "use strict";

  var RULE_VERSION = "wall_xray_v1_provisional";
  var XRAY_STORAGE_KEY = "mp_v1_wall_xray";
  var CLUSTER_TICKS = 5;
  var CONTACT_BPS = 2.0;
  var APPROACH_BPS = 15.0;

  var STATES = Object.freeze([
    "DISARMED",
    "XRAY_TARGET_LOCKED",
    "MONITORING",
    "APPROACHING",
    "CONTACT",
    "DEFENDED",
    "CONSUMED",
    "PULLED",
    "REPLENISHED",
    "ACCEPTED_THROUGH",
    "RECLAIMED",
    "WALL_LOST",
    "DATA_GAP",
    "STOPPED",
    "AMBIGUOUS_TARGET_CLICK",
    "NO_MAJOR_WALL_AT_CLICK",
    "PRIMARY_WALL_LOST"
  ]);

  function num(v) {
    var n = Number(v);
    return Number.isFinite(n) ? n : null;
  }

  function H() {
    if (WD) return WD;
    if (typeof require === "function") {
      try {
        return require("./wall_decision_helpers.js");
      } catch (e) {
        return null;
      }
    }
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

  function classifyUniverse(walls, opts) {
    var helpers = H();
    if (!helpers || typeof helpers.classifyMajorWalls !== "function") {
      return [];
    }
    return helpers.classifyMajorWalls(walls || [], opts || {});
  }

  /**
   * Cluster adjacent levels on the same side within clusterTicks.
   */
  function clusterWalls(walls, opts) {
    opts = opts || {};
    var tick = num(opts.tickSize) || 0.1;
    var clusterTicks = num(opts.clusterTicks) != null ? num(opts.clusterTicks) : CLUSTER_TICKS;
    var tol = clusterTicks * tick;
    var classified = classifyUniverse(walls, opts);
    var bySide = { BID: [], ASK: [] };
    classified.forEach(function (w) {
      if (!w || (w.side !== "BID" && w.side !== "ASK")) return;
      bySide[w.side].push(w);
    });
    function clusterSide(side, list) {
      list.sort(function (a, b) {
        return (num(a.price) || 0) - (num(b.price) || 0);
      });
      var out = [];
      var cur = null;
      for (var i = 0; i < list.length; i += 1) {
        var w = list[i];
        var p = num(w.price);
        if (p == null) continue;
        if (!cur) {
          cur = {
            id: "cluster:" + side + ":" + p,
            side: side,
            members: [w],
            zone_lo: num(w.zone_lo) != null ? num(w.zone_lo) : p,
            zone_hi: num(w.zone_hi) != null ? num(w.zone_hi) : p,
            price: p,
            qty: num(w.qty) || 0,
            notional: wallNotional(w) || 0,
            strongest: w,
            level_count: 1,
            major: !!w.major,
            percentile: num(w.percentile),
            majorRule: w.majorRule || null,
            snapshot_ts: w.snapshot_ts,
            data_age_ms: w.data_age_ms,
            symbol: w.symbol || null
          };
          continue;
        }
        var lo = Math.min(cur.zone_lo, num(w.zone_lo) != null ? num(w.zone_lo) : p);
        var hi = Math.max(cur.zone_hi, num(w.zone_hi) != null ? num(w.zone_hi) : p);
        var gap = Math.min(Math.abs(p - cur.zone_hi), Math.abs(p - cur.zone_lo), Math.abs(p - cur.price));
        if (gap <= tol + 1e-12) {
          cur.members.push(w);
          cur.zone_lo = lo;
          cur.zone_hi = hi;
          cur.qty += num(w.qty) || 0;
          cur.notional += wallNotional(w) || 0;
          cur.level_count += 1;
          if ((wallNotional(w) || 0) >= (wallNotional(cur.strongest) || 0)) {
            cur.strongest = w;
            cur.price = p;
            cur.percentile = num(w.percentile);
            cur.majorRule = w.majorRule || cur.majorRule;
          }
          cur.major = cur.major || !!w.major;
          if (w.snapshot_ts != null) cur.snapshot_ts = w.snapshot_ts;
          if (w.data_age_ms != null) cur.data_age_ms = w.data_age_ms;
        } else {
          cur.price = (cur.zone_lo + cur.zone_hi) / 2;
          out.push(cur);
          cur = {
            id: "cluster:" + side + ":" + p,
            side: side,
            members: [w],
            zone_lo: num(w.zone_lo) != null ? num(w.zone_lo) : p,
            zone_hi: num(w.zone_hi) != null ? num(w.zone_hi) : p,
            price: p,
            qty: num(w.qty) || 0,
            notional: wallNotional(w) || 0,
            strongest: w,
            level_count: 1,
            major: !!w.major,
            percentile: num(w.percentile),
            majorRule: w.majorRule || null,
            snapshot_ts: w.snapshot_ts,
            data_age_ms: w.data_age_ms,
            symbol: w.symbol || null
          };
        }
      }
      if (cur) {
        cur.price = (cur.zone_lo + cur.zone_hi) / 2;
        out.push(cur);
      }
      return out;
    }
    return clusterSide("BID", bySide.BID).concat(clusterSide("ASK", bySide.ASK));
  }

  function distanceBps(price, ref) {
    var p = num(price);
    var r = num(ref);
    if (p == null || r == null || r === 0) return null;
    return ((p - r) / r) * 10000;
  }

  function absDistance(price, ref) {
    var p = num(price);
    var r = num(ref);
    if (p == null || r == null) return null;
    return Math.abs(p - r);
  }

  /**
   * Unique Major/Q95 match at click — no nearest outside tol; ambiguous if multi-hit.
   */
  function matchXrayClick(candidates, price, tickSize, tolTicks) {
    var helpers = H();
    var px = num(price);
    var tick = num(tickSize) || 0.1;
    var tol =
      (num(tolTicks) != null ? num(tolTicks) : (helpers && helpers.V1_PROVISIONAL ? 5 : 5)) * tick;
    var list = (Array.isArray(candidates) ? candidates : []).filter(function (w) {
      return w && w.major === true;
    });
    if (px == null) {
      return { ok: false, wall: null, reason: "NO_MAJOR_WALL_AT_CLICK", status: "NO_MAJOR_WALL_AT_CLICK" };
    }
    if (!list.length) {
      return { ok: false, wall: null, reason: "NO_MAJOR_WALL_AT_CLICK", status: "NO_MAJOR_WALL_AT_CLICK" };
    }
    var hits = [];
    for (var i = 0; i < list.length; i += 1) {
      var w = list[i];
      var lo = num(w.zone_lo) != null ? num(w.zone_lo) : num(w.price);
      var hi = num(w.zone_hi) != null ? num(w.zone_hi) : num(w.price);
      if (lo == null || hi == null) continue;
      if (lo > hi) {
        var t = lo;
        lo = hi;
        hi = t;
      }
      if (px >= lo - tol && px <= hi + tol) {
        hits.push({
          wall: w,
          dist: Math.min(Math.abs(num(w.price) - px), Math.abs((lo + hi) / 2 - px))
        });
      }
    }
    if (!hits.length) {
      return { ok: false, wall: null, reason: "NO_MAJOR_WALL_AT_CLICK", status: "NO_MAJOR_WALL_AT_CLICK" };
    }
    hits.sort(function (a, b) {
      if (a.dist !== b.dist) return a.dist - b.dist;
      return (num(b.wall.notional) || 0) - (num(a.wall.notional) || 0);
    });
    // Multiple in-tolerance hits: never nearest-pick.
    if (hits.length >= 2) {
      return {
        ok: false,
        wall: null,
        reason: "AMBIGUOUS_TARGET_CLICK",
        status: "AMBIGUOUS_TARGET_CLICK",
        hits: hits.slice(0, 5).map(function (h) {
          return h.wall;
        })
      };
    }
    return {
      ok: true,
      wall: hits[0].wall,
      reason: "matched_major_wall",
      status: "XRAY_TARGET_LOCKED",
      hits: 1
    };
  }

  function buildWallRadar(opts) {
    opts = opts || {};
    var live = num(opts.livePrice);
    var sortMode = String(opts.sortMode || "distance");
    var clusters = clusterWalls(opts.walls || [], opts);
    var asks = [];
    var bids = [];
    clusters.forEach(function (c) {
      if (!c) return;
      var distAbs = absDistance(c.price, live);
      var distBps = distanceBps(c.price, live);
      var row = Object.assign({}, c, {
        distance_abs: distAbs,
        distance_bps: distBps,
        freshness_ms: c.data_age_ms,
        wall_trend: opts.trends && opts.trends[c.id] ? opts.trends[c.id] : "stable",
        is_major: !!c.major,
        badge: c.major ? "MAJOR" : "CONTEXT"
      });
      if (c.side === "ASK" && live != null && num(c.price) != null && num(c.price) >= live) {
        asks.push(row);
      } else if (c.side === "BID" && live != null && num(c.price) != null && num(c.price) <= live) {
        bids.push(row);
      }
    });
    function sortList(list) {
      if (sortMode === "size") {
        list.sort(function (a, b) {
          return (num(b.notional) || 0) - (num(a.notional) || 0);
        });
      } else {
        list.sort(function (a, b) {
          var da = num(a.distance_abs);
          var db = num(b.distance_abs);
          if (da == null && db == null) return 0;
          if (da == null) return 1;
          if (db == null) return -1;
          return da - db;
        });
      }
      return list;
    }
    sortList(asks);
    sortList(bids);
    function markDominant(list) {
      var best = null;
      list.forEach(function (r) {
        if (!r.major) return;
        if (!best || (num(r.notional) || 0) > (num(best.notional) || 0)) best = r;
      });
      if (best) best.badge = "DOMINANT";
      return best;
    }
    return {
      ask: asks,
      bid: bids,
      dominant_ask: markDominant(asks),
      dominant_bid: markDominant(bids),
      sortMode: sortMode
    };
  }

  function assignWallRoles(opts) {
    opts = opts || {};
    var live = num(opts.livePrice);
    var primary = opts.primary || null;
    var radar = opts.radar || buildWallRadar(opts);
    var side = primary && String(primary.side || "").toUpperCase();
    var path = side === "ASK" ? radar.ask : side === "BID" ? radar.bid : [];
    var front = null;
    for (var i = 0; i < path.length; i += 1) {
      if (path[i].major || path[i].is_major) {
        front = path[i];
        break;
      }
    }
    if (!front && path.length) front = path[0];
    var backstop = null;
    if (primary && path.length) {
      var pPrice = num(primary.price);
      for (var j = 0; j < path.length; j += 1) {
        var w = path[j];
        if (!w.major && !w.is_major) continue;
        if (primary.id && w.id === primary.id) continue;
        if (side === "ASK" && num(w.price) > pPrice) {
          backstop = w;
          break;
        }
        if (side === "BID" && num(w.price) < pPrice) {
          backstop = w;
          break;
        }
      }
    }
    var dominant = side === "ASK" ? radar.dominant_ask : side === "BID" ? radar.dominant_bid : null;
    var largerBehind = false;
    var sizeRatio = null;
    if (primary && backstop) {
      var pn = wallNotional(primary) || 0;
      var bn = wallNotional(backstop) || 0;
      if (bn > pn && pn > 0) {
        largerBehind = true;
        sizeRatio = bn / pn;
      }
    }
    return {
      FRONT_WALL: front,
      PRIMARY_TARGET: primary,
      BACKSTOP_WALL: backstop,
      DOMINANT_WALL: dominant,
      LARGER_WALL_BEHIND: largerBehind,
      larger_wall_behind_ratio: sizeRatio,
      clear_path: !(backstop && largerBehind)
    };
  }

  function snapshotTarget(wall) {
    if (!wall) return null;
    return {
      id: wall.id != null ? String(wall.id) : String(wall.side) + ":" + String(wall.price),
      side: String(wall.side || "").toUpperCase(),
      price: num(wall.price),
      zone_lo: num(wall.zone_lo) != null ? num(wall.zone_lo) : num(wall.price),
      zone_hi: num(wall.zone_hi) != null ? num(wall.zone_hi) : num(wall.price),
      qty: num(wall.qty),
      notional: wallNotional(wall),
      percentile: num(wall.percentile),
      major_rule: wall.majorRule || wall.major_rule || null,
      major: wall.major === true || wall.is_major === true,
      snapshot_ts: num(wall.snapshot_ts != null ? wall.snapshot_ts : wall.timestamp),
      level_count: wall.level_count != null ? wall.level_count : 1
    };
  }

  function createXraySession(opts) {
    opts = opts || {};
    var helpers = H();
    var symbol = String(opts.symbol || "").toUpperCase();
    var now = opts.nowMs != null ? Number(opts.nowMs) : Date.now();
    var target = snapshotTarget(opts.wall);
    if (!target || !target.major) {
      return { ok: false, session: null, reason: "not_major_q95" };
    }
    var sessionId =
      "wx1_" +
      symbol +
      "_" +
      (opts.tsNs != null ? String(opts.tsNs) : String(now) + "000000");
    var preRoll = opts.preRollAvailable === true;
    var session = {
      sessionId: sessionId,
      symbol: symbol,
      mode: "xray",
      status: "MONITORING",
      phase: "MONITORING",
      target: target,
      target_id: target.id,
      baselineQty: target.qty,
      baselineNotional: target.notional,
      minQtySeen: target.qty,
      createdAtMs: now,
      updatedAtMs: now,
      startedAtMs: now,
      preRollAvailable: preRoll,
      startNote: preRoll ? "XRAY_WITH_PREROLL" : "XRAY_STARTED_NOW",
      bias: "NO_TRADE",
      reasons: [preRoll ? "XRAY_WITH_PREROLL" : "XRAY_STARTED_NOW", "MONITORING"],
      tradeReady: false,
      roles: null,
      lastPrice: num(opts.livePrice),
      lastDistanceBps: null,
      approach: null,
      warnings: [],
      locked: true,
      manualPrimary: true
    };
    session.roles = assignWallRoles({
      livePrice: opts.livePrice,
      primary: target,
      walls: opts.walls || [],
      tickSize: opts.tickSize,
      radar: buildWallRadar({
        livePrice: opts.livePrice,
        walls: opts.walls || [],
        tickSize: opts.tickSize,
        sortMode: "distance"
      })
    });
    if (session.roles.LARGER_WALL_BEHIND) {
      session.warnings.push("LARGER_WALL_BEHIND");
      session.reasons.push("LARGER_WALL_BEHIND");
    }
    return { ok: true, session: session, reason: "xray_locked" };
  }

  function computeApproach(prevDistBps, distBps) {
    var a = num(prevDistBps);
    var b = num(distBps);
    if (a == null || b == null) return null;
    var aa = Math.abs(a);
    var bb = Math.abs(b);
    if (bb < aa - 1e-9) return "approaching";
    if (bb > aa + 1e-9) return "moving_away";
    return "stable";
  }

  function computeBias(side, metrics, roles) {
    metrics = metrics || {};
    var reasons = [];
    var reduce = num(metrics.wallReducePct);
    var explained = num(metrics.tradeExplainedPct);
    var replenish = num(metrics.replenishPct);
    var pull = num(metrics.pullPct);
    var buyShare = num(metrics.aggressorBuyShare);
    var sellShare = num(metrics.aggressorSellShare);
    var accAbove = num(metrics.acceptedAboveSec) || 0;
    var accBelow = num(metrics.acceptedBelowSec) || 0;
    var s = String(side || "").toUpperCase();

    if (roles && roles.LARGER_WALL_BEHIND) {
      reasons.push("LARGER_WALL_BEHIND");
    }

    if (reduce != null && explained != null && reduce >= 0.65 && explained < 0.6) {
      reasons.push("PULL_WITHOUT_EXPLAINED_TRADES");
      return { bias: "NO_TRADE", reasons: reasons.concat(["PULL_RISK"]), tradeReady: false };
    }

    if (s === "ASK") {
      if (
        replenish != null &&
        replenish > 0.25 &&
        buyShare != null &&
        buyShare >= 0.65 &&
        (num(metrics.priceResponseBps) == null || Math.abs(num(metrics.priceResponseBps)) < 3)
      ) {
        reasons.push("ASK_DEFENDED_ABSORBING_BUYS");
        return { bias: "SHORT_BIAS", reasons: reasons, tradeReady: false };
      }
      if (reduce != null && reduce >= 0.65 && explained != null && explained >= 0.6) {
        reasons.push("ASK_CONSUMED_BY_TRADES");
        if (roles && roles.LARGER_WALL_BEHIND) {
          reasons.push("NO_CLEAR_PATH");
          return { bias: "NO_TRADE", reasons: reasons, tradeReady: false };
        }
        if (accAbove >= 15) {
          reasons.push("ACCEPTED_ABOVE");
          return { bias: "LONG_BIAS", reasons: reasons, tradeReady: false };
        }
        return { bias: "NO_TRADE", reasons: reasons.concat(["WAITING_ACCEPTANCE"]), tradeReady: false };
      }
    }

    if (s === "BID") {
      if (
        replenish != null &&
        replenish > 0.25 &&
        sellShare != null &&
        sellShare >= 0.65 &&
        (num(metrics.priceResponseBps) == null || Math.abs(num(metrics.priceResponseBps)) < 3)
      ) {
        reasons.push("BID_DEFENDED_ABSORBING_SELLS");
        return { bias: "LONG_BIAS", reasons: reasons, tradeReady: false };
      }
      if (reduce != null && reduce >= 0.65 && explained != null && explained >= 0.6) {
        reasons.push("BID_CONSUMED_BY_TRADES");
        if (roles && roles.LARGER_WALL_BEHIND) {
          reasons.push("NO_CLEAR_PATH");
          return { bias: "NO_TRADE", reasons: reasons, tradeReady: false };
        }
        if (accBelow >= 15) {
          reasons.push("ACCEPTED_BELOW");
          return { bias: "SHORT_BIAS", reasons: reasons, tradeReady: false };
        }
        return { bias: "NO_TRADE", reasons: reasons.concat(["WAITING_ACCEPTANCE"]), tradeReady: false };
      }
    }

    if (pull != null && pull > 0.5 && (explained == null || explained < 0.3)) {
      reasons.push("PULL_RISK");
      return { bias: "NO_TRADE", reasons: reasons, tradeReady: false };
    }

    return { bias: "NO_TRADE", reasons: reasons.length ? reasons : ["ANALYSING"], tradeReady: false };
  }

  function transitionXray(session, livePrice, metrics, opts) {
    opts = opts || {};
    if (!session || !session.target) {
      return { session: session, changed: false };
    }
    metrics = metrics || {};
    var next = Object.assign({}, session);
    next.updatedAtMs = opts.nowMs != null ? Number(opts.nowMs) : Date.now();
    var live = num(livePrice);
    next.lastPrice = live;
    var distBps = distanceBps(
      (num(session.target.zone_lo) + num(session.target.zone_hi)) / 2,
      live
    );
    if (session.target.side === "ASK") {
      distBps = distanceBps(num(session.target.zone_lo), live);
    } else {
      distBps = distanceBps(num(session.target.zone_hi), live);
    }
    next.approach = computeApproach(session.lastDistanceBps, distBps);
    next.lastDistanceBps = distBps;
    next.distance_abs = absDistance(
      (num(session.target.zone_lo) + num(session.target.zone_hi)) / 2,
      live
    );
    next.distance_bps = distBps;
    next.speed_bps_s = num(metrics.speedBpsS != null ? metrics.speedBpsS : metrics.speed_bps_s);

    if (metrics.dataGap) {
      next.status = "DATA_GAP";
      next.phase = "DATA_GAP";
      next.bias = "NO_TRADE";
      next.reasons = ["DATA_GAP"];
      return { session: next, changed: true };
    }
    if (metrics.wallLost || opts.primaryLost) {
      next.status = "PRIMARY_WALL_LOST";
      next.phase = "WALL_LOST";
      next.bias = "NO_TRADE";
      next.reasons = ["PRIMARY_WALL_LOST"];
      return { session: next, changed: true };
    }

    var absBps = distBps != null ? Math.abs(distBps) : null;
    var phase = "MONITORING";
    if (absBps != null && absBps <= CONTACT_BPS) phase = "CONTACT";
    else if (absBps != null && absBps <= APPROACH_BPS && next.approach === "approaching") {
      phase = "APPROACHING";
    }

    var reduce = num(metrics.wallReducePct);
    var explained = num(metrics.tradeExplainedPct);
    var replenish = num(metrics.replenishPct);
    if (phase === "CONTACT") {
      if (reduce != null && explained != null && reduce >= 0.65 && explained < 0.6) {
        phase = "PULLED";
      } else if (reduce != null && explained != null && reduce >= 0.65 && explained >= 0.6) {
        phase = "CONSUMED";
      } else if (replenish != null && replenish > 0.25) {
        phase = "REPLENISHED";
      } else if (replenish != null && reduce != null && reduce < 0.3) {
        phase = "DEFENDED";
      }
      var accAbove = num(metrics.acceptedAboveSec) || 0;
      var accBelow = num(metrics.acceptedBelowSec) || 0;
      if (phase === "CONSUMED" && session.target.side === "ASK" && accAbove >= 15) {
        phase = "ACCEPTED_THROUGH";
      }
      if (phase === "CONSUMED" && session.target.side === "BID" && accBelow >= 15) {
        phase = "ACCEPTED_THROUGH";
      }
    }

    next.phase = phase;
    next.status = phase === "MONITORING" || phase === "APPROACHING" || phase === "CONTACT" ? phase : phase;
    if (next.status === "XRAY_TARGET_LOCKED") next.status = "MONITORING";

    var bias = computeBias(session.target.side, metrics, session.roles);
    // Lock alone never produces directional ready
    if (!metrics || (phase === "MONITORING" && reduce == null)) {
      bias = { bias: "NO_TRADE", reasons: (session.reasons || []).slice(0, 2).concat(["NO_BIAS_ON_LOCK_ONLY"]), tradeReady: false };
    }
    next.bias = bias.bias;
    next.reasons = bias.reasons;
    next.tradeReady = false;
    if (session.roles && session.roles.LARGER_WALL_BEHIND && phase === "CONSUMED") {
      next.warnings = (next.warnings || []).concat(["LARGER_WALL_BEHIND", "NO_CLEAR_PATH"]);
    }
    return { session: next, changed: true };
  }

  function refreshPrimaryPresence(session, walls, opts) {
    opts = opts || {};
    if (!session || !session.target) return { session: session, lost: false };
    var clusters = clusterWalls(walls || [], opts);
    var still = null;
    for (var i = 0; i < clusters.length; i += 1) {
      var c = clusters[i];
      if (!c) continue;
      if (String(c.id) === String(session.target.id)) {
        still = c;
        break;
      }
      if (
        String(c.side) === String(session.target.side) &&
        Math.abs(num(c.price) - num(session.target.price)) <= (num(opts.tickSize) || 0.1) * 2
      ) {
        still = c;
        break;
      }
    }
    if (!still) {
      return {
        session: Object.assign({}, session, {
          status: "PRIMARY_WALL_LOST",
          phase: "WALL_LOST",
          reasons: ["PRIMARY_WALL_LOST"],
          bias: "NO_TRADE"
        }),
        lost: true
      };
    }
    var nextTarget = snapshotTarget(still);
    // keep original id/zone identity; update qty only
    nextTarget.id = session.target.id;
    nextTarget.zone_lo = session.target.zone_lo;
    nextTarget.zone_hi = session.target.zone_hi;
    return {
      session: Object.assign({}, session, {
        target: Object.assign({}, session.target, { qty: nextTarget.qty, notional: nextTarget.notional })
      }),
      lost: false
    };
  }

  function decisionLabel(state) {
    var map = {
      MONITORING: "XRAY ACTIVE · MONITORING",
      APPROACHING: "XRAY ACTIVE · APPROACHING",
      CONTACT: "XRAY ACTIVE · CONTACT",
      DEFENDED: "XRAY ACTIVE · DEFENDED",
      CONSUMED: "XRAY ACTIVE · CONSUMED",
      PULLED: "XRAY ACTIVE · PULLED",
      REPLENISHED: "XRAY ACTIVE · REPLENISHED",
      ACCEPTED_THROUGH: "XRAY ACTIVE · ACCEPTED",
      RECLAIMED: "XRAY ACTIVE · RECLAIMED",
      WALL_LOST: "WALL LOST",
      PRIMARY_WALL_LOST: "PRIMARY WALL LOST",
      DATA_GAP: "DATA GAP",
      STOPPED: "XRAY STOPPED",
      XRAY_TARGET_LOCKED: "XRAY TARGET LOCKED",
      AMBIGUOUS_TARGET_CLICK: "AMBIGUOUS TARGET",
      NO_MAJOR_WALL_AT_CLICK: "NO MAJOR WALL"
    };
    return map[state] || state || "N/A";
  }

  function decisionTone(state) {
    if (state === "DATA_GAP" || state === "WALL_LOST" || state === "PRIMARY_WALL_LOST") return "data-bad";
    if (state === "CONTACT" || state === "APPROACHING") return "analyse";
    if (state === "CONSUMED" || state === "ACCEPTED_THROUGH") return "long";
    if (state === "DEFENDED" || state === "REPLENISHED") return "short";
    return "armed";
  }

  return {
    RULE_VERSION: RULE_VERSION,
    XRAY_STORAGE_KEY: XRAY_STORAGE_KEY,
    STATES: STATES,
    CLUSTER_TICKS: CLUSTER_TICKS,
    num: num,
    clusterWalls: clusterWalls,
    matchXrayClick: matchXrayClick,
    buildWallRadar: buildWallRadar,
    assignWallRoles: assignWallRoles,
    createXraySession: createXraySession,
    transitionXray: transitionXray,
    refreshPrimaryPresence: refreshPrimaryPresence,
    computeBias: computeBias,
    computeApproach: computeApproach,
    distanceBps: distanceBps,
    snapshotTarget: snapshotTarget,
    decisionLabel: decisionLabel,
    decisionTone: decisionTone
  };
});
