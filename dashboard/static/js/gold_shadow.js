(function () {
  const POLL = Number(window.__GS_POLL_MS || 4000);
  let controller = null;
  let timer = null;
  let tab = "overview";
  const page = { signals: 0, trades: 0, events: 0 };
  const size = 25;

  function $(id) { return document.getElementById(id); }
  function dash(v) { return v === null || v === undefined || v === "" ? "—" : String(v); }
  function badge(kind, text) {
    const cls = String(kind || "UNKNOWN").replace(/[^A-Z0-9_]/g, "");
    const mapped = cls.indexOf("SKIPPED") === 0 ? "SKIPPED" : cls;
    return '<span class="status-badge gs-badge-' + mapped + '">' + dash(text || kind) + "</span>";
  }
  function emptyRow(cols, msg) {
    return '<tr><td colspan="' + cols + '" class="stoch-empty">' + msg + "</td></tr>";
  }

  async function getJson(url) {
    if (controller) controller.abort();
    controller = new AbortController();
    const res = await fetch(url, { signal: controller.signal, credentials: "same-origin" });
    const body = await res.json();
    body._http = res.status;
    return body;
  }

  function card(label, value) {
    return '<div class="profit-summary-card"><div class="summary-label">' + label +
      '</div><div class="summary-value">' + dash(value) + "</div></div>";
  }

  function renderSummary(s) {
    $("gsExchangeWarn").hidden = !s.unexpected_exchange_activity;
    $("gsOffline").hidden = !s.offline;
    $("gsEmpty").hidden = !s.empty_forward;
    const by = s.slots_by_status || {};
    $("gsSummary").innerHTML = [
      card("Modus", s.mode),
      card("DB", s.connected ? "verbunden" : "offline"),
      card("Strategy", s.strategy_id),
      card("Pin", s.frozen_pin),
      card("Universe", s.universe),
      card("Timeframes", (s.timeframes || []).join(" / ")),
      card("Slots", s.slot_count),
      card("FREE", by.FREE || 0),
      card("RESERVED", by.RESERVED || 0),
      card("ENTRY_PENDING", by.ENTRY_PENDING || 0),
      card("OPEN", by.OPEN || 0),
      card("EXIT_PENDING", by.EXIT_PENDING || 0),
      card("Signale", s.signals_total),
      card("ACCEPTED", s.accepted),
      card("Übersprungen", s.skipped),
      card("Open Trades", s.open_trades),
      card("Closed", s.closed_trades),
      card("TP", s.tp),
      card("SL", s.sl),
      card("Net-PnL", s.net_pnl === null ? "—" : s.net_pnl),
      card("Exchange-Orders", s.exchange_orders),
      card("Fills", s.exchange_fills),
      card("Wallet", s.wallet),
    ].join("");
    $("gsSafety").innerHTML =
      "<div class=\"summary-label\">Orders / Fills</div><div class=\"summary-value\">" +
      dash(s.exchange_orders) + " / " + dash(s.exchange_fills) + "</div>" +
      (s.unexpected_exchange_activity ? "<p>UNEXPECTED_EXCHANGE_ACTIVITY_IN_SHADOW</p>" : "<p>Keine Exchange-Aktivität.</p>");
  }

  function fillTable(id, rowsHtml, cols, emptyMsg) {
    const tb = document.querySelector("#" + id + " tbody");
    tb.innerHTML = rowsHtml || emptyRow(cols, emptyMsg);
  }

  async function loadAll() {
    try {
      const summary = await getJson("/api/gold-shadow/summary");
      renderSummary(summary);
      const slots = await getJson("/api/gold-shadow/slots");
      fillTable("gsSlotsTable", (slots.items || []).map(function (r) {
        return "<tr><td>" + dash(r.slot_id) + "</td><td>" + badge(r.status) + "</td><td>" + dash(r.symbol) +
          "</td><td>" + dash(r.direction) + "</td><td>" + dash(r.timeframe) + "</td><td>" + dash(r.signal_id) +
          "</td><td>" + dash(r.trade_id) + "</td><td>" + dash(r.notional) + "</td><td>" + dash(r.version) +
          "</td><td>" + dash(r.updated_at) + "</td><td>" + dash(r.duration_s) + "s</td></tr>";
      }).join(""), 11, "Keine Slots");
      const trades = await getJson("/api/gold-shadow/trades?limit=" + size + "&offset=" + (page.trades * size));
      const open = (trades.items || []).filter(function (t) { return t.status === "OPEN"; });
      fillTable("gsOpenTradesTable", open.map(function (t) {
        return "<tr><td>" + dash(t.trade_id) + "</td><td>" + dash(t.symbol) + "</td><td>" + dash(t.timeframe) +
          "</td><td>" + badge(t.status) + "</td><td>" + dash(t.shadow_entry) + "</td><td>" + badge("TP", t.tp) +
          "</td><td>" + badge("SL", t.sl) + "</td></tr>";
      }).join(""), 7, "Keine offenen Shadow-Trades");
      fillTable("gsTradesTable", (trades.items || []).map(function (t) {
        return "<tr><td>" + dash(t.trade_id) + "</td><td>" + dash(t.signal_id) + "</td><td>" + dash(t.slot_id) +
          "</td><td>" + dash(t.symbol) + "</td><td>" + dash(t.direction) + "</td><td>" + dash(t.timeframe) +
          "</td><td>" + badge(t.status) + "</td><td>" + dash(t.theoretical_entry) + "</td><td>" + dash(t.shadow_entry) +
          "</td><td>" + badge("TP", dash(t.tp)) + "</td><td>" + badge("SL", dash(t.sl)) + "</td><td>" + dash(t.entry_time) +
          "</td><td>" + dash(t.exit_time) + "</td><td>" + badge(t.exit_reason || "UNKNOWN", t.exit_reason) +
          "</td><td>" + dash(t.gross_pnl) + "</td><td>" + dash(t.fees) + "</td><td>" + dash(t.net_pnl) + "</td></tr>";
      }).join(""), 17, "Noch keine Shadow-Trades");
      $("gsTrPage").textContent = "Seite " + (page.trades + 1);
      const q = new URLSearchParams({
        limit: String(size),
        offset: String(page.signals * size),
        symbol: $("gsFSymbol").value.trim(),
        timeframe: $("gsFTf").value,
        direction: $("gsFDir").value,
        decision: $("gsFDec").value,
        reason: $("gsFReason").value.trim(),
      });
      const signals = await getJson("/api/gold-shadow/signals?" + q.toString());
      fillTable("gsSignalsTable", (signals.items || []).map(function (r) {
        const dcls = r.decision && String(r.decision).indexOf("SKIPPED") === 0 ? "SKIPPED" : r.decision;
        return "<tr><td>" + dash(r.signal_id) + "</td><td>" + dash(r.symbol) + "</td><td>" + dash(r.direction) +
          "</td><td>" + dash(r.timeframe) + "</td><td>" + dash(r.confirmation_time) + "</td><td>" + dash(r.entry_time) +
          "</td><td>" + dash(r.theoretical_entry) + "</td><td>" + dash(r.tp_pct) + "</td><td>" + dash(r.sl_pct) +
          "</td><td>" + dash(r.tier_a) + "</td><td>" + dash(r.strategy_version) + "</td><td>" + dash(r.candle_pin) +
          "</td><td>" + dash(r.created_at) + "</td><td>" + badge(dcls, r.decision) + "</td><td>" + dash(r.reason) +
          "</td><td>" + dash(r.slot_id) + "</td><td>" + dash(r.trade_id) + "</td></tr>";
      }).join(""), 17, "Noch keine echten Forward-Signale");
      $("gsSigPage").textContent = "Seite " + (page.signals + 1);
      const dec = await getJson("/api/gold-shadow/decisions");
      const counts = dec.counts || {};
      $("gsDecisionCounts").innerHTML = Object.keys(counts).map(function (k) {
        return card(k, counts[k]);
      }).join("");
      const events = await getJson("/api/gold-shadow/events?limit=" + size + "&offset=" + (page.events * size));
      fillTable("gsEventsTable", (events.items || []).map(function (e) {
        return "<tr><td>" + dash(e.created_at) + "</td><td>" + dash(e.slot_id) + "</td><td>" + badge(e.old_status) +
          "</td><td>" + badge(e.new_status) + "</td><td>" + dash(e.signal_id) + "</td><td>" + dash(e.trade_id) +
          "</td><td>" + dash(e.reason) + "</td></tr>";
      }).join(""), 7, "Keine Slot-Events");
      $("gsEvPage").textContent = "Seite " + (page.events + 1);
    } catch (err) {
      if (err && err.name === "AbortError") return;
      $("gsOffline").hidden = false;
    }
  }

  document.querySelectorAll(".gs-tab").forEach(function (btn) {
    btn.addEventListener("click", function () {
      tab = btn.getAttribute("data-tab");
      document.querySelectorAll(".gs-tab").forEach(function (b) { b.classList.toggle("active", b === btn); });
      document.querySelectorAll(".gs-panel").forEach(function (p) {
        p.hidden = p.getAttribute("data-panel") !== tab;
      });
    });
  });
  $("gsFilterApply").addEventListener("click", function () { page.signals = 0; loadAll(); });
  $("gsSigPrev").addEventListener("click", function () { page.signals = Math.max(0, page.signals - 1); loadAll(); });
  $("gsSigNext").addEventListener("click", function () { page.signals += 1; loadAll(); });
  $("gsTrPrev").addEventListener("click", function () { page.trades = Math.max(0, page.trades - 1); loadAll(); });
  $("gsTrNext").addEventListener("click", function () { page.trades += 1; loadAll(); });
  $("gsEvPrev").addEventListener("click", function () { page.events = Math.max(0, page.events - 1); loadAll(); });
  $("gsEvNext").addEventListener("click", function () { page.events += 1; loadAll(); });

  loadAll();
  timer = setInterval(loadAll, POLL);
})();
