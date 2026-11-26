#!/usr/bin/env node
"use strict";

/** Policy predicates — keep aligned with pd1_exact_origin_density_research_v1.js (MC-POOL-P4B). */

function activeScopeSignature(scopes) {
  return (scopes || [])
    .map(function (sc) {
      return sc.timeframe + "/pd" + sc.pool_detail;
    })
    .join(",");
}

function exactOriginReenableScopeUnchanged(pane, scopes, sym, tf) {
  if (!pane) return false;
  const sig = activeScopeSignature(scopes);
  if (pane.exactOriginStripScopeSig != null && pane.exactOriginStripScopeSig !== sig) return false;
  if (pane.exactOriginStripChartTf != null && pane.exactOriginStripChartTf !== tf) return false;
  if (pane.exactOriginStripSymbol != null && pane.exactOriginStripSymbol !== sym) return false;
  return true;
}

function exactOriginReenableCapsHiAdvanced(pane, hiMsNow) {
  if (!pane || hiMsNow == null) return false;
  if (pane.exactOriginStripCapsHiMs == null) return false;
  return hiMsNow > pane.exactOriginStripCapsHiMs;
}

function exactOriginReenableFreshnessBaselineKnown(pane) {
  return !!(pane && pane.exactOriginStripCapsHiMs != null);
}

function allowSegmentRepaintOnReEnable(pane, hiMsNow, fullyCached, segMatch, hiAdvanced) {
  return (
    fullyCached &&
    segMatch &&
    !hiAdvanced &&
    exactOriginReenableFreshnessBaselineKnown(pane)
  );
}

function stripVisualPreservesStores(paneBefore, paneAfterStripVisual) {
  return (
    paneBefore.exactOriginWindowStores === paneAfterStripVisual.exactOriginWindowStores &&
    paneBefore.exactOriginSegmentCache === paneAfterStripVisual.exactOriginSegmentCache
  );
}

let fail = 0;
function ok(cond, msg) {
  if (!cond) {
    fail += 1;
    console.error("FAIL", msg);
  }
}

const paneBase = {
  exactOriginStripScopeSig: "5m/pd1",
  exactOriginStripChartTf: "5m",
  exactOriginStripSymbol: "SOLUSDT",
  exactOriginStripCapsHiMs: 1000,
};

ok(
  exactOriginReenableScopeUnchanged(paneBase, [{ timeframe: "5m", pool_detail: 1 }], "SOLUSDT", "5m"),
  "scope unchanged same"
);
ok(
  !exactOriginReenableScopeUnchanged(paneBase, [{ timeframe: "5m", pool_detail: 5 }], "SOLUSDT", "5m"),
  "pd change"
);
ok(
  !exactOriginReenableScopeUnchanged(paneBase, [{ timeframe: "5m", pool_detail: 1 }], "SOLUSDT", "15m"),
  "chart tf change"
);
ok(
  !exactOriginReenableScopeUnchanged(paneBase, [{ timeframe: "5m", pool_detail: 1 }], "BNBUSDT", "5m"),
  "symbol change"
);
ok(!exactOriginReenableCapsHiAdvanced(paneBase, 1000), "hi not advanced");
ok(exactOriginReenableCapsHiAdvanced(paneBase, 1001), "hi advanced");
ok(exactOriginReenableFreshnessBaselineKnown(paneBase), "freshness baseline known");
ok(!exactOriginReenableFreshnessBaselineKnown({ exactOriginStripCapsHiMs: null }), "freshness unknown");
ok(
  allowSegmentRepaintOnReEnable(paneBase, 1000, true, true, false),
  "repaint allowed when fresh"
);
ok(
  !allowSegmentRepaintOnReEnable({ exactOriginStripCapsHiMs: null }, 1000, true, true, false),
  "repaint blocked when baseline unknown"
);
ok(
  !allowSegmentRepaintOnReEnable(paneBase, 1001, true, true, true),
  "repaint blocked when hi advanced"
);
(function () {
  const stores = { sk: { chunks: { k: 1 }, order: ["k"] } };
  const seg = { key: "1-2|sym", segments: [{ t: 1 }] };
  const before = { exactOriginWindowStores: stores, exactOriginSegmentCache: seg, exactOriginLoadSerial: 1 };
  const afterVisual = {
    exactOriginWindowStores: stores,
    exactOriginSegmentCache: seg,
    exactOriginLoadSerial: 2,
  };
  ok(stripVisualPreservesStores(before, afterVisual), "visual strip keeps store refs");
  const afterFull = { exactOriginWindowStores: {}, exactOriginSegmentCache: null, exactOriginLoadSerial: 3 };
  ok(!stripVisualPreservesStores(before, afterFull), "full reset clears stores");
})();

if (fail) process.exit(1);
console.log(JSON.stringify({ ok: true, tests: 13, suite: "mc_pool_p4b_cache_lifecycle_v1" }));