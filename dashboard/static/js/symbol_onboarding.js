(function () {
  "use strict";

  const STORAGE_KEY = "symbol_onboarding_active_job_id";
  const $ = (id) => document.getElementById(id);

  const state = {
    planId: null,
    planHash: null,
    planRequest: null,
    jobId: null,
    pollTimer: null,
    pollFail: 0,
    histOffset: 0,
    histLimit: 15,
    histTotal: 0,
    applying: false,
  };

  function show(el, on) {
    if (!el) return;
    el.hidden = !on;
  }

  function setError(id, msg) {
    const el = $(id);
    if (!el) return;
    if (!msg) {
      el.hidden = true;
      el.textContent = "";
      return;
    }
    el.hidden = false;
    el.textContent = msg;
  }

  function formPayload() {
    const preset = ($("soDaysPreset") || {}).value || "30";
    let days = Number(preset);
    if (preset === "custom") {
      days = Number(($("soDaysCustom") || {}).value || 0);
    }
    const purpose = String(($("soPurpose") || {}).value || "onboard");
    const extend = purpose === "extend_history";
    return {
      symbol: String(($("soSymbol") || {}).value || "")
        .trim()
        .toUpperCase(),
      days: days,
      candles_1m: !!($("soCandles") || {}).checked,
      open_interest_5m: !!($("soOi5m") || {}).checked,
      public_trades: !!($("soTrades") || {}).checked,
      with_ob1000: extend ? false : !!($("soOb1000") || {}).checked,
      restart_live: extend ? false : !!($("soRestartLive") || {}).checked,
      purpose: purpose,
    };
  }

  function setMode(mode) {
    const purpose = mode === "extend_history" ? "extend_history" : "onboard";
    $("soPurpose").value = purpose;
    const onboardBtn = $("soModeOnboard");
    const extendBtn = $("soModeExtend");
    if (onboardBtn && extendBtn) {
      onboardBtn.classList.toggle("is-active", purpose === "onboard");
      extendBtn.classList.toggle("is-active", purpose === "extend_history");
      onboardBtn.classList.toggle("stoch-btn-ghost", purpose !== "onboard");
      extendBtn.classList.toggle("stoch-btn-ghost", purpose !== "extend_history");
    }
    const extend = purpose === "extend_history";
    show($("soLiveFieldset"), !extend);
    show($("soRestartLiveWrap"), !extend);
    if (extend) {
      $("soOb1000").checked = false;
      $("soRestartLive").checked = false;
      $("soCandles").checked = true;
      if (!$("soOi5m").checked && !$("soTrades").checked) {
        $("soOi5m").checked = true;
        $("soTrades").checked = true;
      }
      $("soDaysPreset").value = "90";
      syncDaysUi();
      $("soModeHint").textContent =
        "History vergrößern: nur Backfill (Candles/OI/Trades). Kein Live-Restart, kein OB1000. Bereits registrierte Symbole ok.";
      $("soApplyBtn").textContent = "History vergrößern";
      $("soConfirmTitle").textContent = "History-Nachzug bestätigen";
      $("soConfirmText").textContent =
        "Es werden nur historische Daten nachgeladen. Universe bleibt unverändert. Keine Collector-Restarts, kein OB1000.";
      $("soConfirmOk").textContent = "History starten";
    } else {
      $("soModeHint").textContent =
        "Neues Symbol: Universe/Tick, optional Live-Aktivierung und OB1000.";
      $("soApplyBtn").textContent = "Symbol hinzufügen";
      $("soConfirmTitle").textContent = "Aktivierung bestätigen";
      $("soConfirmText").textContent =
        "Durch die Aktivierung werden Live-Collector kurz kontrolliert neu gestartet. Bestehende Symbole werden anschließend automatisch überprüft.";
      $("soConfirmOk").textContent = "Jetzt hinzufügen";
    }
    invalidatePlan();
  }

  function fillExtendForSymbol(symbol) {
    $("soSymbol").value = String(symbol || "").toUpperCase();
    setMode("extend_history");
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function invalidatePlan() {
    state.planId = null;
    state.planHash = null;
    state.planRequest = null;
    const btn = $("soApplyBtn");
    if (btn) btn.disabled = true;
    show($("soPlanBody"), false);
    show($("soPlanEmpty"), true);
  }

  function syncDaysUi() {
    const custom = (($("soDaysPreset") || {}).value || "") === "custom";
    show($("soDaysCustom"), custom);
    show($("soDaysCustomLabel"), custom);
  }

  async function api(path, opts) {
    const options = opts || {};
    const headers = Object.assign({ Accept: "application/json" }, options.headers || {});
    if (options.body != null) {
      headers["Content-Type"] = "application/json";
    }
    const res = await fetch(path, {
      method: options.method || "GET",
      headers: headers,
      body: options.body != null ? JSON.stringify(options.body) : undefined,
      credentials: "same-origin",
    });
    let data = null;
    try {
      data = await res.json();
    } catch (e) {
      data = { success: false, error: "INVALID_RESPONSE" };
    }
    return { status: res.status, data: data };
  }

  function stageClass(status) {
    const s = String(status || "").toUpperCase();
    if (s === "SUCCEEDED") return "so-stage-ok";
    if (s === "RUNNING") return "so-stage-run";
    if (s === "FAILED") return "so-stage-fail";
    if (s === "WAITING" || s === "PENDING") return "so-stage-wait";
    if (s === "SKIPPED") return "so-stage-skip";
    return "";
  }

  function stageLabelStatus(status) {
    const s = String(status || "").toUpperCase();
    if (s === "SUCCEEDED") return "erfolgreich";
    if (s === "RUNNING") return "läuft";
    if (s === "FAILED") return "fehlgeschlagen";
    if (s === "WAITING") return "wartet auf Archiv";
    if (s === "PENDING") return "wartet";
    if (s === "SKIPPED") return "übersprungen";
    if (s === "ROLLED_BACK") return "zurückgerollt";
    return s || "wartet";
  }

  function renderJob(job) {
    if (!job) return;
    const meta = [
      "job_id: " + (job.job_id || "–"),
      "Symbol: " + (job.symbol || "–"),
      "Status: " + (job.status || "–"),
      "Start: " + (job.created_at || "–"),
      "Update: " + (job.updated_at || "–"),
      "Fortschritt: " + (job.progress_pct != null ? job.progress_pct + "%" : "–"),
      "Verdict: " + (job.final_verdict || "–"),
    ];
    if (job.rollback_status) meta.push("Rollback: " + job.rollback_status);
    if (job.error_code) meta.push("error_code: " + job.error_code);
    $("soProgressMeta").textContent = meta.join(" · ");

    const pct = Math.max(0, Math.min(100, Number(job.progress_pct) || 0));
    show($("soBarWrap"), true);
    $("soBar").style.width = pct + "%";

    const ul = $("soStages");
    ul.innerHTML = "";
    (job.stages || []).forEach(function (st) {
      const li = document.createElement("li");
      li.className = stageClass(st.status);
      li.innerHTML =
        "<span>" +
        (st.label || st.name) +
        "</span><span>" +
        stageLabelStatus(st.status) +
        "</span>";
      ul.appendChild(li);
    });

    if (job.waiting_for_archive_hint) {
      show($("soArchiveHint"), true);
      $("soArchiveHint").textContent = job.waiting_for_archive_hint;
    } else {
      show($("soArchiveHint"), false);
    }

    if (job.error_message) setError("soJobError", job.error_message);
    else setError("soJobError", "");
  }

  function renderPlan(payload) {
    const job = payload.job || {};
    const plan = job.plan || {};
    const inst = plan.instrument || {};
    const req = payload.request || {};
    const lines = [
      "Symbol: " + (plan.symbol || req.symbol || ""),
      "Bybit-Status: " + (inst.status || "–"),
      "category: " + (inst.category || "–"),
      "quoteCoin: " + (inst.quote_coin || "–"),
      "tickSize: " + (inst.tick_size || "–"),
      "qtyStep: " + (inst.qty_step || "–"),
      "minOrderQty: " + (inst.min_order_qty || "–"),
      "Historie: " + (plan.days || req.days) + " Tage",
      "Streams: " + ((job.requested_streams || []).join(", ") || "–"),
      "Collector-Restarts: " + (payload.collector_restarts ? "ja (wenn Apply + live)" : "nein"),
      "Warnings: " + ((payload.warnings || []).join(" | ") || "–"),
    ];
    const body = $("soPlanBody");
    body.innerHTML = "<pre class='so-confirm-summary'>" + lines.join("\n") + "</pre>";
    show(body, true);
    show($("soPlanEmpty"), false);
    state.planId = payload.plan_id;
    state.planHash = payload.plan_hash;
    state.planRequest = req;
    $("soApplyBtn").disabled = false;
  }

  function stopPolling() {
    if (state.pollTimer) {
      clearTimeout(state.pollTimer);
      state.pollTimer = null;
    }
  }

  function schedulePoll(delay) {
    stopPolling();
    state.pollTimer = setTimeout(pollJob, delay);
  }

  async function pollJob() {
    if (!state.jobId) return;
    if (state.pollTimer) {
      /* single-flight: clear handle before await */
    }
    const { status, data } = await api(
      "/api/datenverwaltung/symbole/jobs/" + encodeURIComponent(state.jobId)
    );
    if (status === 404) {
      stopPolling();
      setError("soJobError", "Job nicht gefunden");
      return;
    }
    if (!data || !data.success) {
      state.pollFail += 1;
      const backoff = Math.min(30000, 1000 * Math.pow(2, Math.min(state.pollFail, 5)));
      schedulePoll(backoff);
      return;
    }
    state.pollFail = 0;
    renderJob(data);
    const active =
      data.status === "RUNNING" ||
      data.status === "WAITING_FOR_ARCHIVE" ||
      data.active === true;
    if (active && data.status !== "SUCCEEDED" && data.status !== "FAILED" && data.status !== "ROLLED_BACK" && data.status !== "PARTIAL_LIVE_ONLY" && data.status !== "PLANNED") {
      schedulePoll(2000);
      return;
    }
    if (data.status === "RUNNING" || data.status === "WAITING_FOR_ARCHIVE") {
      schedulePoll(data.status === "WAITING_FOR_ARCHIVE" ? 8000 : 2000);
      return;
    }
    stopPolling();
    loadHistory();
    loadOverview();
  }

  function resumeJob(jobId) {
    if (!jobId) return;
    state.jobId = jobId;
    try {
      localStorage.setItem(STORAGE_KEY, jobId);
    } catch (e) {}
    const url = new URL(window.location.href);
    url.searchParams.set("job_id", jobId);
    window.history.replaceState({}, "", url.toString());
    stopPolling();
    pollJob();
  }

  async function runPlan() {
    setError("soFormError", "");
    const payload = formPayload();
    if (!/^[A-Z0-9]{2,20}USDT$/.test(payload.symbol)) {
      setError("soFormError", "Ungültiges Symbol (z. B. AAPLUSDT)");
      return;
    }
    $("soPlanBtn").disabled = true;
    try {
      const { status, data } = await api("/api/datenverwaltung/symbole/plan", {
        method: "POST",
        body: payload,
      });
      if (!data || !data.success) {
        setError("soFormError", (data && (data.error_message || data.error)) || "Plan fehlgeschlagen (" + status + ")");
        invalidatePlan();
        return;
      }
      renderPlan(data);
      if (data.job) renderJob(data.job);
    } catch (e) {
      setError("soFormError", "Netzwerkfehler beim Plan");
      invalidatePlan();
    } finally {
      $("soPlanBtn").disabled = false;
    }
  }

  function openConfirm() {
    if (!state.planId || !state.planHash) return;
    const p = formPayload();
    const summary = [
      "Modus: " + (p.purpose === "extend_history" ? "History vergrößern" : "Symbol hinzufügen"),
      "Symbol: " + p.symbol,
      "Historie: " + p.days + " Tage",
      "Candles 1m: " + (p.candles_1m ? "ja" : "nein"),
      "OI 5m: " + (p.open_interest_5m ? "ja" : "nein"),
      "Public Trades: " + (p.public_trades ? "ja" : "nein"),
      "OB1000 live: " + (p.with_ob1000 ? "ja" : "nein"),
      "Live aktivieren: " + (p.restart_live ? "ja" : "nein"),
      "plan_id: " + state.planId,
    ].join("\n");
    $("soConfirmSummary").textContent = summary;
    show($("soConfirmModal"), true);
  }

  async function confirmApply() {
    if (state.applying) return;
    state.applying = true;
    $("soConfirmOk").disabled = true;
    setError("soFormError", "");
    try {
      const body = Object.assign(formPayload(), {
        confirm: true,
        plan_id: state.planId,
        plan_hash: state.planHash,
      });
      const { status, data } = await api("/api/datenverwaltung/symbole/apply", {
        method: "POST",
        body: body,
      });
      show($("soConfirmModal"), false);
      if (!data || !data.success) {
        setError(
          "soFormError",
          (data && (data.error_message || data.error)) || "Apply fehlgeschlagen (" + status + ")"
        );
        return;
      }
      resumeJob(data.job_id);
      invalidatePlan();
    } catch (e) {
      setError("soFormError", "Netzwerkfehler beim Apply");
    } finally {
      state.applying = false;
      $("soConfirmOk").disabled = false;
    }
  }

  async function loadHistory() {
    const { data } = await api(
      "/api/datenverwaltung/symbole/jobs?limit=" +
        state.histLimit +
        "&offset=" +
        state.histOffset
    );
    const tbody = $("soHistoryBody");
    if (!data || !data.success) {
      tbody.innerHTML = "<tr><td colspan='7' class='stoch-empty'>Fehler</td></tr>";
      return;
    }
    state.histTotal = data.total || 0;
    const page = Math.floor(state.histOffset / state.histLimit) + 1;
    $("soHistPage").textContent = "Seite " + page + " · " + state.histTotal + " Jobs";
    if (!(data.jobs || []).length) {
      tbody.innerHTML = "<tr><td colspan='7' class='stoch-empty'>Keine Jobs</td></tr>";
      return;
    }
    tbody.innerHTML = data.jobs
      .map(function (j) {
        return (
          "<tr>" +
          "<td>" +
          (j.created_at || "–") +
          "</td>" +
          "<td>" +
          (j.symbol || "–") +
          "</td>" +
          "<td>" +
          (j.requested_days != null ? j.requested_days : "–") +
          "</td>" +
          "<td>" +
          (j.status || "–") +
          "</td>" +
          "<td>" +
          (j.progress_pct != null ? j.progress_pct : "–") +
          "</td>" +
          "<td>" +
          (j.final_verdict || "–") +
          "</td>" +
          "<td><button type='button' class='stoch-btn stoch-btn-ghost so-open-job' data-job='" +
          (j.job_id || "") +
          "'>Detail</button></td>" +
          "</tr>"
        );
      })
      .join("");
  }

  async function loadOverview() {
    const { data } = await api("/api/datenverwaltung/symbole/overview");
    const tbody = $("soOverviewBody");
    if (!data || !data.success) {
      tbody.innerHTML = "<tr><td colspan='6' class='stoch-empty'>Fehler</td></tr>";
      return;
    }
    $("soOverviewNote").textContent = data.note || "";
    if (!(data.symbols || []).length) {
      tbody.innerHTML = "<tr><td colspan='6' class='stoch-empty'>Leer</td></tr>";
      return;
    }
    tbody.innerHTML = data.symbols
      .map(function (s) {
        return (
          "<tr>" +
          "<td>" +
          s.symbol +
          "</td>" +
          "<td>" +
          (s.streams || []).join(", ") +
          "</td>" +
          "<td>" +
          (s.ob1000_registered ? "ja" : "nein") +
          "</td>" +
          "<td>" +
          (s.overall_status || "–") +
          "</td>" +
          "<td>" +
          (s.last_job_status || "–") +
          "</td>" +
          "<td><button type='button' class='stoch-btn stoch-btn-ghost so-extend-sym' data-symbol='" +
          (s.symbol || "") +
          "'>History vergrößern</button></td>" +
          "</tr>"
        );
      })
      .join("");
  }

  function bind() {
    ["soSymbol", "soDaysPreset", "soDaysCustom", "soCandles", "soOi5m", "soTrades", "soOb1000", "soRestartLive"].forEach(
      function (id) {
        const el = $(id);
        if (!el) return;
        el.addEventListener("change", function () {
          if (id === "soSymbol") el.value = String(el.value || "").toUpperCase();
          if (id === "soDaysPreset") syncDaysUi();
          invalidatePlan();
        });
        el.addEventListener("input", function () {
          if (id === "soSymbol") el.value = String(el.value || "").toUpperCase();
          invalidatePlan();
        });
      }
    );
    $("soModeOnboard").addEventListener("click", function () {
      setMode("onboard");
    });
    $("soModeExtend").addEventListener("click", function () {
      setMode("extend_history");
    });
    $("soPlanBtn").addEventListener("click", runPlan);
    $("soApplyBtn").addEventListener("click", openConfirm);
    $("soConfirmCancel").addEventListener("click", function () {
      show($("soConfirmModal"), false);
    });
    $("soConfirmOk").addEventListener("click", confirmApply);
    $("soHistPrev").addEventListener("click", function () {
      state.histOffset = Math.max(0, state.histOffset - state.histLimit);
      loadHistory();
    });
    $("soHistNext").addEventListener("click", function () {
      if (state.histOffset + state.histLimit < state.histTotal) {
        state.histOffset += state.histLimit;
        loadHistory();
      }
    });
    $("soHistoryBody").addEventListener("click", function (ev) {
      const btn = ev.target.closest(".so-open-job");
      if (!btn) return;
      resumeJob(btn.getAttribute("data-job"));
    });
    $("soOverviewBody").addEventListener("click", function (ev) {
      const btn = ev.target.closest(".so-extend-sym");
      if (!btn) return;
      fillExtendForSymbol(btn.getAttribute("data-symbol"));
    });
    syncDaysUi();
    setMode("onboard");
  }

  function boot() {
    bind();
    loadHistory();
    loadOverview();
    const url = new URL(window.location.href);
    const fromUrl = url.searchParams.get("job_id");
    let fromStore = null;
    try {
      fromStore = localStorage.getItem(STORAGE_KEY);
    } catch (e) {}
    const jobId = fromUrl || fromStore;
    if (jobId) resumeJob(jobId);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
