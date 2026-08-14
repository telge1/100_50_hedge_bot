(function () {
  "use strict";

  const state = {
    rows: [],
    filtered: [],
    summary: null,
    sortBy: "candle_close_time",
    sortDir: "desc",
    page: 0,
    pageSize: 15,
    chart: null,
    refreshMs: 15000,
    refreshTimer: null,
    loading: false,
    symbolOptions: [],
    researchMode: false,
  };

  const $ = (id) => document.getElementById(id);
  state.researchMode =
    ((document.getElementById("stochDashboardSource") || {}).value || "") ===
    "RESEARCH_1M_TIMING";
  if (state.researchMode) {
    state.sortBy = "original_tier_a_signal_ts";
  }

  function fmtNum(n, d) {
    if (n === null || n === undefined || Number.isNaN(Number(n))) return "–";
    return Number(n).toFixed(d);
  }

  function fmtTs(ts) {
    if (window.StochUtc && typeof window.StochUtc.fmtUtc === "function") {
      return window.StochUtc.fmtUtc(ts);
    }
    if (!ts && ts !== 0) return "–";
    if (typeof ts === "string" && ts.includes("-")) {
      const d = new Date(ts.endsWith("Z") || /[+-]\d{2}:\d{2}$/.test(ts) ? ts : ts + "Z");
      if (Number.isNaN(d.getTime())) return String(ts);
      ts = d.getTime();
    }
    const n = Number(ts);
    const ms = n > 1e12 ? n : n * 1000;
    const d = new Date(ms);
    if (Number.isNaN(d.getTime())) return String(ts);
    const p = (x) => String(x).padStart(2, "0");
    return (
      `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ` +
      `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())} UTC`
    );
  }

  function chipDirection(dir) {
    const d = String(dir || "").toUpperCase();
    const cls = d === "LONG" ? "stoch-chip-long" : "stoch-chip-short";
    return `<span class="stoch-chip ${cls}">${d || "–"}</span>`;
  }

  function chipState(s) {
    const v = String(s || "").toUpperCase();
    let cls = "stoch-chip-pending";
    if (v === "SELECTED") cls = "stoch-chip-done";
    if (v === "SIGNAL" || v === "TIER_A") cls = "stoch-chip-live";
    if (v === "CANDIDATE") cls = "stoch-chip-pending";
    return `<span class="stoch-chip ${cls}">${v || "–"}</span>`;
  }

  function chipResult(r, row) {
    const v = String(r || "OPEN").toUpperCase();
    let cls = "stoch-chip-pending";
    if (v === "WIN") cls = "stoch-chip-live";
    else if (v === "LOSS") cls = "stoch-chip-err";
    else if (v === "BE / WIN") cls = "stoch-chip-be-win";
    else if (v === "BE / LOSS") cls = "stoch-chip-be-loss";
    else if (v === "BE / OPEN" || v === "BE") cls = "stoch-chip-warn";
    else if (v === "OPEN") cls = "stoch-chip-pending";
    else if (v === "WAITING_FOR_1M_EXTREME") cls = "stoch-chip-warn";
    else if (v === "WAITING_FOR_1M_TURN") cls = "stoch-chip-warn";
    else if (v === "ENTRY_TRIGGERED") cls = "stoch-chip-live";
    else if (v === "NO_ENTRY_TIMEOUT") cls = "stoch-chip-err";
    let title = "";
    if (v.startsWith("BE /") && row) {
      const cf = row.counterfactual_no_be_result || v.split(" / ")[1] || "–";
      const cfPnl = row.counterfactual_no_be_pnl_pct;
      const cfExit = row.counterfactual_no_be_exit_time || "–";
      const pnlStr =
        cfPnl === null || cfPnl === undefined || Number.isNaN(Number(cfPnl))
          ? "–"
          : `${Number(cfPnl) > 0 ? "+" : ""}${Number(cfPnl).toFixed(2)}%`;
      title = ` title="No-BE Result: ${cf}&#10;No-BE PnL: ${pnlStr}&#10;No-BE Exit: ${cfExit}"`;
    }
    if (row && (row.one_m_trigger_state || row["1m_trigger_state"])) {
      const st = row.one_m_trigger_state || row["1m_trigger_state"];
      const wait = row.wait_minutes;
      const baseTs = row.original_baseline_entry_ts || "–";
      title =
        ` title="1m state: ${st}&#10;wait_min: ${wait ?? "–"}&#10;baseline entry (compare): ${baseTs}"`;
    }
    return `<span class="stoch-chip ${cls}"${title}>${v}</span>`;
  }

  function chipTriggerState(s) {
    const v = String(s || "–").toUpperCase();
    let cls = "stoch-chip-pending";
    if (v === "WAITING_FOR_1M_EXTREME") cls = "stoch-chip-warn";
    else if (v === "WAITING_FOR_1M_TURN") cls = "stoch-chip-warn";
    else if (v === "ENTRY_TRIGGERED") cls = "stoch-chip-live";
    else if (v === "NO_ENTRY_TIMEOUT") cls = "stoch-chip-err";
    return `<span class="stoch-chip ${cls}">${v}</span>`;
  }

  function fmtPrice(n) {
    if (n === null || n === undefined || Number.isNaN(Number(n)) || Number(n) <= 0) return "–";
    return Number(n).toFixed(6);
  }

  function fmtPnl(n) {
    if (n === null || n === undefined || Number.isNaN(Number(n))) return "–";
    const v = Number(n);
    const sign = v > 0 ? "+" : "";
    return `${sign}${v.toFixed(2)}%`;
  }

  function fmtDuration(sec, result) {
    if (sec === null || sec === undefined || Number.isNaN(Number(sec))) return "–";
    let s = Math.max(0, Math.floor(Number(sec)));
    const d = Math.floor(s / 86400);
    s %= 86400;
    const h = Math.floor(s / 3600);
    s %= 3600;
    const m = Math.floor(s / 60);
    const parts = [];
    if (d) parts.push(`${d}d`);
    if (h) parts.push(`${h}h`);
    if (m || !parts.length) parts.push(`${m}m`);
    const base = parts.join(" ");
    const ru = String(result || "").toUpperCase();
    return ru === "OPEN" || ru === "BE / OPEN" ? `OPEN ${base}` : base;
  }


  function chipTf(tf) {
    const v = String(tf || "").trim() || "–";
    return `<span class="stoch-chip stoch-chip-tf">${v}</span>`;
  }

  function chipTierA(tierA) {
    if (tierA) {
      return `<span class="stoch-chip stoch-chip-live" title="Tier-A">TIER-A</span>`;
    }
    return "";
  }

  function chartIcon() {
    return `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3.5 18.49l6-6.01 4 4L22 6.92l-1.41-1.41-7.09 7.97-4-4L2 16.99z"/></svg>`;
  }

  function unique(arr) {
    return Array.from(new Set(arr.filter(Boolean))).sort();
  }

  function fillSelect(sel, values, allLabel) {
    if (!sel) return;
    const cur = sel.value;
    sel.innerHTML =
      `<option value="">${allLabel}</option>` +
      values.map((v) => `<option value="${v}">${v}</option>`).join("");
    if (values.includes(cur)) sel.value = cur;
  }

  function tsMs(v) {
    if (v == null || v === "") return 0;
    if (typeof v === "number") return v > 1e12 ? v : v * 1000;
    const d = new Date(String(v).endsWith("Z") || /[+-]\d{2}:\d{2}$/.test(String(v)) ? v : String(v) + "Z");
    return Number.isNaN(d.getTime()) ? 0 : d.getTime();
  }

  function fmtPctSigned(n) {
    if (n === null || n === undefined || Number.isNaN(Number(n))) return "–";
    const v = Number(n);
    const sign = v > 0 ? "+" : "";
    return `${sign}${v.toFixed(1)}%`;
  }

  function renderSummary(summary) {
    const s = summary || {};
    const set = (id, val) => {
      const el = $(id);
      if (el) el.textContent = val;
    };
    set("perfSignals", s.signals != null ? String(s.signals) : "–");
    set("perfWins", s.wins != null ? String(s.wins) : "–");
    set("perfLosses", s.losses != null ? String(s.losses) : "–");
    set("perfOpen", s.open != null ? String(s.open) : "–");
    set(
      "perfWinRate",
      s.win_rate_pct == null || Number.isNaN(Number(s.win_rate_pct))
        ? "–"
        : `${Number(s.win_rate_pct).toFixed(1)}%`
    );
    set("perfProfit", fmtPctSigned(s.gross_profit_pct));
    set("perfLoss", fmtPctSigned(s.gross_loss_pct));
    const totalEl = $("perfTotal");
    if (totalEl) {
      totalEl.textContent = fmtPctSigned(s.total_pnl_pct);
      totalEl.classList.remove("stoch-perf-win", "stoch-perf-loss");
      const t = Number(s.total_pnl_pct);
      if (!Number.isNaN(t) && t > 0) totalEl.classList.add("stoch-perf-win");
      if (!Number.isNaN(t) && t < 0) totalEl.classList.add("stoch-perf-loss");
    }
    const meta = $("perfMeta");
    if (meta) {
      const sv = s.strategy_version || (($("stochFilterStrategy") || {}).value || "");
      meta.textContent = `PnL basis: gross · Strategy: ${sv} · Stats = alle Treffer der aktiven Filter (nicht nur Seite)`;
    }
  }

  function applyFilters() {
    // Server already applied symbol/TF/direction/hours/strategy. Local sort + page only.
    let rows = state.rows.slice();
    const key = state.sortBy;
    const dir = state.sortDir === "asc" ? 1 : -1;
    rows.sort((a, b) => {
      let av = a[key];
      let bv = b[key];
      if (key === "candle_close_time" || key === "signal_time" || key === "entry_time") {
        av = tsMs(av);
        bv = tsMs(bv);
      } else if (typeof av === "string") {
        av = av.toLowerCase();
        bv = String(bv || "").toLowerCase();
      } else {
        av = av == null ? -Infinity : Number(av);
        bv = bv == null ? -Infinity : Number(bv);
      }
      if (av < bv) return -1 * dir;
      if (av > bv) return 1 * dir;
      return 0;
    });
    state.filtered = rows;
    renderTable();
  }

  async function load(opts) {
    const preservePage = !!(opts && opts.preservePage);
    if (state.loading) return;
    state.loading = true;
    try {
      const qs = new URLSearchParams();
      qs.set("limit", state.researchMode ? "120" : "500");
      const hours = ($("stochFilterHours") || {}).value || "48";
      qs.set("hours", hours);
      if (state.researchMode) {
        qs.set("tier_a", "true");
        const variant =
          ($("stochFilterTimingVariant") || {}).value || "WAIT_1M_EXTREME_TURN_CROSS";
        qs.set("timing_variant", variant);
      } else {
        const tierScope = ($("stochFilterTierA") || {}).value || "true";
        qs.set("tier_a", tierScope);
        const strategy = ($("stochFilterStrategy") || {}).value || "wave_fade_no_be50_v1";
        qs.set("strategy_version", strategy);
        const selected = ($("stochFilterSelected") || {}).value || "";
        if (selected) qs.set("selected", selected);
      }
      const sym = ($("stochFilterSymbol") || {}).value || "";
      if (sym) qs.set("symbol", sym);
      const direction = ($("stochFilterDirection") || {}).value || "";
      if (direction) qs.set("direction", direction);
      const tf = ($("stochFilterTimeframe") || {}).value || "";
      if (tf) qs.set("timeframe", tf);

      const res = await fetch("/api/stoch/signals?" + qs.toString(), { credentials: "include" });
      let data = {};
      try {
        data = await res.json();
      } catch (_) {
        data = {};
      }
      if (!res.ok || data.success === false || data.feed_ready === false) {
        state.rows = [];
        state.filtered = [];
        state.summary = null;
        renderSummary(null);
        setBanner(
          data.message || data.error || `Signal-Feed nicht erreichbar (HTTP ${res.status})`,
          true
        );
        renderTable();
        return;
      }

      const rows = Array.isArray(data.signals)
        ? data.signals
        : Array.isArray(data.items)
          ? data.items
          : [];
      state.rows = rows.filter((r) => r && !r.is_demo && r.signal_id);
      state.summary = data.summary || null;
      renderSummary(state.summary);
      setBanner("");
      if (!sym) {
        state.symbolOptions = unique(state.rows.map((r) => r.symbol));
      } else if (!state.symbolOptions.length) {
        state.symbolOptions = unique(state.rows.map((r) => r.symbol));
      }
      fillSelect($("stochFilterSymbol"), state.symbolOptions, "Alle Symbole");
      if (sym) {
        const sel = $("stochFilterSymbol");
        if (sel) sel.value = sym;
      }
      if (!preservePage) state.page = 0;
      applyFilters();
    } catch (err) {
      state.rows = [];
      state.filtered = [];
      state.summary = null;
      renderSummary(null);
      setBanner("Signal-Feed nicht erreichbar: " + err, true);
      renderTable();
    } finally {
      state.loading = false;
    }
  }

  function wire() {
    if (window.StochChartModal) {
      state.chart = new window.StochChartModal({});
    }
    [
      "stochFilterSymbol",
      "stochFilterDirection",
      "stochFilterTimeframe",
      "stochFilterSelected",
      "stochFilterHours",
      "stochFilterStrategy",
      "stochFilterTierA",
      "stochFilterTimingVariant",
    ].forEach((id) => {
      const el = $(id);
      if (el) {
        el.addEventListener("change", () => {
          state.page = 0;
          load();
        });
      }
    });
    const refresh = $("stochRefreshBtn");
    if (refresh) refresh.addEventListener("click", () => load({ preservePage: true }));
    const prev = $("stochPrevBtn");
    const next = $("stochNextBtn");
    if (prev) {
      prev.addEventListener("click", () => {
        if (state.page > 0) {
          state.page -= 1;
          renderTable();
        }
      });
    }
    if (next) {
      next.addEventListener("click", () => {
        const maxPage = Math.max(0, Math.ceil(state.filtered.length / state.pageSize) - 1);
        if (state.page < maxPage) {
          state.page += 1;
          renderTable();
        }
      });
    }
    document.querySelectorAll("[data-sort]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const key = btn.getAttribute("data-sort");
        if (state.sortBy === key) {
          state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
        } else {
          state.sortBy = key;
          state.sortDir = key === "candle_close_time" ? "desc" : "asc";
        }
        applyFilters();
      });
    });
  }

  function renderTable() {
    const tbody = $("stochSignalsBody");
    const meta = $("stochMeta");
    if (!tbody) return;

    const start = state.page * state.pageSize;
    const slice = state.filtered.slice(start, start + state.pageSize);

    if (!slice.length) {
      const emptyMsg = state.researchMode
        ? "Keine Research-1m-Timing-Signale (inkl. pending) im gewählten Zeitraum."
        : "Keine echten Tier-A-Signale im gewählten Zeitraum/Filter.";
      tbody.innerHTML = `<tr><td colspan="14" class="stoch-empty">${emptyMsg}</td></tr>`;
    } else if (state.researchMode) {
      tbody.innerHTML = slice
        .map((r, i) => {
          const idx = start + i;
          const entry = r.entry_price != null ? r.entry_price : r.signal_price;
          const trig = r.one_m_trigger_state || r["1m_trigger_state"] || r.result;
          const wait =
            r.wait_minutes === null || r.wait_minutes === undefined
              ? "–"
              : `${Number(r.wait_minutes).toFixed(1)}m`;
          return `<tr data-signal-id="${r.signal_id || ""}">
            <td><button type="button" class="stoch-chart-btn" data-idx="${idx}" title="Signal Chart / Inspector">${chartIcon()}</button></td>
            <td>${r.symbol || "–"}</td>
            <td>${chipTf(r.timeframe || r.signal_tf)}</td>
            <td>${chipDirection(r.trade_direction || r.direction)}</td>
            <td>${chipTriggerState(trig)}</td>
            <td>${fmtPrice(entry)}</td>
            <td>${fmtPrice(r.tp_price)}</td>
            <td>${fmtPrice(r.sl_price)}</td>
            <td>${wait}</td>
            <td>${fmtTs(r.original_tier_a_signal_ts || r.candle_close_time)}</td>
            <td>${chipResult(r.display_result || r.result, r)}</td>
            <td>${fmtPnl(r.pnl_pct)}</td>
            <td>${fmtNum(r.mae_pct, 2)}</td>
            <td>${fmtNum(r.mfe_pct, 2)}</td>
          </tr>`;
        })
        .join("");
    } else {
      tbody.innerHTML = slice
        .map((r, i) => {
          const idx = start + i;
          const entry = r.entry_price != null ? r.entry_price : r.signal_price;
          return `<tr data-signal-id="${r.signal_id || ""}">
            <td><button type="button" class="stoch-chart-btn" data-idx="${idx}" title="Signal Chart / Inspector">${chartIcon()}</button></td>
            <td>${r.symbol || "–"}</td>
            <td>${chipTf(r.timeframe)}</td>
            <td>${chipDirection(r.trade_direction || r.direction)}</td>
            <td>${fmtPrice(entry)}</td>
            <td>${fmtPrice(r.tp_price)}</td>
            <td>${fmtPrice(r.sl_price)}</td>
            <td>${fmtNum(r.stoch_k, 2)}</td>
            <td>${fmtTs(r.candle_close_time || r.signal_time)}</td>
            <td>${chipResult(r.display_result || r.result, r)}</td>
            <td>${fmtPnl(r.pnl_pct)}</td>
            <td>${fmtDuration(r.duration_seconds, r.display_result || r.result)}</td>
          </tr>`;
        })
        .join("");
    }

    if (meta) {
      meta.textContent = `${state.filtered.length} Signale · Seite ${state.page + 1}/${Math.max(1, Math.ceil(state.filtered.length / state.pageSize) || 1)} · auto ${state.refreshMs / 1000}s`;
    }
    const pageLabel = $("stochPageLabel");
    if (pageLabel) pageLabel.textContent = `Seite ${state.page + 1}`;

    tbody.querySelectorAll(".stoch-chart-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const row = state.filtered[Number(btn.getAttribute("data-idx"))];
        if (!row || !state.chart) return;
        const entry = row.entry_price != null && Number(row.entry_price) > 0 ? row.entry_price : null;
        state.chart.open({
          signal_id: row.signal_id,
          symbol: row.symbol,
          timeframe: row.timeframe,
          trade_direction: row.trade_direction || row.direction,
          direction: row.trade_direction || row.direction,
          signal_state: row.signal_state,
          tier_a: row.tier_a,
          stoch_k: row.stoch_k,
          stoch_d: row.stoch_d,
          wave_state: row.wave_state,
          is_demo: false,
          entry_price: entry,
          tp_price: row.tp_price,
          sl_price: row.sl_price,
          signal_time: row.candle_close_time || row.signal_time,
          expected_open_time: row.candle_close_time || row.signal_time,
          expected_open_price: entry,
          open_price: entry,
          expected_tp: row.tp_price,
          expected_sl: row.sl_price,
          generated_at: row.generated_at,
          result: row.display_result || row.result,
          frozen_result: row.frozen_result || row.result,
          display_result: row.display_result || row.result,
          exit_time: row.exit_time,
          exit_price: row.exit_price,
          exit_reason: row.exit_reason,
          be50_activated: row.be50_activated,
          be50_activated_at: row.be50_activated_at,
          pnl_pct: row.pnl_pct,
          duration_seconds: row.duration_seconds,
          counterfactual_no_be_result: row.counterfactual_no_be_result,
          counterfactual_no_be_exit_time: row.counterfactual_no_be_exit_time,
          counterfactual_no_be_pnl_pct: row.counterfactual_no_be_pnl_pct,
        });
      });
    });
  }

  function setBanner(msg, isError) {
    const banner = $("stochFeedBanner");
    if (!banner) return;
    if (!msg) {
      banner.style.display = "none";
      banner.innerHTML = "";
      return;
    }
    banner.style.display = "block";
    banner.className = "stoch-feed-banner" + (isError ? " stoch-feed-banner-error" : "");
    banner.textContent = msg;
  }

  function startAutoRefresh() {
    if (state.refreshTimer) clearInterval(state.refreshTimer);
    state.refreshTimer = setInterval(() => load({ preservePage: true }), state.refreshMs);
  }

  document.addEventListener("DOMContentLoaded", () => {
    wire();
    load();
    startAutoRefresh();
    wireCollector();
  });

  /* ------------------------------------------------------------------ */
  /* Collector Steuerung / Status (proxied via dashboard backend)      */
  /* ------------------------------------------------------------------ */

  const collectorUi = {
    status: null,
    busy: false,
    pollMs: 4000,
    timer: null,
  };

  function setErr(elId, msg) {
    const el = $(elId);
    if (!el) return;
    if (!msg) {
      el.style.display = "none";
      el.textContent = "";
      return;
    }
    el.style.display = "block";
    el.textContent = msg;
  }

  function healthChipClass(hstate) {
    const v = String(hstate || "").toUpperCase();
    if (v === "LIVE" || v === "RUNNING" || v === "ACTIVE" || v === "TRUE" || v === "CONNECTED") {
      return "stoch-chip-live";
    }
    if (
      v === "RECOVERING" ||
      v === "SUBSCRIBING" ||
      v === "STARTING" ||
      v === "CONNECTING" ||
      v === "RECONNECTING" ||
      v === "CAUGHT_UP" ||
      v === "CATCHING_UP" ||
      v === "IDLE"
    ) {
      return "stoch-chip-warn";
    }
    if (v === "ERROR" || v === "STALE" || v === "DEGRADED" || v === "FALSE" || v === "DISCONNECTED") {
      return "stoch-chip-err";
    }
    if (v === "STOPPED" || v === "STOPPING") {
      return "stoch-chip-stopped";
    }
    return "stoch-chip-pending";
  }

  function setChip(elId, label) {
    const el = $(elId);
    if (!el) return;
    const text = label == null || label === "" ? "–" : String(label);
    el.className = "stoch-chip " + healthChipClass(text);
    el.textContent = text;
  }

  function fmtCollectorTs(ts) {
    if (!ts) return "–";
    return fmtTs(ts);
  }

  function countList(v) {
    if (Array.isArray(v)) return v.length;
    if (typeof v === "number") return v;
    return 0;
  }

  function isBusyCollectorState(cstate) {
    const v = String(cstate || "").toUpperCase();
    return ["RUNNING", "LIVE", "STARTING", "RECOVERING", "SUBSCRIBING", "CONNECTING", "RECONNECTING"].includes(v);
  }

  function isStoppedCollectorState(cstate) {
    const v = String(cstate || "").toUpperCase();
    return ["STOPPED", "STOPPING"].includes(v);
  }

  function updateCollectorButtons() {
    const startBtn = $("collectorStartBtn");
    const stopBtn = $("collectorStopBtn");
    if (!startBtn || !stopBtn) return;
    const st = collectorUi.status || {};
    const desired = String(st.desired_state || "").toUpperCase();
    const collector = String(st.collector_state || st.state || "").toUpperCase();
    const startDisabled =
      collectorUi.busy || desired === "RUNNING" || isBusyCollectorState(collector);
    const stopDisabled =
      collectorUi.busy || desired === "STOPPED" || isStoppedCollectorState(collector);
    startBtn.disabled = startDisabled;
    stopBtn.disabled = stopDisabled;
  }

  function renderCollectorStatus(data) {
    collectorUi.status = data || {};
    const st = collectorUi.status;
    const collectorState = st.collector_state || st.state || "–";
    const desired = st.desired_state || "–";
    const shadow =
      st.shadow_mode === true || st.shadow_mode === 1
        ? "ACTIVE"
        : st.shadow_mode === false || st.shadow_mode === 0
          ? "OFF"
          : "–";

    setChip("collectorStateChip", collectorState);
    setChip("collectorDesiredChip", desired);
    setChip("collectorShadowChip", shadow);

    const configured = countList(st.configured_symbols) || Number(st.configured_count) || 0;
    const subscribed = countList(st.subscribed_symbols) || Number(st.subscribed_count) || 0;
    const live = countList(st.live_symbols) || Number(st.live_count) || 0;
    const recovering = countList(st.recovering_symbols) || Number(st.symbols_recovering) || 0;
    const stale = countList(st.stale_symbols) || 0;
    const ws = st.websocket_connected ? "Connected" : "Disconnected";

    const summary = $("collectorStatusSummary");
    if (summary) {
      summary.innerHTML = [
        `<span>WebSocket: <strong class="${st.websocket_connected ? "stoch-pnl-pos" : "stoch-pnl-neg"}">${ws}</strong></span>`,
        `<span>Subscribed: <strong>${subscribed} / ${configured}</strong></span>`,
        `<span>Live: <strong>${live} / ${configured}</strong></span>`,
        `<span>Recovering: <strong>${recovering}</strong></span>`,
        `<span>Stale: <strong>${stale}</strong></span>`,
        `<span>Reconnects: <strong>${st.reconnect_count ?? 0}</strong></span>`,
      ].join("");
    }

    const meta = $("collectorStatusMeta");
    if (meta) {
      meta.innerHTML = [
        `<span>last_message_at: ${fmtCollectorTs(st.last_message_at)}</span>`,
        `<span>last_ping_at: ${fmtCollectorTs(st.last_ping_at)}</span>`,
        `<span>last_pong_at: ${fmtCollectorTs(st.last_pong_at)}</span>`,
        `<span>started_at: ${fmtCollectorTs(st.started_at)}</span>`,
      ].join("");
    }

    const tbody = $("collectorSymbolsBody");
    if (tbody) {
      const symbols = Array.isArray(st.symbols) ? st.symbols : [];
      if (!symbols.length) {
        tbody.innerHTML = `<tr><td colspan="6" class="stoch-empty">Keine Symbol-Daten im Status.</td></tr>`;
      } else {
        tbody.innerHTML = symbols
          .map((row) => {
            const rowState = row.state || "–";
            const lag =
              row.candle_lag_seconds == null || row.candle_lag_seconds === ""
                ? "–"
                : `${Number(row.candle_lag_seconds).toFixed(0)}s`;
            const lastCandle = row.last_closed_candle_at || row.last_persisted_open_time || null;
            return `<tr>
              <td>${row.symbol || "–"}</td>
              <td><span class="stoch-chip ${healthChipClass(rowState)}">${rowState}</span></td>
              <td>${row.subscribed ? "yes" : "no"}</td>
              <td>${fmtCollectorTs(lastCandle)}</td>
              <td>${lag}</td>
              <td>${row.signal_processor_state || "–"}</td>
            </tr>`;
          })
          .join("");
      }
    }

    updateCollectorButtons();
  }

  async function loadCollectorStatus() {
    try {
      const res = await fetch("/api/collector/status", { credentials: "include" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        const msg = data.error || data.detail || `HTTP ${res.status}`;
        setErr("collectorControlError", "Status: " + msg);
        setErr("collectorStatusError", "Status: " + msg);
        updateCollectorButtons();
        return;
      }
      if (String(data.collector_state || "").toUpperCase() === "UNAVAILABLE" || data.error === "collector_api_unreachable") {
        setErr("collectorControlError", "Status: Collector API nicht erreichbar (" + (data.detail || data.error || "offline") + ")");
        setErr("collectorStatusError", "Status: Collector-Prozess auf Port 8787 ist nicht gestartet.");
        renderCollectorStatus(data);
        updateCollectorButtons();
        return;
      }
      setErr("collectorControlError", "");
      setErr("collectorStatusError", "");
      renderCollectorStatus(data);
    } catch (err) {
      setErr("collectorControlError", "Collector API nicht erreichbar: " + err);
      setErr("collectorStatusError", "Collector API nicht erreichbar: " + err);
      updateCollectorButtons();
    }
  }

  async function setDesiredState(desired) {
    collectorUi.busy = true;
    updateCollectorButtons();
    setErr("collectorControlError", "");
    try {
      const res = await fetch("/api/collector/desired_state", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ desired_state: desired }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.error || data.detail || `HTTP ${res.status}`);
      }
      await loadCollectorStatus();
    } catch (err) {
      setErr("collectorControlError", "Steuerung fehlgeschlagen: " + err);
    } finally {
      collectorUi.busy = false;
      updateCollectorButtons();
    }
  }

  function wireCollector() {
    const startBtn = $("collectorStartBtn");
    const stopBtn = $("collectorStopBtn");
    if (startBtn) {
      startBtn.addEventListener("click", () => setDesiredState("RUNNING"));
    }
    if (stopBtn) {
      stopBtn.addEventListener("click", () => setDesiredState("STOPPED"));
    }
    loadCollectorStatus();
    if (collectorUi.timer) clearInterval(collectorUi.timer);
    collectorUi.timer = setInterval(loadCollectorStatus, collectorUi.pollMs);
  }
})();
