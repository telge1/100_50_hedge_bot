# Feature / impact window contract — AEF Discovery V1

**Contract version:** `aef_causal_contract/v1`  
**Price source V1:** public-trade bucket prices (Mid/BBO optional later when `ob_available`).

## 1. Flow vs impact separation

### Recommended default: **Variant B — post-flow impact**

```text
Flow window (aggressor measurement):     [t0, t1)
Impact window (price response):          (t1, t2]
Decision timestamp:                      t2   (both closed)
```

- Features attributed to `t1` **must not** include prices after `t1`.
- Impact features are attributed to `t2`, never back-dated onto `t1`.
- Contemporaneous path inside `[t0,t1)` may be stored as **diagnostic only** (`contemporaneous_move_bps`), never as the efficiency numerator for compression gates.

### Variant comparison

| ID | Definition | Causality | Interpretability | Double-count | Lag | Absorption | Initiative |
|---|---|---|---|---|---|---|---|
| A Same-window | impact inside `[t0,t1]` | weak (price path mixes with flow) | poor attribution | high | low | conflates | conflates |
| **B Post-flow** | impact `(t1,t2]` | **strong** | clear response | low | medium | **best** | **best** |
| C Combo | A diagnostics + B gates | ok if gated on B | complex | medium | medium | ok | ok |
| D Event+fixed | burst end → fixed H | strong | simple | low | fixed | good | good |

**V1 recommendation:** **B** for gate numerators; optionally log A as diagnostic. **D** is acceptable implementation of B with fixed `H ∈ {5s,10s,30s}`.

**Forbidden:** single 15m OHLC or 15m net notional as the unit that both detects sell absorption and buy initiative.

## 2. Burst definition (V1)

**Primary grain:** closed **5s** blocks built from closed **1s** trade aggregates.  
**Secondary:** event-merge adjacent 5s blocks if same dominant side and inter-gap ≤ `G` seconds (no numeric freeze in design).  
**Diagnostic grains:** 1s (too noisy alone), 10s/30s (ablation only).

### 1s aggregate fields (closed second `s`)

- `buy_count, sell_count, buy_qty, sell_qty, buy_notional, sell_notional`
- `net_aggressive_notional = buy_notional - sell_notional`
- `first_trade_price, last_trade_price, high_trade_price, low_trade_price`
- `vwap_all`, `vwap_buy`, `vwap_sell` (notional-weighted)
- Tie order inside equal `trade_ts`: `trade_id ASC` (stable, not exchange-seq truth)

### Burst scores (past-only)

Prefer **notional**, not trade count (one market order → many trades; up to 490 same ms).

- `side_notional` (Sell for LONG compression)
- `dominant_side_share = side_notional / (buy+sell)` with floor on total
- `notional_per_second`
- `notional_score = MAD or rolling percentile vs past closed windows on same symbol`
- Gates: min absolute USDT floor + min share + min score (values **not** optimized here)

Empty seconds: do not LOCF notional (notional=0); price LOCF only for continuous path diagnostics with `price_stale=true`.

## 3. Directional impact and efficiency

### Attribution rule (two-sided windows)

Never assign full `|ΔP|` to both sides.

For a **side-S impact window** after a side-S flow window:

```text
raw_bps = (P_end - P_start) / P_start * 1e4
# P_start = last trade price at t1 (bucket close)
# P_end   = last trade price at t2 (bucket close)

sell_directional_impact_bps = max(0, -raw_bps)   # adverse for sellers = down
buy_directional_impact_bps  = max(0,  raw_bps)   # favorable for buyers = up
```

Optional diagnostics (not gate numerators): MFE/MAE inside `(t1,t2]` relative to `P_start`.

If both sides traded heavily in the **flow** window, efficiency still uses **only** the designated side’s notional in the denominator; dominance share gate must have passed.

### Efficiency / compression (robust V1)

Avoid raw `bps / tiny_usdt`.

```text
notional_norm = side_notional / max(eps_floor, rolling_median_side_notional_past)
efficiency_raw = directional_impact_bps / notional_norm
efficiency_score = winsorized MAD z of efficiency_raw (past-only)   # or percentile rank
compression_score = -efficiency_score   # high compression = low adverse impact per normalized notional
```

Alternate gate form (preferred for stability): **two categorical gates**

1. `notional_score ≥ high`
2. `directional_impact_bps ≤ low` (or efficiency_score ≤ low)

Do not use unstable `eff_buy / eff_sell` as primary flip score.

### Flip score (V1)

**Two-stage categorical + ordinal points** (example structure, not fitted cuts):

- Compression gates passed (points)
- Flip notional score high (points)
- Flip directional impact high (points)
- Flip efficiency score ≫ compression efficiency score via rank-diff or signed score-diff (points)
- Delay in band (points)

`efficiency_flip_score = sum(points)`; no division of two near-zero efficiencies.

## 4. Trade-price limitations

| Issue | V1 handling |
|---|---|
| Bid/ask bounce | Accept as trade-reality; do not claim mid-impact |
| Empty seconds | Skip LOCF for flow; mark coverage; require min traded seconds in window |
| LOCF price | Only for continuous path / structure; flag `stale` |
| Same-ms ties | Sort `trade_id`; do not claim micro-order |
| Bucket close | Features only after `t+1s` |
| Event vs ingest | Use `trade_ts` only |

**Minimum trustworthy horizons for gates:** prefer **≥5s flow + ≥5s impact** (total ≥10s decision lag). 1s-only gates = exploratory.  
**Must not claim without Mid/BBO:** microprice lead, queue imbalance, OFI, wall fate, executed vs cancelled liquidity.

**Parity audit later:** on days with `orderbook_features_1s_v2`, recompute impact with mid/micro closed buckets and compare rank agreement — separate study, not a v1 blocker.

## 5. Trapped-aggressor VWAP

```text
aggressor_vwap = sum(price*notional)_side / sum(notional)_side   over compression flow [t0,t1)
```

Proxy “trapped” (LONG): after compression, price causally **reclaims above** aggressor_vwap and shows acceptance on that side.

**V1 role:** **separate confirmation / analysis label**, not required for `EFFICIENCY_FLIP` gate. May become optional stage in F5.

## 6. Structure and acceptance (causal)

**No future-pivot:** a swing high at time `p` is only finalized when a later closed bucket confirms (e.g. `K` seconds without new high, or break of prior swing). The **event timestamp is the confirmation time**, not `p`.

**V1 micro-high (LONG):** rolling max of last `L` closed 1s highs ending at `flip_decision_ts` (frozen level).  
**Break:** first later closed bucket with `high >= level + break_eps` (or last trade above).  
**Acceptance:** hold `N` closed seconds (or 1 closed 1m) with reclaim depth ≤ `R` bps below level; wick-only pierce without hold = not acceptance.

Failed break: reclaim fully below level within `T_fail` → invalidate.

## 7. OI classifier (label only)

Align to closed 5s (or 5m blocks for stability). Example for window covering flip→acceptance:

| Class | Rule sketch |
|---|---|
| PRICE_UP_OI_DOWN | ΔP>0 and ΔOI<0 |
| PRICE_UP_OI_UP | ΔP>0 and ΔOI>0 |
| PRICE_DOWN_OI_DOWN | ΔP<0 and ΔOI<0 |
| PRICE_DOWN_OI_UP | ΔP<0 and ΔOI>0 |
| MIXED/FLAT | else / tiny moves |
| MISSING | no valid OI |

Missing OI → `oi_class=MISSING`, episode **kept**.

## 8. OB / pool stratification

- `ob_available`, `pool_context_available` flags on every episode.
- Trade-only pipeline must emit episodes when OB missing.
- Never filter `ob_available=false` in core discovery counts without an explicit stratified report.
