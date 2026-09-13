/**
 * Pure Market-Profile frontend hotpath helpers (Node + browser).
 * No network, no DOM, no ClickHouse.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  if (root) {
    root.MpHotpathHelpers = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  /** Visible-tab forming poll interval (ms). Hidden tabs pause entirely. */
  var FORMING_MS = 1000;
  /** Legacy baseline interval (for offline before/after accounting). */
  var FORMING_MS_BASELINE = 250;

  function numOrNull(v) {
    var n = Number(v);
    return Number.isFinite(n) ? n : null;
  }

  /**
   * Signature of every forming field consumed by updateFormingBar / candle tip.
   */
  function formingConsumedSignature(bar) {
    if (!bar || typeof bar !== "object") return "";
    var parts = [
      numOrNull(bar.time),
      numOrNull(bar.open),
      numOrNull(bar.high),
      numOrNull(bar.low),
      numOrNull(bar.close),
      bar.volume == null ? "" : numOrNull(bar.volume),
      bar.final == null && bar.is_final == null
        ? ""
        : String(bar.final != null ? bar.final : bar.is_final),
      bar.forming == null ? "" : String(!!bar.forming),
      bar.seq == null && bar.sequence == null
        ? ""
        : String(bar.seq != null ? bar.seq : bar.sequence),
      bar.version == null ? "" : String(bar.version)
    ];
    return parts.join("|");
  }

  function shouldApplyForming(prevSignature, bar) {
    var next = formingConsumedSignature(bar);
    return {
      apply: next !== "" && next !== prevSignature,
      signature: next
    };
  }

  function createRafCoalescer(rafFn, cafFn) {
    var pending = false;
    var handle = null;
    var raf = rafFn || function (cb) {
      return setTimeout(cb, 0);
    };
    var caf = cafFn || function (id) {
      clearTimeout(id);
    };
    return {
      schedule: function (run) {
        pending = true;
        if (handle != null) return;
        handle = raf(function () {
          handle = null;
          if (!pending) return;
          pending = false;
          run();
        });
      },
      flush: function (run) {
        if (handle != null) {
          caf(handle);
          handle = null;
        }
        if (!pending) return;
        pending = false;
        run();
      },
      cancel: function () {
        pending = false;
        if (handle != null) {
          caf(handle);
          handle = null;
        }
      },
      isArmed: function () {
        return handle != null || pending;
      }
    };
  }

  function createFormingPollController(opts) {
    opts = opts || {};
    var intervalMs = opts.intervalMs != null ? opts.intervalMs : FORMING_MS;
    var setIntervalFn = opts.setInterval || setInterval;
    var clearIntervalFn = opts.clearInterval || clearInterval;
    var isHidden = opts.isHidden || function () { return false; };
    var fetchForming = opts.fetchForming;
    var onBar = opts.onBar || function () {};
    var gen = 0;
    var timer = null;
    var inflight = false;
    var lastSig = "";
    var stats = {
      fetches: 0,
      applied: 0,
      skippedSame: 0,
      skippedHidden: 0,
      skippedStale: 0,
      skippedInflight: 0
    };

    function stop() {
      gen += 1;
      if (timer != null) {
        clearIntervalFn(timer);
        timer = null;
      }
      inflight = false;
    }

    function pause() {
      if (timer != null) {
        clearIntervalFn(timer);
        timer = null;
      }
    }

    function poll(myGen) {
      if (myGen !== gen) return;
      if (isHidden()) {
        stats.skippedHidden += 1;
        return;
      }
      if (inflight) {
        stats.skippedInflight += 1;
        return;
      }
      if (typeof fetchForming !== "function") return;
      inflight = true;
      stats.fetches += 1;
      Promise.resolve()
        .then(function () { return fetchForming(); })
        .then(function (forming) {
          if (myGen !== gen) {
            stats.skippedStale += 1;
            return;
          }
          if (!forming) return;
          var decision = shouldApplyForming(lastSig, forming);
          if (!decision.apply) {
            stats.skippedSame += 1;
            return;
          }
          onBar(forming);
          lastSig = decision.signature;
          stats.applied += 1;
        })
        .catch(function () { /* best-effort */ })
        .then(function () {
          inflight = false;
        });
    }

    function start() {
      stop();
      lastSig = "";
      var myGen = gen;
      timer = setIntervalFn(function () {
        if (myGen !== gen) return;
        if (isHidden()) return;
        poll(myGen);
      }, intervalMs);
      poll(myGen);
    }

    function onVisibilityChange() {
      if (isHidden()) {
        pause();
        return;
      }
      start();
    }

    return {
      FORMING_MS: intervalMs,
      start: start,
      stop: stop,
      pause: pause,
      poll: poll,
      onVisibilityChange: onVisibilityChange,
      getGen: function () { return gen; },
      getStats: function () { return Object.assign({}, stats); },
      getLastSignature: function () { return lastSig; },
      hasTimer: function () { return timer != null; },
      resetStats: function () {
        stats = {
          fetches: 0,
          applied: 0,
          skippedSame: 0,
          skippedHidden: 0,
          skippedStale: 0,
          skippedInflight: 0
        };
      }
    };
  }

  function requestKey(parts) {
    var p = parts || {};
    return [
      p.layer || "",
      p.symbol || "",
      p.timeframe || "",
      p.mpTimeframe || "",
      p.start != null ? String(p.start) : "",
      p.end != null ? String(p.end) : "",
      p.config || "",
      p.generation != null ? String(p.generation) : ""
    ].join("\x1f");
  }

  function createInflightDedupe() {
    var map = Object.create(null);
    return {
      begin: function (key) {
        if (!key) return true;
        if (map[key]) return false;
        map[key] = true;
        return true;
      },
      end: function (key) {
        if (!key) return;
        delete map[key];
      },
      has: function (key) {
        return !!map[key];
      }
    };
  }

  /** Expected fetches per visible minute for a given interval. */
  function formingFetchesPerMinute(intervalMs) {
    return Math.floor(60000 / (intervalMs || FORMING_MS));
  }

  return {
    FORMING_MS: FORMING_MS,
    FORMING_MS_BASELINE: FORMING_MS_BASELINE,
    formingConsumedSignature: formingConsumedSignature,
    shouldApplyForming: shouldApplyForming,
    createRafCoalescer: createRafCoalescer,
    createFormingPollController: createFormingPollController,
    requestKey: requestKey,
    createInflightDedupe: createInflightDedupe,
    formingFetchesPerMinute: formingFetchesPerMinute
  };
});
