/**
 * Pane POST sendJson settlement diagnostics (V7) — static contract + simulated phases.
 */
const assert = require("assert");
const fs = require("fs");
const path = require("path");

const jsPath = path.join(__dirname, "..", "static", "js", "research", "research_charts.js");
const js = fs.readFileSync(jsPath, "utf8");
const fixture = JSON.parse(
  fs.readFileSync(
    path.join(__dirname, "..", "static", "js", "research", "tests", "fixtures", "pane_normal_zones.json"),
    "utf8"
  )
);

assert.ok(js.includes("phase6xt-pane-sendjson-diag-v7"), "ASSET_V v7 diag");
assert.ok(js.includes("function paneSendJsonDiagEnabled"), "diag gate");
assert.ok(js.includes("SEND_START"), "SEND_START phase");
assert.ok(js.includes("FETCH_RESOLVED"), "FETCH_RESOLVED");
assert.ok(js.includes("RESPONSE_BODY_READ_START"), "body read start");
assert.ok(js.includes("RESPONSE_BODY_READ_DONE"), "body read done");
assert.ok(js.includes("JSON_PARSE_DONE"), "JSON_PARSE_DONE");
assert.ok(js.includes("SENDJSON_RETURN"), "SENDJSON_RETURN");
assert.ok(js.includes("SENDJSON_ABORT"), "SENDJSON_ABORT");
assert.ok(js.includes("parsePanePostResponseForDiag"), "split body read + parse when diag on");
assert.ok(js.includes("body = await res.json().catch"), "default path still res.json when diag off");

const sendJsonBlock = js.slice(js.indexOf("async function sendJson"), js.indexOf("function makeReadyGate"));
assert.ok(!sendJsonBlock.includes("fetch ="), "no global fetch monkeypatch");
assert.ok(sendJsonBlock.includes("const paneDiag = isPanePostSendJson"), "pane-only diag gate");

function makeEmitter() {
  const rows = [];
  return {
    rows,
    emit(reqId, phase, ctx, extra) {
      rows.push(Object.assign({ phase, reqId, elapsedMs: Date.now() - ctx._t0 }, extra || {}));
    },
  };
}

async function simulateDiagSettlement(fetchImpl) {
  const diag = makeEmitter();
  const ctx = { _t0: Date.now(), signal: null };
  const reqId = "psd-test";
  diag.emit(reqId, "SEND_START", ctx, {});
  let res;
  try {
    res = await fetchImpl();
  } catch (fetchErr) {
    const aborted = fetchErr && fetchErr.name === "AbortError";
    diag.emit(reqId, aborted ? "SENDJSON_ABORT" : "SENDJSON_ERROR", ctx, {
      errorName: fetchErr && fetchErr.name,
    });
    throw fetchErr;
  }
  diag.emit(reqId, "FETCH_RESOLVED", ctx, { httpStatus: res.status });
  diag.emit(reqId, "RESPONSE_BODY_READ_START", ctx, { httpStatus: res.status });
  const text = await res.text();
  diag.emit(reqId, "RESPONSE_BODY_READ_DONE", ctx, { httpStatus: res.status, bodyBytes: text.length });
  let body;
  let jsonParseError = false;
  try {
    body = text ? JSON.parse(text) : {};
  } catch (e) {
    jsonParseError = true;
    body = {};
  }
  diag.emit(reqId, "JSON_PARSE_DONE", ctx, {
    httpStatus: res.status,
    jsonParseError,
    candleCount: Array.isArray(body.candles) ? body.candles.length : null,
  });
  if (!res.ok) {
    diag.emit(reqId, "SENDJSON_ERROR", ctx, { httpStatus: res.status, httpError: true });
    throw new Error("http " + res.status);
  }
  diag.emit(reqId, "SENDJSON_RETURN", ctx, {
    httpStatus: res.status,
    candleCount: Array.isArray(body.candles) ? body.candles.length : null,
  });
  return { body, rows: diag.rows };
}

function mockResponse(status, text, opts) {
  return {
    status,
    ok: status >= 200 && status < 300,
    text: opts && opts.slowMs
      ? function () {
          return new Promise(function (resolve) {
            setTimeout(function () { resolve(text); }, opts.slowMs);
          });
        }
      : function () { return Promise.resolve(text); },
  };
}

async function testSuccess() {
  const payload = JSON.stringify(fixture);
  const { body, rows } = await simulateDiagSettlement(function () {
    return Promise.resolve(mockResponse(200, payload));
  });
  assert.equal(body.symbol, fixture.symbol);
  assert.ok(rows.some((r) => r.phase === "SENDJSON_RETURN"));
  assert.equal(rows[rows.length - 1].phase, "SENDJSON_RETURN");
  assert.ok(rows.find((r) => r.phase === "JSON_PARSE_DONE").candleCount > 0);
}

async function testSlowBody() {
  const payload = JSON.stringify({ candles: [{ time: 1, open: 1, high: 1, low: 1, close: 1 }] });
  const t0 = Date.now();
  const { rows } = await simulateDiagSettlement(function () {
    return Promise.resolve(mockResponse(200, payload, { slowMs: 40 }));
  });
  const readDone = rows.find((r) => r.phase === "RESPONSE_BODY_READ_DONE");
  assert.ok(readDone.elapsedMs >= 35, "slow body read reflected in elapsedMs");
  assert.ok(Date.now() - t0 >= 35);
}

async function testBadJson() {
  const { body, rows } = await simulateDiagSettlement(function () {
    return Promise.resolve(mockResponse(200, "{not-json"));
  });
  assert.deepEqual(body, {});
  assert.equal(rows.find((r) => r.phase === "JSON_PARSE_DONE").jsonParseError, true);
  assert.equal(rows[rows.length - 1].phase, "SENDJSON_RETURN");
}

async function testHttpError() {
  let err;
  try {
    await simulateDiagSettlement(function () {
      return Promise.resolve(mockResponse(500, '{"detail":"fail"}'));
    });
  } catch (e) {
    err = e;
  }
  assert.ok(err);
}

async function testAbort() {
  const ac = new AbortController();
  ac.abort();
  let err;
  try {
    await simulateDiagSettlement(function () {
      const e = new Error("aborted");
      e.name = "AbortError";
      return Promise.reject(e);
    });
  } catch (e) {
    err = e;
  }
  assert.equal(err.name, "AbortError");
}

async function testCoalescedJoin() {
  const inflight = simulateDiagSettlement(function () {
    return new Promise(function (resolve) {
      setTimeout(function () {
        resolve(mockResponse(200, JSON.stringify(fixture)));
      }, 25);
    });
  });
  const joiner = inflight.then((r) => r);
  const [a, b] = await Promise.all([inflight, joiner]);
  assert.equal(a.body.symbol, b.body.symbol);
}

(async function main() {
  await testSuccess();
  await testSlowBody();
  await testBadJson();
  await testHttpError();
  await testAbort();
  await testCoalescedJoin();
  console.log("test_sendjson_pane_diag_node.js: ok");
})().catch(function (err) {
  console.error(err);
  process.exit(1);
});
