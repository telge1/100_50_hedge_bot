# OI/Liquidation + Trade-Impact + L2 Discovery

## Zweck

Die Discovery-Phase beschreibt kausale Minutenmerkmale für:

1. Preisbewegung, fallendes Open Interest und richtungskorrekte Liquidationen,
2. aggressives Public-Trade-Notional und Preiswirkung pro Notional,
3. echte L2-Stabilisierung ohne `carried_forward`-Dynamik.

Sie erzeugt **keine Strategy-Lab-Trades**, optimiert keine Schwellenwerte und
behauptet keine Profitabilität. Eine Strategy-YAML, Plugin-State-Machine und ein
Backtest-Adapter folgen erst nach Auswertung dieser Rohmerkmale.

## Eingefrorenes Forschungsfenster

```text
[2026-08-20T12:33:00Z, 2026-08-24T06:35:00Z)
51 Coins aus config/universe_tradeable_51.json
5.402 Minuten
```

Die technische Coverage dieses Fensters wurde außerhalb des Scripts manuell
geprüft. Das Manifest hält diese Voraussetzung ausdrücklich fest; insbesondere
ist eine leere Liquidationsminute nur aufgrund dieses externen Feed-Audits als
„kein Ereignis“ interpretierbar.

## Datenquellen

- `signal_generator.candles_1m`
- `orderbook_analysis.public_trades_canonical`
- `orderbook_analysis.open_interest_5s`
- `orderbook_analysis.all_liquidations`
- `orderbook_analysis.orderbook_features_1s_v2`
  (`parser_version='ob200_v3'`, `depth=200`)

Predictor-Queries verwenden `[start, end)`. Candles werden ausschließlich für
den separaten Outcome-Sidecar um den explizit gewählten Label-Horizont
verlängert.

## Kausalität und Datenqualität

- Minute `t` wird erst am Close `t+1m` verfügbar.
- OI-Delta verwendet nur die aktuelle und vorherige gültige Minute.
- Impact-Compression vergleicht aktuelle und unmittelbar vorherige
  abgeschlossene Minute.
- Future-Outcomes stehen nur in `labels_sidecar.csv`.
- Fehlende Candles/OI/L2, ungültiges OI, unvollständige L2-Minuten oder
  `is_valid=0` erzeugen `technical_gap=true` und löschen Vergleichshistorie.
- `carried_forward` darf nie OFI, Add/Remove oder Refill bestätigen.
- L2-Dynamik wird in SQL nur über genuine Sekunden aggregiert:
  `is_valid=1` und kein `carried_forward` in `quality_flags`.
- Eine Minute ohne genuine Sekunde löscht die L2-Vergleichshistorie. Die erste
  nachfolgende genuine Minute kann deshalb noch keine Recovery bestätigen.

## Beschreibende Beobachtungen

`directional_flush_observed` ist keine Handelsfreigabe. Es bedeutet nur:

- adverse Preisbewegung in der betrachteten Minute,
- OI ist gegenüber der vorherigen gültigen Minute gefallen,
- mindestens eine passende Liquidation wurde beobachtet,
- passendes aggressives Notional ist positiv,
- keine technische Lücke in der aktuellen Minute.

Long verwendet `LIQUIDATED_LONG` und aggressives Sell-Notional. Short verwendet
`LIQUIDATED_SHORT` und aggressives Buy-Notional.

`impact_compression_observed` verlangt zusätzlich:

- aggressives Notional mindestens so hoch wie in der vorherigen Minute,
- geringere adverse Preisbewegung pro aggressivem Notional.

Das ist eine deskriptive Minutenrelation, kein optimierter Grenzwert.

`l2_recovery_observed` vergleicht ausschließlich zwei unmittelbar
aufeinanderfolgende gültige Minuten mit jeweils mindestens einer genuine
Sekunde:

```text
directional_depth_change > 0
OR directional_imbalance_change > 0
OR directional_net_add_change > 0
```

Dabei gilt:

- LONG: Bid/Support-Tiefe und `bid_qty_added - bid_qty_removed`
- SHORT: Ask/Resistance-Tiefe und `ask_qty_added - ask_qty_removed`
- `directional_net_add_change = current_directional_net_add
  - previous_directional_net_add`

Eine absolute positive Additions- oder Net-Add-Summe bestätigt niemals allein
eine Recovery. Jeder OR-Term ist genuine-only, richtungskorrekt,
vorgängerbasiert und gap-sicher. Die Relation bleibt deskriptiv und ist noch
keine finale Signaldefinition.

Der spätere Price-Reclaim wird in dieser F1-Phase bewusst noch **nicht**
bestätigt: `previous_close`, OHLC und `close_vs_previous_close_pct` werden als
kausale Rohfelder exportiert. Erst die F2-State-Machine darf das beim Flush
gespeicherte Vor-Flush-Level gegen einen späteren abgeschlossenen Close prüfen.

## Outputs

```text
discovery_manifest.json
quality_by_symbol.csv
minute_features.csv
flush_candidates.csv
distribution_summary.json
labels_sidecar.csv
```

- `minute_features.csv`: zwei Zeilen je Minute (LONG/SHORT), nur kausale Felder.
- `flush_candidates.csv`: rein beschreibende Flush-Beobachtungen.
- `labels_sidecar.csv`: Entry am nächsten 1m-Open sowie MFE/MAE/Forward-Return.
- `distribution_summary.json`: Min/Quartile/Median/Max, gepoolt und je Coin.

Es gibt keine automatische Best-Schwelle und keinen PnL-Report.

`discovery_manifest.json` enthält denselben zentralen Datenvertrag, den auch
die Query verwendet: Orderbook-Tabelle, `parser_version='ob200_v3'`,
`depth=200`, Genuine-/carried-forward-Regeln, vollständige-Minute-Bedingung
sowie L2-, Liquidations- und Aggressorseite für LONG/SHORT.

## Manueller Lauf

Der Label-Horizont ist absichtlich **pflichtig**. Vor dem Lauf muss ein
Research-Horizont festgelegt werden; das Script erfindet keinen Default.

```bash
mkdir -p logs

nohup env PYTHONPATH=src \
  python scripts/run_oi_liq_impact_l2_discovery.py \
  --universe config/universe_tradeable_51.json \
  --start 2026-08-20T12:33:00Z \
  --end 2026-08-24T06:35:00Z \
  --label-horizon-minutes 60 \
  --output-dir results/oi_liq_impact_l2/discovery_smoke_btc_60m_v2 \
  --symbol BTCUSDT \
  > logs/oi_liq_impact_l2_discovery.log 2>&1 &
```

Für einen manuellen Ein-Coin-Smoke kann `--symbol BTCUSDT` ergänzt werden.
Cursor startet weder Smoke noch vollständigen Lauf.

Erfolg:

```text
OI_LIQ_IMPACT_L2_DISCOVERY_COMPLETE
```

Fehler:

```text
OI_LIQ_IMPACT_L2_DISCOVERY_BLOCKED
```

Ergebnisse und Logs werden nicht committed.
