# APT Netto-BE Policy Comparison

Objective: reach true net break-even robustly, early, with low overlay exposure and low peak capital — not maximum profit.

## Answers

1. **Most reliable net BE:** `individual_tp_2p00_target_0p00` (recovered_be=True)

2. **Lowest overlay among recovered:** `individual_tp_scaled_target_0p00` (max_overlay_qty=276.61300000000017)

3. **Lowest peak capital among recovered:** `shared_be_target_0p00` (1468.6411362)

4. **Smallest adverse economics (drawdown proxy):** `shared_be_target_0p00` (-34.19577118432839)

5. **Fastest recovered BE:** `individual_tp_2p00_target_0p00` (3199 bars)

6. **Target sensitivity (0 / 0.25 / 0.50 / 1.00):** see summary table; higher targets delay exit and may raise exposure/capital.

7. **shared_be past first BE?** delay_bars=0; first_be=2026-02-06T00:15:00+00:00; exit=2026-02-06T00:15:00+00:00; final_econ=14.291565877261606

8. **TP vs shared_be for pure BE:** compare recovered rows; prefer lower overlay / capital / duration among recovered_be=true.

9. **Best next research candidate:** `individual_tp_scaled_target_0p00` (lexicographic robust early low-exposure ranking).

10. **Disproportionate capital/exposure cases:** flag any recovered row with max_overlay_to_core_ratio >> peers in the table below.

## Ranking (best first)

| rank | variant | recovered_be | max_ov | peak_cap | adverse | bars | fees | econ |
|---|---|---|---|---|---|---|---|---|
| 1 | individual_tp_scaled_target_0p00 | True | 276.61300000000017 | 1733.2082910000004 | -55.29630336782341 | 3342 | 2.6699667946050005 | 1.6093129440715597 |
| 2 | individual_tp_scaled_target_0p25 | True | 276.61300000000017 | 1733.2082910000004 | -55.29630336782341 | 3342 | 2.6699667946050005 | 1.6093129440715597 |
| 3 | individual_tp_scaled_target_0p50 | True | 276.61300000000017 | 1733.2082910000004 | -55.29630336782341 | 3342 | 2.6699667946050005 | 1.6093129440715597 |
| 4 | individual_tp_scaled_target_1p00 | True | 276.61300000000017 | 1733.2082910000004 | -55.29630336782341 | 3342 | 2.6699667946050005 | 1.6093129440715597 |
| 5 | individual_tp_2p00_target_0p00 | True | 316.122 | 1797.3922860000002 | -58.73528616864335 | 3199 | 2.4281156937400006 | 1.633466944936683 |
| 6 | individual_tp_2p00_target_0p25 | True | 316.122 | 1797.3922860000002 | -58.73528616864335 | 3199 | 2.4281156937400006 | 1.633466944936683 |
| 7 | individual_tp_2p00_target_0p50 | True | 316.122 | 1797.3922860000002 | -58.73528616864335 | 3199 | 2.4281156937400006 | 1.633466944936683 |
| 8 | individual_tp_2p00_target_1p00 | True | 316.122 | 1797.3922860000002 | -58.73528616864335 | 3199 | 2.4281156937400006 | 1.633466944936683 |
| 9 | shared_be_target_0p00 | True | 632.244 | 1468.6411362 | -34.19577118432839 | 5141 | 2.8100492614150006 | 14.291565877261606 |
| 10 | shared_be_target_0p25 | True | 632.244 | 1468.6411362 | -34.19577118432839 | 5141 | 2.8100492614150006 | 14.291565877261606 |
| 11 | shared_be_target_0p50 | True | 632.244 | 1468.6411362 | -34.19577118432839 | 5141 | 2.8100492614150006 | 14.291565877261606 |
| 12 | shared_be_target_1p00 | True | 632.244 | 1468.6411362 | -34.19577118432839 | 5141 | 2.8100492614150006 | 14.291565877261606 |

## Summary table

| policy | target | recovered_be | econ | first_be | exit | bars | max_ov | peak_cap | adverse | fees | adds | status |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| shared_be | 0.0 | True | 14.291565877261606 | 2026-02-06T00:15:00+00:00 | 2026-02-06T00:15:00+00:00 | 5141 | 632.244 | 1468.6411362 | -34.19577118432839 | 2.8100492614150006 | 12 | RECOVERED_BE |
| shared_be | 0.25 | True | 14.291565877261606 | 2026-02-06T00:15:00+00:00 | 2026-02-06T00:15:00+00:00 | 5141 | 632.244 | 1468.6411362 | -34.19577118432839 | 2.8100492614150006 | 12 | RECOVERED_BE |
| shared_be | 0.5 | True | 14.291565877261606 | 2026-02-06T00:15:00+00:00 | 2026-02-06T00:15:00+00:00 | 5141 | 632.244 | 1468.6411362 | -34.19577118432839 | 2.8100492614150006 | 12 | RECOVERED_BE |
| shared_be | 1.0 | True | 14.291565877261606 | 2026-02-06T00:15:00+00:00 | 2026-02-06T00:15:00+00:00 | 5141 | 632.244 | 1468.6411362 | -34.19577118432839 | 2.8100492614150006 | 12 | RECOVERED_BE |
| individual_tp | 0.0 | True | 1.633466944936683 | 2026-01-30T06:25:00+00:00 | 2026-01-30T06:25:00+00:00 | 3199 | 316.122 | 1797.3922860000002 | -58.73528616864335 | 2.4281156937400006 | 7 | RECOVERED_BE |
| individual_tp | 0.25 | True | 1.633466944936683 | 2026-01-30T06:25:00+00:00 | 2026-01-30T06:25:00+00:00 | 3199 | 316.122 | 1797.3922860000002 | -58.73528616864335 | 2.4281156937400006 | 7 | RECOVERED_BE |
| individual_tp | 0.5 | True | 1.633466944936683 | 2026-01-30T06:25:00+00:00 | 2026-01-30T06:25:00+00:00 | 3199 | 316.122 | 1797.3922860000002 | -58.73528616864335 | 2.4281156937400006 | 7 | RECOVERED_BE |
| individual_tp | 1.0 | True | 1.633466944936683 | 2026-01-30T06:25:00+00:00 | 2026-01-30T06:25:00+00:00 | 3199 | 316.122 | 1797.3922860000002 | -58.73528616864335 | 2.4281156937400006 | 7 | RECOVERED_BE |
| individual_tp_scaled | 0.0 | True | 1.6093129440715597 | 2026-01-30T18:20:00+00:00 | 2026-01-30T18:20:00+00:00 | 3342 | 276.61300000000017 | 1733.2082910000004 | -55.29630336782341 | 2.6699667946050005 | 8 | RECOVERED_BE |
| individual_tp_scaled | 0.25 | True | 1.6093129440715597 | 2026-01-30T18:20:00+00:00 | 2026-01-30T18:20:00+00:00 | 3342 | 276.61300000000017 | 1733.2082910000004 | -55.29630336782341 | 2.6699667946050005 | 8 | RECOVERED_BE |
| individual_tp_scaled | 0.5 | True | 1.6093129440715597 | 2026-01-30T18:20:00+00:00 | 2026-01-30T18:20:00+00:00 | 3342 | 276.61300000000017 | 1733.2082910000004 | -55.29630336782341 | 2.6699667946050005 | 8 | RECOVERED_BE |
| individual_tp_scaled | 1.0 | True | 1.6093129440715597 | 2026-01-30T18:20:00+00:00 | 2026-01-30T18:20:00+00:00 | 3342 | 276.61300000000017 | 1733.2082910000004 | -55.29630336782341 | 2.6699667946050005 | 8 | RECOVERED_BE |
