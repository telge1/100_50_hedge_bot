# Abschlussbericht

1. **Verdict:** `BTC_FULL_OB_ANALYSIS_PARTIALLY_OBSERVABLE`
2. **Signale:** A Parent UPPER `11:27:35Z`; B Nested LOWER `11:30:32Z`; C Nested UPPER `12:06:34Z` (Kennzahlen signal_id-getrennt)
3. **Parity:** source=26487 = db=26487; parse_rejects=0; logical_duplicates=0; checkpoint_hash_ok=true; replay_ok=true; book_crossed_count=1 (ein transienter Cross, sonst replaybar)
4. **Coverage/Gaps:** seg0+cont_001+cont_002 finalisiert (bis 12:57); cont_003 `.tmp` unangetastet. cont_001: 7 Resync-/Epoch-Grenzen → fail-closed über Epochen. Nested UPPER (12:06) in Resync-Region.
5. **Ask-Walls:** Vor Crash praktisch nur 1 getrackte große Ask (~81643 @ 12:29:47, ~13s Lead). Starke Ask-Walls (≥100 BTC) überwiegend **während** des Abverkaufs (nach 12:30).
6. **Bid-Veränderungen:** Kumulierte Bid-Tiefe 50 bps sank 12:00–12:10 → 12:25–12:30 (~1139 → ~963 BTC), aber Ask-Tiefe sank parallel; Imbalance blieb nahe 0. Keine großen (≥20) Bid-Reduktionen in den letzten 5 Min vor Crash. Viele frühere `UNMATCHED_L2_CHANGE`-Events sind nicht als klarer Withdrawal beweisbar.
7. **Trades/OI/Liq:** Sep-4 in `btc_doge_research` fehlend (Daten bis ≤2026-08-31); Bybit-Dayfile 404; OI schreibt in unloadbares `orderbook_analysis` → **NOT_AVAILABLE**
8. **Erste objektive bearish Veränderung:** Kein persistentes Ask-heavy-Imbalance-Signal vor 12:30. Nächste harte Beobachtung: Ask-Wall 81643 + Preisbruch ab **12:30:00.472Z** (nach lokalem Hoch 81325.35 @ 12:29:57).
9. **Lead-Time:** Wesentliche Ask-Verteidigung / Crash-Dynamik ≈ **0–13 Sekunden** vor erkanntem Crash-Start — nicht Minuten.
10. **Vorab vs gleichzeitig:** Leichte bilaterale Liquiditätsabnahme vorab (schwach, nicht eindeutig bearish). Klare bearish Full-OB-Wall-Dynamik **größtenteils gleichzeitig mit dem Sturz**.
11. **Beste Forschungsqualität:** Parent (A) um Trigger in seg0/epoch0; Gesamtfenster aller Signale später multi-epoch.
12. **Keine Kreuzkontamination:** ja (`signal_contracts.csv` / `signal_level_findings.json`)
13. **Collector 1692334 / OI 147111** unverändert
14. **DB** `research_full_ob_btc_20260904_signal_analysis` — **26487** Packets
15. **TRADE_DIRECTION=NOT_EVALUATED**

## Phase-H Kurzantworten

| Frage | Antwort |
| --- | --- |
| Starke Ask-Wall über dem Preis vor Crash? | Nur knapp vorher (81643, ~41 bps, 13s); starke Walls vor allem im Crash |
| Absorption aggressiver Käufer? | NOT_EVALUABLE (keine Trades) |
| Käufer scheitern trotz +Delta? | NOT_EVALUABLE |
| Bids vor Crash zurückgezogen? | PARTIALLY — Tiefe sinkt bilateral; kein klarer großer Withdrawal last-5m |
| Bids beim Crash konsumiert? | NOT_EVALUABLE ohne Trade-Link |
| Failed Reclaim VAH/POC/VAL? | FAILED_BREAKOUT OBSERVED (Preisverlauf nach Upper-Signalen) |
| Bearish control vor 12:30? | Nicht klar; eher BEARISH_ONLY_DURING_CRASH für Walls |
| OB erst mit Sturz bearish? | Überwiegend ja für Walls/Refills |
| Bestes Signal-Coverage? | Parent A (Trigger lokal) |
| Kausale OB-Frühwarnung Minuten vorher? | Nein — höchstens schwache bilaterale Ausdünnung; keine belastbare Minuten-Warnung |

Artefakte: `results/btc_full_ob_signal_to_crash_20260904_v1/`
