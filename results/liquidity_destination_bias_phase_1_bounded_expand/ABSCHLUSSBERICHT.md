# LIQUIDITY_DESTINATION_BIAS — Phase 1B Abschlussbericht

Verdict:

`LIQUIDITY_DESTINATION_BOUNDED_HISTORY_V1_SMALL_N`

Phase 1B erzeugt ausschließlich beschreibende, kausale Fakten-Episoden. Die
Outcome-Verteilung ist keine Aussage über Bias, Vorhersagegüte, Edge,
Profitabilität, Strategie oder Trading.

## Git- und Freeze-Status

- Branch: `feature/btc-doge-research-db`
- HEAD vor und nach dem Lauf:
  `9ab70ee955ff5a47e2c42ecaa6d727df581c2d34`
- Vor Beginn bestanden 20 untracked Worktree-Einträge des Nutzers,
  einschließlich der uncommitteten Phase-1-Dateien.
- Hinzugekommen ist ausschließlich
  `results/liquidity_destination_bias_phase_1_bounded_expand/`.
- Kein Reset, Cleanup, Branchwechsel, Commit oder Push.

13 Phase-1-Code-, CLI-, Test-, Contract- und Schema-Dateien stimmen einzeln
mit dem Phase-1-Manifest überein.

```text
freeze_fingerprint_before=ee29a49f941fe85a32ebdda7814649cdc3fb84e9b7c7ccae1eea80a5aaf278d8
freeze_fingerprint_after=ee29a49f941fe85a32ebdda7814649cdc3fb84e9b7c7ccae1eea80a5aaf278d8
freeze_unchanged=true
```

Verifizierter Contract: kanonische LLD-Pools, `available_at <= T0` mit
60 Sekunden Mindestpersistenz, ASK-Unterkante/BID-Oberkante als Touch,
60-Minuten-Horizont, eine aktive Episode pro Symbol und read-only
Public-Trade-1s als Preisweg.

## Coverage-Nachweis

Die geschlossenen 1m-Candles für die LLD-Quelle reichen für beide Symbole von
`2025-12-11T00:00:00Z` bis `2026-09-05T09:55:00Z`. Je Symbol wurden 386.516
geschlossene Minuten gezählt; das entspricht einer lückenlosen Minutenfolge.
Kanonische LLD-Snapshots wurden zusätzlich für beide Symbole an
`2026-08-24T22:53:00Z` und `2026-08-31T22:59:00Z` erfolgreich erzeugt.

Public Trades reichen für BTC bis `2026-08-31T23:59:59.161Z` und für DOGE bis
`2026-08-31T23:59:27.155Z`. Alle abgefragten Events im Untersuchungsbereich
tragen `coverage_status=COMPLETE`. Leere Trade-Sekunden sind gemäß
eingefrorenem Contract gültige No-Trade-Zustände.

Konservativ nachgewiesene T0-Fenster:

- BTC: `[2026-08-24T22:53:00Z, 2026-08-31T23:01:00Z)`
- DOGE: `[2026-08-24T22:53:00Z, 2026-08-31T23:00:00Z)`
- Gemeinsames Fenster:
  `[2026-08-24T22:53:00Z, 2026-08-31T23:00:00Z)`

Der gespeicherte Contract begrenzt einen Kandidatenlauf jedoch unveränderlich
auf 7.200 Sekunden. Die CLI besitzt weder persistierbaren Episode-State noch
einen nachgewiesenen Boundary-Dedup-Merge. Deshalb wurde kein unsicherer
Tagessegment-Merge gebaut. Der größte sichere Einzellauf war:

```text
candidate_window       [2026-08-25T00:05:00Z, 2026-08-25T02:05:00Z)
warmup_window          [2026-08-25T00:00:00Z, 2026-08-25T00:05:00Z)
label_lookahead_window [2026-08-25T02:05:00Z, 2026-08-25T03:05:00Z)
```

Alle 120 Kandidaten je Symbol und alle individuellen Horizonte sind
geschlossen. Längste leere Trade-Sekundenfolge im geladenen Bereich:
BTC 6 Sekunden, DOGE 21 Sekunden; dies ist nach Contract keine Datenlücke.
Details stehen in `coverage_proof.csv`.

## Ausführung und Ergebnisse

BTC wurde zuerst ausgeführt und bestand das vollständige Qualitätsgate.
DOGE wurde erst danach ausgeführt.

| Symbol | Kandidaten | Eligible | Ausschlüsse | UPPER | LOWER | NEITHER | AMBIGUOUS |
|---|---:|---:|---:|---:|---:|---:|---:|
| BTCUSDT | 120 | 19 | 101 | 16 | 2 | 1 | 0 |
| DOGEUSDT | 120 | 15 | 105 | 10 | 4 | 1 | 0 |
| Gesamt | 240 | 34 | 206 | 26 | 6 | 2 | 0 |

Alle 206 Ausschlüsse lauten
`DUPLICATE_OR_OVERLAPPING_EPISODE`. Die Eligible-Quote beträgt BTC 15,83 %,
DOGE 12,50 % und gesamt 14,17 %. Der NEITHER-Anteil beträgt gesamt 5,88 %.

Alle Episoden liegen am UTC-Tag `2026-08-25`:

- BTC: 16 UPPER, 2 LOWER, 1 NEITHER
- DOGE: 10 UPPER, 4 LOWER, 1 NEITHER
- Gesamt: 26 UPPER, 6 LOWER, 2 NEITHER

UTC-Stundenkonzentration: 28 Episoden in Stunde 00, 4 in Stunde 01 und 2 in
Stunde 02. Damit liegen 82,35 % in einer UTC-Stunde. Nicht überlappende
Episoden können trotzdem durch dieselbe Marktphase korreliert sein.

## Deskriptive Verteilungen

P25 / Median / P75:

- First-Touch-Dauer gesamt: 9,75 / 91,00 / 291,50 Sekunden (n=32)
- BTC First-Touch: 10,25 / 58,50 / 136,50 Sekunden (n=18)
- DOGE First-Touch: 10,00 / 122,50 / 316,50 Sekunden (n=14)
- Obere Entfernung gesamt: 4,4805 / 9,3891 / 35,5306 bps
- Untere Entfernung gesamt: 17,1729 / 48,9571 / 92,0561 bps
- BTC oben: 2,1465 / 4,6380 / 10,3061 bps
- BTC unten: 15,0536 / 40,4330 / 125,5043 bps
- DOGE oben: 11,1014 / 38,9451 / 57,0152 bps
- DOGE unten: 23,2418 / 52,0776 / 71,9186 bps

Diese Werte beschreiben nur den erzeugten Datensatz.

## Qualitätsgate

Für beide Symbole bestanden:

- Exitcode 0 und lesbare Core-Artefakte
- unveränderter Contract-Fingerprint
- keine Causality-Verletzung oder unbekanntes Outcome
- keine doppelten Episode-IDs oder überlappenden Eligible-Episoden
- chronologische Sortierung
- Ziele korrekt unter/über `price_t0`
- Knowledge-Cutoff und Target-Availability nicht nach T0
- Touchzeiten innerhalb `[T0, horizon_end)`
- NEITHER nur mit geschlossenem vollständigem Horizont
- vollständige Ausschlussgründe und 120/120 verbuchte Kandidaten
- keine NaN-/Infinity-Werte
- alle manifestierten Core-Hashes gültig

## Idempotenz und Prefix

Der gesamte 2h-Lauf wurde je Symbol wiederholt:

```text
same_input=true
same_contract=true
same_core_output_hashes=true
```

- BTC Core-Fingerprint:
  `355ceb133da88fd036001158a3aba3960bacacf1195231d01828a4b22228f28d`
- DOGE Core-Fingerprint:
  `4d2e3c3b832246567c2fd2fa70ebfd7e9fe2fe2fe93521807fab33e39ae90fcc`

Ein separater Lauf mit identischem Start und Ende `01:05Z` wurde mit dem
sicheren Prefix des 2h-Laufs verglichen. Episode-IDs, T0, eingefrorene Ziele,
Preise, Outcomes, Touchzeiten, Snapshothashes und Ausschlusszeilen sind für
BTC und DOGE identisch. Nur `source_coverage_end_utc` unterscheidet sich
erwartungsgemäß, weil dieses Provenance-Feld das jeweilige Query-Ende
beschreibt. Es wurde nicht segmentiert; ein Segmentgrenzen-Merge war daher
nicht anwendbar und wurde bewusst nicht erfunden.

## Tests und Ressourcen

Vor dem Expand:

- 19/19 Phase-1-Tests bestanden
- 9/9 bestehende LLD-/Kausalitätstests bestanden
- Compile- und Contract-Invarianten bestanden

Nach dem Expand:

- 19/19 Phase-1-Tests erneut bestanden
- Compile-Prüfung bestanden

Ressourcen der Hauptläufe:

- BTC: 4,39s Wall, 1,83s User CPU, 0,02s System CPU, 75.628 KiB Max RSS,
  4,33 Eligible-Episoden/s
- DOGE: 2,05s Wall, 1,45s User CPU, 0,02s System CPU, 71.888 KiB Max RSS,
  7,32 Eligible-Episoden/s
- begrenzte Source-Abfragen: BTC 2,21s, DOGE 0,35s

Single Process und `nice +10` wurden verwendet. Es gab kein auffälliges
Speicherwachstum und keine beobachtete Beeinträchtigung von ClickHouse.

## Engineering-Gate

```text
eligible_episodes_at_least_100=false  (34)
independent_utc_days_at_least_3=false (1)
both_touch_classes_present=true
critical_quality_violation=false
idempotence_passed=true
prefix_boundary_passed=true
ENGINEERING_GATE_PASSED=false
```

Damit ist `SMALL_N` zwingend. Contract, Schwellen und Zeitraum wurden nach
Betrachtung der Outcomes nicht verändert. Phase 2 wurde nicht begonnen.

## Bekannte Begrenzungen

- Der vollständige belegte Kalender konnte wegen des eingefrorenen
  2h-Limits und fehlenden stateful Segment-Merges nicht sicher materialisiert
  werden.
- Nur ein UTC-Tag und starke Stundenkonzentration; keine Aussage über
  unabhängige Marktregime.
- Nur LLD-Pools; keine Full-OB-Destinationen.
- Source-Completeness verwendet Event-`coverage_status` plus Randabdeckung;
  leere Sekunden werden vertragsgemäß nicht als Gap gewertet.
- Kein Bias, keine Wahrscheinlichkeit, keine Feature-Auswahl, kein Modell,
  keine Strategie- oder Profitabilitätsauswertung.

## Sicherheitsbestätigung

```text
BUILDER_CODE_CHANGED=false
CONTRACT_CHANGED=false
LIVE_PROCESSES_CHANGED=false
DATABASE_WRITES_EXECUTED=false
BACKFILL_EXECUTED=false
DASHBOARD_CHANGED=false
COLLECTOR_CHANGED=false
STRATEGY_CHANGED=false
TRADING_ACTIONS_EXECUTED=false
COMMIT_CREATED=false
PUSH_EXECUTED=false
DESTRUCTIVE_ACTIONS_EXECUTED=false
```

**STOP.** Phase 2 wurde nicht begonnen.
