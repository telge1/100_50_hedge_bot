document.addEventListener("DOMContentLoaded", function () {
  initV2BacktestMode();
});

function initV2BacktestMode() {
  const state = {
    runId: "july_2026_pd10_smoke",
    variantKey: "15m_pd10_guard_off",
    page: Number(window.EMA_SIGNAL_PAGE || 0),
    pageSize: Number(window.EMA_SIGNAL_PAGE_SIZE || 50),
    poolDetail: 10,
    guardOn: false,
    chart: null,
  };

  const runSelect = document.getElementById("v2RunSelect");
  const pdSel = document.getElementById("v2PoolDetail");
  const guardSel = document.getElementById("v2GuardOn");
  const applyBtn = document.getElementById("v2FilterApply");

  if (!state.chart && window.EmaSignaleV2ChartModal) {
    state.chart = new window.EmaSignaleV2ChartModal({});
  }

  async function loadRuns() {
    if (!runSelect) return;
    try {
      const res = await fetch("/api/ema-signale/v2-backtest/runs", { credentials: "include" });
      const data = await res.json();
      const runs = (data && data.runs) || [];
      runSelect.innerHTML = runs
        .map(
          (r) =>
            `<option value="${r.run_id}">${r.label || r.run_id} (${r.warning || ""})</option>`
        )
        .join("");
      if (runs.length && !state.runId) {
        state.runId = runs[0].run_id;
      }
      if (state.runId) {
        runSelect.value = state.runId;
      }
    } catch (_e) {
      runSelect.innerHTML =
        '<option value="july_2026_pd10_smoke">Juli 2026 Smoke (PD10, DOGE)</option>';
    }
  }

  function variantKeyFromUi() {
    const pd = Number((pdSel && pdSel.value) || state.poolDetail);
    const g = guardSel && guardSel.checked;
    return `15m_pd${pd}_guard_${g ? "on" : "off"}`;
  }

  async function loadResults() {
    if (runSelect && runSelect.value) {
      state.runId = runSelect.value;
    }
    state.poolDetail = Number((pdSel && pdSel.value) || 10);
    state.guardOn = !!(guardSel && guardSel.checked);
    state.variantKey = variantKeyFromUi();

    const resolveRes = await fetch(
      `/api/ema-signale/v2-backtest/resolve-config?pool_detail=${state.poolDetail}&guard_on=${state.guardOn}`,
      { credentials: "include" }
    );
    const resolve = await resolveRes.json();
    const banner = document.getElementById("v2StatusBanner");
    if (!resolve.available) {
      if (banner) banner.textContent = resolve.message || "Kein Report für diese Konfiguration.";
      renderEmptyTable();
      renderKpis({});
      updatePagination({ total_filtered: 0, page: 0, page_size: state.pageSize, has_prev: false, has_next: false });
      return;
    }
    state.runId = resolve.run_id;
    if (banner) {
      banner.textContent = resolve.warning || "SMOKE / STATE_WARMUP_NOT_PROVEN";
    }

    const url =
      `/api/ema-signale/v2-backtest/results?run_id=${encodeURIComponent(state.runId)}` +
      `&variant_key=${encodeURIComponent(state.variantKey)}` +
      `&page=${state.page}&page_size=${state.pageSize}`;
    const res = await fetch(url, { credentials: "include" });
    const data = await res.json();
    if (!res.ok || !data.success) {
      if (banner) banner.textContent = data.detail || data.message || "Laden fehlgeschlagen";
      return;
    }
    renderKpis(data.summary || {});
    renderTrades(data.trades || []);
    updatePagination(data.pagination || {});
  }

  function renderKpis(summary) {
    const set = (id, val) => {
      const el = document.getElementById(id);
      if (el) el.textContent = val != null ? String(val) : "0";
    };
    set("v2KpiTrades", summary.trades);
    set("v2KpiTp", summary.tp_exits);
    set("v2KpiSl", summary.sl_exits);
    set("v2KpiHorizon", summary.horizon_exits);
    const pnl = document.getElementById("v2KpiPnl");
    if (pnl) {
      pnl.textContent =
        summary.pnl_sum_pct != null ? Number(summary.pnl_sum_pct).toFixed(4) + "%" : "–";
    }
  }

  function fmtPrice(n) {
    if (n == null || Number.isNaN(Number(n))) return "–";
    return Number(n).toFixed(6).replace(/0+$/, "").replace(/\.$/, "");
  }

  function renderTrades(rows) {
    const body = document.getElementById("emaSignalsBody");
    if (!body) return;
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="12" class="stoch-empty">Keine Trades</td></tr>';
      return;
    }
    body.innerHTML = rows
      .map((r, idx) => {
        return `<tr data-idx="${idx}">
          <td>${r.decision_close || "–"}</td>
          <td>${r.symbol || "–"} <button type="button" class="stoch-chart-btn v2-chart-btn" data-idx="${idx}" title="Chart">📈</button></td>
          <td><span class="stoch-chip stoch-chip-short">SHORT</span></td>
          <td>${fmtPrice(r.entry_price)}</td>
          <td>${fmtPrice(r.stop)}</td>
          <td>${fmtPrice(r.tp)}</td>
          <td>${r.exit_time || "–"}</td>
          <td>${r.exit_reason || "–"}</td>
          <td>${r.pnl_pct != null ? Number(r.pnl_pct).toFixed(4) : "–"}</td>
          <td>${r.r_multiple != null ? Number(r.r_multiple).toFixed(2) : "–"}</td>
          <td>PD${r.pool_detail || 10}</td>
          <td>${r.guard_on ? "ON" : "OFF"}</td>
        </tr>`;
      })
      .join("");

    body.querySelectorAll(".v2-chart-btn").forEach((btn) => {
      btn.addEventListener("click", function () {
        const idx = Number(btn.getAttribute("data-idx"));
        const row = rows[idx];
        if (state.chart && row) state.chart.open(row);
      });
    });
    window.__v2TradeRows = rows;
  }

  function renderEmptyTable() {
    const body = document.getElementById("emaSignalsBody");
    if (body) body.innerHTML = '<tr><td colspan="12" class="stoch-empty">Keine Trades</td></tr>';
  }

  function updatePagination(p) {
    const info = document.getElementById("emaPaginationInfo");
    if (!info) return;
    const total = p.total_filtered || 0;
    if (!total) {
      info.textContent = "Keine Trades";
      return;
    }
    const start = p.page * p.page_size + 1;
    const end = Math.min(total, (p.page + 1) * p.page_size);
    info.textContent = `Zeige ${start}-${end} von ${total} Trades`;
    const prev = document.getElementById("emaPagePrev");
    const next = document.getElementById("emaPageNext");
    if (prev) prev.disabled = !p.has_prev;
    if (next) next.disabled = !p.has_next;
  }

  const prevBtn = document.getElementById("emaPagePrev");
  const nextBtn = document.getElementById("emaPageNext");
  if (prevBtn) {
    prevBtn.addEventListener("click", function () {
      if (state.page > 0) {
        state.page -= 1;
        loadResults();
      }
    });
  }
  if (nextBtn) {
    nextBtn.addEventListener("click", function () {
      state.page += 1;
      loadResults();
    });
  }
  if (applyBtn) {
    applyBtn.addEventListener("click", function () {
      state.page = 0;
      loadResults();
    });
  }
  if (runSelect) {
    runSelect.addEventListener("change", function () {
      state.page = 0;
      loadResults();
    });
  }

  loadRuns().then(loadResults);
}
