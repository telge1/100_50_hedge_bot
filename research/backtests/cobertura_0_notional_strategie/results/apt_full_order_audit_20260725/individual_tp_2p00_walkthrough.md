# individual_tp_2p00 — Full Order Walkthrough

- run_id: `individual_tp_2p00_target_0p00`
- overlay_exit_policy: `individual_tp`
- full_exit_target_mode: `net_be`
- target: 0.0 USDT
- safety_buffer: 0.25 USDT
- final_status: `RECOVERED_BE` / `recovered_net_be`
- bars: 3199
- fills: 16

## Event order (within candle)

1. Activate pending TP / shared-BE triggers from prior bar
2. Arm recovery round if activation level touched
3. Process overlay exits (shared BE or individual TP)
4. Net-BE full-exit gate (before adds)
5. Short adds shallow → deep at fixed level triggers
6. Legacy post-add full-exit skipped under `net_be`

## Chronological fills

### Event 1 — overlay_short_add
Zeit: 2026-01-20T16:45:00+00:00
Preis: trigger=1.5469000000000002 fill=1.5469000000000002
Aktion: overlay_short_add
Menge: 158.061
Position vorher: long=395.153000 short=395.153000 overlay_short=0.000000
Position nachher: long=395.153000 short=553.214000 overlay_short=158.061000
Average vorher/nachher (overlay short): 0.00000000 → 1.54690000
Gross PnL (realized delta): 0.00000000
Fee: 0.13447751 (open)
Nettoeffekt (realized - fee): -0.13447751
Warum: low<=add_level trigger=1.5469000000000002; shallow→deep; fill at slipped trigger; TP/BE active next bar
Kausal: ok

### Event 2 — overlay_short_add
Zeit: 2026-01-20T22:30:00+00:00
Preis: trigger=1.5304 fill=1.5304
Aktion: overlay_short_add
Menge: 158.061
Position vorher: long=395.153000 short=553.214000 overlay_short=158.061000
Position nachher: long=395.153000 short=711.275000 overlay_short=316.122000
Average vorher/nachher (overlay short): 1.54690000 → 1.53865000
Gross PnL (realized delta): 0.00000000
Fee: 0.13304310 (open)
Nettoeffekt (realized - fee): -0.13304310
Warum: low<=add_level trigger=1.5304; shallow→deep; fill at slipped trigger; TP/BE active next bar
Kausal: ok

### Event 3 — overlay_tp_close
Zeit: 2026-01-21T16:55:00+00:00
Preis: trigger=1.5142 fill=1.5142
Aktion: overlay_tp_close
Menge: 158.061
Position vorher: long=395.153000 short=711.275000 overlay_short=316.122000
Position nachher: long=395.153000 short=553.214000 overlay_short=158.061000
Average vorher/nachher (overlay short): 1.53865000 → 1.53865000
Gross PnL (realized delta): 3.86459145
Fee: 0.13163478 (close)
Nettoeffekt (realized - fee): 3.73295667
Warum: low<=active_tp trigger=1.5142 tranche=R1-T1; TP active from next bar after entry
Kausal: ok

### Event 4 — overlay_short_add
Zeit: 2026-01-21T16:55:00+00:00
Preis: trigger=1.514 fill=1.514
Aktion: overlay_short_add
Menge: 158.061
Position vorher: long=395.153000 short=553.214000 overlay_short=158.061000
Position nachher: long=395.153000 short=711.275000 overlay_short=316.122000
Average vorher/nachher (overlay short): 1.53865000 → 1.52632500
Gross PnL (realized delta): 0.00000000
Fee: 0.13161739 (open)
Nettoeffekt (realized - fee): -0.13161739
Warum: low<=add_level trigger=1.514; shallow→deep; fill at slipped trigger; TP/BE active next bar
Kausal: ok

### Event 5 — overlay_tp_close
Zeit: 2026-01-25T16:15:00+00:00
Preis: trigger=1.4981 fill=1.4981
Aktion: overlay_tp_close
Menge: 158.061
Position vorher: long=395.153000 short=711.275000 overlay_short=316.122000
Position nachher: long=395.153000 short=553.214000 overlay_short=158.061000
Average vorher/nachher (overlay short): 1.52632500 → 1.52632500
Gross PnL (realized delta): 4.46127173
Fee: 0.13023515 (close)
Nettoeffekt (realized - fee): 4.33103657
Warum: low<=active_tp trigger=1.4981 tranche=R1-T2; TP active from next bar after entry
Kausal: ok

### Event 6 — overlay_short_add
Zeit: 2026-01-25T16:15:00+00:00
Preis: trigger=1.4975 fill=1.4975
Aktion: overlay_short_add
Menge: 158.061
Position vorher: long=395.153000 short=553.214000 overlay_short=158.061000
Position nachher: long=395.153000 short=711.275000 overlay_short=316.122000
Average vorher/nachher (overlay short): 1.52632500 → 1.51191250
Gross PnL (realized delta): 0.00000000
Fee: 0.13018299 (open)
Nettoeffekt (realized - fee): -0.13018299
Warum: low<=add_level trigger=1.4975; shallow→deep; fill at slipped trigger; TP/BE active next bar
Kausal: ok

### Event 7 — overlay_tp_close
Zeit: 2026-01-25T18:10:00+00:00
Preis: trigger=1.482 fill=1.482
Aktion: overlay_tp_close
Menge: 158.061
Position vorher: long=395.153000 short=711.275000 overlay_short=316.122000
Position nachher: long=395.153000 short=553.214000 overlay_short=158.061000
Average vorher/nachher (overlay short): 1.51191250 → 1.51191250
Gross PnL (realized delta): 4.72799966
Fee: 0.12883552 (close)
Nettoeffekt (realized - fee): 4.59916414
Warum: low<=active_tp trigger=1.482 tranche=R1-T3; TP active from next bar after entry
Kausal: ok

### Event 8 — overlay_short_add
Zeit: 2026-01-25T18:10:00+00:00
Preis: trigger=1.481 fill=1.481
Aktion: overlay_short_add
Menge: 158.061
Position vorher: long=395.153000 short=553.214000 overlay_short=158.061000
Position nachher: long=395.153000 short=711.275000 overlay_short=316.122000
Average vorher/nachher (overlay short): 1.51191250 → 1.49645625
Gross PnL (realized delta): 0.00000000
Fee: 0.12874859 (open)
Nettoeffekt (realized - fee): -0.12874859
Warum: low<=add_level trigger=1.481; shallow→deep; fill at slipped trigger; TP/BE active next bar
Kausal: ok

### Event 9 — overlay_tp_close
Zeit: 2026-01-25T19:25:00+00:00
Preis: trigger=1.4659 fill=1.4659
Aktion: overlay_tp_close
Menge: 158.061
Position vorher: long=395.153000 short=711.275000 overlay_short=316.122000
Position nachher: long=395.153000 short=553.214000 overlay_short=158.061000
Average vorher/nachher (overlay short): 1.49645625 → 1.49645625
Gross PnL (realized delta): 4.82975143
Fee: 0.12743589 (close)
Nettoeffekt (realized - fee): 4.70231554
Warum: low<=active_tp trigger=1.4659 tranche=R1-T4; TP active from next bar after entry
Kausal: ok

### Event 10 — overlay_short_add
Zeit: 2026-01-25T19:25:00+00:00
Preis: trigger=1.4646000000000001 fill=1.4646000000000001
Aktion: overlay_short_add
Menge: 158.061
Position vorher: long=395.153000 short=553.214000 overlay_short=158.061000
Position nachher: long=395.153000 short=711.275000 overlay_short=316.122000
Average vorher/nachher (overlay short): 1.49645625 → 1.48052812
Gross PnL (realized delta): 0.00000000
Fee: 0.12732288 (open)
Nettoeffekt (realized - fee): -0.12732288
Warum: low<=add_level trigger=1.4646000000000001; shallow→deep; fill at slipped trigger; TP/BE active next bar
Kausal: ok

### Event 11 — overlay_tp_close
Zeit: 2026-01-25T19:50:00+00:00
Preis: trigger=1.4497 fill=1.4497
Aktion: overlay_tp_close
Menge: 158.061
Position vorher: long=395.153000 short=711.275000 overlay_short=316.122000
Position nachher: long=395.153000 short=553.214000 overlay_short=158.061000
Average vorher/nachher (overlay short): 1.48052812 → 1.48052812
Gross PnL (realized delta): 4.87272427
Fee: 0.12602757 (close)
Nettoeffekt (realized - fee): 4.74669670
Warum: low<=active_tp trigger=1.4497 tranche=R1-T5; TP active from next bar after entry
Kausal: ok

### Event 12 — overlay_short_add
Zeit: 2026-01-25T19:50:00+00:00
Preis: trigger=1.4481000000000002 fill=1.4481000000000002
Aktion: overlay_short_add
Menge: 158.061
Position vorher: long=395.153000 short=553.214000 overlay_short=158.061000
Position nachher: long=395.153000 short=711.275000 overlay_short=316.122000
Average vorher/nachher (overlay short): 1.48052812 → 1.46431406
Gross PnL (realized delta): 0.00000000
Fee: 0.12588847 (open)
Nettoeffekt (realized - fee): -0.12588847
Warum: low<=add_level trigger=1.4481000000000002; shallow→deep; fill at slipped trigger; TP/BE active next bar
Kausal: ok

### Event 13 — overlay_tp_close
Zeit: 2026-01-30T06:25:00+00:00
Preis: trigger=1.4337 fill=1.4337
Aktion: overlay_tp_close
Menge: 158.061
Position vorher: long=395.153000 short=711.275000 overlay_short=316.122000
Position nachher: long=395.153000 short=553.214000 overlay_short=158.061000
Average vorher/nachher (overlay short): 1.46431406 → 1.46431406
Gross PnL (realized delta): 4.83888933
Fee: 0.12463663 (close)
Nettoeffekt (realized - fee): 4.71425270
Warum: low<=active_tp trigger=1.4337 tranche=R1-T6; TP active from next bar after entry
Kausal: ok

### Event 14 — full_exit
Zeit: 2026-01-30T06:25:00+00:00
Preis: trigger=1.4341 fill=1.4341
Aktion: full_exit_overlay_short
Menge: 158.061
Position vorher: long=395.153000 short=553.214000 overlay_short=158.061000
Position nachher: long=395.153000 short=395.153000 overlay_short=0.000000
Average vorher/nachher (overlay short): 1.46431406 → 0.00000000
Gross PnL (realized delta): 4.77566493
Fee: 0.12467140 (close)
Nettoeffekt (realized - fee): 4.65099353
Warum: net_be gate: total_exit_economics >= target+safety_buffer-tol; adverse close fills; flatten all remaining
Kausal: ok

### Event 15 — full_exit
Zeit: 2026-01-30T06:25:00+00:00
Preis: trigger=1.4341 fill=1.4341
Aktion: full_exit_core_long
Menge: 395.153
Position vorher: long=395.153000 short=395.153000 overlay_short=0.000000
Position nachher: long=0.000000 short=395.153000 overlay_short=0.000000
Average vorher/nachher (overlay short): 0.00000000 → 0.00000000
Gross PnL (realized delta): -132.08202010
Fee: 0.31167890 (close)
Nettoeffekt (realized - fee): -132.39369901
Warum: net_be gate: total_exit_economics >= target+safety_buffer-tol; adverse close fills; flatten all remaining
Kausal: ok

### Event 16 — full_exit
Zeit: 2026-01-30T06:25:00+00:00
Preis: trigger=1.4341 fill=1.4341
Aktion: full_exit_core_short
Menge: 395.153
Position vorher: long=0.000000 short=395.153000 overlay_short=0.000000
Position nachher: long=0.000000 short=0.000000 overlay_short=0.000000
Average vorher/nachher (overlay short): 0.00000000 → 0.00000000
Gross PnL (realized delta): 103.77270994
Fee: 0.31167890 (close)
Nettoeffekt (realized - fee): 103.46103104
Warum: net_be gate: total_exit_economics >= target+safety_buffer-tol; adverse close fills; flatten all remaining
Kausal: ok


## Full-exit summary

- **policy**: `individual_tp_2p00`
- **first_net_be_timestamp**: `2026-01-30T06:25:00+00:00`
- **first_net_be_economics**: `1.633466944936683`
- **exit_timestamp**: `2026-01-30T06:25:00+00:00`
- **exit_reason**: `recovered_net_be`
- **final_status**: `RECOVERED_BE`
- **target_usdt**: `0.0`
- **safety_buffer_usdt**: `0.25`
- **tolerance_usdt**: `0.01`
- **threshold_usdt**: `0.24`
- **economics_pre_exit_engine**: `1.633466944936683`
- **estimated_remaining_close_fees_pre**: `0.748029213085`
- **estimated_exit_slippage_pre**: `0.0`
- **actual_final_economics_shadow**: `1.633466944936675`
- **estimate_vs_actual_diff**: `-7.993605777301127e-15`
- **be_to_exit_delay_bars**: `0`
- **exit_not_before_first_be**: `True`
- **exit_immediate_on_first_be**: `True`
- **flat_after_exit**: `True`
- **open_tranches_remaining**: `0`
- **n_full_exit_fills**: `3`
- **exit_fill_prices**: `[1.4341, 1.4341, 1.4341]`
- **exit_fill_qtys**: `[158.061, 395.153, 395.153]`
- **pass_fail**: `PASS`
