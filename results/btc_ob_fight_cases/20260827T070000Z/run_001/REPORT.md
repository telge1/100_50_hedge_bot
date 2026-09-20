# BTC OB Fight Fact Report (Phase 0–1)

**Status:** `FACTS_PARTIAL_RULES_UNFROZEN`
**Schema:** `btc_ob_fight_facts_v2_0`

## Anchor Profile

- Ankerpreis: n/a
- TPO PROFILE — 30m BRACKET PRESENCE
- TPO POC/VAH/VAL: n/a / n/a / n/a
- TPO-Status: TPO_PROFILE_DATA_INSUFFICIENT
- TPO↔Volume Konfluenz-Status: INVALID_OR_MISSING_PROFILE
- VOLUME PROFILE — BASE VOLUME
- Volume-Profile: INTEGRITY_FAILED

## Level-Episoden (Fakten)


## Public-Trade-Fenster


## Open Interest

- OI Start: +47921.73 Source-Einheiten (open_interest)
- OI Ende: +47985.35 Source-Einheiten (open_interest)
- OI Delta: +63.62 Source-Einheiten (open_interest) (+0.133 %)
- OI Sample Count: 718
- OI Freshness: 2026-08-27T07:29:55Z
- OI Source Field: open_interest

## Liquidationen

- Liquidationen gesamt: 271
- Long-Liquidationen: 2 (+0.01 Mio. USD)
- Short-Liquidationen: 269 (+2.01 Mio. USD)
- Größtes Ereignis: +0.30 Mio. USD @ 2026-08-27 06:30:04.455 UTC
- Side-Semantik: liquidated_position_side: LIQUIDATED_LONG / LIQUIDATED_SHORT
- Freshness: 2026-08-27T07:27:09.9Z

## LIQUIDATION FLOW FACTS — NOT_EVALUATED

- Short-Liquidationen: 269 Events, 25.0120 BTC executed, bankruptcy-ref-quote 2,011,120 USD
- Long-Liquidationen: 2 Events, 0.1130 BTC executed, bankruptcy-ref-quote 9,061 USD
- Gesamte Taker-Buys: 0.0000 BTC / +0.00 Mio. USD
- Gesamte Taker-Sells: 0.0000 BTC / +0.00 Mio. USD
- Taker-Delta: +0.00 Mio. USD
- execution_price / execution_notional: NULL (unbekannt)
- Direkte Trade-ID-Zuordnung: nicht verfügbar (HEURISTIC_TEMPORAL_VOLUME_ASSOCIATION)
- Interpretation: NOT_EVALUATED
- ±100ms: allocated_liquidation_base 0.0000 BTC (0.00% of total_taker_buy_base); remaining_unattributed_taker_buy_base 0.0000 BTC; liquidation_capacity_coverage_pct=0.0%
- ±250ms: allocated_liquidation_base 0.0000 BTC (0.00% of total_taker_buy_base); remaining_unattributed_taker_buy_base 0.0000 BTC; liquidation_capacity_coverage_pct=0.0%
- ±500ms: allocated_liquidation_base 0.0000 BTC (0.00% of total_taker_buy_base); remaining_unattributed_taker_buy_base 0.0000 BTC; liquidation_capacity_coverage_pct=0.0%
- ±1000ms: allocated_liquidation_base 0.0000 BTC (0.00% of total_taker_buy_base); remaining_unattributed_taker_buy_base 0.0000 BTC; liquidation_capacity_coverage_pct=0.0%

## Orderbuch-Fakten

- Book-Samples: 3600
- Sample-Gap P50/P95/Max: 1.0 / 1.0 / 2.0 s
- Wall-Beobachtungen: Ask 18000 / Bid 18000
- Eindeutige Wall-Tracks: Ask 4967 / Bid 6512
- Eindeutige Preislevel: Ask 1290 / Bid 1476
- Quantity-Decreases: Ask 1767 / Bid 1594
- Trade-associated Decreases: Ask 0 / Bid 0
- Unmatched Decreases: Ask 1767 / Bid 1594
- Trade-associated Disappearances: Ask 0 / Bid 0
- Unmatched Disappearances: Ask 4962 / Bid 6507
- Refill-Sequenzen nach UNFROZEN_HEURISTIC: Ask 6346 / Bid 3795
- Tracks bis Fensterende sichtbar: Ask 5 / Bid 5
- Heuristik-Contract: UNFROZEN_HEURISTIC (wall_heuristics_v1); Tick-Size 0.1
- Wall-Schwellen (UNFROZEN): max_bps=800, qty_mult=3.0, match_frac=0.3, sample_interval_s=1

## Wall-Heuristik-Sequenzen (UNFROZEN)

- UNFROZEN_HEURISTIC: 10141 Refill-Sequenz(en) beobachtet (Ask 6346, Bid 3795).

## FIGHT FACTS — INTERPRETATION NOT EVALUATED

- Profile-state episodes: 0
- Outside episodes: 0
- Edge consumption events: 0
- Post-trade refills: 0
- Reclaim events: 0

BREAKOUT CONFIRMATION:       NOT_EVALUATED
FAILED BREAKOUT:             NOT_EVALUATED
BUYER/SELLER CONTROL:        NOT_EVALUATED
ABSORPTION:                  NOT_EVALUATED
TRADE DIRECTION:             null

## FIGHT SEQUENCE VALIDATION (Phase 2A.3) — RULES UNFROZEN

- Verdict: `BTC_OB_FIGHT_CANONICAL_ELIGIBILITY_BLOCKED`
- Canonical reclaim contract: `reclaim_event_contract_v3`
- RAW outside observations: 0
- Ambiguous reclaim candidates: 0
- Canonical outside excursions: 0
- Canonical reclaims: 0
- Raw state episodes: 0
- Edge visits (raw): upper=0 lower=0 (total=0)
- Cluster count gap=0: 0 (invariant_ok=True)
- Outside excursions raw/canonical/ambiguous: 0/0/0
- Reclaims (canonical v3): 0 (unique cross_ts=0)
- Nearby liquidity increases: Ask 0 / Bid 0 / Unknown 0
- Fight-time edge observability rows: 0
- Cluster counts by gap: {'0': 0, '1': 0, '2': 0, '5': 0, '10': 0, '30': 0, '60': 0}
- OB200 coverage metrics: {'sample_count': 0}
- Consumption metrics: {'rows': [], 'total_events': 0}
- Exact refills: 0
- Open excursions: 0
- Edge book coverage: {}
- OI/Liq coverage: {'visit_count': 0, 'excursion_count': 0, 'data_insufficient_count': 0}
- Same-timestamp ordering audited: True
- Legacy global-first reclaim enabled: False

BREAKOUT CONFIRMATION:       NOT_EVALUATED
FAILED BREAKOUT:             NOT_EVALUATED
ABSORPTION:                  NOT_EVALUATED
BUYER/SELLER CONTROL:        NOT_EVALUATED
TRADE DIRECTION:             null
RULES FROZEN:                false

## Nicht evaluiert

- Käufer-/Verkäuferkontrolle
- Absorption
- Breakout-Akzeptanz
- Long-/Short-Entry

## Manifest

- OB root: `None`
- auto_extension_enabled: `False`
- rules_frozen: `False`
