# Pilot Validation Plan

## Pilot P1 — Single COMPLETE Full-OB hour (BTC)
Window: `2026-09-05T17:00:00Z`–`18:00:00Z` (manifest COMPLETE; replay ok=True, 0 crossed/neg in probe).
Join: live `public_trades_canonical`, `open_interest_5s`, `all_liquidations`, mid from book.
Deliverables: 3600 state rows (or fewer if partial seconds invalid), 5–10 feature sanity checks, 2–3 manual event spot-checks.

Duration estimate: 0.5–1 engineer-day compute+review.

## Pilot P2 — Prefix/causality parity
Build states on truncated vs full hour file; assert equality for t ≤ truncate.

## Pilot P3 — GAP refusal
Attempt build on a GAP hour; engine must mark PARTIAL/GAP and refuse FULL claims.

## Pilot P4 — OB200 day bridge
One BTC day inside 2026-08-25–31 with research or live joins; compare mid path vs OB200 CH snapshots where present.

## Pilot P5 — Episodes/outcomes offline
≥4h BTC continuous PARTIAL/FULL mix; embargo tests.

## Expansion
Grow Full-OB COMPLETE calendar naturally; rematerialize research trades/OI/liq through Sep before claiming research-DB-only pipelines.
DOGE only after BTC P1–P3 pass.

## Not in pilot
Profit claims, model training beyond descriptive stats, collector restarts.
