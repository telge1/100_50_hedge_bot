# individual_tp_scaled — Full Order Walkthrough

- run_id: `individual_tp_scaled_target_0p00`
- overlay_exit_policy: `individual_tp_scaled`
- full_exit_target_mode: `net_be`
- target: 0.0 USDT
- safety_buffer: 0.25 USDT
- final_status: `RECOVERED_BE` / `recovered_net_be`
- bars: 3342
- fills: 32

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

### Event 2 — overlay_tp_partial
Zeit: 2026-01-20T22:30:00+00:00
Preis: trigger=1.5297 fill=1.5297
Aktion: overlay_tp_partial
Menge: 79.03
Position vorher: long=395.153000 short=553.214000 overlay_short=158.061000
Position nachher: long=395.153000 short=474.184000 overlay_short=79.031000
Average vorher/nachher (overlay short): 1.54690000 → 1.54690000
Gross PnL (realized delta): 1.35931600
Fee: 0.06649071 (close)
Nettoeffekt (realized - fee): 1.29282529
Warum: low<=active_tp trigger=1.5297 tranche=R1-T1; TP active from next bar after entry
Kausal: ok

### Event 3 — overlay_short_add
Zeit: 2026-01-20T22:30:00+00:00
Preis: trigger=1.5304 fill=1.5304
Aktion: overlay_short_add
Menge: 158.061
Position vorher: long=395.153000 short=474.184000 overlay_short=79.031000
Position nachher: long=395.153000 short=632.245000 overlay_short=237.092000
Average vorher/nachher (overlay short): 1.54690000 → 1.53590002
Gross PnL (realized delta): 0.00000000
Fee: 0.13304310 (open)
Nettoeffekt (realized - fee): -0.13304310
Warum: low<=add_level trigger=1.5304; shallow→deep; fill at slipped trigger; TP/BE active next bar
Kausal: ok

### Event 4 — overlay_tp_partial
Zeit: 2026-01-21T16:55:00+00:00
Preis: trigger=1.5142 fill=1.5142
Aktion: overlay_tp_partial
Menge: 39.515
Position vorher: long=395.153000 short=632.245000 overlay_short=237.092000
Position nachher: long=395.153000 short=592.730000 overlay_short=197.577000
Average vorher/nachher (overlay short): 1.53590002 → 1.53590002
Gross PnL (realized delta): 0.85747642
Fee: 0.03290849 (close)
Nettoeffekt (realized - fee): 0.82456793
Warum: low<=active_tp trigger=1.5142 tranche=R1-T1; TP active from next bar after entry
Kausal: ok

### Event 5 — overlay_tp_partial
Zeit: 2026-01-21T16:55:00+00:00
Preis: trigger=1.5134 fill=1.5134
Aktion: overlay_tp_partial
Menge: 79.03
Position vorher: long=395.153000 short=592.730000 overlay_short=197.577000
Position nachher: long=395.153000 short=513.700000 overlay_short=118.547000
Average vorher/nachher (overlay short): 1.53590002 → 1.53590002
Gross PnL (realized delta): 1.77817683
Fee: 0.06578220 (close)
Nettoeffekt (realized - fee): 1.71239463
Warum: low<=active_tp trigger=1.5134 tranche=R1-T2; TP active from next bar after entry
Kausal: ok

### Event 6 — overlay_short_add
Zeit: 2026-01-21T16:55:00+00:00
Preis: trigger=1.514 fill=1.514
Aktion: overlay_short_add
Menge: 158.061
Position vorher: long=395.153000 short=513.700000 overlay_short=118.547000
Position nachher: long=395.153000 short=671.761000 overlay_short=276.608000
Average vorher/nachher (overlay short): 1.53590002 → 1.52338578
Gross PnL (realized delta): 0.00000000
Fee: 0.13161739 (open)
Nettoeffekt (realized - fee): -0.13161739
Warum: low<=add_level trigger=1.514; shallow→deep; fill at slipped trigger; TP/BE active next bar
Kausal: ok

### Event 7 — overlay_tp_partial
Zeit: 2026-01-25T16:15:00+00:00
Preis: trigger=1.4988000000000001 fill=1.4988000000000001
Aktion: overlay_tp_partial
Menge: 39.515
Position vorher: long=395.153000 short=671.761000 overlay_short=276.608000
Position nachher: long=395.153000 short=632.246000 overlay_short=237.093000
Average vorher/nachher (overlay short): 1.52338578 → 1.52338578
Gross PnL (realized delta): 0.97150713
Fee: 0.03257380 (close)
Nettoeffekt (realized - fee): 0.93893333
Warum: low<=active_tp trigger=1.4988000000000001 tranche=R1-T1; TP active from next bar after entry
Kausal: ok

### Event 8 — overlay_tp_partial
Zeit: 2026-01-25T16:15:00+00:00
Preis: trigger=1.4981 fill=1.4981
Aktion: overlay_tp_partial
Menge: 39.515
Position vorher: long=395.153000 short=632.246000 overlay_short=237.093000
Position nachher: long=395.153000 short=592.731000 overlay_short=197.578000
Average vorher/nachher (overlay short): 1.52338578 → 1.52338578
Gross PnL (realized delta): 0.99916763
Fee: 0.03255858 (close)
Nettoeffekt (realized - fee): 0.96660905
Warum: low<=active_tp trigger=1.4981 tranche=R1-T2; TP active from next bar after entry
Kausal: ok

### Event 9 — overlay_tp_partial
Zeit: 2026-01-25T16:15:00+00:00
Preis: trigger=1.4972 fill=1.4972
Aktion: overlay_tp_partial
Menge: 79.03
Position vorher: long=395.153000 short=592.731000 overlay_short=197.578000
Position nachher: long=395.153000 short=513.701000 overlay_short=118.548000
Average vorher/nachher (overlay short): 1.52338578 → 1.52338578
Gross PnL (realized delta): 2.06946226
Fee: 0.06507804 (close)
Nettoeffekt (realized - fee): 2.00438421
Warum: low<=active_tp trigger=1.4972 tranche=R1-T3; TP active from next bar after entry
Kausal: ok

### Event 10 — overlay_short_add
Zeit: 2026-01-25T16:15:00+00:00
Preis: trigger=1.4975 fill=1.4975
Aktion: overlay_short_add
Menge: 158.061
Position vorher: long=395.153000 short=513.701000 overlay_short=118.548000
Position nachher: long=395.153000 short=671.762000 overlay_short=276.609000
Average vorher/nachher (overlay short): 1.52338578 → 1.50859403
Gross PnL (realized delta): 0.00000000
Fee: 0.13018299 (open)
Nettoeffekt (realized - fee): -0.13018299
Warum: low<=add_level trigger=1.4975; shallow→deep; fill at slipped trigger; TP/BE active next bar
Kausal: ok

### Event 11 — overlay_tp_partial
Zeit: 2026-01-25T18:10:00+00:00
Preis: trigger=1.4828000000000001 fill=1.4828000000000001
Aktion: overlay_tp_partial
Menge: 39.515
Position vorher: long=395.153000 short=671.762000 overlay_short=276.609000
Position nachher: long=395.153000 short=632.247000 overlay_short=237.094000
Average vorher/nachher (overlay short): 1.50859403 → 1.50859403
Gross PnL (realized delta): 1.01925095
Fee: 0.03222606 (close)
Nettoeffekt (realized - fee): 0.98702489
Warum: low<=active_tp trigger=1.4828000000000001 tranche=R1-T2; TP active from next bar after entry
Kausal: ok

### Event 12 — overlay_tp_partial
Zeit: 2026-01-25T18:10:00+00:00
Preis: trigger=1.482 fill=1.482
Aktion: overlay_tp_partial
Menge: 39.515
Position vorher: long=395.153000 short=632.247000 overlay_short=237.094000
Position nachher: long=395.153000 short=592.732000 overlay_short=197.579000
Average vorher/nachher (overlay short): 1.50859403 → 1.50859403
Gross PnL (realized delta): 1.05086295
Fee: 0.03220868 (close)
Nettoeffekt (realized - fee): 1.01865428
Warum: low<=active_tp trigger=1.482 tranche=R1-T3; TP active from next bar after entry
Kausal: ok

### Event 13 — overlay_tp_partial
Zeit: 2026-01-25T18:10:00+00:00
Preis: trigger=1.4808000000000001 fill=1.4808000000000001
Aktion: overlay_tp_partial
Menge: 79.03
Position vorher: long=395.153000 short=592.732000 overlay_short=197.579000
Position nachher: long=395.153000 short=513.702000 overlay_short=118.549000
Average vorher/nachher (overlay short): 1.50859403 → 1.50859403
Gross PnL (realized delta): 2.19656190
Fee: 0.06436519 (close)
Nettoeffekt (realized - fee): 2.13219671
Warum: low<=active_tp trigger=1.4808000000000001 tranche=R1-T4; TP active from next bar after entry
Kausal: ok

### Event 14 — overlay_short_add
Zeit: 2026-01-25T18:10:00+00:00
Preis: trigger=1.481 fill=1.481
Aktion: overlay_short_add
Menge: 158.061
Position vorher: long=395.153000 short=513.702000 overlay_short=118.549000
Position nachher: long=395.153000 short=671.763000 overlay_short=276.610000
Average vorher/nachher (overlay short): 1.50859403 → 1.49282620
Gross PnL (realized delta): 0.00000000
Fee: 0.12874859 (open)
Nettoeffekt (realized - fee): -0.12874859
Warum: low<=add_level trigger=1.481; shallow→deep; fill at slipped trigger; TP/BE active next bar
Kausal: ok

### Event 15 — overlay_tp_partial
Zeit: 2026-01-25T19:20:00+00:00
Preis: trigger=1.4669 fill=1.4669
Aktion: overlay_tp_partial
Menge: 39.515
Position vorher: long=395.153000 short=671.763000 overlay_short=276.610000
Position nachher: long=395.153000 short=632.248000 overlay_short=237.095000
Average vorher/nachher (overlay short): 1.49282620 → 1.49282620
Gross PnL (realized delta): 1.02447366
Fee: 0.03188050 (close)
Nettoeffekt (realized - fee): 0.99259315
Warum: low<=active_tp trigger=1.4669 tranche=R1-T3; TP active from next bar after entry
Kausal: ok

### Event 16 — overlay_tp_partial
Zeit: 2026-01-25T19:25:00+00:00
Preis: trigger=1.4659 fill=1.4659
Aktion: overlay_tp_partial
Menge: 39.515
Position vorher: long=395.153000 short=632.248000 overlay_short=237.095000
Position nachher: long=395.153000 short=592.733000 overlay_short=197.580000
Average vorher/nachher (overlay short): 1.49282620 → 1.49282620
Gross PnL (realized delta): 1.06398866
Fee: 0.03185877 (close)
Nettoeffekt (realized - fee): 1.03212989
Warum: low<=active_tp trigger=1.4659 tranche=R1-T4; TP active from next bar after entry
Kausal: ok

### Event 17 — overlay_tp_partial
Zeit: 2026-01-25T19:25:00+00:00
Preis: trigger=1.4645000000000001 fill=1.4645000000000001
Aktion: overlay_tp_partial
Menge: 79.03
Position vorher: long=395.153000 short=592.733000 overlay_short=197.580000
Position nachher: long=395.153000 short=513.703000 overlay_short=118.550000
Average vorher/nachher (overlay short): 1.49282620 → 1.49282620
Gross PnL (realized delta): 2.23861931
Fee: 0.06365669 (close)
Nettoeffekt (realized - fee): 2.17496263
Warum: low<=active_tp trigger=1.4645000000000001 tranche=R1-T5; TP active from next bar after entry
Kausal: ok

### Event 18 — overlay_short_add
Zeit: 2026-01-25T19:25:00+00:00
Preis: trigger=1.4646000000000001 fill=1.4646000000000001
Aktion: overlay_short_add
Menge: 158.061
Position vorher: long=395.153000 short=513.703000 overlay_short=118.550000
Position nachher: long=395.153000 short=671.764000 overlay_short=276.611000
Average vorher/nachher (overlay short): 1.49282620 → 1.47669719
Gross PnL (realized delta): 0.00000000
Fee: 0.12732288 (open)
Nettoeffekt (realized - fee): -0.12732288
Warum: low<=add_level trigger=1.4646000000000001; shallow→deep; fill at slipped trigger; TP/BE active next bar
Kausal: ok

### Event 19 — overlay_tp_partial
Zeit: 2026-01-25T19:50:00+00:00
Preis: trigger=1.4509 fill=1.4509
Aktion: overlay_tp_partial
Menge: 39.515
Position vorher: long=395.153000 short=671.764000 overlay_short=276.611000
Position nachher: long=395.153000 short=632.249000 overlay_short=237.096000
Average vorher/nachher (overlay short): 1.47669719 → 1.47669719
Gross PnL (realized delta): 1.01937593
Fee: 0.03153277 (close)
Nettoeffekt (realized - fee): 0.98784316
Warum: low<=active_tp trigger=1.4509 tranche=R1-T4; TP active from next bar after entry
Kausal: ok

### Event 20 — overlay_tp_partial
Zeit: 2026-01-25T19:50:00+00:00
Preis: trigger=1.4497 fill=1.4497
Aktion: overlay_tp_partial
Menge: 39.515
Position vorher: long=395.153000 short=632.249000 overlay_short=237.096000
Position nachher: long=395.153000 short=592.734000 overlay_short=197.581000
Average vorher/nachher (overlay short): 1.47669719 → 1.47669719
Gross PnL (realized delta): 1.06679393
Fee: 0.03150669 (close)
Nettoeffekt (realized - fee): 1.03528724
Warum: low<=active_tp trigger=1.4497 tranche=R1-T5; TP active from next bar after entry
Kausal: ok

### Event 21 — overlay_tp_partial
Zeit: 2026-01-25T19:50:00+00:00
Preis: trigger=1.4483000000000001 fill=1.4483000000000001
Aktion: overlay_tp_partial
Menge: 79.03
Position vorher: long=395.153000 short=592.734000 overlay_short=197.581000
Position nachher: long=395.153000 short=513.704000 overlay_short=118.551000
Average vorher/nachher (overlay short): 1.47669719 → 1.47669719
Gross PnL (realized delta): 2.24422986
Fee: 0.06295253 (close)
Nettoeffekt (realized - fee): 2.18127733
Warum: low<=active_tp trigger=1.4483000000000001 tranche=R1-T6; TP active from next bar after entry
Kausal: ok

### Event 22 — overlay_short_add
Zeit: 2026-01-25T19:50:00+00:00
Preis: trigger=1.4481000000000002 fill=1.4481000000000002
Aktion: overlay_short_add
Menge: 158.061
Position vorher: long=395.153000 short=513.704000 overlay_short=118.551000
Position nachher: long=395.153000 short=671.765000 overlay_short=276.612000
Average vorher/nachher (overlay short): 1.47669719 → 1.46035625
Gross PnL (realized delta): 0.00000000
Fee: 0.12588847 (open)
Nettoeffekt (realized - fee): -0.12588847
Warum: low<=add_level trigger=1.4481000000000002; shallow→deep; fill at slipped trigger; TP/BE active next bar
Kausal: ok

### Event 23 — overlay_tp_partial
Zeit: 2026-01-30T01:40:00+00:00
Preis: trigger=1.4349 fill=1.4349
Aktion: overlay_tp_partial
Menge: 39.515
Position vorher: long=395.153000 short=671.765000 overlay_short=276.612000
Position nachher: long=395.153000 short=632.250000 overlay_short=237.097000
Average vorher/nachher (overlay short): 1.46035625 → 1.46035625
Gross PnL (realized delta): 1.00590365
Fee: 0.03118504 (close)
Nettoeffekt (realized - fee): 0.97471861
Warum: low<=active_tp trigger=1.4349 tranche=R1-T5; TP active from next bar after entry
Kausal: ok

### Event 24 — overlay_tp_partial
Zeit: 2026-01-30T06:25:00+00:00
Preis: trigger=1.4337 fill=1.4337
Aktion: overlay_tp_partial
Menge: 39.515
Position vorher: long=395.153000 short=632.250000 overlay_short=237.097000
Position nachher: long=395.153000 short=592.735000 overlay_short=197.582000
Average vorher/nachher (overlay short): 1.46035625 → 1.46035625
Gross PnL (realized delta): 1.05332165
Fee: 0.03115896 (close)
Nettoeffekt (realized - fee): 1.02216269
Warum: low<=active_tp trigger=1.4337 tranche=R1-T6; TP active from next bar after entry
Kausal: ok

### Event 25 — overlay_tp_partial
Zeit: 2026-01-30T06:35:00+00:00
Preis: trigger=1.4320000000000002 fill=1.4320000000000002
Aktion: overlay_tp_partial
Menge: 79.03
Position vorher: long=395.153000 short=592.735000 overlay_short=197.582000
Position nachher: long=395.153000 short=513.705000 overlay_short=118.552000
Average vorher/nachher (overlay short): 1.46035625 → 1.46035625
Gross PnL (realized delta): 2.24099431
Fee: 0.06224403 (close)
Nettoeffekt (realized - fee): 2.17875028
Warum: low<=active_tp trigger=1.4320000000000002 tranche=R1-T7; TP active from next bar after entry
Kausal: ok

### Event 26 — overlay_short_add
Zeit: 2026-01-30T06:35:00+00:00
Preis: trigger=1.4317 fill=1.4317
Aktion: overlay_short_add
Menge: 158.061
Position vorher: long=395.153000 short=513.705000 overlay_short=118.552000
Position nachher: long=395.153000 short=671.766000 overlay_short=276.613000
Average vorher/nachher (overlay short): 1.46035625 → 1.44398162
Gross PnL (realized delta): 0.00000000
Fee: 0.12446276 (open)
Nettoeffekt (realized - fee): -0.12446276
Warum: low<=add_level trigger=1.4317; shallow→deep; fill at slipped trigger; TP/BE active next bar
Kausal: ok

### Event 27 — overlay_tp_partial
Zeit: 2026-01-30T18:20:00+00:00
Preis: trigger=1.419 fill=1.419
Aktion: overlay_tp_partial
Menge: 39.515
Position vorher: long=395.153000 short=671.766000 overlay_short=276.613000
Position nachher: long=395.153000 short=632.251000 overlay_short=237.098000
Average vorher/nachher (overlay short): 1.44398162 → 1.44398162
Gross PnL (realized delta): 0.98714869
Fee: 0.03083948 (close)
Nettoeffekt (realized - fee): 0.95630920
Warum: low<=active_tp trigger=1.419 tranche=R1-T6; TP active from next bar after entry
Kausal: ok

### Event 28 — overlay_tp_partial
Zeit: 2026-01-30T18:20:00+00:00
Preis: trigger=1.4175 fill=1.4175
Aktion: overlay_tp_partial
Menge: 39.515
Position vorher: long=395.153000 short=632.251000 overlay_short=237.098000
Position nachher: long=395.153000 short=592.736000 overlay_short=197.583000
Average vorher/nachher (overlay short): 1.44398162 → 1.44398162
Gross PnL (realized delta): 1.04642119
Fee: 0.03080688 (close)
Nettoeffekt (realized - fee): 1.01561430
Warum: low<=active_tp trigger=1.4175 tranche=R1-T7; TP active from next bar after entry
Kausal: ok

### Event 29 — overlay_tp_partial
Zeit: 2026-01-30T18:20:00+00:00
Preis: trigger=1.4158000000000002 fill=1.4158000000000002
Aktion: overlay_tp_partial
Menge: 79.03
Position vorher: long=395.153000 short=592.736000 overlay_short=197.583000
Position nachher: long=395.153000 short=513.706000 overlay_short=118.553000
Average vorher/nachher (overlay short): 1.44398162 → 1.44398162
Gross PnL (realized delta): 2.22719337
Fee: 0.06153987 (close)
Nettoeffekt (realized - fee): 2.16565350
Warum: low<=active_tp trigger=1.4158000000000002 tranche=R1-T8; TP active from next bar after entry
Kausal: ok

### Event 30 — full_exit
Zeit: 2026-01-30T18:20:00+00:00
Preis: trigger=1.4181 fill=1.4181
Aktion: full_exit_overlay_short
Menge: 118.5530000000002
Position vorher: long=395.153000 short=513.706000 overlay_short=118.553000
Position nachher: long=395.153000 short=395.153000 overlay_short=0.000000
Average vorher/nachher (overlay short): 1.44398162 → 0.00000000
Gross PnL (realized delta): 3.06834361
Fee: 0.09246601 (close)
Nettoeffekt (realized - fee): 2.97587761
Warum: net_be gate: total_exit_economics >= target+safety_buffer-tol; adverse close fills; flatten all remaining
Kausal: ok

### Event 31 — full_exit
Zeit: 2026-01-30T18:20:00+00:00
Preis: trigger=1.4181 fill=1.4181
Aktion: full_exit_core_long
Menge: 395.153
Position vorher: long=395.153000 short=395.153000 overlay_short=0.000000
Position nachher: long=0.000000 short=395.153000 overlay_short=0.000000
Average vorher/nachher (overlay short): 0.00000000 → 0.00000000
Gross PnL (realized delta): -138.40446810
Fee: 0.30820156 (close)
Nettoeffekt (realized - fee): -138.71266966
Warum: net_be gate: total_exit_economics >= target+safety_buffer-tol; adverse close fills; flatten all remaining
Kausal: ok

### Event 32 — full_exit
Zeit: 2026-01-30T18:20:00+00:00
Preis: trigger=1.4181 fill=1.4181
Aktion: full_exit_core_short
Menge: 395.153
Position vorher: long=0.000000 short=395.153000 overlay_short=0.000000
Position nachher: long=0.000000 short=0.000000 overlay_short=0.000000
Average vorher/nachher (overlay short): 0.00000000 → 0.00000000
Gross PnL (realized delta): 110.09515794
Fee: 0.30820156 (close)
Nettoeffekt (realized - fee): 109.78695638
Warum: net_be gate: total_exit_economics >= target+safety_buffer-tol; adverse close fills; flatten all remaining
Kausal: ok


## Full-exit summary

- **policy**: `individual_tp_scaled`
- **first_net_be_timestamp**: `2026-01-30T18:20:00+00:00`
- **first_net_be_economics**: `1.6093129440715597`
- **exit_timestamp**: `2026-01-30T18:20:00+00:00`
- **exit_reason**: `recovered_net_be`
- **final_status**: `RECOVERED_BE`
- **target_usdt**: `0.0`
- **safety_buffer_usdt**: `0.25`
- **tolerance_usdt**: `0.01`
- **threshold_usdt**: `0.24`
- **economics_pre_exit_engine**: `1.6093129440715597`
- **estimated_remaining_close_fees_pre**: `0.7088691213450001`
- **estimated_exit_slippage_pre**: `0.0`
- **actual_final_economics_shadow**: `1.6093129440715634`
- **estimate_vs_actual_diff**: `3.774758283725532e-15`
- **be_to_exit_delay_bars**: `0`
- **exit_not_before_first_be**: `True`
- **exit_immediate_on_first_be**: `True`
- **flat_after_exit**: `True`
- **open_tranches_remaining**: `0`
- **n_full_exit_fills**: `3`
- **exit_fill_prices**: `[1.4181, 1.4181, 1.4181]`
- **exit_fill_qtys**: `[118.5530000000002, 395.153, 395.153]`
- **pass_fail**: `PASS`
