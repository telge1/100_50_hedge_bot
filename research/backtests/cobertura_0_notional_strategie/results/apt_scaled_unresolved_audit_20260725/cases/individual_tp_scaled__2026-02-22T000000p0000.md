# Unresolved Audit — `individual_tp_scaled__2026-02-22T000000p0000`

## 1. Startzustand

- start_timestamp: `2026-02-22T00:00:00+00:00`
- start_price: `0.885`
- long/short qty: `734.761` / `734.761`
- long/short avg: `0.9510175741991925` / `0.9124889948954789`
- locked_loss: `28.309297457775962`
- seeding_mode: `relative_notional_invariant`

## 2–3. Chronologische Adds / TP-Fills

1. `2026-02-22T13:00:00+00:00` **overlay_short_add** qty=293.904 px=0.8319000000000001 fee=0.13447430568000002 overlay_after=293.904 econ=-28.572408578735935
2. `2026-02-22T13:40:00+00:00` **overlay_tp_partial** qty=146.952 px=0.8226 fee=0.06648549336 overlay_after=440.856 econ=-26.524164408585932
3. `2026-02-22T13:40:00+00:00` **overlay_short_add** qty=293.904 px=0.8230000000000001 fee=0.13303564560000003 overlay_after=440.856 econ=-26.524164408585932
4. `2026-02-23T01:00:00+00:00` **overlay_tp_partial** qty=73.476 px=0.8143 fee=0.03290732874 overlay_after=1102.1399999999999 econ=-29.03929012595585
5. `2026-02-23T01:00:00+00:00` **overlay_tp_partial** qty=146.952 px=0.8138000000000001 fee=0.06577424568000001 overlay_after=1102.1399999999999 econ=-29.03929012595585
6. `2026-02-23T01:00:00+00:00` **overlay_short_add** qty=293.904 px=0.8142 fee=0.13161315024 overlay_after=1102.1399999999999 econ=-29.03929012595585
7. `2026-02-23T01:00:00+00:00` **overlay_short_add** qty=293.904 px=0.8054 fee=0.13019065488 overlay_after=1102.1399999999999 econ=-29.03929012595585
8. `2026-02-23T01:00:00+00:00` **overlay_short_add** qty=293.904 px=0.7965 fee=0.12875199480000002 overlay_after=1102.1399999999999 econ=-29.03929012595585
9. `2026-02-23T01:05:00+00:00` **overlay_tp_close** qty=73.476 px=0.806 fee=0.032571910800000006 overlay_after=661.2839999999998 econ=-13.888925024185806
10. `2026-02-23T01:05:00+00:00` **overlay_tp_partial** qty=73.476 px=0.8056000000000001 fee=0.03255574608 overlay_after=661.2839999999998 econ=-13.888925024185806
11. `2026-02-23T01:05:00+00:00` **overlay_tp_partial** qty=146.952 px=0.8051 fee=0.06507108036 overlay_after=661.2839999999998 econ=-13.888925024185806
12. `2026-02-23T01:05:00+00:00` **overlay_tp_partial** qty=146.952 px=0.7964 fee=0.06436791504 overlay_after=661.2839999999998 econ=-13.888925024185806
13. `2026-02-23T01:10:00+00:00` **overlay_tp_close** qty=73.476 px=0.7974 fee=0.03222436932 overlay_after=514.3319999999998 econ=-14.035796200365821
14. `2026-02-23T01:10:00+00:00` **overlay_tp_partial** qty=73.476 px=0.797 fee=0.032208204600000005 overlay_after=514.3319999999998 econ=-14.035796200365821
15. `2026-02-23T01:20:00+00:00` **overlay_tp_close** qty=73.476 px=0.7888000000000001 fee=0.031876827840000004 overlay_after=367.37999999999977 econ=-11.540276068235832
16. `2026-02-23T01:20:00+00:00` **overlay_tp_partial** qty=73.476 px=0.7884 fee=0.03186066312 overlay_after=367.37999999999977 econ=-11.540276068235832

## 4–6. Extremwerte

- max_overlay_qty: `1102.1399999999999`
- max_overlay_notional: `894.93768`
- best_economics: `-11.540276068235832` (2026-02-23T01:20:00+00:00)
- worst/adverse economics: `-132.99855669240574`
- max_drawdown: `121.45828062416992`

## 7. Zustand nach 60 Tagen

- status: `DATA_END_OPEN`
- final_economics: `-68.64077196373582`
- distance_to_be_end: `68.89077196373582`
- overlay_qty: `367.37999999999977`
- net_exposure: `-367.37999999999965`

## 8. Offene Tranchen

- `R1-T4` rem=73.476 entry=0.8054 tp_fills=146.952/73.476/0.0 status=partial
- `R1-T5` rem=293.904 entry=0.7965 tp_fills=0.0/0.0/0.0 status=open

## 9. Warum BE nicht erreicht

- Ursachen: `TP_HARVEST_TOO_SLOW, V_REVERSAL`
- max_drop: `-0.10960451977401127`
- max_rally_from_low: `0.4266497461928935`
- overlay_grows_faster_than_tp: `True`

## 10. Extended horizons

- 90d: recovered=False status=DATA_END_OPEN days=90.00347222222223 econ=-65.58313250610578
- 120d: recovered=True status=RECOVERED_BE days=102.08680555555556 econ=7.960029190414231
- full_remaining: recovered=True status=RECOVERED_BE days=102.08680555555556 econ=7.960029190414231

## 11. Replay-CLI

```bash
python -m research.backtests.cobertura_0_notional_strategie.run_scaled_unresolved_audit \
  --run-id individual_tp_scaled__2026-02-22T000000p0000 \
  --output-dir research/backtests/cobertura_0_notional_strategie/results/apt_scaled_unresolved_audit_20260725
```
