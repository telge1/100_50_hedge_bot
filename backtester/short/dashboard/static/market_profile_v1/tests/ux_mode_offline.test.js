/**
 * Offline UX mode / migration / data-quality / legend tests.
 * No network, no ClickHouse, no dashboard.app.
 */
"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");
const fs = require("fs");

const ux = require(path.join(__dirname, "..", "ux_mode_helpers.js"));

describe("settings migration", () => {
  it("new user without settings → standard core", () => {
    const m = ux.migratePersistedSettings(null);
    assert.equal(m.isNewUser, true);
    assert.equal(m.settings.ui_mode, "standard");
    assert.equal(m.settings.showLiquidity, true);
    assert.equal(m.settings.showOi, false);
    assert.equal(m.settings.showFootprint, false);
    assert.equal(m.settings.obpEnabled, false);
  });

  it("existing user without ui_mode → pro, preserves toggles", () => {
    const legacy = {
      symbol: "ETHUSDT",
      showLiquidity: false,
      showOi: true,
      showFootprint: true,
      showHvn: true,
      days: "14"
    };
    const m = ux.migratePersistedSettings(JSON.stringify(legacy));
    assert.equal(m.isNewUser, false);
    assert.equal(m.migratedFromLegacy, true);
    assert.equal(m.settings.ui_mode, "pro");
    assert.equal(m.settings.showLiquidity, false);
    assert.equal(m.settings.showOi, true);
    assert.equal(m.settings.showFootprint, true);
    assert.equal(m.settings.showHvn, true);
    assert.equal(m.settings.days, "14");
  });

  it("existing ui_mode=standard preserved", () => {
    const m = ux.migratePersistedSettings(
      JSON.stringify({ ui_mode: "standard", showLiquidity: true, days: "7" })
    );
    assert.equal(m.migratedFromLegacy, false);
    assert.equal(m.settings.ui_mode, "standard");
    assert.equal(m.settings.days, "7");
  });

  it("existing ui_mode=pro preserved", () => {
    const m = ux.migratePersistedSettings(
      JSON.stringify({ ui_mode: "pro", showOi: true, unknown_legacy_key: 42 })
    );
    assert.equal(m.settings.ui_mode, "pro");
    assert.equal(m.settings.showOi, true);
    assert.equal(m.settings.unknown_legacy_key, 42);
  });

  it("unknown old keys remain harmless", () => {
    const m = ux.migratePersistedSettings(
      JSON.stringify({ ui_mode: "pro", fooBar: "x", nested: { a: 1 } })
    );
    assert.equal(m.settings.fooBar, "x");
    assert.deepEqual(m.settings.nested, { a: 1 });
  });

  it("corrupt settings fall back safely to standard", () => {
    const m = ux.migratePersistedSettings("{not-json");
    assert.equal(m.isNewUser, true);
    assert.equal(m.settings.ui_mode, "standard");
    assert.equal(m.reason, "corrupt_json");
  });

  it("reload retains mode via migrate roundtrip", () => {
    const first = ux.migratePersistedSettings(
      JSON.stringify({ ui_mode: "pro", showOi: true, pro_snapshot: { showOi: true } })
    );
    const again = ux.migratePersistedSettings(JSON.stringify(first.settings));
    assert.equal(again.settings.ui_mode, "pro");
    assert.equal(again.settings.showOi, true);
  });
});

describe("mode switch plan", () => {
  const base = Object.assign({}, ux.STANDARD_CORE, {
    showLiquidity: true,
    showOi: true,
    showFootprint: true,
    obpEnabled: true,
    oblEnabled: false,
    showHvn: true
  });

  it("standard → pro restores snapshot without auto-all-on", () => {
    const snap = { showOi: false, showFootprint: false, obpEnabled: false, oblEnabled: false, showHvn: true, showLiquidity: true };
    const plan = ux.planModeSwitch("standard", "pro", ux.STANDARD_CORE, snap);
    assert.equal(plan.ui_mode, "pro");
    assert.equal(plan.layers.showOi, false);
    assert.equal(plan.layers.showFootprint, false);
    assert.equal(plan.layers.obpEnabled, false);
    assert.equal(plan.layers.showHvn, true);
    assert.ok(!plan.enableFetches.includes("oi"));
    assert.ok(!plan.enableFetches.includes("footprint"));
  });

  it("pro → standard stores snapshot and disables heavy", () => {
    const plan = ux.planModeSwitch("pro", "standard", base, null);
    assert.equal(plan.ui_mode, "standard");
    assert.ok(plan.pro_snapshot);
    assert.equal(plan.pro_snapshot.showOi, true);
    assert.equal(plan.layers.showOi, false);
    assert.equal(plan.layers.showFootprint, false);
    assert.equal(plan.layers.obpEnabled, false);
    assert.equal(plan.layers.showLiquidity, true);
    assert.equal(plan.bumpGeneration, true);
    assert.ok(plan.disableLayers.includes("showOi"));
    assert.ok(plan.disableLayers.includes("showFootprint"));
  });

  it("pro snapshot restore after roundtrip", () => {
    const toStd = ux.planModeSwitch("pro", "standard", base, null);
    const back = ux.planModeSwitch("standard", "pro", toStd.layers, toStd.pro_snapshot);
    assert.equal(back.layers.showOi, true);
    assert.equal(back.layers.showFootprint, true);
    assert.equal(back.layers.obpEnabled, true);
    assert.equal(back.layers.showHvn, true);
  });

  it("standard heavy request count is zero for core", () => {
    assert.equal(ux.standardHeavyRequestCount(ux.STANDARD_CORE), 0);
  });

  it("same mode is no-op", () => {
    const plan = ux.planModeSwitch("pro", "pro", base, null);
    assert.equal(plan.changed, false);
  });
});

describe("data quality honesty", () => {
  it("complete metadata", () => {
    const dq = ux.deriveDataQuality({
      profileMeta: { profiles_built: 10, windows: 10, skipped_windows: [] },
      cached: true,
      liveLagSec: 5,
      developing: false,
      obArchive: true,
      lldAvailable: true,
      oiAvailable: true,
      footprintCoverage: "FULL"
    });
    assert.equal(dq.status, "complete");
    assert.equal(dq.label, "Vollständig");
  });

  it("partial metadata", () => {
    const dq = ux.deriveDataQuality({
      profileMeta: { profiles_built: 7, windows: 10, skipped_windows: [1, 2] }
    });
    assert.equal(dq.status, "partial");
  });

  it("no_ob200_archive", () => {
    const dq = ux.deriveDataQuality({
      profileMeta: { profiles_built: 10, windows: 10, skipped_windows: [] },
      obArchive: "no_ob200_archive"
    });
    assert.equal(dq.status, "unavailable");
    assert.ok(dq.details.some((d) => String(d.detail).includes("no_ob200_archive")));
  });

  it("missing metadata → Unbekannt", () => {
    const dq = ux.deriveDataQuality({});
    assert.equal(dq.status, "unknown");
    assert.equal(dq.label, "Unbekannt");
  });

  it("error status", () => {
    const dq = ux.deriveDataQuality({
      profileMeta: { profiles_built: 10, windows: 10, skipped_windows: [] },
      errorMessage: "boom"
    });
    assert.equal(dq.status, "unavailable");
  });

  it("developing vs closed", () => {
    assert.ok(
      ux.deriveDataQuality({
        profileMeta: { profiles_built: 1, windows: 1, skipped_windows: [] },
        developing: true
      }).details.some((d) => d.detail === "DEVELOPING")
    );
    assert.ok(
      ux.deriveDataQuality({
        profileMeta: { profiles_built: 1, windows: 1, skipped_windows: [] },
        developing: false
      }).details.some((d) => d.detail === "CLOSED")
    );
  });

  it("never invents green without evidence", () => {
    const dq = ux.deriveDataQuality({ profileMeta: null });
    assert.notEqual(dq.status, "complete");
  });
});

describe("legend copy", () => {
  it("contains required terms and LLD ≠ liquidation", () => {
    const sections = ux.legendSections();
    const flat = sections.flatMap((s) => s.items);
    const abbrs = flat.map((i) => i.abbr);
    for (const need of [
      "B CTRL",
      "S CTRL",
      "B ABS",
      "S ABS",
      "VAC UP",
      "VAC DOWN",
      "Proxy",
      "POC",
      "VAH",
      "VAL",
      "HVN",
      "LVN",
      "LLD",
      "Bid-Wall",
      "Ask-Wall",
      "Liq"
    ]) {
      assert.ok(abbrs.includes(need), need);
    }
    const lld = flat.find((i) => i.id === "lld");
    const liq = flat.find((i) => i.id === "liq");
    assert.match(lld.text, /Keine?n? Nachweis|keine echten Liquidationen|Kein Nachweis/i);
    assert.match(liq.text, /Separater|nicht identisch|Nicht identisch/i);
    assert.equal(sections[0].id, "avr");
  });
});

describe("wiring / scope", () => {
  it("app.js bootstraps UX helpers and mode switch", () => {
    const app = fs.readFileSync(path.join(__dirname, "..", "app.js"), "utf8");
    assert.match(app, /MpUxModeHelpers bootstrap/);
    assert.match(app, /applyModeSwitch/);
    assert.match(app, /updateDataQualityBar/);
    assert.match(app, /migratePersistedSettings/);
    assert.match(app, /loadLldWithFallback/);
    assert.match(app, /FORMING_MS/);
    // no new polling interval invented for UX
    assert.doesNotMatch(app, /setInterval\(\s*updateDataQualityBar/);
  });

  it("template has unique required IDs and groups", () => {
    const html = fs.readFileSync(
      path.join(__dirname, "..", "..", "..", "templates", "market_profile_v1.html"),
      "utf8"
    );
    const ids = [...html.matchAll(/\bid=\"([^\"]+)\"/g)].map((m) => m[1]);
    const counts = {};
    ids.forEach((id) => {
      counts[id] = (counts[id] || 0) + 1;
    });
    const dupes = Object.keys(counts).filter((k) => counts[k] > 1);
    assert.deepEqual(dupes, []);
    for (const need of [
      "mpModeStandard",
      "mpModePro",
      "mpDataQuality",
      "mpDqStatus",
      "mpLegendPanel",
      "fpAvrLegend",
      "mpLegend",
      "mpShowLiquidity",
      "mpShowFootprint",
      "mpChart",
      "mpLoad"
    ]) {
      assert.ok(ids.includes(need), need);
    }
    assert.match(html, /data-mp-group=\"markt\"/);
    assert.match(html, /LLD ist|Liquidity Location|keine Handelssignale|Legende/i);
    assert.match(html, /aria-expanded/);
  });

  it("prompt3/4 paths remain in app.js", () => {
    const app = fs.readFileSync(path.join(__dirname, "..", "app.js"), "utf8");
    assert.match(app, /\/api\/research\/liquidity-location/);
    assert.match(app, /shouldFallbackToPane|loadLldWithFallback/);
    assert.match(app, /createInflightDedupe|livePollGen/);
  });
});
