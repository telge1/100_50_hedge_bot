# BTC TPO vs Volume Semantics Audit

**Verdict:** `BTC_TPO_VOLUME_SEMANTICS_NOT_INDEPENDENT_BLOCKED`

**Anchor:** `2026-08-31T19:00:00Z`  
**Session:** `2026-08-31T13:30:00Z` → anchor exklusiv  
**Price step:** `10.0`

## Kurzfazit

Das als **TPO** bezeichnete Profil in BTC OB Fight ist **kein Zeit-/Bracket-TPO**, sondern **Volume-at-Price**
aus `public_trades_canonical` (`sum(size)` pro Preisbin). Das separate Volume Profile verwendet **dieselbe
Gewichtung (Basisvolumen)** und dieselben OA-Algorithmen (`compute_value_area`). Identische POC/VAH/VAL im
Golden-Fall sind daher **keine Konfluenz zweier unabhängiger Verteilungen**, sondern **dieselbe semantische
Messung** mit unterschiedlicher Aggregationsstelle (ClickHouse vs Python-Dedup).

## Levelvergleich Golden

| | POC | VAH | VAL |
|---|---|---|---|
| OA-labeled TPO | 78565.0 | 79140.0 | 78190.0 |
| Local Volume | 78565.0 | 79140.0 | 78190.0 |
| Reference Bracket TPO (Audit) | 78545.0 | 79080.0 | 78230.0 |

## Verteilungsvergleich

- Brackets (30m): `11`
- Summe Bracket-Counts: `486`
- OA volume total: `21512.315000000013`
- Local volume total: `21512.31500000021`
- Hash OA-labeled: `376c81878057f5126468f4e96dfd297208df131a30f801fddd57c86b72003774`
- Hash Local: `de9005c05cef913b142449663aca08f8658e00ece4858d54e20fbda850f628bc`
- Hash Reference Bracket: `7705863cb5d3b4b30afc9b9052e43bd8dd9e459dade9b370a382ea3bffc6c3fb`
- Hashes equal OA vs Local: `False`
- Max norm. share diff: `8.357897707256257e-15`
- Bins with different weights: `0`

## Warum identische Levels trotzdem möglich

Gleiche Session, gleicher Preisstep, gleiche VA-Regel, gleiches Gewichtungsmaß (Basisvolumen). Bei
EXACT-OA-Parität (Golden run_010) sind die Bin-Gewichte praktisch identisch → identische POC/VAH/VAL.
Das Reference Bracket-Profil kann abweichen, weil es ein **anderes Maß** (Präsenz in 30m-Brackets) nutzt.

## Synthetischer Unabhängigkeitstest

- Reference Bracket POC bin: `780` (erwartet A=780)
- Volume POC bin: `790` (erwartet B=790)
- POC differiert: `True`
- Production path volume-weighted: `True`

## TPO↔Volume-Konfluenz

**Status:** `INVALID_SAME_SEMANTICS` — für Fight-Engine **nicht** als zwei
unabhängige Informationen verwendbar.

## Candle-Timeframe

Siehe `dashboard_timeframe_contract.json`: Chart-`timeframe` ändert nur Kerzen, nicht die Profil-Bins.


## Reparaturvorschlag (abgegrenzt, nicht implementiert)

1. Benenne `tpo_*` in BTC OB Fight um zu `oa_volume_*` oder implementiere echtes Bracket-TPO.
2. Echtes TPO: Bracket-Präsenz aus festem Intervall (z. B. 30m) zählen, nicht `sum(size)`.
3. Deaktiviere TPO↔Volume-Konfluenz bis zwei unterschiedliche Maße nachweisbar sind.
4. Fight-Engine erst nach neuem Audit `INDEPENDENT_CONFIRMED`.

