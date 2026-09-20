# LIQUIDITY_DESTINATION_BIAS — Phase 1D Abschlussbericht

Verdict:

`LIQUIDITY_DESTINATION_FULL_BOUNDED_HISTORY_V1_READY_FOR_BASELINES`

Phase 1D materialisiert ausschließlich den eingefrorenen kausalen Episode-Datensatz
über den maximalen sicher belegten Zeitraum. Es wurde kein Bias berechnet, kein
Modell trainiert und keine Strategie entwickelt.

## Git und Freeze

- Branch: `feature/btc-doge-research-db`
- HEAD vor und nach dem Lauf: `9ab70ee955ff5a47e2c42ecaa6d727df581c2d34`
- Vorbestehende Dirty-/untracked-Dateien wurden nicht überschrieben.
- Neu: ausschließlich `results/liquidity_destination_bias_phase_1d_full_bounded_history/`
- Kein Commit oder Push

Vor dem Expand wurden alle Contract-/Builder-/Testdateien gegen das Phase-1C-
Manifest geprüft: vollständig identisch. Nach dem Expand unverändert.

```text
builder.py   0f11bd098d3936cbdce6c1e9aee7f8a3d8d9a08f5813f6dfd65b58d7db30d431
cli.py       69b84faaa5390b16b89d40eccfcfb8039c17a232cea73e187a62c4184dd7bb67
contract.py  1e2449c13a3ba54e0f9c61ec5a90d37c91a297b1eb55a67cf1b80c7c56af4d32
contract.yaml 66ab82f4ac5e1b416594d17fa50116ed8a6daadae01e9fe29a29586072a91a51
contract.json 2aad3b9bc2ba949f72f16a11b04ad53d7428d94bdec13ca55497aa30b450f6c3
CODE_CHANGED=false
CONTRACT_CHANGED=false
TEST_CHANGED=false
```

Pre-Expand-Tests: 38/38 Builder-Tests und 9/9 LLD-/Kausalitätstests bestanden.

## Coverage und Laufgrenzen

Die Phase-1B-Coverage-Artefakte belegen die vorgesehenen Grenzen. Es wurde keine
Grenze ausgeweitet.

| Symbol | candidate_start | candidate_end_exclusive | Stunden | ~2h-Chunks |
|---|---|---|---:|---:|
| BTCUSDT | 2026-08-25T00:05:00Z | 2026-08-31T23:01:00Z | 166,9333 | 84 |
| DOGEUSDT | 2026-08-25T00:05:00Z | 2026-08-31T23:00:00Z | 166,9167 | 84 |

Warm-up beginnt jeweils 5 Minuten vor Start. Horizon-Lookahead endet 60 Minuten
nach dem letzten `T0`. Beide Fenster liegen unter dem expliziten Limit von 168
Stunden.

## Ergebnisse

| Symbol | Kandidaten | Eligible | Ausschlüsse | UPPER | LOWER | NEITHER | AMBIGUOUS |
|---|---:|---:|---:|---:|---:|---:|---:|
| BTCUSDT | 10016 | 879 | 9137 | 404 | 411 | 64 | 0 |
| DOGEUSDT | 10015 | 840 | 9175 | 372 | 405 | 63 | 0 |
| Gesamt | 20031 | 1719 | 18312 | 776 | 816 | 127 | 0 |

Eligible-Quote gesamt: 8,58 %. NEITHER-Anteil: 7,39 %.

Ausschlüsse:

- BTC: 9070 `DUPLICATE_OR_OVERLAPPING_EPISODE`, 44 `MISSING_UPPER_TARGET`,
  23 `MISSING_LOWER_TARGET`
- DOGE: 9086 `DUPLICATE_OR_OVERLAPPING_EPISODE`, 8 `MISSING_UPPER_TARGET`,
  81 `MISSING_LOWER_TARGET`

## Tages- und Stundenverteilung

Unabhängige UTC-Tage: 7 (`2026-08-25` bis `2026-08-31`).

Eligible Episoden gesamt je Tag:

- 2026-08-25: 264
- 2026-08-26: 254
- 2026-08-27: 248
- 2026-08-28: 308
- 2026-08-29: 209
- 2026-08-30: 247
- 2026-08-31: 189

Größter Tag: `2026-08-28` mit 17,92 % der Episoden.  
Größte UTC-Stunde: `13` mit 5,99 % der Episoden.

Nicht überlappende Episoden können trotzdem durch dieselbe Marktphase korreliert
sein. Die Tagesverteilung ist breit genug für ein rein technisches
Engineering-Gate, ersetzt aber keine Regime-Unabhängigkeit.

## First-Touch und Distanzen

P25 / Median / P75:

- First-Touch gesamt: 11,0 / 82,5 / 481,5 Sekunden (n=1592)
- BTC First-Touch: 11 / 82 / 445 Sekunden
- DOGE First-Touch: 12 / 85 / 511 Sekunden
- Obere Distanz gesamt: 4,61 / 18,38 / 45,09 bps
- Untere Distanz gesamt: 4,78 / 19,65 / 43,71 bps
- BTC oben/unten Median: 14,14 / 14,53 bps
- DOGE oben/unten Median: 27,90 / 25,96 bps
- Verhältnis oben/unten Median gesamt: ≈1,08

> Ein häufigeres UPPER_FIRST kann allein durch eine geringere Entfernung des
> oberen Ziels verursacht werden und beweist keine Richtungsvorhersage.

Im vorliegenden Gesamtdatensatz sind UPPER und LOWER nahezu ausgewogen
(776 vs 816), und die Median-Distanzen liegen eng beieinander. Das ist eine
deskriptive Beobachtung und keine Bias- oder Edge-Aussage.

## Prefix-Parität gegen 6h-Pilot

Der sichere Prefix `2026-08-25T00:05Z–06:05Z` des BTC-Vollaufs wurde mit dem
Phase-1C-Pilot verglichen:

- 360 Kandidaten
- 41 Episoden
- 31 UPPER, 7 LOWER, 3 NEITHER
- fachliche Episode-Felder und IDs identisch
- Ausschlusszeilen identisch
- Provenance-Felder `source_coverage_*` bewusst vom Semantikvergleich ausgenommen

Prefix-Parität: bestanden.

## Idempotenz

Der vollständige BTC-Lauf wurde in `idempotence_btc_repeat/` wiederholt
(beide Läufe < 15 Minuten, Serverlast kontrolliert).

```text
same_input=true
same_contract=true
same_core_output_hashes=true
core_fingerprint=a938a9f79ba95cf354dd288d23f0aa841392280de54ff75325189a71ee33f16a
```

DOGE Core-Fingerprint:

`e2f05258a578618c3a8da2a2a4c2826adb0651145c7320d3f7d1715b9c8efb86`

## Qualitätsgate

Beide Symbole bestanden:

- Exitcode 0
- Contract/Builder unverändert
- keine doppelten IDs
- keine überlappenden eligible Episoden
- chronologische Sortierung
- korrekte Zielreihenfolge
- Knowledge-Cutoff und Availability ≤ T0
- Touchzeiten im Horizon
- NEITHER nur mit geschlossenem Horizont
- bekannte Outcomes und Exclusion Reasons
- keine NaN/Infinity
- Core-Hashes gültig
- 10016/10016 bzw. 10015/10015 verbuchte Kandidaten

## Engineering-Gate

```text
eligible_episodes_at_least_100=true   (1719)
independent_utc_days_at_least_3=true  (7)
both_touch_classes_present=true
quality_gate_passed=true
contract_builder_unchanged=true
prefix_parity_passed=true
no_critical_coverage_gap=true
resources_controlled=true
ENGINEERING_GATE_PASSED=true
```

Bedeutung: Der Datensatz ist technisch geeignet für spätere Baseline-Forschung.
Es ist kein Beweis für Bias, Edge, Vorhersagegüte oder Profitabilität.

## Ressourcen

| Lauf | Wall | User CPU | Max RSS | CPU% |
|---|---:|---:|---:|---:|
| BTC full | 4:39,02 | 93,92s | 101.160 KiB | 33 |
| DOGE full | 2:01,54 | 90,20s | 98.812 KiB | 74 |
| BTC idempotence | 4:39,00 | — | 102.004 KiB | — |

Vor BTC: Load ~0,80 / verfügbar ~13 GiB RAM.  
Während BTC-Peak: Load ~6,42; Swap unverändert ~1,2 GiB.  
Nach DOGE: Load ~2,6–3,3; verfügbar weiter ~13 GiB.

Single Process, `nice +10`, keine Parallelisierung. Keine Aborts notwendig.
RSS blieb chunkbegrenzt und wuchs nicht unkontrolliert gegenüber dem 6h-Pilot
(~95 MiB → ~101 MiB).

## Bekannte Grenzen

- Nur LLD_POOL; keine Full-OB-Destinationen.
- Source-Completeness folgt dem eingefrorenen Event-Status-/Randvertrag.
- Episoden können marktphasenkorreliert sein trotz Non-Overlap.
- Keine Bias-, Feature-, Schwellen- oder Modellauswertung.
- Phase 2 / Baselines wurden nicht begonnen.

## Sicherheitsbestätigung

```text
CODE_CHANGED=false
CONTRACT_CHANGED=false
TEST_CHANGED=false
DATABASE_WRITES_EXECUTED=false
BACKFILL_EXECUTED=false
LIVE_PROCESSES_CHANGED=false
DASHBOARD_CHANGED=false
COLLECTOR_CHANGED=false
STRATEGY_CHANGED=false
TRADING_CHANGED=false
COMMIT_CREATED=false
PUSH_EXECUTED=false
BASELINE_STARTED=false
PHASE_2_STARTED=false
```

**STOP.** Keine Baseline und keine Phase 2 beginnen.
