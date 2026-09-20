# LIQUIDITY_DESTINATION_BIAS — Phase 1 Abschlussbericht

Verdict:

`LIQUIDITY_DESTINATION_EPISODE_BUILDER_V1_READY_FOR_BOUNDED_EXPAND`

Phase 1 erzeugt ausschließlich historische, eingefrorene Fakten-Episoden. Sie
erzeugt keinen Bias, keine Wahrscheinlichkeit, kein Trading-Signal und keine
Order.

## Git- und Ausgangszustand

- Branch: `feature/btc-doge-research-db`
- HEAD vor und nach Phase 1: `9ab70ee955ff5a47e2c42ecaa6d727df581c2d34`
- Initiale Dirty-Dateien: 16 untracked, bestehend (große Result-Dumps, `run/`,
  `tmp_debug_multi/` und eine fremde YAML-Datei)
- Bestehende Dirty-Dateien wurden nicht geändert, gelöscht, gestaged oder
  bereinigt.
- Kein Branchwechsel, Commit oder Push.

Alle Änderungen dieses Auftrags liegen ausschließlich in:

- `research/liquidity_destination_bias/`
- `scripts/build_liquidity_destination_episodes.py`
- `tests/research/test_liquidity_destination_episodes.py`
- `results/liquidity_destination_bias_phase_1/`

Phase-0-Artefakte wurden nicht verändert.

## Implementierung

### Vertrag und Datenmodelle

- `contract.py`: Versionen, erlaubte Outcomes, Ausschlussgründe, Spalten und
  expliziter Non-Scope
- `models.py`: immutable `FrozenTarget`, `PriceBucket`, `FrozenEpisode`
- `eligibility.py`: zentrale fail-closed Prüfung

### Kausale Ziele

- `targets.py` verwendet direkt
  `orderbook_analyse.liquidity_pool_signal.chart_pool_adapter.export_snapshot`.
- Source of Truth: `LLD_POOL`,
  `liquidity_pool_signal/canonical_v1`, Timeframe `5m`.
- Nur Pools mit `active_as_of=true` und `available_at <= T0-60s` sind wählbar.
- Oberes Ziel: nächster ASK-Pool strikt oberhalb von `price_t0`.
- Unteres Ziel: nächster BID-Pool strikt unterhalb von `price_t0`.
- Touch oben: eingefrorene Unterkante.
- Touch unten: eingefrorene Oberkante.
- Zielwerte werden in immutable Objekte kopiert; spätere Änderungen am
  Source-Objekt ändern die Episode nicht.

LLD-Pools werden ausdrücklich nicht als sichtbare Full-OB-Walls dargestellt.
OB200, OB1000 und Full OB werden in Phase 1 nicht zur Erfindung ferner Ziele
benutzt.

### Preisquelle und Outcome

- `sources.py`: begrenztes read-only SELECT auf
  `btc_doge_research.research_public_trades FINAL`
- Deduplizierte event-time Trades werden serverseitig zu 1s OHLC aggregiert.
- `price_t0`: letzter Tradebucket strikt vor T0.
- Future Path: Buckets in `[T0, horizon_end)`, ausschließlich für Labeling.
- `path_label.py`: UPPER_FIRST, LOWER_FIRST, NEITHER oder
  SIMULTANEOUS_OR_AMBIGUOUS.
- Reiner Touch, keine Acceptance-Regel.

Ein leerer Trade-Sekundenbucket ist ein valider No-Trade-Zustand, nicht
automatisch eine Datenlücke. Completeness wird über Event-`coverage_status`
und vollständige zeitliche Randabdeckung geprüft.

### Überlappung

Maximal eine aktive Episode pro Symbol. Eine neue Episode darf erst am oder
nach `first_touch_utc` beziehungsweise bei NEITHER nach `horizon_end_utc`
beginnen. Übersprungene 60s-Kandidaten werden mit
`DUPLICATE_OR_OVERLAPPING_EPISODE` protokolliert.

### CLI und Artefakte

`scripts/build_liquidity_destination_episodes.py` unterstützt ausschließlich:

- `--symbol`
- `--start`
- `--end`
- `--horizon-minutes`
- `--output-dir`
- `--require-complete`
- optional `--max-episodes`, `--target-source`, `--log-level`

Kein `--now`, Bias, Predict oder Trade.

Jeder Lauf schreibt:

- `episodes.csv`
- `excluded_candidates.csv`
- `summary.json`
- `contract.json`
- `run_manifest.json`

Episode-IDs und Kernartefakte sind deterministisch. `generated_at_utc` befindet
sich nur im Run-Manifest und beeinflusst die Kern-Hashes nicht.

## Methodische Konflikte und konservative Entscheidung

Phase 0 erwähnte dieselbe Engine später auch für `--now`; Phase 1 verbietet
`--now` ausdrücklich. Es wurde nicht implementiert.

Phase 0 plante Sensitivitätslabels für 5/15/30/60 Minuten. Der Phase-1-Auftrag
fordert einen expliziten CLI-Horizont. Der Builder erzeugt daher nur den
angeforderten, manifestierten Horizont und keine stillen Zusatzlabels.

## Tests

Neue Tests:

- 19/19 bestanden in 0.13s Prozesslaufzeit; max RSS 37,816 KiB.
- Abgedeckt: beide First-Touch-Richtungen, NEITHER, simultaner Touch,
  bereits berührtes Ziel, fehlende Ziele, Überlappung, ungültige Bounds,
  Ziel-Freeze, Future-Pool-Block, Future-Path-Isolation, Horizon-Closure,
  No-Overlap, deterministische ID, Artefakt-Idempotenz, `--require-complete`
  fail-closed und Prefix-Parität.

Bestehende OA-Tests:

- 24/24 bestanden:
  `test_canonical_chart_pool_asof_restore_v1.py`,
  `test_liquidity_pool_signal_foundation_v1.py` und
  `test_liquidity_location_pool_causality_fix_v2.py`
- Laufzeiten 1.21s + 1.52s; max RSS 150,580 KiB.
- Eine bestehende `PytestUnknownMarkWarning` für `integration`; kein Fehler.

Vollständige Ausgabe: `test_results.txt`.

## Begrenzter BTC-Smoke

- Symbol: BTCUSDT
- Kandidatenfenster: `2026-08-30T15:00:00Z` bis
  `2026-08-30T17:00:00Z`
- Horizont: 60 Minuten
- Single Process
- Kandidaten: 120
- Eligible Episoden: 17
- Ausgeschlossen: 103

Outcome-Verteilung:

- `UPPER_FIRST`: 8
- `LOWER_FIRST`: 8
- `NEITHER_WITHIN_HORIZON`: 1
- `SIMULTANEOUS_OR_AMBIGUOUS`: 0

Ausschlüsse:

- `DUPLICATE_OR_OVERLAPPING_EPISODE`: 103

Kausalitäts- und Non-Overlap-Prüfung: bestanden.

## Idempotenz

Der identische begrenzte Lauf wurde zur Reproduzierbarkeitsprüfung zweimal
geschrieben. Alle vier Core-Artefakt-Hashes sind identisch.

Combined Core Fingerprint:

`268b3e42f19f99a56f991af4c1a7f8c8fb9235a1d526e75f9727850bee8c5b23`

## Ressourcen

Primärer Smoke:

- Wall time: 4.16s
- User CPU: 1.67s
- System CPU: 0.02s
- Maximum RSS: 73,008 KiB

DB-Zugriff war ein einzelner begrenzter, serverseitig aggregierter
Read-only-Trade-Querybereich von Warm-up bis Kandidatenende plus Horizont.
Kein Full-History-Scan.

## Bekannte Lücken

- Nur LLD_POOL; keine historische Full-OB-Destination-Forschung.
- Nur BTC-Smoke; DOGE wurde zur Lastbegrenzung nicht zusätzlich ausgeführt.
- Source-Completeness stützt sich auf persistiertes `coverage_status` und
  Randabdeckung; zusätzliche Gap-Kalender wären eine spätere Verbesserung.
- Phase 1 bewertet keine Vorhersagegüte, Kalibrierung oder Profitabilität.
- Die gebundene LLD-Konfiguration ist kanonische Chart-Konfiguration, nicht die
  eingefrorene 30m-Market-Profile-Strategie.

## Sicherheitsbestätigung

```text
LIVE_PROCESSES_CHANGED=false
DATABASE_WRITES_EXECUTED=false
BACKFILL_EXECUTED=false
DASHBOARD_CHANGED=false
COLLECTOR_CHANGED=false
TRADING_ACTIONS_EXECUTED=false
STRATEGY_CHANGED=false
COMMIT_CREATED=false
PUSH_EXECUTED=false
DESTRUCTIVE_ACTIONS_EXECUTED=false
```

Phase 2 wurde nicht begonnen.
