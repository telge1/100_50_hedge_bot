/**
 * Live Orderbook page — sticky in-place updates (no flicker / no clear-before-fetch).
 *
 * Poll flow:
 *   setInterval(refresh)
 *   → pollInFlight guard (no UI clear)
 *   → fetch /api/live-orderbook/snapshot
 *   → if incomplete and lastGood exists: keep DOM (optional status-only)
 *   → else renderSnapshot(complete payload)
 */
(function () {
  "use strict";

  const START_LABEL = "▶ STARTEN";
  const TONES = ["positive", "negative", "warning", "neutral", "mixed"];
  const CARD_NAMES = [
    "resistance",
    "support",
    "support2",
    "absorption",
    "near_price",
    "level_quality",
    "liquidations",
    "money_flow",
    "wall_bias",
    "overall",
  ];
  const LEVEL_CARDS = new Set(["resistance", "support", "support2"]);
  const ACTIVE_STATUSES = new Set([
    "STARTING",
    "LIVE",
    "BOOTSTRAP_OK",
    "WAITING_FOR_DATA",
    "STALE_DATA",
    "RECONNECTING",
    "STOPPING",
  ]);
  const TERMINAL_STATUSES = new Set(["STOPPED", "FAILED", "COMPLETED"]);

  const els = {
    symbol: document.getElementById("lobSymbol"),
    window: document.getElementById("lobWindow"),
    start: document.getElementById("lobStart"),
    stop: document.getElementById("lobStop"),
    restart: document.getElementById("lobRestart"),
    statusDot: document.getElementById("lobStatusDot"),
    statusText: document.getElementById("lobStatusText"),
    watchSymbol: document.getElementById("lobWatchSymbol"),
    windowLabel: document.getElementById("lobWindowLabel"),
    state: document.getElementById("lobState"),
    decision: document.getElementById("lobDecision"),
    mid: document.getElementById("lobMid"),
    dataAge: document.getElementById("lobDataAge"),
    levelSource: document.getElementById("lobLevelSource"),
  };

  let busy = false;
  let pollInFlight = false;
  /** @type {object|null} last complete API snapshot (metadata / status) */
  let lastGoodSnapshot = null;
  /** @type {object|null} last display block that was actually painted (coalesced) */
  let lastPaintedDisplay = null;

  const cards = {};
  CARD_NAMES.forEach((name) => {
    const root = document.querySelector(`[data-card="${name}"]`);
    if (!root) return;
    cards[name] = {
      root,
      title: root.querySelector('[data-role="title"]'),
      move: root.querySelector('[data-role="move"]'),
      headline: root.querySelector('[data-role="headline"]'),
      tech: root.querySelector('[data-role="tech"]'),
      decision: root.querySelector('[data-role="decision"]'),
      metrics: Array.from(root.querySelectorAll("[data-metric]")).sort(
        (a, b) => Number(a.getAttribute("data-metric")) - Number(b.getAttribute("data-metric"))
      ),
      reasonsFor: Array.from(root.querySelectorAll("[data-reason^='for-']")),
      reasonsAgainst: Array.from(root.querySelectorAll("[data-reason^='against-']")),
    };
  });
  const support2Fallback = document.getElementById("lobSupport2Fallback");
  const fieldCache = {};
  /** @type {object|null} last painted OB grid (sticky; avoid flicker) */
  let lastPaintedObGrid = null;

  const obGridEls = {
    root: document.querySelector('[data-card="ob-grid"]'),
    askLevels: document.querySelector('[data-role="ask-levels"]'),
    bidLevels: document.querySelector('[data-role="bid-levels"]'),
    nearMidAsk: document.querySelector('[data-role="near-mid-ask"]'),
    nearMidBid: document.querySelector('[data-role="near-mid-bid"]'),
    mid: document.querySelector('[data-role="grid-mid"]'),
    ladder: document.querySelector('[data-role="grid-ladder"]'),
    extra: document.querySelector('[data-role="grid-extra"]'),
    empty: document.querySelector('[data-role="grid-empty"]'),
  };

  function field(name) {
    if (!(name in fieldCache)) {
      fieldCache[name] = document.querySelector(`[data-field="${name}"]`);
    }
    return fieldCache[name];
  }

  function setText(el, value) {
    if (!el) return;
    const next = value == null ? "" : String(value);
    if (el.textContent !== next) el.textContent = next;
  }

  function setTone(el, tone) {
    if (!el) return;
    const t = TONES.includes(tone) ? tone : "neutral";
    if (el.dataset.tone === t) return;
    if (el.dataset.tone) el.classList.remove(`lob-tone-${el.dataset.tone}`);
    el.dataset.tone = t;
    el.classList.add(`lob-tone-${t}`);
  }

  function setDotClass(el, cls) {
    if (!el) return;
    if (el.dataset.dot === cls) return;
    el.dataset.dot = cls;
    el.className = `lob-status-dot ${cls}`;
  }

  function setHidden(el, hidden) {
    if (!el) return;
    if (Boolean(el.hidden) === Boolean(hidden)) return;
    el.hidden = Boolean(hidden);
  }

  function paintZoneCard(name, payload) {
    if (!payload) return;
    const root = cards[name] && cards[name].root;
    if (!root) return;

    const detected = payload.detected === true;
    if (name === "support2") {
      setHidden(root, !detected);
      setHidden(support2Fallback, detected);
      if (!detected) return;
    }

    const tone = payload.arrow_tone || "neutral";
    const readingTone = payload.tone || "neutral";
    const arrow = payload.arrow || "→";

    setText(field(`${name}-distance`), payload.distance_badge || "ABSTAND —");
    setText(field(`${name}-previous`), payload.prev_price || "NOCH KEIN VERGLEICH");
    setText(field(`${name}-current`), payload.curr_price || "NICHT ERKANNT");
    setText(field(`${name}-movement`), payload.move_pct_display || "NOCH KEIN VERGLEICH");
    setText(field(`${name}-arrow-1`), "→");
    setText(field(`${name}-arrow-2`), arrow);
    setTone(field(`${name}-arrow-2`), tone);
    setTone(field(`${name}-movement`), tone);
    setText(field(`${name}-reading`), payload.headline || "—");
    setTone(field(`${name}-reading`), readingTone);

    const metrics = Array.isArray(payload.metrics) ? payload.metrics : [];
    const byKey = {};
    metrics.forEach((m) => {
      if (m && m.key) byKey[m.key] = m.value;
    });
    setText(field(`${name}-strength`), byKey.strength != null ? byKey.strength : "—");
    setText(field(`${name}-notional`), byKey.notional != null ? byKey.notional : "—");
    setText(field(`${name}-change`), byKey.change != null ? byKey.change : "SAMMELT DATEN");
  }

  function paintCard(name, payload) {
    if (LEVEL_CARDS.has(name)) {
      paintZoneCard(name, payload);
      return;
    }

    const card = cards[name];
    if (!card || !payload) return;

    if (card.title && payload.section_title) setText(card.title, payload.section_title);
    setText(card.headline, payload.headline || "—");
    setTone(card.headline, payload.tone || "neutral");

    if (card.move) {
      setText(card.move, payload.move_line || "—");
      setTone(card.move, payload.arrow_tone || "neutral");
    }

    if (card.tech) {
      const techText = payload.tech ? String(payload.tech) : "";
      setText(card.tech, techText);
      card.tech.classList.toggle("lob-tech-empty", !techText);
    }

    const metrics = Array.isArray(payload.metrics) ? payload.metrics : [];
    card.metrics.forEach((el, idx) => {
      const item = metrics[idx];
      if (!item || item.value == null) return; // sticky: do not blank metrics on partial card
      setText(el, item.value);
    });

    if (name === "overall") {
      const fors = Array.isArray(payload.reasons_for) ? payload.reasons_for : [];
      const against = Array.isArray(payload.reasons_against) ? payload.reasons_against : [];
      card.reasonsFor.forEach((li, idx) => {
        if (idx < fors.length) {
          li.hidden = false;
          setText(li, fors[idx]);
        } else if (idx === 0) {
          li.hidden = false;
          if (!li.textContent || li.textContent === "—") setText(li, "—");
        } else {
          li.hidden = true;
        }
      });
      card.reasonsAgainst.forEach((li, idx) => {
        if (idx < against.length) {
          li.hidden = false;
          setText(li, against[idx]);
        } else if (idx === 0) {
          li.hidden = false;
          if (!li.textContent || li.textContent === "—") setText(li, "—");
        } else {
          li.hidden = true;
        }
      });
      setText(card.decision, payload.decision || "ABWARTEN");
      setTone(card.decision, payload.tone || "mixed");
    }
  }

  function setBusy(on, label) {
    busy = on;
    applyButtonState(els.statusText.textContent || "STOPPED");
    if (on && label) setText(els.start, label);
    else if (!busy) setText(els.start, START_LABEL);
  }

  function applyButtonState(status) {
    const active = ACTIVE_STATUSES.has(status);
    const stopped = TERMINAL_STATUSES.has(status) || !status;
    els.start.disabled = busy || active;
    els.stop.disabled = busy || stopped || status === "STOPPING";
    const hasSymbol =
      (els.watchSymbol.textContent || "").trim() &&
      els.watchSymbol.textContent.trim() !== "—";
    els.restart.disabled = busy || (!active && stopped && !hasSymbol);
    if (!busy) setText(els.start, START_LABEL);
  }

  function statusDotClass(status) {
    if (["LIVE", "BOOTSTRAP_OK"].includes(status)) return "lob-status-dot-live";
    if (["STARTING", "WAITING_FOR_DATA", "RECONNECTING", "STOPPING"].includes(status)) {
      return "lob-status-dot-wait";
    }
    if (["STALE_DATA", "GAP_DETECTED", "FAILED"].includes(status)) return "lob-status-dot-warn";
    return "lob-status-dot-stopped";
  }

  function fmtNum(v, digits) {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return null;
    return Number(v).toFixed(digits == null ? 4 : digits);
  }

  function isEditingControls() {
    const active = document.activeElement;
    return active === els.symbol || active === els.window;
  }

  function statusOf(payload) {
    return (payload && payload.status && payload.status.status) || "";
  }

  /**
   * A snapshot is complete enough to replace the painted UI.
   * Incomplete LIVE payloads (missing mid/display) must not wipe lastGood.
   */
  function isCompleteSnapshot(payload) {
    if (!payload || typeof payload !== "object") return false;
    const view = payload.view;
    if (!view || typeof view !== "object") return false;
    if (!view.display || typeof view.display !== "object") return false;

    const st = statusOf(payload);
    if (TERMINAL_STATUSES.has(st)) return true;

    if (view.mid_price === null || view.mid_price === undefined) return false;
    if (Number.isNaN(Number(view.mid_price))) return false;

    // Need concrete level display objects (even if undetected)
    if (!view.display.resistance || !view.display.support) return false;
    return true;
  }

  function isLevelPlaceholder(card) {
    if (!card || typeof card !== "object") return true;
    if (card.detected === true) return false;
    const cur = String(card.curr_price || "");
    return !cur || cur === "NICHT ERKANNT" || cur === "—";
  }

  /**
   * While runner is active, keep last detected level cards if the new payload
   * briefly reports undetected/placeholder (avoids NICHT ERKANNT flash).
   */
  function coalesceDisplay(prevDisplay, nextDisplay, status) {
    if (!nextDisplay) return prevDisplay || {};
    if (!prevDisplay || !ACTIVE_STATUSES.has(status)) return nextDisplay;

    const out = Object.assign({}, nextDisplay);
    ["resistance", "support", "support2"].forEach((key) => {
      const prev = prevDisplay[key];
      const next = nextDisplay[key];
      if (prev && !isLevelPlaceholder(prev) && isLevelPlaceholder(next)) {
        out[key] = prev;
      }
    });
    return out;
  }

  function applyStatusChrome(payload) {
    const status = payload.status || {};
    const st = status.status || "STOPPED";
    setDotClass(els.statusDot, statusDotClass(st));
    setText(els.statusText, st);
    applyButtonState(st);
  }

  function fmtPriceSmart(price) {
    if (price == null || !Number.isFinite(Number(price))) return "—";
    const p = Number(price);
    const abs = Math.abs(p);
    let digits = 4;
    if (abs >= 1000) digits = 2;
    else if (abs >= 100) digits = 3;
    else if (abs >= 1) digits = 4;
    else if (abs >= 0.1) digits = 5;
    else digits = 6;
    return p.toFixed(digits);
  }

  function fmtSignedPct(pct) {
    if (pct == null || !Number.isFinite(Number(pct))) return "—";
    const v = Number(pct);
    const sign = v > 0 ? "+" : "";
    return `${sign}${v.toFixed(2)} %`;
  }

  function fmtBpsAbs(bps) {
    if (bps == null || !Number.isFinite(Number(bps))) return "—";
    return `${Math.round(Math.abs(Number(bps)))} bps`;
  }

  function policyBadges(policies) {
    const list = Array.isArray(policies) ? policies : [];
    return list
      .map((p) => {
        const cls =
          p === "SECOND_RELEVANT"
            ? "lob-ob-badge lob-ob-badge-second"
            : p === "NEAREST_RELEVANT"
              ? "lob-ob-badge lob-ob-badge-nearest"
              : p === "STRONGEST_RELEVANT"
                ? "lob-ob-badge lob-ob-badge-strong"
                : p === "NEAR_MID"
                  ? "lob-ob-badge lob-ob-badge-nearmid"
                  : "lob-ob-badge";
        return `<span class="${cls}">${p}</span>`;
      })
      .join("");
  }

  function wallClassLabel(wc) {
    if (wc === "STRONG_WALL") return "starke Wall";
    if (wc === "WEAK_CANDIDATE") return "schwacher Kandidat";
    return wc || "";
  }

  function levelRowHtml(level, prefix) {
    const policies = Array.isArray(level.policies) ? level.policies : [];
    const isNearMid = policies.indexOf("NEAR_MID") >= 0;
    const isStrongest = policies.indexOf("STRONGEST_RELEVANT") >= 0;
    const rank = level.rank_by_distance != null ? level.rank_by_distance : "?";
    const idLabel = isNearMid
      ? `N${rank}`
      : `${prefix}${rank}`;
    const price = fmtPriceSmart(level.price);
    const pct = fmtSignedPct(level.distance_pct);
    const bps = fmtBpsAbs(level.distance_bps);
    const mult =
      level.multiple != null && Number.isFinite(Number(level.multiple))
        ? `${Number(level.multiple).toFixed(2)}×`
        : "—";
    const status = level.status || "ACTIVE";
    const wc = wallClassLabel(level.wall_class);
    const badges = policyBadges(policies);
    const weakCls = level.wall_class === "WEAK_CANDIDATE" ? " lob-ob-level-weak" : "";
    const strongCls = isStrongest ? " lob-ob-level-strongest" : "";
    const nearCls = isNearMid ? " lob-ob-level-nearmid" : "";
    const notional =
      level.notional != null && Number.isFinite(Number(level.notional))
        ? Number(level.notional) >= 1000
          ? `${(Number(level.notional) / 1000).toFixed(0)}k`
          : Number(level.notional).toFixed(0)
        : null;
    return (
      `<div class="lob-ob-level${weakCls}${strongCls}${nearCls}" data-rank="${rank}">` +
      `<div class="lob-ob-level-main">` +
      `<span class="lob-ob-level-id">${idLabel}</span>` +
      `<span class="lob-ob-level-price">${price}</span>` +
      `<span class="lob-ob-level-pct">${pct}</span>` +
      `</div>` +
      `<div class="lob-ob-level-meta">${bps} · Stärke ${mult}` +
      (notional ? ` · ${notional}` : "") +
      (wc ? ` · ${wc}` : "") +
      ` · ${status}</div>` +
      (badges ? `<div class="lob-ob-level-badges">${badges}</div>` : "") +
      `</div>`
    );
  }

  function pickDisplayLevels(g, side) {
    const displayKey = side === "ask" ? "display_ask_levels" : "display_bid_levels";
    const compactKey = side === "ask" ? "compact_ask_levels" : "compact_bid_levels";
    const allKey = side === "ask" ? "ask_levels" : "bid_levels";
    const fallbackN = side === "ask" ? 3 : 4;
    if (Array.isArray(g[displayKey]) && g[displayKey].length) return g[displayKey];
    if (Array.isArray(g[compactKey]) && g[compactKey].length) return g[compactKey];
    if (Array.isArray(g[allKey])) return g[allKey].slice(0, fallbackN);
    return [];
  }

  function renderNearMid(el, walls, sideLabel) {
    if (!el) return;
    const rows = Array.isArray(walls) ? walls : [];
    if (!rows.length) {
      el.hidden = true;
      el.innerHTML = "";
      return;
    }
    el.hidden = false;
    const prefix = "N";
    // Ask stack: farther first visually (above mid)
    const visual = sideLabel === "ask" ? rows.slice().reverse() : rows;
    el.innerHTML =
      `<div class="lob-ob-near-mid-label">NEAR MID ${sideLabel === "ask" ? "ASK" : "BID"} <span class="lob-ob-grid-side-hint">&lt;100 bps</span></div>` +
      visual.map((L) => levelRowHtml(L, prefix)).join("");
  }

  function renderObGrid(grid) {
    if (!obGridEls.root) return;

    // Sticky: keep last good grid if this poll has none (gap / start)
    const g = grid && typeof grid === "object" ? grid : lastPaintedObGrid;
    if (!g) {
      setText(field("grid-status"), "—");
      setText(field("grid-snapshot"), "—");
      setText(field("grid-depth-bid"), "—");
      setText(field("grid-depth-ask"), "—");
      setText(field("grid-raw"), "—");
      setText(field("grid-zones"), "—");
      if (obGridEls.askLevels) obGridEls.askLevels.innerHTML = "";
      if (obGridEls.bidLevels) obGridEls.bidLevels.innerHTML = "";
      renderNearMid(obGridEls.nearMidAsk, [], "ask");
      renderNearMid(obGridEls.nearMidBid, [], "bid");
      setText(obGridEls.mid, "—");
      if (obGridEls.empty) {
        obGridEls.empty.hidden = false;
        setText(obGridEls.empty, "Kein Grid-Snapshot (Runner starten).");
      }
      return;
    }

    if (grid && typeof grid === "object") lastPaintedObGrid = grid;

    const status = g.status || "—";
    setText(field("grid-status"), status);
    const snapTs = g.snapshot_ts || "—";
    setText(field("grid-snapshot"), snapTs);
    const vd = g.visible_depth || {};
    setText(
      field("grid-depth-bid"),
      vd.bid_bps != null && Number.isFinite(Number(vd.bid_bps))
        ? Number(vd.bid_bps).toFixed(0)
        : "—"
    );
    setText(
      field("grid-depth-ask"),
      vd.ask_bps != null && Number.isFinite(Number(vd.ask_bps))
        ? Number(vd.ask_bps).toFixed(0)
        : "—"
    );
    const raw = g.raw_level_counts || {};
    const cands = g.candidate_counts || {};
    const strong = g.strong_wall_counts || {};
    setText(
      field("grid-raw"),
      `Bid ${raw.bid != null ? raw.bid : "—"} / Ask ${raw.ask != null ? raw.ask : "—"}`
    );
    setText(
      field("grid-zones"),
      `Bid ${cands.bid != null ? cands.bid : 0} (stark ${strong.bid != null ? strong.bid : 0})` +
        ` / Ask ${cands.ask != null ? cands.ask : 0} (stark ${strong.ask != null ? strong.ask : 0})`
    );

    const midText = fmtPriceSmart(g.mid_price);
    setText(obGridEls.mid, midText);

    const asks = pickDisplayLevels(g, "ask");
    const bids = pickDisplayLevels(g, "bid");

    // Ask: farthest first visually (top of resistance stack)
    const asksVisual = asks.slice().reverse();
    if (obGridEls.askLevels) {
      obGridEls.askLevels.innerHTML = asksVisual.length
        ? asksVisual.map((L) => levelRowHtml(L, "A")).join("")
        : `<div class="lob-ob-level-empty">Keine Ask-Zonen im Band</div>`;
    }
    if (obGridEls.bidLevels) {
      obGridEls.bidLevels.innerHTML = bids.length
        ? bids.map((L) => levelRowHtml(L, "B")).join("")
        : `<div class="lob-ob-level-empty">Keine Bid-Zonen im Band</div>`;
    }

    renderNearMid(obGridEls.nearMidAsk, g.near_mid_ask_walls, "ask");
    renderNearMid(obGridEls.nearMidBid, g.near_mid_bid_walls, "bid");

    const insufficient = status === "INSUFFICIENT_VISIBLE_DEPTH";
    if (obGridEls.empty) {
      if (insufficient) {
        const band = g.search_band_bps || {};
        obGridEls.empty.hidden = false;
        setText(
          obGridEls.empty,
          `INSUFFICIENT_VISIBLE_DEPTH — sichtbare Bid-Tiefe ${
            vd.bid_bps != null ? Number(vd.bid_bps).toFixed(1) : "—"
          } bps, Ask-Tiefe ${
            vd.ask_bps != null ? Number(vd.ask_bps).toFixed(1) : "—"
          } bps; Suchband ${band.min != null ? band.min : 100}–${
            band.max != null ? band.max : 300
          } bps nicht sichtbar.`
        );
      } else if (status === "ERROR") {
        obGridEls.empty.hidden = false;
        setText(obGridEls.empty, "Grid-Fehler — Anzeige übersprungen.");
      } else {
        obGridEls.empty.hidden = true;
        setText(obGridEls.empty, "");
      }
    }

    // Compact ladder + extra levels under details
    if (obGridEls.ladder) {
      const ladderParts = [];
      asksVisual.forEach((L) => {
        const tag =
          Array.isArray(L.policies) && L.policies.indexOf("STRONGEST_RELEVANT") >= 0
            ? " ★"
            : "";
        ladderParts.push(
          `<div class="lob-ob-ladder-row lob-ob-ladder-ask">A${L.rank_by_distance}${tag} · ${fmtPriceSmart(
            L.price
          )} · ${fmtSignedPct(L.distance_pct)}</div>`
        );
      });
      const nearAsk = Array.isArray(g.near_mid_ask_walls) ? g.near_mid_ask_walls.slice().reverse() : [];
      nearAsk.forEach((L) => {
        ladderParts.push(
          `<div class="lob-ob-ladder-row lob-ob-ladder-nearmid">N${L.rank_by_distance} ask · ${fmtPriceSmart(
            L.price
          )} · ${fmtSignedPct(L.distance_pct)}</div>`
        );
      });
      ladderParts.push(
        `<div class="lob-ob-ladder-mid">── ${midText} ──</div>`
      );
      const nearBid = Array.isArray(g.near_mid_bid_walls) ? g.near_mid_bid_walls : [];
      nearBid.forEach((L) => {
        ladderParts.push(
          `<div class="lob-ob-ladder-row lob-ob-ladder-nearmid">N${L.rank_by_distance} bid · ${fmtPriceSmart(
            L.price
          )} · ${fmtSignedPct(L.distance_pct)}</div>`
        );
      });
      bids.forEach((L) => {
        const tag =
          Array.isArray(L.policies) && L.policies.indexOf("STRONGEST_RELEVANT") >= 0
            ? " ★"
            : "";
        ladderParts.push(
          `<div class="lob-ob-ladder-row lob-ob-ladder-bid">B${L.rank_by_distance}${tag} · ${fmtPriceSmart(
            L.price
          )} · ${fmtSignedPct(L.distance_pct)}</div>`
        );
      });
      obGridEls.ladder.innerHTML = ladderParts.join("");
    }

    if (obGridEls.extra) {
      const allAsk = Array.isArray(g.ask_levels) ? g.ask_levels : [];
      const allBid = Array.isArray(g.bid_levels) ? g.bid_levels : [];
      const shownAsk = new Set(
        asks.map((L) => `${L.rank_by_distance}|${L.price}`)
      );
      const shownBid = new Set(
        bids.map((L) => `${L.rank_by_distance}|${L.price}`)
      );
      const extraAsk = allAsk.filter(
        (L) => !shownAsk.has(`${L.rank_by_distance}|${L.price}`)
      );
      const extraBid = allBid.filter(
        (L) => !shownBid.has(`${L.rank_by_distance}|${L.price}`)
      );
      if (!extraAsk.length && !extraBid.length) {
        obGridEls.extra.innerHTML =
          `<div class="lob-ob-level-empty">Keine weiteren Kandidaten (Kandidaten ≠ Richtungsanzeige).</div>`;
      } else {
        const bits = [];
        if (extraAsk.length) {
          bits.push("<div class=\"lob-ob-grid-side-label\">Weitere Ask</div>");
          bits.push(extraAsk.map((L) => levelRowHtml(L, "A")).join(""));
        }
        if (extraBid.length) {
          bits.push("<div class=\"lob-ob-grid-side-label\">Weitere Bid</div>");
          bits.push(extraBid.map((L) => levelRowHtml(L, "B")).join(""));
        }
        obGridEls.extra.innerHTML = bits.join("");
      }
    }
  }

  function renderSnapshot(payload) {
    const status = payload.status || {};
    const runner = status.runner || {};
    const view = payload.view || {};
    const st = status.status || "STOPPED";
    // CRITICAL: coalesce against last *painted* display, not raw lastGood.view.display.
    // Raw lastGood already loses detected levels on the first gap frame; using it as
    // prev makes sticky coalesce fail on the next poll (proven clear path).
    const display = coalesceDisplay(lastPaintedDisplay, view.display || {}, st);

    applyStatusChrome(payload);

    const sym =
      view.symbol ||
      runner.symbol ||
      String(els.symbol.value || "").trim().toUpperCase() ||
      "—";
    setText(els.watchSymbol, sym);
    if (display.window_label) setText(els.windowLabel, display.window_label);
    if (view.state) setText(els.state, view.state);
    if (view.setup || view.decision) setText(els.decision, view.setup || view.decision);

    const midText = fmtNum(view.mid_price);
    if (midText != null) setText(els.mid, midText);

    if (display.data_age_label) setText(els.dataAge, display.data_age_label);
    if (display.level_source || display.sample_label) {
      const src = display.level_source === "report" ? "Report" : display.sample_label || "5s";
      setText(els.levelSource, src);
    }

    CARD_NAMES.forEach((key) => paintCard(key, display[key]));
    lastPaintedDisplay = display;

    renderObGrid(view.ob_grid || null);

    if (!isEditingControls()) {
      if (runner.symbol && els.symbol.value !== runner.symbol) {
        els.symbol.value = runner.symbol;
      }
      if (
        runner.report_interval_seconds &&
        els.window.value !== String(runner.report_interval_seconds)
      ) {
        els.window.value = String(runner.report_interval_seconds);
      }
    }
  }

  async function api(path, options) {
    const res = await fetch(path, {
      credentials: "same-origin",
      cache: "no-store",
      headers: { "Content-Type": "application/json", ...(options && options.headers) },
      ...options,
    });
    if (res.status === 401) {
      window.location.href = "/login";
      throw new Error("unauthorized");
    }
    const data = await res.json().catch(() => null);
    if (!res.ok) {
      throw new Error((data && (data.error || data.detail)) || `HTTP ${res.status}`);
    }
    if (!data || typeof data !== "object") {
      throw new Error("empty snapshot");
    }
    return data;
  }

  async function refresh() {
    if (pollInFlight) return;
    pollInFlight = true;
    try {
      const snap = await api("/api/live-orderbook/snapshot");
      if (!isCompleteSnapshot(snap)) {
        // Keep last painted values; only refresh status chrome if we already have data.
        if (lastGoodSnapshot) {
          applyStatusChrome(snap);
          return;
        }
        // First paint with nothing usable yet — still avoid inventing clears.
        applyStatusChrome(snap);
        return;
      }
      renderSnapshot(snap);
      lastGoodSnapshot = snap;
    } catch (err) {
      console.error(err);
      // On error: do not clear / reset to placeholders.
    } finally {
      pollInFlight = false;
    }
  }

  async function onStart() {
    if (busy) return;
    const symbol = String(els.symbol.value || "").trim().toUpperCase();
    els.symbol.value = symbol;
    setBusy(true, "STARTET...");
    try {
      const data = await api("/api/live-orderbook/start", {
        method: "POST",
        body: JSON.stringify({
          symbol,
          report_interval_seconds: Number(els.window.value),
        }),
      });
      if (!data.success) throw new Error(data.error || "start failed");
      lastGoodSnapshot = null;
      lastPaintedDisplay = null; // new run — allow fresh first complete paint
      lastPaintedObGrid = null;
    } catch (err) {
      alert(String(err.message || err));
    } finally {
      setBusy(false);
      await refresh();
    }
  }

  async function onStop() {
    if (busy) return;
    setBusy(true);
    try {
      const data = await api("/api/live-orderbook/stop", { method: "POST", body: "{}" });
      if (!data.success) throw new Error(data.error || "stop failed");
    } catch (err) {
      alert(String(err.message || err));
    } finally {
      setBusy(false);
      await refresh();
    }
  }

  async function onRestart() {
    if (busy) return;
    const symbol = String(els.symbol.value || "").trim().toUpperCase();
    setBusy(true, "STARTET...");
    try {
      const data = await api("/api/live-orderbook/restart", {
        method: "POST",
        body: JSON.stringify({
          symbol,
          report_interval_seconds: Number(els.window.value),
        }),
      });
      if (!data.success) throw new Error(data.error || "restart failed");
      lastGoodSnapshot = null;
      lastPaintedDisplay = null;
      lastPaintedObGrid = null;
    } catch (err) {
      alert(String(err.message || err));
    } finally {
      setBusy(false);
      await refresh();
    }
  }

  function pollIntervalMs() {
    // UI-Refresh unabhängig vom Report-Fenster (Runner: 1m/2m/5m).
    return 3000;
  }

  let pollTimer = null;
  function startPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(refresh, pollIntervalMs());
  }

  els.start.addEventListener("click", onStart);
  els.stop.addEventListener("click", onStop);
  els.restart.addEventListener("click", onRestart);
  els.symbol.addEventListener("blur", () => {
    els.symbol.value = String(els.symbol.value || "").trim().toUpperCase();
  });

  refresh();
  startPolling();
})();
