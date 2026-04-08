# Fixed Cycle Hedge Bot

Dieser Ordner ist die isolierte Arbeitskopie der modularen Hedge-Runtime fuer die neue geplante Fixed-Cycle-Hedge-Strategie.

## Ziel

- bestehende Runtime-Basis unveraendert als Referenz behalten
- neue Strategie in einem getrennten Ordner entwickeln
- vorbereitete Downside-Zyklen und Upward-Exit-Logik auf derselben Runtime nutzen

## Enthaltene Dateien

- `runtime.py`: gemeinsame Runtime mit REST, WebSocket, Reconcile, Recovery und Audit-Logging
- `order_manager.py`: Bybit REST-Layer
- `position_manager.py`: Positions-Synchronisierung
- `base.py`: Strategie-Interface und Strategy-Context
- `models.py`: Runtime-, Snapshot- und Intent-Modelle
- `registry.py`: registrierte Strategien inkl. `fixed_cycle`
- `runner.py`: gemeinsamer CLI-Runner
- `fixed_cycle_strategy.py`: neue Hedge-Zyklus-Strategie
- `config/fixed_cycle_config.json`: Beispiel-Config fuer die neue Strategie
- `run_fixed_cycle.py`: direkter Wrapper fuer die neue Strategie

## Aktueller Stand der Fixed-Cycle-Strategie

Bereits umgesetzt:

- Start-Hedge mit Long `100%` und Short `50%`
- State-Machine im `strategy_state`
- vorbereitete Downside-Orders pro Zyklus
- Break-Even-Berechnung aus Reststruktur und realisiertem PnL
- TP-Ableitung aus Break-Even plus konfigurierbarem Puffer
- Hard-Stop-Modus ab konfigurierbarem Zyklus
- Rebuild der Struktur nach Fills mit REST-Refresh als Truth-Quelle
- Runner-Integration ueber `--strategy fixed_cycle`

## Startbeispiele

- `python -m fixed_cycle_hedge_bot --strategy fixed_cycle`
- `python -m fixed_cycle_hedge_bot.runner --strategy fixed_cycle --strategy-config-file fixed_cycle_hedge_bot/config/fixed_cycle_config.json`
- `python -m fixed_cycle_hedge_bot.run_fixed_cycle`

Optional:

- `--symbol BTCUSDT`
- `--strategy-state-file logs/fixed_cycle_state.json`
- `--audit-log-file logs/fixed_cycle_audit.jsonl`

## Config

Die Strategie laedt optional eine JSON-Datei ueber:

- `--strategy-config-file fixed_cycle_hedge_bot/config/fixed_cycle_config.json`

Wichtige Default-Felder:

- `base_notional_usdt`
- `hedge_ratio_short`
- `reduction_pct_per_fill`
- `long_fill_distance_pct`
- `short_fill_distance_pct`
- `tp_buffer_pct`
- `tp_profit_target_pct`
- `hard_stop_cycle`
- `hard_stop_pct`
- `max_cycles`

## Tests

Die wichtigste neue Testdatei ist:

- `test_fixed_cycle_strategy.py`

Testlauf:

- `python -m pytest fixed_cycle_hedge_bot -q`
