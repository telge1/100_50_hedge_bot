/* MC-OPI-P3L iframe recorder — active only when host Opi E2E diag is on */
(function (root) {
  "use strict";

  var active = false;
  var pending = [];
  var MAX_PENDING = 300;

  function note(kind, data) {
    if (!active) return;
    pending.push({ t: new Date().toISOString(), kind: kind, data: data || {} });
    while (pending.length > MAX_PENDING) pending.shift();
  }

  root.OpiE2eRecord = {
    start: function () {
      active = true;
      pending.length = 0;
      note("e2e_iframe_start", {});
      return true;
    },
    stop: function () {
      note("e2e_iframe_stop", {});
      active = false;
      return true;
    },
    note: note,
    drain: function () {
      var out = pending.slice();
      pending.length = 0;
      return out;
    },
    isActive: function () {
      return active;
    },
  };

  try {
    if (root.OpiPane && root.OpiPane.recordLifecycleStage) {
      var orig = root.OpiPane.recordLifecycleStage;
      root.OpiPane.recordLifecycleStage = function (stage, extra) {
        var ret = orig.call(root.OpiPane, stage, extra);
        if (active) note("lifecycle", { stage: stage, extra: extra || null });
        return ret;
      };
    }
  } catch (e) { /* ignore */ }
})(typeof window !== "undefined" ? window : globalThis);