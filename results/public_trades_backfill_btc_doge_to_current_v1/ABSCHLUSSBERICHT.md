# ABSCHLUSSBERICHT — public_trades_backfill_btc_doge_to_current_v1

1. Verdict: **PUBLIC_TRADES_BACKFILL_BLOCKED**
2. Quelle: nicht genutzt (wäre public.bybit.com/trading) — Audit SOURCE_CONFIRMED
3. Downloader/Importer: nicht gestartet (wäre SG run_public_trades_7d_backfill.py)
4. Zielzeitraum: 12 Monate bis cutoff_utc=`2026-09-04T14:15:38Z` — **nicht ausgeführt**
5. BTC-Ergebnis: kein Import
6. DOGE-Ergebnis: kein Import
7. Importierte logische Records: 0
8. Physische Duplikate: n/a (kein Import)
9. Logische Duplikate: n/a
10. Rejects: n/a
11. Verbleibende Lücken: unverändert (CH weiterhin ab ~2026-07-19 laut Audit)
12. Resume-Status: nicht gestartet / kein Resume-State angelegt
13. Speicherverbrauch: 0 zusätzliche Importdaten
14. Laufzeit: Preflight-only
15. Canonical: `orderbook_analysis.public_trades_canonical` (bekannt, unberührt)
16. Live/Backfill-Kollisionen: keine (kein Backfill)
17. Collector-PIDs unverändert: 1692334, 147111, 1661773
18. DESTRUCTIVE_ACTIONS_EXECUTED=false

## Blocker

Audit-Verdict war `PUBLIC_TRADES_BACKFILL_PIPELINE_PARTIALLY_READY`, nicht das geforderte `PUBLIC_TRADES_BACKFILL_EXISTING_PIPELINE_READY`.
