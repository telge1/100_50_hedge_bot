/**
 * V3 race scenarios — mirrors orphan-owner hotfix helpers + loadPane finalize path.
 */
const assert = require("assert");
const fs = require("fs");
const path = require("path");

const js = fs.readFileSync(
  path.join(__dirname, "..", "static", "js", "research", "research_charts.js"),
  "utf8"
);
assert.ok(js.includes("scheduleOrphanPaneLoadRecovery"), "production orphan recovery");

const API_PACKED = JSON.parse(
  fs.readFileSync("/tmp/doge_pane_ui.json", "utf8")
);

const PANE_LOAD_RECOVERY_MAX = 2;
const LOADING = "loading…";

function classify(statusText, owner, inFlight) {
  const loading = statusText === LOADING;
  if (!loading) {
    if (/^\d+ ·/.test(statusText) || statusText.includes(" · ready")) return "LOADED";
    if (statusText.startsWith("error")) return "ERROR";
    return "OTHER";
  }
  if (owner || inFlight) return "ACTIVE_LOADING_WITH_PENDING_REQUEST";
  return "LOADING_WITHOUT_ACTIVE_REQUEST";
}

function makeEnv() {
  const status = { text: "" };
  const pane = {
    id: "p1",
    tf: API_PACKED.timeframe || "5m",
    paneGen: 0,
    pendingData: null,
    _paneLoadSeq: 0,
    _paneLoadOwner: null,
    _paneLoadInFlight: null,
    _paneLoadRecoveryTimer: null,
    _paneLoadRecoveryCount: 0,
    el: {
      querySelector: () => ({
        get textContent() { return status.text; },
        set textContent(v) { status.text = v; },
      }),
    },
  };
  const state = { loadGen: 0, replayGen: 0, chartTimeMode: "LIVE", symbol: "DOGEUSDT", panes: { p1: pane } };
  let reqId = 0;
  let loadPaneFn;

  function paneStatusEl(p) { return p.el.querySelector(".trp-pane-status"); }
  function paneStatusFromPendingData(p) {
    const rows = (p.pendingData && p.pendingData.candles) || [];
    return rows.length ? rows.length + " · ready" : "no data";
  }
  function paneLoadOwnerIsCurrent(p, owner) {
    return !!(p && owner && p._paneLoadOwner === owner);
  }
  function clearPaneLoadRecoveryTimer(p) {
    if (p._paneLoadRecoveryTimer) {
      clearTimeout(p._paneLoadRecoveryTimer);
      p._paneLoadRecoveryTimer = null;
    }
  }
  function resetPaneLoadRecoveryCount(p) { p._paneLoadRecoveryCount = 0; }
  function registerPaneLoadOwner(p, paneId, paneGen, gen) {
    const owner = { paneId, paneGen, loadGen: gen, seq: (p._paneLoadSeq || 0) + 1 };
    p._paneLoadSeq = owner.seq;
    p._paneLoadOwner = owner;
    p._paneLoadInFlight = owner;
    return owner;
  }
  function paneLoadGenerationStale(p, paneGen, gen, reqReplayGen) {
    if (gen !== state.loadGen || paneGen !== p.paneGen) return true;
    return false;
  }
  function schedulePaneLoadRecovery(paneId, owner) {
    const p = state.panes[paneId];
    if (!p || p._paneLoadOwner !== owner) return;
    if (p._paneLoadRecoveryTimer) return;
    p._paneLoadRecoveryTimer = setTimeout(function () {
      p._paneLoadRecoveryTimer = null;
      if (p._paneLoadOwner !== owner) return;
      tickRecovery(paneId, owner);
    }, 0);
  }
  function scheduleOrphanPaneLoadRecovery(paneId) {
    const p = state.panes[paneId];
    if (!p || p._paneLoadOwner) return;
    if (p._paneLoadRecoveryTimer) return;
    p._paneLoadRecoveryTimer = setTimeout(function () {
      p._paneLoadRecoveryTimer = null;
      if (p._paneLoadOwner) return;
      tickRecovery(paneId, null);
    }, 0);
  }
  function tickRecovery(paneId, owner) {
    const p = state.panes[paneId];
    const st = paneStatusEl(p);
    if (!st || st.textContent !== LOADING) return;
    if (p.pendingData && (p.pendingData.candles || []).length) {
      st.textContent = paneStatusFromPendingData(p);
      if (owner) p._paneLoadOwner = null;
      resetPaneLoadRecoveryCount(p);
      return;
    }
    const attempts = p._paneLoadRecoveryCount || 0;
    if (attempts >= PANE_LOAD_RECOVERY_MAX) {
      st.textContent = "error · load recovery limit";
      return;
    }
    p._paneLoadRecoveryCount = attempts + 1;
    void loadPaneFn(paneId, { force: true, sourceAction: "recovery", delayMs: 5 });
  }
  function discardPaneLoadResult(p, paneId, owner) {
    if (!p || p._paneLoadOwner !== owner) return;
    schedulePaneLoadRecovery(paneId, owner);
  }
  function exitSupersededPaneLoad(p, paneId, owner, paneGen, gen, reqReplayGen) {
    if (paneLoadGenerationStale(p, paneGen, gen, reqReplayGen)) {
      discardPaneLoadResult(p, paneId, owner);
      return true;
    }
    if (!paneLoadOwnerIsCurrent(p, owner)) {
      if (!p._paneLoadOwner) scheduleOrphanPaneLoadRecovery(paneId);
      return true;
    }
    return false;
  }
  function applyPaneBundle(p, pk, opts) {
    if (opts && opts.throwAt) throw new Error("apply throw");
    p.pendingData = { candles: pk.candles, symbol: pk.symbol, timeframe: pk.timeframe, is_demo: false };
    paneStatusEl(p).textContent = (pk.candles || []).length + " · " + (pk.cache || "load");
  }
  function releasePaneLoadOwner(p, owner) {
    if (!paneLoadOwnerIsCurrent(p, owner)) return;
    p._paneLoadInFlight = null;
    p._paneLoadOwner = null;
    clearPaneLoadRecoveryTimer(p);
    resetPaneLoadRecoveryCount(p);
  }

  async function loadPane(paneId, opts) {
    const p = state.panes[paneId];
    const id = ++reqId;
    const gen = opts && opts.gen != null ? opts.gen : state.loadGen;
    const paneGen = ++p.paneGen;
    const owner = registerPaneLoadOwner(p, paneId, paneGen, gen);
    paneStatusEl(p).textContent = LOADING;
    const delay = (opts && opts.delayMs) || 0;
    let packed;
    try {
      packed = await new Promise((resolve, reject) => {
        setTimeout(() => {
          if (opts && opts.abort) {
            const e = new Error("aborted");
            e.name = "AbortError";
            reject(e);
            return;
          }
          if (opts && opts.fail) reject(new Error(opts.fail));
          else resolve({
            symbol: API_PACKED.symbol,
            timeframe: API_PACKED.timeframe,
            cache: API_PACKED.cache,
            candles: API_PACKED.candles,
          });
        }, delay);
      });
    } catch (err) {
      if (err && err.name === "AbortError") {
        discardPaneLoadResult(p, paneId, owner);
        return;
      }
      throw err;
    }
    if (exitSupersededPaneLoad(p, paneId, owner, paneGen, gen, null)) return;
    applyPaneBundle(p, packed, opts || {});
    if (exitSupersededPaneLoad(p, paneId, owner, paneGen, gen, null)) return;
    releasePaneLoadOwner(p, owner);
  }

  loadPaneFn = loadPane;

  return {
    state, pane, status, loadPane, classify,
    flush: () => new Promise((r) => setTimeout(r, 15)),
    exitSupersededPaneLoad, registerPaneLoadOwner,
  };
}

async function run(name, fn) {
  const e = makeEnv();
  await fn(e);
  await e.flush();
  await e.flush();
  const end = e.classify(e.status.text, e.pane._paneLoadOwner, e.pane._paneLoadInFlight);
  return { name, end, status: e.status.text };
}

(async function () {
  const r = [];

  r.push(await run("A_single", (e) => e.loadPane("p1", { delayMs: 5 })));
  assert.equal(r[r.length - 1].end, "LOADED");
  assert.ok(r[r.length - 1].status.includes("24192"));

  r.push(await run("L_orphan_owner_midflight", async (e) => {
    const p = e.loadPane("p1", { delayMs: 80 });
    await new Promise((x) => setTimeout(x, 20));
    const o = e.pane._paneLoadOwner;
    e.pane._paneLoadOwner = null;
    if (e.pane._paneLoadInFlight === o) e.pane._paneLoadInFlight = null;
    await p;
  }));
  assert.notEqual(r[r.length - 1].end, "LOADING_WITHOUT_ACTIVE_REQUEST", "scenario L");

  r.push(await run("C_parallel_newer_wins", async (e) => {
    const old = e.loadPane("p1", { delayMs: 200 });
    await e.loadPane("p1", { delayMs: 5 });
    await old;
  }));
  assert.equal(r[r.length - 1].end, "LOADED");

  r.push(await run("E_abort_followup", async (e) => {
    await e.loadPane("p1", { delayMs: 5, abort: true });
    await e.flush();
    await e.loadPane("p1", { delayMs: 5 });
  }));
  assert.equal(r[r.length - 1].end, "LOADED");

  r.push(await run("F_recovery_limit", async (e) => {
    e.pane._paneLoadRecoveryCount = PANE_LOAD_RECOVERY_MAX;
    e.status.text = LOADING;
    const paneGen = ++e.pane.paneGen;
    const owner = { paneId: "p1", paneGen, loadGen: 0, seq: 1 };
    e.exitSupersededPaneLoad(e.pane, "p1", owner, paneGen, 0, null);
    await e.flush();
  }));
  assert.equal(r[r.length - 1].status, "error · load recovery limit");

  console.log("pane_load_race_v3_node_ok", r.map((x) => x.name + "=" + x.end).join(" "));
})();
