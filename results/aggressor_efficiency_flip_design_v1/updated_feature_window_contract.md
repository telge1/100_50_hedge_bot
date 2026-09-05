# Updated feature / impact window contract — AEF Discovery V1

**Contract version:** `aef_causal_contract/v1.1-dual-impact`  
**Status:** Supersedes the post-flow-only recommendation in `feature_window_contract.md` for compression gates.  
**Does not delete** the prior file (audit trail).

## 1. Three windows (mandatory)

```text
Flow + contemporaneous impact:   [t0, t1)     usable only when as_of >= t1
Post-flow follow-through:        [t1, t2)     usable only when as_of >= t2
Counter-side search:             [t2, t3)     each counter burst closed on its own clock
```

All intervals half-open. No feature may use prices strictly after its window end.

## 2. Why contemporaneous is required

Compression = high aggressor notional **and low directional move during the attack**.  
Post-flow-only cannot see Case C (efficient burst, flat aftermath) and mislabels it as compression.

**Compression gate numerator (V1):** contemporaneous directional impact (primary).  
**Post-flow:** confirmation / veto (delayed initiative vs absorption / reclaim).

## 3. Price fields

Deterministic trade order: `(trade_ts ASC, trade_id ASC)`.

| Field | Window | Rule |
|---|---|---|
| flow_start_price | `[t0,t1)` | first trade in order |
| flow_end_price | `[t0,t1)` | last trade in order |
| contemporaneous_raw_bps | | `(flow_end-flow_start)/flow_start*1e4` |
| max_up_bps / max_down_bps | same | vs flow_start using hi/lo in window |
| post_flow_start_price | | `= flow_end_price` |
| post_flow_end_price | `[t1,t2)` | last trade; if none → LOCF start + `post_empty=true` |
| sell_directional_impact_bps | | `max(0,-raw_bps)` on the window being measured |
| buy_directional_impact_bps | | `max(0,+raw_bps)` |

Notional never LOCF. Empty flow window → no burst.

## 4. Burst defaults

- Primary flow: **5s**
- Post-flow: **5s** (sensitivity 15s/30s predeclared)
- Counter search: **3m** (sensitivity 1m/5m)
- Adjacent 5s merge: same dominant side + small gap (unfitted)

Trade **count** is diagnostic only; gates use **notional** (+ share + robust rank).

## 5. Compression (LONG / SHORT mirrored)

Confirmed only if:

1. Dominant aggressor side + notional rank/gates  
2. Contemporaneous adverse impact **low** (veto if high → Case C)  
3. Post-flow adverse follow-through **not strong** (veto Case D)  
4. Optional reclaim flag for analysis  

## 6. Counter initiative

Separate closed burst in `[t2,t3)` with own contemporaneous impact.  
V1 flip requires `*_BURST_WITH_IMPACT` (contemporaneous). Delayed-impact = sensitivity only.

Then structure break + acceptance (unchanged causal pivot rule) → `final_decision_ts` → entry next closed 1s.

## 7. Ordinal score skeleton

Compression: notional_rank + inv(contemp_impact_rank) + inv(post_follow_rank) + optional reclaim.  
Hard veto: high contemp adverse impact.  
Initiative: counter notional + contemp impact + post + no full reclaim.  
Flip: both confirmed + order + no invalidation + structure + acceptance.  
No `eff_a/eff_b` primary ratio.

## 8. OB / mid

Still optional. Core contract is trade-price dual-window.  
`ob_available` stratification only; never drop trade-only episodes silently.
