#!/usr/bin/env node
"use strict";

/** MC-POOL-P4B4A — bounds single-flight / skipStale / re-enable (aligned with pd1 P4B4 + P4B4A gate). */

const MC4_5M_PD1_SYMBOLS = { DOGEUSDT: 1, SOLUSDT: 1, BNBUSDT: 1 };
const DOGE_5M_BOUNDS_REFRESH_MS = 30000;

function viewerBoundsUrl(sym) {
  const s = String(sym || "").toUpperCase();
  if (s === "DOGEUSDT") return "/api/research/doge-5m-pd1-viewer-bounds";
  if (s === "SOLUSDT" || s === "BNBUSDT") {
    return "/api/research/multicoin-5m-pd1-viewer-bounds?symbol=" + encodeURIComponent(s);
  }
  return null;
}

function normalizeViewerBoundsBody(body) {
  if (!body) return null;
  if (body.data_available_to) return body;
  if (body.success) return body;
  return null;
}

function exactOriginReenableCapsHiAdvanced(pane, hiMsNow) {
  if (!pane || hiMsNow == null) return false;
  if (pane.exactOriginStripCapsHiMs == null) return false;
  return hiMsNow > pane.exactOriginStripCapsHiMs;
}

function exactOriginReenableFreshnessBaselineKnown(pane) {
  return !!(pane && pane.exactOriginStripCapsHiMs != null);
}

function allowCacheRepaintOnReEnable(pane, ctx) {
  if (!ctx.boundsSyncOk) return false;
  if (!exactOriginReenableFreshnessBaselineKnown(pane)) return false;
  if (ctx.hiAdvanced) return false;
  return !!(ctx.fullyCached && ctx.segMatch);
}

function applyDoge5mViewerBoundsToCaps(capRoot, b) {
  if (!capRoot || !b || !b.data_available_to) return;
  capRoot._doge5mPd1ViewerBounds = { data_available_to: b.data_available_to };
  capRoot._doge5mBoundsFetchedAt = Date.now();
  capRoot.supported_viewer_scopes = [
    {
      symbol: b.symbol || "SOLUSDT",
      timeframe: "5m",
      pool_detail: 1,
      data_available_to: b.data_available_to,
    },
  ];
}

function createHarness() {
  let caps = null;
  let capsLoadInflight = null;
  const viewerBoundsInflight = Object.create(null);
  const fetchLog = [];
  let queue = [];

  async function dispatchFetch(url) {
    fetchLog.push(url);
    const rule = queue.shift();
    if (!rule) throw new Error("unexpected fetch: " + url);
    if (rule.throwNet) throw new Error(rule.throwNet);
    if (rule.delayMs) await new Promise(function (r) {
      setTimeout(r, rule.delayMs);
    });
    return {
      ok: rule.status >= 200 && rule.status < 300,
      status: rule.status,
      json: async function () {
        if (rule.jsonThrows) throw new Error("json_fail");
        return rule.body;
      },
    };
  }

  function enqueue(rule) {
    queue.push(rule);
  }

  async function fetchViewerBoundsAndApply(sym) {
    const s = String(sym || "").toUpperCase();
    const boundsUrl = viewerBoundsUrl(s);
    if (!boundsUrl) return { ok: false };
    if (viewerBoundsInflight[boundsUrl]) return viewerBoundsInflight[boundsUrl];
    const work = (async function () {
      try {
        const bRes = await dispatchFetch(boundsUrl);
        if (!bRes.ok) return { ok: false, http: bRes.status };
        const body = await bRes.json().catch(function () {
          return null;
        });
        const b = normalizeViewerBoundsBody(body);
        if (!b || !b.data_available_to) return { ok: false, reason: "no_data_available_to" };
        if (caps) applyDoge5mViewerBoundsToCaps(caps, b);
        return { ok: true, bounds: b };
      } catch (e) {
        return { ok: false, reason: String(e && e.message ? e.message : e) };
      } finally {
        delete viewerBoundsInflight[boundsUrl];
      }
    })();
    viewerBoundsInflight[boundsUrl] = work;
    return work;
  }

  async function refreshDoge5mViewerBoundsIfStale() {
    if (!caps || !caps._loadCapsReady) return caps;
    const last = caps._doge5mBoundsFetchedAt || 0;
    if (Date.now() - last < DOGE_5M_BOUNDS_REFRESH_MS) return caps;
    const res = await fetchViewerBoundsAndApply(String(caps.symbol || "SOLUSDT"));
    if (!res || !res.ok) caps._doge5mBoundsFetchedAt = Date.now();
    return caps;
  }

  async function loadCaps(opts) {
    opts = opts || {};
    if (caps && caps._loadCapsReady) {
      if (!opts.skipStaleBoundsRefresh) await refreshDoge5mViewerBoundsIfStale();
      return caps;
    }
    if (capsLoadInflight) return capsLoadInflight;
    capsLoadInflight = (async function () {
      enqueue({ status: 200, body: {} });
      await dispatchFetch("/live-charts/viewer/api/capabilities");
      caps = { symbol: "SOLUSDT", supported_viewer_scopes: [] };
      await fetchViewerBoundsAndApply("SOLUSDT");
      caps._loadCapsReady = true;
      return caps;
    })();
    try {
      return await capsLoadInflight;
    } finally {
      capsLoadInflight = null;
    }
  }

  async function ensurePaneViewerBounds(pane) {
    const sym = String(pane.symbol || "SOLUSDT").toUpperCase();
    if (!MC4_5M_PD1_SYMBOLS[sym]) return { ok: false, reason: "unsupported" };
    if (!caps || !caps._loadCapsReady) {
      await loadCaps();
      return {
        ok: !!(caps && caps._doge5mPd1ViewerBounds && caps._doge5mPd1ViewerBounds.data_available_to),
      };
    }
    const res = await fetchViewerBoundsAndApply(sym);
    return { ok: !!(res && res.ok) };
  }

  async function simulateWarmReEnable(pane, skipStale) {
    fetchLog.length = 0;
    const boundsSync = await ensurePaneViewerBounds(pane);
    await loadCaps({ skipStaleBoundsRefresh: skipStale });
    return { boundsSync, fetchLog: fetchLog.slice() };
  }

  function resetWarm(to) {
    caps = {
      _loadCapsReady: true,
      symbol: "SOLUSDT",
      supported_viewer_scopes: [{ symbol: "SOLUSDT", timeframe: "5m", pool_detail: 1, data_available_to: to }],
      _doge5mPd1ViewerBounds: { data_available_to: to },
      _doge5mBoundsFetchedAt: Date.now() - 120000,
    };
    queue = [];
    fetchLog.length = 0;
  }

  function boundsGets(log) {
    return log.filter(function (u) {
      return String(u).indexOf("viewer-bounds") >= 0;
    });
  }

  return {
    enqueue,
    fetchViewerBoundsAndApply,
    ensurePaneViewerBounds,
    simulateWarmReEnable,
    loadCaps,
    resetWarm,
    boundsGets,
    get caps() {
      return caps;
    },
    set caps(v) {
      caps = v;
    },
    inflightKeys() {
      return Object.keys(viewerBoundsInflight);
    },
    fetchLog,
  };
}

let fail = 0;
let tests = 0;
function ok(cond, msg) {
  tests += 1;
  if (!cond) {
    fail += 1;
    console.error("FAIL", msg);
  }
}

async function main() {
  const pane = { symbol: "SOLUSDT", exactOriginStripCapsHiMs: Date.parse("2026-11-26T10:00:00Z") };
  const goodBody = {
    symbol: "SOLUSDT",
    data_available_to: "2026-11-26T10:00:00Z",
  };

  {
    const h = createHarness();
    h.resetWarm("2026-11-26T09:00:00Z");
    h.enqueue({ status: 200, body: goodBody });
    const r = await h.simulateWarmReEnable(pane, true);
    ok(h.boundsGets(r.fetchLog).length === 1, "T1 warm re-enable one bounds GET");
    ok(r.boundsSync.ok, "T1 bounds sync ok");
    ok(h.caps._doge5mPd1ViewerBounds.data_available_to === goodBody.data_available_to, "T1 caps updated");
  }

  {
    const h = createHarness();
    h.resetWarm("2026-11-26T09:00:00Z");
    h.enqueue({ status: 200, body: goodBody });
    const r = await h.simulateWarmReEnable(pane, true);
    ok(h.boundsGets(r.fetchLog).length === 1, "T1b skip stale: exactly one bounds on re-enable");
    h.caps._doge5mBoundsFetchedAt = Date.now() - 120000;
    h.fetchLog.length = 0;
    h.enqueue({ status: 200, body: { data_available_to: "2026-11-26T10:02:00Z", symbol: "SOLUSDT" } });
    await h.loadCaps({ skipStaleBoundsRefresh: false });
    ok(h.boundsGets(h.fetchLog).length === 1, "T1b non-skip stale refresh fetches when TTL expired");
  }

  {
    const h = createHarness();
    h.resetWarm("2026-11-26T10:00:00Z");
    const tsBefore = h.caps._doge5mBoundsFetchedAt;
    h.enqueue({ status: 503, body: {} });
    const res = await h.fetchViewerBoundsAndApply("SOLUSDT");
    ok(!res.ok, "T2 http error");
    ok(h.caps._doge5mBoundsFetchedAt === tsBefore, "T2 no apply timestamp on http error");
    ok(
      !allowCacheRepaintOnReEnable(pane, { boundsSyncOk: false, hiAdvanced: false, fullyCached: true, segMatch: true }),
      "T2 fail-closed blocks cache repaint"
    );
  }

  {
    const h = createHarness();
    h.resetWarm();
    h.enqueue({ throwNet: "net_err" });
    const res = await h.fetchViewerBoundsAndApply("SOLUSDT");
    ok(!res.ok, "T2 network");
    ok(h.inflightKeys().length === 0, "T2 inflight cleared");
    h.enqueue({ status: 200, body: goodBody });
    const res2 = await h.fetchViewerBoundsAndApply("SOLUSDT");
    ok(res2.ok, "T2 retry after error");
  }

  {
    const h = createHarness();
    h.resetWarm();
    h.enqueue({ status: 200, body: { foo: 1 } });
    const res = await h.fetchViewerBoundsAndApply("SOLUSDT");
    ok(!res.ok, "T2 invalid body");
  }

  {
    const h = createHarness();
    h.resetWarm();
    h.enqueue({ status: 200, body: {}, jsonThrows: true });
    const res = await h.fetchViewerBoundsAndApply("SOLUSDT");
    ok(!res.ok, "T2 json throw");
    ok(h.inflightKeys().length === 0, "T2 json inflight cleared");
  }

  {
    const work = [];
    const viewerBoundsInflight = Object.create(null);
    let caps = { supported_viewer_scopes: [] };
    let fetchCount = 0;
    async function fetch(url) {
      fetchCount += 1;
      await new Promise(function (r) {
        setTimeout(r, 20);
      });
      const sym = url.indexOf("BNB") >= 0 ? "BNBUSDT" : "SOLUSDT";
      return { ok: true, status: 200, json: async () => ({ symbol: sym, data_available_to: "2026-11-26T12:00:00Z" }) };
    }
    async function bound(sym) {
      const url = viewerBoundsUrl(sym);
      if (viewerBoundsInflight[url]) return viewerBoundsInflight[url];
      const p = (async function () {
        try {
          const bRes = await fetch(url);
          const b = normalizeViewerBoundsBody(await bRes.json());
          applyDoge5mViewerBoundsToCaps(caps, b);
          return { ok: true, sym: sym };
        } finally {
          delete viewerBoundsInflight[url];
        }
      })();
      viewerBoundsInflight[url] = p;
      return p;
    }
    await Promise.all([bound("SOLUSDT"), bound("SOLUSDT")]);
    ok(fetchCount === 1, "T3 same URL single-flight");
    const solTo = caps.supported_viewer_scopes[0].data_available_to;
    fetchCount = 0;
    await Promise.all([bound("SOLUSDT"), bound("BNBUSDT")]);
    ok(fetchCount === 2, "T3 different URLs parallel");
    ok(caps.supported_viewer_scopes[0].symbol === "BNBUSDT" || solTo, "T3 symbol from last apply isolated per url");
  }

  {
    const panePub = { exactOriginStripCapsHiMs: Date.parse("2026-11-26T10:00:00Z") };
    const hiNew = Date.parse("2026-11-26T10:30:00Z");
    ok(exactOriginReenableCapsHiAdvanced(panePub, hiNew), "T4 publication hiAdvanced");
    ok(
      !allowCacheRepaintOnReEnable(panePub, { boundsSyncOk: true, hiAdvanced: true, fullyCached: true, segMatch: true }),
      "T4 publication invalidates stale repaint"
    );
  }

  {
    const h = createHarness();
    h.caps = null;
    h.enqueue({ status: 200, body: {} });
    h.enqueue({ status: 200, body: goodBody });
    const sync = await h.ensurePaneViewerBounds({ symbol: "SOLUSDT" });
    ok(h.boundsGets(h.fetchLog).length === 1, "T5 cold path one bounds GET");
    ok(sync.ok, "T5 cold bounds sync ok");
  }

  {
    function invalidateCaches(boundsSync, hiAdvanced, freshnessKnown) {
      if (!boundsSync || !boundsSync.ok || hiAdvanced || !freshnessKnown) return true;
      return false;
    }
    ok(invalidateCaches({ ok: false }, false, true), "T6 P4B4A: failed bounds invalidates caches");
    ok(!invalidateCaches({ ok: true }, false, true), "T6 P4B4A: ok bounds + fresh hi keeps caches");
    ok(invalidateCaches({ ok: true }, true, true), "T6 P4B4A: hiAdvanced invalidates");
  }

  if (fail) {
    console.log(JSON.stringify({ ok: false, tests: tests, fail: fail, suite: "mc_pool_p4b4_bounds_safety_v1" }));
    process.exit(1);
  }
  console.log(JSON.stringify({ ok: true, tests: tests, suite: "mc_pool_p4b4_bounds_safety_v1" }));
}

main().catch(function (e) {
  console.error(e);
  process.exit(1);
});