# shared_be — Full Order Walkthrough

- run_id: `shared_be_target_0p00`
- overlay_exit_policy: `shared_be`
- full_exit_target_mode: `net_be`
- target: 0.0 USDT
- safety_buffer: 0.25 USDT
- final_status: `RECOVERED_BE` / `recovered_net_be`
- bars: 5141
- fills: 22

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

### Event 2 — overlay_be_close
Zeit: 2026-01-20T16:50:00+00:00
Preis: trigger=1.5451000000000001 fill=1.5451000000000001
Aktion: shared_overlay_be_close
Menge: 158.061
Position vorher: long=395.153000 short=553.214000 overlay_short=158.061000
Position nachher: long=395.153000 short=395.153000 overlay_short=0.000000
Average vorher/nachher (overlay short): 1.54690000 → 0.00000000
Gross PnL (realized delta): 0.28450980
Fee: 0.13432103 (close)
Nettoeffekt (realized - fee): 0.15018877
Warum: high>=active_shared_be trigger=1.5451000000000001; close all overlay short; active_from prior bar
Kausal: ok

### Event 3 — overlay_short_add
Zeit: 2026-01-25T19:50:00+00:00
Preis: trigger=1.4524000000000001 fill=1.4524000000000001
Aktion: overlay_short_add
Menge: 158.061
Position vorher: long=395.153000 short=395.153000 overlay_short=0.000000
Position nachher: long=395.153000 short=553.214000 overlay_short=158.061000
Average vorher/nachher (overlay short): 0.00000000 → 1.45240000
Gross PnL (realized delta): 0.00000000
Fee: 0.12626229 (open)
Nettoeffekt (realized - fee): -0.12626229
Warum: low<=add_level trigger=1.4524000000000001; shallow→deep; fill at slipped trigger; TP/BE active next bar
Kausal: ok

### Event 4 — overlay_be_close
Zeit: 2026-01-25T19:55:00+00:00
Preis: trigger=1.4508 fill=1.4508
Aktion: shared_overlay_be_close
Menge: 158.061
Position vorher: long=395.153000 short=553.214000 overlay_short=158.061000
Position nachher: long=395.153000 short=395.153000 overlay_short=0.000000
Average vorher/nachher (overlay short): 1.45240000 → 0.00000000
Gross PnL (realized delta): 0.25289760
Fee: 0.12612319 (close)
Nettoeffekt (realized - fee): 0.12677441
Warum: high>=active_shared_be trigger=1.4508; close all overlay short; active_from prior bar
Kausal: ok

### Event 5 — overlay_short_add
Zeit: 2026-01-31T11:40:00+00:00
Preis: trigger=1.3638000000000001 fill=1.3638000000000001
Aktion: overlay_short_add
Menge: 158.061
Position vorher: long=395.153000 short=395.153000 overlay_short=0.000000
Position nachher: long=395.153000 short=553.214000 overlay_short=158.061000
Average vorher/nachher (overlay short): 0.00000000 → 1.36380000
Gross PnL (realized delta): 0.00000000
Fee: 0.11855998 (open)
Nettoeffekt (realized - fee): -0.11855998
Warum: low<=add_level trigger=1.3638000000000001; shallow→deep; fill at slipped trigger; TP/BE active next bar
Kausal: ok

### Event 6 — overlay_be_close
Zeit: 2026-01-31T11:45:00+00:00
Preis: trigger=1.3623 fill=1.3623
Aktion: shared_overlay_be_close
Menge: 158.061
Position vorher: long=395.153000 short=553.214000 overlay_short=158.061000
Position nachher: long=395.153000 short=395.153000 overlay_short=0.000000
Average vorher/nachher (overlay short): 1.36380000 → 0.00000000
Gross PnL (realized delta): 0.23709150
Fee: 0.11842958 (close)
Nettoeffekt (realized - fee): 0.11866192
Warum: high>=active_shared_be trigger=1.3623; close all overlay short; active_from prior bar
Kausal: ok

### Event 7 — overlay_short_add
Zeit: 2026-01-31T14:35:00+00:00
Preis: trigger=1.2806 fill=1.2806
Aktion: overlay_short_add
Menge: 158.061
Position vorher: long=395.153000 short=395.153000 overlay_short=0.000000
Position nachher: long=395.153000 short=553.214000 overlay_short=158.061000
Average vorher/nachher (overlay short): 0.00000000 → 1.28060000
Gross PnL (realized delta): 0.00000000
Fee: 0.11132710 (open)
Nettoeffekt (realized - fee): -0.11132710
Warum: low<=add_level trigger=1.2806; shallow→deep; fill at slipped trigger; TP/BE active next bar
Kausal: ok

### Event 8 — overlay_be_close
Zeit: 2026-01-31T14:40:00+00:00
Preis: trigger=1.2791000000000001 fill=1.2791000000000001
Aktion: shared_overlay_be_close
Menge: 158.061
Position vorher: long=395.153000 short=553.214000 overlay_short=158.061000
Position nachher: long=395.153000 short=395.153000 overlay_short=0.000000
Average vorher/nachher (overlay short): 1.28060000 → 0.00000000
Gross PnL (realized delta): 0.23709150
Fee: 0.11119670 (close)
Nettoeffekt (realized - fee): 0.12589480
Warum: high>=active_shared_be trigger=1.2791000000000001; close all overlay short; active_from prior bar
Kausal: ok

### Event 9 — overlay_short_add
Zeit: 2026-01-31T17:10:00+00:00
Preis: trigger=1.2024000000000001 fill=1.2024000000000001
Aktion: overlay_short_add
Menge: 158.061
Position vorher: long=395.153000 short=395.153000 overlay_short=0.000000
Position nachher: long=395.153000 short=553.214000 overlay_short=158.061000
Average vorher/nachher (overlay short): 0.00000000 → 1.20240000
Gross PnL (realized delta): 0.00000000
Fee: 0.10452890 (open)
Nettoeffekt (realized - fee): -0.10452890
Warum: low<=add_level trigger=1.2024000000000001; shallow→deep; fill at slipped trigger; TP/BE active next bar
Kausal: ok

### Event 10 — overlay_be_close
Zeit: 2026-01-31T17:15:00+00:00
Preis: trigger=1.201 fill=1.201
Aktion: shared_overlay_be_close
Menge: 158.061
Position vorher: long=395.153000 short=553.214000 overlay_short=158.061000
Position nachher: long=395.153000 short=395.153000 overlay_short=0.000000
Average vorher/nachher (overlay short): 1.20240000 → 0.00000000
Gross PnL (realized delta): 0.22128540
Fee: 0.10440719 (close)
Nettoeffekt (realized - fee): 0.11687821
Warum: high>=active_shared_be trigger=1.201; close all overlay short; active_from prior bar
Kausal: ok

### Event 11 — overlay_short_add
Zeit: 2026-02-05T15:10:00+00:00
Preis: trigger=1.1289 fill=1.1289
Aktion: overlay_short_add
Menge: 158.061
Position vorher: long=395.153000 short=395.153000 overlay_short=0.000000
Position nachher: long=395.153000 short=553.214000 overlay_short=158.061000
Average vorher/nachher (overlay short): 0.00000000 → 1.12890000
Gross PnL (realized delta): 0.00000000
Fee: 0.09813928 (open)
Nettoeffekt (realized - fee): -0.09813928
Warum: low<=add_level trigger=1.1289; shallow→deep; fill at slipped trigger; TP/BE active next bar
Kausal: ok

### Event 12 — overlay_short_add
Zeit: 2026-02-05T15:10:00+00:00
Preis: trigger=1.1169 fill=1.1169
Aktion: overlay_short_add
Menge: 158.061
Position vorher: long=395.153000 short=553.214000 overlay_short=158.061000
Position nachher: long=395.153000 short=711.275000 overlay_short=316.122000
Average vorher/nachher (overlay short): 1.12890000 → 1.12290000
Gross PnL (realized delta): 0.00000000
Fee: 0.09709608 (open)
Nettoeffekt (realized - fee): -0.09709608
Warum: low<=add_level trigger=1.1169; shallow→deep; fill at slipped trigger; TP/BE active next bar
Kausal: ok

### Event 13 — overlay_be_close
Zeit: 2026-02-05T15:15:00+00:00
Preis: trigger=1.1216000000000002 fill=1.1216000000000002
Aktion: shared_overlay_be_close
Menge: 316.122
Position vorher: long=395.153000 short=711.275000 overlay_short=316.122000
Position nachher: long=395.153000 short=395.153000 overlay_short=0.000000
Average vorher/nachher (overlay short): 1.12290000 → 0.00000000
Gross PnL (realized delta): 0.41095860
Fee: 0.19500934 (close)
Nettoeffekt (realized - fee): 0.21594926
Warum: high>=active_shared_be trigger=1.1216000000000002; close all overlay short; active_from prior bar
Kausal: ok

### Event 14 — overlay_short_add
Zeit: 2026-02-05T20:15:00+00:00
Preis: trigger=1.0543 fill=1.0543
Aktion: overlay_short_add
Menge: 158.061
Position vorher: long=395.153000 short=395.153000 overlay_short=0.000000
Position nachher: long=395.153000 short=553.214000 overlay_short=158.061000
Average vorher/nachher (overlay short): 0.00000000 → 1.05430000
Gross PnL (realized delta): 0.00000000
Fee: 0.09165404 (open)
Nettoeffekt (realized - fee): -0.09165404
Warum: low<=add_level trigger=1.0543; shallow→deep; fill at slipped trigger; TP/BE active next bar
Kausal: ok

### Event 15 — overlay_be_close
Zeit: 2026-02-05T20:20:00+00:00
Preis: trigger=1.0531000000000001 fill=1.0531000000000001
Aktion: shared_overlay_be_close
Menge: 158.061
Position vorher: long=395.153000 short=553.214000 overlay_short=158.061000
Position nachher: long=395.153000 short=395.153000 overlay_short=0.000000
Average vorher/nachher (overlay short): 1.05430000 → 0.00000000
Gross PnL (realized delta): 0.18967320
Fee: 0.09154972 (close)
Nettoeffekt (realized - fee): 0.09812348
Warum: high>=active_shared_be trigger=1.0531000000000001; close all overlay short; active_from prior bar
Kausal: ok

### Event 16 — overlay_short_add
Zeit: 2026-02-06T00:10:00+00:00
Preis: trigger=0.9899 fill=0.9899
Aktion: overlay_short_add
Menge: 158.061
Position vorher: long=395.153000 short=395.153000 overlay_short=0.000000
Position nachher: long=395.153000 short=553.214000 overlay_short=158.061000
Average vorher/nachher (overlay short): 0.00000000 → 0.98990000
Gross PnL (realized delta): 0.00000000
Fee: 0.08605552 (open)
Nettoeffekt (realized - fee): -0.08605552
Warum: low<=add_level trigger=0.9899; shallow→deep; fill at slipped trigger; TP/BE active next bar
Kausal: ok

### Event 17 — overlay_short_add
Zeit: 2026-02-06T00:10:00+00:00
Preis: trigger=0.9794 fill=0.9794
Aktion: overlay_short_add
Menge: 158.061
Position vorher: long=395.153000 short=553.214000 overlay_short=158.061000
Position nachher: long=395.153000 short=711.275000 overlay_short=316.122000
Average vorher/nachher (overlay short): 0.98990000 → 0.98465000
Gross PnL (realized delta): 0.00000000
Fee: 0.08514272 (open)
Nettoeffekt (realized - fee): -0.08514272
Warum: low<=add_level trigger=0.9794; shallow→deep; fill at slipped trigger; TP/BE active next bar
Kausal: ok

### Event 18 — overlay_short_add
Zeit: 2026-02-06T00:10:00+00:00
Preis: trigger=0.9689000000000001 fill=0.9689000000000001
Aktion: overlay_short_add
Menge: 158.061
Position vorher: long=395.153000 short=711.275000 overlay_short=316.122000
Position nachher: long=395.153000 short=869.336000 overlay_short=474.183000
Average vorher/nachher (overlay short): 0.98465000 → 0.97940000
Gross PnL (realized delta): 0.00000000
Fee: 0.08422992 (open)
Nettoeffekt (realized - fee): -0.08422992
Warum: low<=add_level trigger=0.9689000000000001; shallow→deep; fill at slipped trigger; TP/BE active next bar
Kausal: ok

### Event 19 — overlay_short_add
Zeit: 2026-02-06T00:10:00+00:00
Preis: trigger=0.9583 fill=0.9583
Aktion: overlay_short_add
Menge: 158.061
Position vorher: long=395.153000 short=869.336000 overlay_short=474.183000
Position nachher: long=395.153000 short=1027.397000 overlay_short=632.244000
Average vorher/nachher (overlay short): 0.97940000 → 0.97412500
Gross PnL (realized delta): 0.00000000
Fee: 0.08330842 (open)
Nettoeffekt (realized - fee): -0.08330842
Warum: low<=add_level trigger=0.9583; shallow→deep; fill at slipped trigger; TP/BE active next bar
Kausal: ok

### Event 20 — full_exit
Zeit: 2026-02-06T00:15:00+00:00
Preis: trigger=0.9052 fill=0.9052
Aktion: full_exit_overlay_short
Menge: 632.244
Position vorher: long=395.153000 short=1027.397000 overlay_short=632.244000
Position nachher: long=395.153000 short=395.153000 overlay_short=0.000000
Average vorher/nachher (overlay short): 0.97412500 → 0.00000000
Gross PnL (realized delta): 43.57741770
Fee: 0.31476900 (close)
Nettoeffekt (realized - fee): 43.26264870
Warum: net_be gate: total_exit_economics >= target+safety_buffer-tol; adverse close fills; flatten all remaining
Kausal: ok

### Event 21 — full_exit
Zeit: 2026-02-06T00:15:00+00:00
Preis: trigger=0.9052 fill=0.9052
Aktion: full_exit_core_long
Menge: 395.153
Position vorher: long=395.153000 short=395.153000 overlay_short=0.000000
Position nachher: long=0.000000 short=395.153000 overlay_short=0.000000
Average vorher/nachher (overlay short): 0.00000000 → 0.00000000
Gross PnL (realized delta): -341.07844180
Fee: 0.19673087 (close)
Nettoeffekt (realized - fee): -341.27517268
Warum: net_be gate: total_exit_economics >= target+safety_buffer-tol; adverse close fills; flatten all remaining
Kausal: ok

### Event 22 — full_exit
Zeit: 2026-02-06T00:15:00+00:00
Preis: trigger=0.9052 fill=0.9052
Aktion: full_exit_core_short
Menge: 395.153
Position vorher: long=0.000000 short=395.153000 overlay_short=0.000000
Position nachher: long=0.000000 short=0.000000 overlay_short=0.000000
Average vorher/nachher (overlay short): 0.00000000 → 0.00000000
Gross PnL (realized delta): 312.76913164
Fee: 0.19673087 (close)
Nettoeffekt (realized - fee): 312.57240077
Warum: net_be gate: total_exit_economics >= target+safety_buffer-tol; adverse close fills; flatten all remaining
Kausal: ok


## Full-exit summary

- **policy**: `shared_be`
- **first_net_be_timestamp**: `2026-02-06T00:15:00+00:00`
- **first_net_be_economics**: `14.291565877261606`
- **exit_timestamp**: `2026-02-06T00:15:00+00:00`
- **exit_reason**: `recovered_net_be`
- **final_status**: `RECOVERED_BE`
- **target_usdt**: `0.0`
- **safety_buffer_usdt**: `0.25`
- **tolerance_usdt**: `0.01`
- **threshold_usdt**: `0.24`
- **economics_pre_exit_engine**: `14.291565877261606`
- **estimated_remaining_close_fees_pre**: `0.7082307430000001`
- **estimated_exit_slippage_pre**: `0.0`
- **actual_final_economics_shadow**: `14.291565877261585`
- **estimate_vs_actual_diff**: `-2.1316282072803006e-14`
- **be_to_exit_delay_bars**: `0`
- **exit_not_before_first_be**: `True`
- **exit_immediate_on_first_be**: `True`
- **flat_after_exit**: `True`
- **open_tranches_remaining**: `0`
- **n_full_exit_fills**: `3`
- **exit_fill_prices**: `[0.9052, 0.9052, 0.9052]`
- **exit_fill_qtys**: `[632.244, 395.153, 395.153]`
- **pass_fail**: `PASS`
