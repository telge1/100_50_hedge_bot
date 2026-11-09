# L1 V3I — Dynamic Support vs OLD Allowlist

- drawings SHA256: `615589e345828f087467a62cbd3755ef025f929ea71f03a3346f937ab37d894b` (count 22)
- Snapshot: `l1_v3i_drawings_snapshot_615589e345828f08.json`
- Gleich V3H (`d9a668c028ad13b5…`): **nein**
- Fenster: 2026-08-25 .. 2026-09-20 exkl.

## Vergleichstabelle

| Modus / Variante | Trades | Gewinne | SL | Gesamtbrutto % |
| --- | ---: | ---: | ---: | ---: |
| OLD_ALLOWLIST A | 3 | 2 | 1 | 7.7204 |
| OLD_ALLOWLIST B | 2 | 2 | 0 | 8.9204 |
| OLD_ALLOWLIST C | 2 | 2 | 0 | 8.9204 |
| DYNAMIC_SUPPORT A | 5 | 3 | 2 | 8.0551 |
| DYNAMIC_SUPPORT B | 4 | 3 | 1 | 9.2551 |
| DYNAMIC_SUPPORT C | 4 | 3 | 1 | 9.1796 |

## September (DYNAMIC_SUPPORT)

- Support-Touches (long_trigger_allowed): 21
- C1-Entries: 13
- Zugelassen A/B/C: 42/40/33
- Ausgeführt A/B/C: 1/1/1

## OLD Regressionscheck (vs V3H)

- OLD A brutto 7.7204 (ref 7.7204)
- OLD B/C brutto 8.9204 (ref 8.9204)

## Antworten §19

1. **Ja** — im Modus `DYNAMIC_SUPPORT` werden alle geeigneten diagonalen Support-Trendlinien ohne feste ID-Allowlist geprüft (weiterhin: Support von oben, unter Hauptkanal, kein Hline/Upper-Main).
2. September C1-Entries dynamisch: **13** (OLD: 0).
3. September ausgeführt dynamisch A/B/C: **{'A': 1, 'B': 1, 'C': 1}**.
4. A/B/C-Filterlogik unverändert; Unterschiede nur durch mehr/k weniger Trigger-Linien.
5. Einschränkungen: Kausalität (`drawing_causality`) getrennt von Geometrie; `extend_right=false` endet am 2. Anker; retrospektive Linien nicht als live-kausal deklarieren.

Neue dynamische Support-IDs (nicht in OLD-Allowlist): d-3bd7446b7a5b, d-5c8ffbfb8a06, d-e1c6e07765fe
