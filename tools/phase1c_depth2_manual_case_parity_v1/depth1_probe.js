"use strict";
/** Read-only depth-1 visible zones at as-of (chart path min-origins=1). */
const {
  loadAdapter,
  chartGeometryAtAsOf,
  enrichOriginsFromBands,
  parseIsoMs,
} = require("../phase1b_depth2_landscape_pilot_v1/analyzer_depth2_landscape.js");

function depth1DisplayZones(pools, asOfIso, slotMinutes) {
  const { AD } = loadAdapter();
  const asOfMs = parseIsoMs(asOfIso);
  const chart = chartGeometryAtAsOf(AD, pools, asOfMs, slotMinutes || 5);
  return (chart.depth1 || []).map((z, i) => {
    const en = enrichOriginsFromBands(z, pools, asOfMs, z.side);
    return {
      side: z.side,
      bottom_price: z.bottom_price,
      top_price: z.top_price,
      depth: z.metadata.depth,
      origins: en.origins,
      zone_index: i,
      provenance: "chart_path_min_depth_1",
    };
  });
}

function zonesContainingPrice(zones, price, side) {
  const p = Number(price);
  return zones.filter(
    (z) =>
      z.side === side && z.bottom_price <= p && p <= z.top_price
  );
}

if (require.main === module) {
  const chunks = [];
  process.stdin.on("data", (c) => chunks.push(c));
  process.stdin.on("end", () => {
    try {
      const input = JSON.parse(Buffer.concat(chunks).toString("utf8"));
      const zones = depth1DisplayZones(
        input.pools || [],
        input.as_of_utc,
        input.slot_minutes || 5
      );
      const price = input.price;
      const out = { depth1_zones: zones };
      if (price != null) {
        out.contains_upper_depth1 = zonesContainingPrice(zones, price, "upper");
        out.contains_upper_depth2 = (input.depth2_upper || []).filter(
          (z) => z.bottom_price <= price && price <= z.top_price
        );
      }
      process.stdout.write(JSON.stringify(out));
    } catch (err) {
      process.stderr.write(String(err.stack || err));
      process.exit(1);
    }
  });
}

module.exports = { depth1DisplayZones, zonesContainingPrice };
