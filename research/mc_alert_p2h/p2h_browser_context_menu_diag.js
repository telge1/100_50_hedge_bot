/**
 * MC-ALERT-P2H — paste once in DevTools console on:
 * http://dash.immotel.de:8080/live-charts/research?view=multi
 * Then right-click inside any chart pane. Copy printed JSON back (no tokens).
 */
(function () {
  const out = {
    ts: new Date().toISOString(),
    page: location.href,
    isSecureContext: window.isSecureContext,
    clipboardApi: !!(navigator.clipboard && navigator.clipboard.writeText),
    hostScripts: Array.from(document.scripts).map(function (s) {
      return s.src || "(inline)";
    }),
    panes: [],
    errors: [],
  };

  window.addEventListener(
    "error",
    function (e) {
      out.errors.push({
        msg: e.message,
        src: e.filename,
        line: e.lineno,
      });
    },
    true
  );

  const frames = document.querySelectorAll("iframe.trp-pane-frame");
  frames.forEach(function (frame, idx) {
    const row = {
      index: idx,
      src: frame.src || "",
      title: frame.title || "",
    };
    try {
      const doc = frame.contentDocument;
      const win = frame.contentWindow;
      if (!doc || !win) {
        row.blocked = "no_contentDocument";
        out.panes.push(row);
        return;
      }
      const chartScript = Array.from(doc.scripts).find(function (s) {
        return s.src && s.src.indexOf("chart.js") >= 0;
      });
      row.chartJs = chartScript ? chartScript.src : null;
      row.inIframe = !!win.frameElement;
      row.hasBridge = !!win.bridge;
      row.hasOnPriceAlertCreate = !!(win.bridge && win.bridge.on_price_alert_create);
      row.menuEl = !!doc.getElementById("chart-ctx-menu");
      row.menuHidden = doc.getElementById("chart-ctx-menu")
        ? doc.getElementById("chart-ctx-menu").hidden
        : null;
      row.hasContextMenuFn =
        typeof win.onChartContextMenu === "function" ||
        (doc.body && doc.body.innerHTML.indexOf("onChartContextMenu") >= 0);
      const chartEl = doc.getElementById("chart");
      row.hasChartEl = !!chartEl;
      if (!frame._p2hCtxHook) {
        frame._p2hCtxHook = true;
        doc.addEventListener(
          "contextmenu",
          function (ev) {
            row.lastContextMenu = {
              t: new Date().toISOString(),
              clientX: ev.clientX,
              clientY: ev.clientY,
              target: (ev.target && ev.target.tagName) || "",
            };
            setTimeout(function () {
              const m = doc.getElementById("chart-ctx-menu");
              if (m) {
                row.menuAfterCtx = {
                  hidden: m.hidden,
                  left: m.style.left,
                  top: m.style.top,
                  rect: m.getBoundingClientRect
                    ? JSON.stringify(m.getBoundingClientRect())
                    : null,
                  buttons: m.querySelectorAll("button").length,
                };
              } else {
                row.menuAfterCtx = { missing: true };
              }
              console.log("[P2H diag] pane", idx, JSON.stringify(row, null, 2));
            }, 50);
          },
          true
        );
      }
    } catch (e) {
      row.blocked = String(e);
    }
    out.panes.push(row);
  });

  window.__MC_P2H_CTX_DIAG__ = out;
  console.log("[P2H] Installed. Right-click a chart, then run:");
  console.log("copy(JSON.stringify(window.__MC_P2H_CTX_DIAG__, null, 2))");
  console.log("[P2H] initial snapshot:", JSON.stringify(out, null, 2));
})();