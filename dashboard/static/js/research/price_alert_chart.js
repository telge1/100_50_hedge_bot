/**
 * Chart-integrated price alerts: multi-tab leader, status sync, pane price lines.
 */
(function (root) {
  "use strict";

  const ALERT_SOUND_URL = "/static/sounds/price_alert.wav";
  const STATUS_MS = 5000;
  const LEADER_KEY = "mc_price_alert_leader_v1";
  const SEEN_KEY = "mc_price_alert_seen_events_v1";
  const LEADER_TTL_MS = 4000;
  const CHANNEL = "mc_price_alert_v1";

  let channel = null;
  try {
    channel = typeof BroadcastChannel !== "undefined" ? new BroadcastChannel(CHANNEL) : null;
  } catch (e) {
    channel = null;
  }

  const state = {
    leader: false,
    tabId: "tab_" + Date.now() + "_" + Math.random().toString(36).slice(2, 8),
    knownKeys: null,
    seenEventIds: loadSeen(),
    inFlightCreates: Object.create(null),
    panes: null,
    paneSymbolFn: null,
    apiFn: null,
    setStatusFn: null,
    onLinesChanged: null,
  };

  function loadSeen() {
    try {
      const raw = sessionStorage.getItem(SEEN_KEY);
      const arr = raw ? JSON.parse(raw) : [];
      return new Set(Array.isArray(arr) ? arr.slice(-400) : []);
    } catch (e) {
      return new Set();
    }
  }

  function persistSeen() {
    try {
      const arr = [...state.seenEventIds].slice(-400);
      sessionStorage.setItem(SEEN_KEY, JSON.stringify(arr));
    } catch (e) {}
  }

  function readLeader() {
    try {
      const raw = localStorage.getItem(LEADER_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (e) {
      return null;
    }
  }

  function writeLeader() {
    try {
      localStorage.setItem(
        LEADER_KEY,
        JSON.stringify({ tabId: state.tabId, heartbeat: Date.now() })
      );
    } catch (e) {}
  }

  function electLeader() {
    const now = Date.now();
    const cur = readLeader();
    if (!cur || !cur.tabId || now - (cur.heartbeat || 0) > LEADER_TTL_MS) {
      writeLeader();
    }
    const again = readLeader();
    state.leader = !!(again && again.tabId === state.tabId);
    if (state.leader) writeLeader();
    return state.leader;
  }

  function alertKeyFromRow(key, info) {
    if (key) return key;
    const sym = String(info.symbol || "").toUpperCase();
    const tp = Number(info.target_price);
    const tr = String(info.trigger || "above");
    return `${sym}_${tp.toFixed(6)}_${tr}`;
  }

  async function playAlertSound(times) {
    const n = Math.max(1, times || 2);
    for (let i = 0; i < n; i += 1) {
      await new Promise(function (resolve, reject) {
        const audio = new Audio(ALERT_SOUND_URL + "?v=mc-alert-p2&i=" + i + "&t=" + Date.now());
        audio.volume = 1.0;
        audio.onended = function () { resolve(); };
        audio.onerror = function () { reject(new Error("audio")); };
        const p = audio.play();
        if (p && typeof p.then === "function") p.catch(reject);
      });
    }
  }

  function showBanner(evt) {
    let el = document.getElementById("mc-price-alert-banner");
    if (!el) {
      el = document.createElement("div");
      el.id = "mc-price-alert-banner";
      el.className = "mc-price-alert-banner";
      document.body.appendChild(el);
    }
    const sym = evt.symbol || "";
    const tp = evt.target_price;
    const fp = evt.fired_price;
    const at = evt.fired_at || "";
    el.textContent =
      "🔔 Preisalarm " + sym + " Ziel " + tp + " — Auslösung " + fp + " (" + at + ")";
    el.classList.add("visible");
    clearTimeout(showBanner._t);
    showBanner._t = setTimeout(function () {
      el.classList.remove("visible");
    }, 12000);
  }

  function handleFiredEvent(evt) {
    const eid = evt && evt.event_id;
    if (!eid || state.seenEventIds.has(eid)) return;
    state.seenEventIds.add(eid);
    persistSeen();
    showBanner(evt);
    if (channel) {
      channel.postMessage({ type: "fired", event: evt, tabId: state.tabId });
    }
    if (state.leader) {
      playAlertSound(3).catch(function () {});
    }
  }

  async function fetchStatus() {
    const r = await fetch("/api/price-alert/status", { credentials: "same-origin" });
    return r.json();
  }

  async function fetchEvents(sinceUnix) {
    const qs = sinceUnix > 0 ? "?since=" + encodeURIComponent(sinceUnix) : "";
    const r = await fetch("/api/price-alert/events" + qs, { credentials: "same-origin" });
    return r.json();
  }

  let lastEventSince = Math.floor(Date.now() / 1000) - 30;

  async function pollOnce() {
    electLeader();
    try {
      const [st, ev] = await Promise.all([fetchStatus(), fetchEvents(lastEventSince)]);
      const alerts = (st && st.success && st.alerts) ? st.alerts : {};
      syncAlertLinesFromAlerts(alerts);
      const keys = new Set(Object.keys(alerts));
      if (state.knownKeys !== null) {
        keys.forEach(function (k) {
          if (!state.knownKeys.has(k)) state.knownKeys.add(k);
        });
      }
      state.knownKeys = keys;
      const events = (ev && ev.success && ev.events) ? ev.events : [];
      events.forEach(function (row) {
        if (row && row.fired_at_unix) {
          lastEventSince = Math.max(lastEventSince, Number(row.fired_at_unix));
        }
        handleFiredEvent(row);
      });
    } catch (e) {
      /* ignore */
    }
  }

  function syncAlertLinesFromAlerts(alerts) {
    if (!state.panes || !state.paneSymbolFn || !state.apiFn) return;
    const bySym = {};
    Object.keys(alerts || {}).forEach(function (key) {
      const info = alerts[key];
      if (!info) return;
      const sym = String(info.symbol || "").toUpperCase();
      if (!bySym[sym]) bySym[sym] = [];
      bySym[sym].push({
        alert_key: key,
        target_price: Number(info.target_price),
        trigger: info.trigger,
      });
    });
    Object.keys(state.panes).forEach(function (pid) {
      const pane = state.panes[pid];
      if (!pane) return;
      const sym = state.paneSymbolFn(pane);
      const chart = state.apiFn(pane);
      if (chart && chart.setPriceAlertLines) {
        chart.setPriceAlertLines(bySym[sym] || []);
      }
    });
    if (state.onLinesChanged) state.onLinesChanged(alerts);
  }

  async function referencePrice(symbol) {
    try {
      const r = await fetch(
        "/api/price-alert/current-price?symbol=" + encodeURIComponent(symbol),
        { credentials: "same-origin" }
      );
      const j = await r.json();
      if (j.success && j.price != null) return Number(j.price);
    } catch (e) {}
    return null;
  }

  async function createAlert(symbol, targetPrice) {
    const sym = String(symbol || "").toUpperCase();
    const tp = Number(targetPrice);
    if (!sym || !Number.isFinite(tp) || tp <= 0) {
      return { success: false, error: "Ungültiger Preis" };
    }
    const dedupe = sym + "_" + tp.toFixed(8);
    if (state.inFlightCreates[dedupe]) {
      return { success: false, error: "Alert wird bereits erstellt…" };
    }
    state.inFlightCreates[dedupe] = true;
    try {
      const ref = await referencePrice(sym);
      if (ref == null || !Number.isFinite(ref)) {
        return { success: false, error: "Referenzpreis nicht verfügbar" };
      }
      const trigger = tp > ref ? "above" : "below";
      const r = await fetch("/api/price-alert/start", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          symbol: sym,
          target_price: tp,
          trigger: trigger,
        }),
      });
      const j = await r.json();
      if (j.success) {
        await pollOnce();
        if (state.setStatusFn) {
          state.setStatusFn(
            "Preisalarm " + sym + " " + (trigger === "above" ? "≥" : "≤") + " " + tp,
            "success"
          );
        }
      }
      return j;
    } finally {
      delete state.inFlightCreates[dedupe];
    }
  }

  async function deleteAlert(alertKey, symbol, targetPrice, trigger) {
    const body = { symbol: String(symbol || "").toUpperCase() };
    if (alertKey) body.alert_key = alertKey;
    if (targetPrice != null) body.target_price = targetPrice;
    if (trigger) body.trigger = trigger;
    const r = await fetch("/api/price-alert/stop", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const j = await r.json();
    if (j.success) await pollOnce();
    return j;
  }

  function initHost(opts) {
    opts = opts || {};
    state.panes = opts.panes;
    state.paneSymbolFn = opts.paneSymbol;
    state.apiFn = opts.api;
    state.setStatusFn = opts.setStatus;
    state.onLinesChanged = opts.onLinesChanged;
    if (channel) {
      channel.onmessage = function (ev) {
        const data = ev.data || {};
        if (data.type === "fired" && data.tabId !== state.tabId) {
          handleFiredEvent(data.event);
        }
        if (data.type === "sync_request") {
          pollOnce();
        }
      };
    }
    setInterval(function () {
      electLeader();
      if (state.leader) writeLeader();
    }, LEADER_TTL_MS / 2);
    setInterval(pollOnce, STATUS_MS);
    pollOnce();
  }

  function mountSoundButton(anchor) {
    if (!anchor || document.getElementById("mcPriceAlertSoundBtn")) return;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.id = "mcPriceAlertSoundBtn";
    btn.className = "btn-test-sound research-alert-sound-btn";
    btn.textContent = "🔊 Alert-Ton";
    btn.title = "Browser-Alert-Ton aktivieren";
    btn.addEventListener("click", function () {
      playAlertSound(1).then(function () {
        if (state.setStatusFn) state.setStatusFn("Alert-Ton bereit.", "success");
      }).catch(function () {
        if (state.setStatusFn) state.setStatusFn("Alert-Ton blockiert (Autoplay).", "error");
      });
    });
    anchor.appendChild(btn);
  }

  root.MCPriceAlert = {
    initHost: initHost,
    createAlert: createAlert,
    deleteAlert: deleteAlert,
    pollOnce: pollOnce,
    playAlertSound: playAlertSound,
    mountSoundButton: mountSoundButton,
    alertKeyFromRow: alertKeyFromRow,
  };
})(typeof window !== "undefined" ? window : globalThis);