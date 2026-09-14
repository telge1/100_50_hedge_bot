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
      paintBreakpoint();
      updateLabel();
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
    var label = "BP " + state.bp.price + " · " + H.decisionLabel(state.bp.status || "ARMED");
    if (state.bp.targetStatus === "AMBIGUOUS_TARGET") {
      label = "BP " + state.bp.price + " · AMBIGUOUS TARGET";
    } else if (state.bp.status === "TARGET_LOCKED") {
      label = "BP " + state.bp.price + " · TARGET LOCKED";
    } else if (state.bp.status === "ARMED") {
      label = "BP " + state.bp.price + " · ARMED";
    } else if (state.bp.status === "TRIGGERED") {
      label = "BP " + state.bp.price + " · TRIGGERED";
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
    if (state.bp.targetStatus === "AMBIGUOUS_TARGET") {
      el.textContent = "BP " + state.bp.price + " · AMBIGUOUS TARGET";
      return;
    }
    el.textContent = "BP " + state.bp.price + " · " + H.decisionLabel(state.bp.status || "ARMED");
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
          side: String(b.side || "").toUpperCase(),
          price: Number(b.price),
          qty: Number(b.qty != null ? b.qty : b.size),
          notional: Number(b.notional != null ? b.notional : b.value),
          zone_lo: b.zone_lo,
          zone_hi: b.zone_hi,
          major: b.major === false ? false : true
        };
      });
    } catch (e) {
      return [];
    }
  }

  function readAvrFromFootprint() {
    try {
      var FC = root.FootprintCandles;
      if (!FC) return { value: null, status: "DATA_UNAVAILABLE" };
      var st = FC._state;
      var candles = st && st.payload && st.payload.candles;
      if (!candles || !candles.length) return { value: null, status: "DATA_UNAVAILABLE" };
      var last = candles[candles.length - 1];
      var avr = last && last.avr;
      var name = avr && (avr.final_state || avr.dominant_state);
      return name ? { value: String(name), status: "ok" } : { value: null, status: "DATA_UNAVAILABLE" };
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
    updateLabel();
    setToolActive(false);
    renderPanel();
  }

  function clearBreakpoint() {
    state.bp = null;
    state.decision = null;
    state.liveMetrics = null;
    state.session = null;
    stopAnalysisLoop();
    persist();
    paintBreakpoint();
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
    html += row("Wall-Zone", tw ? ((tw.zone_lo != null ? tw.zone_lo : tw.price) + "–" + (tw.zone_hi != null ? tw.zone_hi : tw.price)) : "DATA UNAVAILABLE");
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
      minBtn.addEventListener("click", function () {
        var body = $("wdPanelBody");
        if (!body) return;
        body.hidden = !body.hidden;
        minBtn.setAttribute("aria-expanded", body.hidden ? "false" : "true");
      });
    }
    var closeBtn = $("wdPanelClose");
    if (closeBtn && !closeBtn._wdBound) {
      closeBtn._wdBound = true;
      closeBtn.addEventListener("click", closePanel);
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

    root.__mpOnWallBpDrag = function (payload) {
      if (!state.bp || !payload || payload.price == null) return;
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
      updateLabel();
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
    };
    if (root.__mpChartReady) paintBreakpoint();
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
      if (frame.trigger) {
        state.bp = H.applyTrigger(state.bp, { triggered: true, atMs: Date.now(), reason: "fixture" });
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
    root.__mpWallDecision = {
      onPrice: onPrice,
      placeBreakpoint: placeBreakpoint,
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
          fixtureMode: state.fixtureMode,
          fixtureAllowed: FIXTURE_ALLOWED
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
