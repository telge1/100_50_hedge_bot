# Pool State Machine

Erkennt für einen Coin, in welcher Phase das wiederkehrende Pool-Muster gerade ist.

## Aufbau

- `rules.md` beschreibt das Muster.
- `coin_profile.py` hält die Schwellen eines Coins.
- `state_machine.py` setzt aus den erkannten Teilen die Phase.
- `scanner.py` ist der Startlauf über die letzten Wochen oder Monate.

## Phasen

- `upper_build`: Preis arbeitet sich durch obere Pools und Zwischenstufen.
- `stair_down`: der Fall holt die neu entstandenen unteren Pools stufenweise.
- `exhausted`: der letzte alte untere Pool ist erreicht.
- `neutral`: keine klare Phase.

## Start

Der Scanner liest zuerst den Kontext der letzten Monate, danach steht die Phase. Eine einzelne Kerze schaltet sie nicht um.

```bash
PYTHONPATH=. python -m pool_state_maschine.scanner
```
