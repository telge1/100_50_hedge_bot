/**
 * UTC time labels for Research Charts (Lightweight Charts crosshair).
 * Exposed as window.ResearchChartTimeUtc for chart.js and tests.
 */
(function (root) {
  "use strict";

  function pad2(n) {
    return String(n).padStart(2, "0");
  }

  /** Crosshair time-axis label: YYYY-MM-DD HH:mm UTC (always UTC, candle open time). */
  function formatCrosshairTimeUtc(unixSec) {
    const n = Number(unixSec);
    if (!Number.isFinite(n)) return "";
    const d = new Date(n * 1000);
    return (
      d.getUTCFullYear() +
      "-" +
      pad2(d.getUTCMonth() + 1) +
      "-" +
      pad2(d.getUTCDate()) +
      " " +
      pad2(d.getUTCHours()) +
      ":" +
      pad2(d.getUTCMinutes()) +
      " UTC"
    );
  }

  root.ResearchChartTimeUtc = {
    formatCrosshairTimeUtc: formatCrosshairTimeUtc,
  };
})(typeof window !== "undefined" ? window : globalThis);
