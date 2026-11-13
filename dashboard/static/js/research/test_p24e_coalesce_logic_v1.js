#!/usr/bin/env node
"use strict";

function eoRefreshViewportUnixPair(opts) {
  const o = opts || {};
  if (o.fromUnix == null || o.toUnix == null) return null;
  const from = Math.floor(Number(o.fromUnix));
  const to = Math.floor(Number(o.toUnix));
  if (!Number.isFinite(from) || !Number.isFinite(to) || from >= to) return null;
  return { from: from, to: to };
}

function eoRefreshPendingSupersedesInflight(pending) {
  const p = pending || {};
  return !!(p.viewport || p.force || (p.fromUnix != null && p.toUnix != null));
}

function pendingRefreshMateriallyChangesInflight(inflightOpts, pending) {
  const p = pending || {};
  const i = inflightOpts || {};
  if (p.force) return true;
  const pVp = eoRefreshViewportUnixPair(p);
  const iVp = eoRefreshViewportUnixPair(i);
  if (pVp && iVp) {
    return pVp.from !== iVp.from || pVp.to !== iVp.to;
  }
  if (pVp && !iVp) return true;
  if (!pVp && iVp) return false;
  if (p.viewport && !p.force && !pVp) {
    return false;
  }
  return eoRefreshPendingSupersedesInflight(p);
}

const base = { fromUnix: 1000, toUnix: 2000, viewport: true };
let fail = 0;
function ok(cond, msg) {
  if (!cond) {
    fail += 1;
    console.error("FAIL", msg);
  }
}

ok(!pendingRefreshMateriallyChangesInflight(base, { fromUnix: 1000, toUnix: 2000, viewport: true }), "identical vp");
ok(!pendingRefreshMateriallyChangesInflight(base, { viewport: true }), "bare viewport vs bounded inflight");
ok(pendingRefreshMateriallyChangesInflight(base, { fromUnix: 1001, toUnix: 2000, viewport: true }), "moved from");
ok(pendingRefreshMateriallyChangesInflight(base, { force: true }), "force");
ok(pendingRefreshMateriallyChangesInflight({ viewport: true }, { fromUnix: 1, toUnix: 2, viewport: true }), "new explicit vp on open inflight");

if (fail) process.exit(1);
console.log(JSON.stringify({ ok: true, tests: 5 }));
