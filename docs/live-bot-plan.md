# Live-Bot-Plan

Ziel dieses Dokuments ist ein einfacher Plan für den späteren Live-Bot.
Der Bot soll **nicht** selbst neue Signale erfinden, sondern nur die Signale des Scanners ausführen.

## Kurz gesagt

Der Scanner liefert bereits sehr gute Daten:

- Entry-Preis
- Stop-Loss
- Take-Profit

Der Live-Bot soll dann nur noch:

1. das Signal prüfen
2. den Trade auf Bybit platzieren
3. Stop-Loss und Take-Profit anhängen
4. den Status sauber speichern
5. nach einem Neustart wieder wissen, was offen ist

## Was schon feststeht

- Die Short-Baseline ist eingefroren und wird nicht leichtfertig geändert.
- Der Scanner ist die Quelle für die Signale.
- Die aktuelle Arbeit soll nur den Live-Workflow vorbereiten.
- Die bestehenden Datenquellen bleiben:
  - Pools aus `dashboard/research_charts/service.py`
  - OB aus `ob_microstructure_breakout_bot/data/orderbook.py`
  - Delta aus `ob_microstructure_breakout_bot/data/trades.py`
  - Bars aus `ob_microstructure_breakout_bot/data/bars.py`

## Was der Live-Bot tun soll

Der Bot ist nur die Ausführungsschicht.

Er bekommt ein Signal und macht dann ungefähr das hier:

1. Signal annehmen
2. Signal prüfen
3. Order auf Bybit setzen
4. SL und TP setzen
5. Ergebnis speichern
6. Bei Fehlern oder Neustart den echten Zustand wieder einlesen

## Was der Live-Bot nicht tun soll

- keine neue Strategie bauen
- keine Baseline-Regeln verändern
- keine neuen Entry-Regeln erfinden
- keine neuen TP-/SL-Regeln definieren

## Einfacher Ablauf

1. Scanner findet ein Setup.
2. Scanner gibt Entry, SL und TP aus.
3. Live-Bot liest das Signal.
4. Live-Bot prüft, ob der Trade noch gültig ist.
5. Live-Bot platziert den Trade auf Bybit.
6. Live-Bot speichert alles in einem kleinen Status-Log.
7. Wenn der Bot neu startet, liest er den offenen Zustand wieder ein.

## Wichtige Design-Regel

Der Live-Bot soll **dünn** bleiben.
Das heißt:

- Signale kommen von außen
- Ausführung passiert im Bot
- Strategie bleibt im Scanner und im Backtester

So bleibt der Bot später einfacher zu warten.

## Getroffene Entscheidungen

Diese Punkte sind jetzt festgehalten:

- Der Einstieg soll per **Market-Order** laufen.
- Ein Signal bleibt gültig, bis es **SL**, **TP** oder den **Failure-Exit** erreicht.
- Paper-/Live-Trades haben **keinen Timeout**.
- **Failure-Exit (Short):** sobald der offene Trade live überwacht wird und gleichzeitig gilt: Preis ist wieder am Entry (±0.20 % darunter oder schon darüber, aber noch vor dem SL), darüber liegt ein dicker ACTIVE-Pool (Stärke ≥ 4, Abstand ≤ 1.5 %), OB ≥ 1.05 und Delta 10m ist positiv. Dann sofort zum aktuellen Preis schließen, nicht auf den SL warten.
- Es soll **immer nur ein Trade pro Symbol** offen sein.
- **SL** und **TP** kommen bereits im Signal mit.
- Wie Restart und Verbindungsabbruch genau behandelt werden, entscheiden wir noch.

## TP-Fallback: 5m Setup, 1m bei zu tiefen Pools

**Regel in einem Satz:**
5m bleibt Setup und SL-Basis; wenn die nächsten brauchbaren Lower-Pools in 5m zu weit nach unten liegen (Richtung Pump-Start / zu grobe Struktur), schalten wir für den TP auf 1m-Pools um und nehmen dort den nächsten ACTIVE lower Pool.

Ablauf:

1. Setup weiter auf **5m** (Rank 1/2, Watch, Touch, Flow, Reversal).
2. SL bleibt: **touched pool top + 0.2 %**.
3. Zuerst versuchen wir den TP aus **5m ACTIVE lower pools**.
4. Wenn der 5m-TP **fehlt** oder der Entry→TP-Abstand **zu groß** ist
   (Default: **> 3.0 %**), dann laden wir **1m ACTIVE lower pools**.
5. TP = nearest ACTIVE lower **1m**-Pool mit Entry→TP room **>= 0.8 %**.
6. Wenn auch 1m keinen brauchbaren TP liefert: **Skip**.

## Hedge-Bot als Vorlage

Als technische Basis schauen wir uns den vorhandenen Hedge-Bot in
`/home/telgenbuescher/projects/spread_recovery_hedge_short_dev` an.

Der wichtigste Kern liegt dort in:

- `fixed_cycle_hedge_bot/runtime.py`
- `fixed_cycle_hedge_bot/order_manager.py`
- `fixed_cycle_hedge_bot/fixed_cycle_strategy.py`
- `fixed_cycle_hedge_bot/models.py`
- `fixed_cycle_hedge_bot/post_flat_runtime_reset.py`

Wichtig dabei:

- Dort ist schon viel Bybit-Logik fertig.
- Es gibt bereits State, Recovery und Order-Verwaltung.
- Die Strategie selbst ist aber noch auf das alte Hedge-Modell gebaut.
- Wir wollen also eher die **Technik** übernehmen, nicht die **Strategie**.

## Wichtig fuer Precision und Coin-Unterschiede

Das ist der Teil, der fuer alle Coins besonders wichtig ist:

- Der Bot liest pro Symbol die Bybit-Regeln wie `tick_size` und `qty_step`.
- Preise werden auf die richtige Preisstufe normalisiert.
- Mengen werden auf die richtige Stufe gerundet.
- Es gibt Schutz gegen zu kleine Mengen und gegen zu grosse Mengen.
- Der Leverage wird pro Symbol gesetzt und gecached.

Die sauberere Stelle dafuer ist im neuen Hedge-Kern:

- `fixed_cycle_hedge_bot/order_manager.py`

Dort gibt es bereits:

- `fetch_instrument_info(...)`
- `get_cached_instrument_rules(...)`
- `normalize_price(...)`
- `normalize_qty(...)`
- `ensure_max_leverage(...)`

## Konkrete Stellen zum Wiederverwenden

Das sind die Teile, die wir fuer unseren simplen Market-Entry-Bot am ehesten
1:1 oder fast 1:1 uebernehmen koennen:

### Order- und Symbol-Regeln

Aus `fixed_cycle_hedge_bot/order_manager.py`:

- `ensure_hedge_mode(...)`
- `set_leverage(...)`
- `ensure_max_leverage(...)`
- `fetch_instrument_info(...)`
- `get_cached_instrument_rules(...)`
- `normalize_price(...)`
- `normalize_qty(...)`

Das ist die Stelle fuer:

- richtige Dezimalstellen
- richtige Mengenrundung
- Symbol-Regeln von Bybit
- maximalen Leverage pro Coin

### Bot-Lebenszyklus und Recovery

Aus `fixed_cycle_hedge_bot/runtime.py`:

- `submit_intent(...)`
- `_ensure_max_leverage_before_trading(...)`
- `_load_strategy_state(...)`
- `_recover_active_orders_from_exchange(...)`

Das ist die Stelle fuer:

- Order abschicken
- offenen Zustand speichern
- offene Orders nach Neustart wiederfinden
- vor dem Trading nochmal die Bybit-Regeln absichern

### Was wir daraus fuer unseren Bot ableiten

Unser Bot braucht am Ende im Kern nur:

1. Signal annehmen
2. Market-Order setzen
3. Mengen und Preise nach Bybit-Regeln normalisieren
4. SL und TP setzen
5. Status speichern
6. Nach Neustart offenen Zustand wiederherstellen

## Zielarchitektur fuer unseren Bot

Fuer unser Projekt reicht wahrscheinlich diese kleine Struktur:

### 1. Signal-Input

Ein kleines Modul nimmt das Signal vom Scanner an.
Das Signal enthaelt bereits:

- Symbol
- Richtung
- Markt-Einstieg
- TP
- SL
- ggf. Notiz oder Signal-ID

### 2. Execution-Service

Ein Dienst macht dann nur diese Schritte:

1. Signal validieren
2. Market-Order auf Bybit senden
3. Positionsgroesse korrekt runden
4. TP und SL setzen
5. Ergebnis speichern

### 3. Bybit-Adapter

Hier liegen die Exchange-Aufrufe.
Das sollte aus der Hedge-Basis uebernommen werden:

- `ensure_hedge_mode(...)`
- `ensure_max_leverage(...)`
- `normalize_price(...)`
- `normalize_qty(...)`
- `set_full_position_trading_stop(...)`

### 4. State / Recovery

Ein kleines State-File oder Ledger merkt sich:

- offenes Signal
- offene Position
- gesetzte TP/SL-Werte
- Bybit-Order-IDs
- Restart-Zeitpunkt

## Wie wir TP und SL setzen

Der Hedge-Bot hat dafuer schon eine gute, einfache Loesung:

- nach dem Entry wird `set_full_position_trading_stop(...)` benutzt
- dabei werden `takeProfit` und `stopLoss` direkt an die Position gehangen
- `tpTriggerBy` und `slTriggerBy` laufen ueber `MarkPrice`
- `tpslMode = "Full"` ist fuer unseren simplen Bot die richtige Variante

Das passt gut zu unserem Ablauf:

1. Market-Order ausfuehren
2. Position bestaetigen
3. TP/SL direkt auf die offene Position setzen
4. Keine komplizierten Teil-Exits, solange wir das nicht extra brauchen

Wenn wir spaeter doch feinere Exit-Logik wollen, koennen wir die Partial-Methoden
aus dem Hedge-Bot als naechste Stufe nehmen.

Das alte Vendor-Modul hat aehnliche Logik, ist aber eher ein Fallback und etwas grober.
Fuer unseren Live-Bot sollten wir deshalb eher die neue `fixed_cycle_hedge_bot`-Logik als Vorlage nehmen.

## Konkrete Dateistruktur

So koennte der Live-Bot im neuen Workspace aussehen:

```text
bot/
  __init__.py
  __main__.py
  config.py
  models.py
  state_store.py
  signal_ingest.py
  executor.py
  bybit_adapter.py
  recovery.py
  runner.py
```

### Was die Dateien tun

- `config.py`: Symbol-Regeln, Defaults und Laufzeit-Settings
- `models.py`: Signal-, Order- und State-Modelle
- `state_store.py`: lokales JSON oder kleines Ledger fuer offene Trades
- `signal_ingest.py`: Signal vom Scanner annehmen und validieren
- `executor.py`: Market-Entry, TP/SL setzen und Ergebnis behandeln
- `bybit_adapter.py`: Bybit-Aufrufe, Mengen/Preise runden, Exit-Calls
- `recovery.py`: offene Positionen und offene Orders nach Neustart abgleichen
- `runner.py`: Hauptablauf fuer den Live-Bot
- `__main__.py`: Startpunkt fuer `python -m bot`

### Was wir am Anfang wirklich brauchen

Fuer die erste Version reichen wahrscheinlich schon:

- `models.py`
- `state_store.py`
- `signal_ingest.py`
- `executor.py`
- `bybit_adapter.py`
- `runner.py`

Alles andere kann spaeter folgen, falls wir es brauchen.

## Live-Datenfluss

So laeuft der erste Echtzeit-Flow am besten:

```text
5m Candles
   -> Pool-Liquidity-Erkennung
   -> Pool naehert sich (ca. 0.8 % Vorwarnung)
   -> OB + Delta werden aktiv beobachtet
   -> Touch am Pool
   -> Entry-Pruefung
   -> Signal mit Entry / SL / TP
   -> Forward-Test-Logging oder spaeter echte Bybit-Order
```

### Was in welchem Schritt passiert

- **5m Candles**: neue Bars kommen rein oder werden aktualisiert.
- **Pool-Liquidity-Erkennung**: der Code sucht die aktiven Pools und Cluster.
- **Vorwarnung bei ca. 0.8 %**: der Coin kommt auf die Watchlist.
- **OB + Delta**: jetzt wird genau geprueft, ob das Setup stark genug ist.
- **Touch**: der Preis erreicht den Pool.
- **Entry-Pruefung**: nur wenn das Signal passt, wird der Trade freigegeben.
- **Signal mit Entry / SL / TP**: der Live-Bot bekommt alles fertig vorbereitet.
- **Forward-Test oder Live**: zuerst nur loggen, spaeter dann wirklich senden.

## Forward-Test zuerst

Vor dem echten Live-Start machen wir zuerst einen **Forward Test**.

Das bedeutet:

- Scanner laeuft live
- Signale werden normal erzeugt
- Entry, SL und TP werden nur geloggt
- es wird **keine** Order auf Bybit gesendet
- wir lassen das 1 bis 2 Tage laufen

### Was wir dabei pruefen

- Kommen die Signale rechtzeitig?
- Stimmen Preis, SL und TP?
- Passt die Precision fuer alle Coins?
- Bleiben die Daten stabil?
- Sieht das Timing im Echtbetrieb noch gut aus?

### Was der Forward-Test-Ordner dafuer sammelt

Der neue Ordner `bot/forward_test/` ist fuer genau diese Phase gedacht.
Dort liegen jetzt:

- `scanner.py` / `runner.py` — Dry-Run-Scanner
- `logs/events.jsonl` — watch / touch / skip / idle / error
- `logs/signals.jsonl` — fertige Dry-Signale (Entry / SL / TP)
- `state/seen.json` — Dedup gegen Log-Spam

Start:

```bash
python -m bot.forward_test --once
python -m bot.forward_test
```

Wichtig: `place_order=False` — es werden **keine** Bybit-Orders gesendet.

Wenn der Forward Test sauber laeuft, koennen wir danach erst den echten Live-Modus freischalten.

### Empfohlenes Start-Universum (ohne BTC)

Fuer den ersten Testlauf nehmen wir diese Coins:

- `ETHUSDT`
- `SOLUSDT`
- `XRPUSDT`
- `DOGEUSDT`
- `BNBUSDT`
- `SUIUSDT`
- `ADAUSDT`
- `AVAXUSDT`
- `LINKUSDT`

Warum genau diese Auswahl:

- sehr liquide
- meist saubere OB-/Delta-Signale
- gute Mischung aus grossen und aktiven Coins
- fuer einen ersten Forward Test noch klein genug, um sauber zu beobachten
- **`BTCUSDT` bewusst raus**, weil wir dort aktuell keine OB-Live-Daten einsammeln

Wenn wir mehr Signal-Frequenz wollen, koennen wir spaeter `ADAUSDT` oder `LINKUSDT`
auch gegen `TONUSDT` oder `APTUSDT` tauschen.

## Noch offen

Diese Punkte müssen wir als Nächstes sauber entscheiden:

- Was passiert bei Neustart oder Verbindungsabbruch?

## Empfohlene nächste Richtung

Am besten bauen wir jetzt zuerst den **kleinen Ausführungs-Rahmen**:

- Signal-Form festlegen
- Order-Ledger anlegen
- Bybit-Placement kapseln
- Reconnect- und Restart-Logik definieren

Danach kann der Live-Bot Schritt für Schritt an den Scanner angebunden werden.
