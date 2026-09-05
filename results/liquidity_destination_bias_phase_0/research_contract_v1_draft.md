# LIQUIDITY_DESTINATION_BIAS — Research Contract V1 (Draft)

**Status:** Phase-0 draft only — not implemented, not frozen for trading.  
**Contract id (proposed):** `liquidity_destination_bias_contract_v1`  
**Scope:** Research classification of which of two frozen targets is touched first.  
**Non-goals:** live order routing; coupling to frozen 30m A+ market-profile edge strategy; claiming profitability.

---

## 1. Prognosezeitpunkt `T0`

- `T0` is an explicit UTC timestamp (second resolution for V1 labels; sub-second reserved).
- **Hard rule:** every feature and both target blocks use only information with `event_time <= T0` (and `known_at <= T0` for derived pools).
- Receive-time may be logged for freshness audits but **must not** admit future event payloads.
- Engine used for `--timestamp` and `--now` must be identical; `--now` sets `T0 = floor_utc_second(now)` and records data freshness.

## 2. Startbedingung (objective, strategy-decoupled)

A case is **eligible to open** at `T0` iff all hold:

1. **Price between two candidates:** mid (or last trade if mid missing) satisfies  
   `lower_target.price_high < price_t0 < upper_target.price_low`  
   with non-overlapping bands and gap at least `min_gap_bps` (default proposal: **5 bps**).
2. **Dual-target present:** exactly one primary UPPER and one primary LOWER candidate after prioritization.
3. **Warm-up:** at least `warmup_seconds` (default **300s**) of mandatory sources before `T0`.
4. **Not already touching:** price has not touched either band in the last `touch_cooldown_seconds` (default **30s**).
5. **Not A+-coupled:** start condition must **not** require VAH/VAL cross-in, nested profile edge, or A+ scanner roles.

Sampling modes for history builder (choose one per run; record in config):

- `grid`: evaluate every N seconds when (1)–(4) hold (default N=60).
- `event`: open when a new dual-target pair appears or rank changes.

## 3. Zielblock-Definition (do not conflate classes)

V1 must pick **one primary block class** per run and store it in config:

| Class | Meaning | Primary detectors | Book-coverage gate |
|-------|---------|-------------------|--------------------|
| `RESTING_WALL` | Visible resting liquidity in reconstructed book | OB200 walls / execution or structure wall detectors | **Mandatory** both bands inside recorded book span at `T0` |
| `LLD_POOL` | Historically derived liquidity-location pool | `liquidity_pool_signal` / TRP LLD engine | Soft: pool geometry known_at≤T0; optional wall-overlap enrichment |
| `PROFILE_LEVEL` | POC/VAH/VAL or similar | market_profile / fight profiles | Soft; **forbidden as sole start condition in V1 default** |

**Recommended V1 default:** `LLD_POOL` for target identity + optional `RESTING_WALL` overlap score as feature — because BTC OB200 span ≈ **8 bps** cannot host many “far” resting walls (evidence: CH pilot hour span_bps≈8.08).

### Recognition / prioritization at `T0` (LLD_POOL default)

- UPPER = nearest ASK pool with `pool.lower > price_t0`, meeting min size/persistence.
- LOWER = nearest BID pool with `pool.upper < price_t0`, meeting min size/persistence.
- Size: pool strength / width / notional proxy ≥ configured floors (reuse Stage-A zone-fill ideas where applicable).
- Persistence: pool `known_at` age ≥ `min_persist_seconds` (default **60s**) and not invalidated as-of `T0`.
- Spoof/pulling: for RESTING_WALL class, require persistence across ≥K book samples and cancel-rate below threshold; for LLD_POOL, pulling is N/A — mark `spoof_filter=not_applicable`.
- **Freeze:** serialize immutable `upper_target` and `lower_target` structs into the episode record at `T0` (prices, side, class, ids, sizes, known_at). Later path labels may **never** move these prices.

## 4. First-Touch Outcome

**Primary price path (V1):** 1s public-trade bucket OHLC reconstructed as `high/low` from trades in each second (fallback: mid from OB200 1s if trades empty that second → mark `price_source=mixed` and eligibility PARTIAL).

**Touch definition (V1 primary):** first time path intersects the **near edge** of the frozen band:

- UPPER touch when `high_t >= upper_target.price_low`
- LOWER touch when `low_t <= lower_target.price_high`

**Horizon:** primary **60 minutes**; also compute labels for **5 / 15 / 30 / 60** (sensitivity columns).

**Outcomes:**

| Code | Rule |
|------|------|
| `UPPER_FIRST` | Upper near-edge touched strictly earlier than lower |
| `LOWER_FIRST` | Lower earlier |
| `SIMULTANEOUS_OR_AMBIGUOUS` | Both touched in same 1s bucket OR within `ambiguity_ms` (default 1000ms) |
| `NEITHER_WITHIN_HORIZON` | Neither touched by `T0+H` |
| `INELIGIBLE_DATA` | Failed eligibility (see §5) |

**Spikes:** V1 labels pure touch (no acceptance filter). Acceptance (hold N seconds beyond edge) is a **secondary label** only (`ACCEPT_UPPER` etc.), never mixed into the primary metric without renaming.

**Fees / PnL:** forbidden in outcome definition.

## 5. Eligibility hard blockers → `INELIGIBLE_DATA`

- Target outside recorded book span when class=`RESTING_WALL` (`edge_book_coverage` OUTSIDE)
- OB snapshot/delta gap across `[T0-warmup, T0]` or across path window for mandatory sources
- Missing/broken sequence for OB200 reconstruction used in features
- Missing public trades for path window (or below min trade density)
- Missing price path
- Unclear/overlapping targets; rank ties without deterministic tie-break
- Target already touched at `T0`
- Upper/lower overlap or gap < `min_gap_bps`
- Insufficient warm-up
- Any feature using `event_time > T0` (leakage)
- Target mutated after freeze
- `--require-complete` and any mandatory source ≠ COMPLETE

Reuse SR `eligibility_contract.py` pattern with LDB-specific mandatory set:

- Mandatory: `PUBLIC_TRADES` (path), `TARGET_GEOMETRY` (pools or walls), `PRICE_PATH`
- Conditional mandatory: `OB200` if any book feature or RESTING_WALL class
- Context: OI, liquidations, profile overlays

## 6. Bias output (research classification only)

Map calibrated P(UPPER_FIRST) vs baselines to:

`STRONG_LONG_BIAS | MODERATE_LONG_BIAS | NEUTRAL_WAIT | MODERATE_SHORT_BIAS | STRONG_SHORT_BIAS | INSUFFICIENT_DATA`

V1 may start **rule-based** (distance, imbalance, taker delta) before any model.  
**Explicit:** good first-touch accuracy ≠ profitable trade.

## 7. Common-denominator rule (Full OB)

Historical training features must be computable from sources available on the eligible historical calendar (primarily OB200 + trades + LLD).  
**Forbidden:** train on Full-OB-only features then score live Full-OB without a matched historical Full-OB population.  
Episodic FR Full-OB may power **shadow enrichment** and ablation studies, never silent train/serve skew.

## 8. Versioning

Episode store and config must embed: `contract_version`, `feature_set_version`, `block_class`, `horizon_set`, `source_fingerprint`s, `git_commit` (when implemented).
