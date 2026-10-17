/* PD1 Landscape Replay — chart viewer v2 (axes, price, zoom) */
(function () {
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
    for (const b of prices) {
      const t = parseIso(b.t);
      if (t < view.t0 || t > view.t1) continue;
      lo = Math.min(lo, b.l, b.c);
      hi = Math.max(hi, b.h, b.c);
    }
    if (!isFinite(lo)) {
      lo = fullExtents.priceMin;
      hi = fullExtents.priceMax;
    }
    return { lo, hi };
  }

  function autoFitPriceY() {
    const { lo, hi } = priceRangeInView();
    const pad = Math.max((hi - lo) * 0.06, 0.00015);
    view.y0 = lo - pad;
    view.y1 = hi + pad;
    draw();
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

  function originCountsAtTime(t, priceBins, pStep) {
    const lower = Array.from({ length: priceBins }, () => new Set());
    const upper = Array.from({ length: priceBins }, () => new Set());
    for (const pl of pools) {
      if (!poolActiveAt(pl, t)) continue;
      const side = pl.side === "upper" ? upper : lower;
      const b0 = Math.max(0, Math.floor((pl.bottom - view.y0) / pStep));
      const b1 = Math.min(priceBins - 1, Math.floor((pl.top - view.y0) / pStep));
      for (let bi = b0; bi <= b1; bi++) side[bi].add(pl.origin);
    }
    return { lower, upper };
  }

  function drawOriginDensity() {
    const priceBins = Math.min(140, Math.max(48, Math.floor(plotH() / 3)));
    const pStep = (view.y1 - view.y0) / priceBins;

    for (let ck = 0; ; ck++) {
      const t = view.t0 + ck * DENSITY_STEP_MS;
      if (t > view.t1) break;
      const { lower, upper } = originCountsAtTime(t, priceBins, pStep);
      const x0 = tToX(t);
      const x1 = tToX(Math.min(t + DENSITY_STEP_MS, view.t1));
      const w = Math.max(1, x1 - x0);
      for (let bi = 0; bi < priceBins; bi++) {
        const nL = lower[bi].size;
        const nU = upper[bi].size;
        if (!nL && !nU) continue;
        const y = view.y0 + bi * pStep;
        const yt = yToPx(y + pStep);
        const yb = yToPx(y);
        const h = yb - yt;
        if (nL) {
          ctx.fillStyle = densityFill("lower", densityBucket(nL));
          ctx.fillRect(x0, yt, w * 0.52, h);
        }
        if (nU) {
          ctx.fillStyle = densityFill("upper", densityBucket(nU));
          ctx.fillRect(x0 + w * 0.48, yt, w * 0.52, h);
        }
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
    ctx.fillStyle = "rgba(20,24,32,0.85)";
    ctx.fillRect(x, y, 200, 72);
    ctx.fillStyle = "#ccc";
    ctx.fillText("independent ACTIVE origins", x + 6, y + 4);
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
    ctx.fillText("links=lower  rechts=upper", x + 6, y + 62);
  }

  function drawCandlesAndPrice() {
    const barW = Math.max(1, (plotW() / Math.max(1, prices.length)) * 0.55);
    for (const b of prices) {
      const t = parseIso(b.t);
      if (t < view.t0 || t > view.t1) continue;
      const x = tToX(t);
      const yo = yToPx(b.o);
      const yc = yToPx(b.c);
      const yh = yToPx(b.h);
      const yl = yToPx(b.l);
      const up = b.c >= b.o;
      ctx.strokeStyle = up ? "#26a69a" : "#ef5350";
      ctx.fillStyle = up ? "rgba(38,166,154,0.85)" : "rgba(239,83,80,0.85)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, yh);
      ctx.lineTo(x, yl);
      ctx.stroke();
      const top = Math.min(yo, yc);
      const bot = Math.max(yo, yc);
      const bh = Math.max(1, bot - top);
      ctx.fillRect(x - barW / 2, top, barW, bh);
    }

    ctx.strokeStyle = "#ffffff";
    ctx.lineWidth = 2;
    ctx.beginPath();
    let started = false;
    for (const b of prices) {
      const t = parseIso(b.t);
      if (t < view.t0 || t > view.t1) continue;
      const x = tToX(t);
      const y = yToPx(b.c);
      if (!started) {
        ctx.moveTo(x, y);
        started = true;
      } else ctx.lineTo(x, y);
    }
    ctx.stroke();
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
    for (const e of events) {
      const t = parseIso(e.asof_utc);
      if (t < view.t0 || t > view.t1) continue;
      const x = tToX(t);
      ctx.fillStyle = col[e.label] || "#aaa";
      ctx.beginPath();
      ctx.arc(x, yToPx((view.y0 + view.y1) / 2), 3.5, 0, Math.PI * 2);
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
    if (!mouse.inside || mouse.x == null) return;
    const { x, y } = mouse;
    if (x < M.left || x > M.left + plotW() || y < M.top || y > M.top + plotH()) return;
    ctx.strokeStyle = "rgba(200,200,200,0.55)";
    ctx.lineWidth = 1;
    ctx.setLineDash([2, 2]);
    ctx.beginPath();
    ctx.moveTo(x, M.top);
    ctx.lineTo(x, M.top + plotH());
    ctx.moveTo(M.left, y);
    ctx.lineTo(M.left + plotW(), y);
    ctx.stroke();
    ctx.setLineDash([]);
    const t = pxToT(x);
    const py = pxToY(y);
    const label = `${formatTime(t)} UTC · ${formatPrice(py)}`;
    ctx.fillStyle = "rgba(20,24,32,0.92)";
    ctx.fillRect(M.left + 4, M.top + 4, ctx.measureText(label).width + 10, 18);
    ctx.fillStyle = "#e8e8e8";
    ctx.font = "11px system-ui,sans-serif";
    ctx.textAlign = "left";
    ctx.textBaseline = "top";
    ctx.fillText(label, M.left + 8, M.top + 6);
  }

  function draw() {
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

    drawCandlesAndPrice();
    drawEvents();
    drawGridAndAxes();
    drawAsofMarkers();
    drawCrosshair();
  }

  function resize() {
    draw();
  }

  function setAsof(isoZ) {
    asofInput.value = isoZ.slice(0, 16);
    tCursor = parseIso(isoZ);
    detail.textContent = describeAt(tCursor);
    draw();
  }

  function describeAt(t) {
    const iso = isoFromMs(t);
    const active = pools.filter((p) => poolActiveAt(p, t));
    const dc = Object.keys(clustersByDc || {})
      .sort()
      .reverse()
      .find((k) => parseIso(k) <= t);
    let txt = `As-of ${iso}\nACTIVE pools: ${active.length}\n`;
    if (dc && clustersByDc[dc]) {
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

  function zoomAt(px, py, scale) {
    const t = pxToT(px);
    const y = pxToY(py);
    const halfT = (view.t1 - view.t0) * scale * 0.5;
    const halfY = (view.y1 - view.y0) * scale * 0.5;
    view.t0 = t - halfT;
    view.t1 = t + halfT;
    view.y0 = y - halfY;
    view.y1 = y + halfY;
    view.t0 = Math.max(fullExtents.t0, view.t0);
    view.t1 = Math.min(fullExtents.t1, view.t1);
    draw();
  }

  canvas.addEventListener("mousedown", (e) => {
    dragging = true;
    lastXY = [e.offsetX, e.offsetY];
    canvas.style.cursor = "grabbing";
  });
  window.addEventListener("mouseup", () => {
    dragging = false;
    canvas.style.cursor = "crosshair";
  });
  canvas.addEventListener("mousemove", (e) => {
    mouse.x = e.offsetX;
    mouse.y = e.offsetY;
    mouse.inside = true;
    if (dragging) {
      const dx = e.offsetX - lastXY[0];
      const dy = e.offsetY - lastXY[1];
      lastXY = [e.offsetX, e.offsetY];
      const dt = (dx / plotW()) * (view.t1 - view.t0);
      const dyP = (dy / plotH()) * (view.y1 - view.y0);
      view.t0 -= dt;
      view.t1 -= dt;
      view.y0 += dyP;
      view.y1 += dyP;
      view.t0 = Math.max(fullExtents.t0, view.t0);
      view.t1 = Math.min(fullExtents.t1, view.t1);
    }
    draw();
  });
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
      draw();
      return;
    }
    if (e.altKey) {
      const t = pxToT(e.offsetX);
      const halfT = (view.t1 - view.t0) * scale * 0.5;
      view.t0 = t - halfT;
      view.t1 = t + halfT;
      view.t0 = Math.max(fullExtents.t0, view.t0);
      view.t1 = Math.min(fullExtents.t1, view.t1);
      draw();
      return;
    }
    zoomAt(e.offsetX, e.offsetY, scale);
  });
  canvas.addEventListener("click", (e) => {
    if (e.offsetX < M.left || e.offsetX > M.left + plotW()) return;
    setAsof(isoFromMs(pxToT(e.offsetX)));
  });

  modeSel.addEventListener("change", draw);
  showEvents.addEventListener("change", draw);
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

  async function load() {
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

    const closes = prices.map((b) => b.c);
    fullExtents.t0 = parseIso(meta.period_start);
    fullExtents.t1 = parseIso(meta.period_end);
    fullExtents.priceMin = Math.min(...closes);
    fullExtents.priceMax = Math.max(...closes);
    fullExtents.poolMin = Math.min(...pools.map((x) => x.bottom));
    fullExtents.poolMax = Math.max(...pools.map((x) => x.top));

    view.t0 = fullExtents.t0;
    view.t1 = fullExtents.t1;
    autoFitPriceY();

    document.getElementById("hint").textContent =
      `${pools.length} pools · ${events.length} events · Rad=Zoom · Shift+Rad=Y · Alt+Rad=Zeit`;
    addToolbarButtons();
    document.getElementById("btnAnch1").onclick = () => setAsof(meta.anchors[0]);
    document.getElementById("btnAnch2").onclick = () => setAsof(meta.anchors[1]);
    document.getElementById("btnAnch3").onclick = () => setAsof(meta.anchors[2]);
    setAsof(meta.anchors[1]);
    resize();
  }

  window.addEventListener("resize", resize);
  load().catch((err) => {
    detail.textContent = "Load error: " + err;
  });
})();
