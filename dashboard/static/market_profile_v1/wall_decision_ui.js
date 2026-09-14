/**
 * Floating Wall Decision panel + BP tool on the real Market Profile page.
 * Fixture applyFixture is blocked unless ?wd_fixture=1 (preview tool only).
 */
(function (root) {
  "use strict";
  var H = root.MpWallDecisionHelpers;
  if (!H) return;

  var FIXTURE_ALLOWED =
    typeof location !== "undefined" &&
    /(?:\?|&)wd_fixture=1(?:&|$)/.test(String(location.search || ""));

  var state = {
    toolActive: false,
    targetPickActive: false,
    bp: null,
    decision: null,
    lastPrice: null,
    panelOpen: false,
    fixtureMode: false,
    analysisTimer: null,
    analysisInflight: false,
    session: null,
    liveMetrics: null,
    acceptAboveMs: 0,
    acceptBelowMs: 0,
    lastAcceptTs: null
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

  function fmtNum(v, digits) {
    if (v == null || !Number.isFinite(Number(v))) return "DATA UNAVAILABLE";
    return Number(v).toFixed(digits == null ? 2 : digits);
  }

  function loadPersisted() {
    try {
      var store = H.loadStore(localStorage.getItem(H.STORAGE_KEY));
      var sym = symbol();
      var raw = store.bySymbol[sym];
      if (!raw) return;
      state.bp = Object.assign({}, raw, {
        symbol: sym,
        triggered: false,
        sessionId: null,
        status: raw.status === "TRIGGERED" ? "ARMED" : raw.status
      });
      if (state.bp.status === "TRIGGERED") state.bp.status = "ARMED";
      // Legacy auto-locked targets are not armed until user re-confirms.
      if (state.bp.manualTarget !== true || !state.bp.targetWall) {
        state.bp.targetWall = null;
        state.bp = H.refreshTargetCandidates(state.bp, wallsFromChart(), {
          refPrice: state.lastPrice
        });
        if (state.bp.status === "ARMED" || state.bp.status === "TARGET_LOCKED") {
          state.bp.status =
            (state.bp.targetCandidates && state.bp.targetCandidates.length)
              ? "TARGET_CANDIDATES_READY"
              : "BP_SET_WAITING_TARGET";
        }
      }
      paintBreakpoint();
      syncTargetVisuals();
      updateLabel();
      if (
        state.bp &&
        (state.bp.status === "TARGET_CANDIDATES_READY" ||
          state.bp.status === "BP_SET_WAITING_TARGET")
      ) {
        enterTargetPickMode();
      }
    } catch (e) { /* ignore */ }
  }

  function persist() {
    try {
      var store = H.loadStore(localStorage.getItem(H.STORAGE_KEY));
      store = H.saveSymbolBreakpoint(store, symbol(), state.bp);
      localStorage.setItem(H.STORAGE_KEY, JSON.stringify(store));
    } catch (e) { /* ignore */ }
  }

  function paintBreakpoint() {
    var api = chartApi();
    if (!api || typeof api.setWallBreakpoint !== "function") return;
    if (!state.bp || state.bp.price == null) {
      api.clearWallBreakpoint();
      return;
    }
    var label = "BP " + state.bp.price + " · " + H.decisionLabel(state.bp.status || "DISARMED");
    if (state.bp.status === "TARGET_LOCKED") {
      label = "BP " + state.bp.price + " · TARGET LOCKED";
    } else if (state.bp.status === "ARMED") {
      label = "BP " + state.bp.price + " · ARMED";
    } else if (state.bp.status === "TRIGGERED") {
      label = "BP " + state.bp.price + " · TRIGGERED";
    } else if (
      state.bp.status === "TARGET_CANDIDATES_READY" ||
      state.bp.status === "BP_SET_WAITING_TARGET"
    ) {
      label = "BP " + state.bp.price + " · BP SET · SELECT TARGET";
    }
    api.setWallBreakpoint({
      price: state.bp.price,
      title: label,
      color: "#f59e0b"
    });
  }

  function updateLabel() {
    var el = $("wdBpLabel");
    if (!el) return;
    if (!state.bp) {
      el.textContent = "BP –";
      return;
    }
    el.textContent = "BP " + state.bp.price + " · " + H.decisionLabel(state.bp.status || "DISARMED");
  }

  function wallsFromChart() {
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
          // Never invent major=true — Q95 classifier decides.
          major: b.major === true
        };
      });
    } catch (e) {
      return [];
    }
  }

  function syncTargetVisuals() {
    var api = chartApi();
    if (!api) return;
    var candidates =
      (state.bp && state.bp.targetCandidates) ||
      [];
    var locked = state.bp && state.bp.targetWall;
    if (typeof api.setWallTargetGuides === "function") {
      if (!state.bp || state.bp.price == null) {
        api.clearWallTargetGuides();
      } else {
        api.setWallTargetGuides({
          candidates: candidates,
          lockedId: locked && locked.id
        });
      }
    }
    if (typeof api.setWallTargetHighlight === "function") {
      if (!state.bp) {
        api.clearWallTargetHighlight();
      } else {
        api.setWallTargetHighlight({
          ids: candidates.map(function (c) {
            return c && c.id;
          }).filter(Boolean),
          lockedId: locked && locked.id
        });
      }
    }
  }

  function enterTargetPickMode() {
    state.targetPickActive = true;
    state.toolActive = false;
    var btn = $("mpWallBpTool");
    if (btn) btn.classList.toggle("active", false);
    var api = chartApi();
    if (api && typeof api.setInteractionMode === "function") {
      api.setInteractionMode("wall_target");
    }
  }

  function exitTargetPickMode() {
    state.targetPickActive = false;
    var api = chartApi();
    if (api && typeof api.setInteractionMode === "function") {
      if (state.toolActive) api.setInteractionMode("wall_bp");
      else api.setInteractionMode("select");
    }
  }

  function readAvrFromFootprint() {
    var sym = String(symbol() || "").toUpperCase();
    // Authoritative path: __mpWallDecisionContext.avr (native AVR BTCUSDT/5m).
    // Never leak BTC AVR into another symbol.
    try {
      var bridge = root.MpWallDecisionAvrContext;
      if (bridge && typeof bridge.setActiveSymbol === "function") {
        bridge.setActiveSymbol(sym);
      }
      if (bridge && typeof bridge.readStateName === "function") {
        var fromCtx = bridge.readStateName({ symbol: sym });
        if (fromCtx && fromCtx.status === "ok" && fromCtx.value) return fromCtx;
        return {
          value: null,
          status: "DATA_UNAVAILABLE",
          reason: (fromCtx && fromCtx.reason) || "DATA_UNAVAILABLE"
        };
      }
      var ctxAvr = root.__mpWallDecisionContext && root.__mpWallDecisionContext.avr;
      if (
        ctxAvr &&
        ctxAvr.available &&
        ctxAvr.state &&
        String(ctxAvr.symbol || "").toUpperCase() === "BTCUSDT" &&
        sym === "BTCUSDT"
      ) {
        return { value: String(ctxAvr.state), status: "ok", avr: ctxAvr };
      }
    } catch (e0) {
      /* fall through */
    }
    if (sym !== "BTCUSDT") {
      return { value: null, status: "DATA_UNAVAILABLE", reason: "symbol_unsupported" };
    }
    // Legacy fallback: Footprint store only when it already holds AVR candles for BTC.
    try {
      var FC = root.FootprintCandles;
      if (!FC) return { value: null, status: "DATA_UNAVAILABLE" };
      var st = FC._state;
      if (!st || String(st.symbol || "").toUpperCase() !== "BTCUSDT") {
        return { value: null, status: "DATA_UNAVAILABLE" };
      }
      var candles =
        (st && st.payload && st.payload.candles) || (st && st.historyCandles) || [];
      if (!candles || !candles.length) return { value: null, status: "DATA_UNAVAILABLE" };
      for (var i = candles.length - 1; i >= 0; i -= 1) {
        var avr = candles[i] && candles[i].avr;
        var name = avr && (avr.final_state || avr.dominant_state);
        if (name) return { value: String(name), status: "ok" };
      }
      return { value: null, status: "DATA_UNAVAILABLE" };
    } catch (e) {
      return { value: null, status: "DATA_UNAVAILABLE" };
    }
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
    if (state.toolActive) state.targetPickActive = false;
    var btn = $("mpWallBpTool");
    if (btn) btn.classList.toggle("active", state.toolActive);
    var api = chartApi();
    if (api && typeof api.setInteractionMode === "function") {
      api.setInteractionMode(state.toolActive ? "wall_bp" : "select");
    }
  }

  function placeBreakpoint(price) {
    var sym = symbol();
    var tick = H.defaultTickSize(sym);
    state.bp = H.createBreakpointState({
      symbol: sym,
      price: price,
      tickSize: tick,
      refPrice: state.lastPrice,
      walls: wallsFromChart()
    });
    state.decision = null;
    state.liveMetrics = null;
    state.session = null;
    stopAnalysisLoop();
    persist();
    paintBreakpoint();
    syncTargetVisuals();
    updateLabel();
    setToolActive(false);
    enterTargetPickMode();
    renderPanel();
  }

  function lockTargetFromClick(payload) {
    if (!state.bp || state.bp.triggered) return;
    if (
      state.bp.status !== "TARGET_CANDIDATES_READY" &&
      state.bp.status !== "BP_SET_WAITING_TARGET" &&
      state.bp.status !== "TARGET_LOST_BEFORE_TRIGGER"
    ) {
      return;
    }
    state.bp = H.refreshTargetCandidates(state.bp, wallsFromChart(), {
      refPrice: state.lastPrice
    });
    var candidates = state.bp.targetCandidates || [];
    var wall = null;
    if (payload && payload.id != null) {
      for (var i = 0; i < candidates.length; i += 1) {
        if (candidates[i] && String(candidates[i].id) === String(payload.id)) {
          wall = candidates[i];
          break;
        }
      }
    }
    var locked = H.lockManualTarget(state.bp, wall, {
      price: payload && payload.price,
      candidates: candidates
    });
    if (!locked.ok) {
      // Missed click on empty chart space — do not invent a wall.
      syncTargetVisuals();
      updateLabel();
      return;
    }
    state.bp = locked.state;
    state.decision = null;
    state.liveMetrics = null;
    state.session = null;
    stopAnalysisLoop();
    persist();
    paintBreakpoint();
    syncTargetVisuals();
    updateLabel();
    exitTargetPickMode();
    openPanel();
    renderPanel();
  }

  function clearBreakpoint() {
    state.bp = null;
    state.decision = null;
    state.liveMetrics = null;
    state.session = null;
    stopAnalysisLoop();
    exitTargetPickMode();
    persist();
    paintBreakpoint();
    syncTargetVisuals();
    updateLabel();
    closePanel();
  }

  function updateAcceptance(price) {
    var tw = state.bp && state.bp.targetWall;
    if (!tw || price == null) return;
    var now = Date.now();
    if (state.lastAcceptTs == null) state.lastAcceptTs = now;
    var dt = Math.max(0, (now - state.lastAcceptTs) / 1000);
    state.lastAcceptTs = now;
    var lo = Number(tw.zone_lo != null ? tw.zone_lo : tw.price);
    var hi = Number(tw.zone_hi != null ? tw.zone_hi : tw.price);
    if (price > hi) state.acceptAboveMs += dt * 1000;
    else if (price < lo) state.acceptBelowMs += dt * 1000;
  }

  function onPrice(price) {
    state.lastPrice = H.num(price);
    if (!state.bp || state.fixtureMode) return;
    if (
      !state.bp.triggered &&
      state.bp.status !== "ARMED" &&
      (state.bp.status === "TARGET_CANDIDATES_READY" ||
        state.bp.status === "BP_SET_WAITING_TARGET" ||
        state.bp.status === "TARGET_LOST_BEFORE_TRIGGER")
    ) {
      state.bp = H.refreshTargetCandidates(state.bp, wallsFromChart(), {
        refPrice: state.lastPrice
      });
      syncTargetVisuals();
      updateLabel();
    } else if (!state.bp.triggered && state.bp.status === "ARMED" && state.bp.targetWall) {
      state.bp = H.refreshTargetCandidates(state.bp, wallsFromChart(), {
        refPrice: state.lastPrice
      });
      if (state.bp.status === "TARGET_LOST_BEFORE_TRIGGER") {
        syncTargetVisuals();
        updateLabel();
        enterTargetPickMode();
        renderPanel();
        return;
      }
      syncTargetVisuals();
    }
    if (state.bp.triggered) {
      updateAcceptance(state.lastPrice);
      return;
    }
    var trig = H.evaluateTrigger(state.bp, state.lastPrice, {});
    if (!trig.triggered) return;
    state.bp = H.applyTrigger(state.bp, trig);
    var tw = state.bp.targetWall;
    state.session = {
      sessionId: state.bp.sessionId,
      triggerPrice: state.lastPrice,
      baselineQty: tw && tw.qty != null ? Number(tw.qty) : null,
      baselineNotional: tw && tw.notional != null ? Number(tw.notional) : null,
      minQtySeen: tw && tw.qty != null ? Number(tw.qty) : null,
      startedAtMs: trig.atMs,
      oiAtTrigger: readOiCurrent()
    };
    state.acceptAboveMs = 0;
    state.acceptBelowMs = 0;
    state.lastAcceptTs = Date.now();
    persist();
    paintBreakpoint();
    syncTargetVisuals();
    updateLabel();
    openPanel();
    state.decision = { state: "WALL_ATTACK", reasons: ["ANALYSING"], tradeReady: false };
    renderPanel();
    shadowPost();
    startAnalysisLoop();
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

  function pollLiveMetrics() {
    if (!state.bp || !state.bp.triggered || !state.session || state.analysisInflight) return;
    if (state.fixtureMode) return;
    state.analysisInflight = true;
    var avrNow = readAvrFromFootprint();
    var body = {
      symbol: state.bp.symbol,
      breakpoint: state.bp.price,
      target_wall: state.bp.targetWall,
      baseline_qty: state.session.baselineQty,
      baseline_notional: state.session.baselineNotional,
      triggered_at: state.bp.triggeredAtMs
        ? new Date(state.bp.triggeredAtMs).toISOString()
        : null,
      trigger_price: state.session.triggerPrice,
      live_price: state.lastPrice,
      accepted_above_sec: state.acceptAboveMs / 1000,
      accepted_below_sec: state.acceptBelowMs / 1000,
      min_qty_seen: state.session.minQtySeen,
      avr_state: avrNow.status === "ok" ? avrNow.value : null,
      oi_at_trigger: state.session.oiAtTrigger,
      oi_current: readOiCurrent()
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
          state.decision = {
            state: "DATA_GAP",
            reasons: ["LIVE_METRICS_HTTP_FAILED"],
            tradeReady: false
          };
          renderPanel();
          return;
        }
        state.liveMetrics = payload;
        if (payload.wall_current_qty != null && state.session) {
          var cur = Number(payload.wall_current_qty);
          if (Number.isFinite(cur)) {
            if (state.session.minQtySeen == null || cur < state.session.minQtySeen) {
              state.session.minQtySeen = cur;
            }
          }
        }
        var metrics = {
          wallSide: payload.wall_side || (state.bp && state.bp.wallSide),
          wallReducePct: payload.wall_reduce_pct,
          tradeExplainedPct: payload.trade_explained_pct,
          replenishPct: payload.replenish_pct,
          aggressorBuyShare: payload.aggressor_buy_share,
          aggressorSellShare: payload.aggressor_sell_share,
          priceResponseBps: payload.price_response_bps,
          acceptedAboveSec: payload.accepted_above_sec || 0,
          acceptedBelowSec: payload.accepted_below_sec || 0,
          dataGap: !!payload.data_gap,
          stale: !!payload.stale,
          wallLost: !!payload.wall_lost,
          // Only server-proven trade query failures — never invent incompleteness
          // when trade_explained is legitimately 0 / unavailable for other reasons.
          incompleteTrades: !!payload.incomplete_trades,
          epochBoundary: !!payload.epoch_boundary,
          retestHeld: false
        };
        state.decision = H.transitionDecision(
          (state.decision && state.decision.state) || "WALL_ATTACK",
          metrics
        );
        renderPanel();
        if (state.decision && (state.decision.state === "LONG_READY" || state.decision.state === "SHORT_READY" || state.decision.state === "NO_TRADE" || state.decision.state === "WALL_LOST")) {
          shadowPost();
        }
      })
      .catch(function () {
        state.decision = {
          state: "DATA_GAP",
          reasons: ["LIVE_METRICS_FETCH_ERROR"],
          tradeReady: false
        };
        renderPanel();
      })
      .then(function () {
        state.analysisInflight = false;
      });
  }

  function shadowPost() {
    if (!state.bp || !state.bp.sessionId) return;
    try {
      fetch("/api/wall-decision/v1/shadow", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({
          session_id: state.bp.sessionId,
          symbol: state.bp.symbol,
          breakpoint: state.bp.price,
          target_wall: state.bp.targetWall,
          armed_at: state.bp.createdAtMs ? new Date(state.bp.createdAtMs).toISOString() : null,
          triggered_at: state.bp.triggeredAtMs ? new Date(state.bp.triggeredAtMs).toISOString() : null,
          state: (state.decision && state.decision.state) || state.bp.status,
          reason_codes: (state.decision && state.decision.reasons) || [],
          metrics: state.liveMetrics || {},
          data_gap: !!(state.liveMetrics && state.liveMetrics.data_gap),
          event_time: state.liveMetrics && state.liveMetrics.event_time,
          available_at: state.liveMetrics && state.liveMetrics.available_at
        })
      }).catch(function () { /* non-fatal */ });
    } catch (e) { /* ignore */ }
  }

  function openPanel() {
    var panel = $("wdPanel");
    if (!panel) return;
    panel.hidden = false;
    state.panelOpen = true;
    restorePanelPos();
    renderPanel();
  }

  function closePanel() {
    var panel = $("wdPanel");
    if (panel) panel.hidden = true;
    state.panelOpen = false;
    try {
      if (root.__mpWallXray && typeof root.__mpWallXray.onPanelClosed === "function") {
        root.__mpWallXray.onPanelClosed();
      }
    } catch (e) { /* ignore */ }
  }

  function renderPanel() {
    var body = $("wdPanelBody");
    var title = $("wdPanelState");
    if (!body) return;
    var bp = state.bp;
    var dec = state.decision;
    var m = state.liveMetrics || {};
    var stateName = (dec && dec.state) || (bp && bp.status) || "DISARMED";
    if (title) {
      title.textContent = H.decisionLabel(stateName);
      title.className = "wd-state wd-tone-" + H.decisionTone(stateName);
    }
    function row(k, v) {
      return '<div class="wd-row"><span class="wd-k">' + k + '</span><span class="wd-v">' + (v == null || v === "" ? "N/A" : v) + "</span></div>";
    }
    var tw = bp && bp.targetWall;
    var ageSec = bp && bp.triggeredAtMs ? ((Date.now() - bp.triggeredAtMs) / 1000).toFixed(1) + "s" : "N/A";
    var html = "";
    html += row("Symbol", bp && bp.symbol);
    html += row("Session", bp && bp.sessionId);
    html += row("Breakpoint", bp && bp.price);
    html += row("Richtung", bp && bp.direction);
    html += row("Zielseite", bp && bp.wallSide);
    html += row("Target", bp && bp.targetStatus);
    html += row(
      "Kandidaten",
      bp && bp.targetCandidates && bp.targetCandidates.length
        ? String(bp.targetCandidates.length)
        : "0"
    );
    html += row(
      "Major-Regel",
      tw && tw.majorRule
        ? tw.majorRule
        : tw && tw.percentile != null
          ? "Q" + Math.round(tw.percentile)
          : "N/A"
    );
    html += row("Percentile", tw && tw.percentile != null ? Number(tw.percentile).toFixed(1) : "N/A");
    html += row("Wall-Zone", tw ? ((tw.zone_lo != null ? tw.zone_lo : tw.price) + "–" + (tw.zone_hi != null ? tw.zone_hi : tw.price)) : "DATA UNAVAILABLE");
    html += row("Wall-Notional", tw && tw.notional != null ? fmtNum(tw.notional, 0) + " USDT" : "N/A");
    html += row("Wall-Baseline", state.session && state.session.baselineQty != null ? state.session.baselineQty : "N/A");
    html += row("Wall aktuell", m.wall_current_qty != null ? m.wall_current_qty : "DATA UNAVAILABLE");
    html += row("Wall-ID", tw && tw.id);
    html += row("Analysezeit", ageSec);
    html += row("Datenstatus", m.data_gap ? "DATA GAP" : m.stale ? "STALE" : m.incomplete_trades ? "TRADES INCOMPLETE" : "OK");
    html += row("Decision", H.decisionLabel(stateName));
    html += row("Reasons", dec && dec.reasons ? dec.reasons.join(", ") : "–");
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
    html += row("Buy-/Sell-Anteil", m.aggressor_buy_share != null ? fmtPct(m.aggressor_buy_share) + " / " + fmtPct(m.aggressor_sell_share) : "DATA UNAVAILABLE");
    html += row("Preisreaktion bps", fmtNum(m.price_response_bps, 2));
    html += row("Geschw. bps/s", fmtNum(m.speed_bps_s, 3));
    html += row("Effizienz bps/Mio", fmtNum(m.efficiency_bps_per_mio, 2));
    html += row("AVR", m.avr_state != null ? m.avr_state : "DATA UNAVAILABLE");
    html += row("OI Δ", m.oi_delta != null ? m.oi_delta : "DATA UNAVAILABLE");
    html += row("Accept above/below", fmtNum(m.accepted_above_sec, 1) + "s / " + fmtNum(m.accepted_below_sec, 1) + "s");
    html += '<p class="wd-note">V1_PROVISIONAL · echte MP-Route · keine Fixtures · keine Orderausführung</p>';
    body.innerHTML = html;
  }

  function restorePanelPos() {
    var panel = $("wdPanel");
    if (!panel) return;
    try {
      var raw = localStorage.getItem(H.PANEL_POS_KEY);
      if (!raw) return;
      var pos = JSON.parse(raw);
      if (pos && Number.isFinite(pos.left) && Number.isFinite(pos.top)) {
        panel.style.left = pos.left + "px";
        panel.style.top = pos.top + "px";
        panel.style.right = "auto";
      }
    } catch (e) { /* ignore */ }
  }

  function bindPanelDrag() {
    var panel = $("wdPanel");
    var head = $("wdPanelHead");
    if (!panel || !head || head._wdBound) return;
    head._wdBound = true;
    var dragging = false;
    var ox = 0;
    var oy = 0;
    head.addEventListener("pointerdown", function (ev) {
      if (ev.button !== 0) return;
      dragging = true;
      var rect = panel.getBoundingClientRect();
      ox = ev.clientX - rect.left;
      oy = ev.clientY - rect.top;
      head.setPointerCapture(ev.pointerId);
    });
    head.addEventListener("pointermove", function (ev) {
      if (!dragging) return;
      var host = $("mpChart") || document.body;
      var hr = host.getBoundingClientRect();
      var left = ev.clientX - hr.left - ox;
      var top = ev.clientY - hr.top - oy;
      left = Math.max(0, Math.min(left, hr.width - panel.offsetWidth));
      top = Math.max(0, Math.min(top, hr.height - 40));
      panel.style.left = left + "px";
      panel.style.top = top + "px";
      panel.style.right = "auto";
    });
    head.addEventListener("pointerup", function () {
      if (!dragging) return;
      dragging = false;
      try {
        localStorage.setItem(
          H.PANEL_POS_KEY,
          JSON.stringify({ left: parseFloat(panel.style.left) || 0, top: parseFloat(panel.style.top) || 0 })
        );
      } catch (e) { /* ignore */ }
    });
  }

  function bindUi() {
    var btn = $("mpWallBpTool");
    if (btn && !btn._wdBound) {
      btn._wdBound = true;
      btn.addEventListener("click", function () {
        setToolActive(!state.toolActive);
      });
    }
    var minBtn = $("wdPanelMin");
    if (minBtn && !minBtn._wdBound) {
      minBtn._wdBound = true;
      minBtn.addEventListener("click", function (ev) {
        if (ev && ev.stopPropagation) ev.stopPropagation();
        var body = $("wdPanelBody");
        if (!body) return;
        body.hidden = !body.hidden;
        minBtn.setAttribute("aria-expanded", body.hidden ? "false" : "true");
        minBtn.title = body.hidden ? "Ausklappen" : "Einklappen";
        minBtn.textContent = body.hidden ? "+" : "–";
      });
    }
    var closeBtn = $("wdPanelClose");
    if (closeBtn && !closeBtn._wdBound) {
      closeBtn._wdBound = true;
      closeBtn.addEventListener("click", function (ev) {
        if (ev && ev.stopPropagation) ev.stopPropagation();
        closePanel();
      });
    }
    var clearBtn = $("wdClearBp");
    if (clearBtn && !clearBtn._wdBound) {
      clearBtn._wdBound = true;
      clearBtn.addEventListener("click", clearBreakpoint);
    }
    bindPanelDrag();

    root.__mpOnWallBpClick = function (payload) {
      if (!state.toolActive) return;
      var price = payload && payload.price;
      if (price == null) return;
      placeBreakpoint(price);
    };

    root.__mpOnWallTargetClick = function (payload) {
      if (!state.targetPickActive && !(state.bp && !state.bp.targetWall)) return;
      lockTargetFromClick(payload || {});
    };

    root.__mpOnWallBpDrag = function (payload) {
      if (!state.bp || !payload || payload.price == null) return;
      if (state.bp.triggered) return;
      state.bp = H.rearmAfterMove(state.bp, payload.price, {
        symbol: state.bp.symbol,
        refPrice: state.lastPrice,
        walls: wallsFromChart()
      });
      state.decision = null;
      state.liveMetrics = null;
      state.session = null;
      stopAnalysisLoop();
      persist();
      paintBreakpoint();
      syncTargetVisuals();
      updateLabel();
      enterTargetPickMode();
      renderPanel();
    };

    document.addEventListener("contextmenu", function (ev) {
      if (!state.bp) return;
      var t = ev.target;
      if (t && t.closest && t.closest("#wdBpHit, .wd-bp-line")) {
        ev.preventDefault();
        clearBreakpoint();
      }
    });

    var sym = $("mpSymbol");
    if (sym && !sym._wdBound) {
      sym._wdBound = true;
      sym.addEventListener("change", function () {
        stopAnalysisLoop();
        state.bp = null;
        state.decision = null;
        state.liveMetrics = null;
        state.session = null;
        loadPersisted();
        paintBreakpoint();
        updateLabel();
        closePanel();
      });
    }

    // Re-paint when chart becomes ready (price line API available).
    var prevReady = root.__mpOnChartReady;
    root.__mpOnChartReady = function () {
      if (typeof prevReady === "function") prevReady();
      paintBreakpoint();
      syncTargetVisuals();
    };
    if (root.__mpChartReady) {
      paintBreakpoint();
      syncTargetVisuals();
    }
  }

  function applyFixture(sequence) {
    if (!FIXTURE_ALLOWED) {
      try {
        console.warn("[wd] applyFixture blocked on real Market Profile route (use ?wd_fixture=1 only on preview tools)");
      } catch (e) { /* ignore */ }
      return;
    }
    state.fixtureMode = true;
    sequence = sequence || [];
    var i = 0;
    function step() {
      if (i >= sequence.length) return;
      var frame = sequence[i++];
      if (frame.breakpoint != null) {
        state.bp = H.createBreakpointState({
          symbol: frame.symbol || "BTCUSDT",
          price: frame.breakpoint,
          refPrice: frame.refPrice,
          walls: frame.walls || []
        });
      }
      if (frame.lockTarget && state.bp) {
        var lockedFx = H.lockManualTarget(state.bp, frame.lockTarget, {
          candidates: state.bp.targetCandidates || frame.walls || []
        });
        if (lockedFx.ok) state.bp = lockedFx.state;
      }
      if (frame.trigger) {
        if (!state.bp || !state.bp.targetWall) {
          /* fixture cannot trigger without locked target */
        } else {
          state.bp = H.applyTrigger(state.bp, { triggered: true, atMs: Date.now(), reason: "fixture" });
        }
      }
      if (frame.metrics) {
        state.decision = H.transitionDecision(frame.prevState || "TRIGGERED", frame.metrics);
      }
      if (frame.state) {
        state.decision = { state: frame.state, reasons: frame.reasons || ["FIXTURE"], tradeReady: !!frame.tradeReady };
      }
      paintBreakpoint();
      updateLabel();
      openPanel();
      renderPanel();
      setTimeout(step, frame.delayMs || 400);
    }
    step();
  }

  function init() {
    bindUi();
    loadPersisted();
    updateLabel();
    // AVR feed is independent of Footprint visual / chart TF (native 5m).
    try {
      if (root.MpWallDecisionAvrContext && typeof root.MpWallDecisionAvrContext.start === "function") {
        root.MpWallDecisionAvrContext.start({ intervalMs: 5000, symbol: symbol() });
      }
    } catch (eStart) {
      /* ignore */
    }
    root.__mpWallDecision = {
      onPrice: onPrice,
      placeBreakpoint: placeBreakpoint,
      lockTargetFromClick: lockTargetFromClick,
      clearBreakpoint: clearBreakpoint,
      openPanel: openPanel,
      applyFixture: applyFixture,
      setToolActive: setToolActive,
      getState: function () {
        return {
          bp: state.bp,
          decision: state.decision,
          lastPrice: state.lastPrice,
          liveMetrics: state.liveMetrics,
          session: state.session,
          fixtureMode: state.fixtureMode,
          fixtureAllowed: FIXTURE_ALLOWED,
          targetPickActive: state.targetPickActive
        };
      }
    };
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})(typeof globalThis !== "undefined" ? globalThis : window);
