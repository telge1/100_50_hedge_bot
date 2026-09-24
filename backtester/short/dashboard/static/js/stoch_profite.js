(function () {
  "use strict";

  const state = {
    rows: [],
    filtered: [],
    sortBy: "open_time",
    sortDir: "desc",
    page: 0,
    pageSize: 15,
    chart: null,
  };

  const $ = (id) => document.getElementById(id);

  function fmtNum(n, d) {
    if (n === null || n === undefined || Number.isNaN(Number(n))) return "–";
    return Number(n).toFixed(d);
  }

  function fmtTs(ts) {
    if (window.StochUtc && typeof window.StochUtc.fmtUtc === "function") {
      return window.StochUtc.fmtUtc(ts);
    }
    if (!ts && ts !== 0) return "–";
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
    if (v === "COMPLETED" || v === "TP_HIT") cls = "stoch-chip-done";
    if (v.includes("SL")) cls = "stoch-chip-sl";
    return `<span class="stoch-chip ${cls}">${v || "–"}</span>`;
  }

  function pnlClass(n) {
    const v = Number(n);
    if (Number.isNaN(v)) return "";
    return v >= 0 ? "stoch-pnl-pos" : "stoch-pnl-neg";
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
    sel.innerHTML = `<option value="">${allLabel}</option>` + values.map((v) => `<option value="${v}">${v}</option>`).join("");
    if (values.includes(cur)) sel.value = cur;
  }

  function applyFilters() {
    const symbol = ($("stochFilterSymbol") || {}).value || "";
    const direction = ($("stochFilterDirection") || {}).value || "";
    const tradeState = ($("stochFilterState") || {}).value || "";
    const batchId = ($("stochFilterBatch") || {}).value || "";

    state.filtered = state.rows.filter((r) => {
      if (symbol && r.symbol !== symbol) return false;
      if (direction && String(r.trade_direction).toUpperCase() !== direction) return false;
      if (tradeState && String(r.trade_state).toUpperCase() !== tradeState) return false;
      if (batchId && String(r.batch_id) !== batchId) return false;
      return true;
    });

    state.filtered.sort((a, b) => {
      let av = a[state.sortBy];
      let bv = b[state.sortBy];
      if (state.sortBy === "open_time" || state.sortBy === "close_time") {
        av = Number(av) || 0;
        bv = Number(bv) || 0;
      }
      if (av == null) return 1;
      if (bv == null) return -1;
      if (typeof av === "string") {
        const cmp = av.localeCompare(bv);
        return state.sortDir === "asc" ? cmp : -cmp;
      }
      return state.sortDir === "asc" ? av - bv : bv - av;
    });

    const maxPage = Math.max(0, Math.ceil(state.filtered.length / state.pageSize) - 1);
    if (state.page > maxPage) state.page = maxPage;
    renderTable();
  }

  function renderTable() {
    const tbody = $("stochProfitsBody");
    const meta = $("stochMeta");
    if (!tbody) return;

    const start = state.page * state.pageSize;
    const slice = state.filtered.slice(start, start + state.pageSize);

    if (!slice.length) {
      tbody.innerHTML = `<tr><td colspan="12" class="stoch-empty">Keine Stoch-Profite vorhanden (Feed noch nicht angebunden).</td></tr>`;
    } else {
      tbody.innerHTML = slice
        .map((r, i) => {
          const idx = start + i;
          const demo = r.is_demo ? `<span class="stoch-chip stoch-chip-demo">DEMO</span>` : "";
          return `<tr>
            <td><button type="button" class="stoch-chart-btn" data-idx="${idx}" title="Chart">${chartIcon()}</button></td>
            <td>${r.symbol || "–"} ${demo}</td>
            <td class="${pnlClass(r.pnl)}">${fmtNum(r.pnl, 4)}</td>
            <td class="${pnlClass(r.pnl_percent)}">${fmtNum(r.pnl_percent, 2)}%</td>
            <td>${fmtTs(r.open_time)}</td>
            <td>${fmtTs(r.close_time)}</td>
            <td>${fmtNum(r.open_price, 6)}</td>
            <td>${fmtNum(r.close_price, 6)}</td>
            <td>${chipState(r.trade_state)}</td>
            <td>${r.batch_id || "–"}</td>
            <td>${chipDirection(r.trade_direction)}</td>
            <td>${r.strategy || "wave_fade"}</td>
          </tr>`;
        })
        .join("");
    }

    if (meta) {
      meta.textContent = `${state.filtered.length} Trades · Seite ${state.page + 1}/${Math.max(1, Math.ceil(state.filtered.length / state.pageSize) || 1)}`;
    }

    const pageLabel = $("stochPageLabel");
    if (pageLabel) pageLabel.textContent = `Seite ${state.page + 1}`;

    tbody.querySelectorAll(".stoch-chart-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const row = state.filtered[Number(btn.getAttribute("data-idx"))];
        if (row && state.chart) state.chart.open(row);
      });
    });
  }

  async function load() {
    const banner = $("stochFeedBanner");
    try {
      const res = await fetch("/api/stoch/profits", { credentials: "include" });
      const data = await res.json();
      state.rows = Array.isArray(data.records) ? data.records : [];
      if (banner) {
        if (data.feed_ready) {
          banner.style.display = "none";
        } else {
          banner.style.display = "block";
          banner.innerHTML =
            data.message ||
            "Wave-Fade / Stoch-Feed ist noch nicht angebunden. Es werden Demo-Zeilen mit dem Ziel-Schema angezeigt.";
        }
      }
      fillSelect($("stochFilterSymbol"), unique(state.rows.map((r) => r.symbol)), "Alle Symbole");
      fillSelect($("stochFilterBatch"), unique(state.rows.map((r) => String(r.batch_id || ""))), "Alle Batches");
      fillSelect(
        $("stochFilterState"),
        unique(state.rows.map((r) => String(r.trade_state || "").toUpperCase())),
        "Alle States"
      );
      state.page = 0;
      applyFilters();
    } catch (err) {
      if (banner) {
        banner.style.display = "block";
        banner.textContent = "Fehler beim Laden der Stoch-Profite: " + err;
      }
    }
  }

  function wire() {
    state.chart = new window.StochChartModal({});
    ["stochFilterSymbol", "stochFilterDirection", "stochFilterState", "stochFilterBatch"].forEach((id) => {
      const el = $(id);
      if (el) el.addEventListener("change", () => {
        state.page = 0;
        applyFilters();
      });
    });
    const refresh = $("stochRefreshBtn");
    if (refresh) refresh.addEventListener("click", load);
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
          state.sortDir = "desc";
        }
        applyFilters();
      });
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    wire();
    load();
  });
})();
