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
    poolResearch: false,
    frozenResearchJob: false,
    researchJobId: "",
    researchEvaluation: false,
  };

  const frozenEval = { job: null, pollTimer: null, pollMs: 4000 };

  function frozenEvalJobActive() {
    const st = frozenEval.job && frozenEval.job.state;
    return st === "QUEUED" || st === "RUNNING";
  }

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
    if (
      row &&
      row.source === "FROZEN_RESEARCH_JOB" &&
      row.outcomes_computed !== true
    ) {
      return '<span class="stoch-chip stoch-chip-none">nicht berechnet</span>';
    }
    const v = String(r || "OPEN").toUpperCase();
    let cls = "stoch-chip-pending";
    if (v === "WIN") cls = "stoch-chip-live";
    else if (v === "LOSS") cls = "stoch-chip-err";
    else if (v === "BE / WIN") cls = "stoch-chip-be-win";
    else if (v === "BE / LOSS") cls = "stoch-chip-be-loss";
    else if (v === "BE / OPEN" || v === "BE") cls = "stoch-chip-warn";
    else if (v === "OPEN") cls = "stoch-chip-open";
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

  function selectedResearchJobId() {
    return (($("stochFilterResearchJob") || {}).value || "").trim();
  }

  function selectedEvaluationId() {
    return (($("stochFilterResearchEval") || {}).value || "").trim();
  }

  function storedResearchJobId() {
    try {
      const source = localStorage.getItem("stoch.signal_source") || "";
      const jobId = localStorage.getItem("stoch.research_job_id") || "";
      if (
        (source === "FROZEN_RESEARCH_JOB" || source === "FROZEN_RESEARCH_EVALUATION") &&
        /^[0-9a-f]{32}$/.test(jobId)
      ) {
        return jobId;
      }
    } catch (_) {}
    return "";
  }

  function selectedJobOption() {
    const sel = $("stochFilterResearchJob");
    if (!sel || sel.selectedIndex < 0) return null;
    return sel.options[sel.selectedIndex] || null;
  }

  function applyJobSourceUi(job) {
    const jobId = selectedResearchJobId();
    const evalId = selectedEvaluationId();
    const on = !!jobId;
    state.frozenResearchJob = on;
    state.researchJobId = jobId;
    state.researchEvaluation = !!evalId;
    const strat = $("stochFilterStrategy");
    const stratLabel = $("stochFilterStrategyLabel");
    const stratNote = $("stochStrategyJobNote");
    const hours = $("stochFilterHours");
    const hoursLabel = $("stochFilterHoursLabel");
    const win = $("stochJobWindowNote");
    const evalWrap = $("stochEvalFilterWrap");
    const evalBtn = $("frozenEvalStartBtn");
    if (evalWrap) evalWrap.hidden = !on;
    if (evalBtn) evalBtn.disabled = !on || frozenEvalJobActive();
    if (strat) {
      strat.disabled = on;
      strat.hidden = on;
    }
    if (stratLabel) stratLabel.hidden = on;
    if (stratNote) {
      stratNote.hidden = !on;
      stratNote.textContent = evalId
        ? "Canonical Causal Frozen Tier-A · cross_recognition · NO_BE50 / full 1m / SL_FIRST"
        : "Canonical Causal Frozen Tier-A · nur Plan-Signale, Outcomes fehlen noch";
    }
    if (hours) {
      hours.disabled = on;
      hours.hidden = on;
    }
    if (hoursLabel) hoursLabel.hidden = on;
    if (win) {
      if (on) {
        const opt = selectedJobOption();
        const start = (job && job.signal_start) || (opt && opt.getAttribute("data-start")) || "";
        const end = (job && job.signal_end_exclusive) || (opt && opt.getAttribute("data-end")) || "";
        win.hidden = false;
        win.textContent = start && end
          ? ("Job-Fenster: " + start + " – " + end)
          : "Job-Fenster: aus Job-Manifest";
      } else {
        win.hidden = true;
        win.textContent = "";
      }
    }
  }

  function persistStochChartBridge() {
    const jobId = selectedResearchJobId();
    const evalId = selectedEvaluationId();
    const filterSym = (($("stochFilterSymbol") || {}).value || "").trim().toUpperCase();
    const rowSym = ((state.rows[0] || {}).symbol || "").toString().trim().toUpperCase();
    try {
      if (jobId) {
        localStorage.setItem("stoch.research_job_id", jobId);
        if (evalId) {
          localStorage.setItem("stoch.signal_source", "FROZEN_RESEARCH_EVALUATION");
          localStorage.setItem("stoch.research_evaluation_id", evalId);
        } else {
          localStorage.setItem("stoch.signal_source", "FROZEN_RESEARCH_JOB");
          localStorage.removeItem("stoch.research_evaluation_id");
        }
      } else {
        localStorage.setItem("stoch.signal_source", "FROZEN_BASELINE");
        localStorage.removeItem("stoch.research_job_id");
        localStorage.removeItem("stoch.research_evaluation_id");
        const sv = (($("stochFilterStrategy") || {}).value || "wave_fade_no_be50_v1");
        localStorage.setItem("stoch.strategy_version", sv);
      }
      if (filterSym || rowSym) localStorage.setItem("stoch.last_symbol", filterSym || rowSym);
    } catch (_) {}
  }

  function isPoolStrategy() {
    return (($("stochFilterStrategy") || {}).value || "") === "POOL_ORDER_PLAN_V1";
  }

  function isEmaFlipStrategy() {
    return (($("stochFilterStrategy") || {}).value || "") === "EMA_POOL_TREND_FLIP_V1";
  }

  function isEzmStrategy() {
    return (($("stochFilterStrategy") || {}).value || "") === "ema_zone_microstructure_confirmation_v1";
  }

  function isCausalFrozenStrategy() {
    return (($("stochFilterStrategy") || {}).value || "") === "wave_fade_frozen_f16ae32_causal_entry_v1";
  }

  function researchJobStrategyId() {
    if (isEzmStrategy()) return "ema_zone_microstructure_confirmation_v1";
    // Default / causal frozen: omit → server uses Frozen Fade (backward compatible)
    if (isCausalFrozenStrategy()) return "wave_fade_frozen_f16ae32_causal_entry_v1";
    return null;
  }

  function setEmaFlipResearchUi(on, banner) {
    state.emaFlipResearch = !!on;
    const el = $("stochPoolResearchBanner");
    if (el && on) {
      el.style.display = "block";
      el.removeAttribute("hidden");
      const title = el.querySelector(".stoch-research-banner-title") || el;
      if (banner && banner.title) {
        el.innerHTML =
          `<div class="stoch-research-banner-title">${banner.title}</div>` +
          `<div class="stoch-research-banner-sub" id="stochPoolResearchWindow">${String(banner.body || "").replace(/\n/g, "<br>")}</div>`;
      }
    }
    const extra = $("stochPoolPerfExtra");
    if (extra) extra.style.display = on ? "grid" : extra.style.display;
    const hours = $("stochFilterHours");
    if (hours && on) hours.disabled = true;
    const thead = document.querySelector("#stochSignalsTable thead tr");
    if (thead && on && !state.researchMode) {
      thead.innerHTML = `
          <th></th>
          <th><button type="button" data-sort="signal_time">Signalzeit</button></th>
          <th><button type="button" data-sort="entry_time">Entry-Zeit</button></th>
          <th><button type="button" data-sort="symbol">Symbol</button></th>
          <th>Signal-TF</th>
          <th>Orig.</th>
          <th>Ausgeführt</th>
          <th>Decision</th>
          <th>Entry</th>
          <th>EMA9</th>
          <th>EMA20</th>
          <th>Sep/ATR</th>
          <th>EMA-Trend</th>
          <th>Last Cross</th>
          <th>Pool-TF</th>
          <th>Bias up/dn</th>
          <th>Schutzpool</th>
          <th>SL</th>
          <th>SL-Abstand</th>
          <th>WIDE</th>
          <th>Exit</th>
          <th>Exit-Grund</th>
          <th>Gross</th>
          <th>Fees</th>
          <th>Net</th>
          <th>Hold</th>
          <th>Variant</th>`;
      thead.querySelectorAll("[data-sort]").forEach((btn) => {
        btn.addEventListener("click", () => {
          const key = btn.getAttribute("data-sort");
          if (state.sortBy === key) {
            state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
          } else {
            state.sortBy = key;
            state.sortDir = key === "candle_close_time" || key === "entry_time" ? "desc" : "asc";
          }
          applyFilters();
        });
      });
    }
  }

  function setPoolResearchUi(on, banner) {
    state.poolResearch = !!on;
    const el = $("stochPoolResearchBanner");
    if (el) {
      el.style.display = on ? "block" : "none";
      if (on) el.removeAttribute("hidden");
      else el.setAttribute("hidden", "hidden");
    }
    const extra = $("stochPoolPerfExtra");
    if (extra) extra.style.display = on ? "grid" : "none";
    const poolFilters = $("stochPoolFilters");
    if (poolFilters) poolFilters.style.display = on ? "flex" : "none";
    const hours = $("stochFilterHours");
    if (hours) hours.disabled = !!on;
    const thead = document.querySelector("#stochSignalsTable thead tr");
    if (thead && !state.researchMode) {
      if (on) {
        thead.innerHTML = `
          <th></th>
          <th><button type="button" data-sort="entry_time">Zeit</button></th>
          <th><button type="button" data-sort="symbol">Symbol</button></th>
          <th><button type="button" data-sort="trade_direction">LONG/SHORT</button></th>
          <th><button type="button" data-sort="signal_timeframe">Signal-TF</button></th>
          <th>Pool-TF</th>
          <th>Entry</th>
          <th>Pool-Modus</th>
          <th><button type="button" data-sort="entry_pool_count">Pools</button></th>
          <th>SL</th>
          <th>SL-Abstand</th>
          <th>TP1</th>
          <th>TP1-Größe</th>
          <th>TP2</th>
          <th>TP2-Größe</th>
          <th><button type="button" data-sort="outcome">Outcome</button></th>
          <th>Gross</th>
          <th>Fees</th>
          <th>Net</th>
          <th>Hold</th>
          <th>5m Open</th>
          <th>5m Close</th>`;
      } else if (!thead.querySelector('[data-sort="candle_close_time"]')) {
        thead.innerHTML = `
          <th></th>
          <th><button type="button" data-sort="symbol">Symbol</button></th>
          <th><button type="button" data-sort="timeframe">TF</button></th>
          <th><button type="button" data-sort="trade_direction">Direction</button></th>
          <th><button type="button" data-sort="entry_price">Entry</button></th>
          <th>TP</th>
          <th>SL</th>
          <th>Stoch K</th>
          <th><button type="button" data-sort="candle_close_time">Signal Time</button></th>
          <th><button type="button" data-sort="result">Result</button></th>
          <th><button type="button" data-sort="pnl_pct">PnL</button></th>
          <th><button type="button" data-sort="duration_seconds">Duration</button></th>`;
      }
      thead.querySelectorAll("[data-sort]").forEach((btn) => {
        btn.addEventListener("click", () => {
          const key = btn.getAttribute("data-sort");
          if (state.sortBy === key) {
            state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
          } else {
            state.sortBy = key;
            state.sortDir = key === "candle_close_time" || key === "entry_time" ? "desc" : "asc";
          }
          applyFilters();
        });
      });
    }
    if (on && banner && banner.window_label) {
      const win = $("stochPoolResearchWindow");
      if (win) win.innerHTML = String(banner.window_label).replace(/\n/g, "<br>");
    }
  }

  function renderSummary(summary) {
    const s = summary || {};
    const set = (id, val) => {
      const el = $(id);
      if (el) el.textContent = val;
    };
    set("perfSignals", s.signals != null ? String(s.signals) : (s.filtered_signals != null ? String(s.filtered_signals) : "–"));
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
      if (state.emaFlipResearch || sv === "EMA_POOL_TREND_FLIP_V1") {
        meta.textContent = "EMA-Pool-Trend-Flip PnL: net after fees · RESEARCH / BACKTEST ONLY";
        set("perfGrossPnl", fmtPctSigned(s.gross_pnl_pct));
        set("perfFees", fmtPctSigned(s.fees_pct));
        set("perfNet", fmtPctSigned(s.net_pnl_pct));
        set(
          "perfPf",
          s.profit_factor == null || Number.isNaN(Number(s.profit_factor))
            ? "–"
            : Number(s.profit_factor).toFixed(2)
        );
        set("perfMdd", fmtPctSigned(s.max_drawdown_pct));
        set("perfSlWide", s.sl_too_wide_count != null ? String(s.sl_too_wide_count) : "–");
        const totalEl = $("perfTotal");
        if (totalEl) totalEl.textContent = fmtPctSigned(s.net_pnl_pct);
      } else if (state.poolResearch || sv === "POOL_ORDER_PLAN_V1") {
        meta.textContent = "Pool-V1 PnL: net after fees · Baseline PnL: gross";
        set("perfReady", s.ready != null ? String(s.ready) : "–");
        set("perfNoPlan", s.no_plan != null ? String(s.no_plan) : "–");
        set("perfGrossPnl", fmtPctSigned(s.gross_pnl_pct));
        set("perfFees", fmtPctSigned(s.fees_pct));
        set("perfNet", fmtPctSigned(s.net_pnl_pct));
        set(
          "perfPf",
          s.profit_factor == null || Number.isNaN(Number(s.profit_factor))
            ? "–"
            : Number(s.profit_factor).toFixed(2)
        );
        set("perfMdd", fmtPctSigned(s.max_drawdown_pct));
        set("perfSlWide", s.sl_too_wide_count != null ? String(s.sl_too_wide_count) : "–");
        set("perfOneTarget", s.one_target_count != null ? String(s.one_target_count) : "–");
        set("perfTwoTargets", s.two_target_count != null ? String(s.two_target_count) : "–");
        set("perfIgnored", s.ignored_duplicates != null ? String(s.ignored_duplicates) : "–");
        const totalEl = $("perfTotal");
        if (totalEl) totalEl.textContent = fmtPctSigned(s.net_pnl_pct);
      } else if (state.researchEvaluation || s.outcomes_computed === true) {
        meta.textContent =
          "Frozen NO_BE50 · SL_FIRST · PnL basis: gross · OPEN excluded from win rate · Execution-Dedup nicht angewendet";
      } else if (state.frozenResearchJob || s.outcomes_computed === false) {
        meta.textContent =
          "Frozen Research Job · Outcomes nicht berechnet · keine Wins/Losses/Open/PnL";
      } else {
        meta.textContent = `PnL basis: gross · Strategy: ${sv} · Stats = alle Treffer der aktiven Filter (nicht nur Seite)`;
      }
    }
  }

  function applyFilters() {
    let rows = state.rows.slice();
    if (state.poolResearch) {
      const outcome = (($("stochFilterOutcome") || {}).value || "").toUpperCase();
      const slWide = (($("stochFilterSlWide") || {}).value || "");
      const targets = (($("stochFilterTargets") || {}).value || "");
      rows = rows.filter((r) => {
        if (outcome) {
          const got = String(r.display_result || r.outcome || r.result || "").toUpperCase();
          if (got !== outcome) return false;
        }
        if (slWide === "true" && !r.sl_too_wide) return false;
        if (slWide === "false" && r.sl_too_wide) return false;
        if (targets === "one") {
          const one = Number(r.tp1_size) === 1 && !(Number(r.tp2_size) > 0);
          if (!one) return false;
        }
        if (targets === "two") {
          const two = Number(r.tp1_size) === 0.5 && Number(r.tp2_size) === 0.5;
          if (!two) return false;
        }
        return true;
      });
    }
    const key = state.sortBy;
    const dir = state.sortDir === "asc" ? 1 : -1;
    rows.sort((a, b) => {
      let av = a[key];
      let bv = b[key];
      if (key === "candle_close_time" || key === "signal_time" || key === "entry_time" || key === "last_5m_open" || key === "last_5m_close") {
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
      const jobId = selectedResearchJobId();
      const evalId = selectedEvaluationId();
      state.researchJobId = jobId;
      state.frozenResearchJob = !!jobId;
      state.researchEvaluation = !!evalId;
      applyJobSourceUi();
      const qs = new URLSearchParams();
      qs.set("limit", state.researchMode ? "120" : "500");
      if (evalId) qs.set("evaluation_id", evalId);
      if (!jobId) {
        const hours = ($("stochFilterHours") || {}).value || "48";
        qs.set("hours", hours);
      }
      if (state.researchMode && !jobId) {
        qs.set("tier_a", "true");
        const variant =
          ($("stochFilterTimingVariant") || {}).value || "WAIT_1M_EXTREME_TURN_CROSS";
        qs.set("timing_variant", variant);
      } else {
        const tierScope = ($("stochFilterTierA") || {}).value || "true";
        qs.set("tier_a", tierScope);
        if (!jobId) {
          const strategy = ($("stochFilterStrategy") || {}).value || "wave_fade_no_be50_v1";
          qs.set("strategy_version", strategy);
          const selected = ($("stochFilterSelected") || {}).value || "";
          if (selected) qs.set("selected", selected);
        }
      }
      const sym = ($("stochFilterSymbol") || {}).value || "";
      if (sym) qs.set("symbol", sym);
      const direction = ($("stochFilterDirection") || {}).value || "";
      if (direction) qs.set("direction", direction);
      const tf = ($("stochFilterTimeframe") || {}).value || "";
      if (tf) qs.set("timeframe", tf);

      const url = jobId
        ? "/api/stoch/frozen-fade-jobs/" + encodeURIComponent(jobId) + "/signals?" + qs.toString()
        : "/api/stoch/signals?" + qs.toString();
      const res = await fetch(url, { credentials: "include" });
      let data = {};
      try {
        data = await res.json();
      } catch (_) {
        data = {};
      }
      const poolOn = isPoolStrategy() && !jobId;
      const emaOn = isEmaFlipStrategy() && !jobId;
      setPoolResearchUi(poolOn && !emaOn, data.banner);
      setEmaFlipResearchUi(emaOn, data.banner);
      if (!res.ok || data.success === false || data.feed_ready === false) {
        state.rows = [];
        state.filtered = [];
        state.summary = null;
        renderSummary(null);
        persistStochChartBridge();
        applyJobSourceUi(data.job);
        setBanner(
          data.message || data.error || `Signal-Feed nicht erreichbar (HTTP ${res.status})`,
          true
        );
        renderTable();
        return;
      }

      const rows = Array.isArray(data.rows)
        ? data.rows
        : Array.isArray(data.signals)
          ? data.signals
          : Array.isArray(data.items)
            ? data.items
            : [];
      state.rows = rows.filter((r) => r && !r.is_demo && r.signal_id);
      state.summary = data.summary || null;
      renderSummary(state.summary);
      if (jobId) {
        const job = data.job || {};
        const start = job.signal_start || "";
        const end = job.signal_end_exclusive || "";
        const evaluated = !!(data.outcomes_computed || evalId);
        setBanner(
          (job.identity_kind === "EZM_CANDIDATE_DISCOVERY" || (job.strategy_id || job.strategy_version || "").indexOf("ema_zone") === 0
            ? "EZM Candidate Discovery"
            : "Frozen Research Job ") +
            (job.strategy_version || job.strategy_id || "wave_fade_frozen_f16ae32") +
            " · Job " +
            jobId +
            (evalId ? " · Evaluation " + evalId : "") +
            (start && end ? " · Fenster " + start + " – " + end : "") +
            (job.identity_kind === "EZM_CANDIDATE_DISCOVERY"
              ? " · Research Candidates · keine Trades / kein PnL"
              : (evaluated
                ? " · Canonical Causal Frozen Tier-A · cross_recognition · NO_BE50 / full 1m / SL_FIRST"
                : " · Outcomes nicht berechnet · Exit-Policy nicht angewendet · Execution-Dedup nicht angewendet · BE50-Outcome nicht aktiv")),
          false
        );
        applyJobSourceUi(job);
        if (Array.isArray(job.selected_symbols) && job.selected_symbols.length) {
          state.symbolOptions = unique(job.selected_symbols);
        }
      } else {
        setBanner("");
      }
      if (!sym) {
        state.symbolOptions = unique((state.symbolOptions.length ? state.symbolOptions : []).concat(state.rows.map((r) => r.symbol)));
      } else if (!state.symbolOptions.length) {
        state.symbolOptions = unique(state.rows.map((r) => r.symbol));
      }
      persistStochChartBridge();
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
      "stochFilterResearchJob",
      "stochFilterResearchEval",
      "stochFilterTierA",
      "stochFilterTimingVariant",
    ].forEach((id) => {
      const el = $(id);
      if (el) {
        el.addEventListener("change", () => {
          state.page = 0;
          if (id === "stochFilterResearchJob") {
            applyJobSourceUi();
            loadResearchEvalCatalog();
          }
          load();
        });
      }
    });
    ["stochFilterOutcome", "stochFilterSlWide", "stochFilterTargets"].forEach((id) => {
      const el = $(id);
      if (el) {
        el.addEventListener("change", () => {
          state.page = 0;
          applyFilters();
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
    persistStochChartBridge();
  }

  function renderTable() {
    const evalHeads = document.querySelectorAll(".stoch-eval-col");
    evalHeads.forEach(function (el) {
      el.hidden = !state.researchEvaluation;
    });
    const tbody = $("stochSignalsBody");
    const meta = $("stochMeta");
    if (!tbody) return;

    const start = state.page * state.pageSize;
    const slice = state.filtered.slice(start, start + state.pageSize);

    if (!slice.length) {
      const emptyMsg = state.researchMode
        ? "Keine Research-1m-Timing-Signale (inkl. pending) im gewählten Zeitraum."
        : state.emaFlipResearch
          ? "EMA-Pool-Trend-Flip-Artefakt nicht verfügbar"
          : state.poolResearch
          ? "Pool-V1-Artefakt nicht verfügbar"
          : state.frozenResearchJob
            ? "Keine Frozen-Research-Job-Signale für den gewählten Filter."
            : "Keine echten Tier-A-Signale im gewählten Zeitraum/Filter.";
      tbody.innerHTML = `<tr><td colspan="18" class="stoch-empty">${emptyMsg}</td></tr>`;
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
    } else if (state.emaFlipResearch) {
      tbody.innerHTML = slice
        .map((r, i) => {
          const idx = start + i;
          const prot = r.protection_pool || r.sl_cluster || {};
          const hold =
            r.hold_minutes == null || Number.isNaN(Number(r.hold_minutes))
              ? "–"
              : `${Number(r.hold_minutes).toFixed(0)}m`;
          const open = String(r.outcome || "").toUpperCase() === "OPEN";
          const noT = r.decision === "NO_TRADE" || r.decision === "BLOCKED";
          return `<tr data-signal-id="${r.signal_id || ""}">
            <td><button type="button" class="stoch-chart-btn" data-idx="${idx}" title="Signal Chart / Inspector">${chartIcon()}</button></td>
            <td>${fmtTs(r.signal_time)}</td>
            <td>${fmtTs(r.entry_time)}</td>
            <td>${r.symbol || "–"}</td>
            <td>${chipTf(r.signal_timeframe || r.timeframe)}</td>
            <td>${chipDirection(r.original_direction)}</td>
            <td>${chipDirection(r.executed_direction || r.trade_direction)}</td>
            <td>${r.decision || "–"}</td>
            <td>${fmtPrice(r.entry_price)}</td>
            <td>${fmtNum(r.ema9, 6)}</td>
            <td>${fmtNum(r.ema20, 6)}</td>
            <td>${fmtNum(r.ema_sep_atr, 3)}</td>
            <td>${r.ema_trend || "–"}</td>
            <td>${r.last_confirmed_cross || "–"}</td>
            <td>5m</td>
            <td>${fmtNum(r.upper_pool_bias_score, 2)} / ${fmtNum(r.lower_pool_bias_score, 2)}</td>
            <td>${prot.cluster_id != null ? "#" + prot.cluster_id : "–"}</td>
            <td>${noT ? "–" : fmtPrice(r.sl_price)}</td>
            <td>${noT ? "–" : fmtNum(r.sl_distance_pct, 2)}</td>
            <td>${r.sl_too_wide ? "true" : "false"}</td>
            <td>${open || noT ? "–" : fmtTs(r.exit_time)}</td>
            <td>${r.exit_reason || "–"}</td>
            <td>${open || noT ? "–" : fmtPnl(r.gross_pnl_pct)}</td>
            <td>${open || noT ? "–" : fmtPnl(r.fees_pct)}</td>
            <td>${open || noT ? "–" : fmtPnl(r.net_pnl_pct)}</td>
            <td>${hold}</td>
            <td>${r.variant || "–"}</td>
          </tr>`;
        })
        .join("");
    } else if (state.poolResearch) {
      tbody.innerHTML = slice
        .map((r, i) => {
          const idx = start + i;
          const noPlan = String(r.plan_status || "") === "NO_PLAN";
          const open = String(r.outcome || r.display_result || "").toUpperCase() === "OPEN";
          const slWide = r.sl_too_wide
            ? '<span class="stoch-badge-sl-wide">SL TOO WIDE</span>'
            : "";
          const hold =
            r.hold_minutes == null || Number.isNaN(Number(r.hold_minutes))
              ? "–"
              : `${Number(r.hold_minutes).toFixed(0)}m`;
          const reason = noPlan ? r.no_plan_reason || "NO_PLAN" : r.initial_target_mode || "–";
          const outcomeChip = open
            ? '<span class="stoch-chip stoch-chip-open">OPEN</span>'
            : chipResult(r.display_result || r.outcome || r.result, r);
          return `<tr data-signal-id="${r.signal_id || ""}">
            <td><button type="button" class="stoch-chart-btn" data-idx="${idx}" title="Signal Chart / Inspector">${chartIcon()}</button></td>
            <td>${fmtTs(r.entry_time || r.candle_close_time)}</td>
            <td>${r.symbol || "–"}</td>
            <td>${chipDirection(r.trade_direction || r.direction)}</td>
            <td>${chipTf(r.signal_timeframe || r.timeframe)}</td>
            <td>${chipTf(r.pool_interval || r.pool_timeframe || "5m")}</td>
            <td>${fmtPrice(r.entry_price)}</td>
            <td>${reason}</td>
            <td>${r.entry_pool_count == null ? "–" : String(r.entry_pool_count)}</td>
            <td>${noPlan ? "–" : fmtPrice(r.sl_price)}${slWide}</td>
            <td>${noPlan ? "–" : fmtNum(r.sl_distance_pct, 2)}</td>
            <td>${noPlan ? "–" : fmtPrice(r.tp1_price)}</td>
            <td>${noPlan || r.tp1_size == null ? "–" : Math.round(Number(r.tp1_size) * 100) + "%"}</td>
            <td>${noPlan ? "–" : fmtPrice(r.tp2_price)}</td>
            <td>${noPlan || r.tp2_size == null ? "–" : Math.round(Number(r.tp2_size) * 100) + "%"}</td>
            <td>${outcomeChip}</td>
            <td>${open || noPlan ? "–" : fmtPnl(r.gross_pnl_pct)}</td>
            <td>${open || noPlan ? "–" : fmtPnl(r.fees_pct)}</td>
            <td>${open || noPlan ? "–" : fmtPnl(r.net_pnl_pct)}</td>
            <td>${hold}</td>
            <td>${fmtTs(r.last_5m_open)}</td>
            <td>${fmtTs(r.last_5m_close)}</td>
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
            <td>${r.plan_status ? (fmtPrice(r.tp1_price) + (r.tp1_size != null ? " (" + Math.round(Number(r.tp1_size) * 100) + "%)" : "") + (r.tp2_price != null ? " / " + fmtPrice(r.tp2_price) : "")) : fmtPrice(r.tp_price)}</td>
            <td>${fmtPrice(r.sl_price)}${r.sl_too_wide ? " wide" : ""}</td>
            <td>${fmtNum(r.stoch_k, 2)}</td>
            <td>${fmtTs(r.candle_close_time || r.signal_time)}</td>
            <td>${chipResult(r.display_result || r.result, r)}</td>
            <td>${fmtPnl(r.net_pnl_pct != null ? r.net_pnl_pct : r.pnl_pct)}</td>
            <td>${fmtDuration(r.duration_seconds, r.display_result || r.result)}</td>
            ${state.researchEvaluation ? `<td>${r.exit_reason || "–"}</td><td>${r.be50_activated ? "ja" : "nein"}</td><td>${fmtTs(r.exit_time)}</td>` : ""}
          </tr>`;
        })
        .join("");
    }

    if (meta) {
      meta.textContent = `${state.filtered.length} Signale · Seite ${state.page + 1}/${Math.max(1, Math.ceil(state.filtered.length / state.pageSize) || 1)}` +
        (state.frozenResearchJob ? " · Job-Artefakt (kein Poll)" : ` · auto ${state.refreshMs / 1000}s`);
      const sv = (($("stochFilterStrategy") || {}).value || "");
      if (sv === "POOL_ORDER_PLAN_V1") {
        meta.textContent += " · RESEARCH/BACKTEST Pool Order Plan";
      }
      if (sv === "EMA_POOL_TREND_FLIP_V1") {
        meta.textContent += " · RESEARCH/BACKTEST EMA Pool Trend Flip";
      }
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
          tp_price: row.ezm_research ? null : row.tp_price,
          sl_price: row.ezm_research ? null : row.sl_price,
          signal_time: row.decision_at || row.candle_close_time || row.signal_time || row.entry_time,
          expected_open_time: row.decision_at || row.candle_close_time || row.signal_time || row.entry_time,
          expected_open_price: entry,
          open_price: entry,
          expected_tp: row.ezm_research || row.plan_status === "NO_PLAN" || row.plan_status === "RESEARCH_CANDIDATE_NO_TRADE"
            ? null
            : row.tp1_price || row.tp_price,
          expected_sl: row.ezm_research || row.plan_status === "NO_PLAN" || row.plan_status === "RESEARCH_CANDIDATE_NO_TRADE"
            ? null
            : row.sl_price,
          generated_at: row.generated_at,
          result: row.outcomes_computed === false ? null : (row.display_result || row.result),
          frozen_result: row.outcomes_computed === false ? null : (row.frozen_result || row.result),
          display_result: row.outcomes_computed === false ? "nicht berechnet" : (row.display_result || row.result),
          outcomes_computed: row.outcomes_computed,
          source: row.source,
          job_id: row.job_id,
          ezm_research: !!row.ezm_research,
          candidate_state: row.candidate_state,
          decision_at: row.decision_at,
          decision_price: row.decision_price,
          research_note: row.research_note,
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
          pool_research: !!row.pool_research,
          ema_flip_research: !!row.ema_flip_research,
          original_direction: row.original_direction,
          executed_direction: row.executed_direction,
          decision: row.decision,
          ema9: row.ema9,
          ema20: row.ema20,
          ema_sep_atr: row.ema_sep_atr,
          ema_trend: row.ema_trend,
          last_confirmed_cross: row.last_confirmed_cross,
          upper_pool_bias_score: row.upper_pool_bias_score,
          lower_pool_bias_score: row.lower_pool_bias_score,
          protection_pool: row.protection_pool,
          ratchet_steps: row.ratchet_steps || [],
          active_upper_pools: row.active_upper_pools || [],
          active_lower_pools: row.active_lower_pools || [],
          weak_cross_candidates: row.weak_cross_candidates || [],
          confirmed_cross_events: row.confirmed_cross_events || [],
          variant: row.variant,
          flipped: row.flipped,
          aligned: row.aligned,
          gross_pnl_pct: row.gross_pnl_pct,
          fees_pct: row.fees_pct,
          net_pnl_pct: row.net_pnl_pct,
          tp1_price: row.tp1_price,
          tp1_size: row.tp1_size,
          tp2_price: row.tp2_price,
          tp2_size: row.tp2_size,
          legs: row.legs || [],
          sl_cluster: row.sl_cluster,
          tp1_cluster: row.tp1_cluster,
          tp2_cluster: row.tp2_cluster,
          snapshot_as_of: row.snapshot_as_of,
          last_5m_open: row.last_5m_open,
          last_5m_close: row.last_5m_close,
          signal_timeframe: row.signal_timeframe || row.timeframe,
          pool_timeframe: row.pool_timeframe || row.pool_interval || "5m",
          entry_pool_count: row.entry_pool_count,
          sl_too_wide: row.sl_too_wide,
          sl_distance_pct: row.sl_distance_pct,
          plan_status: row.plan_status,
          outcome: row.outcome,
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
    state.refreshTimer = setInterval(() => {
      if (isPoolStrategy() || isEmaFlipStrategy() || selectedResearchJobId()) return;
      load({ preservePage: true });
    }, state.refreshMs);
  }

  async function loadResearchJobCatalog() {
    const sel = $("stochFilterResearchJob");
    if (!sel) return;
    const wanted = sel.value || storedResearchJobId();
    try {
      const res = await fetch("/api/stoch/frozen-fade-jobs", { credentials: "include" });
      const data = await res.json().catch(function () { return {}; });
      const jobs = Array.isArray(data.jobs) ? data.jobs : [];
      sel.innerHTML = "";
      const live = document.createElement("option");
      live.value = "";
      live.textContent = "Live-Baseline (Collector)";
      sel.appendChild(live);
      jobs.forEach(function (job) {
        const opt = document.createElement("option");
        opt.value = job.job_id;
        opt.setAttribute("data-start", job.signal_start || "");
        opt.setAttribute("data-end", job.signal_end_exclusive || "");
        const when = job.finished_at || job.created_at || "";
        const coins = (job.selected_symbols || []).join(", ");
        opt.textContent =
          (when ? when.slice(0, 16) + "Z · " : "") +
          coins +
          " · Raw " +
          (job.raw_total == null ? "–" : job.raw_total) +
          " / Tier-A " +
          (job.tier_a_total == null ? "–" : job.tier_a_total) +
          " · " +
          (job.state || "");
        sel.appendChild(opt);
      });
      if (wanted && Array.prototype.some.call(sel.options, function (o) { return o.value === wanted; })) {
        sel.value = wanted;
      } else if (wanted) {
        const missing = document.createElement("option");
        missing.value = wanted;
        missing.textContent = "Job " + wanted + " (nicht im Katalog)";
        sel.appendChild(missing);
        sel.value = wanted;
      } else {
        sel.value = "";
      }
      applyJobSourceUi();
      loadResearchEvalCatalog();
    } catch (_err) {
      applyJobSourceUi();
    }
  }

  function storedEvaluationId() {
    try {
      const evid = localStorage.getItem("stoch.research_evaluation_id") || "";
      if (/^[0-9a-f]{32}$/.test(evid)) return evid;
    } catch (_) {}
    return "";
  }

  async function loadResearchEvalCatalog() {
    const sel = $("stochFilterResearchEval");
    if (!sel) return;
    const jobId = selectedResearchJobId();
    const wanted = sel.value || storedEvaluationId();
    sel.innerHTML = "";
    const none = document.createElement("option");
    none.value = "";
    none.textContent = "Keine Evaluation (nur Plan)";
    sel.appendChild(none);
    if (!jobId) {
      sel.value = "";
      applyJobSourceUi();
      return;
    }
    try {
      const res = await fetch(
        "/api/stoch/frozen-fade-evaluations?source_job_id=" + encodeURIComponent(jobId),
        { credentials: "include" }
      );
      const data = await res.json().catch(function () { return {}; });
      const evals = Array.isArray(data.evaluations) ? data.evaluations : [];
      evals.forEach(function (ev) {
        const opt = document.createElement("option");
        opt.value = ev.evaluation_id;
        const when = ev.finished_at || ev.created_at || "";
        opt.textContent =
          (when ? when.slice(0, 16) + "Z · " : "") +
          (ev.state || "") +
          " · WR " +
          (ev.win_rate_pct == null ? "–" : Number(ev.win_rate_pct).toFixed(1) + "%");
        sel.appendChild(opt);
      });
      if (wanted && Array.prototype.some.call(sel.options, function (o) { return o.value === wanted; })) {
        sel.value = wanted;
      } else {
        sel.value = "";
      }
      applyJobSourceUi();
    } catch (_err) {
      applyJobSourceUi();
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    wire();
    loadResearchJobCatalog().then(function () {
      return loadResearchEvalCatalog();
    }).then(function () { load(); });
    startAutoRefresh();
    wireCollector();
    wireUniverse51();
    wireDataCollectorHealth();
  });

  /* ------------------------------------------------------------------ */
  /* Collector Steuerung / Status (proxied via dashboard backend)      */
  /* ------------------------------------------------------------------ */

  const collectorUi = {
    status: null,
    feeds: null,
    services: null,
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

  function feedChip(active, labelOn, labelOff, title) {
    const on = !!active;
    const label = on ? (labelOn || "ON") : (labelOff || "OFF");
    const cls = on ? "stoch-chip-live" : "stoch-chip-err";
    const tip = title ? ` title="${String(title).replace(/"/g, "&quot;")}"` : "";
    return `<span class="stoch-chip ${cls}"${tip}>${label}</span>`;
  }

  function feedChipState(info) {
    const label = info.label || (info.active ? "ON" : "OFF");
    const cls =
      label === "REC" || label === "WARN"
        ? "stoch-chip-warn"
        : info.active
          ? "stoch-chip-live"
          : "stoch-chip-err";
    const tip = info.title ? ` title="${String(info.title).replace(/"/g, "&quot;")}"` : "";
    return `<span class="stoch-chip ${cls}"${tip}>${label}</span>`;
  }

  function symbolSet(list) {
    const out = new Set();
    (Array.isArray(list) ? list : []).forEach(function (s) {
      const v = String(s || "").trim().toUpperCase();
      if (v) out.add(v);
    });
    return out;
  }

  function candleFeedInfo(row, candleSet) {
    const sym = String(row.symbol || "").toUpperCase();
    if (!candleSet.has(sym)) {
      return { active: false, label: "OFF", title: "nicht im Candle-Universe" };
    }
    const st = String(row.state || "").toUpperCase();
    if (st === "LIVE") return { active: true, label: "ON", title: "1m Candles live" };
    if (
      st === "RECOVERING" ||
      st === "CONNECTING" ||
      st === "SUBSCRIBING" ||
      st === "STARTING" ||
      st === "RECONNECTING"
    ) {
      return { active: true, label: "REC", title: "Candle-Recovery / Connect" };
    }
    return { active: false, label: "OFF", title: "Candle konfiguriert, State=" + (st || "–") };
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
    const feeds = collectorUi.feeds || {};
    const obFeed = feeds.orderbook_live || {};
    const oiFeed = feeds.oi_liquidation || {};
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

    const candleSet = symbolSet(st.candle_symbols || st.configured_symbols);
    const publicEnabled = !!st.public_trades_enabled;
    const publicSet = publicEnabled ? symbolSet(st.public_trade_symbols) : new Set();
    const signalSet = symbolSet(st.signal_symbols);
    const obSet = obFeed.running ? symbolSet(obFeed.symbols) : new Set();
    const oiSet = oiFeed.running ? symbolSet(oiFeed.symbols) : new Set();

    const live = countList(st.live_symbols) || Number(st.live_count) || 0;
    const recovering = countList(st.recovering_symbols) || Number(st.symbols_recovering) || 0;
    const stale = countList(st.stale_symbols) || 0;
    const ws = st.websocket_connected ? "Connected" : "Disconnected";
    const ptMetrics = st.public_trade_metrics || {};

    const summary = $("collectorStatusSummary");
    if (summary) {
      summary.innerHTML = [
        `<span>WebSocket: <strong class="${st.websocket_connected ? "stoch-pnl-pos" : "stoch-pnl-neg"}">${ws}</strong></span>`,
        `<span>Candles: <strong>${live} live / ${candleSet.size}</strong>${recovering ? " · recovering " + recovering : ""}${stale ? " · stale " + stale : ""}</span>`,
        `<span>Public: <strong class="${publicEnabled && publicSet.size ? "stoch-pnl-pos" : "stoch-pnl-neg"}">${
          publicEnabled ? publicSet.size + " symbols" : "OFF"
        }</strong></span>`,
        `<span>Signals: <strong>${signalSet.size || 0}</strong></span>`,
        `<span>OB Live: <strong class="${obFeed.running ? "stoch-pnl-pos" : "stoch-pnl-neg"}">${
          obFeed.running ? "ON (" + (obFeed.symbol_count || obSet.size) + ")" : "OFF"
        }</strong></span>`,
        `<span>OI/Liq: <strong class="${oiFeed.running ? "stoch-pnl-pos" : "stoch-pnl-neg"}">${
          oiFeed.running ? "ON (" + (oiFeed.symbol_count || oiSet.size) + ")" : "OFF"
        }</strong></span>`,
      ].join("");
    }

    const meta = $("collectorStatusMeta");
    if (meta) {
      meta.innerHTML = [
        `<span>last_message_at: ${fmtCollectorTs(st.last_message_at)}</span>`,
        `<span>started_at: ${fmtCollectorTs(st.started_at)}</span>`,
        `<span>reconnects: ${st.reconnect_count ?? 0}</span>`,
        `<span>public_rows: ${ptMetrics.rows_inserted == null ? "–" : ptMetrics.rows_inserted}</span>`,
        `<span>OB pid: ${obFeed.pid == null ? "–" : obFeed.pid}</span>`,
        `<span>OI pid: ${oiFeed.pid == null ? "–" : oiFeed.pid}</span>`,
      ].join("");
    }

    const tbody = $("collectorSymbolsBody");
    if (tbody) {
      const symbols = Array.isArray(st.symbols) ? st.symbols : [];
      if (!symbols.length) {
        tbody.innerHTML = `<tr><td colspan="8" class="stoch-empty">Keine Symbol-Daten im Status.</td></tr>`;
      } else {
        tbody.innerHTML = symbols
          .map(function (row) {
            const sym = String(row.symbol || "").toUpperCase();
            const rowState = row.state || "–";
            const lastCandle = row.last_closed_candle_at || row.last_persisted_open_time || null;
            const candleInfo = candleFeedInfo(row, candleSet);
            const publicOn = publicSet.has(sym);
            const signalOn = signalSet.has(sym);
            const obOn = obSet.has(sym);
            const oiOn = oiSet.has(sym);
            return `<tr>
              <td>${row.symbol || "–"}</td>
              <td><span class="stoch-chip ${healthChipClass(rowState)}">${rowState}</span></td>
              <td>${feedChipState(candleInfo)}</td>
              <td>${feedChip(
                publicOn,
                "ON",
                "OFF",
                publicEnabled
                  ? publicOn
                    ? "public trades subscribed"
                    : "nicht in public_trade_symbols"
                  : "public trades disabled on collector"
              )}</td>
              <td>${feedChip(signalOn, "ON", "OFF", signalOn ? "signal worker demand" : "kein Signal-Demand")}</td>
              <td>${feedChip(
                obOn,
                "ON",
                "OFF",
                obFeed.running
                  ? obOn
                    ? "OB V3 live universe"
                    : "nicht im OB-Live-Universe"
                  : "OB V3 Live Collector nicht aktiv"
              )}</td>
              <td>${feedChip(
                oiOn,
                "ON",
                "OFF",
                oiFeed.running
                  ? oiOn
                    ? "OI + Liquidationen live"
                    : "nicht im OI/Liq-Universe"
                  : "OI/Liq Collector nicht aktiv"
              )}</td>
              <td>${fmtCollectorTs(lastCandle)}</td>
            </tr>`;
          })
          .join("");
      }
    }

    updateCollectorButtons();
  }

  async function loadCollectorStatus() {
    try {
      const [res, feedsRes, svcRes] = await Promise.all([
        fetch("/api/collector/status", { credentials: "include" }),
        fetch("/api/collector/live-feeds", { credentials: "include" }),
        fetch("/api/collector/services", { credentials: "include" }),
      ]);
      const data = await res.json().catch(() => ({}));
      const feeds = await feedsRes.json().catch(() => ({}));
      const services = await svcRes.json().catch(() => ({}));
      if (feedsRes.ok) {
        collectorUi.feeds = feeds;
      }
      if (svcRes.ok) {
        collectorUi.services = services;
        renderCollectorServices(services);
      }
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

  function renderCollectorServices(services) {
    const body = $("collectorServicesBody");
    if (!body) return;
    const stoch = (services && services.stoch) || {};
    const pub = (services && services.public) || {};
    const ob = (services && services.orderbook_live) || {};
    const oi = (services && services.oi_liquidation) || {};
    const gap = (pub.gap_backfill || {});
    setChip(
      "collectorPublicGapChip",
      gap.running ? "RUNNING" : "IDLE"
    );

    function row(name, statusHtml, backfill, actionsHtml) {
      return (
        "<tr><td>" +
        name +
        "</td><td>" +
        statusHtml +
        "</td><td>" +
        backfill +
        "</td><td>" +
        actionsHtml +
        "</td></tr>"
      );
    }

    const stochState = stoch.collector_state || (stoch.running ? "RUNNING" : "STOPPED");
    const stochStatus =
      '<span class="stoch-chip ' +
      healthChipClass(stochState) +
      '">' +
      stochState +
      "</span>" +
      (stoch.pid ? " · pid " + stoch.pid : "");
    const pubStatus = feedChip(
      !!pub.enabled,
      "ON (" + (pub.symbol_count || 0) + ")",
      "OFF",
      pub.backfill_on_enable || ""
    );
    const obStatus = feedChip(
      !!ob.running,
      "ON (" + (ob.symbol_count || 0) + ")",
      "OFF",
      "status only"
    );
    const oiStatus = feedChip(
      !!oi.running,
      "ON (" + (oi.symbol_count || 0) + ")",
      "OFF",
      "status only"
    );

    const busy = collectorUi.busy ? " disabled" : "";
    const pubBtn = pub.enabled
      ? '<button type="button" class="stoch-btn" data-svc="public" data-act="stop"' +
        busy +
        ">Public stoppen</button>"
      : '<button type="button" class="stoch-btn" data-svc="public" data-act="start"' +
        busy +
        ">Public starten + Gap-Backfill</button>";
    const stochBtns =
      '<button type="button" class="stoch-btn" data-svc="stoch" data-act="restart"' +
      busy +
      ">Neu starten (Recovery)</button>";
    const obBtn = ob.running
      ? '<button type="button" class="stoch-btn" data-svc="orderbook_live" data-act="stop"' +
        busy +
        ">OB stoppen</button> " +
        '<button type="button" class="stoch-btn" data-svc="orderbook_live" data-act="restart"' +
        busy +
        ">OB neu starten</button>"
      : '<button type="button" class="stoch-btn" data-svc="orderbook_live" data-act="start"' +
        busy +
        ">OB starten</button>";
    const oiBtn = oi.running
      ? '<button type="button" class="stoch-btn" data-svc="oi_liquidation" data-act="stop"' +
        busy +
        ">OI/Liq stoppen</button> " +
        '<button type="button" class="stoch-btn" data-svc="oi_liquidation" data-act="restart"' +
        busy +
        ">OI/Liq neu starten</button>"
      : '<button type="button" class="stoch-btn" data-svc="oi_liquidation" data-act="start"' +
        busy +
        ">OI/Liq starten</button>";

    body.innerHTML = [
      row(
        "Candles + Signals",
        stochStatus,
        "Auto-RECOVERING bei Start",
        stochBtns
      ),
      row(
        "Public Trades",
        pubStatus +
          (gap.running
            ? ' · <span class="stoch-chip stoch-chip-warn">Gap-Backfill läuft</span>'
            : ""),
        pub.backfill_on_enable || "Live ab Connect + Archiv-Gap",
        pubBtn
      ),
      row(
        "OB V3 Live",
        obStatus + (ob.pid ? " · pid " + ob.pid : ""),
        ob.backfill_on_start || "Recovery on connect (kein 30d)",
        obBtn
      ),
      row(
        "OI/Liq Live",
        oiStatus + (oi.pid ? " · pid " + oi.pid : ""),
        oi.backfill_on_start || "Live ab Connect",
        oiBtn
      ),
    ].join("");

    body.querySelectorAll("button[data-svc]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        runServiceAction(btn.getAttribute("data-svc"), btn.getAttribute("data-act"));
      });
    });
  }

  async function runServiceAction(serviceId, action) {
    const messages = {
      "stoch:restart":
        "Stoch-Collector neu starten?\nCandles: Auto-Recovery (Lücken füllen).\nPublic-Flag bleibt wie bisher.",
      "stoch:stop":
        "Stoch-Collector wirklich stoppen?\nCandles/Signals/Public (falls an) stoppen.",
      "stoch:start": "Stoch-Collector starten (Candles mit Auto-Recovery)?",
      "public:start":
        "Public Trades aktivieren?\n1) Stoch-Neustart mit --enable-public-trades\n2) Candle Auto-Recovery\n3) Archiv-Gap-Backfill letzte Tage\nOB/OI/Import werden nicht angefasst.",
      "public:stop":
        "Public Trades deaktivieren?\nStoch-Neustart ohne Public-Flag; Candles bleiben mit Recovery an.",
      "orderbook_live:start":
        "OB V3 Live starten (universe51)?\nCollector-eigene Recovery beim Connect.\nKein 30d-Import.",
      "orderbook_live:stop":
        "OB V3 Live stoppen?\nNur der Live-Collector — 30d-Import bleibt unberührt.",
      "orderbook_live:restart":
        "OB V3 Live neu starten?\nKurze Unterbrechung, dann Recovery on connect.\nKein 30d-Import.",
      "oi_liquidation:start":
        "OI/Liq Live starten (51 Symbole)?\nLive ab Connect.",
      "oi_liquidation:stop":
        "OI/Liq Live stoppen?\nNur dieser Collector.",
      "oi_liquidation:restart":
        "OI/Liq Live neu starten?",
    };
    const key = String(serviceId) + ":" + String(action);
    const msg = messages[key] || "Aktion ausführen: " + key + "?";
    if (!window.confirm(msg)) return;

    collectorUi.busy = true;
    updateCollectorButtons();
    setErr("collectorControlError", "");
    try {
      const res = await fetch(
        "/api/collector/services/" +
          encodeURIComponent(serviceId) +
          "/" +
          encodeURIComponent(action),
        {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ confirm: true }),
        }
      );
      const data = await res.json().catch(() => ({}));
      if (!res.ok || data.ok === false) {
        throw new Error(data.error || data.detail || data.detail || `HTTP ${res.status}`);
      }
      await loadCollectorStatus();
    } catch (err) {
      setErr("collectorControlError", "Steuerung fehlgeschlagen: " + err);
    } finally {
      collectorUi.busy = false;
      updateCollectorButtons();
      renderCollectorServices(collectorUi.services || {});
    }
  }

  async function setDesiredState(desired) {
    if (desired === "STOPPED") {
      await runServiceAction("stoch", "stop");
      return;
    }
    await runServiceAction("stoch", "start");
  }

  function wireCollector() {
    const startBtn = $("collectorStartBtn");
    const stopBtn = $("collectorStopBtn");
    if (startBtn) {
      startBtn.addEventListener("click", function () {
        runServiceAction("stoch", "start");
      });
    }
    if (stopBtn) {
      stopBtn.addEventListener("click", function () {
        runServiceAction("stoch", "stop");
      });
    }
    loadCollectorStatus();
    if (collectorUi.timer) clearInterval(collectorUi.timer);
    collectorUi.timer = setInterval(loadCollectorStatus, collectorUi.pollMs);
  }

  /* ------------------------------------------------------------------ */
  /* Historisches 51-Coin-Universe (read-only coverage, no jobs)        */
  /* ------------------------------------------------------------------ */

  const universe51 = {
    coins: [],
    selected: {},
    job: null,
    pollTimer: null,
    pollMs: 3000,
    coverageReloadedForJob: null,
  };

  function universe51FreshnessChip(status) {
    const v = String(status || "").toUpperCase();
    if (v === "CURRENT") return "stoch-chip stoch-chip-ok";
    if (v === "UPDATE_AVAILABLE") return "stoch-chip stoch-chip-update";
    return "stoch-chip stoch-chip-nodata";
  }

  function universe51UpdateAvailableCount() {
    return universe51.coins.filter(function (c) {
      return c.freshness_status === "UPDATE_AVAILABLE";
    }).length;
  }

  function universe51StatusChip(status) {
    const v = String(status || "").toUpperCase();
    if (v === "FULL") return "stoch-chip stoch-chip-ok";
    if (v === "LISTING_LIMITED") return "stoch-chip stoch-chip-listing";
    if (v === "INCOMPLETE") return "stoch-chip stoch-chip-incomplete";
    return "stoch-chip stoch-chip-nodata";
  }

  function universe51SelectedSymbols() {
    return universe51.coins
      .filter(function (c) {
        return c.testable && universe51.selected[c.symbol];
      })
      .map(function (c) {
        return c.symbol;
      });
  }

  function universe51SelectedCount() {
    return universe51SelectedSymbols().length;
  }

  function universe51TestableCount() {
    return universe51.coins.filter(function (c) {
      return c.testable;
    }).length;
  }

  function universe51JobActive() {
    const state = universe51.job && universe51.job.state;
    return state === "QUEUED" || state === "RUNNING";
  }

  function universe51CoinJobState(symbol) {
    const coins = (universe51.job && universe51.job.coins) || [];
    for (let i = 0; i < coins.length; i++) {
      if (coins[i].symbol === symbol) return coins[i].state;
    }
    return "";
  }

  function syncUniverse51SelectAll() {
    const master = $("universe51SelectAll");
    if (!master) return;
    const testable = universe51TestableCount();
    const selected = universe51SelectedCount();
    master.indeterminate = selected > 0 && selected < testable;
    master.checked = testable > 0 && selected === testable;
  }

  function universe51ActionButton(c) {
    const jobActive = universe51JobActive() || (typeof frozenFadeJobActive === "function" && frozenFadeJobActive());
    const coinState = universe51CoinJobState(c.symbol);
    if (coinState === "UPDATING") {
      return "<button type=\"button\" class=\"stoch-btn universe51-update-one\" data-symbol=\"" +
        c.symbol +
        "\" disabled>Aktualisieren</button>";
    }
    if (c.freshness_status === "CURRENT") {
      return "<button type=\"button\" class=\"stoch-btn universe51-update-one\" data-symbol=\"" +
        c.symbol +
        "\" disabled>Aktuell</button>";
    }
    const disabled = jobActive ? "disabled" : "";
    return "<button type=\"button\" class=\"stoch-btn universe51-update-one\" data-symbol=\"" +
      c.symbol +
      "\" " +
      disabled +
      ">Aktualisieren</button>";
  }

  function renderUniverse51Progress() {
    const el = $("universe51Progress");
    if (!el) return;
    if (!universe51JobActive() && !(universe51.job && universe51.job.state)) {
      el.style.display = "none";
      el.textContent = "";
      return;
    }
    const job = universe51.job || {};
    if (!job.state) {
      el.style.display = "none";
      return;
    }
    el.style.display = "block";
    const n = job.completed_symbols == null ? 0 : job.completed_symbols;
    const total = job.total_symbols == null ? 0 : job.total_symbols;
    const current = job.current_symbol || "–";
    const ok = job.success_count == null ? 0 : job.success_count;
    const failed = job.failed_count == null ? 0 : job.failed_count;
    el.textContent =
      (universe51JobActive() ? "Datenaktualisierung läuft · " : "") +
      "Coin " +
      n +
      " von " +
      total +
      " · " +
      current +
      " · " +
      ok +
      " erfolgreich · " +
      failed +
      " fehlgeschlagen";
  }

  function renderUniverse51Summary() {
    const summary = $("universe51Summary");
    if (summary) {
      summary.textContent =
        universe51.coins.length +
        " Coins vorhanden · " +
        universe51TestableCount() +
        " testbar · " +
        universe51SelectedCount() +
        " ausgewählt · " +
        universe51UpdateAvailableCount() +
        " Update verfügbar";
    }
    syncUniverse51SelectAll();
    const batch = $("universe51UpdateSelected");
    if (batch) {
      const n = universe51SelectedCount();
      const all = universe51.coins.length === 51 && n === 51;
      batch.textContent = all
        ? "51 ausgewählte Coins aktualisieren"
        : "Ausgewählte aktualisieren";
      batch.disabled = n === 0 || universe51JobActive() || (typeof frozenFadeJobActive === "function" && frozenFadeJobActive());
    }
    renderUniverse51Progress();
    if (typeof frozenFadeSyncControls === "function") {
      frozenFadeSyncControls();
    }
  }

  function renderUniverse51Table() {
    const tbody = $("universe51Body");
    if (!tbody) return;
    if (!universe51.coins.length) {
      tbody.innerHTML = '<tr><td colspan="13" class="stoch-empty">Keine Universe-Daten</td></tr>';
      return;
    }
    tbody.innerHTML = universe51.coins
      .map(function (c) {
        const disabled = c.testable ? "" : "disabled";
        const checked = c.testable && universe51.selected[c.symbol] ? "checked" : "";
        const rowClass = c.testable ? "" : "universe51-row-disabled";
        return (
          "<tr class=\"" +
          rowClass +
          "\">" +
          "<td><input type=\"checkbox\" class=\"universe51-coin\" data-symbol=\"" +
          c.symbol +
          "\" " +
          disabled +
          " " +
          checked +
          " /></td>" +
          "<td>" +
          c.symbol +
          "</td>" +
          "<td>" +
          (c.data_from || "–") +
          "</td>" +
          "<td>" +
          (c.data_to || "–") +
          "</td>" +
          "<td>" +
          (c.days_available == null ? "–" : c.days_available) +
          "</td>" +
          "<td>" +
          (c.candle_count == null ? "–" : c.candle_count) +
          "</td>" +
          "<td>" +
          (c.expected_count == null ? "–" : c.expected_count) +
          "</td>" +
          "<td>" +
          (c.missing_count == null ? "–" : c.missing_count) +
          "</td>" +
          "<td><span class=\"" +
          universe51StatusChip(c.coverage_status) +
          "\">" +
          (c.coverage_status || "NO_DATA") +
          "</span></td>" +
          "<td><span class=\"" +
          universe51FreshnessChip(c.freshness_status) +
          "\">" +
          (c.freshness_status || "NO_DATA") +
          "</span></td>" +
          "<td>" +
          (c.lag_minutes == null ? "–" : c.lag_minutes) +
          "</td>" +
          "<td>" +
          (c.update_from || "–") +
          "</td>" +
          "<td>" +
          universe51ActionButton(c) +
          "</td>" +
          "</tr>"
        );
      })
      .join("");
    tbody.querySelectorAll(".universe51-coin").forEach(function (box) {
      box.addEventListener("change", function () {
        const sym = box.getAttribute("data-symbol");
        universe51.selected[sym] = !!box.checked;
        renderUniverse51Summary();
      });
    });
    tbody.querySelectorAll(".universe51-update-one").forEach(function (btn) {
      btn.addEventListener("click", function () {
        const sym = btn.getAttribute("data-symbol");
        startUniverse51Update([sym], "1 ausgewählten Coin bis zur letzten geschlossenen Minute aktualisieren?");
      });
    });
    renderUniverse51Summary();
  }

  function applyUniverse51SelectAll(on) {
    universe51.coins.forEach(function (c) {
      if (!c.testable) return;
      universe51.selected[c.symbol] = !!on;
    });
    renderUniverse51Table();
  }

  function stopUniverse51JobPoll() {
    if (universe51.pollTimer) {
      clearInterval(universe51.pollTimer);
      universe51.pollTimer = null;
    }
  }

  function startUniverse51JobPoll() {
    if (universe51.pollTimer) return;
    universe51.pollTimer = setInterval(loadUniverse51JobStatus, universe51.pollMs);
  }

  async function loadUniverse51Coverage(preserveSelection) {
    const errEl = "universe51Error";
    setErr(errEl, "");
    const prev = preserveSelection ? Object.assign({}, universe51.selected) : null;
    try {
      const res = await fetch("/api/stoch/universe-51-coverage", { credentials: "include" });
      const data = await res.json().catch(function () {
        return {};
      });
      if (!res.ok || data.success === false) {
        setErr(errEl, data.error || data.message || "Coverage nicht verfügbar");
      }
      universe51.coins = Array.isArray(data.coins) ? data.coins : [];
      universe51.selected = {};
      universe51.coins.forEach(function (c) {
        if (c.testable) {
          universe51.selected[c.symbol] = prev ? !!prev[c.symbol] : true;
        }
      });
      const meta = $("universe51Meta");
      if (meta) {
        const ch = data.clickhouse || {};
        meta.textContent =
          "requested_from " +
          (data.requested_from || "–") +
          " · as_of " +
          (data.as_of || "–") +
          " · freshness_reference " +
          (data.freshness_reference || "–") +
          " · grace " +
          (data.freshness_grace_minutes == null ? "10" : data.freshness_grace_minutes) +
          " min" +
          " · " +
          (ch.database || "") +
          "." +
          (ch.table || "") +
          " FINAL";
      }
      renderUniverse51Table();
    } catch (err) {
      setErr(errEl, "Coverage-API nicht erreichbar: " + err);
    }
  }

  async function loadUniverse51JobStatus() {
    try {
      const res = await fetch("/api/stoch/universe-51-update/status", { credentials: "include" });
      const data = await res.json().catch(function () {
        return {};
      });
      universe51.job = data && data.job_id ? data : null;
      const active = universe51JobActive();
      if (active) {
        startUniverse51JobPoll();
      } else {
        stopUniverse51JobPoll();
        if (data && data.job_id && data.state && data.state !== "QUEUED" && data.state !== "RUNNING") {
          if (universe51.coverageReloadedForJob !== data.job_id) {
            universe51.coverageReloadedForJob = data.job_id;
            loadUniverse51Coverage(true);
          }
        }
      }
      renderUniverse51Table();
    } catch (_err) {
      /* keep coverage table usable */
    }
  }

  async function startUniverse51Update(symbols, confirmText) {
    if (!symbols.length || universe51JobActive() || (typeof frozenFadeJobActive === "function" && frozenFadeJobActive())) return;
    const n = symbols.length;
    let text = confirmText;
    if (!text) {
      if (n === 51) {
        text = "51 ausgewählte Coins aktualisieren?\nDieser Vorgang kann einige Minuten dauern.";
      } else {
        text = n + " ausgewählte Coins bis zur letzten geschlossenen Minute aktualisieren?";
      }
    }
    if (!window.confirm(text)) return;
    const errEl = "universe51Error";
    setErr(errEl, "");
    try {
      const res = await fetch("/api/stoch/universe-51-update", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbols: symbols }),
      });
      const data = await res.json().catch(function () {
        return {};
      });
      if (res.status === 409) {
        setErr(errEl, "Ein Update-Job läuft bereits.");
        loadUniverse51JobStatus();
        return;
      }
      if (!res.ok || data.success === false) {
        setErr(errEl, data.error || "Update konnte nicht gestartet werden");
        return;
      }
      universe51.job = data;
      universe51.coverageReloadedForJob = null;
      startUniverse51JobPoll();
      loadUniverse51JobStatus();
    } catch (err) {
      setErr(errEl, "Update-API nicht erreichbar");
    }
  }

  function wireUniverse51() {
    const master = $("universe51SelectAll");
    if (master) {
      master.addEventListener("change", function () {
        applyUniverse51SelectAll(master.checked);
      });
    }
    const batch = $("universe51UpdateSelected");
    if (batch) {
      batch.addEventListener("click", function () {
        const symbols = universe51SelectedSymbols();
        startUniverse51Update(symbols);
      });
    }
    loadUniverse51Coverage();
    loadUniverse51JobStatus();
    wireFrozenFade();
  }

  const frozenFade = {
    job: null,
    pollTimer: null,
    pollMs: 3000,
    windowLocked: false,
    defaultsApplied: false,
    resultShownJobId: null,
  };

  function frozenFadeJobActive() {
    const state = frozenFade.job && frozenFade.job.state;
    return state === "QUEUED" || state === "RUNNING";
  }

  function toDatetimeLocalUtc(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "";
    const p = function (n) { return String(n).padStart(2, "0"); };
    return d.getUTCFullYear() + "-" + p(d.getUTCMonth() + 1) + "-" + p(d.getUTCDate()) +
      "T" + p(d.getUTCHours()) + ":" + p(d.getUTCMinutes());
  }

  function fromDatetimeLocalUtc(value) {
    const v = String(value || "").trim();
    if (!v) return "";
    return v.length === 16 ? v + ":00Z" : v + "Z";
  }

  function frozenFadeWindowValid() {
    const start = fromDatetimeLocalUtc(($("frozenFadeStart") || {}).value);
    const end = fromDatetimeLocalUtc(($("frozenFadeEnd") || {}).value);
    if (!start || !end) return false;
    return Date.parse(start) < Date.parse(end);
  }

  function frozenFadeSyncControls() {
    const n = universe51SelectedCount();
    const summary = $("frozenFadeSelectionSummary");
    if (summary) {
      summary.textContent = n + " Coins ausgewählt · ~" + n + " min · ~" + (n * 20) + " MB";
    }
    const est = $("frozenFadeEstimates");
    if (est && n === 51) {
      est.textContent =
        "Geschätzte Laufzeit: ungefähr 1 Minute pro Coin; 51 Coins ungefähr 45–60 Minuten. " +
        "Geschätzter Artefaktspeicher: ungefähr 20 MB pro Coin; 51 Coins ungefähr 1 GB.";
    }
    const startBtn = $("frozenFadeStartBtn");
    const updateBlocks = universe51JobActive();
    const freezeBlocks = frozenFadeJobActive();
    if (startBtn) {
      startBtn.disabled = n < 1 || !frozenFadeWindowValid() || updateBlocks || freezeBlocks;
    }
    const start = $("frozenFadeStart");
    const end = $("frozenFadeEnd");
    if (start) start.disabled = freezeBlocks;
    if (end) end.disabled = freezeBlocks;
    const resume = $("frozenFadeResumeBtn");
    if (resume) {
      const allow = frozenFade.job && frozenFade.job.resumable && !freezeBlocks && !updateBlocks;
      resume.style.display = allow ? "inline-block" : "none";
      resume.disabled = !allow;
    }
  }

  function frozenFadeTfCell(row, tf) {
    const pt = (row.per_timeframe || {})[tf] || {};
    return (pt.raw == null ? "–" : pt.raw) + "/" + (pt.tier_a == null ? "–" : pt.tier_a);
  }

  function frozenFadeWarmupCell(c) {
    if (c.warmup_label) return c.warmup_label;
    const state = String(c.state || "");
    if (state === "PENDING") return "Noch nicht gestartet";
    if (state === "RUNNING") return "Läuft";
    if (state === "INTERRUPTED") return "Unterbrochen";
    if (c.warmup_schema_error) return "Artefaktfehler";
    const by = c.warmup_complete_by_tf || {};
    const tfs = ["15m", "30m", "1h", "4h"];
    const missing = tfs.filter(function (tf) { return typeof by[tf] !== "boolean"; });
    if (state === "FAILED" || state === "TIMEOUT") {
      if (missing.length) return "Artefaktfehler";
    }
    if (state === "COMPLETED" || state === "SKIPPED_RESUME_COMPLETE") {
      if (missing.length) return "Artefaktfehler";
      const ok = tfs.filter(function (tf) { return by[tf] === true; });
      const bad = tfs.filter(function (tf) { return by[tf] === false; });
      if (c.warmup_complete === true && bad.length === 0) return "vollständig";
      if (ok.length && bad.length) return "teilweise (" + bad.join(", ") + ")";
      if (c.warmup_complete === false || bad.length === 4) return "unvollständig";
      return "unvollständig";
    }
    if (missing.length) return "–";
    const ok = tfs.filter(function (tf) { return by[tf] === true; });
    const bad = tfs.filter(function (tf) { return by[tf] === false; });
    if (c.warmup_complete === true && bad.length === 0) return "vollständig";
    if (ok.length && bad.length) return "teilweise (" + bad.join(", ") + ")";
    if (c.warmup_complete === false || bad.length === 4) return "unvollständig";
    return "unvollständig";
  }

  function renderFrozenFadeJob() {
    const job = frozenFade.job || {};
    const prog = $("frozenFadeProgress");
    const barWrap = $("frozenFadeBarWrap");
    const bar = $("frozenFadeBar");
    const totals = $("frozenFadeTotals");
    const resultCard = $("frozenFadeResultCard");
    const body = $("frozenFadeResultBody");
    const active = frozenFadeJobActive();
    if (prog) {
      if (!job.state) {
        prog.style.display = "none";
      } else {
        prog.style.display = "block";
        const idx = job.current_index == null ? 0 : job.current_index;
        const total = job.total_coins == null ? 0 : job.total_coins;
        prog.textContent =
          (active ? "Job läuft · " : "") +
          "Coin " + idx + " von " + total +
          " · " + (job.current_symbol || "–") +
          " · Raw " + (job.raw_total == null ? 0 : job.raw_total) +
          " · Tier-A " + (job.tier_a_total == null ? 0 : job.tier_a_total) +
          " · Fehler " + (job.failed_coins == null ? 0 : job.failed_coins) +
          " · Job-ID " + (job.job_id || "–") +
          " · " + (job.state || "");
      }
    }
    if (barWrap && bar) {
      barWrap.style.display = job.state ? "block" : "none";
      bar.style.width = Math.max(0, Math.min(100, job.progress_percent || 0)) + "%";
    }
    const summary = job.combined_summary || {};
    if (totals) {
      totals.textContent =
        "Ausgewertet " + (summary.coins_evaluated == null ? (job.total_coins || "–") : summary.coins_evaluated) +
        " · erfolgreich " + (summary.successful_coins == null ? (job.successful_coins || 0) : summary.successful_coins) +
        " · fehlgeschlagen " + (summary.failed_coins == null ? (job.failed_coins || 0) : summary.failed_coins) +
        " · Raw " + (summary.raw_candidates == null ? (job.raw_total || 0) : summary.raw_candidates) +
        " · Tier-A " + (summary.tier_a == null ? (job.tier_a_total || 0) : summary.tier_a) +
        " · ohne Tier-A " + (summary.coins_without_tier_a == null ? "–" : summary.coins_without_tier_a) +
        " · mit Signalen " + (summary.coins_with_signals == null ? "–" : summary.coins_with_signals) +
        " · Warm-up-Warnungen " + (summary.warmup_warnings == null ? "–" : summary.warmup_warnings) +
        " · Multi-TF " + (summary.multi_tf_collisions == null ? "–" : summary.multi_tf_collisions);
    }
    const done = job.state && job.state !== "QUEUED" && job.state !== "RUNNING";
    if (resultCard && body) {
      if (done && Array.isArray(job.coins) && job.coins.length) {
        const jobId = String(job.job_id || "");
        const wasHidden = resultCard.style.display === "none";
        const isNewJob = jobId && jobId !== frozenFade.resultShownJobId;
        resultCard.style.display = "";
        if (wasHidden || isNewJob) {
          resultCard.open = true;
        }
        if (jobId) frozenFade.resultShownJobId = jobId;
        body.innerHTML = job.coins.map(function (c) {
          return "<tr><td>" + (c.symbol || "") + "</td><td>" + (c.state || "") +
            "</td><td>" + (c.raw_total == null ? "–" : c.raw_total) +
            "</td><td>" + (c.tier_a_total == null ? "–" : c.tier_a_total) +
            "</td><td>" + frozenFadeTfCell(c, "15m") +
            "</td><td>" + frozenFadeTfCell(c, "30m") +
            "</td><td>" + frozenFadeTfCell(c, "1h") +
            "</td><td>" + frozenFadeTfCell(c, "4h") +
            "</td><td>" + frozenFadeWarmupCell(c) +
            "</td><td>" + (c.multi_tf_collision_count == null ? "–" : c.multi_tf_collision_count) +
            "</td><td>" + (c.duration_seconds == null ? "–" : c.duration_seconds) +
            "</td></tr>";
        }).join("");
      } else if (!done) {
        resultCard.style.display = "none";
        resultCard.open = false;
        frozenFade.resultShownJobId = null;
      }
    }
    frozenFadeSyncControls();
  }

  function stopFrozenFadePoll() {
    if (frozenFade.pollTimer) {
      clearInterval(frozenFade.pollTimer);
      frozenFade.pollTimer = null;
    }
  }

  function startFrozenFadePoll() {
    if (frozenFade.pollTimer) return;
    frozenFade.pollTimer = setInterval(loadFrozenFadeStatus, frozenFade.pollMs);
  }

  async function loadFrozenFadeStatus() {
    try {
      const res = await fetch("/api/stoch/frozen-fade-jobs/status", { credentials: "include" });
      const data = await res.json().catch(function () { return {}; });
      if (data.defaults && !frozenFade.defaultsApplied) {
        const s = $("frozenFadeStart");
        const e = $("frozenFadeEnd");
        if (s && !s.value) s.value = toDatetimeLocalUtc(data.defaults.signal_start);
        if (e && !e.value) e.value = toDatetimeLocalUtc(data.defaults.signal_end_exclusive);
        frozenFade.defaultsApplied = true;
      }
      frozenFade.job = data && data.job_id ? data : frozenFade.job;
      if (!data.job_id) {
        if (!frozenFadeJobActive()) frozenFade.job = data && data.state ? data : frozenFade.job;
      }
      if (frozenFadeJobActive()) {
        startFrozenFadePoll();
      } else {
        stopFrozenFadePoll();
      }
      renderFrozenFadeJob();
      renderUniverse51Table();
      if (data && (data.state === "COMPLETED" || data.state === "COMPLETED_WITH_ERRORS")) {
        loadResearchJobCatalog();
      }
    } catch (_err) {
      /* keep page usable */
    }
  }

  async function startFrozenFadeJob() {
    const symbols = universe51SelectedSymbols();
    if (!symbols.length || frozenFadeJobActive() || universe51JobActive()) return;
    const start = fromDatetimeLocalUtc(($("frozenFadeStart") || {}).value);
    const end = fromDatetimeLocalUtc(($("frozenFadeEnd") || {}).value);
    const strategyId = researchJobStrategyId();
    const ezm = strategyId === "ema_zone_microstructure_confirmation_v1";
    const text = ezm
      ? ("EZM Candidate Discovery für " + symbols.length + " Coin(s)?\n" +
         "UTC " + start + " → " + end + " (Ende exklusiv)\n" +
         "Nur Research Candidates – keine Trades / kein PnL.")
      : ("Frozen-Signale für " + symbols.length + " Coin(s) berechnen?\n" +
         "UTC " + start + " → " + end + " (Ende exklusiv)\n" +
         "Nur Signalerzeugung – noch kein Profit-Backtest.");
    if (!window.confirm(text)) return;
    setErr("frozenFadeError", "");
    try {
      const body = {
        symbols: symbols,
        signal_start: start,
        signal_end_exclusive: end,
      };
      if (strategyId) body.strategy_id = strategyId;
      const res = await fetch("/api/stoch/frozen-fade-jobs", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json().catch(function () { return {}; });
      if (res.status === 409) {
        setErr("frozenFadeError", data.error || "Ein Research-Job läuft bereits.");
        loadFrozenFadeStatus();
        return;
      }
      if (!res.ok || data.success === false) {
        setErr("frozenFadeError", data.error || "Job konnte nicht gestartet werden");
        return;
      }
      frozenFade.job = data;
      startFrozenFadePoll();
      loadFrozenFadeStatus();
    } catch (_err) {
      setErr("frozenFadeError", "Frozen-Job-API nicht erreichbar");
    }
  }

  async function resumeFrozenFadeJob() {
    const jobId = frozenFade.job && frozenFade.job.job_id;
    if (!jobId || frozenFadeJobActive() || universe51JobActive()) return;
    if (!window.confirm("Unterbrochenen Frozen-Job " + jobId + " fortsetzen?")) return;
    try {
      const res = await fetch("/api/stoch/frozen-fade-jobs/" + encodeURIComponent(jobId) + "/resume", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      const data = await res.json().catch(function () { return {}; });
      if (!res.ok || data.success === false) {
        setErr("frozenFadeError", data.error || "Resume fehlgeschlagen");
        return;
      }
      frozenFade.job = data;
      startFrozenFadePoll();
      loadFrozenFadeStatus();
    } catch (_err) {
      setErr("frozenFadeError", "Resume-API nicht erreichbar");
    }
  }

  function wireFrozenFade() {
    const startBtn = $("frozenFadeStartBtn");
    if (startBtn) startBtn.addEventListener("click", startFrozenFadeJob);
    const resumeBtn = $("frozenFadeResumeBtn");
    if (resumeBtn) resumeBtn.addEventListener("click", resumeFrozenFadeJob);
    ["frozenFadeStart", "frozenFadeEnd"].forEach(function (id) {
      const el = $(id);
      if (el) el.addEventListener("change", frozenFadeSyncControls);
    });
    loadFrozenFadeStatus();
    wireFrozenEval();
  }

  async function loadFrozenEvalStatus() {
    try {
      const res = await fetch("/api/stoch/frozen-fade-evaluations/status", { credentials: "include" });
      const data = await res.json().catch(function () { return {}; });
      frozenEval.job = data && data.evaluation_id ? data : frozenEval.job;
      const prog = $("frozenEvalProgress");
      if (prog) {
        if (frozenEvalJobActive()) {
          prog.style.display = "block";
          prog.textContent =
            (data.state || "") +
            " · " +
            (data.current_symbol || "") +
            " · " +
            (data.progress_percent || 0) +
            "%";
          startFrozenEvalPoll();
        } else {
          prog.style.display = frozenEval.job && frozenEval.job.state ? "block" : "none";
          if (frozenEval.job && frozenEval.job.state) {
            prog.textContent = String(frozenEval.job.state);
          }
          stopFrozenEvalPoll();
          if (frozenEval.job && (frozenEval.job.state === "COMPLETED" || frozenEval.job.state === "COMPLETED_WITH_ERRORS")) {
            loadResearchEvalCatalog();
          }
        }
      }
      applyJobSourceUi();
    } catch (_err) {}
  }

  function startFrozenEvalPoll() {
    if (frozenEval.pollTimer) return;
    frozenEval.pollTimer = setInterval(loadFrozenEvalStatus, frozenEval.pollMs);
  }

  function stopFrozenEvalPoll() {
    if (frozenEval.pollTimer) {
      clearInterval(frozenEval.pollTimer);
      frozenEval.pollTimer = null;
    }
  }

  async function startFrozenEvalJob() {
    const jobId = selectedResearchJobId();
    if (!jobId || frozenEvalJobActive()) return;
    const ok = window.confirm(
      "Frozen-NO_BE50-Outcomes für diesen Job berechnen? Nur Tier-A, unabhängig je Signal, Initial-TP/SL SL_FIRST, ohne BE50, ohne Execution-Dedup, ohne ClickHouse-Writes."
    );
    if (!ok) return;
    setErr("frozenEvalError", "");
    try {
      const res = await fetch("/api/stoch/frozen-fade-evaluations", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source_job_id: jobId }),
      });
      const data = await res.json().catch(function () { return {}; });
      if (!res.ok || data.success === false) {
        setErr("frozenEvalError", data.error || "Evaluation konnte nicht gestartet werden");
        return;
      }
      frozenEval.job = data;
      startFrozenEvalPoll();
      applyJobSourceUi();
    } catch (_err) {
      setErr("frozenEvalError", "Evaluation-API nicht erreichbar");
    }
  }

  function wireFrozenEval() {
    const btn = $("frozenEvalStartBtn");
    if (btn) btn.addEventListener("click", startFrozenEvalJob);
    const resume = $("frozenEvalResumeBtn");
    if (resume) {
      resume.addEventListener("click", async function () {
        const eid = frozenEval.job && frozenEval.job.evaluation_id;
        if (!eid || frozenEvalJobActive()) return;
        try {
          const res = await fetch(
            "/api/stoch/frozen-fade-evaluations/" + encodeURIComponent(eid) + "/resume",
            { method: "POST", credentials: "include", headers: { "Content-Type": "application/json" }, body: "{}" }
          );
          const data = await res.json().catch(function () { return {}; });
          if (!res.ok || data.success === false) {
            setErr("frozenEvalError", data.error || "Resume fehlgeschlagen");
            return;
          }
          frozenEval.job = data;
          startFrozenEvalPoll();
        } catch (_err) {
          setErr("frozenEvalError", "Resume-API nicht erreichbar");
        }
      });
    }
    loadFrozenEvalStatus();
  }

  /* ------------------------------------------------------------------ */
  /* Daten-Collector Status (evidence API; fail-closed backfill)         */
  /* ------------------------------------------------------------------ */

  const dataHealthUi = {
    csrf: null,
    pollMs: 12000,
    timer: null,
    jobTimer: null,
  };

  function statusDotClass(status) {
    const s = String(status || "UNKNOWN").toUpperCase();
    if (s === "HEALTHY") return "stoch-chip stoch-chip-live";
    if (s === "DEGRADED" || s === "BACKFILLING") return "stoch-chip stoch-chip-warn";
    if (s === "STALE" || s === "STOPPED") return "stoch-chip stoch-chip-warn";
    return "stoch-chip";
  }

  function fmtLag(v) {
    if (v == null || v === "") return "–";
    const n = Number(v);
    if (!Number.isFinite(n)) return "–";
    if (n < 120) return n.toFixed(1) + "s";
    if (n < 3600) return (n / 60).toFixed(1) + "m";
    return (n / 3600).toFixed(1) + "h";
  }

  async function ensureDataHealthCsrf() {
    const res = await fetch("/api/collector-health/csrf", { credentials: "include" });
    const data = await res.json().catch(function () { return {}; });
    if (!res.ok) throw new Error(data.error || "csrf failed");
    dataHealthUi.csrf = data.csrf_token;
    return dataHealthUi.csrf;
  }

  function renderDataCollectorHealth(report) {
    const body = $("dataCollectorHealthBody");
    const meta = $("dataCollectorHealthMeta");
    if (meta) {
      meta.textContent =
        "Prüfung: " +
        (report.checked_at || "–") +
        " · OI-SoT: " +
        ((report.oi_sot && report.oi_sot.table) || "open_interest_5m_history") +
        " · contract " +
        (report.contract_version || "");
    }
    if (!body) return;
    const rows = report.collectors || [];
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="11" class="stoch-empty">Keine Collector</td></tr>';
      return;
    }
    body.innerHTML = rows
      .map(function (c) {
        const pid = c.process_running ? "PID " + (c.pid || "?") : "stopped";
        const cov =
          c.fresh_symbol_count != null && c.expected_symbol_count != null
            ? c.fresh_symbol_count + "/" + c.expected_symbol_count
            : c.coverage_status || "–";
        const drops =
          (c.dropped_events != null ? "drops=" + c.dropped_events : "") +
          (c.queue_depth != null ? " q=" + c.queue_depth : "");
        return (
          "<tr>" +
          '<td><span class="' +
          statusDotClass(c.status) +
          '">' +
          (c.status || "UNKNOWN") +
          "</span></td>" +
          "<td>" +
          (c.display_name || c.collector_id) +
          "</td>" +
          "<td>" +
          (c.granularity || "–") +
          "</td>" +
          "<td>" +
          pid +
          "</td>" +
          "<td>" +
          fmtLag(c.lag_seconds) +
          "</td>" +
          "<td>" +
          (c.last_successful_write_at || "–") +
          "</td>" +
          "<td>" +
          cov +
          "</td>" +
          "<td>" +
          fmtLag(c.lag_seconds) +
          "</td>" +
          "<td>" +
          (drops || "–") +
          "</td>" +
          "<td>" +
          (c.backfill_status || "–") +
          "</td>" +
          "<td>" +
          (c.evidence || c.reason || c.last_error || "–") +
          "</td>" +
          "</tr>"
        );
      })
      .join("");
  }

  async function loadDataCollectorHealth() {
    try {
      const res = await fetch("/api/collector-health", { credentials: "include" });
      const data = await res.json().catch(function () { return {}; });
      if (!res.ok) {
        setErr("dataCollectorHealthError", data.error || data.detail || "HTTP " + res.status);
        return;
      }
      setErr("dataCollectorHealthError", "");
      renderDataCollectorHealth(data);
    } catch (err) {
      setErr("dataCollectorHealthError", "Health-API: " + err);
    }
  }

  async function postOiBackfill(path, body) {
    const token = dataHealthUi.csrf || (await ensureDataHealthCsrf());
    const res = await fetch(path, {
      method: "POST",
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": token,
      },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(function () { return {}; });
    return { res: res, data: data };
  }

  function defaultOiWindow() {
    const end = new Date();
    const start = new Date(end.getTime() - 3 * 86400000);
    return {
      start: start.toISOString().replace(/\.\d{3}Z$/, "Z"),
      end: end.toISOString().replace(/\.\d{3}Z$/, "Z"),
    };
  }

  function pollOiJob(jobId) {
    if (dataHealthUi.jobTimer) clearInterval(dataHealthUi.jobTimer);
    dataHealthUi.jobTimer = setInterval(async function () {
      try {
        const res = await fetch("/api/collector-backfill/jobs/" + encodeURIComponent(jobId), {
          credentials: "include",
        });
        const job = await res.json().catch(function () { return {}; });
        const meta = $("oiBackfillJobMeta");
        const log = $("oiBackfillJobLog");
        if (meta) {
          meta.textContent =
            "Job " +
            (job.job_id || jobId) +
            " · " +
            (job.status || "") +
            " · " +
            (job.job_kind || "");
        }
        if (log) {
          log.textContent = JSON.stringify(job.result || job.error || job, null, 2);
        }
        if (job.status === "COMPLETED" || job.status === "FAILED") {
          clearInterval(dataHealthUi.jobTimer);
          dataHealthUi.jobTimer = null;
          loadDataCollectorHealth();
        }
      } catch (_e) {
        /* ignore transient */
      }
    }, 2000);
  }

  async function startOiDetect() {
    const w = defaultOiWindow();
    const confirmed = window.confirm(
      "OI 5m Lücken prüfen?\nSymbole: BTCUSDT, DOGEUSDT\n" + w.start + " → " + w.end + "\nNur Detect (kein Write)."
    );
    if (!confirmed) return;
    try {
      await ensureDataHealthCsrf();
      const out = await postOiBackfill("/api/collector-backfill/detect", {
        collector_id: "oi_5m_history",
        job_kind: "oi_5m_detect",
        symbols: ["BTCUSDT", "DOGEUSDT"],
        start: w.start,
        end: w.end,
      });
      if (!out.res.ok || out.data.success === false) {
        setErr("dataCollectorHealthError", out.data.error || "Detect fehlgeschlagen");
        return;
      }
      setErr("dataCollectorHealthError", "");
      pollOiJob(out.data.job_id);
    } catch (err) {
      setErr("dataCollectorHealthError", String(err));
    }
  }

  async function startOiDryRun() {
    const w = defaultOiWindow();
    const confirmed = window.confirm(
      "OI 5m Dry-Run Backfill (keine Inserts)?\nBTCUSDT, DOGEUSDT\n" + w.start + " → " + w.end
    );
    if (!confirmed) return;
    try {
      await ensureDataHealthCsrf();
      const out = await postOiBackfill("/api/collector-backfill/start", {
        collector_id: "oi_5m_history",
        job_kind: "oi_5m_backfill_dry_run",
        symbols: ["BTCUSDT", "DOGEUSDT"],
        start: w.start,
        end: w.end,
      });
      if (!out.res.ok || out.data.success === false) {
        setErr("dataCollectorHealthError", out.data.error || "Dry-Run fehlgeschlagen");
        return;
      }
      setErr("dataCollectorHealthError", "");
      pollOiJob(out.data.job_id);
    } catch (err) {
      setErr("dataCollectorHealthError", String(err));
    }
  }

  function wireDataCollectorHealth() {
    const detect = $("oiGapDetectBtn");
    const dry = $("oiBackfillDryRunBtn");
    const pt = $("ptBackfillBtn");
    if (detect) detect.addEventListener("click", startOiDetect);
    if (dry) dry.addEventListener("click", startOiDryRun);
    if (pt) {
      pt.disabled = true;
      pt.title = "DEGRADED — LIVE CURRENT BUT DATA LOSS POSSIBLE";
    }
    loadDataCollectorHealth();
    if (dataHealthUi.timer) clearInterval(dataHealthUi.timer);
    dataHealthUi.timer = setInterval(loadDataCollectorHealth, dataHealthUi.pollMs);
  }
})();
