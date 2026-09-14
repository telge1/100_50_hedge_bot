/**
 * Wall X-Ray V1 UI — Major/Q95 wall click starts read-only analysis (no BP required).
 * BP mode remains available via existing wall_decision_ui.js.
 */
(function (root) {
  "use strict";
  var H = root.MpWallDecisionHelpers;
  var X = root.MpWallXrayHelpers;
  if (!H || !X) return;

  var state = {
    toolActive: false,
    session: null,
    radar: null,
    radarSort: "distance",
    lastPrice: null,
    liveMetrics: null,
    panelOpen: false,
    analysisTimer: null,
    analysisInflight: false,
    acceptAboveMs: 0,
    acceptBelowMs: 0,
    lastAcceptTs: null,
    lastClickStatus: null
  };

  function $(id) {
    return document.getElementById(id);
  }

  function chartApi() {
    return root.chartApi || null;
  }

  function symbol() {
    var el = $("mpSymbol");
    return el ? String(el.value || "").toUpperCase() : "";
  }

  function fmtPct(v) {
    if (v == null || !Number.isFinite(Number(v))) return "DATA UNAVAILABLE";
    return (Number(v) * 100).toFixed(1) + "%";
  }

  /** Convert bps → percent display (1 bps = 0.01%). */
  function fmtBpsAsPct(bps, digits) {
    if (bps == null || !Number.isFinite(Number(bps))) return "DATA UNAVAILABLE";
    var d = digits == null ? 3 : digits;
    return (Number(bps) / 100).toFixed(d) + "%";
  }

  function fmtNum(v, digits) {
    if (v == null || !Number.isFinite(Number(v))) return "DATA UNAVAILABLE";
    return Number(v).toFixed(digits == null ? 2 : digits);
  }

  function wallsFromObp() {
    var api = chartApi();
    if (!api || typeof api.debugOrderbookProfile !== "function") return [];
    try {
      var dbg = api.debugOrderbookProfile();
      var bars = (dbg && dbg.bars) || [];
      return bars.map(function (b, i) {
        return {
          id: b.id || ((b.side || "UNK") + ":" + b.price + ":" + i),
          symbol: b.symbol || symbol(),
          side: String(b.side || "").toUpperCase(),
          price: Number(b.price),
          qty: Number(b.qty != null ? b.qty : b.size),
          notional: Number(b.notional != null ? b.notional : b.value),
          value: Number(b.value != null ? b.value : b.notional),
          zone_lo: b.zone_lo != null ? b.zone_lo : b.price,
          zone_hi: b.zone_hi != null ? b.zone_hi : b.price,
          wall_ratio: b.wall_ratio != null ? b.wall_ratio : b.ratio,
          percentile: b.percentile,
          timestamp: b.timestamp,
          major: b.major === true,
          source: "obp"
        };
      });
    } catch (e) {
      return [];
    }
  }

  function wallsFromLevels() {
    var api = chartApi();
    if (!api || typeof api.debugOrderbookLevels !== "function") return [];
    try {
      var dbg = api.debugOrderbookLevels();
      if (!dbg) return [];
      var out = [];
      function pushSide(side, levels) {
        (levels || []).forEach(function (lv, i) {
          var price = Number(lv && (lv.price != null ? lv.price : lv[0]));
          var qty = Number(lv && (lv.size != null ? lv.size : lv.qty != null ? lv.qty : lv[1]));
          if (!Number.isFinite(price) || !Number.isFinite(qty) || qty <= 0) return;
          out.push({
            id: "lvl:" + side + ":" + price + ":" + i,
            symbol: symbol(),
            side: side,
            price: price,
            qty: qty,
            notional: price * qty,
            value: price * qty,
            zone_lo: price,
            zone_hi: price,
            timestamp: dbg.timestamp_utc || dbg.timestamp,
            major: false,
            source: "levels"
          });
        });
      }
      pushSide("BID", dbg.bids || []);
      pushSide("ASK", dbg.asks || []);
      // Keep strongest levels for classification (perf).
      out.sort(function (a, b) {
        return (b.notional || 0) - (a.notional || 0);
      });
      return out.slice(0, 400);
    } catch (e) {
      return [];
    }
  }

  function bookDepth() {
    var bridge = root.__mpObBookBridge;
    if (bridge && typeof bridge.depth === "function") {
      var d = Number(bridge.depth());
      if (d === 0 || d === 1000 || d === 200) return d;
    }
    var el = $("mpOblDepth");
    if (!el) return 1000;
    var v = String(el.value || "1000");
    if (v === "0") return 0;
    if (v === "200") return 200;
    return 1000;
  }

  function bookDepthLabel() {
    var d = bookDepth();
    if (d === 0) return "FULL";
    if (d === 1000) return "OB1000";
    return "OB200";
  }

  function wallsUniverse() {
    var levels = wallsFromLevels();
    var obp = wallsFromObp();
    // Always merge: chart OBP majors + live Levels (OB1000/FULL).
    // FULL-only dropped OBP majors and left only CONTEXT rows → no lock possible.
    if (!obp.length) return levels;
    if (!levels.length) return obp;
    return obp.concat(levels);
  }

  function wallsFromChart() {
    return wallsUniverse();
  }

  function clientWallQtyForTarget(tw) {
    if (!tw) return null;
    var walls = wallsUniverse();
    var lo = Number(tw.zone_lo != null ? tw.zone_lo : tw.price);
    var hi = Number(tw.zone_hi != null ? tw.zone_hi : tw.price);
    if (!Number.isFinite(lo) || !Number.isFinite(hi)) return null;
    if (lo > hi) {
      var t = lo;
      lo = hi;
      hi = t;
    }
    var side = String(tw.side || "").toUpperCase();
    var sum = 0;
    var hit = false;
    walls.forEach(function (w) {
      if (!w || String(w.side || "").toUpperCase() !== side) return;
      var p = Number(w.price);
      var q = Number(w.qty);
      if (!Number.isFinite(p) || !Number.isFinite(q)) return;
      if (p >= lo - 1e-9 && p <= hi + 1e-9) {
        sum += q;
        hit = true;
      }
    });
    return hit ? sum : null;
  }

  function ensureBookFeed() {
    var bridge = root.__mpObBookBridge;
    if (!bridge) return;
    try {
      if (typeof bridge.enableLevels === "function") bridge.enableLevels();
      if (typeof bridge.ensureLease === "function") bridge.ensureLease(symbol());
      if (typeof bridge.refreshLevels === "function") bridge.refreshLevels();
    } catch (e) { /* ignore */ }
  }

  function readAvr() {
    var sym = String(symbol() || "").toUpperCase();
    try {
      var bridge = root.MpWallDecisionAvrContext;
      if (bridge && typeof bridge.setActiveSymbol === "function") bridge.setActiveSymbol(sym);
      if (bridge && typeof bridge.readStateName === "function") {
        var fromCtx = bridge.readStateName({ symbol: sym });
        if (fromCtx && fromCtx.status === "ok" && fromCtx.value) return fromCtx;
        return { value: null, status: "DATA_UNAVAILABLE", reason: (fromCtx && fromCtx.reason) || "DATA_UNAVAILABLE" };
      }
    } catch (e0) { /* ignore */ }
    if (sym !== "BTCUSDT") {
      return { value: null, status: "DATA_UNAVAILABLE", reason: "symbol_unsupported" };
    }
    return { value: null, status: "DATA_UNAVAILABLE" };
  }

  function readOiCurrent() {
    try {
      var oi = root.__mpLastOi;
      if (!oi || oi.value == null || !Number.isFinite(Number(oi.value))) return null;
      return Number(oi.value);
    } catch (e) {
      return null;
    }
  }

  function setToolActive(on) {
    state.toolActive = !!on;
    var btn = $("mpWallXrayTool");
    if (btn) btn.classList.toggle("active", state.toolActive);
    var api = chartApi();
    if (api && typeof api.setInteractionMode === "function") {
      api.setInteractionMode(state.toolActive ? "wall_xray" : "select");
    }
    if (state.toolActive) {
      ensureBookFeed();
      refreshRadar();
      syncVisuals();
    }
  }

  function majorCandidates() {
    var clusters = X.clusterWalls(wallsFromChart(), {
      tickSize: H.defaultTickSize(symbol()),
      nowMs: Date.now()
    });
    var majors = clusters.filter(function (c) {
      return c && c.major === true;
    });
    if (majors.length) return majors;
    // If Q95 yields none (sparse FULL sample), promote strongest per side so
    // contact/lock is possible without inventing random mid-book ticks.
    var bySide = { ASK: [], BID: [] };
    clusters.forEach(function (c) {
      if (!c || (c.side !== "ASK" && c.side !== "BID")) return;
      bySide[c.side].push(c);
    });
    var promoted = [];
    ["ASK", "BID"].forEach(function (side) {
      bySide[side].sort(function (a, b) {
        return (Number(b.notional) || 0) - (Number(a.notional) || 0);
      });
      bySide[side].slice(0, 3).forEach(function (c) {
        promoted.push(
          Object.assign({}, c, {
            major: true,
            is_major: true,
            majorRule: c.majorRule || "xray_top_size_promote",
            badge: c.badge === "DOMINANT" ? "DOMINANT" : "MAJOR"
          })
        );
      });
    });
    return promoted;
  }

  function maybeAutoLockOnContact(price) {
    if (!state.toolActive || state.session || price == null) return;
    var cands = majorCandidates();
    if (!cands.length) {
      state.lastClickStatus = "NO_MAJOR_WALL_NEAR_PRICE";
      return;
    }
    var live = Number(price);
    var best = null;
    var bestAbs = null;
    cands.forEach(function (w) {
      var ref =
        w.side === "ASK"
          ? Number(w.zone_lo != null ? w.zone_lo : w.price)
          : Number(w.zone_hi != null ? w.zone_hi : w.price);
      if (!Number.isFinite(ref) || ref === 0) return;
      // ASK only above/at live; BID only below/at live
      if (w.side === "ASK" && ref < live - 1e-9) return;
      if (w.side === "BID" && ref > live + 1e-9) return;
      var bps = (Math.abs(ref - live) / live) * 10000;
      if (bps > 2.5) return; // CONTACT band
      if (bestAbs == null || bps < bestAbs) {
        bestAbs = bps;
        best = w;
      }
    });
    if (!best) return;
    state.lastClickStatus = "AUTO_CONTACT_LOCK";
    startXrayFromWall(best);
  }

  function refreshRadar() {
    state.radar = X.buildWallRadar({
      livePrice: state.lastPrice,
      walls: wallsFromChart(),
      tickSize: H.defaultTickSize(symbol()),
      sortMode: state.radarSort,
      nowMs: Date.now()
    });
    if (state.session && state.session.target) {
      state.session.roles = X.assignWallRoles({
        livePrice: state.lastPrice,
        primary: state.session.target,
        walls: wallsFromChart(),
        tickSize: H.defaultTickSize(symbol()),
        radar: state.radar
      });
    }
  }

  function syncVisuals() {
    var api = chartApi();
    if (!api) return;
    var majors = majorCandidates();
    var locked = state.session && state.session.target;
    var highlightIds = [];
    majors.forEach(function (c) {
      if (c && c.id != null) highlightIds.push(String(c.id));
      if (c && Array.isArray(c.members)) {
        c.members.forEach(function (m) {
          if (m && m.id != null) highlightIds.push(String(m.id));
        });
      }
      if (c && c.strongest && c.strongest.id != null) {
        highlightIds.push(String(c.strongest.id));
      }
    });
    var lockedHighlight = null;
    if (locked) {
      lockedHighlight = String(locked.id);
      majors.forEach(function (c) {
        if (!c || String(c.id) !== String(locked.id)) return;
        if (c.strongest && c.strongest.id != null) lockedHighlight = String(c.strongest.id);
        else if (c.members && c.members[0] && c.members[0].id != null) {
          lockedHighlight = String(c.members[0].id);
        }
      });
    }
    if (typeof api.setWallTargetHighlight === "function") {
      api.setWallTargetHighlight({
        ids: highlightIds,
        lockedId: lockedHighlight
      });
    }
    if (typeof api.setWallTargetGuides === "function") {
      api.setWallTargetGuides({
        candidates: majors,
        lockedId: locked && locked.id
      });
    }
    if (typeof api.setXrayZone === "function") {
      if (locked) {
        api.setXrayZone({
          zone_lo: locked.zone_lo,
          zone_hi: locked.zone_hi,
          side: locked.side,
          title: "XRAY " + locked.side
        });
      } else if (typeof api.clearXrayZone === "function") {
        api.clearXrayZone();
      }
    }
    var stopBtn = $("xrStop");
    if (stopBtn) stopBtn.hidden = !state.session;
  }

  function stopAnalysisLoop() {
    if (state.analysisTimer) {
      clearInterval(state.analysisTimer);
      state.analysisTimer = null;
    }
  }

  function startAnalysisLoop() {
    stopAnalysisLoop();
    pollLiveMetrics();
    state.analysisTimer = setInterval(function () {
      pollLiveMetrics();
    }, 2000);
  }

  function stopXray(reason) {
    if (state.session) {
      state.session = Object.assign({}, state.session, {
        status: "STOPPED",
        phase: "STOPPED",
        reasons: [reason || "STOPPED"]
      });
      shadowPost();
    }
    state.session = null;
    state.liveMetrics = null;
    stopAnalysisLoop();
    syncVisuals();
    renderPanel();
  }

  function startXrayFromWall(wall) {
    if (state.session) {
      stopXray("REPLACED_BY_NEW_XRAY");
    }
    ensureBookFeed();
    var created = X.createXraySession({
      symbol: symbol(),
      wall: wall,
      livePrice: state.lastPrice,
      walls: wallsFromChart(),
      tickSize: H.defaultTickSize(symbol()),
      preRollAvailable: false,
      nowMs: Date.now()
    });
    if (!created.ok) {
      state.lastClickStatus = created.reason;
      renderPanel();
      return;
    }
    state.session = created.session;
    state.session.bookDepth = bookDepth();
    state.session.bookDepthLabel = bookDepthLabel();
    state.acceptAboveMs = 0;
    state.acceptBelowMs = 0;
    state.lastAcceptTs = Date.now();
    state.lastClickStatus = "XRAY_TARGET_LOCKED";
    openPanel();
    syncVisuals();
    renderPanel();
    shadowPost();
    startAnalysisLoop();
  }

  function onXrayClick(payload) {
    if (!state.toolActive && !(state.session && payload && payload.force)) return;
    var price = payload && payload.price;
    if (price == null) return;
    var cands = majorCandidates();
    var match = X.matchXrayClick(cands, price, H.defaultTickSize(symbol()), 5);
    state.lastClickStatus = match.status || match.reason;
    if (!match.ok) {
      openPanel();
      renderPanel();
      return;
    }
    startXrayFromWall(match.wall);
  }

  function updateAcceptance(price) {
    var tw = state.session && state.session.target;
    if (!tw || price == null) return;
    var now = Date.now();
    if (state.lastAcceptTs == null) state.lastAcceptTs = now;
    var dt = Math.max(0, (now - state.lastAcceptTs) / 1000);
    state.lastAcceptTs = now;
    var lo = Number(tw.zone_lo);
    var hi = Number(tw.zone_hi);
    if (price > hi) state.acceptAboveMs += dt * 1000;
    else if (price < lo) state.acceptBelowMs += dt * 1000;
  }

  function onPrice(price) {
    state.lastPrice = H.num(price);
    if (state.toolActive || state.session) {
      refreshRadar();
      syncVisuals();
    }
    if (state.toolActive && !state.session) {
      maybeAutoLockOnContact(state.lastPrice);
    }
    if (!state.session) return;
    updateAcceptance(state.lastPrice);
    var presence = X.refreshPrimaryPresence(state.session, wallsFromChart(), {
      tickSize: H.defaultTickSize(symbol())
    });
    state.session = presence.session;
    if (presence.lost) {
      shadowPost();
      renderPanel();
      return;
    }
    var tr = X.transitionXray(state.session, state.lastPrice, {
      wallReducePct: state.liveMetrics && state.liveMetrics.wall_reduce_pct,
      tradeExplainedPct: state.liveMetrics && state.liveMetrics.trade_explained_pct,
      replenishPct: state.liveMetrics && state.liveMetrics.replenish_pct,
      pullPct: state.liveMetrics && state.liveMetrics.pull_pct,
      aggressorBuyShare: state.liveMetrics && state.liveMetrics.aggressor_buy_share,
      aggressorSellShare: state.liveMetrics && state.liveMetrics.aggressor_sell_share,
      priceResponseBps: state.liveMetrics && state.liveMetrics.price_response_bps,
      speedBpsS: state.liveMetrics && state.liveMetrics.speed_bps_s,
      acceptedAboveSec: state.acceptAboveMs / 1000,
      acceptedBelowSec: state.acceptBelowMs / 1000,
      dataGap: state.liveMetrics && state.liveMetrics.data_gap,
      wallLost: state.liveMetrics && state.liveMetrics.wall_lost
    }, { nowMs: Date.now() });
    state.session = tr.session;
    renderPanel();
  }

  function pollLiveMetrics() {
    if (!state.session || !state.session.target || state.analysisInflight) return;
    state.analysisInflight = true;
    ensureBookFeed();
    var tw = state.session.target;
    var avrNow = readAvr();
    var bridge = root.__mpObBookBridge;
    var leaseId = bridge && typeof bridge.leaseId === "function" ? bridge.leaseId() : null;
    var clientQty = clientWallQtyForTarget(tw);
    var body = {
      symbol: state.session.symbol,
      breakpoint: tw.price,
      target_wall: {
        id: tw.id,
        side: tw.side,
        price: tw.price,
        zone_lo: tw.zone_lo,
        zone_hi: tw.zone_hi,
        qty: tw.qty,
        notional: tw.notional
      },
      baseline_qty: state.session.baselineQty,
      baseline_notional: state.session.baselineNotional,
      triggered_at: new Date(state.session.startedAtMs).toISOString(),
      trigger_price: state.lastPrice,
      live_price: state.lastPrice,
      accepted_above_sec: state.acceptAboveMs / 1000,
      accepted_below_sec: state.acceptBelowMs / 1000,
      min_qty_seen: state.session.minQtySeen,
      avr_state: avrNow.status === "ok" ? avrNow.value : null,
      oi_at_trigger: null,
      oi_current: readOiCurrent(),
      xray: true,
      preferred_depth: bookDepth() === 200 ? 1000 : bookDepth(),
      lease_id: leaseId,
      client_wall_qty: clientQty
    };
    fetch("/api/wall-decision/v1/live-metrics", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify(body)
    })
      .then(function (res) {
        return res.ok ? res.json() : null;
      })
      .then(function (payload) {
        if (!payload || payload.success === false) {
          // Soft-fail with client qty so UI is not blank at contact.
          if (clientQty != null) {
            state.liveMetrics = {
              wall_current_qty: clientQty,
              wall_reduce_pct:
                state.session.baselineQty > 0
                  ? Math.max(0, (state.session.baselineQty - clientQty) / state.session.baselineQty)
                  : null,
              trade_explained_pct: null,
              pull_pct: null,
              replenish_pct: null,
              data_gap: true,
              adapters: { wall_current: "ok", wall_source: "client_chart_wall" },
              errors: ["LIVE_METRICS_HTTP_FAILED"]
            };
            renderPanel();
            return;
          }
          state.session = Object.assign({}, state.session, {
            status: "DATA_GAP",
            phase: "DATA_GAP",
            reasons: ["LIVE_METRICS_HTTP_FAILED"],
            bias: "NO_TRADE"
          });
          renderPanel();
          return;
        }
        state.liveMetrics = payload;
        if (payload.wall_current_qty == null && clientQty != null) {
          payload.wall_current_qty = clientQty;
          if (state.session.baselineQty > 0) {
            payload.wall_reduce_pct = Math.max(
              0,
              (state.session.baselineQty - clientQty) / state.session.baselineQty
            );
          }
        }
        if (payload.wall_current_qty != null && state.session) {
          var cur = Number(payload.wall_current_qty);
          if (Number.isFinite(cur)) {
            if (state.session.minQtySeen == null || cur < state.session.minQtySeen) {
              state.session.minQtySeen = cur;
            }
          }
        }
        var metrics = {
          wallReducePct: payload.wall_reduce_pct,
          tradeExplainedPct: payload.trade_explained_pct,
          replenishPct: payload.replenish_pct,
          pullPct: payload.pull_pct,
          aggressorBuyShare: payload.aggressor_buy_share,
          aggressorSellShare: payload.aggressor_sell_share,
          priceResponseBps: payload.price_response_bps,
          speedBpsS: payload.speed_bps_s,
          acceptedAboveSec: payload.accepted_above_sec || state.acceptAboveMs / 1000,
          acceptedBelowSec: payload.accepted_below_sec || state.acceptBelowMs / 1000,
          dataGap: !!payload.data_gap,
          wallLost: !!payload.wall_lost
        };
        var tr = X.transitionXray(state.session, state.lastPrice, metrics, { nowMs: Date.now() });
        state.session = tr.session;
        renderPanel();
        shadowPost();
      })
      .catch(function () {
        if (clientQty != null) {
          state.liveMetrics = {
            wall_current_qty: clientQty,
            data_gap: true,
            adapters: { wall_source: "client_chart_wall" },
            errors: ["LIVE_METRICS_FETCH_ERROR"]
          };
          renderPanel();
          return;
        }
        state.session = Object.assign({}, state.session, {
          status: "DATA_GAP",
          phase: "DATA_GAP",
          reasons: ["LIVE_METRICS_FETCH_ERROR"],
          bias: "NO_TRADE"
        });
        renderPanel();
      })
      .then(function () {
        state.analysisInflight = false;
      });
  }

  function shadowPost() {
    if (!state.session || !state.session.sessionId) return;
    try {
      fetch("/api/wall-decision/v1/shadow", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({
          session_id: state.session.sessionId,
          symbol: state.session.symbol,
          breakpoint: state.session.target && state.session.target.price,
          target_wall: state.session.target,
          armed_at: state.session.createdAtMs
            ? new Date(state.session.createdAtMs).toISOString()
            : null,
          triggered_at: state.session.startedAtMs
            ? new Date(state.session.startedAtMs).toISOString()
            : null,
          state: state.session.status || state.session.phase,
          reason_codes: state.session.reasons || [],
          metrics: Object.assign({}, state.liveMetrics || {}, {
            xray: true,
            bias: state.session.bias,
            phase: state.session.phase,
            distance_bps: state.session.distance_bps,
            roles: state.session.roles,
            warnings: state.session.warnings,
            start_note: state.session.startNote
          }),
          data_gap: !!(state.liveMetrics && state.liveMetrics.data_gap)
        })
      }).catch(function () { /* non-fatal */ });
    } catch (e) { /* ignore */ }
  }

  function openPanel() {
    var panel = $("wdPanel");
    if (!panel) return;
    panel.hidden = false;
    state.panelOpen = true;
    var body = $("wdPanelBody");
    var minBtn = $("wdPanelMin");
    if (body && body.hidden) {
      // keep user collapse; only ensure panel chrome visible
    }
    renderPanel();
  }

  function closePanel() {
    var panel = $("wdPanel");
    if (panel) panel.hidden = true;
    state.panelOpen = false;
  }

  function renderRadarHtml(radar) {
    if (!radar) return "<div class='wd-note'>Wall Radar: keine Daten</div>";
    function list(title, rows) {
      var html = "<div class='xr-radar-side'><div class='xr-radar-h'>" + title + "</div>";
      if (!rows || !rows.length) {
        html += "<div class='wd-note'>–</div></div>";
        return html;
      }
      var maxN = 1;
      rows.forEach(function (r) {
        if ((r.notional || 0) > maxN) maxN = r.notional;
      });
      rows.slice(0, 8).forEach(function (r) {
        var w = Math.max(4, Math.round(((r.notional || 0) / maxN) * 100));
        html +=
          "<div class='xr-radar-row' data-xr-id='" +
          String(r.id).replace(/'/g, "") +
          "'>" +
          "<div class='xr-radar-meta'>" +
          (r.badge || "") +
          " · " +
          fmtNum(r.zone_lo, 1) +
          "–" +
          fmtNum(r.zone_hi, 1) +
          " · " +
          fmtBpsAsPct(r.distance_bps, 3) +
          " · p" +
          fmtNum(r.percentile, 0) +
          "</div>" +
          "<div class='xr-bar'><span style='width:" +
          w +
          "%'></span></div>" +
          "<div class='xr-radar-sub'>" +
          fmtNum(r.qty, 3) +
          " · " +
          fmtNum(r.notional, 0) +
          " USDT · " +
          (r.wall_trend || "stable") +
          "</div></div>";
      });
      html += "</div>";
      return html;
    }
    return (
      "<div class='xr-radar'>" +
      "<div class='xr-radar-tools'>" +
      "<button type='button' class='wd-icon-btn' id='xrSortDist'>Distanz</button>" +
      "<button type='button' class='wd-icon-btn' id='xrSortSize'>Größe</button>" +
      "</div>" +
      list("ASK ↑", radar.ask) +
      list("BID ↓", radar.bid) +
      "</div>"
    );
  }

  function renderPanel() {
    var body = $("wdPanelBody");
    var title = $("wdPanelState");
    if (!body) return;
    var s = state.session;
    var m = state.liveMetrics || {};
    var stateName = (s && (s.status || s.phase)) || state.lastClickStatus || "DISARMED";
    if (title) {
      title.textContent = s ? X.decisionLabel(stateName) : state.lastClickStatus || "XRAY IDLE";
      title.className = "wd-state wd-tone-" + X.decisionTone(stateName);
    }
    function row(k, v) {
      return (
        '<div class="wd-row"><span class="wd-k">' +
        k +
        '</span><span class="wd-v">' +
        (v == null || v === "" ? "N/A" : v) +
        "</span></div>"
      );
    }
    var tw = s && s.target;
    var roles = s && s.roles;
    var html = "";
    html += row("Mode", s ? "XRAY ACTIVE" : "XRAY IDLE");
    html += row("Wall-Quelle", bookDepthLabel() + (bookDepth() === 0 ? " (Levels)" : " (OBP+Levels)"));
    if (!s) {
      html += row("Hinweis", "Major/Q95 oder Top-Wall anklicken · bei Kontakt Auto-Lock · OB1000/FULL umschalten");
      html += row("Live-Metriken", "warten auf Target-Lock (sonst keine Contact-Daten)");
    }
    if (state.lastClickStatus && !s) html += row("Click", state.lastClickStatus);
    html += row("Symbol", s && s.symbol);
    html += row("Session", s && s.sessionId);
    html += row("Target", tw ? tw.side + " · " + tw.id : "–");
    html += row("Wall-Zone", tw ? tw.zone_lo + "–" + tw.zone_hi : "–");
    html += row(
      "Distanz",
      s && s.distance_bps != null
        ? fmtNum(s.distance_abs, 2) + " / " + fmtBpsAsPct(s.distance_bps, 3)
        : "–"
    );
    html += row("Approach", s && s.approach);
    html += row("Phase", s && s.phase);
    html += row("Bias", s && s.bias);
    html += row("Reasons", s && s.reasons ? s.reasons.join(", ") : "–");
    html += row("Start", s && s.startNote);
    html += row("Wall-Baseline", s && s.baselineQty != null ? s.baselineQty : "–");
    if (s) {
      html += row("Wall aktuell", m.wall_current_qty != null ? m.wall_current_qty : "DATA UNAVAILABLE");
      html += row("Wall-Abbau %", fmtPct(m.wall_reduce_pct));
      html += row("Trades erklärt %", fmtPct(m.trade_explained_pct));
      html += row("Pull-Anteil", fmtPct(m.pull_pct));
      html += row("Replenishment", fmtPct(m.replenish_pct));
      html += row(
        "Agg Buy/Sell",
        m.aggressor_buy_notional != null
          ? fmtNum(m.aggressor_buy_notional, 0) + " / " + fmtNum(m.aggressor_sell_notional, 0)
          : "DATA UNAVAILABLE"
      );
      html += row("AVR", m.avr_state != null ? m.avr_state : "DATA UNAVAILABLE");
      html += row("OI Δ", m.oi_delta != null ? m.oi_delta : "DATA UNAVAILABLE");
      html += row("Wall-Source", (m.adapters && m.adapters.wall_source) || "–");
    }
    html += row("Warnings", s && s.warnings && s.warnings.length ? s.warnings.join(", ") : "–");
    if (roles) {
      html += row("FRONT", roles.FRONT_WALL ? roles.FRONT_WALL.id : "–");
      html += row("BACKSTOP", roles.BACKSTOP_WALL ? roles.BACKSTOP_WALL.id : "–");
      html += row("DOMINANT", roles.DOMINANT_WALL ? roles.DOMINANT_WALL.id : "–");
      html += row("LARGER_BEHIND", roles.LARGER_WALL_BEHIND ? "YES · x" + fmtNum(roles.larger_wall_behind_ratio, 2) : "NO");
    }
    html += row("Datenstatus", m.data_gap ? "DATA GAP" : m.stale ? "STALE" : m.incomplete_trades ? "TRADES INCOMPLETE" : "OK");
    html += "<p class='wd-note'>XRAY V1 · read-only · keine Orderausführung · BP-Modus bleibt optional</p>";
    html += renderRadarHtml(state.radar);
    body.innerHTML = html;

    var sd = $("xrSortDist");
    var ss = $("xrSortSize");
    if (sd) {
      sd.onclick = function () {
        state.radarSort = "distance";
        refreshRadar();
        renderPanel();
      };
    }
    if (ss) {
      ss.onclick = function () {
        state.radarSort = "size";
        refreshRadar();
        renderPanel();
      };
    }
    body.querySelectorAll("[data-xr-id]").forEach(function (el) {
      el.addEventListener("click", function () {
        var id = el.getAttribute("data-xr-id");
        var cands = majorCandidates();
        var wall = null;
        for (var i = 0; i < cands.length; i += 1) {
          if (String(cands[i].id) === String(id)) {
            wall = cands[i];
            break;
          }
        }
        if (wall) startXrayFromWall(wall);
      });
    });
  }

  function bindUi() {
    var btn = $("mpWallXrayTool");
    if (btn && !btn._xrBound) {
      btn._xrBound = true;
      btn.addEventListener("click", function () {
        setToolActive(!state.toolActive);
        if (state.toolActive) openPanel();
      });
    }
    var stopBtn = $("xrStop");
    if (stopBtn && !stopBtn._xrBound) {
      stopBtn._xrBound = true;
      stopBtn.addEventListener("click", function () {
        stopXray("USER_STOP");
        setToolActive(false);
      });
    }
    var depthSel = $("mpOblDepth");
    if (depthSel && !depthSel._xrBound) {
      depthSel._xrBound = true;
      depthSel.addEventListener("change", function () {
        ensureBookFeed();
        if (state.toolActive || state.session) {
          refreshRadar();
          syncVisuals();
          renderPanel();
        }
      });
    }
    root.__mpOnWallXrayClick = function (payload) {
      onXrayClick(payload || {});
    };
    var prevReady = root.__mpOnChartReady;
    root.__mpOnChartReady = function () {
      if (typeof prevReady === "function") prevReady();
      syncVisuals();
    };
  }

  function init() {
    bindUi();
    root.__mpWallXray = {
      onPrice: onPrice,
      setToolActive: setToolActive,
      stopXray: stopXray,
      startXrayFromWall: startXrayFromWall,
      onXrayClick: onXrayClick,
      onPanelClosed: closePanel,
      getState: function () {
        return {
          toolActive: state.toolActive,
          session: state.session,
          radar: state.radar,
          lastPrice: state.lastPrice,
          liveMetrics: state.liveMetrics,
          lastClickStatus: state.lastClickStatus,
          panelOpen: state.panelOpen
        };
      }
    };
    // Feed price from existing wall decision hook if present
    var prev = root.__mpWallDecision;
    if (prev && typeof prev.onPrice === "function") {
      var orig = prev.onPrice;
      prev.onPrice = function (px) {
        orig(px);
        onPrice(px);
      };
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})(typeof globalThis !== "undefined" ? globalThis : window);
