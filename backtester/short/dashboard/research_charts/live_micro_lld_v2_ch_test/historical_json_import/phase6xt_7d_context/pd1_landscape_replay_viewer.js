/* PD1 Landscape Replay — chart viewer v2 (axes, price, zoom) */
(function () {
  const REPLAY_GEN = "phase6xt_axis_density_render_v1";
  const ZOOM_DRAG_SENS = 0.0042;
  const MIN_Y_SPAN = 0.00006;
  const MIN_T_SPAN_MS = 15 * 60 * 1000;
  const FIVE_MIN_MS = 5 * 60 * 1000;
  const canvas = document.getElementById("c");
  const ctx = canvas.getContext("2d");
  const detail = document.getElementById("detail");
  const modeSel = document.getElementById("mode");
  const asofInput = document.getElementById("asof");
  const showEvents = document.getElementById("showEvents");
  const toolbar = document.getElementById("toolbar");

  const M = { left: 58, right: 72, top: 24, bottom: 40 };

  let meta, prices, pools, events, clustersByDc;
  let fullExtents = { t0: 0, t1: 1, poolMin: 0, poolMax: 1, priceMin: 0, priceMax: 0 };
  let view = { t0: 0, t1: 1, y0: 0, y1: 1 };
  let dragging = false;
  /** @type {"time"|"pricePan"|"priceZoom"|"timeZoom"} locked at pointerdown */
  let dragMode = "time";
  /** @type {{ px?: number, py?: number, span: number, anchor: number, anchorRatio: number } | null} */
  let axisZoomStart = null;
  /** When true, skip autoFitPriceY on pan end (manual Y via Shift+drag or Shift+wheel). */
  let manualPriceY = false;
  let dragMovedPx = 0;
  let lastXY = [0, 0];
  let mouse = { x: null, y: null, inside: false };
  let tCursor = 0;

  function parseIso(s) {
    return new Date(String(s).replace("Z", "+00:00")).getTime();
  }

  function isoFromMs(ms) {
    return new Date(ms).toISOString().replace(".000", "");
  }

  function cssSize() {
    const wrap = document.getElementById("canvas-wrap");
    return { w: wrap ? wrap.clientWidth : canvas.width, h: wrap ? wrap.clientHeight : canvas.height };
  }

  function plotW() {
    return Math.max(10, cssSize().w - M.left - M.right);
  }

  function plotH() {
    return Math.max(10, cssSize().h - M.top - M.bottom);
  }

  function tToX(t) {
    return M.left + ((t - view.t0) / (view.t1 - view.t0)) * plotW();
  }

  function yToPx(y) {
    return M.top + ((view.y1 - y) / (view.y1 - view.y0)) * plotH();
  }

  function pxToT(x) {
    return view.t0 + ((x - M.left) / plotW()) * (view.t1 - view.t0);
  }

  function pxToY(py) {
    return view.y1 - ((py - M.top) / plotH()) * (view.y1 - view.y0);
  }

  function plotRightX() {
    return M.left + plotW();
  }

  function plotBottomY() {
    return M.top + plotH();
  }

  /** Right price-axis band — drag here scales Y (Research LWC axisPressedMouseMove). */
  function isPriceAxisX(x) {
    const pr = plotRightX();
    return x >= pr && x <= pr + M.right;
  }

  /** Bottom time-axis band — drag here scales T. */
  function isTimeAxisHit(x, y) {
    return y >= plotBottomY() && y <= plotBottomY() + M.bottom && x >= M.left && x <= plotRightX();
  }

  function maxYSpan() {
    const band = yBandMax() - yBandMin();
    return Math.max(MIN_Y_SPAN * 4, band > 0 ? band * 1.25 : 1);
  }

  function maxTSpanMs() {
    const { lo, hi } = timePanLoHi();
    return Math.max(MIN_T_SPAN_MS, hi - lo);
  }

  function clampViewY() {
    const span = view.y1 - view.y0;
    if (!Number.isFinite(span) || span <= 0) return;
    let s = Math.max(MIN_Y_SPAN, Math.min(maxYSpan(), span));
    const mid = (view.y0 + view.y1) / 2;
    view.y0 = mid - s / 2;
    view.y1 = mid + s / 2;
  }

  function clampViewT() {
    let span = view.t1 - view.t0;
    if (!Number.isFinite(span) || span <= 0) return;
    const { lo, hi } = timePanLoHi();
    span = Math.max(MIN_T_SPAN_MS, Math.min(maxTSpanMs(), span));
    const mid = (view.t0 + view.t1) / 2;
    let t0 = mid - span / 2;
    let t1 = mid + span / 2;
    if (t0 < lo) {
      t0 = lo;
      t1 = t0 + span;
    }
    if (t1 > hi) {
      t1 = hi;
      t0 = t1 - span;
    }
    view.t0 = t0;
    view.t1 = t1;
  }

  function applyAxisZoomFromDrag(mode, x, y) {
    if (!axisZoomStart) return;
    if (mode === "priceZoom") {
      const dy = y - axisZoomStart.py;
      const factor = Math.exp(dy * ZOOM_DRAG_SENS);
      let span = axisZoomStart.span * factor;
      span = Math.max(MIN_Y_SPAN, Math.min(maxYSpan(), span));
      const anchor = axisZoomStart.anchor;
      const r = axisZoomStart.anchorRatio;
      view.y0 = anchor - span * r;
      view.y1 = anchor + span * (1 - r);
      clampViewY();
      manualPriceY = true;
      return;
    }
    if (mode === "timeZoom") {
      const dx = x - axisZoomStart.px;
      const factor = Math.exp(-dx * ZOOM_DRAG_SENS);
      let span = axisZoomStart.span * factor;
      span = Math.max(MIN_T_SPAN_MS, Math.min(maxTSpanMs(), span));
      const anchor = axisZoomStart.anchor;
      const r = axisZoomStart.anchorRatio;
      view.t0 = anchor - span * r;
      view.t1 = anchor + span * (1 - r);
      const { lo, hi } = timePanLoHi();
      if (view.t0 < lo) {
        view.t0 = lo;
        view.t1 = lo + span;
      }
      if (view.t1 > hi) {
        view.t1 = hi;
        view.t0 = hi - span;
      }
      clampViewT();
    }
  }

  /** Candle body width from visible time span (not merged prices.length). */
  function candleBodyWidthPx() {
    const spanMs = Math.max(FIVE_MIN_MS, view.t1 - view.t0);
    const barsInView = spanMs / FIVE_MIN_MS;
    const slotPx = plotW() / Math.max(1, barsInView);
    return Math.max(1, Math.min(14, slotPx * 0.72));
  }

  function candleLayoutForBar(b) {
    const t = parseIso(b.t);
    const x = Math.round(tToX(t)) + 0.5;
    const yo = yToPx(b.o);
    const yc = yToPx(b.c);
    const yh = yToPx(b.h);
    const yl = yToPx(b.l);
    const top = Math.min(yo, yc);
    const bot = Math.max(yo, yc);
    const bodyH = Math.max(b.o === b.c ? 1 : 1, bot - top);
    const barW = candleBodyWidthPx();
    return { t, x, yo, yc, yh, yl, top, bot, bodyH, barW };
  }

  function hexToRgba(hex) {
    if (!hex || hex.length < 7) return "rgba(128,128,128,0.25)";
    const r = parseInt(hex.slice(1, 3), 16);
    const g = parseInt(hex.slice(3, 5), 16);
    const b = parseInt(hex.slice(5, 7), 16);
    const a = hex.length >= 9 ? parseInt(hex.slice(7, 9), 16) / 255 : 0.25;
    return `rgba(${r},${g},${b},${a})`;
  }

  function poolActiveAt(pl, t) {
    for (const seg of pl.segments || []) {
      const a = parseIso(seg[0]);
      const b = parseIso(seg[1]);
      if (t >= a && t <= b) return true;
    }
    return false;
  }

  function priceRangeInView() {
    let lo = Infinity;
    let hi = -Infinity;
    let n = 0;
    for (const b of prices || []) {
      const t = parseIso(b.t);
      if (t < view.t0 || t > view.t1) continue;
      n++;
      lo = Math.min(lo, b.l, b.c);
      hi = Math.max(hi, b.h, b.c);
    }
    if (!n || !isFinite(lo)) return null;
    return { lo, hi };
  }

  function recomputePriceTimeSpan() {
    let t0 = Infinity;
    let t1 = -Infinity;
    for (const b of prices || []) {
      const t = parseIso(b.t);
      if (!Number.isFinite(t)) continue;
      t0 = Math.min(t0, t);
      t1 = Math.max(t1, t);
    }
    if (!isFinite(t0)) return { t0: 0, t1: 0 };
    return { t0, t1 };
  }

  function pricesCoverRange(vt0, vt1) {
    if (!prices?.length) return false;
    for (const b of prices) {
      const t = parseIso(b.t);
      if (t >= vt0 && t <= vt1) return true;
    }
    return false;
  }

  function timePanLoHi() {
    if (capabilities?.dynamic_historical && dataBounds.t1 > dataBounds.t0) {
      return { lo: dataBounds.t0, hi: dataBounds.t1 };
    }
    return { lo: fullExtents.t0, hi: fullExtents.t1 };
  }

  function autoFitPriceY() {
    manualPriceY = false;
    const r = priceRangeInView();
    if (!r) {
      return false;
    }
    const pad = Math.max((r.hi - r.lo) * 0.06, 0.00015);
    view.y0 = r.lo - pad;
    view.y1 = r.hi + pad;
    draw();
    return true;
  }

  function panPriceByDelta(dyPx) {
    const dyP = (dyPx / plotH()) * (view.y1 - view.y0);
    view.y0 += dyP;
    view.y1 += dyP;
    manualPriceY = true;
  }

  function fitAllPoolsY() {
    view.y0 = fullExtents.poolMin * 0.9995;
    view.y1 = fullExtents.poolMax * 1.0005;
    draw();
  }

  function resetView() {
    view.t0 = fullExtents.t0;
    view.t1 = fullExtents.t1;
    autoFitPriceY();
  }

  function niceNumber(range, count) {
    const rough = range / count;
    const mag = Math.pow(10, Math.floor(Math.log10(rough)));
    const norm = rough / mag;
    let nice = 1;
    if (norm > 5) nice = 10;
    else if (norm > 2) nice = 5;
    else if (norm > 1) nice = 2;
    return nice * mag;
  }

  function formatPrice(y) {
    if (y >= 1) return y.toFixed(4);
    if (y >= 0.01) return y.toFixed(5);
    return y.toFixed(6);
  }

  function formatTime(ms) {
    const d = new Date(ms);
    const mo = String(d.getUTCMonth() + 1).padStart(2, "0");
    const da = String(d.getUTCDate()).padStart(2, "0");
    const hh = String(d.getUTCHours()).padStart(2, "0");
    const mm = String(d.getUTCMinutes()).padStart(2, "0");
    if (view.t1 - view.t0 > 36 * 3600 * 1000) return `${mo}-${da} ${hh}:${mm}`;
    return `${hh}:${mm}`;
  }

  /** Crosshair / axis hint: full UTC calendar time. */
  function formatCrosshairUtc(ms) {
    const d = new Date(ms);
    const y = d.getUTCFullYear();
    const mo = String(d.getUTCMonth() + 1).padStart(2, "0");
    const da = String(d.getUTCDate()).padStart(2, "0");
    const hh = String(d.getUTCHours()).padStart(2, "0");
    const mm = String(d.getUTCMinutes()).padStart(2, "0");
    const ss = String(d.getUTCSeconds()).padStart(2, "0");
    const span = view.t1 - view.t0;
    if (span <= 2 * 3600 * 1000) return `${y}-${mo}-${da} ${hh}:${mm}:${ss} UTC`;
    return `${y}-${mo}-${da} ${hh}:${mm} UTC`;
  }

  function drawGridAndAxes() {
    const x0 = M.left;
    const y0 = M.top;
    const w = plotW();
    const h = plotH();

    ctx.strokeStyle = "#2a3140";
    ctx.lineWidth = 1;

    const tStep = niceNumber(view.t1 - view.t0, 8);
    let t = Math.ceil(view.t0 / tStep) * tStep;
    ctx.fillStyle = "#9aa3b5";
    ctx.font = "11px system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    for (; t <= view.t1; t += tStep) {
      const x = tToX(t);
      if (x < x0 || x > x0 + w) continue;
      ctx.beginPath();
      ctx.moveTo(x, y0);
      ctx.lineTo(x, y0 + h);
      ctx.stroke();
      ctx.fillText(formatTime(t), x, y0 + h + 6);
    }

    const yStep = niceNumber(view.y1 - view.y0, 7);
    let y = Math.ceil(view.y0 / yStep) * yStep;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    for (; y <= view.y1; y += yStep) {
      const py = yToPx(y);
      if (py < y0 || py > y0 + h) continue;
      ctx.beginPath();
      ctx.moveTo(x0, py);
      ctx.lineTo(x0 + w, py);
      ctx.stroke();
      ctx.fillText(formatPrice(y), x0 + w + 6, py);
    }

    ctx.strokeStyle = "#555";
    ctx.strokeRect(x0, y0, w, h);
  }

  function drawPoolsStrength() {
    const fillMap = meta.FILL_BY_CLASS || {};
    const borders = meta.side_borders || {};
    for (const pl of pools) {
      for (const seg of pl.segments || []) {
        const t0 = parseIso(seg[0]);
        const t1 = parseIso(seg[1]) + 300000;
        if (t1 < view.t0 || t0 > view.t1) continue;
        const x0 = tToX(Math.max(t0, view.t0));
        const x1 = tToX(Math.min(t1, view.t1));
        const yb = yToPx(pl.bottom);
        const yt = yToPx(pl.top);
        const h = yb - yt;
        if (h < 0.5) continue;
        ctx.fillStyle = hexToRgba(fillMap[pl.strength_class] || fillMap.Weak);
        ctx.fillRect(x0, yt, Math.max(1, x1 - x0), h);
        ctx.strokeStyle = borders[pl.side] || "#888";
        ctx.lineWidth = 0.5;
        ctx.strokeRect(x0, yt, Math.max(1, x1 - x0), h);
      }
    }
  }

  const DENSITY_STEP_MS = 5 * 60 * 1000;
  const DC_BAND_CACHE_MAX = 160;
  const DENSITY_PREFETCH_MARGIN_MS = 6 * 3600 * 1000;
  const GOTO_VIEW_PAD_MS = 4 * 3600 * 1000;
  /** @type {Map<number, {lower: object[], upper: object[]}>} keyed by causal decision_close ms */
  const dcBandCache = new Map();
  let densityPrewarmReady = false;
  let densityPrewarmPromise = null;
  let navState = "idle";
  let drawPending = false;
  let detailPending = false;
  let capabilities = null;
  let dataBounds = { t0: 0, t1: 0 };
  let loadedSpan = { t0: 0, t1: 0 };
  /** Bumped only on user navigation (GO TO / bootstrap). Prefetch must not invalidate in-flight GO TO. */
  let dataCommitEpoch = 0;
  let goToNavSeq = 0;
  const NAV_TRACE_MAX = 24;
  const navTrace = [];
  let edgePrefetchTimer = null;
  const yBandMin = () => fullExtents.poolMin * 0.9995;
  const yBandMax = () => fullExtents.poolMax * 1.0005;

  function causalDecisionCloseMs(t) {
    const d = new Date(t);
    const slot = Math.floor(d.getUTCMinutes() / 15) * 15;
    return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), d.getUTCHours(), slot, 0, 0);
  }

  function poolCoversPrice(pl, p) {
    return pl.bottom <= p && p <= pl.top;
  }

  function exactOriginDepthAt(t, p, side) {
    const origins = new Set();
    for (const pl of pools) {
      if (!poolActiveAt(pl, t)) continue;
      if (pl.side !== side) continue;
      if (!poolCoversPrice(pl, p)) continue;
      origins.add(pl.origin);
    }
    return origins.size;
  }

  function poolsCoveringAt(t, p, side) {
    const out = [];
    for (const pl of pools) {
      if (!poolActiveAt(pl, t)) continue;
      if (pl.side !== side) continue;
      if (!poolCoversPrice(pl, p)) out.push(pl);
    }
    return out;
  }

  function computeExactBandsForSide(t, side, yMin, yMax) {
    const active = pools.filter((pl) => poolActiveAt(pl, t) && pl.side === side);
    if (!active.length) return [];
    const events = [];
    for (const pl of active) {
      const lo = Math.max(yMin, pl.bottom);
      const hi = Math.min(yMax, pl.top);
      if (hi < lo) continue;
      events.push({ price: lo, delta: 1, pid: pl.pool_id, origin: pl.origin });
      events.push({ price: hi, delta: -1, pid: pl.pool_id, origin: pl.origin });
    }
    if (!events.length) return [];
    events.sort((a, b) => (a.price === b.price ? b.delta - a.delta : a.price - b.price));
    const activePools = new Map();
    const bands = [];
    let prevPrice = null;
    let prevDepth = 0;
    const depthNow = () => new Set(activePools.values()).size;
    for (const ev of events) {
      if (prevPrice !== null && ev.price > prevPrice && prevDepth > 0) {
        bands.push({ y0: prevPrice, y1: ev.price, depth: prevDepth });
      }
      if (ev.delta > 0) activePools.set(ev.pid, ev.origin);
      else activePools.delete(ev.pid);
      prevDepth = depthNow();
      prevPrice = ev.price;
    }
    const merged = [];
    for (const b of bands) {
      const last = merged[merged.length - 1];
      if (last && last.depth === b.depth && Math.abs(b.y0 - last.y1) < 1e-15) last.y1 = b.y1;
      else merged.push({ ...b });
    }
    return merged;
  }

  function trimDcBandCache() {
    while (dcBandCache.size > DC_BAND_CACHE_MAX) {
      const first = dcBandCache.keys().next().value;
      dcBandCache.delete(first);
    }
  }

  function ensureDcBands(t) {
    const dc = causalDecisionCloseMs(t);
    if (!dcBandCache.has(dc)) {
      const yMin = yBandMin();
      const yMax = yBandMax();
      dcBandCache.set(dc, {
        lower: computeExactBandsForSide(t, "lower", yMin, yMax),
        upper: computeExactBandsForSide(t, "upper", yMin, yMax),
      });
      trimDcBandCache();
    }
    return dcBandCache.get(dc);
  }

  function prewarmDensityRange(tLo, tHi) {
    const yMin = yBandMin();
    const yMax = yBandMax();
    let t = Math.floor(tLo / DENSITY_STEP_MS) * DENSITY_STEP_MS;
    const end = Math.ceil(tHi / DENSITY_STEP_MS) * DENSITY_STEP_MS;
    for (; t <= end; t += DENSITY_STEP_MS) {
      const dc = causalDecisionCloseMs(t);
      if (!dcBandCache.has(dc)) {
        dcBandCache.set(dc, {
          lower: computeExactBandsForSide(t, "lower", yMin, yMax),
          upper: computeExactBandsForSide(t, "upper", yMin, yMax),
        });
        trimDcBandCache();
      }
    }
    densityPrewarmReady = true;
  }

  function getExactBandsCached(t, side) {
    return ensureDcBands(t)[side];
  }

  function prewarmExactDensityCache() {
    if (densityPrewarmPromise) return densityPrewarmPromise;
    densityPrewarmPromise = new Promise((resolve) => {
      const yMin = yBandMin();
      const yMax = yBandMax();
      let t = fullExtents.t0;
      const step = () => {
        const batchEnd = Math.min(fullExtents.t1, t + 40 * DENSITY_STEP_MS);
        for (; t <= batchEnd; t += DENSITY_STEP_MS) {
          const dc = causalDecisionCloseMs(t);
          if (!dcBandCache.has(dc)) {
            dcBandCache.set(dc, {
              lower: computeExactBandsForSide(t, "lower", yMin, yMax),
              upper: computeExactBandsForSide(t, "upper", yMin, yMax),
            });
            trimDcBandCache();
          }
        }
        if (t <= fullExtents.t1) {
          setTimeout(step, 0);
        } else {
          densityPrewarmReady = true;
          resolve();
          requestDraw();
        }
      };
      step();
    });
    return densityPrewarmPromise;
  }

  function maxExactInView(t, side) {
    const bands = getExactBandsCached(t, side).filter((b) => b.y1 > view.y0 && b.y0 < view.y1);
    if (!bands.length) return { max: 0, intervals: [] };
    const peak = Math.max(...bands.map((b) => b.depth));
    const intervals = bands.filter((b) => b.depth === peak).map((b) => ({ y0: b.y0, y1: b.y1 }));
    return { max: peak, intervals };
  }

  function binUnionAt(t, p, side, priceBins) {
    const pStep = (view.y1 - view.y0) / priceBins;
    const bi = Math.min(priceBins - 1, Math.max(0, Math.floor((p - view.y0) / pStep)));
    const origins = new Set();
    for (const pl of pools) {
      if (!poolActiveAt(pl, t)) continue;
      if (pl.side !== side) continue;
      const b0 = Math.max(0, Math.floor((pl.bottom - view.y0) / pStep));
      const b1 = Math.min(priceBins - 1, Math.floor((pl.top - view.y0) / pStep));
      if (bi < b0 || bi > b1) continue;
      origins.add(pl.origin);
    }
    return origins.size;
  }

  function densityBucket(n) {
    if (n <= 0) return -1;
    if (n === 1) return 0;
    if (n <= 3) return 1;
    if (n <= 5) return 2;
    if (n <= 7) return 3;
    return 4;
  }

  /** independent ACTIVE origins — separate palettes for lower / upper */
  function densityFill(side, bucket) {
    const lower = ["rgba(26,35,126,0.35)", "rgba(57,73,171,0.45)", "rgba(92,107,192,0.55)", "rgba(121,134,203,0.65)", "rgba(159,168,218,0.78)"];
    const upper = ["rgba(74,20,140,0.35)", "rgba(106,27,154,0.45)", "rgba(142,36,170,0.55)", "rgba(171,71,188,0.65)", "rgba(206,147,216,0.78)"];
    const pal = side === "upper" ? upper : lower;
    return pal[Math.max(0, Math.min(4, bucket))];
  }

  function drawExactBandsColumn(t, x0, w, side) {
    const bands = getExactBandsCached(t, side).filter((b) => b.y1 > view.y0 && b.y0 < view.y1);
    const half = w / 2;
    const colX = side === "lower" ? x0 : x0 + half;
    const colW = half;
    for (const b of bands) {
      const y0 = Math.max(b.y0, view.y0);
      const y1 = Math.min(b.y1, view.y1);
      const yt = yToPx(y1);
      const yb = yToPx(y0);
      const h = Math.max(0, yb - yt);
      if (h < 0.2) continue;
      ctx.fillStyle = densityFill(side, densityBucket(b.depth));
      ctx.fillRect(colX, yt, colW, h);
    }
  }

  function drawBinUnionDiagnosticColumn(t, x0, w) {
    const priceBins = Math.min(140, Math.max(48, Math.floor(plotH() / 3)));
    const pStep = (view.y1 - view.y0) / priceBins;
    const lower = Array.from({ length: priceBins }, () => new Set());
    const upper = Array.from({ length: priceBins }, () => new Set());
    for (const pl of pools) {
      if (!poolActiveAt(pl, t)) continue;
      const side = pl.side === "upper" ? upper : lower;
      const b0 = Math.max(0, Math.floor((pl.bottom - view.y0) / pStep));
      const b1 = Math.min(priceBins - 1, Math.floor((pl.top - view.y0) / pStep));
      for (let bi = b0; bi <= b1; bi++) side[bi].add(pl.origin);
    }
    for (let bi = 0; bi < priceBins; bi++) {
      const nL = lower[bi].size;
      const nU = upper[bi].size;
      if (!nL && !nU) continue;
      const y = view.y0 + bi * pStep;
      const yt = yToPx(y + pStep);
      const yb = yToPx(y);
      const h = yb - yt;
      if (nL) {
        ctx.strokeStyle = "rgba(100,181,246,0.55)";
        ctx.strokeRect(x0, yt, w * 0.52, h);
      }
      if (nU) {
        ctx.strokeStyle = "rgba(186,104,200,0.55)";
        ctx.strokeRect(x0 + w * 0.48, yt, w * 0.52, h);
      }
    }
  }

  function drawOriginDensity() {
    if (!pools?.length) return;
    const tStart = Math.floor(view.t0 / DENSITY_STEP_MS) * DENSITY_STEP_MS;
    const pxPerCol = (DENSITY_STEP_MS / (view.t1 - view.t0)) * plotW();
    let step = DENSITY_STEP_MS;
    if (pxPerCol < 0.75) step = DENSITY_STEP_MS * Math.max(1, Math.ceil(0.75 / pxPerCol));
    const runs = [];
    let run = null;
    for (let t = tStart; t <= view.t1; t += step) {
      const xLeft = Math.floor(tToX(t));
      const xRight = Math.ceil(tToX(Math.min(t + step, view.t1)));
      const w = Math.max(1, xRight - xLeft);
      const dc = causalDecisionCloseMs(t);
      if (run && run.dc === dc && step === DENSITY_STEP_MS) {
        run.w = xLeft + w - run.x0;
        run.tEnd = t;
      } else {
        if (run) runs.push(run);
        run = { dc, t, tEnd: t, x0: xLeft, w };
      }
    }
    if (run) runs.push(run);
    for (let i = 0; i < runs.length - 1; i++) {
      const gap = runs[i + 1].x0 - (runs[i].x0 + runs[i].w);
      if (gap > 0 && gap <= 2) runs[i].w += gap;
    }
    for (const col of runs) {
      const tDraw = col.t;
      drawExactBandsColumn(tDraw, col.x0, col.w, "lower");
      drawExactBandsColumn(tDraw, col.x0, col.w, "upper");
      if (document.getElementById("showBinUnion")?.checked) {
        drawBinUnionDiagnosticColumn(tDraw, col.x0, col.w);
      }
    }
    drawDensityLegend();
  }

  function drawDensityLegend() {
    const x = M.left + 8;
    const y = M.top + 6;
    ctx.font = "10px system-ui,sans-serif";
    ctx.textAlign = "left";
    ctx.textBaseline = "top";
    ctx.fillStyle = "rgba(20,24,32,0.88)";
    ctx.fillRect(x, y, 248, 88);
    ctx.fillStyle = "#ccc";
    ctx.fillText("B — exact independent origin depth @ price", x + 6, y + 4);
    const labels = ["1", "2–3", "4–5", "6–7", "8+"];
    for (let i = 0; i < 5; i++) {
      ctx.fillStyle = densityFill("lower", i);
      ctx.fillRect(x + 6, y + 18 + i * 10, 10, 8);
      ctx.fillStyle = densityFill("upper", i);
      ctx.fillRect(x + 22, y + 18 + i * 10, 10, 8);
      ctx.fillStyle = "#aaa";
      ctx.fillText(labels[i], x + 38, y + 17 + i * 10);
    }
    ctx.fillStyle = "#888";
    ctx.fillText("blau=lower  violett=upper (pixel = exakte Intervalle)", x + 6, y + 68);
    ctx.fillText("Diagnose-Checkbox ≠ exakte Tiefe", x + 6, y + 80);
  }

  function drawCandlesticks() {
    const barW = candleBodyWidthPx();
    for (const b of prices) {
      const t = parseIso(b.t);
      if (t < view.t0 || t > view.t1) continue;
      const lay = candleLayoutForBar(b);
      const up = b.c >= b.o;
      const stroke = up ? "#26a69a" : "#ef5350";
      const fill = up ? "rgba(38,166,154,0.92)" : "rgba(239,83,80,0.92)";
      ctx.strokeStyle = stroke;
      ctx.fillStyle = fill;
      ctx.lineWidth = 1;
      const wickX = lay.x;
      ctx.beginPath();
      ctx.moveTo(wickX, lay.yh);
      ctx.lineTo(wickX, lay.yl);
      ctx.stroke();
      const bodyW = Math.max(1, barW);
      const bodyX = lay.x - bodyW / 2;
      if (b.o === b.c) {
        ctx.beginPath();
        ctx.moveTo(bodyX, lay.yo);
        ctx.lineTo(bodyX + bodyW, lay.yo);
        ctx.stroke();
      } else {
        ctx.fillRect(bodyX, lay.top, bodyW, Math.max(1, lay.bodyH));
      }
    }
  }

  /** Disabled in landscape viewer — white close overlay obscures density. */
  function drawWhiteClosePriceLine() {
    return;
  }

  function drawCandlesAndPrice() {
    drawCandlesticks();
    drawWhiteClosePriceLine();
  }

  function drawEvents() {
    if (!showEvents.checked) return;
    const col = {
      POOL_ADDED: "#4caf50",
      POOL_REMOVED: "#f44336",
      CLUSTER_JOINED: "#ffeb3b",
      CLUSTER_SPLIT: "#ff9800",
      GAP_NARROWED: "#00bcd4",
      GAP_WIDENED: "#e040fb",
    };
    const yEv = M.top + plotH() - 6;
    for (const e of events) {
      const t = parseIso(e.asof_utc);
      if (t < view.t0 || t > view.t1) continue;
      const x = tToX(t);
      ctx.fillStyle = col[e.label] || "#aaa";
      ctx.beginPath();
      ctx.arc(x, yEv, 2.5, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  function drawAsofMarkers() {
    if (!tCursor) return;
    const x = tToX(tCursor);
    if (x >= M.left && x <= M.left + plotW()) {
      ctx.strokeStyle = "rgba(255,235,59,0.9)";
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(x, M.top);
      ctx.lineTo(x, M.top + plotH());
      ctx.stroke();
      ctx.setLineDash([]);
    }
    let close = null;
    for (const b of prices) {
      const t = parseIso(b.t);
      if (t <= tCursor) close = b.c;
    }
    if (close != null) {
      const y = yToPx(close);
      ctx.strokeStyle = "rgba(255,235,59,0.75)";
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(M.left, y);
      ctx.lineTo(M.left + plotW(), y);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = "#ffeb3b";
      ctx.font = "11px system-ui,sans-serif";
      ctx.textAlign = "left";
      ctx.fillText(formatPrice(close), M.left + plotW() + 6, y);
    }
  }

  function drawCrosshair() {
    if (!mouse.inside || mouse.x == null || mouse.y == null) return;
    const { x, y } = mouse;
    if (x < M.left || x > M.left + plotW() || y < M.top || y > M.top + plotH()) return;
    const plotBottom = M.top + plotH();
    const plotRight = M.left + plotW();
    ctx.strokeStyle = "rgba(180,190,205,0.65)";
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(x, M.top);
    ctx.lineTo(x, plotBottom);
    ctx.moveTo(M.left, y);
    ctx.lineTo(plotRight, y);
    ctx.stroke();
    ctx.setLineDash([]);

    const t = pxToT(x);
    const py = pxToY(y);
    const timeLabel = formatCrosshairUtc(t);
    const priceLabel = formatPrice(py);
    ctx.font = "11px system-ui,sans-serif";

    const timeW = ctx.measureText(timeLabel).width + 10;
    let timeX = x - timeW / 2;
    timeX = Math.max(M.left + 2, Math.min(plotRight - timeW - 2, timeX));
    const timeY = plotBottom + 4;
    ctx.fillStyle = "rgba(20,24,32,0.94)";
    ctx.fillRect(timeX, timeY, timeW, 18);
    ctx.fillStyle = "#e8ecf4";
    ctx.textAlign = "left";
    ctx.textBaseline = "top";
    ctx.fillText(timeLabel, timeX + 5, timeY + 3);

    const priceW = ctx.measureText(priceLabel).width + 10;
    const priceX = plotRight + 4;
    let priceY = y - 9;
    priceY = Math.max(M.top + 2, Math.min(plotBottom - 16, priceY));
    ctx.fillStyle = "rgba(20,24,32,0.94)";
    ctx.fillRect(priceX, priceY, priceW, 18);
    ctx.fillStyle = "#e8ecf4";
    ctx.textAlign = "left";
    ctx.textBaseline = "top";
    ctx.fillText(priceLabel, priceX + 5, priceY + 3);
  }

  function requestDraw() {
    if (drawPending) return;
    drawPending = true;
    requestAnimationFrame(() => {
      drawPending = false;
      drawNow();
    });
  }

  function draw() {
    requestDraw();
  }

  function drawNow() {
    const dpr = window.devicePixelRatio || 1;
    const wrap = document.getElementById("canvas-wrap");
    const cssW = wrap.clientWidth;
    const cssH = wrap.clientHeight;
    if (canvas.width !== cssW * dpr || canvas.height !== cssH * dpr) {
      canvas.width = cssW * dpr;
      canvas.height = cssH * dpr;
      canvas.style.width = cssW + "px";
      canvas.style.height = cssH + "px";
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    tCursor = asofInput.value ? parseIso(asofInput.value + ":00.000Z") : view.t1;

    ctx.fillStyle = "#161a22";
    ctx.fillRect(0, 0, cssW, cssH);

    if (modeSel.value === "origin") drawOriginDensity();
    else drawPoolsStrength();

    drawGridAndAxes();
    drawCandlesticks();
    drawEvents();
    drawAsofMarkers();
    drawCrosshair();
    if (modeSel.value === "origin" && !dragging) scheduleDetailUpdate();
  }

  function scheduleDetailUpdate() {
    if (detailPending) return;
    detailPending = true;
    requestAnimationFrame(() => {
      detailPending = false;
      updateDetailFromPointer();
    });
  }

  function resize() {
    draw();
  }

  function setAsof(isoZ) {
    asofInput.value = isoZ.slice(0, 16);
    tCursor = parseIso(isoZ);
    detail.textContent = describeAt(tCursor, null);
    updateNavStatusBar();
    draw();
  }

  function setNavStatus(msg, kind) {
    const el = document.getElementById("pd1NavStatus");
    if (!el) return;
    el.textContent = msg;
    el.classList.remove("pd1-status-error", "pd1-status-loading");
    if (kind === "error") el.classList.add("pd1-status-error");
    if (kind === "loading") el.classList.add("pd1-status-loading");
  }

  function formatUtcMs(ms) {
    return new Date(ms).toISOString().replace(".000Z", "Z");
  }

  /** Align window bounds to 5m UTC grid (server vis_grid + candle keys are 5m slots). */
  function snapMsTo5mFloor(ms) {
    return Math.floor(ms / FIVE_MIN_MS) * FIVE_MIN_MS;
  }

  function snapMsTo5mCeil(ms) {
    return Math.ceil(ms / FIVE_MIN_MS) * FIVE_MIN_MS;
  }

  function snapWindowRequestMs(fromMs, toMs) {
    return { fromMs: snapMsTo5mFloor(fromMs), toMs: snapMsTo5mCeil(toMs) };
  }

  function traceNav(event, detail) {
    navTrace.push({
      t: Date.now(),
      event,
      replayGen: REPLAY_GEN,
      dataCommitEpoch,
      goToNavSeq,
      detail: detail || {},
    });
    if (navTrace.length > NAV_TRACE_MAX) navTrace.shift();
  }

  function beginDataCommitEpoch(reason) {
    dataCommitEpoch += 1;
    traceNav("epoch_bump", { reason, dataCommitEpoch });
    return dataCommitEpoch;
  }

  function updateNavStatusBar() {
    if (!meta) return;
    const mode = modeSel.value === "origin" ? "B — Exact Origin-Density" : "A — Original-Strength";
    const asof = asofInput.value ? asofInput.value + ":00Z" : "—";
    const loadedLo = formatUtcMs(fullExtents.t0);
    const loadedHi = formatUtcMs(fullExtents.t1);
    const visLo = formatUtcMs(view.t0);
    const visHi = formatUtcMs(view.t1);
    const st =
      navState === "loading"
        ? "Loading"
        : navState === "error"
          ? "Error"
          : "Ready";
    setNavStatus(
      `Status: ${st} · ${REPLAY_GEN} · Modus: ${mode} · Sichtbar: ${visLo} → ${visHi} · Geladen: ${loadedLo} → ${loadedHi} · As-of: ${asof} · DC-Cache: ${dcBandCache.size}/${DC_BAND_CACHE_MAX} · epoch ${dataCommitEpoch}`,
      navState === "error" ? "error" : navState === "loading" ? "loading" : null
    );
  }

  /** Same UTC parsing as Research Charts (`goto_time.js`). */
  function parseGoToUtcInput(raw) {
    if (typeof ResearchGotoTime !== "undefined" && ResearchGotoTime.parseGotoUtcToUnix) {
      const u = ResearchGotoTime.parseGotoUtcToUnix(raw);
      if (u != null) return u * 1000;
    }
    const s = String(raw || "").trim().replace(/\s+UTC$/i, "");
    if (!s) return null;
    if (/^\d{4}-\d{2}-\d{2}T/.test(s)) {
      const ms = Date.parse(s.endsWith("Z") ? s : s + "Z");
      return Number.isFinite(ms) ? ms : null;
    }
    const m = s.match(/^(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})(?::(\d{2}))?$/);
    if (!m) return null;
    const sec = m[6] ? parseInt(m[6], 10) : 0;
    const ms = Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], sec);
    return Number.isFinite(ms) ? ms : null;
  }

  function gotoViewPadMs() {
    if (typeof ResearchGotoTime !== "undefined" && ResearchGotoTime.GOTO_VIEW_HALF_SEC) {
      return ResearchGotoTime.GOTO_VIEW_HALF_SEC * 1000;
    }
    return GOTO_VIEW_PAD_MS;
  }

  function gotoWindowForMs(tMs) {
    const sec = Math.floor(tMs / 1000);
    if (typeof ResearchGotoTime !== "undefined" && ResearchGotoTime.gotoLoadWindow) {
      const w = ResearchGotoTime.gotoLoadWindow(sec);
      return {
        fromMs: w.from * 1000,
        toMs: w.to * 1000,
        viewPadMs: w.viewPad * 1000,
      };
    }
    const pad = gotoViewPadMs();
    return { fromMs: tMs - pad, toMs: tMs + pad, viewPadMs: pad };
  }

  function loadedSpanContainsMs(tMs) {
    return loadedSpan.t1 > loadedSpan.t0 && tMs >= loadedSpan.t0 && tMs <= loadedSpan.t1;
  }

  function scheduleEdgePrefetch() {
    if (!capabilities?.dynamic_historical) return;
    if (edgePrefetchTimer) clearTimeout(edgePrefetchTimer);
    edgePrefetchTimer = setTimeout(() => {
      edgePrefetchTimer = null;
      maybePrefetchAdjacent().catch(() => {});
    }, 150);
  }

  function commitViewToTime(tMs, padMs) {
    const pad = padMs != null ? padMs : GOTO_VIEW_PAD_MS;
    const { lo, hi } = timePanLoHi();
    view.t0 = Math.max(lo, tMs - pad);
    view.t1 = Math.min(hi, tMs + pad);
    if (view.t1 - view.t0 < 15 * 60 * 1000) {
      view.t0 = Math.max(lo, tMs - pad);
      view.t1 = Math.min(hi, tMs + pad);
    }
    if (!autoFitPriceY()) {
      throw new Error(
        `Keine Candlesticks im sichtbaren Fenster (${formatUtcMs(view.t0)} – ${formatUtcMs(view.t1)}). ` +
          "Historisches Preisfenster wurde nicht geladen."
      );
    }
    prewarmDensityRange(view.t0 - DENSITY_PREFETCH_MARGIN_MS, view.t1 + DENSITY_PREFETCH_MARGIN_MS);
    setAsof(formatUtcMs(tMs));
    traceNav("view_commit", {
      tMs,
      viewT: [view.t0, view.t1],
      viewY: [view.y0, view.y1],
      priceInView: priceRangeInView(),
    });
    updateNavStatusBar();
    draw();
  }

  async function goToUtcFromInput() {
    const raw = document.getElementById("gotoUtc")?.value;
    const tMs = parseGoToUtcInput(raw);
    if (tMs == null) {
      navState = "error";
      setNavStatus("GO TO: Ungültiges Format (z.B. 2026-09-15 01:45:00 UTC)", "error");
      return;
    }
    if (capabilities?.dynamic_historical && (tMs < dataBounds.t0 || tMs > dataBounds.t1)) {
      navState = "error";
      setNavStatus(
        `GO TO: außerhalb verfügbarer Historie (${formatUtcMs(dataBounds.t0)} – ${formatUtcMs(dataBounds.t1)}).`,
        "error"
      );
      updateNavStatusBar();
      return;
    }
    const reqSeq = ++goToNavSeq;
    const commitEpoch = beginDataCommitEpoch("go_to");
    navState = "loading";
    updateNavStatusBar();
    const t0 = performance.now();
    traceNav("go_to_start", { raw, tMs, commitEpoch, reqSeq });
    try {
      if (capabilities?.dynamic_historical) {
        await ensureLoadedForGoto(tMs, commitEpoch);
      } else if (!loadedSpanContainsMs(tMs)) {
        navState = "error";
        setNavStatus(
          `GO TO: Zeitpunkt außerhalb des geladenen Datenfensters (${formatUtcMs(fullExtents.t0)} – ${formatUtcMs(fullExtents.t1)}). Kein stiller Ersatz.`,
          "error"
        );
        updateNavStatusBar();
        return;
      }
      if (reqSeq !== goToNavSeq) return;
      const pad = gotoViewPadMs();
      commitViewToTime(tMs, pad);
      navState = "idle";
      const ms = Math.round(performance.now() - t0);
      setNavStatus(
        `GO TO OK: ${formatUtcMs(tMs)} · Fokus ±${Math.round(pad / 3600000)}h · ${ms}ms · ${REPLAY_GEN}`,
        null
      );
      traceNav("go_to_ok", { tMs, ms, commitEpoch });
    } catch (err) {
      if (reqSeq !== goToNavSeq) return;
      navState = "error";
      const msg = err && err.message ? err.message : String(err);
      setNavStatus("GO TO Fehler: " + msg, "error");
      traceNav("go_to_error", { tMs, commitEpoch, message: msg });
    }
    updateNavStatusBar();
  }

  function describeAt(t, price) {
    const iso = isoFromMs(t);
    const dcMs = causalDecisionCloseMs(t);
    const dcIso = isoFromMs(dcMs);
    const active = pools.filter((p) => poolActiveAt(p, t));
    let txt = `As-of ${iso}\ncausal decision_close: ${dcIso}\nACTIVE pools: ${active.length}\n`;
    if (price != null && modeSel.value === "origin") {
      const p = price;
      const dL = exactOriginDepthAt(t, p, "lower");
      const dU = exactOriginDepthAt(t, p, "upper");
      const priceBins = 80;
      const binL = binUnionAt(t, p, "lower", priceBins);
      const binU = binUnionAt(t, p, "upper", priceBins);
      txt += `\n@ price ${formatPrice(p)}:\n  exact lower origin depth: ${dL}\n  exact upper origin depth: ${dU}\n`;
      txt += `  (diagnostic bin-union @80: L=${binL} U=${binU})\n`;
      for (const side of ["lower", "upper"]) {
        const cov = poolsCoveringAt(t, p, side);
        if (!cov.length) continue;
        const origins = [...new Set(cov.map((pl) => pl.origin))].sort();
        txt += `\n${side} covering (${cov.length} pools, ${origins.length} origins):\n`;
        cov.slice(0, 12).forEach((pl) => {
          txt += `  ${pl.pool_id}\n    origin ${pl.origin}\n    [${formatPrice(pl.bottom)}, ${formatPrice(pl.top)}]\n`;
        });
        if (cov.length > 12) txt += `  … +${cov.length - 12} pools\n`;
      }
      const visL = maxExactInView(t, "lower");
      const visU = maxExactInView(t, "upper");
      txt += `\nmax exact depth in visible Y:\n  lower ${visL.max}  upper ${visU.max}\n`;
      if (visL.max) {
        txt += `  lower peak interval(s): ${visL.intervals
          .slice(0, 2)
          .map((iv) => `[${formatPrice(iv.y0)},${formatPrice(iv.y1)}]`)
          .join(" ")}\n`;
      }
    }
    const dc = Object.keys(clustersByDc || {})
      .sort()
      .reverse()
      .find((k) => parseIso(k) <= t);
    if (dc && clustersByDc[dc] && modeSel.value !== "origin") {
      for (const side of ["lower", "upper"]) {
        const cls = clustersByDc[dc][side] || [];
        txt += `\n${side} clusters @ dc ${dc}: ${cls.length}\n`;
        cls.slice(0, 3).forEach((c, i) => {
          txt += `  [${i}] ${c.bottom}-${c.top} origins=${c.origin_count} pools=${c.pool_ids.length}\n`;
        });
      }
    }
    const ev = events.filter((e) => e.asof_utc === iso);
    if (ev.length) txt += `\nEvents @ asof: ${ev.map((e) => e.label).join(", ")}\n`;
    return txt;
  }

  function updateDetailFromPointer() {
    if (!mouse.inside || mouse.x == null) return;
    const { x, y } = mouse;
    if (x < M.left || x > M.left + plotW() || y < M.top || y > M.top + plotH()) return;
    const t = pxToT(x);
    const p = pxToY(y);
    detail.textContent = describeAt(t, p);
  }

  function panTimeByDelta(dtMs) {
    const span = view.t1 - view.t0;
    if (!Number.isFinite(span) || span <= 0) return;
    let t0 = view.t0 + dtMs;
    let t1 = view.t1 + dtMs;
    const { lo, hi } = timePanLoHi();
    if (t0 < lo) {
      t0 = lo;
      t1 = t0 + span;
    }
    if (t1 > hi) {
      t1 = hi;
      t0 = t1 - span;
    }
    if (t1 - t0 < span * 0.99) {
      t0 = Math.max(lo, t1 - span);
    }
    view.t0 = Math.max(lo, t0);
    view.t1 = Math.min(hi, t1);
    const edge = span * 0.08;
    if (view.t0 - loadedSpan.t0 < edge || loadedSpan.t1 - view.t1 < edge) {
      scheduleEdgePrefetch();
    }
  }

  function zoomAt(px, py, scale) {
    const t = pxToT(px);
    const y = pxToY(py);
    const halfT = (view.t1 - view.t0) * scale * 0.5;
    const halfY = (view.y1 - view.y0) * scale * 0.5;
    view.t0 = t - halfT;
    view.t1 = t + halfT;
    view.y0 = y - halfY;
    view.y1 = y + halfY;
    const { lo, hi } = timePanLoHi();
    view.t0 = Math.max(lo, view.t0);
    view.t1 = Math.min(hi, view.t1);
    draw();
  }

  function canvasPointerXY(e) {
    const rect = canvas.getBoundingClientRect();
    return [e.clientX - rect.left, e.clientY - rect.top];
  }

  function onCanvasPointerMove(e) {
    const [x, y] = canvasPointerXY(e);
    const w = plotW() + M.left + M.right;
    const h = plotH() + M.top + M.bottom;
    mouse.x = x;
    mouse.y = y;
    mouse.inside = x >= M.left && x <= M.left + plotW() && y >= M.top && y <= M.top + plotH();
    if (dragging) {
      const dx = x - lastXY[0];
      const dy = y - lastXY[1];
      dragMovedPx += Math.abs(dx) + Math.abs(dy);
      lastXY = [x, y];
      if (dragMode === "pricePan") {
        panPriceByDelta(dy);
      } else if (dragMode === "priceZoom" || dragMode === "timeZoom") {
        applyAxisZoomFromDrag(dragMode, x, y);
      } else {
        const dt = (-dx / plotW()) * (view.t1 - view.t0);
        panTimeByDelta(dt);
      }
    }
    draw();
  }

  canvas.addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;
    const [x, y] = canvasPointerXY(e);
    const inPlot =
      x >= M.left && x <= plotRightX() && y >= M.top && y <= plotBottomY();
    const onPriceAxis = isPriceAxisX(x);
    const onTimeAxis = isTimeAxisHit(x, y);
    if (!inPlot && !onPriceAxis && !onTimeAxis) return;
    axisZoomStart = null;
    if (onTimeAxis) {
      dragMode = "timeZoom";
      const anchor = (view.t0 + view.t1) / 2;
      axisZoomStart = {
        px: x,
        span: view.t1 - view.t0,
        anchor,
        anchorRatio: (anchor - view.t0) / (view.t1 - view.t0),
      };
    } else if (onPriceAxis) {
      dragMode = "priceZoom";
      const anchor = (view.y0 + view.y1) / 2;
      axisZoomStart = {
        py: y,
        span: view.y1 - view.y0,
        anchor,
        anchorRatio: (anchor - view.y0) / (view.y1 - view.y0),
      };
    } else if (e.shiftKey) {
      dragMode = "pricePan";
    } else {
      dragMode = "time";
    }
    dragging = true;
    dragMovedPx = 0;
    lastXY = [x, y];
    if (dragMode === "priceZoom" || dragMode === "pricePan") canvas.style.cursor = "ns-resize";
    else if (dragMode === "timeZoom") canvas.style.cursor = "ew-resize";
    else canvas.style.cursor = "grabbing";
    e.preventDefault();
  });
  window.addEventListener("mouseup", () => {
    if (dragging) {
      if (dragMode === "time" && !manualPriceY) autoFitPriceY();
      if (dragMode === "time" || dragMode === "timeZoom") {
        prewarmDensityRange(view.t0 - DENSITY_PREFETCH_MARGIN_MS, view.t1 + DENSITY_PREFETCH_MARGIN_MS);
        if (dragMode === "time") maybePrefetchAdjacent().catch(() => {});
      }
      updateNavStatusBar();
    }
    dragging = false;
    dragMode = "time";
    axisZoomStart = null;
    canvas.style.cursor = "crosshair";
  });
  window.addEventListener("mousemove", onCanvasPointerMove);
  canvas.addEventListener("mouseleave", () => {
    mouse.inside = false;
    draw();
  });
  canvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    const scale = e.deltaY > 0 ? 1.12 : 0.88;
    if (e.shiftKey) {
      const y = pxToY(e.offsetY);
      const halfY = (view.y1 - view.y0) * scale * 0.5;
      view.y0 = y - halfY;
      view.y1 = y + halfY;
      manualPriceY = true;
      draw();
      return;
    }
    if (e.altKey) {
      const t = pxToT(e.offsetX);
      const halfT = (view.t1 - view.t0) * scale * 0.5;
      view.t0 = t - halfT;
      view.t1 = t + halfT;
      const { lo, hi } = timePanLoHi();
      view.t0 = Math.max(lo, view.t0);
      view.t1 = Math.min(hi, view.t1);
      autoFitPriceY();
      draw();
      return;
    }
    zoomAt(e.offsetX, e.offsetY, scale);
  });
  canvas.addEventListener("click", (e) => {
    if (dragMovedPx > 6) return;
    if (e.offsetX < M.left || e.offsetX > M.left + plotW()) return;
    setAsof(isoFromMs(pxToT(e.offsetX)));
  });

  modeSel.addEventListener("change", () => {
    document.getElementById("binUnionWrap").style.opacity = modeSel.value === "origin" ? "1" : "0.45";
    if (modeSel.value === "origin") prewarmExactDensityCache();
    draw();
  });
  showEvents.addEventListener("change", draw);
  document.getElementById("showBinUnion")?.addEventListener("change", draw);
  asofInput.addEventListener("change", () => {
    tCursor = asofInput.value ? parseIso(asofInput.value + ":00.000Z") : view.t1;
    draw();
  });

  function addToolbarButtons() {
    const mk = (label, fn) => {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = label;
      b.addEventListener("click", fn);
      toolbar.appendChild(b);
    };
    mk("Auto-Fit Preis", autoFitPriceY);
    mk("Y: alle Pools", fitAllPoolsY);
    mk("Reset Zoom", resetView);
  }

  function dataUrl(name) {
    const base = (window.PD1_VIEWER_BASE || "").replace(/\/?$/, "/");
    return base + "data/" + name;
  }

  function apiUrl(path) {
    const base = (window.PD1_VIEWER_BASE || "").replace(/\/?$/, "/");
    return base + String(path).replace(/^\//, "");
  }

  function mergeSegments(segments) {
    const sorted = (segments || [])
      .map((s) => [parseIso(s[0]), parseIso(s[1]), s[0], s[1]])
      .sort((a, b) => a[0] - b[0]);
    const out = [];
    for (const row of sorted) {
      if (!out.length) {
        out.push([row[2], row[3]]);
        continue;
      }
      const last = out[out.length - 1];
      const lastEnd = parseIso(last[1]);
      if (row[0] <= lastEnd + 300000) last[1] = row[3];
      else out.push([row[2], row[3]]);
    }
    return out;
  }

  function mergePools(existing, incoming) {
    const byId = new Map((existing || []).map((p) => [p.pool_id, p]));
    for (const pl of incoming || []) {
      const cur = byId.get(pl.pool_id);
      if (!cur) {
        byId.set(pl.pool_id, pl);
        continue;
      }
      byId.set(pl.pool_id, {
        ...cur,
        ...pl,
        segments: mergeSegments([...(cur.segments || []), ...(pl.segments || [])]),
      });
    }
    return Array.from(byId.values());
  }

  function mergePrices(a, b) {
    const map = new Map((a || []).map((x) => [x.t, x]));
    for (const row of b || []) map.set(row.t, row);
    return Array.from(map.values()).sort((x, y) => parseIso(x.t) - parseIso(y.t));
  }

  function mergeClusters(a, b) {
    return Object.assign({}, a || {}, b || {});
  }

  function recomputeExtentsFromLoaded() {
    if (!prices?.length) return;
    const closes = prices.map((b) => b.c);
    const priceSpan = recomputePriceTimeSpan();
    loadedSpan.t0 = parseIso(meta.period_start);
    loadedSpan.t1 = parseIso(meta.period_end);
    if (priceSpan.t1 > priceSpan.t0) {
      loadedSpan.t0 = Math.min(loadedSpan.t0, priceSpan.t0);
      loadedSpan.t1 = Math.max(loadedSpan.t1, priceSpan.t1);
    }
    fullExtents.t0 = loadedSpan.t0;
    fullExtents.t1 = loadedSpan.t1;
    fullExtents.priceMin = Math.min(...closes);
    fullExtents.priceMax = Math.max(...closes);
    if (pools?.length) {
      fullExtents.poolMin = Math.min(...pools.map((x) => x.bottom));
      fullExtents.poolMax = Math.max(...pools.map((x) => x.top));
    }
  }

  function applyWindowPayload(data, merge, source) {
    const incomingPrices = data.prices || [];
    const fullReplace = !merge || !meta;
    traceNav("window_apply_start", {
      source: source || "unknown",
      merge: !!merge,
      fullReplace,
      incomingPrices: incomingPrices.length,
      incomingPools: (data.pools || []).length,
    });
    if (fullReplace) {
      meta = data.meta;
      prices = data.prices;
      pools = data.pools;
      events = data.events || [];
      clustersByDc = data.clusters_by_dc || {};
      dcBandCache.clear();
      densityPrewarmPromise = null;
      densityPrewarmReady = false;
    } else {
      prices = mergePrices(prices, incomingPrices);
      let t0 = loadedSpan.t0 || Infinity;
      let t1 = loadedSpan.t1 || 0;
      if (incomingPrices.length) {
        t0 = Math.min(t0, parseIso(data.meta.period_start));
        t1 = Math.max(t1, parseIso(data.meta.period_end));
        for (const row of incomingPrices) {
          const ts = parseIso(row.t);
          if (Number.isFinite(ts)) {
            t0 = Math.min(t0, ts);
            t1 = Math.max(t1, ts);
          }
        }
      }
      meta = {
        ...meta,
        ...data.meta,
        period_start: formatUtcMs(t0),
        period_end: formatUtcMs(t1),
      };
      pools = mergePools(pools, data.pools);
      clustersByDc = mergeClusters(clustersByDc, data.clusters_by_dc);
      events = events || [];
    }
    recomputeExtentsFromLoaded();
    if (fullReplace) {
      prewarmExactDensityCache();
    } else if (view.t1 > view.t0) {
      prewarmDensityRange(view.t0 - DENSITY_PREFETCH_MARGIN_MS, view.t1 + DENSITY_PREFETCH_MARGIN_MS);
    }
    traceNav("window_apply_done", {
      source: source || "unknown",
      loadedSpan: [loadedSpan.t0, loadedSpan.t1],
      priceSpan: recomputePriceTimeSpan(),
      prices: prices?.length || 0,
      pools: pools?.length || 0,
    });
    draw();
  }

  const inflightWindowPromises = new Map();

  async function fetchWindow(fromMs, toMs, merge, commitEpoch, source) {
    const key = `${fromMs}-${toMs}|${merge ? "m" : "r"}`;
    if (inflightWindowPromises.has(key)) {
      traceNav("window_fetch_join", { key, source });
      return inflightWindowPromises.get(key);
    }
    const epochAtStart = commitEpoch != null ? commitEpoch : dataCommitEpoch;
    const tFetch = performance.now();
    const run = async () => {
      const snapped = snapWindowRequestMs(fromMs, toMs);
      fromMs = snapped.fromMs;
      toMs = snapped.toMs;
      const res = await fetch(apiUrl("api/window"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ from_utc: formatUtcMs(fromMs), to_utc: formatUtcMs(toMs) }),
      });
      let data = {};
      try {
        data = await res.json();
      } catch (_) {
        data = {};
      }
      const ms = Math.round(performance.now() - tFetch);
      if (!res.ok) {
        const msg = data.message || data.detail || `HTTP ${res.status}`;
        traceNav("window_fetch_http_error", { source, status: res.status, ms, message: msg });
        throw new Error(data.code ? `${data.code}: ${msg}` : msg);
      }
      if (epochAtStart !== dataCommitEpoch) {
        traceNav("window_fetch_stale_epoch", { source, epochAtStart, dataCommitEpoch, ms });
        return null;
      }
      const pr = data.prices || [];
      let pMin = null;
      let pMax = null;
      let tMin = null;
      let tMax = null;
      for (const row of pr) {
        const ts = parseIso(row.t);
        if (!Number.isFinite(ts)) continue;
        tMin = tMin == null ? ts : Math.min(tMin, ts);
        tMax = tMax == null ? ts : Math.max(tMax, ts);
        pMin = pMin == null ? row.l : Math.min(pMin, row.l, row.c);
        pMax = pMax == null ? row.h : Math.max(pMax, row.h, row.c);
      }
      traceNav("window_fetch_ok", {
        source,
        ms,
        epochAtStart,
        httpStatus: res.status,
        candleCount: pr.length,
        candleTMin: tMin,
        candleTMax: tMax,
        candlePMin: pMin,
        candlePMax: pMax,
        fromMs,
        toMs,
      });
      applyWindowPayload(data, merge, source);
      return data;
    };
    const promise = run();
    inflightWindowPromises.set(key, promise);
    try {
      return await promise;
    } finally {
      inflightWindowPromises.delete(key);
    }
  }

  /** Match server MAX_5M_BARS: floor((to-from)/300s)+1 must be <= max_export_hours*12. */
  function maxWindowSpanMs() {
    const max5mBars = (capabilities?.max_export_hours || 72) * 12;
    return (max5mBars - 1) * 5 * 60 * 1000;
  }

  function clampWindowRequest(fromMs, toMs) {
    const maxSpan = maxWindowSpanMs();
    let f = Math.max(dataBounds.t0, fromMs);
    let t = Math.min(dataBounds.t1, toMs);
    if (t - f > maxSpan) {
      const mid = (f + t) / 2;
      f = Math.max(dataBounds.t0, mid - maxSpan / 2);
      t = Math.min(dataBounds.t1, mid + maxSpan / 2);
    }
    return snapWindowRequestMs(f, t);
  }

  async function ensureLoadedForGoto(tMs, commitEpoch) {
    const pad = gotoViewPadMs();
    const needLo = tMs - pad;
    const needHi = tMs + pad;
    if (loadedSpanContainsMs(tMs) && pricesCoverRange(needLo, needHi)) return;
    /** Same visible/load half-window as Research Charts (`ResearchGotoTime.gotoLoadWindow`). */
    const win = gotoWindowForMs(tMs);
    let { fromMs, toMs } = clampWindowRequest(win.fromMs, win.toMs);
    const merged = loadedSpan.t1 > loadedSpan.t0;
    const payload = await fetchWindow(fromMs, toMs, merged, commitEpoch, "go_to");
    if (!payload) {
      throw new Error("Fenster-Laden abgebrochen (veraltete Antwort) — bitte erneut GO TO.");
    }
    if (!loadedSpanContainsMs(tMs)) {
      throw new Error(
        `GO TO: ${formatUtcMs(tMs)} außerhalb geladenem Fenster (${formatUtcMs(loadedSpan.t0)} – ${formatUtcMs(loadedSpan.t1)})`
      );
    }
    if (!pricesCoverRange(needLo, needHi)) {
      const n = (payload.prices || []).length;
      throw new Error(
        `GO TO: API lieferte keine 5m-Candles für ${formatUtcMs(needLo)} – ${formatUtcMs(needHi)} (${n} Preiszeilen im letzten Fenster).`
      );
    }
  }

  async function maybePrefetchAdjacent() {
    if (!capabilities?.dynamic_historical) return;
    const span = view.t1 - view.t0;
    const edge = span * 0.06;
    const maxSpan = maxWindowSpanMs();
    const epoch = dataCommitEpoch;
    if (view.t0 - loadedSpan.t0 < edge && loadedSpan.t0 > dataBounds.t0) {
      const toMs = loadedSpan.t0;
      const fromMs = Math.max(dataBounds.t0, toMs - maxSpan);
      await fetchWindow(fromMs, toMs, true, epoch, "prefetch_left");
    } else if (loadedSpan.t1 - view.t1 < edge && loadedSpan.t1 < dataBounds.t1) {
      const fromMs = loadedSpan.t1;
      const toMs = Math.min(dataBounds.t1, fromMs + maxSpan);
      await fetchWindow(fromMs, toMs, true, epoch, "prefetch_right");
    }
  }

  function finishViewerBootstrap() {
    document.getElementById("hint").textContent =
      `${pools.length} pools · ${events.length} events · Rad=Zoom · Shift+Ziehen=Y-Pan · Preisachse=Y-Zoom · Zeitachse=X-Zoom` +
      (capabilities?.dynamic_historical ? " · dynamische Historie" : "");
    addToolbarButtons();
    if (meta.anchors?.[0]) document.getElementById("btnAnch1").onclick = () => setAsof(meta.anchors[0]);
    if (meta.anchors?.[1]) document.getElementById("btnAnch2").onclick = () => setAsof(meta.anchors[1]);
    if (meta.anchors?.[2]) document.getElementById("btnAnch3").onclick = () => setAsof(meta.anchors[2]);
    if (showEvents) showEvents.checked = false;
    document.getElementById("btnGoToUtc")?.addEventListener("click", () => goToUtcFromInput());
    document.getElementById("gotoUtc")?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") goToUtcFromInput();
    });
    prewarmExactDensityCache();
    const bootAsof = meta.anchors?.[1] || meta.period_start;
    setAsof(bootAsof);
    navState = "idle";
    updateNavStatusBar();
    resize();
  }

  async function loadStaticBundle() {
    const [m, p, pl, ev, cl] = await Promise.all([
      fetch(dataUrl("meta.json")).then((r) => r.json()),
      fetch(dataUrl("prices.json")).then((r) => r.json()),
      fetch(dataUrl("pools.json")).then((r) => r.json()),
      fetch(dataUrl("events.json")).then((r) => r.json()),
      fetch(dataUrl("clusters_by_dc.json")).then((r) => r.json()),
    ]);
    meta = m;
    prices = p;
    pools = pl;
    events = ev;
    clustersByDc = cl;
    recomputeExtentsFromLoaded();
    view.t0 = fullExtents.t0;
    view.t1 = fullExtents.t1;
    autoFitPriceY();
    finishViewerBootstrap();
  }

  async function load() {
    try {
      const capRes = await fetch(apiUrl("api/capabilities"));
      if (capRes.ok) {
        capabilities = await capRes.json();
        if (capabilities.dynamic_historical) {
          dataBounds.t0 = parseIso(capabilities.data_available_from);
          dataBounds.t1 = parseIso(capabilities.data_available_to);
          let fromMs = dataBounds.t0;
          let toMs = Math.min(dataBounds.t1, fromMs + (capabilities.max_export_hours || 72) * 3600 * 1000);
          try {
            const m = await fetch(dataUrl("meta.json")).then((r) => r.json());
            fromMs = parseIso(m.period_start);
            toMs = parseIso(m.period_end);
            meta = { ...m };
          } catch (_) {
            /* no static meta */
          }
          ({ fromMs, toMs } = clampWindowRequest(fromMs, toMs));
          navState = "loading";
          updateNavStatusBar();
          const bootEpoch = beginDataCommitEpoch("bootstrap");
          const bootPayload = await fetchWindow(fromMs, toMs, false, bootEpoch, "bootstrap");
          if (!bootPayload) {
            throw new Error("Bootstrap-Fenster verworfen (Navigation überschrieben) — Seite neu laden.");
          }
          view.t0 = fullExtents.t0;
          view.t1 = fullExtents.t1;
          autoFitPriceY();
          finishViewerBootstrap();
          return;
        }
      }
    } catch (_) {
      /* fallback static */
    }
    await loadStaticBundle();
  }

  window.PD1_LANDSCAPE_NAV = {
    replayGen: REPLAY_GEN,
    trace: () => navTrace.slice(),
    snapshot: () => ({
      replayGen: REPLAY_GEN,
      dataCommitEpoch,
      goToNavSeq,
      loadedSpan: { ...loadedSpan },
      dataBounds: { ...dataBounds },
      fullExtents: { ...fullExtents },
      view: { ...view },
      manualPriceY,
      dragMode,
      prices: prices?.length || 0,
      pools: pools?.length || 0,
      dcCache: dcBandCache.size,
    }),
    candleBodyWidthPx: () => candleBodyWidthPx(),
    candleLayoutForBar: (b) => candleLayoutForBar(b),
    simulatePricePanPx: (dyPx) => {
      const before = { y0: view.y0, y1: view.y1, t0: view.t0, t1: view.t1 };
      panPriceByDelta(dyPx);
      return {
        before,
        after: { y0: view.y0, y1: view.y1, t0: view.t0, t1: view.t1 },
        manualPriceY,
      };
    },
    resolveDragMode: (x, y, shiftKey) => {
      if (isTimeAxisHit(x, y)) return "timeZoom";
      if (isPriceAxisX(x)) return "priceZoom";
      if (shiftKey) return "pricePan";
      return "time";
    },
    applyAxisZoomFromDrag,
  };

  window.addEventListener("resize", resize);
  load().catch((err) => {
    detail.textContent = "Load error: " + err;
  });
})();
