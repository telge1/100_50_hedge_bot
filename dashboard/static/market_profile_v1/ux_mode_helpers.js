/**
 * Pure Market-Profile UX helpers (Node + browser).
 * Standard/Pro migration, data-quality derivation, legend copy.
 * No network, no ClickHouse, no DOM mutation except optional builders.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  if (root) {
    root.MpUxModeHelpers = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  var SETTINGS_SCHEMA = 2;
  var STORAGE_KEY = "mp_v1_settings";

  /** Layer keys that Standard may suppress (heavy / detail). */
  var HEAVY_LAYER_KEYS = [
    "showVolumeLine",
    "splitBuySell",
    "showHvn",
    "showLvn",
    "showSinglePrints",
    "showNakedPoc",
    "showShape",
    "showOi",
    "showFootprint",
    "final",
    "fpShowAvrPanel",
    "obpEnabled",
    "oblEnabled"
  ];

  /** Core visual defaults for brand-new Standard users. */
  var STANDARD_CORE = {
    showHistogram: true,
    showVolumeLine: false,
    splitBuySell: false,
    showPoc: true,
    showValueArea: true,
    showHvn: false,
    showLvn: false,
    showSinglePrints: false,
    showNakedPoc: false,
    extendLevels: true,
    showShape: false,
    showLiquidity: true,
    showOi: false,
    showFootprint: false,
    final: false,
    fpShowAvrPanel: false,
    obpEnabled: false,
    oblEnabled: false
  };

  function isObject(v) {
    return v !== null && typeof v === "object" && !Array.isArray(v);
  }

  function clone(obj) {
    return JSON.parse(JSON.stringify(obj || {}));
  }

  /**
   * Safe migration of persisted settings.
   * @param {string|null} rawString localStorage blob
   * @returns {{ok:boolean, isNewUser:boolean, migratedFromLegacy:boolean, settings:object, reason?:string}}
   */
  function migratePersistedSettings(rawString) {
    if (rawString == null || rawString === "") {
      return {
        ok: true,
        isNewUser: true,
        migratedFromLegacy: false,
        settings: Object.assign(
          {
            ui_mode: "standard",
            settings_schema: SETTINGS_SCHEMA,
            pro_snapshot: null
          },
          STANDARD_CORE
        )
      };
    }
    var parsed;
    try {
      parsed = JSON.parse(rawString);
    } catch (err) {
      return {
        ok: true,
        isNewUser: true,
        migratedFromLegacy: false,
        reason: "corrupt_json",
        settings: Object.assign(
          {
            ui_mode: "standard",
            settings_schema: SETTINGS_SCHEMA,
            pro_snapshot: null
          },
          STANDARD_CORE
        )
      };
    }
    if (!isObject(parsed)) {
      return {
        ok: true,
        isNewUser: true,
        migratedFromLegacy: false,
        reason: "not_object",
        settings: Object.assign(
          {
            ui_mode: "standard",
            settings_schema: SETTINGS_SCHEMA,
            pro_snapshot: null
          },
          STANDARD_CORE
        )
      };
    }

    var out = clone(parsed);
    var migratedFromLegacy = false;
    if (out.ui_mode !== "standard" && out.ui_mode !== "pro") {
      // Existing user without ui_mode: preserve visual state as Pro.
      out.ui_mode = "pro";
      migratedFromLegacy = true;
    }
    if (out.settings_schema == null) out.settings_schema = SETTINGS_SCHEMA;
    if (out.pro_snapshot === undefined) out.pro_snapshot = null;
    // Unknown legacy keys remain on `out` harmlessly.
    return {
      ok: true,
      isNewUser: false,
      migratedFromLegacy: migratedFromLegacy,
      settings: out
    };
  }

  function captureProSnapshot(layerState) {
    var snap = {};
    HEAVY_LAYER_KEYS.forEach(function (k) {
      if (Object.prototype.hasOwnProperty.call(layerState, k)) {
        snap[k] = !!layerState[k];
      }
    });
    // Also preserve core toggles so restore is complete.
    [
      "showHistogram",
      "showPoc",
      "showValueArea",
      "showLiquidity",
      "extendLevels"
    ].forEach(function (k) {
      if (Object.prototype.hasOwnProperty.call(layerState, k)) {
        snap[k] = !!layerState[k];
      }
    });
    return snap;
  }

  function applyStandardMask(layerState) {
    return Object.assign({}, layerState || {}, STANDARD_CORE);
  }

  function restoreProSnapshot(layerState, snapshot) {
    if (!isObject(snapshot)) return Object.assign({}, layerState || {});
    return Object.assign({}, layerState || {}, snapshot);
  }

  /**
   * Mode transition plan — pure; caller applies DOM / network effects.
   */
  function planModeSwitch(fromMode, toMode, currentLayers, existingSnapshot) {
    fromMode = fromMode === "standard" ? "standard" : "pro";
    toMode = toMode === "standard" ? "standard" : "pro";
    if (fromMode === toMode) {
      return {
        changed: false,
        ui_mode: toMode,
        layers: currentLayers,
        pro_snapshot: existingSnapshot || null,
        bumpGeneration: false,
        enableFetches: [],
        disableLayers: []
      };
    }
    if (toMode === "standard") {
      var snap = captureProSnapshot(currentLayers);
      var masked = applyStandardMask(currentLayers);
      var disableLayers = HEAVY_LAYER_KEYS.filter(function (k) {
        return !!currentLayers[k] && !masked[k];
      });
      var enableFetches = [];
      if (masked.showLiquidity && !currentLayers.showLiquidity) {
        enableFetches.push("lld");
      }
      return {
        changed: true,
        ui_mode: "standard",
        layers: masked,
        pro_snapshot: snap,
        bumpGeneration: true,
        enableFetches: enableFetches,
        disableLayers: disableLayers
      };
    }
    // → pro
    var restored = restoreProSnapshot(currentLayers, existingSnapshot);
    var enableFetchesPro = [];
    if (restored.showLiquidity && !currentLayers.showLiquidity) enableFetchesPro.push("lld");
    if (restored.showOi && !currentLayers.showOi) enableFetchesPro.push("oi");
    if (restored.showFootprint && !currentLayers.showFootprint) enableFetchesPro.push("footprint");
    if (restored.obpEnabled && !currentLayers.obpEnabled) enableFetchesPro.push("obp");
    if (restored.oblEnabled && !currentLayers.oblEnabled) enableFetchesPro.push("obl");
    return {
      changed: true,
      ui_mode: "pro",
      layers: restored,
      pro_snapshot: existingSnapshot || null,
      bumpGeneration: false,
      enableFetches: enableFetchesPro,
      disableLayers: []
    };
  }

  function standardHeavyRequestCount(layers) {
    var n = 0;
    if (layers.showOi) n += 1;
    if (layers.showFootprint) n += 1;
    if (layers.obpEnabled) n += 1;
    if (layers.oblEnabled) n += 1;
    return n;
  }

  /**
   * Honest data-quality summary from already-loaded metadata only.
   * Missing evidence → Unbekannt (never invent Gesund/Vollständig).
   */
  function deriveDataQuality(sources) {
    sources = sources || {};
    var details = [];
    var rank = { unknown: 0, unavailable: 1, delayed: 2, partial: 3, complete: 4 };
    var worst = null;

    function note(status, label, detail) {
      details.push({ status: status, label: label, detail: detail || "" });
      if (worst == null || rank[status] < rank[worst]) worst = status;
    }

    var meta = sources.profileMeta;
    if (!meta || typeof meta !== "object") {
      note("unknown", "Market Profile", "Keine Metadaten geladen");
    } else {
      var built = Number(meta.profiles_built);
      var windows = Number(meta.windows);
      var skipped = Array.isArray(meta.skipped_windows) ? meta.skipped_windows.length : null;
      if (!isFinite(built) || !isFinite(windows)) {
        note("unknown", "Market Profile", "Fensterzahlen unbekannt");
      } else if (windows <= 0) {
        note("unavailable", "Market Profile", "Keine Fenster");
      } else if (built < windows || (skipped != null && skipped > 0)) {
        note(
          "partial",
          "Market Profile",
          built + "/" + windows + " Fenster" + (skipped ? ", " + skipped + " ohne Daten" : "")
        );
      } else {
        note("complete", "Market Profile", built + "/" + windows + " Fenster");
      }
      if (meta.error || meta.error_code) {
        note("unavailable", "Market Profile Fehler", String(meta.error || meta.error_code));
      }
    }

    if (sources.cached === true) details.push({ status: "complete", label: "Cache", detail: "Treffer" });
    else if (sources.cached === false) details.push({ status: "complete", label: "Cache", detail: "Frischer Abruf" });
    else details.push({ status: "unknown", label: "Cache", detail: "Unbekannt" });

    if (sources.liveLagSec == null || !isFinite(Number(sources.liveLagSec))) {
      details.push({ status: "unknown", label: "Live-Lag", detail: "Unbekannt" });
    } else {
      var lag = Number(sources.liveLagSec);
      if (lag > 120) note("delayed", "Live-Lag", Math.round(lag) + "s");
      else details.push({ status: "complete", label: "Live-Lag", detail: Math.round(lag) + "s" });
    }

    var developing = sources.developing;
    if (developing === true) details.push({ status: "partial", label: "Profil", detail: "DEVELOPING" });
    else if (developing === false) details.push({ status: "complete", label: "Profil", detail: "CLOSED" });
    else details.push({ status: "unknown", label: "Profil", detail: "CLOSED/DEVELOPING unbekannt" });

    if (sources.obArchive === "no_ob200_archive") {
      note("unavailable", "Orderbook-Archiv", "no_ob200_archive");
    } else if (sources.obArchive === true) {
      details.push({ status: "complete", label: "Orderbook-Archiv", detail: "Verfügbar" });
    } else if (sources.obArchive === false) {
      note("unavailable", "Orderbook-Archiv", "Nicht verfügbar");
    } else {
      details.push({ status: "unknown", label: "Orderbook-Archiv", detail: "Unbekannt" });
    }

    if (sources.lldAvailable === true) details.push({ status: "complete", label: "LLD", detail: "Geladen" });
    else if (sources.lldAvailable === false) details.push({ status: "unavailable", label: "LLD", detail: "Nicht verfügbar" });
    else details.push({ status: "unknown", label: "LLD", detail: "Unbekannt" });

    if (sources.oiAvailable === true) details.push({ status: "complete", label: "Open Interest", detail: "Geladen" });
    else if (sources.oiAvailable === false) details.push({ status: "unavailable", label: "OI", detail: "Nicht verfügbar" });
    else details.push({ status: "unknown", label: "Open Interest", detail: "Unbekannt" });

    if (sources.footprintCoverage == null || sources.footprintCoverage === "") {
      details.push({ status: "unknown", label: "Footprint", detail: "Unbekannt" });
    } else {
      var cov = String(sources.footprintCoverage).toUpperCase();
      if (cov === "FULL" || cov === "COMPLETE" || cov === "OK") {
        details.push({ status: "complete", label: "Footprint", detail: cov });
      } else if (cov === "PARTIAL" || cov === "GAP") {
        note("partial", "Footprint", cov);
      } else if (cov === "NONE" || cov === "MISSING") {
        note("unavailable", "Footprint", cov);
      } else {
        details.push({ status: "unknown", label: "Footprint", detail: cov });
      }
    }

    if (sources.errorMessage) {
      note("unavailable", "Fehler", String(sources.errorMessage).slice(0, 120));
    }

    var status = worst || "unknown";
    var labels = {
      complete: "Vollständig",
      partial: "Teilweise",
      delayed: "Verzögert",
      unavailable: "Nicht verfügbar",
      unknown: "Unbekannt"
    };
    // Never claim overall complete unless every critical channel proved complete
    // and nothing worse was noted. If any unknown remains among profile/cache, keep honest.
    var hasUnknownCritical = details.some(function (d) {
      return (
        d.status === "unknown" &&
        (d.label === "Market Profile" || d.label.indexOf("Fehler") >= 0)
      );
    });
    if (status === "complete" && hasUnknownCritical) status = "unknown";

    return {
      status: status,
      label: labels[status] || "Unbekannt",
      details: details
    };
  }

  function legendSections(colors) {
    colors = colors || {};
    return [
      {
        id: "avr",
        title: "Footprint / AVR",
        items: [
          {
            id: "b_ctrl",
            abbr: "B CTRL",
            title: "Buy Control",
            color: colors.buyControl || "rgba(38, 166, 154, 0.92)",
            text: "Käufer dominieren den Orderflow in diesem Abschnitt."
          },
          {
            id: "s_ctrl",
            abbr: "S CTRL",
            title: "Sell Control",
            color: colors.sellControl || "rgba(239, 83, 80, 0.92)",
            text: "Verkäufer dominieren den Orderflow in diesem Abschnitt."
          },
          {
            id: "b_abs",
            abbr: "B ABS",
            title: "Buy Absorption",
            color: colors.buyAbs || "rgba(45, 212, 191, 0.92)",
            text: "Kaufaggressoren treffen auf aufnehmende Verkaufslimits (Kandidat)."
          },
          {
            id: "s_abs",
            abbr: "S ABS",
            title: "Sell Absorption",
            color: colors.sellAbs || "rgba(245, 158, 11, 0.92)",
            text: "Verkaufsaggressoren treffen auf aufnehmende Kauflimits (Kandidat)."
          },
          {
            id: "vac_up",
            abbr: "VAC UP",
            title: "Aufwärtsvakuum",
            color: colors.vacUp || "rgba(56, 189, 248, 0.75)",
            text: "Dünne Gegenseite nach oben (Proxy bei unvollständiger Evidenz)."
          },
          {
            id: "vac_down",
            abbr: "VAC DOWN",
            title: "Abwärtsvakuum",
            color: colors.vacDown || "rgba(167, 139, 250, 0.75)",
            text: "Dünne Gegenseite nach unten (Proxy bei unvollständiger Evidenz)."
          },
          {
            id: "proxy",
            abbr: "Proxy",
            title: "Proxy",
            color: colors.proxy || "rgba(100, 110, 125, 0.7)",
            text: "Angenäherte Klassifikation — direkte Evidenz unvollständig. Kein Handelssignal."
          }
        ]
      },
      {
        id: "mp",
        title: "Market Profile",
        items: [
          {
            id: "poc",
            abbr: "POC",
            title: "Point of Control",
            color: colors.poc || "#ef4444",
            text: "Preis mit dem höchsten Volumen im Profilfenster."
          },
          {
            id: "vah",
            abbr: "VAH",
            title: "Value Area High",
            color: colors.valueArea || "#3b82f6",
            text: "Oberes Ende der Value Area."
          },
          {
            id: "val",
            abbr: "VAL",
            title: "Value Area Low",
            color: colors.valueArea || "#3b82f6",
            text: "Unteres Ende der Value Area."
          },
          {
            id: "hvn",
            abbr: "HVN",
            title: "High Volume Node",
            color: colors.hvn || "#a855f7",
            text: "Lokales Volumenhoch — oft akzeptierter Preisbereich."
          },
          {
            id: "lvn",
            abbr: "LVN",
            title: "Low Volume Node",
            color: colors.lvn || "#64748b",
            text: "Lokales Volumentief — oft schneller durchlaufen."
          }
        ]
      },
      {
        id: "context",
        title: "Kontext (keine Signale)",
        items: [
          {
            id: "lld",
            abbr: "LLD",
            title: "Liquidity Location",
            color: colors.lld || "#228bab",
            text: "Berechnete Liquiditäts-Zonen aus Kerzenstruktur. Kein Nachweis echter Liquidationen."
          },
          {
            id: "liq",
            abbr: "Liq",
            title: "Echte Liquidation",
            color: colors.liquidation || "#f59e0b",
            text: "Separater Liquidationsdatenpfad (Exchange). Nicht identisch mit LLD."
          },
          {
            id: "bid_wall",
            abbr: "Bid-Wall",
            title: "Bid-Wall",
            color: colors.bidWall || "rgba(38, 166, 154, 0.7)",
            text: "Sichtbare Kauflimit-Liquidität im Orderbuch — kann verschoben oder gelöscht werden."
          },
          {
            id: "ask_wall",
            abbr: "Ask-Wall",
            title: "Ask-Wall",
            color: colors.askWall || "rgba(239, 83, 80, 0.7)",
            text: "Sichtbare Verkaufslimit-Liquidität im Orderbuch — kann verschoben oder gelöscht werden."
          }
        ]
      }
    ];
  }

  return {
    SETTINGS_SCHEMA: SETTINGS_SCHEMA,
    STORAGE_KEY: STORAGE_KEY,
    HEAVY_LAYER_KEYS: HEAVY_LAYER_KEYS,
    STANDARD_CORE: STANDARD_CORE,
    migratePersistedSettings: migratePersistedSettings,
    captureProSnapshot: captureProSnapshot,
    applyStandardMask: applyStandardMask,
    restoreProSnapshot: restoreProSnapshot,
    planModeSwitch: planModeSwitch,
    standardHeavyRequestCount: standardHeavyRequestCount,
    deriveDataQuality: deriveDataQuality,
    legendSections: legendSections
  };
});
