# Event ↔ Orderbook Coverage Audit

Stand: 2026-08-08. Keine Trading-Regel, kein Live-Code. Nur Eventliste + Coverage.

## Quellen (bekannte Events)

| Quelle | Inhalt |
|---|---|
| `orderbook_analyse/.../apt_1h_4h_protected_level_event_inventory/audit_queue.csv` | APT 1h/4h eligible breaks |
| `.../c3_protected_low_historical_event_catalog/event_decisions.csv` | APT+DOGE protected-low breaks |
| `.../c3_protected_high_historical_event_catalog/event_decisions.csv` | APT+DOGE protected-high breaks |

## Historical Bybit OB (validierter Replayer)

Tage unter `data/bybit_historical_orderbook/`:

- **APTUSDT:** 2025-12-29, 2025-12-30, 2026-01-06, 2026-01-18, 2026-05-12, 2026-05-23
- **DOGEUSDT:** 2026-01-06, 2026-01-15, 2026-02-20, 2026-02-28

## ClickHouse OB (Recorder)

- **APTUSDT:** 2026-07-26 08:45 → 2026-08-07 07:42 UTC (+ Trades im gleichen Fenster)
- **DOGEUSDT:** 2026-07-26 22:11 → 2026-08-08 ~08:42 UTC (+ Trades)

## Ergebnis Coverage (±5m um Event-Timestamp)

| Coverage | n | Bedeutung |
|---|---:|---|
| `HISTORICAL_OB_FULL` | **0** | Kein bekanntes Break/Reclaim-Event liegt auf den Dowload-Tagen |
| `CLICKHOUSE_OB_FULL` | **54** | ±5m Fenster vollständig in CH `orderbook_deltas` (+ Trades) |
| `NO_OB_COVERAGE` | **3** | Break vor CH-Start (APT audit_queue, early Jul 24–25) |

Auf den Historical-OB-Tagen existieren in der APT-Inventory nur **NO_EVENT**-Levels (confirm/active, aber kein Touch/Break) → ungeeignet für Break-vs-Reclaim-Diskriminierung.

## Blocker für den geplanten Schritt

Der validierte **Historical-OB-Replayer** und die **bekannten Break/Reclaim-Events** haben **keine gemeinsame Zeitachse**.

Deshalb wurden **noch keine** −5m…+5m Feature-Fenster rekonstruiert und Fragen 2–4 (Feature-Trennung / Lookahead / Hedge-Signal) sind **noch nicht beantwortbar**.

## Nächste Optionen (ohne neue Live-Logik)

1. **Historical OB für Juli-Event-Tage nachziehen** (Bybit Download) → dann Historical-Replayer + bekannte Events.
2. **Bestehenden CH-Replay** auf die 54 CH-gedeckten Events anwenden (kein Historical-Replayer; vorhandene Infrastruktur in `orderbook_analyse`).
3. **Auf Historical-OB-Tagen neue Break/Reclaim-Events** aus Candles/C3.4B ableiten → dann Historical-Replayer, aber nicht mehr „bereits bekannte“ Juli-Events.

Artifacts: `event_coverage.csv`, `summary.json`.
