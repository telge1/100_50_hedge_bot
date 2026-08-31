# Clear Pool Selection Rule V1

**Status:** FROZEN (selection logic only — no entry, no PnL, no strategy)  
**ID:** `CLEAR_POOL_SELECTION_RULE_V1`  
**Frozen at:** `2026-08-31T18:00:00Z`  
**Evidence base:** `results/canonical_pool_wall_trade_reaction_v1/` (corrected touch + raw zone check)

---

## One-line rule

**Only act on large HTF pool zones that are full of resting book liquidity on approach; then decide from what that zone liquidity does — everything else is secondary.**

German (working form):

> Große HTF-Zonen, die beim Anlauf wirklich voll mit Book-Liquidität sind — der Rest ist nachrangig. Entscheidung = Verhalten dieser Zonen-Liquidität.

---

## Two stages (strict order)

### Stage A — FILTER (candidate gate)

A pool is a **candidate** only if **all** of the following hold:

| # | Requirement | V1 definition |
|---|---|---|
| A1 | Symbol | `BTCUSDT` (V1 scope) |
| A2 | Source | Canonical LLD via `chart_pool_adapter.export_snapshot` / frozen structural episodes — **not** arrivals CSV / freeze CaseSpec |
| A3 | Timeframe | `15m` **or** `30m` |
| A4 | Size | Same-TF component membership **P ≥ 5** (cluster, not singleton/pair) |
| A5 | Zone geometry | Use component/pool `[lower, upper]`; BID front=`upper`, ASK front=`lower` |
| A6 | Approach / touch | Mid must be **inside the zone band** (front/back ± small tolerance). Far “almost touches” do **not** count |
| A7 | Book fill on approach | At (or immediately before) first valid approach: resting liquidity **inside** `[lower, upper]` on the pool side — **distributed zone depth**, not merely “one dominant 1s wall somewhere during the episode” |

**A7 operational meaning (V1):**

- Prefer full-depth OB200 (raw archive) when available: zone has **≥ 2** resting levels and material zone notional/qty.
- 1s `orderbook_features_1s_v2` dominant-wall-in-zone is only a **weak proxy** (often disagrees at exact touch). Do not treat it as SoT for “zone full”.

If any of A1–A7 fail → **IGNORE** (not a candidate).

### Stage B — DECIDE (only on Stage-A candidates)

Observe **only the liquidity inside that zone** around approach:

| Observation | Meaning (V1 language) | Lean |
|---|---|---|
| Zone liquidity holds / refreshes while mid tests front | Defense | Rejection more plausible |
| Zone liquidity shrinks with aggressive trades into the zone | Eaten | Pass-through more plausible |
| Zone liquidity vanishes with little trade notional | Pulled | Uncertain / often skip |
| Insufficient book/trade data | No decision | No trade / no label |

Stage B does **not** invent entries, TP/SL, or PnL. It only classifies zone-liquidity behavior.

---

## Explicit non-goals / secondary

Do **not** prioritize for selection:

- Small `5m` components as primary candidates
- Singleton / pair pools (P ≤ 2) as primary candidates
- Empty zones (no resting book in `[lower, upper]` on approach)
- Chart labels / P-Σ cosmetics alone
- “Dominant wall” from 1s features without zone-depth confirmation
- Manual review of every visible component number
- Outcomes, forward returns, MFE/MAE, PnL (out of scope for this rule freeze)

---

## Evidence that justified freezing this rule

From corrected Pool × Wall × Trade V1 + raw zone check:

- Wall/zone presence still separates rejects after touch fix (wall YES ~56% vs NO ~34%; within TF+membership ~+23pp).
- Strong filter (15m/30m + P≥5 + wall proxy) ~60% reject vs weak ~7%.
- Raw depth: when zone actually filled at touch ~72% reject vs ~20% when empty; median ~77 levels in zone; zone qty ~17× single dominant wall.
- Conclusion: **filled HTF zone** > single-wall proxy > raw chart pool visibility.

---

## Data sources (allowed)

| Role | Source |
|---|---|
| Pool geometry | Canonical LLD / structural `raw_pool_episodes` |
| Zone depth (preferred) | Raw OB200 archive (`data/orderbook_raw_shadow/ob200_v3`) |
| Zone depth (proxy only) | `orderbook_features_1s_v2` |
| Eat vs pull | `public_trades_canonical` (+ book qty change) |
| Forbidden as pool SoT | `pool_arrivals_v2.csv`, expansion freeze, EXP CaseSpec, `selected_pool.json` |
| Unavailable | `orderbook_deltas` (broken; no alias) |

---

## Next implementation steps (not part of this freeze)

1. Codify Stage A as a deterministic candidate selector.
2. Codify Stage B labels: `ZONE_HELD` / `ZONE_EATEN` / `ZONE_PULLED` / `ZONE_UNKNOWN`.
3. Build a shortlist from Stage A+B for chart spot-check (20–30 cases).
4. Only later: entry contract — **after** this selection rule is stable.

---

## Change control

- Changing A3/A4/A7 thresholds or Stage-B semantics requires a new version (`V2`) and a new evidence folder.
- Do not silently retune on PnL.
