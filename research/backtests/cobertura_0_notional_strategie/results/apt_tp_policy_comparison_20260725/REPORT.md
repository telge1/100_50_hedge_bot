# APT Overlay Exit Policy Comparison

Fair A/B on identical APT seed / candles / ladder / fees.

## Answers

1. **Is Individual-TP better than Shared-BE?** No variant beat Shared-BE on final total economics (or Shared-BE missing).

2. **Best final economics:** `shared_be` (30.596847805021635)

3. **Lowest reversal risk** (|max_loss_after_low|): `shared_be` (max_loss_after_low=0.0)

4. **Lowest overlay exposure:** `individual_tp_scaled_1_2_3` (max_overlay_qty=276.61300000000017)

5. **Fee increase vs Shared-BE:**
   - `shared_be`: total_fees=3.4489 (Δ vs BE +0.0000)
   - `individual_tp_0p50`: total_fees=3.7396 (Δ vs BE +0.2907)
   - `individual_tp_0p75`: total_fees=4.1075 (Δ vs BE +0.6586)
   - `individual_tp_1p00`: total_fees=3.6264 (Δ vs BE +0.1775)
   - `individual_tp_1p50`: total_fees=2.7585 (Δ vs BE -0.6904)
   - `individual_tp_2p00`: total_fees=2.4281 (Δ vs BE -1.0208)
   - `individual_tp_scaled_1_2_3`: total_fees=2.6700 (Δ vs BE -0.7789)

6. **Locked spread after overlay flat:** Core averages are never changed by overlay exits; `locked_spread_loss_final` equals initial while core remains. No worse locked spread from TP/BE overlay closes.

7. **Unresolved / safety:** Attention: individual_tp_0p50, individual_tp_0p75

8. **Next research candidate:** `shared_be` remains the primary candidate (best final economics by a wide margin). Secondary exposure-focused candidate: `individual_tp_scaled_1_2_3` (lowest max overlay) or `individual_tp_2p00` (best individual-TP economics among recovered TP variants). Tight TPs (0.5–0.75%) failed to recover on this case.

## Summary table

| variant | status | final_econ | adds | tp_closes | max_ov_qty | open_fees | close_fees | bars | adverse_econ |
|---|---|---|---|---|---|---|---|---|---|
| shared_be | RECOVERED | 30.596847805021635 | 16 | 7 | 632.244 | 1.5448700369850004 | 1.9040364966700005 | 5141 | -34.19577118432839 |
| individual_tp_0p50 | DATA_END_OPEN | -11.317726909333361 | 21 | 21 | 632.244 | 1.8755913412500005 | 1.86404656581 | 45898 | -46.917068181663346 |
| individual_tp_0p75 | DATA_END_OPEN | -0.24197851951347898 | 24 | 24 | 632.244 | 2.0626723408500007 | 2.0448335763900003 | 45898 | -37.216000129808414 |
| individual_tp_1p00 | RECOVERED | 0.6090684566913852 | 18 | 16 | 632.244 | 1.6625519836200005 | 1.9638292983650003 | 39146 | -33.86590578307342 |
| individual_tp_1p50 | RECOVERED | 0.8604792710066161 | 10 | 9 | 632.244 | 1.1407420431000002 | 1.6177905245700002 | 5031 | -94.85806800561336 |
| individual_tp_2p00 | RECOVERED | 1.633466944936683 | 7 | 6 | 316.122 | 0.9112809378750002 | 1.5168347558650002 | 3199 | -58.73528616864335 |
| individual_tp_scaled_1_2_3 | RECOVERED | 1.6093129440715597 | 8 | 0 | 276.61300000000017 | 1.0357437014100002 | 1.6342230931950004 | 3342 | -55.29630336782341 |

## Per-variant detail

- `shared_be` | status=RECOVERED | econ=30.596847805021635 | adds=16 | max_ov=632.244 | fees_open=1.5448700369850004 | fees_close=1.9040364966700005
- `individual_tp_0p50` | status=DATA_END_OPEN | econ=-11.317726909333361 | adds=21 | max_ov=632.244 | fees_open=1.8755913412500005 | fees_close=1.86404656581
- `individual_tp_0p75` | status=DATA_END_OPEN | econ=-0.24197851951347898 | adds=24 | max_ov=632.244 | fees_open=2.0626723408500007 | fees_close=2.0448335763900003
- `individual_tp_1p00` | status=RECOVERED | econ=0.6090684566913852 | adds=18 | max_ov=632.244 | fees_open=1.6625519836200005 | fees_close=1.9638292983650003
- `individual_tp_1p50` | status=RECOVERED | econ=0.8604792710066161 | adds=10 | max_ov=632.244 | fees_open=1.1407420431000002 | fees_close=1.6177905245700002
- `individual_tp_2p00` | status=RECOVERED | econ=1.633466944936683 | adds=7 | max_ov=316.122 | fees_open=0.9112809378750002 | fees_close=1.5168347558650002
- `individual_tp_scaled_1_2_3` | status=RECOVERED | econ=1.6093129440715597 | adds=8 | max_ov=276.61300000000017 | fees_open=1.0357437014100002 | fees_close=1.6342230931950004

## Event order (engine)

1. Activate pending exits from prior bar  
2. Arm recovery round if activation touched  
3. Process exits (shared BE **or** individual TP; highest TP first)  
4. Process adds shallow→deep  
5. New exits inactive until next bar  
6. Full-exit gate via `total_exit_economics`
