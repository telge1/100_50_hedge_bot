# Phase 1C — Nachgewiesene Ursache vor Implementierung

Stand vor jeder Phase-1C-Codeänderung:

- Branch: `feature/btc-doge-research-db`
- HEAD: `9ab70ee955ff5a47e2c42ecaa6d727df581c2d34`
- fachlicher YAML-Contract:
  `66ab82f4ac5e1b416594d17fa50116ed8a6daadae01e9fe29a29586072a91a51`
- `contract.py`:
  `1e2449c13a3ba54e0f9c61ec5a90d37c91a297b1eb55a67cf1b80c7c56af4d32`
- generiertes `contract.json`:
  `2aad3b9bc2ba949f72f16a11b04ad53d7428d94bdec13ca55497aa30b450f6c3`

## Ursache

Das Zwei-Stunden-Limit wird ausschließlich in
`BuildConfig.__post_init__()` in `builder.py` erzwungen. Dort wird die
Differenz `end-start` gegen den operativen Guard
`MAX_CANDIDATE_WINDOW_SECONDS=7200` geprüft. Die CLI besitzt bislang keine
explizite Freigabe für einen größeren Wert.

Der Guard ist nicht Teil von Zielwahl, Eligibility, Touch, Outcome oder
Debounce. Er begrenzt nur die zulässige Größe eines Auftrags. Der gespeicherte
Contract dokumentiert den bisherigen Safety-Default, nicht eine fachliche
Zwei-Stunden-Definition einer Episode.

## Bestehender Daten- und Zustandsfluss

`build_episodes()` lädt vor der Kandidatenschleife mit einem einzigen
`load_trade_seconds()` den kompletten Bereich
`[start-5m, end+horizon)`. ClickHouse aggregiert `research_public_trades
FINAL` serverseitig zu 1s-OHLC; anschließend werden sämtliche Sekundenbucket
als Python-Dictionary im RAM gehalten.

Kandidaten werden chronologisch im 60s-Raster verarbeitet. Nur wenn kein
aktiver Vorgänger blockiert, ruft der Builder den kanonischen LLD-Provider
für genau dieses T0 auf. `active_until` liegt lokal in einem einzigen
`build_episodes()`-Aufruf und schützt dort korrekt vor überlappenden
Episoden. Ein nachträglicher Merge unabhängiger Tagesläufe hätte diesen
globalen Zustand nicht und wäre daher unsicher.

## Skalierung

- Trade-Querygröße und `TradeSecondSeries.buckets` wachsen linear mit dem
  gesamten Zeitraum. Ein Sieben-Tage-Lauf würde alle 1s-Buckets gleichzeitig
  materialisieren.
- Episode- und Ausschlusslisten wachsen mit den Kandidatenminuten, sind aber
  wesentlich kleiner als die Trade-Buckets.
- Kanonische LLD-Snapshots werden sequenziell für nicht blockierte Kandidaten
  berechnet; deren CPU-/Quellzugriff wächst mit deren Anzahl.
- Es existiert kein Parallelpfad und kein persistierter Segment-State.

## Technische Folgerung

Ein einziger logisch kontinuierlicher Lauf ist sicherer als ein CSV-Merge,
weil `active_until`, Reihenfolge und Episode-IDs global bleiben. Für
begrenzten RAM muss nur die Tradequelle intern in chronologische,
überlappende Abfragefenster zerlegt werden. Jeder interne Chunk benötigt
5 Minuten Warm-up und den vollständigen Horizon-Lookahead. Der aktive
Episode-State darf zwischen Chunks nicht zurückgesetzt werden.

Die Phase-1C-Lösung wird deshalb:

1. den 2h-Default unverändert lassen,
2. längere Aufträge nur mit explizitem Flag und endlichem Maximalwert
   zulassen,
3. Trades in höchstens 2h großen Kandidatenchunks laden,
4. `active_until`, Outputreihenfolge und globale Source-Provenance über alle
   Chunks tragen,
5. keine unabhängigen CSV-Dateien zusammenführen und keine fachliche
   Contract-Datei ändern.
