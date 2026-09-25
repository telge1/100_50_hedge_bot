# Forward Test Mode (Dry Run)

Dieser Ordner ist fuer den geplanten Dry-Run des Live-Bots gedacht.

## Ziel

- Scanner live laufen lassen
- Signale erfassen
- **keine** echten Bybit-Orders senden
- Verhalten 1 bis 2 Tage beobachten

## Start

Aus dem Workspace-Root:

```bash
# Einmaliger Scan der Start-Coins (ohne BTC)
python -m bot.forward_test --once

# Dauerhaft alle 30s scannen
python -m bot.forward_test

# Eigenes Universe
python -m bot.forward_test --once --symbols ETHUSDT,DOGEUSDT,SOLUSDT
```

Hinweis: `BTCUSDT` ist standardmaessig **nicht** dabei, weil dort keine OB-Live-Daten eingesammelt werden.

## Status CLI

Zeigt offene + geschlossene Trades als Markdown-Tabelle auf einen Blick:

```bash
cd /home/telgenbuescher/projects/pools+ob+delta_bot

python -m bot.forward_test.cli          # alles (Tabellen)
python -m bot.forward_test.cli open
python -m bot.forward_test.cli closed
python -m bot.forward_test.cli summary
python -m bot.forward_test.cli paths
```

## Logs

- `bot/forward_test/logs/events.jsonl` — watch / touch / skip / idle / error
- `bot/forward_test/logs/signals.jsonl` — fertige Dry-Signale (Entry/SL/TP)
- `bot/forward_test/logs/trades.jsonl` — geschlossene Paper-Trades mit PnL %
- `bot/forward_test/state/open_trades.json` — aktuell offene Paper-Trades
- `bot/forward_test/state/pnl_summary.json` — Gesamt-PnL / Wins / Losses
- `bot/forward_test/state/seen.json` — Dedup-Keys gegen Spam
- `bot/forward_test/logs/runner.out` — Live-Konsolenausgabe des Dauerlaufs

## Paper-Trade Tracking

Nach einem Dry-Signal wird ein Paper-Trade geoeffnet und weiterbeobachtet:

- SL / TP werden an geschlossenen 5m-Bars geprüft
- Same-Bar SL+TP: SL zuerst (wie Backtester)
- **Failure-Exit (nur Short, live):** jeder offene Trade wird bei jedem Poll geprüft. Schließen zum aktuellen Preis, wenn alle Punkte zusammen zutreffen: Preis wieder am Entry (0.20 % darunter oder schon darüber, aber vor dem SL), dicker ACTIVE Upper-Pool direkt darüber (Stärke ≥ 4, max. 1.5 % über Entry), OB ≥ 1.05 und Delta 10m positiv. Kein Bar-Zähler, kein Lookahead.
- Exit wird mit `pnl_pct` geloggt (`tp` / `sl` / `failure`)
- nur **ein** offener Paper-Trade pro Symbol
- **kein Timeout**
- keine echten Bybit-Orders

## Regeln (frozen short)

- Rank 1 + Rank 2 only
- Watch ab ca. 0.8% vor Pool
- Flow: OB >= 1.05 und Delta >= 100k
- Entry nach confirmed reversal candle
- **Regime-Filter vor dem Short:** 1h und 4h, EMA9 und EMA20 beide über EMA59 = bullish. Ist 1h oder 4h bullish, wird das Short-Signal ignoriert. Bearish nur wenn beide Timeframes bearish sind. Neutral (gemischt) darf noch shorten.
- SL = Pool-Top + 0.2%
- TP = nearest ACTIVE lower pool mit Entry->TP room >= 0.8%
- **TP-Fallback:** wenn 5m-Lower-Pools zu weit weg sind (> 3.0% Entry→TP),
  dann TP aus **1m ACTIVE lower pools** wählen

## Hinweis zu OB

Der Dry-Run liest Live-OB aus dem Collector-Archive:

- `orderbook_analyse/data/orderbook_raw_shadow/ob1000_v1/`

Das ist der laufende `raw-archive-only` Collector (inkl. offene `.zst.tmp` Segmente).
Der alte Backtester-Pfad `full_ob_v1` wird hier **nicht** benutzt.
