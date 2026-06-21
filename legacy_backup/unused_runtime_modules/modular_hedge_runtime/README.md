# Modular Hedge Runtime

Dieser Ordner enthaelt die neue gemeinsame Bot-Basis fuer kuenftige Hedge-Bots.

Ziel:
- eine wiederverwendbare Runtime fuer REST + WebSocket
- austauschbare Strategien
- detailliertes Audit-Logging mit Formeln, Inputs und Ergebnissen

Enthaltene Dateien:
- `models.py`: gemeinsame Runtime- und Event-Modelle
- `audit_logger.py`: JSONL-Audit-Logging
- `base.py`: gemeinsames Strategie-Interface
- `order_manager.py`: lokale Bybit REST-Order- und Symbol-Anbindung
- `position_manager.py`: lokale Positions-Helferklasse
- `registry.py`: zentrale Registrierung aller verfuegbaren Strategien
- `runtime.py`: generische Hedge-Runtime
- `runner.py`: gemeinsamer CLI-Runner fuer alle registrierten Strategien
- `__main__.py`: Start per `python -m modular_hedge_runtime`
- `dynamic_breakeven_strategy.py`: erster neuer Fill-getriebener Bot
- `basket_exit_strategy.py`: zweite Beispiel-Strategie fuer kompletten Basket-Exit
- `run_dynamic_breakeven.py`: Kompatibilitaets-Wrapper fuer die erste Strategie

Wichtige Eigenschaften:
- REST fuer Startup, Snapshot, Reconcile und Order-Ausfuehrung
- WebSocket fuer schnelle Fill- und Order-Events
- strukturierte Traces pro Berechnung
- Replace-Mechanik fuer gegnerische Exit-Orders
- zentrale Strategie-Auswahl ueber einen gemeinsamen Runner

Startbeispiele:
- `python -m modular_hedge_runtime --strategy dynamic_breakeven`
- `python -m modular_hedge_runtime --strategy basket_exit`
- `python -m modular_hedge_runtime.runner --strategy dynamic_breakeven --symbol BTCUSDT`
- `python -m modular_hedge_runtime --strategy dynamic_breakeven --strategy-state-file logs/dynamic_state.json`

Naechste sinnvolle Schritte:
- zweiten Bot auf dieselbe Basis haengen
- Persistenz fuer Strategy-State erweitern
- Cancel/Replace und Orderstatus-Handling weiter absichern
