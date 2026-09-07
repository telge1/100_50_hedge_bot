/* Footprint candles overlay — BTCUSDT / 5m / DISPLAY.
 * Adaptive display buckets + separated history/forming stores.
 * Lives in dashboard/footprint_candles; market_profile_v1 only hosts hooks.
 */
(function (global) {
  "use strict";

  var SUPPORTED_SYMBOL = "BTCUSDT";
  var SUPPORTED_TF = "5m";
  var BUCKET_STEP = 5.0;
  var RAW_STEP = 5.0;
  var MODE = "DISPLAY";
  var BUFFER_S = 900;
  var DEBOUNCE_MS = 200;
  var FORMING_MS = 2000;
  var MAX_SPAN_S = 6 * 3600;
  var FULL_BAR = 60;
  var DELTA_BAR = 40;
  var COMPACT_BAR = 28;
  var FULL_LEVEL_H = 11;
  var DELTA_LEVEL_H = 3;
  var DISPLAY_STEPS = [5, 10, 15, 20, 25, 50];
  var IMBALANCE_RATIO = 3.0;
  var MIN_COMPARE_SIZE = 0.01;
  var STACKED_MIN_LEVELS = 3;

  var COLORS = {
    bid: "rgba(239, 83, 80, 0.85)",
    ask: "rgba(38, 166, 154, 0.85)",
    bidFill: "rgba(239, 83, 80, 0.18)",
    askFill: "rgba(38, 166, 154, 0.18)",
    vpoc: "#f0b90b",
    imbAsk: "rgba(38, 166, 154, 0.55)",
    imbBid: "rgba(239, 83, 80, 0.55)",
    warn: "#eab308",
    missing: "#ef4444",
    hint: "rgba(180, 190, 210, 0.85)",
    openTick: "rgba(230, 235, 245, 0.95)",
    closeUp: "rgba(38, 166, 154, 0.95)",
    closeDown: "rgba(239, 83, 80, 0.95)",
    hiLo: "rgba(170, 180, 200, 0.65)",
    badge: "rgba(15, 18, 28, 0.82)",
    badgeText: "#e8edf5",
    bodyUpFill: "rgba(38, 166, 154, 0.18)",
    bodyDownFill: "rgba(239, 83, 80, 0.18)",
    bodyUpStroke: "rgba(38, 166, 154, 0.85)",
    bodyDownStroke: "rgba(239, 83, 80, 0.85)",
    wick: "rgba(190, 200, 215, 0.75)"
  };

  var AVR_BADGES = {
    SELLER_CONTROL: "S CTRL",
    BUYER_CONTROL: "B CTRL",
    SELL_ABSORPTION_CANDIDATE: "S ABS",
    BUY_ABSORPTION_CANDIDATE: "B ABS",
    VACUUM_DOWN_PROXY: "VAC ↓",
    VACUUM_UP_PROXY: "VAC ↑",
    BALANCED: "",
    INSUFFICIENT_DATA: "",
    INSUFFICIENT_BASELINE: ""
  };

  var STATE_VIS = {
    SELLER_CONTROL: { fill: "rgba(239, 83, 80, 0.92)", strip: "rgba(239, 83, 80, 0.85)" },
    BUYER_CONTROL: { fill: "rgba(38, 166, 154, 0.92)", strip: "rgba(38, 166, 154, 0.85)" },
    SELL_ABSORPTION_CANDIDATE: { fill: "rgba(245, 158, 11, 0.92)", strip: "rgba(245, 158, 11, 0.85)" },
    BUY_ABSORPTION_CANDIDATE: { fill: "rgba(45, 212, 191, 0.92)", strip: "rgba(45, 212, 191, 0.85)" },
    VACUUM_DOWN_PROXY: { fill: "rgba(167, 139, 250, 0.75)", strip: "rgba(167, 139, 250, 0.7)" },
    VACUUM_UP_PROXY: { fill: "rgba(56, 189, 248, 0.75)", strip: "rgba(56, 189, 248, 0.7)" },
    BALANCED: { fill: "rgba(100, 110, 125, 0.7)", strip: "rgba(80, 88, 100, 0.65)" },
    INSUFFICIENT_DATA: { fill: null, strip: "rgba(40, 44, 52, 0.35)" },
    INSUFFICIENT_BASELINE: { fill: null, strip: "rgba(40, 44, 52, 0.35)" }
  };

  var STATE_STRIP_H = 10;
  var TIME_AXIS_PAD = 28; /* keep strip above Lightweight Charts time axis */
  var BODY_MIN_H = 3;
  var TICK_LEN_MIN = 6;

  var TRANSPARENT = "rgba(0, 0, 0, 0)";
  var CANDLE_COLOR_KEYS = [
    "upColor",
    "downColor",
    "borderColor",
    "borderUpColor",
    "borderDownColor",
    "wickColor",
    "wickUpColor",
    "wickDownColor"
  ];

  var state = {
    enabled: false,
    chart: null,
    candleSeries: null,
    symbol: SUPPORTED_SYMBOL,
    timeframe: SUPPORTED_TF,
    /* Merged view for draw / panel / legacy tests */
    payload: null,
    historyCandles: [],
    historyMeta: null,
    historyInflight: null,
    historyGen: 0,
    historyLoading: false,
    formingInflight: null,
    formingGen: 0,
    formingLoading: false,
    /* legacy alias used by older tests */
    inflight: null,
    gen: 0,
    formingTimer: null,
    debounceTimer: null,
    rafPending: false,
    statusEl: null,
    unsupported: false,
    savedCandleStyle: null,
    candlesTransparent: false,
    candleSeriesRef: null,
    panelVisible: true, /* AVR visuals (strip/badges) */
    panelExpanded: false, /* ERWEITERT detail panel */
    selectedTime: null,
    selectedLabel: "Aktuelle Candle",
    crosshairUnsub: null,
    lastRenderMode: "fallback",
    lastDisplayStep: RAW_STEP,
    lastDebug: null,
    lastDrawStats: null,
    /* Forming bucket tracking + closed-candle finalize */
    lastFormingBucket: null,
    finalizeInflight: null,
    finalizeGen: 0,
    /* Keyed AVR history: symbol|timeframe|candle_start → mark */
    avrMarks: {}
  };

  function $(id) {
    return document.getElementById(id);
  }

  function canvas() {
    return $("fpOverlay");
  }

  function debugEnabled() {
    try {
      if (global.__FP_DEBUG__ === true) return true;
      if (typeof localStorage !== "undefined" && localStorage.getItem("fpDebug") === "1") {
        return true;
      }
    } catch (e) { /* ignore */ }
    return false;
  }

  function debugLog(payload) {
    if (!debugEnabled()) return;
    state.lastDebug = payload;
    try {
      console.info("[fp-debug]", payload);
    } catch (e) { /* ignore */ }
  }

  function setStatus(text, kind) {
    var el = state.statusEl || $("fpStatus");
    if (!el) return;
    el.textContent = text || "";
    el.className = "fp-status" + (kind ? " is-" + kind : "");
  }

  function fmtNotional(v) {
    var av = Math.abs(Number(v) || 0);
    if (av >= 1e6) {
      var m = av / 1e6;
      return (m >= 10 ? m.toFixed(0) : m.toFixed(1)) + "M";
    }
    if (av >= 1e3) {
      var k = av / 1e3;
      return (k >= 100 ? k.toFixed(0) : k.toFixed(k >= 10 ? 1 : 2)) + "K";
    }
    return av.toFixed(0);
  }

  function fmtDeltaHeader(avr) {
    if (!avr) return "";
    var d = Number(avr.candle_delta_notional);
    var pct = Number(avr.delta_pct);
    var clv = Number(avr.clv);
    if (!isFinite(d) && !isFinite(pct)) return "";
    var dStr = (d >= 0 ? "+" : "−") + fmtNotional(Math.abs(d));
    var pStr = isFinite(pct) ? (pct >= 0 ? "+" : "−") + Math.abs(pct).toFixed(0) + "%" : "–";
    var cStr = isFinite(clv) ? clv.toFixed(2) : "–";
    return "Δ " + dStr + " | " + pStr + " | CLV " + cStr;
  }

  function badgeForAvr(avr) {
    var spec = badgeSpec(avr);
    return spec && spec.label ? spec.label : "";
  }

  function chartWorthyState(st) {
    return !!(STATE_VIS[st] && STATE_VIS[st].fill && AVR_BADGES[st]);
  }

  /** Single chart badge from dominant_state only. */
  function badgeSpec(avr) {
    if (!avr) return null;
    var st = avr.dominant_state || "";
    if (!chartWorthyState(st)) {
      if (st === "BALANCED") return { kind: "dot", state: st, label: "", color: STATE_VIS.BALANCED.strip, muted: false };
      if (st === "INSUFFICIENT_DATA" || st === "INSUFFICIENT_BASELINE") {
        return { kind: "none", state: st, label: "", color: null, muted: true };
      }
      return null;
    }
    var cov = avr.coverage_status || avr.verification || "";
    var unverified =
      avr.verification === "UNVERIFIED" ||
      cov === "PARTIAL" ||
      cov === "UNKNOWN";
    var label = AVR_BADGES[st];
    if (unverified) label = label + " ?";
    var vis = STATE_VIS[st];
    return {
      kind: "badge",
      state: st,
      label: label,
      color: vis.fill,
      strip: vis.strip,
      muted: !!unverified,
      strength: avr.dominant_strength
    };
  }

  function fmtSignedNotional(v) {
    var n = Number(v);
    if (!isFinite(n)) return "";
    var sign = n >= 0 ? "+" : "−";
    return sign + fmtNotional(Math.abs(n));
  }

  function fmtCompactDelta(v) {
    var n = Number(v);
    if (!isFinite(n)) return "";
    var av = Math.abs(n);
    var sign = n >= 0 ? "+" : "−";
    if (av >= 1e6) {
      var m = av / 1e6;
      return sign + (m >= 10 ? m.toFixed(0) : m.toFixed(1)) + "M";
    }
    if (av >= 1e3) {
      var k = av / 1e3;
      return sign + (k >= 100 ? k.toFixed(0) : k.toFixed(k >= 10 ? 0 : 1)) + "K";
    }
    return sign + av.toFixed(0);
  }

  /* ---------- Adaptive display aggregation (pure) ---------- */

  function alignDisplayLow(priceLow, displayStep) {
    var step = Number(displayStep);
    var p = Number(priceLow);
    return Math.floor(p / step + 1e-12) * step;
  }

  function chooseDisplayStep(pxPerRaw5, minRowPx) {
    var px = Number(pxPerRaw5);
    var need = Number(minRowPx);
    if (!(px > 0) || !(need > 0)) return null;
    for (var i = 0; i < DISPLAY_STEPS.length; i += 1) {
      var step = DISPLAY_STEPS[i];
      var rowPx = px * (step / RAW_STEP);
      if (rowPx + 1e-9 >= need) return step;
    }
    return null;
  }

  function pickDisplayVpoc(groups) {
    var bestKey = null;
    var bestVol = -1;
    var keys = Object.keys(groups);
    keys.sort(function (a, b) { return Number(a) - Number(b); });
    for (var i = 0; i < keys.length; i += 1) {
      var g = groups[keys[i]];
      var total = g.bid_size + g.ask_size;
      if (total > bestVol || (total === bestVol && (bestKey === null || Number(keys[i]) < Number(bestKey)))) {
        bestVol = total;
        bestKey = keys[i];
      }
    }
    return bestKey;
  }

  function computeDisplayImbalances(sortedLows, groups) {
    var askImb = {};
    var bidImb = {};
    var lowSet = {};
    var i;
    for (i = 0; i < sortedLows.length; i += 1) {
      lowSet[sortedLows[i]] = true;
      askImb[sortedLows[i]] = false;
      bidImb[sortedLows[i]] = false;
    }
    var step = null;
    if (sortedLows.length >= 2) {
      step = Number(sortedLows[1]) - Number(sortedLows[0]);
    }
    for (i = 0; i < sortedLows.length; i += 1) {
      var low = sortedLows[i];
      var cur = groups[low];
      if (!cur) continue;
      if (step != null) {
        var lower = low - step;
        if (lowSet[lower]) {
          var bidLower = groups[lower].bid_size;
          if (cur.ask_size >= MIN_COMPARE_SIZE && bidLower > 0) {
            if (cur.ask_size / bidLower >= IMBALANCE_RATIO) askImb[low] = true;
          }
        }
        var higher = low + step;
        if (lowSet[higher]) {
          var askHigher = groups[higher].ask_size;
          if (cur.bid_size >= MIN_COMPARE_SIZE && askHigher > 0) {
            if (cur.bid_size / askHigher >= IMBALANCE_RATIO) bidImb[low] = true;
          }
        }
      }
    }
    return { ask: askImb, bid: bidImb };
  }

  function markStackedFlags(sortedLows, flags) {
    var stacked = {};
    var n = sortedLows.length;
    var i = 0;
    while (i < n) {
      var low = sortedLows[i];
      stacked[low] = false;
      if (!flags[low]) {
        i += 1;
        continue;
      }
      var j = i;
      while (j < n && flags[sortedLows[j]]) {
        if (j > i) {
          var step = Number(sortedLows[j]) - Number(sortedLows[j - 1]);
          var expect = Number(sortedLows[1]) - Number(sortedLows[0]);
          if (!(Math.abs(step - expect) < 1e-9)) break;
        }
        j += 1;
      }
      if (j - i >= STACKED_MIN_LEVELS) {
        for (var k = i; k < j; k += 1) stacked[sortedLows[k]] = true;
      }
      i = j > i ? j : i + 1;
    }
    return stacked;
  }

  /**
   * Aggregate raw $5 levels into a display step. Does not mutate input.
   * When displayStep > RAW_STEP, raw imbalance flags are suppressed and
   * recomputed on the aggregated grid (or left false if unsafe).
   */
  function aggregateLevelsForDisplay(rawLevels, displayStep) {
    var step = Number(displayStep) || RAW_STEP;
    var levels = rawLevels || [];
    if (!levels.length) {
      return { levels: [], displayStep: step, vpocLow: null };
    }
    if (step <= RAW_STEP + 1e-9) {
      var copied = levels.map(function (lv) {
        return Object.assign({}, lv);
      });
      return { levels: copied, displayStep: RAW_STEP, vpocLow: null };
    }

    var groups = {};
    for (var i = 0; i < levels.length; i += 1) {
      var lv = levels[i];
      var low = alignDisplayLow(lv.price_low != null ? lv.price_low : lv.price, step);
      var g = groups[low];
      if (!g) {
        g = {
          price_low: low,
          price_high: low + step,
          bid_size: 0,
          ask_size: 0,
          bid_notional: 0,
          ask_notional: 0
        };
        groups[low] = g;
      }
      g.bid_size += Number(lv.bid_size) || 0;
      g.ask_size += Number(lv.ask_size) || 0;
      g.bid_notional += Number(lv.bid_notional) || 0;
      g.ask_notional += Number(lv.ask_notional) || 0;
    }

    var lows = Object.keys(groups)
      .map(Number)
      .sort(function (a, b) { return a - b; });
    var vpocKey = pickDisplayVpoc(groups);
    var imb = computeDisplayImbalances(lows, groups);
    var stackedAsk = markStackedFlags(lows, imb.ask);
    var stackedBid = markStackedFlags(lows, imb.bid);

    var out = [];
    for (var j = 0; j < lows.length; j += 1) {
      var key = lows[j];
      var row = groups[key];
      var totalSize = row.bid_size + row.ask_size;
      var totalNotional = row.bid_notional + row.ask_notional;
      var deltaSize = row.ask_size - row.bid_size;
      var deltaNotional = row.ask_notional - row.bid_notional;
      out.push({
        price_low: row.price_low,
        price_high: row.price_high,
        bid_size: row.bid_size,
        ask_size: row.ask_size,
        bid_notional: row.bid_notional,
        ask_notional: row.ask_notional,
        total_size: totalSize,
        total_notional: totalNotional,
        delta_size: deltaSize,
        delta_notional: deltaNotional,
        delta_pct: totalSize > 0 ? (deltaSize / totalSize) * 100 : 0,
        is_vpoc: String(key) === String(vpocKey),
        ask_imbalance: !!imb.ask[key],
        bid_imbalance: !!imb.bid[key],
        stacked_ask: !!stackedAsk[key],
        stacked_bid: !!stackedBid[key],
        display_step: step,
        imbalances_recomputed: true
      });
    }
    return { levels: out, displayStep: step, vpocLow: vpocKey != null ? Number(vpocKey) : null };
  }

  /**
   * Choose render mode + display step from measured geometry.
   * opts.levelHeight = pixel height of one raw $5 bucket (preferred).
   * opts.pxPerRaw5 alias; opts.barSpacing / barWidth.
   */
  function chooseRenderPlan(opts) {
    opts = opts || {};
    var bs = opts.barSpacing != null ? Number(opts.barSpacing) : barWidthPx();
    var px =
      opts.pxPerRaw5 != null
        ? Number(opts.pxPerRaw5)
        : opts.levelHeight != null
          ? Number(opts.levelHeight)
          : 0;

    function tryMode(mode, minRow, minBar) {
      if (!(bs + 1e-9 >= minBar)) return null;
      var step = chooseDisplayStep(px, minRow);
      if (step == null) return null;
      return { mode: mode, displayStep: step, pxPerRaw5: px, barSpacing: bs };
    }

    /* FULL: tall enough (after adaptive) + wide enough for Bid×Ask text */
    var full = tryMode("full", FULL_LEVEL_H, FULL_BAR);
    if (full) return full;

    /* DELTA: rows visible + compact delta width */
    var delta = tryMode("delta", DELTA_LEVEL_H, DELTA_BAR);
    if (delta) return delta;

    /* COMPACT: levels drawable vertically, horizontal too narrow for Bid×Ask */
    var compact = tryMode("compact", DELTA_LEVEL_H, COMPACT_BAR);
    if (compact) return compact;

    /* Last resort compact: largest step still may draw vPOC tick + badge */
    if (bs + 1e-9 >= COMPACT_BAR && px > 0) {
      var last = DISPLAY_STEPS[DISPLAY_STEPS.length - 1];
      return { mode: "compact", displayStep: last, pxPerRaw5: px, barSpacing: bs };
    }

    return { mode: "fallback", displayStep: null, pxPerRaw5: px, barSpacing: bs };
  }

  function measurePxPerRaw5(samplePrice) {
    if (!state.candleSeries) return 0;
    var p = Number(samplePrice);
    if (!isFinite(p)) return 0;
    var y0 = priceToY(p);
    var y1 = priceToY(p + RAW_STEP);
    if (y0 == null || y1 == null) return 0;
    return Math.abs(y1 - y0);
  }

  function statusForPlan(plan, coverage) {
    var cov = coverage || "UNKNOWN";
    if (!plan || plan.mode === "fallback") return { text: "Mehr hineinzoomen", kind: "warn" };
    var rawLabel = "Raw $" + RAW_STEP;
    if (plan.mode === "compact") {
      /* Compact draws no price-row text — only mention Display if aggregation used for vPOC */
      if (plan.displayStep && plan.displayStep > RAW_STEP + 1e-9) {
        return { text: "Compact · " + rawLabel + " · Display $" + plan.displayStep, kind: "warn" };
      }
      return { text: "Compact · " + rawLabel, kind: "warn" };
    }
    var stepLabel = plan.displayStep != null ? "Display $" + plan.displayStep : "";
    if (plan.mode === "full") {
      return {
        text: "Full · " + stepLabel + " · " + rawLabel + " · " + cov,
        kind: cov === "COMPLETE" ? "ok" : "warn"
      };
    }
    if (plan.mode === "delta") {
      return { text: "Delta · " + stepLabel + " · " + rawLabel, kind: "warn" };
    }
    return { text: "Mehr hineinzoomen", kind: "warn" };
  }

  function insufficientReasonLines(avr) {
    var lines = [];
    var stName = avr.dominant_state || avr.final_state || "";
    var ev = avr.evidence || avr.dominant_evidence || {};
    var base = ev.baseline || avr.baseline || {};
    if (stName === "INSUFFICIENT_BASELINE" || stName === "INSUFFICIENT_DATA") {
      lines.push("Reason: " + stName);
      if (base.required_valid_seconds != null || ev.required_valid_seconds != null) {
        lines.push(
          "Need valid 1s: " +
            (base.required_valid_seconds != null
              ? base.required_valid_seconds
              : ev.required_valid_seconds)
        );
      }
      if (base.valid_seconds != null || ev.valid_seconds != null || ev.available_valid_seconds != null) {
        lines.push(
          "Have valid 1s: " +
            (base.valid_seconds != null
              ? base.valid_seconds
              : ev.valid_seconds != null
                ? ev.valid_seconds
                : ev.available_valid_seconds)
        );
      }
      if (base.feature_samples != null || ev.feature_samples != null) {
        lines.push(
          "Feature samples: " +
            (base.feature_samples != null ? base.feature_samples : ev.feature_samples)
        );
      }
      if (ev.reason) lines.push("Detail: " + ev.reason);
      if (ev.gap || base.gap) lines.push("Gap: yes");
      if (ev.preroll_missing || base.preroll_missing) lines.push("Preroll: missing");
      if (avr.provisional) lines.push("Forming/provisional candle");
    }
    return lines;
  }

  function humanState(st) {
    if (!st) return "–";
    return String(st)
      .replace(/_CANDIDATE$/, "")
      .replace(/_PROXY$/, "")
      .replace(/_/g, " ");
  }

  function pickPanelCandle(payload) {
    var candles = (payload && payload.candles) || [];
    if (!candles.length) return { candle: null, label: "" };
    if (state.selectedTime != null) {
      for (var i = 0; i < candles.length; i += 1) {
        if (Number(candles[i].time) === Number(state.selectedTime)) {
          return { candle: candles[i], label: "Ausgewählte Candle" };
        }
      }
      /* Crosshair on a time with no store entry — do not fall back to live candle */
      return { candle: null, label: "Ausgewählte Candle" };
    }
    return { candle: candles[candles.length - 1], label: "Aktuelle Candle" };
  }

  function formatCandleTime(t) {
    var n = Number(t);
    if (!isFinite(n)) return "–";
    try {
      return new Date(n * 1000).toISOString().replace("T", " ").replace(".000Z", "Z");
    } catch (e) {
      return String(n);
    }
  }

  function updateAvrPanel(payload) {
    var panel = $("fpAvrPanel");
    var body = $("fpAvrPanelBody");
    var hint = $("fpAvrPanelHint");
    if (!panel || !body) return;
    var avrOn = state.panelVisible;
    var expanded = state.panelExpanded;
    if (!state.enabled || !avrOn || !expanded) {
      panel.hidden = true;
      return;
    }
    var pick = pickPanelCandle(payload);
    var last = pick.candle;
    if (hint) hint.textContent = pick.label || "Aktuelle Candle";
    if (!last || !last.avr) {
      body.textContent = "AVR: keine Metriken für diese Candle";
      panel.hidden = false;
      return;
    }
    var avr = last.avr;
    var thirds = avr.third_states || {};
    var counts = avr.state_counts || {};
    var nWin = 0;
    Object.keys(counts).forEach(function (k) { nWin += Number(counts[k]) || 0; });
    var sharePct = "–";
    if (avr.state_share && avr.dominant_state && avr.state_share[avr.dominant_state] != null) {
      sharePct = (Number(avr.state_share[avr.dominant_state]) * 100).toFixed(0) + "%";
    }
    var verify = avr.verification || "–";
    if (
      (avr.coverage_status === "PARTIAL" || avr.coverage_status === "UNKNOWN") &&
      verify !== "UNVERIFIED"
    ) {
      verify = "UNVERIFIED";
    }
    var insuf =
      avr.dominant_state === "INSUFFICIENT_DATA" ||
      avr.dominant_state === "INSUFFICIENT_BASELINE";
    var lines = [];
    lines.push("Zeit: " + formatCandleTime(last.time));
    if (insuf) {
      lines.push(
        avr.provisional
          ? "Formende Candle – warte auf ausreichende Daten"
          : "Unvollständige Daten – keine verlässliche Klassifikation"
      );
      lines.push("Status: " + humanState(avr.dominant_state));
      var reason = insufficientReasonLines(avr);
      if (reason.length) lines = lines.concat(reason);
      else {
        lines.push("Vorhandene Fenster: " + nWin);
      }
    } else {
      lines.push("Dominant: " + humanState(avr.dominant_state));
      lines.push("Final: " + humanState(avr.final_state));
      lines.push(
        "Strength: " +
          (avr.dominant_strength != null ? Number(avr.dominant_strength).toFixed(1) : "–")
      );
      lines.push("Share: " + sharePct);
      lines.push("Verification: " + verify);
      lines.push(
        "Lifecycle: " +
          (avr.provisional || (avr.panel && avr.panel.provisional) ? "Forming" : "Closed")
      );
      lines.push("");
      lines.push("EARLY / MIDDLE / LATE");
      lines.push("  Early:  " + humanState(thirds.EARLY));
      lines.push("  Middle: " + humanState(thirds.MIDDLE));
      lines.push("  Late:   " + humanState(thirds.LATE));
      lines.push("");
      lines.push("Details");
      lines.push("  Windows: " + nWin);
      if (Object.keys(counts).length) {
        lines.push(
          "  Counts: " +
            Object.keys(counts)
              .map(function (k) {
                return humanState(k) + "=" + counts[k];
              })
              .join(", ")
        );
      }
      var p = avr.panel || {};
      if (p.aggression_percentile != null) lines.push("  Aggression p: " + p.aggression_percentile);
      if (p.velocity_percentile != null) lines.push("  Velocity p: " + p.velocity_percentile);
      if (p.efficiency_percentile != null) lines.push("  Efficiency p: " + p.efficiency_percentile);
      if (p.price_progress_bps != null) lines.push("  Move: " + p.price_progress_bps + " bps");
      if (p.window_seconds != null) {
        lines.push("  Evidence window: " + p.window_seconds + "s @" + (p.available_at || "–"));
      }
      if (avr.coverage_status) lines.push("  Coverage: " + avr.coverage_status);
      if (avr.config_hash) lines.push("  config hash: " + avr.config_hash);
      if (avr.provisional) lines.push("  provisional: true");
    }
    if (insuf && avr.config_hash) {
      lines.push("config hash: " + avr.config_hash);
    }
    /* Keep contract strings discoverable for tests */
    body.setAttribute("data-dominant_state", avr.dominant_state || "");
    body.setAttribute("data-dominant_strength", avr.dominant_strength != null ? String(avr.dominant_strength) : "");
    body.setAttribute("data-state_share", sharePct);
    body.setAttribute("data-state_counts", String(nWin));
    body.setAttribute("data-final_state", avr.final_state || "");
    body.setAttribute("data-thirds", "EARLY,MIDDLE,LATE");
    body.setAttribute("data-config_hash", avr.config_hash || "");
    body.setAttribute("data-provisional", avr.provisional ? "true" : "false");
    body.setAttribute("data-candle_time", String(last.time != null ? last.time : ""));
    body.textContent = lines.join("\n");
    panel.hidden = false;
  }

  function setPanelVisible(on) {
    state.panelVisible = !!on;
    var chk = $("fpShowAvrPanel");
    if (chk) chk.checked = state.panelVisible;
    if (!state.panelVisible) {
      state.panelExpanded = false;
      syncExpandButton();
    }
    updateAvrPanel(state.payload);
    syncAvrLegend();
    scheduleDraw();
  }

  function syncAvrLegend() {
    var el = $("fpAvrLegend");
    if (!el) return;
    var show = !!(state.enabled && state.panelVisible && !state.unsupported && modeSupported());
    el.hidden = !show;
    if (!show) return;
    if (el.getAttribute("data-built") === "1") return;
    var items = [
      { key: "SELLER_CONTROL", abbr: "S CTRL", label: "Verkäuferkontrolle" },
      { key: "BUYER_CONTROL", abbr: "B CTRL", label: "Käuferkontrolle" },
      { key: "SELL_ABSORPTION_CANDIDATE", abbr: "S ABS", label: "Verkaufsabsorption" },
      { key: "BUY_ABSORPTION_CANDIDATE", abbr: "B ABS", label: "Kaufabsorption" },
      { key: "VACUUM_DOWN_PROXY", abbr: "VAC ↓", label: "Abwärtsvakuum, Proxy" },
      { key: "VACUUM_UP_PROXY", abbr: "VAC ↑", label: "Aufwärtsvakuum, Proxy" },
      { key: "BALANCED", abbr: "BAL", label: "Ausgeglichen" },
      { key: "INSUFFICIENT_DATA", abbr: "Grau", label: "Daten unzureichend" }
    ];
    var html = '<span class="fp-avr-legend-title">AVR</span>';
    for (var i = 0; i < items.length; i += 1) {
      var it = items[i];
      var col = (STATE_VIS[it.key] && STATE_VIS[it.key].strip) || "rgba(40,44,52,0.35)";
      html +=
        '<span class="fp-avr-legend-item">' +
        '<span class="fp-avr-legend-swatch" style="background:' +
        col +
        '"></span>' +
        '<span class="fp-avr-legend-abbr">' +
        it.abbr +
        "</span>" +
        '<span class="fp-avr-legend-desc">= ' +
        it.label +
        "</span></span>";
    }
    html +=
      '<span class="fp-avr-legend-item">' +
      '<span class="fp-avr-legend-swatch fp-avr-legend-q">?</span>' +
      '<span class="fp-avr-legend-abbr">?</span>' +
      '<span class="fp-avr-legend-desc">= Nicht verifiziert</span></span>';
    el.innerHTML = html;
    el.setAttribute("data-built", "1");
  }

  function setPanelExpanded(on) {
    state.panelExpanded = !!on && !!state.panelVisible;
    syncExpandButton();
    updateAvrPanel(state.payload);
  }

  function syncExpandButton() {
    var btn = $("fpAvrExpand");
    if (!btn) return;
    btn.setAttribute("aria-expanded", state.panelExpanded ? "true" : "false");
    btn.disabled = !state.panelVisible;
  }

  function wirePanelControls() {
    var chk = $("fpShowAvrPanel");
    if (chk && !chk._fpBound) {
      state.panelVisible = !!chk.checked;
      chk.addEventListener("change", function () {
        setPanelVisible(chk.checked);
      });
      chk._fpBound = true;
    }
    var btn = $("fpAvrExpand");
    if (btn && !btn._fpBound) {
      btn.addEventListener("click", function () {
        if (!state.panelVisible) return;
        setPanelExpanded(!state.panelExpanded);
      });
      btn._fpBound = true;
    }
    syncExpandButton();
  }

  function bindCrosshair() {
    if (state.crosshairUnsub) {
      try { state.crosshairUnsub(); } catch (e) { /* ignore */ }
      state.crosshairUnsub = null;
    }
    if (!state.chart || typeof state.chart.subscribeCrosshairMove !== "function") return;
    function onMove(param) {
      if (!state.enabled) return;
      var t = null;
      if (param && param.time != null) {
        t = typeof param.time === "number" ? param.time : Number(param.time);
      }
      if (t != null && isFinite(t)) {
        /* snap to 5m bucket */
        t = Math.floor(t / 300) * 300;
        if (state.selectedTime !== t) {
          state.selectedTime = t;
          state.selectedLabel = "Ausgewählte Candle";
          updateAvrPanel(state.payload);
        }
        return;
      }
      /* Leave chart / empty crosshair → back to current (forming) candle */
      if (state.selectedTime != null) {
        state.selectedTime = null;
        state.selectedLabel = "Aktuelle Candle";
        updateAvrPanel(state.payload);
      }
    }
    state.chart.subscribeCrosshairMove(onMove);
    state.crosshairUnsub = function () {
      try {
        if (state.chart && state.chart.unsubscribeCrosshairMove) {
          state.chart.unsubscribeCrosshairMove(onMove);
        }
      } catch (e) { /* ignore */ }
    };
  }

  function clearCanvas() {
    var c = canvas();
    if (!c) return;
    var ctx = c.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, c.width, c.height);
  }

  function abortHistory() {
    if (state.historyInflight) {
      try {
        state.historyInflight.abort();
      } catch (e) { /* ignore */ }
      state.historyInflight = null;
    }
    state.historyLoading = false;
    syncLegacyInflight();
  }

  function abortForming() {
    if (state.formingInflight) {
      try {
        state.formingInflight.abort();
      } catch (e) { /* ignore */ }
      state.formingInflight = null;
    }
    state.formingLoading = false;
    syncLegacyInflight();
  }

  function abortFinalize() {
    if (state.finalizeInflight) {
      try {
        state.finalizeInflight.abort();
      } catch (e) { /* ignore */ }
      state.finalizeInflight = null;
    }
  }

  function abortAllFetches() {
    abortHistory();
    abortForming();
    abortFinalize();
  }

  function syncLegacyInflight() {
    state.inflight =
      state.historyInflight || state.formingInflight || state.finalizeInflight;
  }

  function stopFormingPoll() {
    if (state.formingTimer) {
      clearInterval(state.formingTimer);
      state.formingTimer = null;
    }
  }

  function modeSupported() {
    return (
      String(state.symbol || "").toUpperCase() === SUPPORTED_SYMBOL &&
      String(state.timeframe || "").toLowerCase() === SUPPORTED_TF
    );
  }

  function getBarSpacing() {
    try {
      if (state.chart && state.chart.timeScale) {
        var opt = state.chart.timeScale().options();
        if (opt && typeof opt.barSpacing === "number") return opt.barSpacing;
      }
      if (global.chartApi && global.chartApi.getBarSpacing) {
        return Number(global.chartApi.getBarSpacing()) || 0;
      }
    } catch (e) { /* ignore */ }
    return 0;
  }

  function getVisibleUnixRange() {
    if (!state.chart || !state.chart.timeScale) return null;
    try {
      var vr = state.chart.timeScale().getVisibleRange();
      if (!vr || vr.from == null || vr.to == null) return null;
      var from = typeof vr.from === "number" ? vr.from : Number(vr.from);
      var to = typeof vr.to === "number" ? vr.to : Number(vr.to);
      if (!isFinite(from) || !isFinite(to)) return null;
      from = Math.floor(from);
      to = Math.ceil(to);
      if (!(to > from)) return null;
      return { from: from, to: to };
    } catch (e) {
      return null;
    }
  }

  function getVisiblePriceRangeApprox() {
    try {
      if (!state.candleSeries || typeof state.candleSeries.coordinateToPrice !== "function") {
        return null;
      }
      var region = chartRegion();
      if (!region) return null;
      var hi = state.candleSeries.coordinateToPrice(0);
      var lo = state.candleSeries.coordinateToPrice(region.h);
      if (hi == null || lo == null) return null;
      return { high: Math.max(hi, lo), low: Math.min(hi, lo) };
    } catch (e) {
      return null;
    }
  }

  /** Clamp a requested window to the API hard cap (6h). Prefers keeping `to`. */
  function clampQueryWindow(from, to) {
    var maxSpan = MAX_SPAN_S;
    from = Math.floor(Number(from));
    to = Math.ceil(Number(to));
    if (!isFinite(from) || !isFinite(to) || !(to > from)) return null;
    if (to - from > maxSpan) from = to - maxSpan;
    if (!(to > from)) return null;
    return { from: from, to: to };
  }

  /** Exact current 5m bucket [start, end) for forming poll — never the full viewport. */
  function formingCandleWindow(nowUnix) {
    var now = Math.floor(Number(nowUnix));
    var bucket = Math.floor(now / 300) * 300;
    return { from: bucket, to: bucket + 300 };
  }

  function chartRegion() {
    var pane = $("price-pane") || $("mpChart");
    var chartEl = $("chart");
    if (!pane || !chartEl) return null;
    var pr = pane.getBoundingClientRect();
    var cr = chartEl.getBoundingClientRect();
    return {
      w: Math.max(1, Math.floor(cr.width || pr.width)),
      h: Math.max(1, Math.floor(cr.height || pr.height)),
      left: cr.left - pr.left,
      top: cr.top - pr.top
    };
  }

  function sizeCanvas(region) {
    var c = canvas();
    if (!c || !region) return null;
    var dpr = window.devicePixelRatio || 1;
    var w = region.w;
    var h = region.h;
    if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) {
      c.width = Math.round(w * dpr);
      c.height = Math.round(h * dpr);
    }
    c.style.width = w + "px";
    c.style.height = h + "px";
    c.style.left = (region.left || 0) + "px";
    c.style.top = (region.top || 0) + "px";
    var ctx = c.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    return ctx;
  }

  function priceToY(price) {
    if (!state.candleSeries) return null;
    var y = state.candleSeries.priceToCoordinate(price);
    return y === null || y === undefined || !isFinite(y) ? null : y;
  }

  function timeToX(t) {
    if (!state.chart || !state.chart.timeScale) return null;
    var x = state.chart.timeScale().timeToCoordinate(t);
    return x === null || x === undefined || !isFinite(x) ? null : x;
  }

  function barWidthPx() {
    var bs = getBarSpacing();
    if (bs > 0) return bs;
    return 0;
  }

  /** Legacy gate used by tests — raw level height, no adaptive. */
  function gateMode(levelHeight) {
    var bs = barWidthPx();
    if (bs < DELTA_BAR || levelHeight < DELTA_LEVEL_H) return "fallback";
    if (bs >= FULL_BAR && levelHeight >= FULL_LEVEL_H) return "full";
    return "delta";
  }

  function isTransparentColor(c) {
    if (c == null) return false;
    var s = String(c).toLowerCase().replace(/\s/g, "");
    return (
      s === "rgba(0,0,0,0)" ||
      s === "rgb(0,0,0,0)" ||
      s === "transparent" ||
      s === "#0000" ||
      s === "#00000000"
    );
  }

  function styleLooksTransparent(style) {
    if (!style) return false;
    return (
      isTransparentColor(style.upColor) &&
      isTransparentColor(style.downColor) &&
      isTransparentColor(style.wickUpColor) &&
      isTransparentColor(style.wickDownColor)
    );
  }

  function readCandleStyle(series) {
    if (!series || typeof series.options !== "function") return null;
    var opts;
    try {
      opts = series.options();
    } catch (e) {
      return null;
    }
    if (!opts) return null;
    var out = {};
    for (var i = 0; i < CANDLE_COLOR_KEYS.length; i += 1) {
      var k = CANDLE_COLOR_KEYS[i];
      if (opts[k] != null) out[k] = opts[k];
    }
    return out;
  }

  function buildTransparentStyle(base) {
    var out = {};
    var keys = base && Object.keys(base).length ? Object.keys(base) : CANDLE_COLOR_KEYS;
    for (var i = 0; i < keys.length; i += 1) {
      out[keys[i]] = TRANSPARENT;
    }
    out.upColor = TRANSPARENT;
    out.downColor = TRANSPARENT;
    out.borderUpColor = TRANSPARENT;
    out.borderDownColor = TRANSPARENT;
    out.wickUpColor = TRANSPARENT;
    out.wickDownColor = TRANSPARENT;
    return out;
  }

  function captureOriginalCandleStyle() {
    if (state.savedCandleStyle) return true;
    var series = state.candleSeries;
    if (!series) return false;
    var style = readCandleStyle(series);
    if (!style || !Object.keys(style).length) return false;
    if (styleLooksTransparent(style)) return false;
    state.savedCandleStyle = style;
    state.candleSeriesRef = series;
    return true;
  }

  function applyCandleStyle(style, expectTransparent) {
    var series = state.candleSeries;
    if (!series || typeof series.applyOptions !== "function" || !style) return false;
    try {
      series.applyOptions(style);
      state.candlesTransparent = !!expectTransparent;
      return true;
    } catch (e) {
      return false;
    }
  }

  function hideCandleBodies(genAtDecision) {
    if (genAtDecision != null && genAtDecision !== state.gen) return false;
    if (!state.enabled || !modeSupported() || state.unsupported) return false;
    if (!state.candleSeries) return false;
    if (state.candleSeriesRef && state.candleSeriesRef !== state.candleSeries) {
      state.savedCandleStyle = null;
      state.candlesTransparent = false;
      state.candleSeriesRef = null;
    }
    if (!captureOriginalCandleStyle()) {
      if (state.candlesTransparent) return true;
      return false;
    }
    if (state.candlesTransparent) return true;
    return applyCandleStyle(buildTransparentStyle(state.savedCandleStyle), true);
  }

  function restoreCandleBodies() {
    if (!state.candlesTransparent && !state.savedCandleStyle) return true;
    if (!state.savedCandleStyle) {
      state.candlesTransparent = false;
      return false;
    }
    if (!state.candleSeries) {
      state.candlesTransparent = false;
      return false;
    }
    var ok = applyCandleStyle(state.savedCandleStyle, false);
    if (ok) state.candlesTransparent = false;
    return ok;
  }

  /**
   * Visibility contract:
   * - transparent: FULL / DELTA / COMPACT actually drawn
   * - original: off, unsupported, MISSING, no levels, API error, zoom fallback
   */
  function syncCandleBodiesForMode(visMode, genAtDecision) {
    if (visMode === "full" || visMode === "delta" || visMode === "compact") {
      hideCandleBodies(genAtDecision);
    } else {
      restoreCandleBodies();
    }
  }

  /**
   * Decide visibility mode from module state + zoom (no canvas side effects).
   * Uses adaptive display-step selection when levelHeight = px per raw $5.
   */
  function resolveVisibilityMode(opts) {
    opts = opts || {};
    var enabled = opts.enabled != null ? opts.enabled : state.enabled;
    var unsupported = opts.unsupported != null ? opts.unsupported : state.unsupported;
    var symbol = opts.symbol != null ? opts.symbol : state.symbol;
    var timeframe = opts.timeframe != null ? opts.timeframe : state.timeframe;
    var payload = opts.payload !== undefined ? opts.payload : state.payload;
    var barSpacing = opts.barSpacing != null ? opts.barSpacing : barWidthPx();
    var levelHeight = opts.levelHeight != null ? opts.levelHeight : null;

    if (!enabled) return "off";
    if (
      unsupported ||
      String(symbol || "").toUpperCase() !== SUPPORTED_SYMBOL ||
      String(timeframe || "").toLowerCase() !== SUPPORTED_TF
    ) {
      return "unsupported";
    }
    if (!payload || !payload.candles || !payload.candles.length) return "no_data";
    var cov = payload.coverage || "UNKNOWN";
    if (cov === "MISSING") return "missing";

    var anyLevels = false;
    var candles = payload.candles;
    var i;
    for (i = 0; i < candles.length; i += 1) {
      if (candles[i].coverage === "MISSING") continue;
      if ((candles[i].levels || []).length) {
        anyLevels = true;
        break;
      }
    }
    if (!anyLevels) return "no_levels";

    var px = levelHeight != null ? Number(levelHeight) : 0;
    if (!(px > 0) && opts.pxPerRaw5 != null) px = Number(opts.pxPerRaw5);
    var plan = chooseRenderPlan({ barSpacing: barSpacing, pxPerRaw5: px, levelHeight: px });
    return plan.mode;
  }

  /* ---------- Store helpers ---------- */

  function candleTime(c) {
    return Number(c && (c.time != null ? c.time : c.open_time));
  }

  function avrMarkKey(candleStart) {
    return String(state.symbol) + "|" + String(state.timeframe) + "|" + String(candleStart);
  }

  function syncAvrMarksFromCandles() {
    var next = {};
    var list = state.historyCandles || [];
    for (var i = 0; i < list.length; i += 1) {
      var c = list[i];
      var t = candleTime(c);
      if (!isFinite(t) || !c || !c.avr) continue;
      next[avrMarkKey(t)] = {
        key: avrMarkKey(t),
        symbol: state.symbol,
        timeframe: state.timeframe,
        candle_start: t,
        dominant_state: c.avr.dominant_state,
        dominant_strength: c.avr.dominant_strength,
        state_share: c.avr.state_share,
        final_state: c.avr.final_state,
        third_states: c.avr.third_states,
        verification: c.avr.verification,
        provisional: !!c.avr.provisional,
        config_hash: c.avr.config_hash,
        coverage_status: c.avr.coverage_status,
        avr: c.avr
      };
    }
    state.avrMarks = next;
    return next;
  }

  function candleIsProvisional(c) {
    return !!(c && c.avr && c.avr.provisional);
  }

  function candleIsFinalized(c) {
    return !!(c && c.avr && c.avr.provisional === false);
  }

  /**
   * Upsert by candle_start. policy:
   *  - "replace" (default): last write wins
   *  - "history": protect live forming slot; never demote finalized → provisional
   *  - "forming": only accept current forming bucket (caller filters); replace that slot
   *  - "finalize": force closed summary over provisional
   */
  function upsertCandleList(existing, incoming, policy) {
    var pol = policy || "replace";
    var by = {};
    var list = existing || [];
    var i;
    for (i = 0; i < list.length; i += 1) {
      by[candleTime(list[i])] = list[i];
    }
    var formingStart = null;
    if (pol === "history") {
      formingStart = formingCandleWindow(Math.floor(Date.now() / 1000)).from;
    }
    var inc = incoming || [];
    for (i = 0; i < inc.length; i += 1) {
      var c = inc[i];
      var t = candleTime(c);
      if (!isFinite(t)) continue;
      var prev = by[t];
      if (pol === "history" && prev) {
        /* Do not clobber fresher local forming candle with history for same bucket */
        if (formingStart != null && t === formingStart && candleIsProvisional(prev)) {
          continue;
        }
        /* Never replace finalized closed candle with a provisional copy */
        if (candleIsFinalized(prev) && candleIsProvisional(c)) {
          continue;
        }
      }
      if (pol === "finalize" && prev && candleIsFinalized(prev) && candleIsProvisional(c)) {
        continue;
      }
      by[t] = c;
    }
    return Object.keys(by)
      .map(Number)
      .filter(isFinite)
      .sort(function (a, b) {
        return a - b;
      })
      .map(function (t) {
        return by[t];
      });
  }

  function pruneCandles(candles, keepFrom, keepTo) {
    var from = Math.floor(Number(keepFrom));
    var to = Math.ceil(Number(keepTo));
    if (!isFinite(from) || !isFinite(to)) return candles || [];
    /* retain a small pad so pan doesn't flash-empty */
    from -= 600;
    to += 600;
    return (candles || []).filter(function (c) {
      var t = candleTime(c);
      return t >= from && t < to;
    });
  }

  function rebuildPayloadView() {
    var candles = state.historyCandles.slice();
    var coverage = (state.historyMeta && state.historyMeta.coverage) || "UNKNOWN";
    var meta = state.historyMeta ? Object.assign({}, state.historyMeta) : {};
    state.payload = Object.assign({}, meta, {
      success: true,
      candles: candles,
      coverage: coverage
    });
    syncAvrMarksFromCandles();
    return state.payload;
  }

  function applyHistoryBody(body, queryFrom, queryTo) {
    if (!body || !body.candles) return;
    /* Merge into store; do not drop candles outside this response that are still needed */
    state.historyCandles = upsertCandleList(state.historyCandles, body.candles, "history");
    var vis = getVisibleUnixRange();
    var keepFrom = vis ? vis.from - BUFFER_S : queryFrom;
    var keepTo = vis ? vis.to + BUFFER_S : queryTo;
    var clamped = clampQueryWindow(keepFrom, keepTo);
    if (clamped) {
      state.historyCandles = pruneCandles(
        state.historyCandles,
        clamped.from,
        clamped.to + 300
      );
    }
    state.historyMeta = {
      coverage: body.coverage,
      coverage_note: body.coverage_note,
      range: body.range,
      symbol: body.symbol,
      timeframe: body.timeframe,
      avr_engine: body.avr_engine,
      meta: body.meta
    };
    rebuildPayloadView();
  }

  function applyFormingBody(body) {
    if (!body || !body.candles || !body.candles.length) return;
    var fw = formingCandleWindow(Math.floor(Date.now() / 1000));
    var latest = body.candles[body.candles.length - 1];
    var t = candleTime(latest);
    /* Stale forming response for a previous bucket: accept only if already finalized */
    if (isFinite(t) && t !== fw.from) {
      if (candleIsFinalized(latest)) {
        state.historyCandles = upsertCandleList(state.historyCandles, [latest], "finalize");
        rebuildPayloadView();
      }
      return;
    }
    /* Forming must never replace the history list wholesale */
    state.historyCandles = upsertCandleList(state.historyCandles, [latest], "forming");
    if (!state.historyMeta) {
      state.historyMeta = {
        coverage: body.coverage || "UNKNOWN"
      };
    }
    rebuildPayloadView();
  }

  function applyFinalizeBody(body) {
    if (!body || !body.candles || !body.candles.length) return;
    /* Prefer the candle matching requested closed interval; take last otherwise */
    var closed = body.candles[body.candles.length - 1];
    state.historyCandles = upsertCandleList(state.historyCandles, [closed], "finalize");
    rebuildPayloadView();
  }

  function clearContextStore() {
    state.historyCandles = [];
    state.historyMeta = null;
    state.payload = null;
    state.avrMarks = {};
    state.lastFormingBucket = null;
    state.selectedTime = null;
    state.selectedLabel = "Aktuelle Candle";
  }

  /* ---------- Draw ---------- */

  function rectsOverlap(a, b) {
    return !(a.x + a.w <= b.x || b.x + b.w <= a.x || a.y + a.h <= b.y || b.y + b.h <= a.y);
  }

  function clampLabelBox(box, region) {
    var x = box.x;
    var y = box.y;
    if (x < 2) x = 2;
    if (x + box.w > region.w - 2) x = Math.max(2, region.w - 2 - box.w);
    if (y < 2) y = 2;
    if (y + box.h > region.h - STATE_STRIP_H - TIME_AXIS_PAD - 4) {
      y = Math.max(2, region.h - STATE_STRIP_H - TIME_AXIS_PAD - 4 - box.h);
    }
    return { x: x, y: y, w: box.w, h: box.h };
  }

  function placeLabel(preferred, region, occupied, tryBelow) {
    var candidates = [preferred];
    if (tryBelow) {
      /* Extra vertical slots so adjacent candle badges can stack under priority */
      var offsets = [preferred.h + 4, preferred.h + 4 + preferred.h + 3, -(preferred.h + 4)];
      for (var oi = 0; oi < offsets.length; oi += 1) {
        candidates.push({
          x: preferred.x,
          y: preferred.y + offsets[oi],
          w: preferred.w,
          h: preferred.h
        });
      }
    }
    for (var i = 0; i < candidates.length; i += 1) {
      var box = clampLabelBox(candidates[i], region);
      var hit = false;
      for (var j = 0; j < occupied.length; j += 1) {
        if (rectsOverlap(box, occupied[j])) {
          hit = true;
          break;
        }
      }
      if (!hit) return box;
    }
    return null;
  }

  /** Candle silhouette: wick, body, open/close ticks (CSS pixels). */
  function drawCandleSilhouette(ctx, candle, x0, x1) {
    var yO = priceToY(candle.open);
    var yC = priceToY(candle.close);
    var yH = priceToY(candle.high);
    var yL = priceToY(candle.low);
    if (yH == null || yL == null) return null;
    var midX = (x0 + x1) / 2;
    var bullish = Number(candle.close) >= Number(candle.open);
    var stroke = bullish ? COLORS.bodyUpStroke : COLORS.bodyDownStroke;
    var fill = bullish ? COLORS.bodyUpFill : COLORS.bodyDownFill;
    var barW = Math.max(4, x1 - x0);
    var bodyW = Math.max(3, Math.min(barW * 0.55, barW - 2));
    var bx0 = midX - bodyW / 2;
    var tick = Math.max(TICK_LEN_MIN, Math.min(14, barW * 0.35));

    ctx.strokeStyle = COLORS.wick;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(midX, yH);
    ctx.lineTo(midX, yL);
    ctx.stroke();

    if (yO != null && yC != null) {
      var top = Math.min(yO, yC);
      var bot = Math.max(yO, yC);
      var h = Math.max(BODY_MIN_H, bot - top);
      if (Math.abs(yO - yC) < 0.75) {
        /* Doji: horizontal body line */
        ctx.strokeStyle = stroke;
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.moveTo(bx0, yO);
        ctx.lineTo(bx0 + bodyW, yO);
        ctx.stroke();
      } else {
        ctx.fillStyle = fill;
        ctx.strokeStyle = stroke;
        ctx.lineWidth = 1.25;
        ctx.fillRect(bx0, top, bodyW, h);
        ctx.strokeRect(bx0 + 0.5, top + 0.5, bodyW - 1, h - 1);
      }
    }

    if (yO != null) {
      ctx.strokeStyle = COLORS.openTick;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(midX - tick, yO);
      ctx.lineTo(midX, yO);
      ctx.stroke();
    }
    if (yC != null) {
      ctx.strokeStyle = bullish ? COLORS.closeUp : COLORS.closeDown;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(midX, yC);
      ctx.lineTo(midX + tick, yC);
      ctx.stroke();
    }
    return { yH: yH, yL: yL, midX: midX, bullish: bullish };
  }

  function drawDisplayVpoc(ctx, levels, x0, x1) {
    for (var i = 0; i < levels.length; i += 1) {
      var lv = levels[i];
      if (!lv.is_vpoc) continue;
      var yt = priceToY(lv.price_high);
      var yb = priceToY(lv.price_low);
      if (yt == null || yb == null) continue;
      var y = (yt + yb) / 2;
      ctx.strokeStyle = COLORS.vpoc;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(x0, y);
      ctx.lineTo(x1, y);
      ctx.stroke();
      return y;
    }
    return null;
  }

  function drawStrengthBar(ctx, box, strength, color, muted) {
    var s = Number(strength);
    if (!isFinite(s) || s <= 0) return;
    var maxW = box.w;
    var w = Math.max(2, Math.min(maxW, (Math.min(s, 100) / 100) * maxW));
    ctx.globalAlpha = muted ? 0.45 : 0.75;
    ctx.fillStyle = color || COLORS.hint;
    ctx.fillRect(box.x, box.y + box.h + 1, w, 2);
    ctx.globalAlpha = 1;
  }

  function drawStateBadge(ctx, spec, box, muted) {
    if (!spec || spec.kind === "none") return;
    if (spec.kind === "dot") {
      ctx.fillStyle = spec.color || COLORS.hint;
      ctx.beginPath();
      ctx.arc(box.x + box.w / 2, box.y + 3, 2, 0, Math.PI * 2);
      ctx.fill();
      return;
    }
    ctx.globalAlpha = muted || spec.muted ? 0.55 : 0.92;
    ctx.fillStyle = COLORS.badge;
    ctx.fillRect(box.x, box.y, box.w, box.h);
    ctx.strokeStyle = spec.color || COLORS.hint;
    ctx.lineWidth = 1;
    ctx.strokeRect(box.x + 0.5, box.y + 0.5, box.w - 1, box.h - 1);
    ctx.fillStyle = COLORS.badgeText;
    ctx.font = "9px DejaVu Sans Mono, monospace";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(spec.label, box.x + box.w / 2, box.y + box.h / 2 + 0.5);
    ctx.globalAlpha = 1;
    drawStrengthBar(ctx, box, spec.strength, spec.color, spec.muted);
  }

  function stripBandY(region) {
    return region.h - TIME_AXIS_PAD - STATE_STRIP_H;
  }

  function drawStateStrip(ctx, region, segments) {
    if (!state.panelVisible || !segments.length) return;
    var y = stripBandY(region);
    if (y < 4) return;
    /* Dark rail so the strip is always visible against the chart */
    ctx.globalAlpha = 0.92;
    ctx.fillStyle = "rgba(8, 10, 14, 0.88)";
    ctx.fillRect(0, y - 1, region.w, STATE_STRIP_H + 2);
    ctx.strokeStyle = "rgba(140, 150, 170, 0.45)";
    ctx.lineWidth = 1;
    ctx.strokeRect(0.5, y - 0.5, region.w - 1, STATE_STRIP_H + 1);

    for (var i = 0; i < segments.length; i += 1) {
      var seg = segments[i];
      if (!seg.color) continue;
      var x0 = Math.max(0, seg.x0);
      var x1 = Math.min(region.w, seg.x1);
      if (x1 - x0 < 1) continue;
      ctx.globalAlpha = seg.alpha != null ? seg.alpha : 0.9;
      ctx.fillStyle = seg.color;
      ctx.fillRect(x0, y, Math.max(2, x1 - x0 - 1), STATE_STRIP_H);
    }
    ctx.globalAlpha = 1;
    ctx.font = "9px DejaVu Sans Mono, monospace";
    ctx.fillStyle = "rgba(200, 210, 225, 0.9)";
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText("AVR", 4, y + STATE_STRIP_H / 2);
  }

  function stripAlphaForAvr(avr) {
    if (!avr) return 0.35;
    if (
      avr.verification === "UNVERIFIED" ||
      avr.coverage_status === "PARTIAL" ||
      avr.coverage_status === "UNKNOWN"
    ) {
      return 0.55;
    }
    return 0.85;
  }

  function pushStripSegment(stripSegs, avr, x0, x1) {
    if (!avr) return;
    var stName = avr.dominant_state || "";
    var vis = STATE_VIS[stName] || null;
    stripSegs.push({
      x0: x0,
      x1: x1,
      color: vis ? vis.strip : "rgba(40,44,52,0.35)",
      alpha: stripAlphaForAvr(avr)
      /* no "?" glyph in the strip — verification only via badge/panel */
    });
  }

  function drawAvrStripAndBadgesOnly(ctx, region, payload, bs, drawStats) {
    if (!state.panelVisible || !payload || !payload.candles) return;
    var occupied = [];
    var stripSegs = [];
    var half = Math.max(2, bs * 0.45);
    for (var i = 0; i < payload.candles.length; i += 1) {
      var candle = payload.candles[i];
      if (candle.coverage === "MISSING") continue;
      var xMid = timeToX(candle.time);
      if (xMid === null) continue;
      if (xMid < -bs || xMid > region.w + bs) continue;
      var x0 = xMid - half;
      var x1 = xMid + half;
      var avr = candle.avr;
      if (!avr) continue;
      pushStripSegment(stripSegs, avr, x0, x1);
      var spec = badgeSpec(avr);
      if (spec && spec.kind === "badge") {
        ctx.font = "9px DejaVu Sans Mono, monospace";
        var tw = ctx.measureText(spec.label).width + 10;
        var bh = 13;
        var prefer = { x: xMid - tw / 2, y: 18, w: tw, h: bh };
        var badgeBox = placeLabel(prefer, region, occupied, true);
        if (badgeBox) {
          occupied.push(badgeBox);
          drawStateBadge(ctx, spec, badgeBox, spec.muted);
          if (drawStats) drawStats.badges += 1;
        }
      } else if (spec && spec.kind === "dot") {
        drawStateBadge(ctx, spec, { x: xMid - 2, y: 22, w: 4, h: 4 }, false);
      }
    }
    drawStateStrip(ctx, region, stripSegs);
    if (drawStats) drawStats.stripSegs = stripSegs.length;
  }

  function scheduleDraw() {
    if (state.rafPending) return;
    state.rafPending = true;
    window.requestAnimationFrame(function () {
      state.rafPending = false;
      draw();
    });
  }

  function draw() {
    var genAtDraw = state.gen;
    if (!state.enabled) {
      clearCanvas();
      syncCandleBodiesForMode("off", genAtDraw);
      updateAvrPanel(null);
      return;
    }
    var region = chartRegion();
    var ctx = sizeCanvas(region);
    if (!ctx || !region) {
      syncCandleBodiesForMode("no_data", genAtDraw);
      return;
    }

    if (state.unsupported || !modeSupported()) {
      ctx.fillStyle = COLORS.hint;
      ctx.font = "12px DejaVu Sans Mono, monospace";
      ctx.fillText("Footprint für diesen Modus noch nicht verfügbar (nur BTCUSDT / 5m)", 12, 24);
      setStatus("Footprint: Modus nicht unterstützt", "warn");
      syncCandleBodiesForMode("unsupported", genAtDraw);
      return;
    }

    var payload = state.payload;
    if (!payload || !payload.candles || !payload.candles.length) {
      setStatus(state.historyLoading ? "Footprint lädt …" : "Footprint: keine Daten", "warn");
      syncCandleBodiesForMode("no_data", genAtDraw);
      return;
    }

    var cov = payload.coverage || "UNKNOWN";
    if (cov === "MISSING") {
      ctx.fillStyle = COLORS.missing;
      ctx.font = "12px DejaVu Sans Mono, monospace";
      ctx.fillText("Footprint-Daten fehlen", 12, 24);
      setStatus("Footprint-Daten fehlen", "error");
      syncCandleBodiesForMode("missing", genAtDraw);
      return;
    }

    var bs = barWidthPx();
    var samplePx = 0;
    var drawn = 0;
    var visCount = 0;
    var anyLevels = false;
    var plan = null;
    var i;
    var occupied = [];
    var stripSegs = [];
    var labelJobs = [];
    var drawStats = { silhouettes: 0, badges: 0, deltas: 0, levelTexts: 0, stripSegs: 0 };

    for (i = 0; i < payload.candles.length; i += 1) {
      var c0 = payload.candles[i];
      if (c0.coverage === "MISSING") continue;
      if (!(c0.levels || []).length) continue;
      anyLevels = true;
      var mid = c0.levels[Math.floor(c0.levels.length / 2)];
      var probe = mid && mid.price_low != null ? mid.price_low : c0.low;
      samplePx = measurePxPerRaw5(probe);
      if (samplePx > 0) break;
    }

    if (!anyLevels) {
      syncCandleBodiesForMode("no_levels", genAtDraw);
      setStatus("Footprint: keine Levels", "warn");
      updateAvrPanel(payload);
      return;
    }

    plan = chooseRenderPlan({ barSpacing: bs, pxPerRaw5: samplePx });
    state.lastRenderMode = plan.mode;
    state.lastDisplayStep = plan.displayStep || RAW_STEP;

    if (plan.mode === "fallback") {
      ctx.fillStyle = COLORS.hint;
      ctx.font = "11px DejaVu Sans Mono, monospace";
      ctx.fillText("Mehr hineinzoomen", 12, region.h - 28);
      /* Strip + badges remain so historical AVR is never lost when zoomed out */
      drawAvrStripAndBadgesOnly(ctx, region, payload, bs, drawStats);
      var stFb = statusForPlan(plan, cov);
      setStatus(stFb.text, stFb.kind);
      syncCandleBodiesForMode("fallback", genAtDraw);
      updateAvrPanel(payload);
      state.lastDrawStats = drawStats;
      return;
    }

    var displayStep = plan.displayStep || RAW_STEP;
    var mode = plan.mode;

    /* Legend */
    ctx.font = "10px DejaVu Sans Mono, monospace";
    ctx.fillStyle = COLORS.hint;
    ctx.textAlign = "left";
    if (mode === "compact") {
      ctx.fillText(
        displayStep > RAW_STEP + 1e-9
          ? "Compact · Raw $" + RAW_STEP + " · Display $" + displayStep
          : "Compact · Raw $" + RAW_STEP,
        12,
        14
      );
    } else {
      ctx.fillText("Raw $" + RAW_STEP + " · Display $" + displayStep, 12, 14);
    }

    for (i = 0; i < payload.candles.length; i += 1) {
      var candle = payload.candles[i];
      if (candle.coverage === "MISSING") continue;

      var xMid = timeToX(candle.time);
      if (xMid === null) continue;
      /* Skip off-screen candles so badges do not stack on the left edge */
      if (xMid < -bs || xMid > region.w + bs) continue;
      visCount += 1;
      var half = Math.max(2, bs * 0.45);
      var x0 = xMid - half;
      var x1 = xMid + half;
      var barW = x1 - x0;
      var avr = candle.avr;
      var rawLevels = candle.levels || [];
      var levels = [];
      var sil = null;

      if (rawLevels.length) {
        var agg = aggregateLevelsForDisplay(rawLevels, displayStep);
        levels = agg.levels || [];
      }

      if (levels.length) {
        drawn += 1;

        /* 1) Silhouette first */
        sil = drawCandleSilhouette(ctx, candle, x0, x1);
        if (sil) drawStats.silhouettes += 1;

        /* 2) Levels / vPOC by mode */
        if (mode === "full" || mode === "delta") {
        for (var li = 0; li < levels.length; li += 1) {
          var lv = levels[li];
          var yt = priceToY(lv.price_high);
          var yb = priceToY(lv.price_low);
          if (yt === null || yb === null) continue;
          if (yb < -4 || yt > region.h + 4) continue;
          var h = Math.max(1, yb - yt);
          var midLX = (x0 + x1) / 2;
          var delta = Number(lv.delta_size) || 0;
          ctx.fillStyle = delta >= 0 ? COLORS.askFill : COLORS.bidFill;
          ctx.fillRect(x0, yt, barW, h);
          if (lv.is_vpoc) {
            ctx.strokeStyle = COLORS.vpoc;
            ctx.lineWidth = 1.5;
            ctx.strokeRect(x0 + 0.5, yt + 0.5, barW - 1, Math.max(1, h - 1));
          }
          if (
            mode === "full" &&
            candle.coverage === "COMPLETE" &&
            (lv.ask_imbalance || lv.bid_imbalance || lv.stacked_ask || lv.stacked_bid)
          ) {
            ctx.fillStyle = lv.ask_imbalance || lv.stacked_ask ? COLORS.imbAsk : COLORS.imbBid;
            ctx.fillRect(x0, yt, 3, h);
            ctx.fillRect(x1 - 3, yt, 3, h);
          }
          if (mode === "full" && h >= FULL_LEVEL_H * 0.85 && barW >= FULL_BAR * 0.9) {
            ctx.font = "9px DejaVu Sans Mono, monospace";
            ctx.textBaseline = "middle";
            var yText = (yt + yb) / 2;
            ctx.fillStyle = COLORS.bid;
            ctx.textAlign = "left";
            ctx.fillText(fmtNotional(lv.bid_notional), x0 + 3, yText);
            ctx.fillStyle = COLORS.ask;
            ctx.textAlign = "right";
            ctx.fillText(fmtNotional(lv.ask_notional), x1 - 3, yText);
            ctx.strokeStyle = "rgba(120,130,150,0.35)";
            ctx.beginPath();
            ctx.moveTo(midLX, yt + 1);
            ctx.lineTo(midLX, yb - 1);
            ctx.stroke();
            drawStats.levelTexts += 1;
          } else if (mode === "delta" && h >= DELTA_LEVEL_H && barW >= DELTA_BAR * 0.85) {
            ctx.font = "9px DejaVu Sans Mono, monospace";
            ctx.textAlign = "center";
            ctx.textBaseline = "middle";
            ctx.fillStyle = delta >= 0 ? COLORS.ask : COLORS.bid;
            ctx.fillText(fmtCompactDelta(lv.delta_notional != null ? lv.delta_notional : delta), midLX, (yt + yb) / 2);
          }
        }
        } else if (mode === "compact") {
          drawDisplayVpoc(ctx, levels, x0, x1);
        }
      }

      /* Defer labels: badges must win over deltas across candles (priority). */
      labelJobs.push({
        candle: candle,
        avr: avr,
        xMid: xMid,
        x0: x0,
        x1: x1,
        barW: barW,
        yHi: sil ? sil.yH : priceToY(candle.high),
        yLo: sil ? sil.yL : priceToY(candle.low),
        mode: mode
      });

      /* State strip segment — always one per candle with AVR (priority #1) */
      if (state.panelVisible && avr) {
        pushStripSegment(stripSegs, avr, x0, x1);
      }
    }

    /* Pass A: classification badges for every candle (before any delta). */
    if (state.panelVisible) {
      for (i = 0; i < labelJobs.length; i += 1) {
        var job = labelJobs[i];
        var specA = badgeSpec(job.avr);
        if (specA && specA.kind === "badge") {
          ctx.font = "9px DejaVu Sans Mono, monospace";
          var twA = ctx.measureText(specA.label).width + 10;
          var bhA = 13;
          var preferA = {
            x: job.xMid - twA / 2,
            y: (job.yHi != null ? job.yHi : 20) - bhA - 4,
            w: twA,
            h: bhA
          };
          var badgeBoxA = placeLabel(preferA, region, occupied, true);
          if (badgeBoxA) {
            occupied.push(badgeBoxA);
            drawStateBadge(ctx, specA, badgeBoxA, specA.muted);
            drawStats.badges += 1;
            job.badgePlaced = true;
          }
        } else if (specA && specA.kind === "dot" && job.yLo != null) {
          drawStateBadge(ctx, specA, { x: job.xMid - 2, y: job.yLo + 4, w: 4, h: 4 }, false);
          job.badgePlaced = true;
        }
        job.spec = specA;
      }
    }

    /* Pass B: candle deltas — yield to badges (never block classification labels). */
    for (i = 0; i < labelJobs.length; i += 1) {
      var jobB = labelJobs[i];
      var avrB = jobB.avr;
      var specB = jobB.spec;
      var showCandleDelta =
        jobB.mode === "compact" || jobB.mode === "full" || jobB.mode === "delta";
      if (
        !showCandleDelta ||
        !avrB ||
        jobB.barW < COMPACT_BAR ||
        (specB && specB.kind === "badge" && !jobB.badgePlaced)
      ) {
        continue;
      }
      var dVal =
        avrB.candle_delta_notional != null
          ? avrB.candle_delta_notional
          : jobB.candle.candle_delta_notional;
      var dFull = fmtCompactDelta(dVal);
      var dShort = fmtCompactDelta(dVal);
      if (!(Math.abs(Number(dVal) || 0) >= 1e6) && jobB.barW < FULL_BAR) {
        dFull = dShort;
      }
      if (!dFull) continue;
      ctx.font = "9px DejaVu Sans Mono, monospace";
      var dw = ctx.measureText(dFull).width + 4;
      var dPrefer = {
        x: jobB.xMid - dw / 2,
        y:
          (jobB.yHi != null ? jobB.yHi : 16) -
          (specB && specB.kind === "badge" ? 28 : 14),
        w: dw,
        h: 11
      };
      var dBox = placeLabel(dPrefer, region, occupied, true);
      if (!dBox && dFull !== dShort) {
        dw = ctx.measureText(dShort).width + 4;
        dPrefer.w = dw;
        dPrefer.x = jobB.xMid - dw / 2;
        dBox = placeLabel(dPrefer, region, occupied, true);
        dFull = dShort;
      }
      if (dBox) {
        occupied.push(dBox);
        ctx.fillStyle = Number(dVal) >= 0 ? COLORS.ask : COLORS.bid;
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.fillText(dFull, dBox.x + dBox.w / 2, dBox.y);
        drawStats.deltas += 1;
      }
    }

    drawStateStrip(ctx, region, stripSegs);
    drawStats.stripSegs = stripSegs.length;

    if (genAtDraw !== state.gen) return;

    var st = statusForPlan(plan, cov);
    setStatus(st.text, st.kind);
    syncCandleBodiesForMode(mode, genAtDraw);
    updateAvrPanel(payload);
    state.lastDrawStats = drawStats;

    debugLog({
      mode: mode,
      displayStep: displayStep,
      barSpacing: bs,
      pxPerRaw5: samplePx,
      canvasCssH: region.h,
      historyN: state.historyCandles.length,
      drawn: drawn,
      visCandles: visCount,
      drawStats: drawStats
    });
  }

  /* ---------- Fetch ---------- */

  function buildParams(from, to) {
    return new URLSearchParams({
      symbol: SUPPORTED_SYMBOL,
      timeframe: SUPPORTED_TF,
      mode: MODE,
      bucket_step: String(BUCKET_STEP),
      from: String(Math.floor(from)),
      to: String(Math.ceil(to))
    });
  }

  function fetchHistory(range) {
    if (!state.enabled || !modeSupported()) return;
    if (!range) return;
    if (state.historyInflight) {
      /* one history request at a time — abort previous */
      abortHistory();
    }
    var gen = state.historyGen;
    var from = range.from - BUFFER_S;
    var to = range.to + BUFFER_S;
    var clamped = clampQueryWindow(from, to);
    if (!clamped) {
      setStatus("Footprint: ungültiger Zeitbereich", "error");
      return;
    }
    from = clamped.from;
    to = clamped.to;

    var ctrl = new AbortController();
    state.historyInflight = ctrl;
    state.historyLoading = true;
    syncLegacyInflight();
    setStatus("Footprint lädt …", "busy");

    fetch("/api/footprint-candles?" + buildParams(from, to).toString(), {
      signal: ctrl.signal,
      credentials: "same-origin"
    })
      .then(function (res) {
        return res.json().then(function (body) {
          return { ok: res.ok, body: body };
        });
      })
      .then(function (out) {
        if (gen !== state.historyGen || ctrl.signal.aborted) return;
        if (!state.enabled || !modeSupported() || state.unsupported) {
          restoreCandleBodies();
          return;
        }
        if (!out.ok || !out.body || out.body.success !== true) {
          var msg = (out.body && out.body.message) || "Footprint-Fehler";
          setStatus(msg, "error");
          restoreCandleBodies();
          scheduleDraw();
          return;
        }
        applyHistoryBody(out.body, from, to);
        scheduleDraw();
      })
      .catch(function (err) {
        if (err && err.name === "AbortError") return;
        if (gen !== state.historyGen) return;
        setStatus("Footprint Netzwerkfehler", "error");
        restoreCandleBodies();
        scheduleDraw();
      })
      .then(function () {
        if (state.historyInflight === ctrl) {
          state.historyInflight = null;
          state.historyLoading = false;
          syncLegacyInflight();
        }
      });
  }

  function fetchFinalize(candleStart) {
    if (!state.enabled || !modeSupported()) return Promise.resolve();
    var from = Math.floor(Number(candleStart));
    if (!isFinite(from)) return Promise.resolve();
    var to = from + 300;
    var gen = state.finalizeGen;
    var ctrl = new AbortController();
    state.finalizeInflight = ctrl;
    syncLegacyInflight();

    return fetch("/api/footprint-candles?" + buildParams(from, to).toString(), {
      signal: ctrl.signal,
      credentials: "same-origin"
    })
      .then(function (res) {
        return res.json().then(function (body) {
          return { ok: res.ok, body: body };
        });
      })
      .then(function (out) {
        if (gen !== state.finalizeGen || ctrl.signal.aborted) return;
        if (!state.enabled || !modeSupported() || state.unsupported) return;
        if (!out.ok || !out.body || out.body.success !== true) return;
        applyFinalizeBody(out.body);
        scheduleDraw();
      })
      .catch(function (err) {
        if (err && err.name === "AbortError") return;
      })
      .then(function () {
        if (state.finalizeInflight === ctrl) {
          state.finalizeInflight = null;
          syncLegacyInflight();
        }
      });
  }

  function fetchForming() {
    if (!state.enabled || !modeSupported()) return;
    if (typeof document !== "undefined" && document.hidden) return;
    if (state.formingInflight || state.finalizeInflight) return;

    var now = Math.floor(Date.now() / 1000);
    var fw = formingCandleWindow(now);
    var prevBucket = state.lastFormingBucket;

    /* Bucket rolled: finalize previous closed candle before polling the new forming slot */
    if (prevBucket != null && prevBucket !== fw.from) {
      state.lastFormingBucket = fw.from;
      state.finalizeGen += 1;
      fetchFinalize(prevBucket).then(function () {
        if (!state.enabled || !modeSupported()) return;
        doFetchForming(fw);
      });
      return;
    }
    state.lastFormingBucket = fw.from;
    doFetchForming(fw);
  }

  function doFetchForming(fw) {
    if (!state.enabled || !modeSupported()) return;
    if (state.formingInflight) return;
    var gen = state.formingGen;
    var ctrl = new AbortController();
    state.formingInflight = ctrl;
    state.formingLoading = true;
    syncLegacyInflight();

    fetch("/api/footprint-candles?" + buildParams(fw.from, fw.to).toString(), {
      signal: ctrl.signal,
      credentials: "same-origin"
    })
      .then(function (res) {
        return res.json().then(function (body) {
          return { ok: res.ok, body: body };
        });
      })
      .then(function (out) {
        if (gen !== state.formingGen || ctrl.signal.aborted) return;
        if (!state.enabled || !modeSupported() || state.unsupported) return;
        if (!out.ok || !out.body || out.body.success !== true) return;
        applyFormingBody(out.body);
        scheduleDraw();
      })
      .catch(function (err) {
        if (err && err.name === "AbortError") return;
      })
      .then(function () {
        if (state.formingInflight === ctrl) {
          state.formingInflight = null;
          state.formingLoading = false;
          syncLegacyInflight();
        }
      });
  }

  /** @deprecated thin wrapper kept for tests / older hooks */
  function fetchRange(range, formingOnly) {
    if (formingOnly) {
      fetchForming();
      return;
    }
    fetchHistory(range);
  }

  function requestVisible(opts) {
    opts = opts || {};
    if (!state.enabled) return;
    if (!modeSupported()) {
      state.unsupported = true;
      stopFormingPoll();
      abortAllFetches();
      state.payload = null;
      state.historyCandles = [];
      state.historyMeta = null;
      scheduleDraw();
      return;
    }
    state.unsupported = false;
    if (opts.formingOnly) {
      fetchForming();
      return;
    }
    var range = getVisibleUnixRange();
    if (!range) {
      setStatus("Footprint: sichtbarer Bereich fehlt", "warn");
      return;
    }
    fetchHistory(range);
  }

  function debouncedReload() {
    if (!state.enabled) return;
    if (!modeSupported()) {
      requestVisible();
      return;
    }
    if (state.debounceTimer) clearTimeout(state.debounceTimer);
    state.debounceTimer = setTimeout(function () {
      state.debounceTimer = null;
      state.historyGen += 1;
      state.gen += 1;
      requestVisible();
    }, DEBOUNCE_MS);
  }

  function startFormingPoll() {
    stopFormingPoll();
    if (!state.enabled || !modeSupported()) return;
    state.formingTimer = setInterval(function () {
      if (!state.enabled || !modeSupported()) {
        stopFormingPoll();
        return;
      }
      if (typeof document !== "undefined" && document.hidden) return;
      fetchForming();
    }, FORMING_MS);
  }

  function enable() {
    state.enabled = true;
    state.gen += 1;
    state.historyGen += 1;
    state.formingGen += 1;
    state.finalizeGen += 1;
    clearContextStore();
    var c = canvas();
    if (c) c.hidden = false;
    wirePanelControls();
    bindCrosshair();
    syncAvrLegend();
    /* Immediate visible-range history fetch, then forming poll (separate controllers). */
    requestVisible();
    startFormingPoll();
  }

  function disable() {
    state.enabled = false;
    state.gen += 1;
    state.historyGen += 1;
    state.formingGen += 1;
    state.finalizeGen += 1;
    abortAllFetches();
    stopFormingPoll();
    if (state.debounceTimer) {
      clearTimeout(state.debounceTimer);
      state.debounceTimer = null;
    }
    if (state.crosshairUnsub) {
      try { state.crosshairUnsub(); } catch (e) { /* ignore */ }
      state.crosshairUnsub = null;
    }
    clearContextStore();
    state.unsupported = false;
    state.panelExpanded = false;
    syncExpandButton();
    clearCanvas();
    restoreCandleBodies();
    updateAvrPanel(null);
    syncAvrLegend();
    var c = canvas();
    if (c) c.hidden = true;
    setStatus("");
  }

  function setContext(ctx) {
    if (!ctx) return;
    var prevSym = state.symbol;
    var prevTf = state.timeframe;
    if (ctx.chart) {
      state.chart = ctx.chart;
      if (state.enabled) bindCrosshair();
    }
    if (ctx.candleSeries) {
      if (state.candleSeries && ctx.candleSeries !== state.candleSeries) {
        restoreCandleBodies();
        state.savedCandleStyle = null;
        state.candlesTransparent = false;
        state.candleSeriesRef = null;
      }
      state.candleSeries = ctx.candleSeries;
    }
    if (ctx.symbol != null) state.symbol = ctx.symbol;
    if (ctx.timeframe != null) state.timeframe = ctx.timeframe;
    if (ctx.statusEl) state.statusEl = ctx.statusEl;
    if (!state.enabled) return;
    var changed = prevSym !== state.symbol || prevTf !== state.timeframe;
    if (!modeSupported()) {
      state.gen += 1;
      state.historyGen += 1;
      state.formingGen += 1;
      state.finalizeGen += 1;
      abortAllFetches();
      stopFormingPoll();
      state.unsupported = true;
      clearContextStore();
      restoreCandleBodies();
      syncAvrLegend();
      scheduleDraw();
      return;
    }
    if (changed) {
      state.gen += 1;
      state.historyGen += 1;
      state.formingGen += 1;
      state.finalizeGen += 1;
      abortAllFetches();
      state.unsupported = false;
      restoreCandleBodies();
      clearContextStore();
      syncAvrLegend();
      requestVisible();
      startFormingPoll();
    }
  }

  function onVisibleRange() {
    if (!state.enabled) return;
    debouncedReload();
  }

  function onResize() {
    if (!state.enabled) return;
    scheduleDraw();
  }

  function isEnabled() {
    return !!state.enabled;
  }

  function cleanup() {
    disable();
    state.savedCandleStyle = null;
    state.candlesTransparent = false;
    state.candleSeriesRef = null;
  }

  if (typeof document !== "undefined") {
    document.addEventListener("visibilitychange", function () {
      if (!state.enabled) return;
      if (document.hidden) return;
      if (!modeSupported()) return;
      fetchForming();
    });
  }
  if (typeof window !== "undefined") {
    window.addEventListener("pagehide", cleanup);
  }

  global.FootprintCandles = {
    setContext: setContext,
    enable: enable,
    disable: disable,
    isEnabled: isEnabled,
    onVisibleRange: onVisibleRange,
    onResize: onResize,
    scheduleDraw: scheduleDraw,
    redraw: scheduleDraw,
    cleanup: cleanup,
    setPanelVisible: setPanelVisible,
    setPanelExpanded: setPanelExpanded,
    setSelectedTime: function (t) {
      state.selectedTime = t;
      state.selectedLabel = t != null ? "Ausgewählte Candle" : "Aktuelle Candle";
      updateAvrPanel(state.payload);
    },
    /* test / debug hooks */
    _state: state,
    _gateMode: gateMode,
    _fmtNotional: fmtNotional,
    _fmtCompactDelta: fmtCompactDelta,
    _clearCanvas: clearCanvas,
    _clampQueryWindow: clampQueryWindow,
    _formingCandleWindow: formingCandleWindow,
    _resolveVisibilityMode: resolveVisibilityMode,
    _syncCandleBodiesForMode: syncCandleBodiesForMode,
    _hideCandleBodies: hideCandleBodies,
    _restoreCandleBodies: restoreCandleBodies,
    _captureOriginalCandleStyle: captureOriginalCandleStyle,
    _buildTransparentStyle: buildTransparentStyle,
    _TRANSPARENT: TRANSPARENT,
    _CANDLE_COLOR_KEYS: CANDLE_COLOR_KEYS,
    _AVR_BADGES: AVR_BADGES,
    _STATE_VIS: STATE_VIS,
    _fmtDeltaHeader: fmtDeltaHeader,
    _badgeForAvr: badgeForAvr,
    _badgeSpec: badgeSpec,
    _drawCandleSilhouette: drawCandleSilhouette,
    _placeLabel: placeLabel,
    _rectsOverlap: rectsOverlap,
    _chooseDisplayStep: chooseDisplayStep,
    _chooseRenderPlan: chooseRenderPlan,
    _aggregateLevelsForDisplay: aggregateLevelsForDisplay,
    _alignDisplayLow: alignDisplayLow,
    _upsertCandleList: upsertCandleList,
    _pruneCandles: pruneCandles,
    _avrMarkKey: avrMarkKey,
    _syncAvrMarksFromCandles: syncAvrMarksFromCandles,
    _applyHistoryBody: applyHistoryBody,
    _applyFormingBody: applyFormingBody,
    _applyFinalizeBody: applyFinalizeBody,
    _fetchFinalize: fetchFinalize,
    _fetchHistory: fetchHistory,
    _fetchForming: fetchForming,
    _fetchRange: fetchRange,
    _abortHistory: abortHistory,
    _abortForming: abortForming,
    _abortFinalize: abortFinalize,
    _pickPanelCandle: pickPanelCandle,
    _pushStripSegment: pushStripSegment,
    _syncAvrLegend: syncAvrLegend,
    _clearContextStore: clearContextStore,
    _DISPLAY_STEPS: DISPLAY_STEPS,
    _FULL_BAR: FULL_BAR,
    _DELTA_BAR: DELTA_BAR,
    _COMPACT_BAR: COMPACT_BAR,
    _FULL_LEVEL_H: FULL_LEVEL_H,
    _DELTA_LEVEL_H: DELTA_LEVEL_H,
    _STATE_STRIP_H: STATE_STRIP_H,
    _statusForPlan: statusForPlan,
    _debugLog: debugLog,
    _humanState: humanState
  };
})(window);
