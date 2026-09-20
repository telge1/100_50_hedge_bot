# LIQUIDITY_DESTINATION_BIAS — Phase 1C Abschlussbericht

Verdict:

`LIQUIDITY_DESTINATION_MULTIDAY_BUILDER_V1_READY_FOR_FULL_BOUNDED_EXPAND`

Phase 1C erweitert ausschließlich die technische Laufzeitarchitektur. Der
fachliche Episode-Contract bleibt unverändert. Es wurde kein vollständiger
Sieben-Tage-Lauf durchgeführt.

## Nachgewiesene Ursache

Vor der ersten Codeänderung wurde `root_cause.md` erstellt. Das
Zwei-Stunden-Limit lag ausschließlich in `BuildConfig.__post_init__()`:
`end-start` wurde gegen `MAX_CANDIDATE_WINDOW_SECONDS=7200` geprüft. Das war
ein operativer Safety-Guard, keine Ziel-, Eligibility-, Touch-, Outcome- oder
Debounce-Regel.

Der alte Builder lud `[start-5m, end+horizon)` mit einer einzigen
ClickHouse-Abfrage zu 1s-Buckets und hielt das vollständige Dictionary im
RAM. `active_until` wurde dagegen bereits in einer einzigen chronologischen
Schleife korrekt global geführt. Ein bloßes Zusammenführen unabhängiger
Tages-CSVs wäre unsicher gewesen.

Skalierende Komponenten waren:

- Trade-Query und Trade-Bucket-Dictionary linear zur Zeitspanne
- LLD-Snapshots sequenziell für jeden nicht blockierten Kandidaten
- Episode- und Ausschlusslisten linear zu den Kandidatenminuten

## Technische Lösung

Der Lauf bleibt logisch kontinuierlich und single-worker. Nur die
Public-Trade-Quelle wird intern in chronologische Kandidatenchunks von
standardmäßig zwei Stunden aufgeteilt. Jeder Chunk lädt zusätzlich fünf
Minuten Warm-up und den vollständigen 60-Minuten-Lookahead.

`active_until`, Episode-Reihenfolge, IDs und Ziel-Freeze werden über alle
Chunks weitergeführt. Source-Provenance wird nach dem Lauf auf die globale
Coverage normalisiert. Es werden keine unabhängigen Ergebnisdateien
zusammengeführt.

Sicherheitsregeln:

- Ohne `--allow-bounded-expand` bleibt das 2h-Limit aktiv.
- Mit Flag ist `--max-window-hours` zwingend und muss in `(0,168]` liegen.
- Der angeforderte Zeitraum darf den expliziten Maximalwert nicht
  überschreiten.
- `--chunk-hours` muss in `(0,2]` liegen; Default ist 2.
- Es gibt keine unbeschränkte Force-Option und keine Parallelisierung.

Die CLI-Hilfe beschreibt RAM-/Query-Auswirkung und sequenzielle read-only
Chunks.

## Geänderte Dateien

- `research/liquidity_destination_bias/builder.py`
  - erweiterte fail-closed `BuildConfig`-Validierung
  - `_process_grid()` für global getragenen Episode-State
  - `_result()` und `_with_coverage()` für deterministische Ergebnisse
  - `build_episodes()` mit begrenztem, chronologischem Trade-Chunking
- `research/liquidity_destination_bias/cli.py`
  - `parse_chunk_hours()`
  - `--allow-bounded-expand`
  - `--max-window-hours`
  - `--chunk-hours`
- `tests/research/test_liquidity_destination_multiday.py`
  - 19 neue Testfälle für die 16 geforderten Safety-, Boundary-,
    Determinismus- und Kausalitätsanforderungen

Unverändert blieben insbesondere `contract.py`, Eligibility, Zielwahl,
Touch-Label, Datenquelle, Entry-Script und die bisherigen 19 Tests. Die
technische Builder-Kennung bleibt zur exakten Core-Rückwärtsparität
`liquidity_destination_episode_builder_v1`; die neue Fähigkeit ist rein
operativ und explizit opt-in.

## Contract-Freeze

```text
contract_frozen_v1.yaml before=66ab82f4ac5e1b416594d17fa50116ed8a6daadae01e9fe29a29586072a91a51
contract_frozen_v1.yaml after =66ab82f4ac5e1b416594d17fa50116ed8a6daadae01e9fe29a29586072a91a51
contract.py before=1e2449c13a3ba54e0f9c61ec5a90d37c91a297b1eb55a67cf1b80c7c56af4d32
contract.py after =1e2449c13a3ba54e0f9c61ec5a90d37c91a297b1eb55a67cf1b80c7c56af4d32
contract.json before=2aad3b9bc2ba949f72f16a11b04ad53d7428d94bdec13ca55497aa30b450f6c3
contract.json after =2aad3b9bc2ba949f72f16a11b04ad53d7428d94bdec13ca55497aa30b450f6c3
CONTRACT_UNCHANGED=true
```

Kanonische LLD-Ziele, 60s Mindestpersistenz, `available_at <= T0`,
ASK-Unterkante/BID-Oberkante, 60m-Horizont, Non-Overlap und Public-Trade-1s
sind unverändert.

## Rückwärtsparität

Das alte Phase-1B-Fenster `[2026-08-25T00:05Z, 02:05Z)` wurde ohne neue
Flags erneut ausgeführt.

BTC:

- 120 Kandidaten, 19 eligible
- 16 UPPER_FIRST, 2 LOWER_FIRST, 1 NEITHER
- 101 `DUPLICATE_OR_OVERLAPPING_EPISODE`
- Core-Fingerprint exakt:
  `355ceb133da88fd036001158a3aba3960bacacf1195231d01828a4b22228f28d`

DOGE:

- 120 Kandidaten, 15 eligible
- 10 UPPER_FIRST, 4 LOWER_FIRST, 1 NEITHER
- 105 `DUPLICATE_OR_OVERLAPPING_EPISODE`
- Core-Fingerprint exakt:
  `4d2e3c3b832246567c2fd2fa70ebfd7e9fe2fe2fe93521807fab33e39ae90fcc`

Damit sind fachliche Felder und alle vier Core-Artefakte bytegenau
rückwärtskompatibel.

## Tests

Vor dem Pilot:

- bisherige Phase-1-Tests: 19/19 bestanden
- neue Phase-1C-Testfälle: 19/19 bestanden
- kombiniert: 38/38 in 0,19s, Max RSS 38.968 KiB
- bestehende LLD-/Kausalitätstests: 9/9 bestanden
- Compile-Prüfung und IDE-Lints: bestanden

Nach dem Pilot:

- 38/38 erneut bestanden
- Compile-Prüfung erneut bestanden

Geprüft wurden unter anderem Default-Guard, Flag/Limit-Kombinationen,
ungültige Fenster, Contract-Freeze, Episode über Chunk-Grenze, kein
doppeltes Opening, kein verlorenes Outcome, geschlossenes NEITHER,
Chunkgrößenparität, IDs, Prefix, Idempotenz und No-Future-Targetwahl.

## Begrenzter 6h-Pilot

Genau ein Mehrstundenpilot wurde ausgeführt:

```text
symbol=BTCUSDT
candidate_window=[2026-08-25T00:05:00Z, 2026-08-25T06:05:00Z)
horizon_minutes=60
candidate_chunks=3 x 2h
single_worker=true
nice=+10
```

Ergebnis:

- 360 Kandidaten
- 41 eligible Episoden
- 31 UPPER_FIRST
- 7 LOWER_FIRST
- 3 NEITHER_WITHIN_HORIZON
- 0 SIMULTANEOUS_OR_AMBIGUOUS
- 305 `DUPLICATE_OR_OVERLAPPING_EPISODE`
- 14 `MISSING_UPPER_TARGET`
- Core-Fingerprint:
  `009048ed15801cb890eb7a91cc0e002d949416760f4dd1782fe90c8852709bff`

Diese Verteilung ist ausschließlich ein technisches Faktenresultat. Sie
wird ausdrücklich nicht als Bias interpretiert; insbesondere waren im
Phase-1B-Datensatz obere und untere Distanzen stark asymmetrisch.

Pilot-Gates bestanden:

- 360/360 Kandidaten verbucht
- keine doppelten IDs
- keine überlappenden Episoden
- chronologische Reihenfolge
- alle Horizons vollständig geschlossen
- bekannte Outcomes
- Target-Availability nicht nach T0
- Touchzeiten im Horizon
- keine NaN-/Infinity-Werte
- Core-Hashes gültig

## Chunk-, Boundary-, Prefix- und Idempotenznachweis

Synthetische Tests erzeugten bei 2m- und 3m-internen Chunks identische
fachliche Outputs und IDs. Episoden mit Touch beziehungsweise NEITHER über
einer Chunk-Grenze wurden weder verloren noch doppelt geöffnet.

Empirisch wurde der erste interne 2h-Chunk des 6h-Piloten gegen den
eigenständigen alten 2h-Lauf geprüft:

- Episode-IDs, T0, Ziele, Preise, Outcomes, Touchzeiten und Snapshothashes
  identisch
- Ausschlusszeilen vollständig identisch
- ausschließlich `source_coverage_end_utc` ist für den kontinuierlichen
  6h-Lauf erwartungsgemäß global statt auf 2h begrenzt

Idempotenz ist durch den aktuellen 2h-Lauf gegen den früheren Phase-1B-Lauf
mit identischem Core-Fingerprint sowie durch wiederholte synthetische
Chunk-Läufe bestätigt.

## Ressourcen

- BTC 2h Rückwärtsparität: 4,44s Wall, 75.456 KiB Max RSS
- DOGE 2h Rückwärtsparität: 2,00s Wall, 72.292 KiB Max RSS
- BTC 6h: 12,13s Wall, 5,20s User CPU, 0,03s System CPU,
  95.388 KiB Max RSS, 43 % CPU

Bei dreifacher Kandidatenzeit stieg Max RSS nur um etwa 26 % gegenüber dem
2h-BTC-Lauf. Es gab keine Swaps, keine unkontrollierte RAM-Zunahme und keine
beobachtete ClickHouse-Beeinträchtigung.

## Erwartete sichere spätere Sieben-Tage-Ausführung

Ein später separat freigegebener BTC-Lauf kann mit
`--allow-bounded-expand --max-window-hours 168` und dem 2h-Chunk-Default
ausgeführt werden. Das belegte Fenster ab `2026-08-25T00:05Z` bis
`2026-08-31T23:01Z` benötigt 84 sequenzielle Chunks. Jede einzelne
Trade-Abfrage bleibt einschließlich Warm-up und Lookahead höchstens 3h05m
groß und damit unter dem bestehenden 3h10m-Source-Guard.

Der Lauf bleibt single-worker und hält höchstens einen Trade-Chunk plus
kleine Ergebnislisten im RAM. Laufzeit und ClickHouse-Last müssen trotzdem
überwacht werden; der 6h-Pilot beweist kontrollierte Skalierung, nicht eine
Lastgarantie für jede Marktphase.

## Bekannte Risiken und Grenzen

- 84 sequenzielle Abfragen wiederholen Warm-up/Lookahead an Grenzen; dies
  begrenzt RAM, erhöht aber die gesamte Read-Arbeit.
- LLD-Snapshotkosten wachsen mit nicht blockierten Kandidaten.
- Ergebnislisten bleiben bis zum finalen CSV-Schreiben im RAM, sind jedoch
  minutenbasiert und wesentlich kleiner als Trade-Rohdaten.
- Source-Completeness verwendet weiterhin den eingefrorenen
  Event-Status-/Randvertrag.
- DOGE erhielt keinen optionalen 6h-Pilot; seine 2h-Rückwärtsparität ist
  vollständig bestätigt.
- Kein Full OB, kein `--now`, keine Bias-/Modell-/Strategie-Funktion.

## Git- und Sicherheitsstatus

- Branch: `feature/btc-doge-research-db`
- HEAD unverändert:
  `9ab70ee955ff5a47e2c42ecaa6d727df581c2d34`
- Vorbestehend: 20 untracked Top-Level-Einträge
- Phase 1C: zwei geänderte vorbestehende untracked Code-Dateien, eine neue
  Testdatei und ausschließlich neue Artefakte im Phase-1C-Ergebnisverzeichnis
- Kein Commit oder Push

```text
FULL_SEVEN_DAY_RUN_EXECUTED=false
BIAS_CALCULATION_EXECUTED=false
DATABASE_WRITES_EXECUTED=false
LIVE_PROCESSES_CHANGED=false
DASHBOARD_CHANGED=false
COLLECTOR_CHANGED=false
STRATEGY_CHANGED=false
TRADING_CHANGED=false
COMMIT_CREATED=false
PUSH_EXECUTED=false
```

**STOP.** Der vollständige historische Expand wurde nicht begonnen.
