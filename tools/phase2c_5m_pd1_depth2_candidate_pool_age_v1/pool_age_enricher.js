"use strict";
/**
 * Phase 2C — causal Depth-2 upper pool age at signal (read-only).
 */
const {
  loadAdapter,
  buildDepth2Intervals,
  parseIsoMs,
} = require("../phase1b_depth2_landscape_pilot_v1/analyzer_depth2_landscape.js");

const IDENTITY_DOC = {
  version: "phase2c_pool_track_v1",
  same_pool_if:
    "At each 5m as-of, pick best-matching upper Depth-2 band to Phase-2A reference pool.",
  match_score:
    "0.6 * price_IoU + 0.4 * origin_Jaccard; require score >= 0.35 OR (IoU >= 0.2 && >=1 shared origin).",
  boundary_drift:
    "Allowed — bounds may shift bar-to-bar while track continues if match score holds.",
  new_pool:
    "Backward walk stops when no band meets threshold (zone ended or identity lost).",
  depth1: "Never used — only display_intervals from min_depth=2 chart path.",
  uncertainty:
    "identity_uncertain=true when best score in [0.35, 0.45) without shared origin.",
};

const DEFAULTS = {
  slot_minutes: 5,
  age_gate_bars: 10,
  max_lookback_bars: 600,
  min_match_score: 0.35,
};

function isoFromMs(ms) {
  return new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z");
}

function priceIoU(a0, a1, b0, b1) {
  const lo = Math.max(a0, b0);
  const hi = Math.min(a1, b1);
  if (hi < lo) return 0;
  const inter = hi - lo;
  const union = Math.max(a1, b1) - Math.min(a0, b0);
  return union > 0 ? inter / union : 0;
}

function originJaccard(origA, origB) {
  const A = new Set(origA || []);
  const B = new Set(origB || []);
  if (!A.size && !B.size) return 0;
  let inter = 0;
  A.forEach((o) => {
    if (B.has(o)) inter++;
  });
  const union = new Set([...A, ...B]).size;
  return union ? inter / union : 0;
}

function matchBand(band, ref) {
  const iou = priceIoU(
    Number(band.bottom_price),
    Number(band.top_price),
    Number(ref.upper_pool_lower),
    Number(ref.upper_pool_upper)
  );
  const oj = originJaccard(band.origins, ref.upper_pool_origins || ref.origins);
  const shared = (ref.upper_pool_origins || ref.origins || []).filter((o) =>
    (band.origins || []).includes(o)
  ).length;
  const score = 0.6 * iou + 0.4 * oj;
  const ok =
    score >= DEFAULTS.min_match_score || (iou >= 0.2 && shared >= 1);
  const uncertain = ok && score < 0.45 && shared === 0;
  return { ok, score, iou, origin_jaccard: oj, shared_origin_count: shared, uncertain };
}

function upperBandsAt(AD, pools, asOfIso, slotMin) {
  const landscape = buildDepth2Intervals(AD, pools, asOfIso, slotMin);
  return (landscape.display_intervals || []).filter((x) => x.side === "upper");
}

function bestMatch(bands, ref) {
  let best = null;
  for (const b of bands) {
    const m = matchBand(b, ref);
    if (!m.ok) continue;
    if (!best || m.score > best.score) best = { band: b, ...m };
  }
  return best;
}

function tracePoolAge(pools, ref, signalUtc, contextFromUtc, cfg) {
  const slotMs = cfg.slot_minutes * 60 * 1000;
  const { AD } = loadAdapter(cfg.adapter_path);
  const signalMs = parseIsoMs(signalUtc);
  const contextFromMs = contextFromUtc ? parseIsoMs(contextFromUtc) : 0;

  const timeline = [];
  let t = signalMs;
  let firstSeenMs = signalMs;
  let bars = 0;
  let anyUncertain = false;

  while (t >= contextFromMs && bars < cfg.max_lookback_bars) {
    const asOf = isoFromMs(t);
    const bands = upperBandsAt(AD, pools, asOf, cfg.slot_minutes);
    const hit = bestMatch(bands, ref);
    if (!hit) break;
    if (hit.uncertain) anyUncertain = true;
    timeline.push({
      as_of_utc: asOf,
      match_score: hit.score,
      band_bottom: hit.band.bottom_price,
      band_top: hit.band.top_price,
      depth: hit.band.depth,
      origins: hit.band.origins,
    });
    firstSeenMs = t;
    bars += 1;
    t -= slotMs;
  }

  timeline.reverse();
  const ageBars = bars;
  const ageMinutes = ageBars * cfg.slot_minutes;
  const gate = cfg.age_gate_bars;
  const eligible = ageBars >= gate;
  const eligibleSinceMs = firstSeenMs + (gate - 1) * slotMs;
  let barsBetween = null;
  let eligibleSinceUtc = null;
  if (eligible) {
    eligibleSinceUtc = isoFromMs(eligibleSinceMs);
    barsBetween = Math.round((signalMs - eligibleSinceMs) / slotMs);
  }

  return {
    touch_pool_first_seen_utc: ageBars > 0 ? isoFromMs(firstSeenMs) : null,
    touch_pool_age_bars: ageBars,
    touch_pool_age_minutes: ageMinutes,
    eligible_by_age_10bars: eligible,
    eligible_since_utc: eligibleSinceUtc,
    bars_between_eligibility_and_signal: barsBetween,
    projected_eligible_since_utc_if_continuous: isoFromMs(eligibleSinceMs),
    signal_before_age_gate: !eligible,
    signal_after_age_gate: eligible,
    identity_uncertain: anyUncertain,
    lookback_stopped_reason:
      ageBars === 0
        ? "no_match_at_signal"
        : t < contextFromMs
          ? "context_from_reached"
          : bars >= cfg.max_lookback_bars
            ? "max_lookback"
            : "pool_track_ended",
    backward_timeline_sample: timeline.slice(0, 5).concat(
      timeline.length > 10 ? [{ note: "..." }] : [],
      timeline.slice(-5)
    ),
  };
}

function enrichCandidates(input) {
  const cfg = { ...DEFAULTS, ...(input.config || {}) };
  const pools = input.pools || [];
  const contextFrom =
    input.context_from_utc ||
    (input.window && input.window.context_from_utc) ||
    null;
  const out = [];
  for (const cand of input.candidates || []) {
    const ref = {
      upper_pool_lower: cand.upper_pool_lower,
      upper_pool_upper: cand.upper_pool_upper,
      upper_pool_origins:
        cand.upper_pool_origins ||
        (cand.candidate && cand.candidate.upper_pool_origins) ||
        [],
    };
    const age = tracePoolAge(
      pools,
      ref,
      cand.signal_close_utc,
      contextFrom,
      cfg
    );
    out.push({
      ...cand,
      pool_age: age,
      pool_identity_rules: IDENTITY_DOC,
    });
  }
  return {
    identity_rules: IDENTITY_DOC,
    config: cfg,
    context_from_utc: contextFrom,
    candidates: out,
  };
}

if (require.main === module) {
  const chunks = [];
  process.stdin.on("data", (c) => chunks.push(c));
  process.stdin.on("end", () => {
    try {
      const input = JSON.parse(Buffer.concat(chunks).toString("utf8"));
      process.stdout.write(JSON.stringify(enrichCandidates(input)));
    } catch (e) {
      process.stderr.write(String(e.stack || e));
      process.exit(1);
    }
  });
}

module.exports = {
  enrichCandidates,
  tracePoolAge,
  matchBand,
  priceIoU,
  IDENTITY_DOC,
};
