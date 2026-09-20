# current_direction_feature_contract_v1_draft

**Status:** DRAFT — not frozen for live use  
**Generated (UTC):** `2026-09-05T14:32:25Z`  
**Research-only.** Separate from `LIQUIDITY_DESTINATION_BIAS`.

## Cutoff rules (inherited from Phase-2B proofs)

```text
available_at <= T0
```

For fully aggregated time buckets (conservative):

```text
bucket_end < T0
```

Also:

- Public-trade feature buckets must be **fully closed before T0**.
- The **T0 second must not** be used as a finished feature bucket.
- OI only from a closed bucket already available at T0.
- Full-session POC/VAH/VAL at earlier T0 = **lookahead** → forbidden.

## Knowledge cutoff

```text
feature_cutoff_utc == t0_utc
knowledge_cutoff_utc == t0_utc
```

All feature inputs must satisfy `available_at <= feature_cutoff_utc`.

## Lookback windows (candidates — not outcome-tuned)

```text
5s, 15s, 30s, 60s, 180s, 300s, full_available_full_ob_preroll (<=600s)
```

Windows clipped to available pre-roll; if required min pre-roll missing → `FULL_OB_CACHE_INCOMPLETE` / `INSUFFICIENT_DATA`.

## Feature groups

### Full OB (require `--require-full-ob`)

| Feature | Safe rule | Notes |
|---------|-----------|-------|
| Bid/Ask liquidity distance bands | book as_of T0 from deltas ≤ T0 | needs pre-roll or live snapshot+history |
| Book imbalance / microprice / spread | as_of T0 | live socket snapshot can supply point state |
| Top-of-book change / add-cancel / refill / consumption | event_time ≤ T0 over lookback | needs delta history (bridge) |
| Wall persistence / pulling-stacking / pressure | same | |
| Sequence/gap status | metadata at T0 | fail-closed on open gap |

### Public Trades

| Feature | Safe rule |
|---------|-----------|
| Taker buy/sell volume, delta, count | `event_time < T0` |
| Aggressor speed / notional per second | closed seconds only (`bucket_end < T0`) |
| Price move per aggressive volume | same |
| Absorption hints | descriptive only until frozen |

### Context

| Feature | Safe rule | Status |
|---------|-----------|--------|
| Price velocity / short momentum / realized vol | closed trade buckets `< T0` | SAFE with SoT freeze |
| OI change | last closed received bucket `bucket_end ≤ T0` | SAFE_IF_CLOSED |
| Liquidations | `event_time < T0` (or receive≤T0 for live parity) | CONTEXT / sparse OK |
| Causal market profile to T0 | build only from data ≤ T0 | BLOCKED until causal builder |
| Position vs POC/VAH/VAL | forming causal only | BLOCKED_DEFAULT full-session |
| EMA/trend if present | indicator available_at ≤ T0 | optional |

## Mandatory vs optional (V1 pilot)

**Mandatory for `--require-full-ob`:** Full-OB pre-roll coverage ≥ configured min, sequence OK, live book ready, public trades fresh enough for reference price + path observer.

**Optional:** OI, liquidations, profile, OB200, EMA.

## Explicit non-goals

- No LLD pool destination labels.
- No pool presence required.
- No calibrated probability claims in V1.
