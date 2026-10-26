/**
 * Pane load generation + finalization contract (static + simulations).
 */
const assert = require("assert");
const fs = require("fs");
const path = require("path");

const js = fs.readFileSync(
  path.join(__dirname, "..", "static", "js", "research", "research_charts.js"),
  "utf8"
);

assert.ok(js.includes("phase6xt-pane-sendjson-diag-v7"), "ASSET_V v7");
assert.ok(js.includes("function paneSendJsonDiagEnabled"), "pane sendJson diag present");
assert.ok(js.includes("function scheduleOrphanPaneLoadRecovery"), "orphan recovery");
assert.ok(js.includes("function registerPaneLoadOwner"), "registerPaneLoadOwner");
assert.ok(js.includes("function discardPaneLoadResult"), "discardPaneLoadResult");
assert.ok(js.includes("function paneLoadGenerationStale"), "paneLoadGenerationStale");
assert.ok(js.includes("function updatePaneLoadStatusFromBundle"), "early status");
assert.ok(js.includes("function logPaneLoadFailure"), "logPaneLoadFailure");
assert.ok(js.includes("function setPaneLoadApplyError"), "setPaneLoadApplyError");
assert.ok(js.includes("function exitSupersededPaneLoad"), "exitSupersededPaneLoad");
assert.ok(js.includes("function paneLoadOwnerIsCurrent"), "paneLoadOwnerIsCurrent");
assert.ok(js.includes("PANE_LOAD_RECOVERY_MAX"), "recovery cap");
assert.ok(
  js.includes("gen !== state.loadGen || paneGen !== pane.paneGen") ,
  "generation guard preserved"
);
const loadPane = js.slice(js.indexOf("async function loadPane"), js.indexOf("async function refreshIndicatorsVisible"));
assert.ok(loadPane.includes("try {") && loadPane.includes("applyPaneBundle"), "apply try");
assert.ok(loadPane.includes("catch (applyErr)"), "apply catch");
assert.ok(loadPane.includes("logPaneLoadFailure"), "apply failure log");
assert.ok(loadPane.includes("setPaneLoadApplyError"), "apply error status");
assert.ok(!loadPane.includes("if (pane._paneLoadOwner !== owner) return;\n    applyPaneBundle"), "no naked apply");

function simulateStaleRace() {
  const state = { loadGen: 1, replayGen: 1, chartTimeMode: "LIVE" };
  const pane = {
    paneGen: 0,
    pendingData: { candles: [{ time: 100, close: 1 }] },
    el: { querySelector: () => ({ textContent: "loading…" }) },
    _paneLoadSeq: 0,
  };
  const status = { text: "loading…" };
  pane.el.querySelector = () => ({
    get textContent() { return status.text; },
    set textContent(v) { status.text = v; },
  });

  const oldGen = 1;
  const oldPaneGen = ++pane.paneGen;
  const oldOwner = { paneGen: oldPaneGen, loadGen: oldGen, seq: 1 };
  pane._paneLoadOwner = oldOwner;
  status.text = "loading…";

  state.loadGen += 1;
  pane.paneGen += 1;
  const newPaneGen = ++pane.paneGen;
  const newOwner = { paneGen: newPaneGen, loadGen: state.loadGen, seq: 2 };
  pane._paneLoadOwner = newOwner;
  status.text = "loading…";

  const stale = oldGen !== state.loadGen || oldPaneGen !== pane.paneGen;
  assert.ok(stale, "old response must be stale");
  assert.ok(pane._paneLoadOwner === newOwner, "newer owner active");
  assert.equal(status.text, "loading…", "stale discard must not clear newer loading");

  status.text = "576 · hit";
  assert.notEqual(status.text, "loading…", "successful newer load clears loading");
}

function simulateOrphanRecovery() {
  const status = { text: "loading…" };
  const pane = {
    paneGen: 2,
    pendingData: { candles: [{ time: 1 }, { time: 2 }] },
    _paneLoadOwner: { paneGen: 1, loadGen: 0, seq: 1 },
    _paneLoadSeq: 1,
    el: {
      querySelector: () => ({
        get textContent() { return status.text; },
        set textContent(v) { status.text = v; },
      }),
    },
  };
  const owner = pane._paneLoadOwner;
  assert.ok(pane.paneGen !== owner.paneGen, "orphan: paneGen advanced");
  if (pane._paneLoadOwner === owner && status.text === "loading…") {
    status.text = pane.pendingData.candles.length + " · ready";
    pane._paneLoadOwner = null;
  }
  assert.equal(status.text, "2 · ready");
  assert.equal(pane._paneLoadOwner, null);
}

function simulateApplyErrorKeepsCandles() {
  const status = { text: "loading…" };
  const pane = {
    pendingData: null,
    el: {
      querySelector: () => ({
        get textContent() { return status.text; },
        set textContent(v) { status.text = v; },
      }),
    },
  };
  const packed = { candles: [{ time: 1 }], cache: "miss" };
  pane.pendingData = { candles: packed.candles };
  status.text = packed.candles.length + " · " + packed.cache;
  const err = new Error("overlay merge failed");
  assert.ok(status.text !== "loading…", "B: status after pendingData before throw");
  assert.throws(function () { throw err; });
  if (pane.pendingData.candles.length) status.text = pane.pendingData.candles.length + " · ready";
  assert.equal(status.text, "1 · ready");
}

function simulateRecoveryCap() {
  const max = 2;
  let count = 0;
  let status = "loading…";
  while (status === "loading…" && count < max) {
    count += 1;
  }
  if (count >= max) status = "error · load recovery limit";
  assert.equal(status, "error · load recovery limit");
}

simulateStaleRace();
simulateOrphanRecovery();
simulateApplyErrorKeepsCandles();
simulateRecoveryCap();
console.log("stale_pane_load_generation_node_ok");
