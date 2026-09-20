# ABSCHLUSSBERICHT — Research Public-Trade Rematerialization v1

## Verdict

**BTC_DOGE_RESEARCH_TRADE_REMATERIALIZATION_STARTED**

Root Cause bewiesen, Backup + Quarantäne vorhanden, Phase-7-Pilot (2×) und
Idempotenz bestanden, source-pure Fight-Golden (BTC+DOGE) bestanden, Full-Backfill
läuft unter Lock/nohup. Gesamthistorie noch nicht fertig → kein READY.

## Root Cause (−2h)

| Fakt | Wert |
|------|------|
| Shift | konstant **−7200 s** |
| Host-TZ | Europe/Paris (CEST, UTC+2) |
| CH Server | `timezone() = UTC` |
| Mechanismus | `clickhouse_connect` liefert UTC-`DateTime64` als **naive** Python-Datetime; Insert interpretiert Naive als lokal (CEST) → Speicherung −2h in UTC-Spalte |
| Probe | aware UTC bleibt korrekt; naive wall-clock wird um −2h verschoben |
| Betroffen | Pilot-Batches `phase1:btc_run_018` (80738 Rows, intended 18:30–19:30Z) und `phase1:doge_20260829_1145_1230` (4275 Rows) |
| Nicht | Source-Shift, DST-Ambiguität, pauschaler Transform `+2h` |

## Branch / HEAD / Dirty

- Branch: `feature/btc-doge-research-db`
- HEAD: `2af0477dc4654d47603a2845a0ea1463b0bcfa12`
- Dirty: ja (viele uncommitted Working-Tree-Dateien; **kein Commit, kein Push**)

## Source / Target DDL & Key

- Source (read-only): `orderbook_analysis.public_trades_canonical` — `trade_ts DateTime64(3,'UTC')`
- Target: `btc_doge_research.research_public_trades` — **ReplacingMergeTree(record_version) ORDER BY (symbol, trade_id)**
- Quarantäne: `research_public_trades_invalid_shifted_v0` (85013 shifted Rows erhalten)
- Kanonischer Key: **`(symbol, trade_id)`** / `event_key = {symbol}\|{trade_id}`
- Contract: `research_public_trades_contract_v2`
- Build-ID: `8244abe0186ff712a7917eb66e397dd7c93cc28457aaf0d5a42652fe8c96e323`

## Backup & Rollback

- Parquet-Backup unter `results/btc_doge_research_trade_rematerialization_v1/backup/`
- `backup_manifest.json`, `rollback_plan.md`
- Rollback: Quarantäne behalten; shifted Rows **nicht** zurück in kanonische Sicht kopieren

## Reparaturarchitektur

Variante **B** (versioniert, auditierbar):

1. RENAME shifted MergeTree → `*_invalid_shifted_v0`
2. Neu: ReplacingMergeTree + server-seitiges `INSERT SELECT` aus OA
3. Claim → IN_PROGRESS → COMPLETE / FAILED in `research_trade_rematerialization_watermarks`
4. Resume: ClickHouse-Watermark SoT; `IDEMPOTENT_SKIP` bei gleichem Source-Fingerprint
5. File Lock (`runner.lock`) verhindert parallele Runner
6. Fight-Loader: `research_public_trades FINAL`; Companion nur explizit

UTC-Contract: keine naiven Inserts; Timestamps bleiben in ClickHouse; Anchor-Vergleiche UTC.

## Pilot (Phase 7) — 2× PASS

| Segment | Fenster | Pass1 | Pass2 | Parität |
|---------|---------|-------|-------|---------|
| A BTC Golden 18 | 2026-08-31T18:00–19:00Z | IDEMPOTENT_SKIP | IDEMPOTENT_SKIP | exact |
| A BTC Golden 19 | 2026-08-31T19:00–20:00Z | IDEMPOTENT_SKIP | IDEMPOTENT_SKIP | exact |
| B DOGE Complete | 2026-08-31T13:00–14:00Z | COMPLETE | IDEMPOTENT_SKIP | exact |
| C −2h Inventar | 2026-08-29T11:00–12:00Z (DOGE, aus affected_segments) | COMPLETE | IDEMPOTENT_SKIP | exact |

- `pilot_results.json`, `pilot_parity.csv`, `pilot_idempotency.json`
- Idempotenz: **IDEMPOTENCY_PASS** (Pass2 = 4× IDEMPOTENT_SKIP)

## Full-Backfill (Phase 8)

| Größe | Wert |
|-------|------|
| Segmente | 2112 (2100 non-empty) |
| Erwartete Unique Trades | ~82.0M |
| Disk free | ~365 GB |
| Est. Rest | ~525 min bei 2k rows/s (konservativ) |
| Batch | 1 UTC-Stunde |

**Monitoring**

```bash
# Status
.venv/bin/python scripts/run_btc_doge_trade_rematerialization.py --status

# Live
cat run/btc_doge_trade_rematerialization/heartbeat.json
cat run/btc_doge_trade_rematerialization/progress.json
tail -f logs/btc_doge_trade_rematerialization.log

# PIDs
cat run/btc_doge_trade_rematerialization/runner.pid
cat run/btc_doge_trade_rematerialization/launcher.pid
ps -p "$(cat run/btc_doge_trade_rematerialization/runner.pid)" -o pid,etime,cmd

# Stop / Resume
kill -TERM "$(cat run/btc_doge_trade_rematerialization/runner.pid)"
# danach:
nohup env PYTHONUNBUFFERED=1 \
  .venv/bin/python scripts/run_btc_doge_trade_rematerialization.py --run --resume \
  >> logs/btc_doge_trade_rematerialization.log 2>&1 &
```

Aktueller Runner-PID: siehe `run/btc_doge_trade_rematerialization/runner.pid` (beim Abschluss dokumentiert in `final_verdict.json`).

## Falsche Rows / Kanonische Sicht

- Vorher: shifted Rows in kanonischem Tabellennamen
- Nachher: shifted nur in `*_invalid_shifted_v0`; kanonisch = v2 FINAL
- Shifted canonical timestamps (Join auf rematerialisiertem Build): **0**
- Duplicate canonical keys: **0**
- `stale_ACTIVE_batch`: 1 während laufendem Segment (erwartet)

## Downstream

Fight-CLI baut TPO/Volume **kausal** aus `research_public_trades`-Events
(`session_start <= ts < anchor`). Alte Derived-Tabellen =
`NOT_USED_BY_FIGHT_CLI` / nicht entscheidungsfähig. Kein erzwungener Full-Rebuild
der großen Bucket/TPO-Tabellen in dieser Phase.

## Fight Golden (source-pure)

### BTC `2026-08-31T19:00:00Z`

- Eligibility: **DATA_COMPLETE**
- Status: **BTC_OB_FIGHT_CANONICAL_ELIGIBILITY_READY**
- `lineage_companion_used=false`, `mixed_sources_used=false`, `raw_archive_replay_used=false`
- TPO POC/VAH/VAL: **78545 / 79080 / 78230**
- Volume VPOC/VVAH/VVAL: **78565 / 79140 / 78190**
- 19:00–19:10 Delta ≈ **+2.76 Mio. USD**, Preis ≈ **+25.88 bps**
- Wall clock: **~41 s** (`/usr/bin/time -v`)
- Pfad: `fight_golden_v2/.../20260831T190000Z/run_001`

### DOGE `2026-08-31T13:00:00Z`

- Eligibility: **DATA_COMPLETE**, companion=false
- Status: **BTC_OB_FIGHT_CANONICAL_ELIGIBILITY_READY**

## Tests

`tests/research/test_btc_doge_trade_rematerialization.py` + bestehende Fight-CLI-Tests:
**29 passed** (`test_results.txt`).

## Collector / Live

OI/Liq-Collector und Live-Collector unverändert aktiv; keine Collector-/Raw-OB-Änderung.
Operative DBs: keine Writes.

## Fight-CLI-Optimierung fortsetzen?

**Ja.** Source-pure Golden ist grün; Hard Gate `RESEARCH_TRADE_EVENTS_MISSING` für das
Golden-Fenster ist behoben. Full-History-Rematerialisierung kann parallel weiterlaufen;
Optimierungsarbeit an der Fight-CLI darf fortgesetzt werden, solange keine READY-Behauptung
für die gesamte History gemacht wird.

## Bestätigung

- Kein Commit
- Kein Push
- Kein Legacy-Companion im Standardpfad
- Kein pauschaler +2h-Fix
