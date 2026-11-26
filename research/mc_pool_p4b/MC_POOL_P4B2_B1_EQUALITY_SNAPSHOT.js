/**
 * MC-POOL-P4B2 — Minimal B1 equality snapshot (TOP frame, after P4A harness optional).
 * Read-only: fingerprints pool_ids + segment signatures + renderKey (not counts-only).
 */
(function () {
  "use strict";
  if (window.__MC_POOL_P4B2_EQ__) return window.__MC_POOL_P4B2_EQ__;

  function researchPane(paneId) {
    var st = window.__researchDebug;
    if (!st || !st.panes) return null;
    if (paneId) return st.panes[paneId] || null;
    return st.panes["pane-0"] || st.panes[Object.keys(st.panes)[0]] || null;
  }

  function poolIdsFromChunks(pane) {
    var ids = new Set();
    if (!pane) return [];
    var stores = pane.exactOriginWindowStores || {};
    Object.keys(stores).forEach(function (sk) {
      var store = stores[sk];
      if (!store || !store.chunks) return;
      Object.keys(store.chunks).forEach(function (ck) {
        var ent = store.chunks[ck];
        if (!ent || ent.status !== "ok" || !ent.pools) return;
        ent.pools.forEach(function (p) {
          if (p && p.pool_id != null) ids.add(String(p.pool_id));
        });
      });
    });
    return Array.from(ids).sort();
  }

  function segmentSignature(pane) {
    var segs =
      (pane &&
        pane.exactOriginSegmentCache &&
        pane.exactOriginSegmentCache.segments) ||
      [];
    return segs
      .map(function (s) {
        var md = (s && s.metadata) || {};
        var lo = s && s.price_lo != null ? s.price_lo : s && s.start != null ? s.start : "";
        var hi = s && s.price_hi != null ? s.price_hi : s && s.end != null ? s.end : "";
        return [lo, hi, md.depth, md.pool_id, md.origin_count].join(":");
      })
      .sort()
      .join("|");
  }

  function metricsSlice() {
    var R = window.PD1ExactOriginResearch;
    if (!R || !R.getLastMetrics) return null;
    var m = R.getLastMetrics() || {};
    return {
      pools: m.pools,
      rawZones: m.rawZones,
      mergedSegments: m.mergedSegments,
      sweepCacheHit: m.sweepCacheHit,
      windowFetch: m.windowFetch,
      adapterMs: m.adapterMs,
      canvasSegmentsDrawn: m.canvasSegmentsDrawn,
      canvasSegmentsTotal: m.canvasSegmentsTotal,
    };
  }

  function fingerprint(tag, paneId) {
    var pane = researchPane(paneId);
    var R = window.PD1ExactOriginResearch;
    return {
      tag: tag || "snap",
      t: Date.now(),
      pane: pane ? pane.id || paneId || "pane-0" : null,
      renderKey: pane && pane.exactOriginSegmentCache ? pane.exactOriginSegmentCache.key : null,
      stripCapsHiMs: pane ? pane.exactOriginStripCapsHiMs : null,
      loadSerial: pane ? pane.exactOriginLoadSerial : null,
      freshnessKnown:
        R && R.exactOriginReenableFreshnessBaselineKnown && pane
          ? !!R.exactOriginReenableFreshnessBaselineKnown(pane)
          : null,
      pool_ids: poolIdsFromChunks(pane),
      segment_sig: segmentSignature(pane),
      metrics: metricsSlice(),
    };
  }

  function diffKeys(a, b, keys) {
    var issues = [];
    (keys || []).forEach(function (k) {
      var va = a && a[k];
      var vb = b && b[k];
      if (JSON.stringify(va) !== JSON.stringify(vb)) issues.push(k);
    });
    return issues;
  }

  function compare(ref, cand) {
    var issues = diffKeys(ref, cand, [
      "pool_ids",
      "segment_sig",
      "renderKey",
      "metrics",
    ]);
    return {
      equal: !issues.length,
      issues: issues,
      ref: ref,
      cand: cand,
    };
  }

  window.__MC_POOL_P4B2_EQ__ = {
    fingerprint: fingerprint,
    compare: compare,
    help: function () {
      console.log(
        "[MC-POOL-P4B2-EQ] B1 flow:",
        "1) stabil ON → ref=__MC_POOL_P4B2_EQ__.fingerprint('ref_canonical')",
        "2) OFF→ON (cache path) → cand=__MC_POOL_P4B2_EQ__.fingerprint('cand_reenable')",
        "3) __MC_POOL_P4B2_EQ__.compare(ref,cand)",
        "Optional canonical: await PD1ExactOriginResearch.refreshPane(pane,{force:true}) before ref"
      );
    },
  };
  console.log("[MC-POOL-P4B2-EQ] ready — __MC_POOL_P4B2_EQ__.help()");
  return window.__MC_POOL_P4B2_EQ__;
})();