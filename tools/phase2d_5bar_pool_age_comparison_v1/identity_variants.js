"use strict";
/** Alternative pool-age traces (Phase 2D only — does not change Phase 2C defaults). */
const {
  loadAdapter,
  buildDepth2Intervals,
  parseIsoMs,
} = require("../phase1b_depth2_landscape_pilot_v1/analyzer_depth2_landscape.js");
const { matchBand, priceIoU } = require("../phase2c_5m_pd1_depth2_candidate_pool_age_v1/pool_age_enricher.js");

function isoFromMs(ms) {
  return new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z");
}

function upperBandsAt(AD, pools, asOfIso, slotMin) {
  const landscape = buildDepth2Intervals(AD, pools, asOfIso, slotMin);
  return (landscape.display_intervals || []).filter((x) => x.side === "upper");
}

function matchOriginSticky(band, ref) {
  const shared = (ref.upper_pool_origins || []).filter((o) =>
    (band.origins || []).includes(o)
  ).length;
  const iou = priceIoU(
    Number(band.bottom_price),
    Number(band.top_price),
    Number(ref.upper_pool_lower),
    Number(ref.upper_pool_upper)
  );
  const ok = shared >= 1 && iou >= 0.1;
  return { ok, shared, iou };
}

function traceWithMatcher(pools, ref, signalUtc, contextFromUtc, cfg, matcher) {
  const slotMs = cfg.slot_minutes * 60 * 1000;
  const { AD } = loadAdapter();
  const signalMs = parseIsoMs(signalUtc);
  const contextFromMs = contextFromUtc ? parseIsoMs(contextFromUtc) : 0;
  let t = signalMs;
  let firstSeenMs = signalMs;
  let bars = 0;
  while (t >= contextFromMs && bars < cfg.max_lookback_bars) {
    const bands = upperBandsAt(AD, pools, isoFromMs(t), cfg.slot_minutes);
    let hit = null;
    for (const b of bands) {
      const m = matcher(b, ref);
      if (m.ok) {
        if (!hit || (m.score != null && m.score > hit.score)) hit = { band: b, ...m };
        else if (m.score == null) hit = { band: b, ...m };
      }
    }
    if (!hit) break;
    firstSeenMs = t;
    bars += 1;
    t -= slotMs;
  }
  return {
    touch_pool_age_bars: bars,
    touch_pool_first_seen_utc: bars > 0 ? isoFromMs(firstSeenMs) : null,
    eligible_5: bars >= 5,
    eligible_10: bars >= 10,
  };
}

function runVariants(input) {
  const cfg = {
    slot_minutes: 5,
    max_lookback_bars: 600,
    min_match_score_loose: 0.25,
  };
  const pools = input.pools || [];
  const ctx = input.context_from_utc;
  const out = [];
  for (const cand of input.candidates || []) {
    const ref = {
      upper_pool_lower: cand.upper_pool_lower,
      upper_pool_upper: cand.upper_pool_upper,
      upper_pool_origins: cand.upper_pool_origins || [],
    };
    const baseline = cand.phase2c_age_bars;
    const looseMatcher = (band, r) => {
      const m = matchBand(band, r);
      if (m.ok) return m;
      const iou = priceIoU(
        Number(band.bottom_price),
        Number(band.top_price),
        Number(r.upper_pool_lower),
        Number(r.upper_pool_upper)
      );
      const oj =
        (band.origins || []).filter((o) => (r.upper_pool_origins || []).includes(o))
          .length > 0
          ? 0.3
          : 0;
      const score = 0.6 * iou + 0.4 * oj;
      return { ok: score >= cfg.min_match_score_loose, score, iou };
    };
    const loose = traceWithMatcher(pools, ref, cand.signal_close_utc, ctx, cfg, looseMatcher);
    const sticky = traceWithMatcher(pools, ref, cand.signal_close_utc, ctx, cfg, matchOriginSticky);
    out.push({
      candidate_id: cand.candidate_id,
      phase2c_continuous_depth2_age_bars: baseline,
      variant_loose_score_0_25: loose,
      variant_origin_sticky_iou_0_1: sticky,
      identity_comparison_note:
        sticky.touch_pool_age_bars > baseline
          ? "ORIGIN_STICKY_LONGER_THAN_PHASE2C"
          : loose.touch_pool_age_bars > baseline
            ? "LOOSE_LONGER_THAN_PHASE2C"
            : "SAME_OR_SHORTER",
    });
  }
  return out;
}

if (require.main === module) {
  const chunks = [];
  process.stdin.on("data", (c) => chunks.push(c));
  process.stdin.on("end", () => {
    const input = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    process.stdout.write(JSON.stringify(runVariants(input)));
  });
}

module.exports = { runVariants };
