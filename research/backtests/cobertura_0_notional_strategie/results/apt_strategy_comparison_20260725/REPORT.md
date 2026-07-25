# APT Strategy Comparison (shared_be / TP / dynamic long equalization)

## Answers

1. **Best final economics:** `shared_be` (30.596847805021635)

2. **Lowest overlay exposure:** `dynamic_long_equalization_3pct` (max_overlay_short_qty=158.061)

3. **Most additional capital (peak gross notional):** `dynamic_long_equalization_5pct` (2824.4726640000003)

4. **Equalization triggered at spread caps:** `dynamic_long_equalization_3pct` (cap=0.03), `dynamic_long_equalization_4pct` (cap=0.04), `dynamic_long_equalization_5pct` (cap=0.05)

5. **New long/short spread after equalization:** `dynamic_long_equalization_3pct` → 0.029851951050550083; `dynamic_long_equalization_4pct` → 0.039759544818821324; `dynamic_long_equalization_5pct` → 0.04967725713713442

6. **Equalization vs closing short-adds:** best eq `dynamic_long_equalization_3pct` econ=-27.944924563048342 vs shared_be=30.596847805021635 vs tp2%=1.633466944936683.

7. **Post-equalization path stress:** see `post_equalization_stress.csv`.
   - `dynamic_long_equalization_3pct` drop2=-28.50435115626838 rally2=-28.54037645194838 drop10=-28.434491292348362 rally10=None
   - `dynamic_long_equalization_4pct` drop2=-47.756802156898374 rally2=-47.79545284039839 drop10=-47.656889357648396 rally10=None
   - `dynamic_long_equalization_5pct` drop2=-71.6804259272883 rally2=None drop10=-71.5701680424083 rally10=None

8. **Next research candidate:** keep `shared_be` as economics leader; treat dynamic_long_equalization as a capital/structure alternative when fills occur; `individual_tp_2p00` / scaled remain exposure-focused backups.

## Summary table

| variant | status | econ | short_adds | eq_fills | max_ov | capital | locked_final |
|---|---|---|---|---|---|---|---|
| shared_be | RECOVERED | 30.596847805021635 | 16 | 0 | 632.244 | 1468.6411362000001 | 28.309310161323403 |
| individual_tp_2p00 | RECOVERED | 1.633466944936683 | 7 | 0 | 316.122 | 1797.3922860000002 | 28.309310161323403 |
| individual_tp_scaled | RECOVERED | 1.6093129440715597 | 8 | 0 | 276.61300000000017 | 1733.2082910000001 | 28.309310161323403 |
| dynamic_long_equalization_3pct | EQUALIZED_LOCKED | -27.944924563048342 | 1 | 1 | 158.061 | 1797.3922860000002 | 27.313525861323384 |
| dynamic_long_equalization_4pct | EQUALIZED_LOCKED | -47.00780824364834 | 2 | 1 | 316.122 | 2310.9324750000005 | 45.99633606132321 |
| dynamic_long_equalization_5pct | EQUALIZED_LOCKED | -70.74901933688821 | 3 | 1 | 474.183 | 2824.4726640000003 | 69.35775186132332 |
| dynamic_long_equalization_6pct | RECOVERED | 0.06592777650691661 | 5 | 0 | 790.3050000000001 | 2363.8037505 | 28.309310161323403 |
