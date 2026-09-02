# Phase 2 Recovery-/Code-Preflight

Status: `PASS_FOR_COVERAGE_AUDIT`; noch kein Phase-2-DDL/DML ausgeführt.

## Repository

- Branch: `feature/btc-doge-research-db`
- HEAD: `48bf56fbff1e82abee0c8ff09a95a1701df10965`
- Tracked Worktree: sauber
- Vorbestehend untracked: Phase-1B-Code/-Tests/-Ergebnisse sowie zahlreiche fachfremde
  Dateien und Ergebnisordner; nichts davon wurde gelöscht oder verschoben.
- Vorbestehender Phase-2-Versuch: keiner gefunden.

## Prozesse und Speicher

- Kein laufender BTC-/DOGE-Research-Backfill und kein pytest-Prozess.
- PID 3946369: OB200 `raw-archive-only` BTC/DOGE.
- PID 147111: OI-/Liquidations-Collector.
- PID 1661773: Live-Service mit Public Trades.
- Freier Speicher: 372 GiB auf `/dev/sda2` (58 % belegt).
- Die Prozesse werden nicht verändert oder neu gestartet.

## ClickHouse

- Erreichbar, Version `26.7.1.1315`.
- `btc_doge_research` existiert bereits aus Phase 1.
- `market_research` existiert nicht und wird nicht angelegt.
- 13 kanonische/Phase-1-Tabellen plus zwei erhaltene
  `*_invalid_timezone_v0`-Tabellen existieren.
- Bereits geschrieben: zwei vollständige Phase-1-Pilotbatches,
  85.013 Trades, 64 Liquidationen, 56.414 OB200-Events und 6.300 OB-1s-Rows.
- Alle im Phase-1-DDL definierten Tabellen existieren real; keine Phase-1-Tabelle existiert
  nur als Code.
- Die neuen Phase-2-Logikbereiche (Producer-Lineage, Schema-Versionen, 100/500-ms
  Trade-Buckets, Profile, OI-Observations und 1s Integer-Tick-OB) existieren noch nicht.

## Recovery-Entscheidung

Der eingefrorene Contract setzt `TARGET_DATABASE=btc_doge_research`. Daher wird der
abweichende Namensvorschlag `market_research` nicht verwendet: eine zweite Datenbank wäre
ein konkurrierendes Schema und widerspräche dem Recovery-Hinweis. Bestehende Phase-1-Tabellen,
Transformer, Source-Reader, Fingerprints und Batch-Guards werden wiederverwendet.

## Zugang und Rechte

Die Verbindung wird über `dashboard/research_charts/clickhouse_config.py` geladen; keine
Credentials werden kopiert oder in Berichte geschrieben. Der verwendete ClickHouse-Account
besitzt technisch globale Schreibrechte, also nicht ausschließlich Rechte auf die
Research-Datenbank. Phase-1-Code begrenzt Writes fail-closed auf `btc_doge_research`;
Phase 2 muss denselben Guard für jedes DDL/DML verwenden. Writes außerhalb dieses Ziels
sind verboten.

## Vorläufige Tages-Coverage

- Candles: 1.440/1.440 je Symbol an 25., 26. und 27. August.
- Public Trades: deduplizierte Tagesbestände je Symbol vorhanden.
- OI: 25. August hat nur 17.279 statt 17.280 Beobachtungen je Symbol; daher nicht erster
  zweifelsfrei vollständiger Tag.
- 26. August hat 17.280/17.280 OI-Beobachtungen je Symbol und ist der führende Kandidat.
- Liquidationen sind ereignisbasiert; Tagesränder ohne Ereignis sind kein Coverage-Gap.
- Die 48 OB200-Stundensegmente und ihre `data.u`-Kontinuität müssen vor DDL/DML noch
  vollständig geprüft werden.

## Stop-Gate

DDL/DML bleibt blockiert, bis der gemeinsame Tag einschließlich aller Raw-Segmente,
Source-Fingerprints, Transition Contract und queue_full-Grenze zweifelsfrei bestanden ist.
