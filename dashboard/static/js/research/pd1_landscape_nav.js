/**
 * PD1 Landscape Viewer — thin navigation adapter shared with Research Charts.
 * UTC parsing and GO-TO window geometry come from goto_time.js (ResearchGotoTime).
 * Canvas pan/zoom stay in replay.js; this module documents the contract only.
 */
(function (root) {
  "use strict";

  function requireGoto() {
    if (typeof ResearchGotoTime === "undefined") {
      throw new Error("ResearchGotoTime missing — load /static/js/research/goto_time.js first");
    }
    return ResearchGotoTime;
  }

  function parseGoToMs(raw) {
    const u = requireGoto().parseGotoUtcToUnix(raw);
    return u == null ? null : u * 1000;
  }

  function gotoLoadWindowMs(tMs) {
    const g = requireGoto();
    const w = g.gotoLoadWindow(Math.floor(tMs / 1000));
    return {
      fromMs: w.from * 1000,
      toMs: w.to * 1000,
      viewPadMs: w.viewPad * 1000,
      asOfIso: w.as_of_iso,
    };
  }

  function spanContainsMs(t0, t1, tMs) {
    return t1 > t0 && tMs >= t0 && tMs <= t1;
  }

  root.Pd1LandscapeNav = {
    parseGoToMs,
    gotoLoadWindowMs,
    spanContainsMs,
  };
})(typeof globalThis !== "undefined" ? globalThis : this);
