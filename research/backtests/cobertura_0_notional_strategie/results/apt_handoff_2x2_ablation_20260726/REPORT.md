# APT Cobertura 2×2 Handoff Ablation

**Decision: `APT_2X2_ABLATION_START_TIME_DOMINATES`**

## Answers

1. A reproduces real handoff replay: **True** (fails=[])
2. D reproduces Phase-A: **True** (fails=[])
3. B recovered (hist book @ 03:55): **True** (`RECOVERED`)
4. C recovered (Phase-A book @ 00:00): **False** (`DATA_END_OPEN`)
5. Start-time effect:
   - B−A recovered flip: {'a': False, 'b': True, 'flipped': True}
   - D−C recovered flip: {'a': False, 'b': True, 'flipped': True}
   - Δ realized_overlay_pnl B−A: 42.90179739999999
   - Δ realized_overlay_pnl D−C: 57.202275900000096
6. Book effect:
   - C−A recovered flip: {'a': False, 'b': False, 'flipped': False}
   - D−B recovered flip: {'a': True, 'b': True, 'flipped': False}
   - Δ realized_overlay_pnl C−A: 1.2881889999998943
   - Δ realized_overlay_pnl D−B: 15.588667499999993
7. Largest overlay qty: **phase_a_book_at_0000**
8. Most adds/round: **historical_book_at_0000**
9. Dominant recovery driver (decision): **APT_2X2_ABLATION_START_TIME_DOMINATES**
10. Phase-A transferability to real historical blockers: **limited** — recovery tracks start time, not book. Historical handoff book recovers at 03:55; Phase-A book also fails at 00:00. Phase-A success is therefore not transferable as a book property.

Warnings: `03:55 candle open 1.6469 != prescribed start_price 1.6456 (Phase-A fingerprint uses config_start_price=1.6456)`

## Variant table

| variant | start | state | rounds | add fills | realized overlay | exit econ |
|---|---|---|---:|---:|---:|---:|
| `historical_book_at_0000` | `2026-01-19T00:00:00+00:00` | `DATA_END_OPEN` | 16 | 26 | 3.864600 | -14.057663582471257 |
| `historical_book_at_0355` | `2026-01-19T03:55:00+00:00` | `RECOVERED` | 8 | 16 | 46.766397 | 30.137720809588785 |
| `phase_a_book_at_0000` | `2026-01-19T00:00:00+00:00` | `DATA_END_OPEN` | 16 | 26 | 5.152789 | -28.330206943903477 |
| `phase_a_book_at_0355` | `2026-01-19T03:55:00+00:00` | `RECOVERED` | 8 | 16 | 62.355064 | 30.596847805021635 |

## Seed books

- historical: `{'core_long_qty': 296.365, 'core_long_avg': 1.864531340748192, 'core_short_qty': 296.365, 'core_short_avg': 1.8171506068270433}`
- phase_a: `{'core_long_qty': 395.153, 'core_long_avg': 1.768355389945979, 'core_short_qty': 395.153, 'core_short_avg': 1.696714}`

Decision: `APT_2X2_ABLATION_START_TIME_DOMINATES`

