# L1 — Kanalabstand, Keil-Diagnostik, Variante A vs B

Isolierte Forschung (retrospective drawings). Obere Grenze: `d-22f8d9f720ce`. Mindestabstand B: **1.50 %**.

## Aufgabe 2 — Pilotzeitraum A vs B (max. ein offener Long)

| | Executed | SL | CHANNEL_UPPER | Σ Brutto-PnL % |
| --- | ---: | ---: | ---: | ---: |
| **A** Baseline | 5 | 4 | 1 | 1.7449 |
| **B** Room ≥ 1.50 % | 4 | 3 | 1 | 2.9449 |

**Neu blockiert in B (vs ausgeführt in A):**

- `te-d-a6d9a05da2f1-0033` 2026-08-28 01:55:00 UTC — A: SL -1.2000% → B: `REJECTED_INSUFFICIENT_CHANNEL_ROOM`

### Referenz Fehl-Entry 28.08.2026 01:55 UTC

- Entry/Kandidat: **0.0892000000** | Upper@Entry: 0.0899431410
- Kanalabstand: **0.8331 %** (Spec-Chart ~0,64 % an Upper; hier effektives TP-Ziel liegt auf Upper-Linie → Room = Potenzial)
- TP-Potenzial: 0.8331 % | Gewinn/SL-Ratio: 0.6943
- Keil: Gap Touch→Entry Δ = -0.0394 pp | steigende Support + fallende Upper: yes
- A: SL (-1.2000 %) | B: REJECTED_INSUFFICIENT_CHANNEL_ROOM

## Aufgabe 1 & 3 — Kandidatentabelle (Long-Trigger erlaubt)

Vollständige CSV: `l1_channel_room_candidates.csv`.

| UTC (Entry/Kandidat) | drawing_id | Preis | Room % | TP-Pot. % | Gewinn/SL | Pool | A | B | Ergebnis A |
| --- | --- | ---: | ---: | ---: | ---: | --- | --- | --- | ---: |
| 2026-08-25 21:15:00 UTC | `d-03350098db64` | 0.08585 | 8.2132 | 7.6878 | 6.4065 | yes (62) | SL | SL | -1.2000 |
| 2026-08-25 21:20:00 UTC | `d-03350098db64` | 0.08603 | 7.9814 | 7.4625 | 6.2188 | yes (62) | GLOBAL_MAX_ONE_OPEN_LONG | GLOBAL_MAX_ONE_OPEN_LONG | — |
| 2026-08-26 14:45:00 UTC | `d-03350098db64` | 0.08425 | 9.0995 | 9.0995 | 7.5829 | no | NO_C1_CONFIRM_AFTER_FIRST_TOUCH | NO_C1_CONFIRM_AFTER_FIRST_TOUCH | — |
| 2026-08-26 15:05:00 UTC | `d-a6d9a05da2f1` | 0.08376 | 9.7192 | 9.7192 | 8.0993 | no | NO_C1_CONFIRM_AFTER_FIRST_TOUCH | NO_C1_CONFIRM_AFTER_FIRST_TOUCH | — |
| 2026-08-26 15:10:00 UTC | `d-a6d9a05da2f1` | 0.08444 | 8.8285 | 8.3728 | 6.9773 | yes (74) | CHANNEL_UPPER | CHANNEL_UPPER | 6.5449 |
| 2026-08-26 15:25:00 UTC | `d-03350098db64` | 0.08423 | 9.0870 | 9.0870 | 7.5725 | no | NO_C1_CONFIRM_AFTER_FIRST_TOUCH | NO_C1_CONFIRM_AFTER_FIRST_TOUCH | — |
| 2026-08-26 15:25:00 UTC | `d-a6d9a05da2f1` | 0.08436 | 8.9151 | 8.4756 | 7.0630 | yes (74) | GLOBAL_MAX_ONE_OPEN_LONG | GLOBAL_MAX_ONE_OPEN_LONG | — |
| 2026-08-26 16:00:00 UTC | `d-03350098db64` | 0.08447 | 8.7344 | 8.3343 | 6.9453 | yes (74) | GLOBAL_MAX_ONE_OPEN_LONG | GLOBAL_MAX_ONE_OPEN_LONG | — |
| 2026-08-26 16:00:00 UTC | `d-03350098db64` | 0.08421 | 9.0760 | 9.0760 | 7.5634 | no | NO_C1_CONFIRM_AFTER_FIRST_TOUCH | NO_C1_CONFIRM_AFTER_FIRST_TOUCH | — |
| 2026-08-26 16:05:00 UTC | `d-03350098db64` | 0.08462 | 8.5362 | 8.1423 | 6.7852 | yes (74) | GLOBAL_MAX_ONE_OPEN_LONG | GLOBAL_MAX_ONE_OPEN_LONG | — |
| 2026-08-26 16:10:00 UTC | `d-03350098db64` | 0.08420 | 9.0729 | 9.0729 | 7.5608 | no | NO_C1_CONFIRM_AFTER_FIRST_TOUCH | NO_C1_CONFIRM_AFTER_FIRST_TOUCH | — |
| 2026-08-26 16:15:00 UTC | `d-a6d9a05da2f1` | 0.08394 | 9.4081 | 9.4081 | 7.8401 | no | NO_C1_CONFIRM_AFTER_FIRST_TOUCH | NO_C1_CONFIRM_AFTER_FIRST_TOUCH | — |
| 2026-08-26 17:10:00 UTC | `d-a6d9a05da2f1` | 0.08442 | 8.7212 | 8.3985 | 6.9987 | yes (74) | GLOBAL_MAX_ONE_OPEN_LONG | GLOBAL_MAX_ONE_OPEN_LONG | — |
| 2026-08-26 17:15:00 UTC | `d-03350098db64` | 0.08445 | 8.6771 | 8.3600 | 6.9666 | yes (74) | GLOBAL_MAX_ONE_OPEN_LONG | GLOBAL_MAX_ONE_OPEN_LONG | — |
| 2026-08-26 17:15:00 UTC | `d-a6d9a05da2f1` | 0.08445 | 8.6771 | 8.3600 | 6.9666 | yes (74) | GLOBAL_MAX_ONE_OPEN_LONG | GLOBAL_MAX_ONE_OPEN_LONG | — |
| 2026-08-26 20:10:00 UTC | `d-a6d9a05da2f1` | 0.08497 | 7.8192 | 7.6968 | 6.4140 | yes (74) | GLOBAL_MAX_ONE_OPEN_LONG | GLOBAL_MAX_ONE_OPEN_LONG | — |
| 2026-08-26 20:15:00 UTC | `d-a6d9a05da2f1` | 0.08455 | 8.3512 | 8.3512 | 6.9594 | no | NO_C1_CONFIRM_AFTER_FIRST_TOUCH | NO_C1_CONFIRM_AFTER_FIRST_TOUCH | — |
| 2026-08-26 20:25:00 UTC | `d-a6d9a05da2f1` | 0.08457 | 8.3075 | 8.3075 | 6.9229 | no | NO_C1_CONFIRM_AFTER_FIRST_TOUCH | NO_C1_CONFIRM_AFTER_FIRST_TOUCH | — |
| 2026-08-26 20:45:00 UTC | `d-a6d9a05da2f1` | 0.08497 | 7.7806 | 7.6968 | 6.4140 | yes (74) | GLOBAL_MAX_ONE_OPEN_LONG | GLOBAL_MAX_ONE_OPEN_LONG | — |
| 2026-08-27 05:40:00 UTC | `d-a6d9a05da2f1` | 0.08650 | 5.2953 | 5.0058 | 4.1715 | yes (69) | GLOBAL_MAX_ONE_OPEN_LONG | GLOBAL_MAX_ONE_OPEN_LONG | — |
| 2026-08-27 12:35:00 UTC | `d-a6d9a05da2f1` | 0.08704 | 4.1899 | 4.1899 | 3.4916 | no | NO_C1_CONFIRM_AFTER_FIRST_TOUCH | NO_C1_CONFIRM_AFTER_FIRST_TOUCH | — |
| 2026-08-27 13:55:00 UTC | `d-a6d9a05da2f1` | 0.08773 | 3.2909 | 2.8041 | 2.3367 | yes (60) | GLOBAL_MAX_ONE_OPEN_LONG | GLOBAL_MAX_ONE_OPEN_LONG | — |
| 2026-08-27 14:00:00 UTC | `d-a6d9a05da2f1` | 0.08808 | 2.8751 | 2.3955 | 1.9963 | yes (60) | GLOBAL_MAX_ONE_OPEN_LONG | GLOBAL_MAX_ONE_OPEN_LONG | — |
| 2026-08-27 18:25:00 UTC | `d-a6d9a05da2f1` | 0.08816 | 2.5004 | 2.3026 | 1.9189 | yes (56) | GLOBAL_MAX_ONE_OPEN_LONG | GLOBAL_MAX_ONE_OPEN_LONG | — |
| 2026-08-27 18:35:00 UTC | `d-a6d9a05da2f1` | 0.08796 | 2.7206 | 2.7206 | 2.2671 | no | NO_C1_CONFIRM_AFTER_FIRST_TOUCH | NO_C1_CONFIRM_AFTER_FIRST_TOUCH | — |
| 2026-08-27 18:50:00 UTC | `d-a6d9a05da2f1` | 0.08829 | 2.3230 | 2.1520 | 1.7933 | yes (56) | GLOBAL_MAX_ONE_OPEN_LONG | GLOBAL_MAX_ONE_OPEN_LONG | — |
| 2026-08-27 18:55:00 UTC | `d-a6d9a05da2f1` | 0.08838 | 2.2135 | 2.0480 | 1.7066 | yes (56) | GLOBAL_MAX_ONE_OPEN_LONG | GLOBAL_MAX_ONE_OPEN_LONG | — |
| 2026-08-27 19:00:00 UTC | `d-a6d9a05da2f1` | 0.08867 | 1.8739 | 1.7142 | 1.4285 | yes (56) | GLOBAL_MAX_ONE_OPEN_LONG | GLOBAL_MAX_ONE_OPEN_LONG | — |
| 2026-08-27 20:10:00 UTC | `d-a6d9a05da2f1` | 0.08841 | 2.0994 | 2.0133 | 1.6778 | yes (58) | GLOBAL_MAX_ONE_OPEN_LONG | GLOBAL_MAX_ONE_OPEN_LONG | — |
| 2026-08-27 20:25:00 UTC | `d-a6d9a05da2f1` | 0.08863 | 1.8301 | 1.7601 | 1.4668 | yes (56) | GLOBAL_MAX_ONE_OPEN_LONG | GLOBAL_MAX_ONE_OPEN_LONG | — |
| 2026-08-27 20:35:00 UTC | `d-a6d9a05da2f1` | 0.08827 | 2.2376 | 2.2376 | 1.8647 | no | NO_C1_CONFIRM_AFTER_FIRST_TOUCH | NO_C1_CONFIRM_AFTER_FIRST_TOUCH | — |
| 2026-08-27 20:40:00 UTC | `d-a6d9a05da2f1` | 0.08856 | 1.8948 | 1.8406 | 1.5338 | yes (56) | GLOBAL_MAX_ONE_OPEN_LONG | GLOBAL_MAX_ONE_OPEN_LONG | — |
| 2026-08-27 21:00:00 UTC | `d-a6d9a05da2f1` | 0.08867 | 1.7472 | 1.7142 | 1.4285 | yes (56) | GLOBAL_MAX_ONE_OPEN_LONG | GLOBAL_MAX_ONE_OPEN_LONG | — |
| 2026-08-27 21:05:00 UTC | `d-a6d9a05da2f1` | 0.08871 | 1.6961 | 1.6684 | 1.3903 | yes (56) | GLOBAL_MAX_ONE_OPEN_LONG | GLOBAL_MAX_ONE_OPEN_LONG | — |
| 2026-08-27 21:25:00 UTC | `d-a6d9a05da2f1` | 0.08859 | 1.8127 | 1.8061 | 1.5051 | yes (56) | GLOBAL_MAX_ONE_OPEN_LONG | GLOBAL_MAX_ONE_OPEN_LONG | — |
| 2026-08-27 23:20:00 UTC | `d-a6d9a05da2f1` | 0.08902 | 1.2000 | 1.0559 | 0.8800 | yes (55) | GLOBAL_MAX_ONE_OPEN_LONG | REJECTED_INSUFFICIENT_CHANNEL_ROOM | — |
| 2026-08-27 23:25:00 UTC | `d-a6d9a05da2f1` | 0.08910 | 1.1039 | 0.9652 | 0.8043 | yes (55) | GLOBAL_MAX_ONE_OPEN_LONG | REJECTED_INSUFFICIENT_CHANNEL_ROOM | — |
| 2026-08-28 00:00:00 UTC | `d-a6d9a05da2f1` | 0.08879 | 1.4202 | 1.4202 | 1.1835 | no | NO_C1_CONFIRM_AFTER_FIRST_TOUCH | NO_C1_CONFIRM_AFTER_FIRST_TOUCH | — |
| 2026-08-28 00:35:00 UTC | `d-a6d9a05da2f1` | 0.08888 | 1.2816 | 1.2816 | 1.0680 | no | NO_C1_CONFIRM_AFTER_FIRST_TOUCH | NO_C1_CONFIRM_AFTER_FIRST_TOUCH | — |
| 2026-08-28 01:15:00 UTC | `d-a6d9a05da2f1` | 0.08927 | 0.7960 | 0.7729 | 0.6441 | yes (55) | GLOBAL_MAX_ONE_OPEN_LONG | REJECTED_INSUFFICIENT_CHANNEL_ROOM | — |
| 2026-08-28 01:55:00 UTC | `d-a6d9a05da2f1` | 0.08920 | 0.8331 | 0.8331 | 0.6943 | yes (50) | SL | REJECTED_INSUFFICIENT_CHANNEL_ROOM | -1.2000 |
| 2026-08-28 02:40:00 UTC | `d-1ff495203465` | 0.08810 | 2.0443 | 2.0443 | 1.7036 | yes (50) | SL | SL | -1.2000 |
| 2026-08-28 02:55:00 UTC | `d-1ff495203465` | 0.08776 | 2.4236 | 2.4236 | 2.0197 | yes (50) | GLOBAL_MAX_ONE_OPEN_LONG | GLOBAL_MAX_ONE_OPEN_LONG | — |
| 2026-08-28 03:00:00 UTC | `d-1ff495203465` | 0.08748 | 2.7453 | 2.7453 | 2.2878 | no | NO_C1_CONFIRM_AFTER_FIRST_TOUCH | NO_C1_CONFIRM_AFTER_FIRST_TOUCH | — |
| 2026-08-28 03:25:00 UTC | `d-1ff495203465` | 0.08797 | 2.1472 | 2.1472 | 1.7893 | yes (50) | GLOBAL_MAX_ONE_OPEN_LONG | GLOBAL_MAX_ONE_OPEN_LONG | — |
| 2026-08-28 04:15:00 UTC | `d-1ff495203465` | 0.08784 | 2.2451 | 2.2451 | 1.8709 | yes (50) | GLOBAL_MAX_ONE_OPEN_LONG | GLOBAL_MAX_ONE_OPEN_LONG | — |
| 2026-08-28 10:00:00 UTC | `d-1ff495203465` | 0.08594 | 4.1282 | 4.1282 | 3.4402 | no | NO_C1_CONFIRM_AFTER_FIRST_TOUCH | NO_C1_CONFIRM_AFTER_FIRST_TOUCH | — |
| 2026-08-28 16:30:00 UTC | `d-1ff495203465` | 0.08485 | 5.0372 | 4.0778 | 3.3982 | yes (57) | SL | SL | -1.2000 |
| 2026-08-28 17:15:00 UTC | `d-0e5e7004dccd` | 0.08523 | 4.5174 | 4.5174 | 3.7645 | no | NO_C1_CONFIRM_AFTER_FIRST_TOUCH | NO_C1_CONFIRM_AFTER_FIRST_TOUCH | — |

### Keil-/Konvergenz (nur Diagnose)

Für jeden Kandidaten: Abstand **untere Trigger-Linie ↔ obere Hauptkanallinie** in % (Wedge-Gap) bei Touch-Start vs Entscheidungszeitpunkt. Negatives `wedge_gap_narrowing` = Gap verkleinert sich bis Entry (Konvergenz).

| touch_utc | line | Gap@touch % | Gap@entry % | Δ Gap | rising sup / falling upper |
| --- | --- | ---: | ---: | ---: | --- |
| 2026-08-25 21:05:00 UTC | `d-03350098db64` | 9.4278 | 9.4247 | -0.0031 | no |
| 2026-08-25 21:10:00 UTC | `d-03350098db64` | 9.4263 | 9.4232 | -0.0031 | no |
| 2026-08-26 14:45:00 UTC | `d-03350098db64` | 9.0995 | 9.0995 | 0.0000 | no |
| 2026-08-26 15:05:00 UTC | `d-a6d9a05da2f1` | 9.7192 | 9.7192 | 0.0000 | yes |
| 2026-08-26 15:00:00 UTC | `d-a6d9a05da2f1` | 9.7415 | 9.6969 | -0.0445 | yes |
| 2026-08-26 15:25:00 UTC | `d-03350098db64` | 9.0870 | 9.0870 | 0.0000 | no |
| 2026-08-26 15:15:00 UTC | `d-a6d9a05da2f1` | 9.6747 | 9.6302 | -0.0445 | yes |
| 2026-08-26 15:50:00 UTC | `d-03350098db64` | 9.0792 | 9.0760 | -0.0031 | no |
| 2026-08-26 16:00:00 UTC | `d-03350098db64` | 9.0760 | 9.0760 | 0.0000 | no |
| 2026-08-26 15:55:00 UTC | `d-03350098db64` | 9.0776 | 9.0745 | -0.0031 | no |
| 2026-08-26 16:10:00 UTC | `d-03350098db64` | 9.0729 | 9.0729 | 0.0000 | no |
| 2026-08-26 16:15:00 UTC | `d-a6d9a05da2f1` | 9.4081 | 9.4081 | 0.0000 | yes |
| 2026-08-26 17:00:00 UTC | `d-a6d9a05da2f1` | 9.2087 | 9.1645 | -0.0442 | yes |
| 2026-08-26 17:05:00 UTC | `d-03350098db64` | 9.0557 | 9.0526 | -0.0031 | no |
| 2026-08-26 17:05:00 UTC | `d-a6d9a05da2f1` | 9.1866 | 9.1424 | -0.0442 | yes |
| 2026-08-26 20:00:00 UTC | `d-a6d9a05da2f1` | 8.4168 | 8.3731 | -0.0437 | yes |
| 2026-08-26 20:15:00 UTC | `d-a6d9a05da2f1` | 8.3512 | 8.3512 | 0.0000 | yes |
| 2026-08-26 20:25:00 UTC | `d-a6d9a05da2f1` | 8.3075 | 8.3075 | 0.0000 | yes |
| 2026-08-26 20:35:00 UTC | `d-a6d9a05da2f1` | 8.2639 | 8.2202 | -0.0437 | yes |
| 2026-08-27 05:30:00 UTC | `d-a6d9a05da2f1` | 5.9649 | 5.9226 | -0.0423 | yes |
| 2026-08-27 12:35:00 UTC | `d-a6d9a05da2f1` | 4.1899 | 4.1899 | 0.0000 | yes |
| 2026-08-27 13:45:00 UTC | `d-a6d9a05da2f1` | 3.9018 | 3.8607 | -0.0411 | yes |
| 2026-08-27 13:50:00 UTC | `d-a6d9a05da2f1` | 3.8812 | 3.8402 | -0.0411 | yes |
| 2026-08-27 18:15:00 UTC | `d-a6d9a05da2f1` | 2.8014 | 2.7610 | -0.0404 | yes |
| 2026-08-27 18:35:00 UTC | `d-a6d9a05da2f1` | 2.7206 | 2.7206 | 0.0000 | yes |
| 2026-08-27 18:40:00 UTC | `d-a6d9a05da2f1` | 2.7004 | 2.6600 | -0.0404 | yes |
| 2026-08-27 18:45:00 UTC | `d-a6d9a05da2f1` | 2.6802 | 2.6398 | -0.0404 | yes |
| 2026-08-27 18:50:00 UTC | `d-a6d9a05da2f1` | 2.6600 | 2.6197 | -0.0403 | yes |
| 2026-08-27 20:00:00 UTC | `d-a6d9a05da2f1` | 2.3781 | 2.3379 | -0.0402 | yes |
| 2026-08-27 20:15:00 UTC | `d-a6d9a05da2f1` | 2.3179 | 2.2777 | -0.0401 | yes |
| 2026-08-27 20:35:00 UTC | `d-a6d9a05da2f1` | 2.2376 | 2.2376 | 0.0000 | yes |
| 2026-08-27 20:30:00 UTC | `d-a6d9a05da2f1` | 2.2576 | 2.2175 | -0.0401 | yes |
| 2026-08-27 20:50:00 UTC | `d-a6d9a05da2f1` | 2.1774 | 2.1374 | -0.0401 | yes |
| 2026-08-27 20:55:00 UTC | `d-a6d9a05da2f1` | 2.1574 | 2.1174 | -0.0401 | yes |
| 2026-08-27 21:15:00 UTC | `d-a6d9a05da2f1` | 2.0773 | 2.0373 | -0.0400 | yes |
| 2026-08-27 23:10:00 UTC | `d-a6d9a05da2f1` | 1.6187 | 1.5789 | -0.0397 | yes |
| 2026-08-27 23:15:00 UTC | `d-a6d9a05da2f1` | 1.5988 | 1.5590 | -0.0397 | yes |
| 2026-08-28 00:00:00 UTC | `d-a6d9a05da2f1` | 1.4202 | 1.4202 | 0.0000 | yes |
| 2026-08-28 00:35:00 UTC | `d-a6d9a05da2f1` | 1.2816 | 1.2816 | 0.0000 | yes |
| 2026-08-28 01:05:00 UTC | `d-a6d9a05da2f1` | 1.1630 | 1.1235 | -0.0395 | yes |
| 2026-08-28 01:45:00 UTC | `d-a6d9a05da2f1` | 1.0052 | 0.9658 | -0.0394 | yes |
| 2026-08-28 02:30:00 UTC | `d-1ff495203465` | 2.6484 | 2.6807 | 0.0323 | no |
| 2026-08-28 02:45:00 UTC | `d-1ff495203465` | 2.6968 | 2.7291 | 0.0323 | no |
| 2026-08-28 03:00:00 UTC | `d-1ff495203465` | 2.7453 | 2.7453 | 0.0000 | no |
| 2026-08-28 03:15:00 UTC | `d-1ff495203465` | 2.7939 | 2.8263 | 0.0324 | no |
| 2026-08-28 04:05:00 UTC | `d-1ff495203465` | 2.9561 | 2.9887 | 0.0325 | no |
| 2026-08-28 10:00:00 UTC | `d-1ff495203465` | 4.1282 | 4.1282 | 0.0000 | no |
| 2026-08-28 16:20:00 UTC | `d-1ff495203465` | 5.4228 | 5.4574 | 0.0346 | no |
| 2026-08-28 17:15:00 UTC | `d-0e5e7004dccd` | 4.5174 | 4.5174 | 0.0000 | yes |

## Aufgabe 4 — 27.08.2026 12:00–14:00 UTC

- Touch **2026-08-27 12:35:00 UTC** `te-d-a6d9a05da2f1-0012` line `d-a6d9a05da2f1` | C1=no | Kandidat@ 2026-08-27 12:35:00 UTC Preis 0.0870448766 | Room **4.1899 %** | Pool no | A: **NO_C1_CONFIRM_AFTER_FIRST_TOUCH** | B: NO_C1_CONFIRM_AFTER_FIRST_TOUCH
- Touch **2026-08-27 13:45:00 UTC** `te-d-a6d9a05da2f1-0013` line `d-a6d9a05da2f1` | C1=yes | Kandidat@ 2026-08-27 13:55:00 UTC Preis 0.0877300000 | Room **3.2909 %** | Pool yes (60) | A: **GLOBAL_MAX_ONE_OPEN_LONG** | B: GLOBAL_MAX_ONE_OPEN_LONG
- Touch **2026-08-27 13:50:00 UTC** `te-d-a6d9a05da2f1-0014` line `d-a6d9a05da2f1` | C1=yes | Kandidat@ 2026-08-27 14:00:00 UTC Preis 0.0880800000 | Room **2.8751 %** | Pool yes (60) | A: **GLOBAL_MAX_ONE_OPEN_LONG** | B: GLOBAL_MAX_ONE_OPEN_LONG
_Hinweis: Qualifizierte Kandidaten am 27.08. wurden in A durch **GLOBAL_MAX_ONE_OPEN_LONG** blockiert (Referenzlong ab 26.08. 15:10 noch offen)._

## Aufgabe 4 — 27.08.2026 17:00–19:00 UTC

- Touch **2026-08-27 18:15:00 UTC** `te-d-a6d9a05da2f1-0015` line `d-a6d9a05da2f1` | C1=yes | Kandidat@ 2026-08-27 18:25:00 UTC Preis 0.0881600000 | Room **2.5004 %** | Pool yes (56) | A: **GLOBAL_MAX_ONE_OPEN_LONG** | B: GLOBAL_MAX_ONE_OPEN_LONG
- Touch **2026-08-27 18:35:00 UTC** `te-d-a6d9a05da2f1-0016` line `d-a6d9a05da2f1` | C1=no | Kandidat@ 2026-08-27 18:35:00 UTC Preis 0.0879619277 | Room **2.7206 %** | Pool no | A: **NO_C1_CONFIRM_AFTER_FIRST_TOUCH** | B: NO_C1_CONFIRM_AFTER_FIRST_TOUCH
- Touch **2026-08-27 18:40:00 UTC** `te-d-a6d9a05da2f1-0017` line `d-a6d9a05da2f1` | C1=yes | Kandidat@ 2026-08-27 18:50:00 UTC Preis 0.0882900000 | Room **2.3230 %** | Pool yes (56) | A: **GLOBAL_MAX_ONE_OPEN_LONG** | B: GLOBAL_MAX_ONE_OPEN_LONG
- Touch **2026-08-27 18:45:00 UTC** `te-d-a6d9a05da2f1-0018` line `d-a6d9a05da2f1` | C1=yes | Kandidat@ 2026-08-27 18:55:00 UTC Preis 0.0883800000 | Room **2.2135 %** | Pool yes (56) | A: **GLOBAL_MAX_ONE_OPEN_LONG** | B: GLOBAL_MAX_ONE_OPEN_LONG
- Touch **2026-08-27 18:50:00 UTC** `te-d-a6d9a05da2f1-0019` line `d-a6d9a05da2f1` | C1=yes | Kandidat@ 2026-08-27 19:00:00 UTC Preis 0.0886700000 | Room **1.8739 %** | Pool yes (56) | A: **GLOBAL_MAX_ONE_OPEN_LONG** | B: GLOBAL_MAX_ONE_OPEN_LONG
_Hinweis: Qualifizierte Kandidaten am 27.08. wurden in A durch **GLOBAL_MAX_ONE_OPEN_LONG** blockiert (Referenzlong ab 26.08. 15:10 noch offen)._

## Interpretation

- Trades knapp unter der oberen Kanalbegrenzung zeigen **Room % < 1,50** und oft **Gewinn/SL-Ratio < 1** (weniger potentielles Brutto-TP als fixer SL).
- Keil-Geometrie (steigende Unterstützung + fallende Obergrenze) ist bei den `d-a6d9a05da2f1`-Phasen am Fensterende typisch; Gap schrumpft Richtung Entry.
- Filter B entfernt den 01:55-SL ohne den 26.08-CHANNEL_UPPER (+6,54 %) zu verlieren.

