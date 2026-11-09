# L1 V3AV — Historischer Backtest (aktualisierte Trendlinien)

**Run:** `20261109T183302Z` · Equality **PASS**
**SSOT:** `/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard/research_charts/user_data/drawings.json`
**SHA256:** `9db7d9418813ad66b4e3b17ec618e7cddc0be4f7ea82381630670757a26c44af`
**V3AS SHA (vorher):** `afff18cf062e72d8`
**Modus:** `RETROSPECTIVE_EXPLORATORY_NOT_CAUSALLY_VALIDATED`

## 1. Zeichnungen vs. V3AS

| | V3AS | V3AV |
|---|---:|---:|
| JSON-Einträge | 9 | 6 |
| Trendlinien (Backtest) | 5 | 6 |

**Hinweis:** V3AV ersetzt die V3AS-Zeichnungsmenge vollständig (keine ID-Überlappung).
Neu/entfernt: `l1_v3av_drawings_diff_vs_v3as.csv`

### Trend-Anker (V3AV)

- `d-6df07d085e08`: 2026-09-02 08:30:00 UTC @ 0.08010484122856866 → 2026-09-05 23:00:00 UTC @ 0.08567220190906669
- `d-755b9ca8482c`: 2026-07-24 10:30:00 UTC @ 0.06828153324283892 → 2026-07-26 04:30:00 UTC @ 0.07007453367817314
- `d-8037f795d49c`: 2026-08-09 21:30:00 UTC @ 0.06920157429581791 → 2026-08-11 18:00:00 UTC @ 0.06992145817064159
- `d-cfa4b3e611ee`: 2026-09-17 03:30:00 UTC @ 0.08041105634167099 → 2026-09-23 03:00:00 UTC @ 0.08760128861711179
- `d-e0ea2ce2fb44`: 2026-07-08 03:00:00 UTC @ 0.07161967409664495 → 2026-07-21 22:00:00 UTC @ 0.0716837588071542
- `d-f94556cc377f`: 2026-06-30 12:00:00 UTC @ 0.06949402807180473 → 2026-07-03 08:00:00 UTC @ 0.07354325034037901

## 2. Varianten

| Variante | Trends | Touch-Epi | Kandidaten | Gates↓ | Slot↓ | Trades | TP/SL/SBE | Hit% | Σ gross% |
|---|---:|---:|---:|---:|---:|---:|---|---:|---:|
| V3AK | 6 | 128 | 68 | 73 | 10 | 9 | 8/1/0 | 88.89 | 5.8314 |
| Watcher_A2 | 6 | 62 | 56 | 56 | 8 | 10 | 8/1/1 | 80.0 | 5.9128 |
| Watcher_A3 | 6 | 49 | 23 | 20 | 1 | 7 | 6/1/0 | 85.71 | 5.0532 |
| Watcher_B | 6 | 55 | 30 | 30 | 2 | 6 | 4/1/1 | 66.67 | 0.9752 |

## 3. Vergleich V3AS

| Variante | Trades V3AS → V3AV | Kandidaten V3AS → V3AV |
|---|---|---|
| V3AK | 3 → 9 | 25 → 68 |
| Watcher_A2 | 2 → 10 | 36 → 56 |
| Watcher_A3 | 3 → 7 | 14 → 23 |
| Watcher_B | 1 → 6 | 11 → 30 |

- Gleiche (Variante, Event, Entry-Zeit) wie V3AS: **0** (erwartbar **0** bei neuen Linien)
- Nur V3AV: **32** Trades

## 4. Artefakte

- `l1_v3av_all_executed_trades.csv` — **Review-Liste** (TP-Pool-ID, SL/TP-%)
- `l1_v3av_variant_summary.csv`
- `l1_v3av_by_month.csv` / `l1_v3av_by_line.csv`
- Snapshot: im Ordner `l1_v3av_drawings_snapshot_<sha>.json`

**STOP** — keine Zeichnungs-/Produktionsänderungen.