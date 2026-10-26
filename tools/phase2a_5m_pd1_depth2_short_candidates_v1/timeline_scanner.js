"use strict";
/**
 * Phase 2A — chronological short candidate scan (read-only, production adapter).
 */
const {
  loadAdapter,
  buildDepth2Intervals,
  analyzeWindowPayload,
  parseIsoMs,
  computeGaps,
} = require("../phase1b_depth2_landscape_pilot_v1/analyzer_depth2_landscape.js");

const DEFAULTS = {
  proximity_pct: 0.05,
  large_gap_pct: 0.5,
  slot_minutes: 5,
  rejection_mode: "rejection_v1",
  cooldown_candles: 6,
  max_output: 20,
};

function isoFromMs(ms) {
  return new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z");
}

function nextOpenMs(closeMs, slotMin) {
  return closeMs + slotMin * 60 * 1000;
}

function candleMeetsUpperProximity(c, pool, proximityPct) {
  const bot = Number(pool.bottom_price);
  const top = Number(pool.top_price);
  const tol = bot * (1 - proximityPct / 100);
  const inside = c.h >= bot && c.l <= top;
  const touch = c.h >= tol && c.l <= top;
  const nearBelow = c.c < bot && c.c >= tol;
  return inside || touch || nearBelow;
}

function rejectionOk(c, mode) {
  const bearish = c.c < c.o;
  if (!bearish) return false;
  if (mode === "rejection_v1") return true;
  if (mode === "rejection_v2") return c.c < c.pool_bottom_touch;
  return false;
}

function pickUpperPool(c, uppers, proximityPct) {
  let best = null;
  let bestDist = Infinity;
  for (const p of uppers) {
    if (!candleMeetsUpperProximity(c, p, proximityPct)) continue;
    const bot = Number(p.bottom_price);
    const dist = Math.abs(c.h - bot) / bot * 100;
    if (dist < bestDist) {
      bestDist = dist;
      best = p;
    }
  }
  return best ? { pool: best, dist_pct: bestDist } : null;
}

function poolKey(p) {
  return [p.bottom_price, p.top_price].join("|");
}

function scanTimeline(input) {
  const cfg = { ...DEFAULTS, ...(input.config || {}) };
  const pools = input.pools || [];
  const prices = (input.prices || []).slice().sort((a, b) => parseIsoMs(a.t) - parseIsoMs(b.t));
  const scanFrom = parseIsoMs(input.scan_from_utc);
  const scanTo = parseIsoMs(input.scan_to_utc);
  const { AD } = loadAdapter(input.adapter_path);

  const byTime = new Map();
  prices.forEach((row) => {
    byTime.set(parseIsoMs(row.t), row);
  });

  const raw = [];
  const lastEmitByPool = new Map();

  for (const row of prices) {
    const tMs = parseIsoMs(row.t);
    if (tMs < scanFrom || tMs > scanTo) continue;
    const c = {
      t: row.t,
      o: Number(row.o),
      h: Number(row.h),
      l: Number(row.l),
      c: Number(row.c),
    };
    const asOf = isoFromMs(tMs);
    const landscape = buildDepth2Intervals(AD, pools, asOf, cfg.slot_minutes);
    const uppers = (landscape.display_intervals || []).filter((x) => x.side === "upper");
    if (!uppers.length) continue;

    const pick = pickUpperPool(c, uppers, cfg.proximity_pct);
    if (!pick) continue;

    c.pool_bottom_touch = Number(pick.pool.bottom_price);
    if (!rejectionOk(c, cfg.rejection_mode)) continue;

    const pk = poolKey(pick.pool);
    const idx = prices.findIndex((r) => parseIsoMs(r.t) === tMs);
    const lastIdx = lastEmitByPool.get(pk);
    if (lastIdx != null && idx - lastIdx < cfg.cooldown_candles) continue;
    lastEmitByPool.set(pk, idx);

    const entryMs = nextOpenMs(tMs, cfg.slot_minutes);
    const entryRow = byTime.get(entryMs);
    const entryPrice = entryRow ? Number(entryRow.o) : c.c;

    const distToLower =
      ((Number(pick.pool.bottom_price) - entryPrice) / entryPrice) * 100;
    if (entryPrice >= Number(pick.pool.bottom_price)) {
      // entry at open above pool lower — still record distance (may be negative)
    }

    raw.push({
      signal_close_utc: asOf,
      signal_close_ms: tMs,
      entry_open_utc: isoFromMs(entryMs),
      entry_open_ms: entryMs,
      entry_price: entryPrice,
      upper_pool_lower: Number(pick.pool.bottom_price),
      upper_pool_upper: Number(pick.pool.top_price),
      upper_pool_depth: pick.pool.depth,
      upper_pool_origins: pick.pool.origins || [],
      distance_to_pool_lower_pct:
        (entryPrice - Number(pick.pool.bottom_price)) /
        Number(pick.pool.bottom_price) *
        100,
      trigger_reason: `${cfg.rejection_mode}+depth2_upper_proximity_${cfg.proximity_pct}pct`,
      candle: c,
    });
  }

  raw.sort((a, b) => a.signal_close_ms - b.signal_close_ms);
  const total_found = raw.length;
  const selected = raw.slice(0, cfg.max_output);

  const profiles = [];
  for (const cand of selected) {
    const analysis = analyzeWindowPayload(
      { pools, prices, window: input.window, provenance: input.provenance },
      {
        AD,
        as_of_utc: cand.entry_open_utc,
        slot_minutes: cfg.slot_minutes,
        large_gap_pct: cfg.large_gap_pct,
        reference_price: cand.entry_price,
      }
    );
    const gapsU = computeGaps(
      analysis.landscape.display_intervals.filter((x) => x.side === "upper"),
      "upper",
      cfg.large_gap_pct
    );
    const gapsL = computeGaps(
      analysis.landscape.display_intervals.filter((x) => x.side === "lower"),
      "lower",
      cfg.large_gap_pct
    );
    const largeU = gapsU.gaps.filter((g) => g.large_gap);
    const largeL = gapsL.gaps.filter((g) => g.large_gap);
    const nearestLarge = largeU.find(
      (g) => g.gap_bottom_price >= cand.upper_pool_upper
    );

    profiles.push({
      ...cand,
      sl_reference_price:
        cand.upper_pool_upper * (1 + 0.002),
      parity_ok: analysis.landscape.parity.ok,
      upper_interval_count: analysis.landscape.display_intervals.filter(
        (x) => x.side === "upper"
      ).length,
      lower_interval_count: analysis.landscape.display_intervals.filter(
        (x) => x.side === "lower"
      ).length,
      upper_large_gap_count: largeU.length,
      lower_large_gap_count: largeL.length,
      nearest_upper_large_gap_pct: nearestLarge ? nearestLarge.gap_pct : null,
      gaps_upper: gapsU.gaps,
      gaps_lower: gapsL.gaps,
      landscape_intervals: analysis.landscape.display_intervals,
      full_analysis: analysis,
    });
  }

  return {
    config: cfg,
    scan_from_utc: input.scan_from_utc,
    scan_to_utc: input.scan_to_utc,
    total_candidates_found: total_found,
    candidates_output: profiles.length,
    rejection_variants_documented: [
      "rejection_v1: bearish close (c<o) after depth2 upper proximity touch",
      "rejection_v2: v1 plus close below pool lower (not used unless configured)",
    ],
    duplicate_rule: `cooldown_candles=${cfg.cooldown_candles} per upper pool key`,
    candidates: profiles,
  };
}

if (require.main === module) {
  const chunks = [];
  process.stdin.on("data", (c) => chunks.push(c));
  process.stdin.on("end", () => {
    try {
      const input = JSON.parse(Buffer.concat(chunks).toString("utf8"));
      process.stdout.write(JSON.stringify(scanTimeline(input)));
    } catch (e) {
      process.stderr.write(String(e.stack || e));
      process.exit(1);
    }
  });
}

module.exports = { scanTimeline, candleMeetsUpperProximity };
