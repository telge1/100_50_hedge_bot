# Abschlussbericht

1. **Gesamtverdict:** `BTC_FLOW_DATA_PARTIALLY_AVAILABLE`
2. **Public Trades:** **ja** — `orderbook_analysis.public_trades_canonical` (389723 Rows im Analysefenster)
3. **OI:** **nein** für Sep-4 — letzte Rows **2026-09-01T16:46:23Z** in `open_interest_events`
4. **Liquidationen:** **nein** für Sep-4 — letzte Rows **2026-09-01T16:36:59Z** in `all_liquidations`
5. **Ursache „nicht verfügbar“:** Trades = **Analyse-Query-Bug** (falsche Tabelle + Pauschalvermeidung von `orderbook_analysis`); OI/Liq = **Collector-/Writer-Gap** trotz laufendem PID 147111
6. **UTC/Symbol/Schema:** `TIMEZONE_SHIFT_DETECTED=false`; `SYMBOL_MAPPING_OK=true`; Canonical-Schema ok, Prior-Analyse falsch (`event_time` / research-Spiegel)
7. **Coverage/Lücken:** Trades max Gap **7s**; OI/Liq Gap seit **~01.09. 16:46 UTC** bis jetzt
8. **Geänderte/neue Dateien (Offline):** `adapter/canonical_flow_reader.py`, `tests/test_sep4_canonical_flow_windows.py`, recomputed CSVs/Reports unter diesem Ordner — alter Crash-REPORT unangetastet
9. **Ergänzte Fakten:** `signal_flow_facts.csv`, `crash_flow_facts.csv` (Trades); OI/Liq weiterhin `NOT_AVAILABLE_COLLECTOR_GAP`
10. **PIDs:** Collector **1692334**, OI **147111** unverändert
11. **DB-Writes = 0**
12. **Restart = nein**
13. **TRADE_DIRECTION=NOT_EVALUATED**
