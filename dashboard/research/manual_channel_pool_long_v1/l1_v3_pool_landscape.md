# L1 V3 — Upper-D2-Pool-Landschaft

## Reproduzierbarkeit / Zeichnungen

- Pfad: `/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard/research_charts/user_data/drawings.json`
- SHA256: `3e7041de2814607b4bd4ad4a07df143c155072b09473b96ff151fe1e2e0a1cc4`
- Zeichnungen: 19
- Obere Kanallinie `d-22f8d9f720ce` vorhanden: True

**Allowlist in drawings.json:**

- `d-03350098db64`: ja
- `d-0e5e7004dccd`: ja
- `d-1ff495203465`: **NEIN (keine Touches möglich)**
- `d-248d74050cc8`: ja
- `d-a6d9a05da2f1`: ja

Gesicherter früherer 4-Trade-Lauf (`l1_variant_B_trades_20261108T074319Z.bak`) setzte `d-1ff495203465` voraus — in der aktuellen drawings.json **fehlt** diese Linie.

## Datenquelle Pool-SSOT

Identisch zum L1-Gate: `load_historical_pool_state_at_close`, **DOGEUSDT 5m PD1**, Snapshot am Confirm-Bar-Close. Katalog-Member: `micro` upper bands mit lower/upper_bound, strength.
Kanonische **Exact Unique-Origin D2-Zähler** sind in den ACTIVE-Zeilen hier **nicht** exponiert; Proxy: Anzahl distinkter `pool_id`-Origin-Slots in qualifizierten Bändern.

## Forschungsfilter (vorläufig)

- `MIN_BANDS_PER_SUBSTANTIAL_ZONE` = 3
- `MAX_DISTANCE_TO_FIRST_SUBSTANTIAL_UPPER_PCT` = 2.20 %
- `MIN_SUBSTANTIAL_UPPER_ZONES` = 3

## A — Referenzfälle Pool-Landschaft

### Referenz 1 — Verlust 25.08.2026 21:15 UTC
- Entry: 0.0858500000 | Linie: `d-03350098db64`
- SSOT-Close: 2026-08-25 21:15:00 UTC
- ACTIVE upper: 62 | Gate-qualifiziert: 62
- Zonen / substanziell / dünn: 11 / 10 / 1
- Nächste substanzielle Zone: 2.4345 % | Bänder in Zone: 2
- Origin-Slots (Proxy): 62 | substanziell im Kanal: 6
- Kanalraum: 8.2132 %
- Landschaftsfilter: no — `FIRST_SUBSTANTIAL_TOO_FAR;NEAREST_ZONE_LOW_BAND_DENSITY`

### Referenz 2 — Gewinn 26.08.2026 15:10 UTC
- Entry: 0.0844400000 | Linie: `d-a6d9a05da2f1`
- SSOT-Close: 2026-08-26 15:10:00 UTC
- ACTIVE upper: 75 | Gate-qualifiziert: 74
- Zonen / substanziell / dünn: 14 / 13 / 1
- Nächste substanzielle Zone: 1.8830 % | Bänder in Zone: 5
- Origin-Slots (Proxy): 74 | substanziell im Kanal: 8
- Kanalraum: 8.8285 %
- Landschaftsfilter: yes — `PASS`

**Unterscheidbarkeit (Messwerte):** ja — Verlust abgelehnt, Gewinn bestanden.

## B — Vergleich Variante A vs B+POOL

| | Executed | SL | Pool TP | Channel TP | Open | Σ Brutto % |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| **A** (bisherige B, Room 1,50 %) | 2 | 1 | 0 | 1 | 0 | 5.3449 |
| **B+POOL** | 1 | 0 | 0 | 1 | 0 | 6.5449 |

- Kandidaten durch Pool-Landschaftsfilter (B+POOL): **2**

### C — Gewinn-Trades A

### Variante A

| Entry UTC | Entry | SL | TP@Entry | Exit | Exit UTC | Typ | Line | PnL % |
| --- | --- | --- | --- | --- | --- | --- | --- | ---: |
| 2026-08-26 15:10:00 UTC | 0.0844400000 | 0.0834267200 | 0.0915100000 | 0.0899665421 | 2026-08-28 01:30:00 UTC | CHANNEL_UPPER | `d-a6d9a05da2f1` | 6.5449 |

### Stop-Loss Variante A

| Entry UTC | Entry | SL | TP@Entry | Exit | SL UTC | Line | PnL % |
| --- | --- | --- | --- | --- | --- | --- | ---: |
| 2026-08-25 21:15:00 UTC | 0.0858500000 | 0.0848198000 | 0.0924500000 | 0.0848198000 | 2026-08-26 14:40:00 UTC | `d-03350098db64` | -1.2000 |

### Variante B+POOL

| Entry UTC | Entry | SL | TP@Entry | Exit | Exit UTC | Typ | Line | PnL % |
| --- | --- | --- | --- | --- | --- | --- | --- | ---: |
| 2026-08-26 15:10:00 UTC | 0.0844400000 | 0.0834267200 | 0.0915100000 | 0.0899665421 | 2026-08-28 01:30:00 UTC | CHANNEL_UPPER | `d-a6d9a05da2f1` | 6.5449 |

### Stop-Loss Variante B+POOL

_Keine._

## D — Alle Long-Trigger-Kandidaten (Auszug Metriken)

Vollständige CSV: `l1_v3_pool_candidates.csv`.

| Entry UTC | line | Room % | qual bands | subst zones | near dist % | near bands | Pool pass | A exit | B+ exit |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |
| 2026-08-25 21:15:00 UTC | `d-03350098db64` | 8.2132 | 62 | 10 | 2.4345 | 2 | no | SL | NO_TRADE |
| 2026-08-25 21:20:00 UTC | `d-03350098db64` | 7.9814 | 62 | 10 | 2.2202 | 2 | no | NO_TRADE | NO_TRADE |
| 2026-08-26 16:00:00 UTC | `d-03350098db64` | 8.7344 | 74 | 13 | 1.8468 | 5 | yes | NO_TRADE | NO_TRADE |
| 2026-08-26 16:05:00 UTC | `d-03350098db64` | 8.5362 | 74 | 13 | 1.6663 | 5 | yes | NO_TRADE | NO_TRADE |
| 2026-08-26 17:15:00 UTC | `d-03350098db64` | 8.6771 | 74 | 13 | 1.8709 | 5 | yes | NO_TRADE | NO_TRADE |
| 2026-08-26 15:10:00 UTC | `d-a6d9a05da2f1` | 8.8285 | 74 | 13 | 1.8830 | 5 | yes | CHANNEL_UPPER | CHANNEL_UPPER |
| 2026-08-26 15:25:00 UTC | `d-a6d9a05da2f1` | 8.9151 | 74 | 13 | 1.9796 | 5 | yes | NO_TRADE | NO_TRADE |
| 2026-08-26 17:10:00 UTC | `d-a6d9a05da2f1` | 8.7212 | 74 | 13 | 1.9071 | 5 | yes | NO_TRADE | NO_TRADE |
| 2026-08-26 17:15:00 UTC | `d-a6d9a05da2f1` | 8.6771 | 74 | 13 | 1.8709 | 5 | yes | NO_TRADE | NO_TRADE |
| 2026-08-26 20:10:00 UTC | `d-a6d9a05da2f1` | 7.8192 | 74 | 13 | 1.2475 | 5 | yes | NO_TRADE | NO_TRADE |
| 2026-08-26 20:45:00 UTC | `d-a6d9a05da2f1` | 7.7806 | 74 | 13 | 1.2475 | 5 | yes | NO_TRADE | NO_TRADE |
| 2026-08-27 05:40:00 UTC | `d-a6d9a05da2f1` | 5.2953 | 69 | 11 | 0.6127 | 4 | yes | NO_TRADE | NO_TRADE |
| 2026-08-27 13:55:00 UTC | `d-a6d9a05da2f1` | 3.2909 | 60 | 10 | 0.9233 | 6 | yes | NO_TRADE | NO_TRADE |
| 2026-08-27 14:00:00 UTC | `d-a6d9a05da2f1` | 2.8751 | 60 | 10 | 0.5223 | 6 | yes | NO_TRADE | NO_TRADE |
| 2026-08-27 18:25:00 UTC | `d-a6d9a05da2f1` | 2.5004 | 56 | 8 | 1.8489 | 6 | yes | NO_TRADE | NO_TRADE |
| 2026-08-27 18:50:00 UTC | `d-a6d9a05da2f1` | 2.3230 | 56 | 8 | 1.6989 | 6 | yes | NO_TRADE | NO_TRADE |
| 2026-08-27 18:55:00 UTC | `d-a6d9a05da2f1` | 2.2135 | 56 | 8 | 1.5954 | 6 | yes | NO_TRADE | NO_TRADE |
| 2026-08-27 19:00:00 UTC | `d-a6d9a05da2f1` | 1.8739 | 56 | 8 | 1.2631 | 6 | yes | NO_TRADE | NO_TRADE |
| 2026-08-27 20:10:00 UTC | `d-a6d9a05da2f1` | 2.0994 | 58 | 8 | 1.5609 | 6 | yes | NO_TRADE | NO_TRADE |
| 2026-08-27 20:25:00 UTC | `d-a6d9a05da2f1` | 1.8301 | 56 | 8 | 1.3088 | 6 | yes | NO_TRADE | NO_TRADE |
| 2026-08-27 20:40:00 UTC | `d-a6d9a05da2f1` | 1.8948 | 56 | 8 | 1.3889 | 6 | yes | NO_TRADE | NO_TRADE |
| 2026-08-27 21:00:00 UTC | `d-a6d9a05da2f1` | 1.7472 | 56 | 8 | 1.2631 | 6 | yes | NO_TRADE | NO_TRADE |
| 2026-08-27 21:05:00 UTC | `d-a6d9a05da2f1` | 1.6961 | 56 | 8 | 1.2175 | 6 | yes | NO_TRADE | NO_TRADE |
| 2026-08-27 21:25:00 UTC | `d-a6d9a05da2f1` | 1.8127 | 56 | 8 | 1.3546 | 6 | yes | NO_TRADE | NO_TRADE |
| 2026-08-27 23:20:00 UTC | `d-a6d9a05da2f1` | 1.2000 | 55 | 8 | 0.8650 | 6 | yes | NO_TRADE | NO_TRADE |
| 2026-08-27 23:25:00 UTC | `d-a6d9a05da2f1` | 1.1039 | 55 | 8 | 0.7744 | 6 | yes | NO_TRADE | NO_TRADE |
| 2026-08-28 01:15:00 UTC | `d-a6d9a05da2f1` | 0.7960 | 55 | 8 | 0.5825 | 6 | yes | NO_TRADE | NO_TRADE |
| 2026-08-28 01:55:00 UTC | `d-a6d9a05da2f1` | 0.8331 | 50 | 8 | 1.1996 | 1 | no | NO_TRADE | NO_TRADE |
