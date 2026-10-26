"use strict";
/**
 * Phase 2G — causal upper-pool opportunity grouping (research dry-run).
 * Extends Phase 2C identity matching; no production scanner changes.
 */
const {
  loadAdapter,
  buildDepth2Intervals,
  parseIsoMs,
} = require("../phase1b_depth2_landscape_pilot_v1/analyzer_depth2_landscape.js");
const {
  matchBand,
  priceIoU,
  IDENTITY_DOC,
} = require("../phase2c_5m_pd1_depth2_candidate_pool_age_v1/pool_age_enricher.js");

const DEDUP_VERSION = "phase2g_opportunity_dedup_v1";
const SLOT_MIN = 5;

function isoFromMs(ms) {
  return new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z");
}

function originIntersection(a, b) {
  const B = new Set(b || []);
  return (a || []).filter((o) => B.has(o));
}

function mergeZoneRef(ref, band) {
  const lo = Math.min(
    Number(ref.upper_pool_lower),
    Number(band.bottom_price)
  );
  const hi = Math.max(
    Number(ref.upper_pool_upper),
    Number(band.top_price)
  );
  const origins = new Set([
    ...(ref.upper_pool_origins || []),
    ...(band.origins || []),
  ]);
  return {
    upper_pool_lower: lo,
    upper_pool_upper: hi,
    upper_pool_origins: [...origins].sort(),
  };
}

function refFromCandidate(cand) {
  return {
    upper_pool_lower: Number(cand.upper_pool_lower),
    upper_pool_upper: Number(cand.upper_pool_upper),
    upper_pool_origins: cand.upper_pool_origins || [],
  };
}

function upperBandsAt(AD, pools, asOfIso, slotMin) {
  const landscape = buildDepth2Intervals(AD, pools, asOfIso, slotMin);
  return (landscape.display_intervals || []).filter((x) => x.side === "upper");
}

function bestMatchBand(bands, ref) {
  let best = null;
  for (const b of bands) {
    const m = matchBand(b, ref);
    if (!m.ok) continue;
    if (!best || m.score > best.score) best = { band: b, ...m };
  }
  return best;
}

function bestBandSharedOrigins(bands, anchorOrigins, minShared = 2) {
  const A = new Set(anchorOrigins || []);
  let best = null;
  for (const b of bands) {
    const shared = (b.origins || []).filter((o) => A.has(o)).length;
    if (shared < minShared) continue;
    const score = 0.4 + shared * 0.1;
    if (!best || shared > best.shared_origin_count) {
      best = {
        band: b,
        ok: true,
        score,
        shared_origin_count: shared,
        origin_lineage_match: true,
      };
    }
  }
  return best;
}

function continuityBetween(pools, ref, fromUtc, toUtc, adapterPath, slotMin) {
  const { AD } = loadAdapter(adapterPath);
  const fromMs = parseIsoMs(fromUtc);
  const toMs = parseIsoMs(toUtc);
  if (toMs <= fromMs) {
    return { continuous: true, bars_checked: 0, steps: [] };
  }
  let refZone = { ...ref, upper_pool_origins: [...(ref.upper_pool_origins || [])] };
  const steps = [];
  const slotMs = slotMin * 60 * 1000;
  let bars = 0;
  for (let t = toMs; t > fromMs; t -= slotMs) {
    const asOf = isoFromMs(t);
    const bands = upperBandsAt(AD, pools, asOf, slotMin);
    const hit = bestMatchBand(bands, refZone);
    if (!hit) {
      return {
        continuous: false,
        broken_at_utc: asOf,
        bars_checked: bars,
        steps,
      };
    }
    steps.push({
      as_of_utc: asOf,
      match_score: hit.score,
      band_bottom: hit.band.bottom_price,
      band_top: hit.band.top_price,
      depth: hit.band.depth,
    });
    refZone = mergeZoneRef(refZone, hit.band);
    bars += 1;
  }
  return { continuous: true, bars_checked: bars, steps };
}

function opportunityIdFromFirst(cand) {
  const sig = String(cand.signal_close_utc).replace(/[-:]/g, "").replace("Z", "Z");
  return `OPP_${cand.candidate_id}`;
}

function identityConfidence(hit, cont, sharedCount) {
  if (!hit || !cont.continuous) return "NOT_VERIFIED";
  if (hit.score >= 0.45 && sharedCount >= 1) return "HIGH";
  if (hit.score >= 0.35 || sharedCount >= 1) return "MEDIUM";
  return "LOW";
}

function initOpportunityRefs(cand, bandsNow) {
  const touchRef = refFromCandidate(cand);
  const hit = bestMatchBand(bandsNow, touchRef);
  const linkRef =
    hit && hit.ok ? mergeZoneRef(touchRef, hit.band) : { ...touchRef };
  return {
    anchorRef: { ...touchRef, upper_pool_origins: [...touchRef.upper_pool_origins] },
    linkRef,
    trackedRef: { ...linkRef, upper_pool_origins: [...linkRef.upper_pool_origins] },
  };
}

function originLineageContinuity(
  pools,
  anchorOrigins,
  fromUtc,
  toUtc,
  adapterPath,
  slotMin
) {
  const { AD } = loadAdapter(adapterPath);
  const fromMs = parseIsoMs(fromUtc);
  const toMs = parseIsoMs(toUtc);
  if (toMs <= fromMs) {
    return { continuous: true, bars_checked: 0, steps: [] };
  }
  const core = new Set(anchorOrigins || []);
  const slotMs = slotMin * 60 * 1000;
  const steps = [];
  let bars = 0;
  for (let t = toMs; t > fromMs; t -= slotMs) {
    const asOf = isoFromMs(t);
    const bands = upperBandsAt(AD, pools, asOf, slotMin);
    const hit = bands.find((b) =>
      (b.origins || []).some((o) => core.has(o))
    );
    if (!hit) {
      return {
        continuous: false,
        broken_at_utc: asOf,
        bars_checked: bars,
        steps,
      };
    }
    steps.push({
      as_of_utc: asOf,
      band_bottom: hit.bottom_price,
      band_top: hit.top_price,
      shared_origin_count: (hit.origins || []).filter((o) => core.has(o)).length,
    });
    bars += 1;
  }
  return { continuous: true, bars_checked: bars, steps };
}

function resolveContinuity(pools, opp, signalUtc, adapterPath, slotMin) {
  const attempts = [
    ["LINK_REF_SINCE_LAST_SIGNAL", opp.linkRef, opp.last_signal_utc],
    ["TRACKED_REF_SINCE_LAST_SIGNAL", opp.trackedRef, opp.last_signal_utc],
    ["ANCHOR_REF_SINCE_FIRST_SIGNAL", opp.anchorRef, opp.first_signal_utc],
    ["TRACKED_REF_SINCE_FIRST_SIGNAL", opp.trackedRef, opp.first_signal_utc],
  ];
  for (const [mode, ref, fromUtc] of attempts) {
    const cont = continuityBetween(
      pools,
      ref,
      fromUtc,
      signalUtc,
      adapterPath,
      slotMin
    );
    if (cont.continuous) {
      return { ...cont, continuity_mode: mode, continuity_ref: mode.includes("ANCHOR") ? "anchor" : "tracked_or_link" };
    }
  }
  const lineage = originLineageContinuity(
    pools,
    opp.anchorRef.upper_pool_origins,
    opp.last_signal_utc,
    signalUtc,
    adapterPath,
    slotMin
  );
  if (lineage.continuous) {
    return {
      ...lineage,
      continuity_mode: "ORIGIN_LINEAGE_SINCE_LAST_SIGNAL",
    };
  }
  const lineageLong = originLineageContinuity(
    pools,
    opp.anchorRef.upper_pool_origins,
    opp.first_signal_utc,
    signalUtc,
    adapterPath,
    slotMin
  );
  if (lineageLong.continuous) {
    return {
      ...lineageLong,
      continuity_mode: "ORIGIN_LINEAGE_SINCE_FIRST_SIGNAL",
    };
  }
  return { continuous: false, continuity_mode: "BROKEN", bars_checked: 0, steps: [] };
}

function strictLinkOk(cand, opp, bandsNow, cont, adapterPath) {
  const touchRef = refFromCandidate(cand);
  if (!cont.continuous) {
    return null;
  }
  let hit = bestMatchBand(bandsNow, opp.linkRef);
  let refForTouch = opp.linkRef;
  if (!hit || !hit.ok) {
    if (cont.continuity_mode.includes("TRACKED")) {
      hit = bestMatchBand(bandsNow, opp.trackedRef);
      refForTouch = opp.trackedRef;
    }
    if ((!hit || !hit.ok) && cont.continuity_mode.includes("ANCHOR")) {
      hit = bestMatchBand(bandsNow, opp.anchorRef);
      refForTouch = opp.anchorRef;
    }
  }
  const sharedAnchorPre = originIntersection(
    touchRef.upper_pool_origins,
    opp.anchorRef.upper_pool_origins
  );
  if ((!hit || !hit.ok) && sharedAnchorPre.length >= 2 && cont.continuous) {
    const lineage = bestBandSharedOrigins(
      bandsNow,
      opp.anchorRef.upper_pool_origins,
      2
    );
    if (lineage) {
      hit = lineage;
      refForTouch = {
        upper_pool_lower: Number(lineage.band.bottom_price),
        upper_pool_upper: Number(lineage.band.top_price),
        upper_pool_origins: lineage.band.origins || [],
      };
    }
  }
  if (!hit || !hit.ok) {
    return null;
  }
  const touchHit = matchBand(
    {
      bottom_price: touchRef.upper_pool_lower,
      top_price: touchRef.upper_pool_upper,
      origins: touchRef.upper_pool_origins,
    },
    refForTouch
  );
  const sharedAnchor = originIntersection(
    touchRef.upper_pool_origins,
    opp.anchorRef.upper_pool_origins
  );
  const sharedLink = originIntersection(
    touchRef.upper_pool_origins,
    opp.linkRef.upper_pool_origins
  );
  const shared = [...new Set([...sharedAnchor, ...sharedLink])];
  const overlap = priceIoU(
    touchRef.upper_pool_lower,
    touchRef.upper_pool_upper,
    refForTouch.upper_pool_lower,
    refForTouch.upper_pool_upper
  );
  const lineageMode = String(cont.continuity_mode || "").includes("ORIGIN_LINEAGE");
  const originOk = shared.length >= 1 || (lineageMode && sharedAnchorPre.length >= 2);
  const geometryOk =
    touchHit.ok ||
    hit.score >= 0.35 ||
    overlap >= 0.15 ||
    (lineageMode && hit.ok);
  if (!originOk || !geometryOk) {
    return null;
  }
  return { hit, touchHit, shared, overlap, cont };
}

function assignOpportunities(input) {
  const pools = input.pools || [];
  const adapterPath = input.adapter_path;
  const candidates = [...(input.candidates || [])].sort((a, b) =>
    a.signal_close_utc < b.signal_close_utc
      ? -1
      : a.signal_close_utc > b.signal_close_utc
        ? 1
        : 0
  );

  const { AD } = loadAdapter(adapterPath);
  const openOpps = [];
  const assignments = [];
  const timeline = [];

  for (const cand of candidates) {
    const signalUtc = cand.signal_close_utc;
    const bandsNow = upperBandsAt(AD, pools, signalUtc, SLOT_MIN);
    let linked = null;
    let bestRank = -1;

    const oppsByRecency = [...openOpps].sort((a, b) =>
      a.last_signal_utc < b.last_signal_utc ? 1 : -1
    );

    for (const opp of oppsByRecency) {
      const cont = resolveContinuity(
        pools,
        opp,
        signalUtc,
        adapterPath,
        SLOT_MIN
      );
      const verdict = strictLinkOk(cand, opp, bandsNow, cont, adapterPath);
      if (!verdict) continue;

      const rank =
        verdict.hit.score +
        (verdict.touchHit.ok ? verdict.touchHit.score : 0) +
        verdict.shared.length * 0.1;
      if (rank > bestRank) {
        bestRank = rank;
        linked = { opp, ...verdict };
      }
    }

    if (linked) {
      const { opp, cont, hit, touchHit, shared, overlap } = linked;
      const conf = identityConfidence(hit, cont, shared.length);
      const poolStatus = cont.continuous ? "CONTINUOUS_TRACK" : "BROKEN";
      opp.linkRef = mergeZoneRef(opp.linkRef, hit.band);
      opp.trackedRef = mergeZoneRef(opp.trackedRef, hit.band);
      opp.last_signal_utc = signalUtc;
      opp.last_candidate_id = cand.candidate_id;
      opp.candidate_ids.push(cand.candidate_id);

      const row = {
        candidate_id: cand.candidate_id,
        signal_close_utc: signalUtc,
        entry_open_utc: cand.entry_open_utc,
        opportunity_id: opp.opportunity_id,
        first_candidate_id: opp.first_candidate_id,
        first_signal_utc: opp.first_signal_utc,
        upper_zone_lower: Number(cand.upper_pool_lower),
        upper_zone_upper: Number(cand.upper_pool_upper),
        shared_origin_ids: shared,
        price_overlap_score: overlap,
        pool_continuity_status: poolStatus,
        duplicate_of_candidate_id: opp.first_candidate_id,
        dedup_reason: "SAME_OPPORTUNITY_DUPLICATE",
        identity_confidence: conf,
        manual_label: "",
        link_evidence: {
          continuity_mode: cont.continuity_mode,
          continuity_bars_checked: cont.bars_checked,
          landscape_match_score: hit.score,
          touch_match_score: touchHit.ok ? touchHit.score : null,
          shared_origin_count: shared.length,
        },
      };
      assignments.push(row);
      timeline.push({
        event: "candidate_linked",
        candidate_id: cand.candidate_id,
        opportunity_id: opp.opportunity_id,
        signal_close_utc: signalUtc,
        continuity: cont,
        link_ref_after: opp.linkRef,
      });
    } else {
      const refs = initOpportunityRefs(cand, bandsNow);
      const opp = {
        opportunity_id: opportunityIdFromFirst(cand),
        first_candidate_id: cand.candidate_id,
        first_signal_utc: signalUtc,
        last_signal_utc: signalUtc,
        last_candidate_id: cand.candidate_id,
        candidate_ids: [cand.candidate_id],
        anchorRef: refs.anchorRef,
        linkRef: refs.linkRef,
        trackedRef: refs.trackedRef,
      };
      openOpps.push(opp);

      assignments.push({
        candidate_id: cand.candidate_id,
        signal_close_utc: signalUtc,
        entry_open_utc: cand.entry_open_utc,
        opportunity_id: opp.opportunity_id,
        first_candidate_id: opp.first_candidate_id,
        first_signal_utc: opp.first_signal_utc,
        upper_zone_lower: Number(cand.upper_pool_lower),
        upper_zone_upper: Number(cand.upper_pool_upper),
        shared_origin_ids: cand.upper_pool_origins || [],
        price_overlap_score: 1,
        pool_continuity_status: "PRIMARY_NEW_OPPORTUNITY",
        duplicate_of_candidate_id: "",
        dedup_reason: "PRIMARY",
        identity_confidence: "HIGH",
        manual_label: "",
        link_evidence: {
          landscape_match_score: bestMatchBand(bandsNow, refs.anchorRef)?.score ?? null,
        },
      });
      timeline.push({
        event: "opportunity_opened",
        candidate_id: cand.candidate_id,
        opportunity_id: opp.opportunity_id,
        signal_close_utc: signalUtc,
        anchor_ref: opp.anchorRef,
        link_ref: opp.linkRef,
      });
    }
  }

  const grouped = [];
  for (const opp of openOpps) {
    grouped.push({
      opportunity_id: opp.opportunity_id,
      first_candidate_id: opp.first_candidate_id,
      first_signal_utc: opp.first_signal_utc,
      last_signal_utc: opp.last_signal_utc,
      candidate_count: opp.candidate_ids.length,
      candidate_ids: opp.candidate_ids,
      tracked_zone_lower: opp.trackedRef.upper_pool_lower,
      tracked_zone_upper: opp.trackedRef.upper_pool_upper,
      tracked_origin_ids: opp.trackedRef.upper_pool_origins,
      anchor_zone_lower: opp.anchorRef.upper_pool_lower,
      anchor_zone_upper: opp.anchorRef.upper_pool_upper,
    });
  }

  return {
    version: DEDUP_VERSION,
    identity_rules_phase2c: IDENTITY_DOC,
    research_params: {
      min_pool_age_bars: input.min_pool_age_bars ?? 5,
      min_pool_gap_pct: input.min_pool_gap_pct ?? 0.5,
      note: "Age/gap are metadata only in 2G — not applied as hard filters.",
    },
    backtest_layers: {
      raw_signals: "assignments (one row per scanner candidate)",
      trade_opportunities: "grouped (one row per opportunity_id)",
      executed_trades: "reserved — backtester chooses entries from opportunities",
    },
    assignments,
    grouped_opportunities: grouped,
    opportunity_timeline: timeline,
  };
}

if (require.main === module) {
  const chunks = [];
  process.stdin.on("data", (c) => chunks.push(c));
  process.stdin.on("end", () => {
    try {
      const input = JSON.parse(Buffer.concat(chunks).toString("utf8"));
      process.stdout.write(JSON.stringify(assignOpportunities(input)));
    } catch (e) {
      process.stderr.write(String(e.stack || e));
      process.exit(1);
    }
  });
}

module.exports = {
  assignOpportunities,
  continuityBetween,
  originLineageContinuity,
  resolveContinuity,
  mergeZoneRef,
  opportunityIdFromFirst,
  DEDUP_VERSION,
};
