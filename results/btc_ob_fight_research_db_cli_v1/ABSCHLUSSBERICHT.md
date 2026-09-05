# BTC/DOGE OB Fight — Research-DB CLI Abschlussbericht

**Verdict:** `BTC_DOGE_FIGHT_RESEARCH_DB_CLI_PARTIAL`  
**Contract:** `fight_data_eligibility_contract_v1`  
**Datum:** 2026-09-03

---

## 1. Finales Verdict

**`BTC_DOGE_FIGHT_RESEARCH_DB_CLI_PARTIAL`**

Die CLI ist auf `btc_doge_research` umgestellt, kausal, gate-fähig und golden-paritätisch nutzbar.  
Nicht READY, weil:

- vollständiger 60m-Faktenlauf warm noch **~34 s** (Ziel &lt;10 s)
- `--coverage-only` warm **~5 s** (Ziel &lt;1 s)
- Public-Trades oft über dokumentierten **Lineage-Companion** (`orderbook_analysis.public_trades_canonical`), nicht über materialisierte `research_public_trades`-Events
- Rest-Hardcoding BTC-Tick in einzelnen Edge-/HVN-Metadatenpfaden (Anzeige/DOGE-Profile korrigiert; Wall/Loader symbolfähig)

---

## 2. Empfohlenes CLI-Kommando

```bash
python scripts/run_btc_ob_fight_case.py \
  --timestamp 2026-08-31T19:00:00Z \
  --symbol BTCUSDT \
  --data-source research-db \
  --require-complete
```

DOGE:

```bash
python scripts/run_btc_ob_fight_case.py \
  --timestamp 2026-08-31T13:00:00Z \
  --symbol DOGEUSDT \
  --data-source research-db
```

Coverage-only:

```bash
python scripts/run_btc_ob_fight_case.py \
  --timestamp 2026-08-31T19:00:00Z \
  --symbol BTCUSDT \
  --data-source research-db \
  --coverage-only
```

---

## 3. Branch / HEAD / Dirty

| Feld | Wert |
|------|------|
| Branch | `feature/btc-doge-research-db` |
| HEAD | `2af0477dc4654d47603a2845a0ea1463b0bcfa12` |
| Dirty | ja (viele uncommittete Research-/Fight-/Results-Dateien; kein Commit/Push) |
| Collector-PIDs (Beobachtung) | u. a. OI/Liq-Collector, Stoch live-collector, `orderbook_v2_live` raw-archive BTC+DOGE |
| ClickHouse | read-only in dieser Phase; Version laut Preflight `26.7.1.1315` |

Artefakte Phase 0: `preflight_call_graph.json`, `source_mapping.csv`.

---

## 4. Alter und neuer Datenpfad

**Alt (raw-legacy):**  
`run_btc_ob_fight_case.py` → zstd OB200-Stundenarchive + Dashboard/OA-Replay → ~20 Minuten.

**Neu (Standard `research-db`):**  
CLI → `research_db_cli.run_research_db_analysis` → Coverage-/Eligibility-Gate →  
`research_db_loader` (OB200/Trades/OI/Liq/Candles) → kausale TPO/Volume aus Trades → Wall-/Fight-/Sequence-Pipeline → Outputs.

- Kein stiller Raw-Fallback
- Keine Vermischung Research-DB ↔ Raw im selben Lauf
- Optional `--data-source raw-legacy` isoliert mit Warnung `LEGACY_SLOW_RAW_REPLAY`

---

## 5. Research-Tabellen und Lineage

| Quelle | Tabelle / Pfad | Rolle |
|--------|----------------|-------|
| OB200 | `btc_doge_research.research_ob200_snapshots_1s` | Pflicht |
| Public Trades | `research_public_trades` wenn Fenster abgedeckt; sonst Lineage-Companion `orderbook_analysis.public_trades_canonical` | Pflicht |
| OI | `research_open_interest_observations` | Kontext |
| Liquidationen | `research_liquidation_events` | Kontext |
| Candles | kanonisch / COVERAGE_ONLY (`signal_generator.candles_1m`) | Kontext |
| Batch-Status | `research_batch_runs` | Lineage/Coverage |
| Session-Profile bins | nur Audit/Parität, **nicht** Fight-Input (nicht kausal für beliebigen Anchor) |

Manifest-Felder: `data_source=BTC_DOGE_RESEARCH_DB`, `database=btc_doge_research`, `raw_archive_replay_used=false`, `mixed_sources_used=false`, optional `lineage_companion_used=true`.

---

## 6. Eligibility-Contract

`fight_data_eligibility_contract_v1`

Gate-Zustände:

1. `DATA_COMPLETE`
2. `CONTEXT_PARTIAL`
3. `DATA_PARTIAL_FACTS_ONLY`
4. `DATA_NOT_AVAILABLE` (inkl. OB200 vollständig absent, auch wenn Trades existieren)
5. `DATA_CONTRACT_ERROR`

Booleans: `facts_computation_allowed`, `interpretation_allowed`, `trade_decision_eligible`, `profile_causality_passed`, `mandatory_data_complete`, `context_data_complete`.

Immer in dieser Phase: `rules_frozen=false`, `trade_verdict_evaluated=false`, `direction=null`.

---

## 7. Pflicht- und Kontextquellen

**Pflicht:** Public Trades (Fight-Fenster), OB200 (Fight-Fenster), Profil-Trades kausal bis Anchor, Preis-/Trade-Sequenz.

**Kontext:** OI, Liquidationen, Candles, optionale Features.

PARTIAL wird nie als COMPLETE behandelt. OI-PARTIAL → `CONTEXT_PARTIAL`, nicht automatisch `DATA_PARTIAL_FACTS_ONLY`.

---

## 8. Exit Codes

| Code | Bedeutung |
|------|-----------|
| 0 | Faktenlauf `DATA_COMPLETE` oder `CONTEXT_PARTIAL` (ohne `--require-complete`-Verletzung) |
| 1 | technischer Fehler |
| 2 | CLI-Fehler |
| 3 | `DATA_NOT_AVAILABLE` |
| 4 | PARTIAL/CONTEXT_PARTIAL bei `--require-complete` |
| 5 | `DATA_CONTRACT_ERROR` |

Abwärtskompatibel: bisheriges „Daten fehlen“ bleibt im Daten-Exit-Pfad (3).

---

## 9. Profile-Causalitätsnachweis

- Gespeicherte Session-TPO/Volume-Bins **nicht** als Fight-Input
- Rebuild aus deduplizierten Trades mit `session_start <= ts < anchor` (`anchor_exclusive=true`)
- Genuine 30m-Bracket-TPO, Volume base-volume-weighted, getrennte Semantiken
- Golden: Integrity/Prefix/Trade-Size-Invarianz PASS; OA-Volume-Parität EXACT

---

## 10. BTC-Golden-Parität

Lauf: `results/btc_ob_fight_cases/20260831T190000Z/run_029` (Log: `golden_btc_run3.log`)

| Größe | Soll | Ist |
|-------|------|-----|
| TPO POC/VAH/VAL | 78545 / 79080 / 78230 | 78545.0 / 79080.0 / 78230.0 |
| VPOC/VVAH/VVAL | 78565 / 79140 / 78190 | 78565.0 / 79140.0 / 78190.0 |
| 0–10m Taker-Delta | ~+2,76 Mio. USD | +2,76 Mio. USD |
| Preiswirkung | ~+25,88 bps | +25,88 bps |
| Raw-Replay | false | false |
| Trade-Verdict | false / null | false / null |
| Eligibility | DATA_COMPLETE | DATA_COMPLETE |

Runtime ~33,7 s wall / Peak RSS ~1,52 GB.

---

## 11. BTC-Partial-Ergebnis

Fenster um dokumentierte Lücke `2026-08-27T06:42:23Z`  
Anchor `2026-08-27T07:00:00Z`, ±30m → `run_002`

- Eligibility: `DATA_PARTIAL_FACTS_ONLY`
- Missing: `OB200 2026-08-27T06:42:23Z` (+ Trade-Coverage incomplete für diesen Tag)
- `trade_decision_eligible=false`
- `--require-complete` → Exit **4**
- kein Raw-Fallback

---

## 12. DOGE-Complete-Ergebnis

Anchor `2026-08-31T13:00:00Z` (OB 12:30–13:30 vollständig; 11:00 hat 3599s und wurde bewusst vermieden)  
Lauf: `.../20260831T130000Z/run_004`

- Eligibility: `DATA_COMPLETE`
- TPO POC/VAH/VAL: **0.08267 / 0.08505 / 0.08140**
- VPOC/VVAH/VVAL: **0.08273 / 0.08445 / 0.08075**
- kein Raw-Replay; Runtime ~21 s

---

## 13. DATA_NOT_AVAILABLE-Ergebnis

Anchor `2026-08-20T12:00:00Z` (vor OB-Historie ab ~2026-08-24T22:47:54Z)  
`--coverage-only` → Exit **3**, Eligibility `DATA_NOT_AVAILABLE`, Coverage-Outputs vorhanden, keine Fight-Interpretation.

---

## 14. Laufzeiten vorher/nachher

| Modus | Vorher | Nachher |
|-------|--------|---------|
| Voller Faktenlauf BTC Golden | ~20 min (Raw-Replay) | **~34 s** |
| Coverage-only | n/a | **~5 s** |
| DOGE Complete | n/a | **~21 s** |

Phase-Breakdown BTC Golden (warm, nach Opt.): Load/Eligibility ~5 s, Profile ~2 s, OB-Adapt ~4 s, Wall ~1,5 s, Fight ~1 s, Sequence/Edge ~14 s, Rest I/O.

---

## 15. Peak RAM vorher/nachher

| | Peak RSS |
|--|----------|
| Alt (Roharchiv, grob) | deutlich höher / minutenlanger Replay |
| Neu BTC Golden | **~1,52 GB** |
| Neu DOGE | **~1,27 GB** |
| Coverage-only | ~0,85 GB |

---

## 16. Query-Timings (BTC Golden, Beispiel)

| Quelle | elapsed_s | rows |
|--------|-----------|------|
| OB200 | ~0,48 | 3601 |
| PUBLIC_TRADES (Companion) | ~2,56 | ~850k |
| OPEN_INTEREST | ~0,005 | 720 |
| LIQUIDATIONS | ~0,003 | 8 |
| CANDLES_1M | ~0,002 | 1 |

---

## 17. Tests

Datei: `tests/research/test_btc_ob_fight_research_db_cli.py` — **14 passed**.

Abgedeckt u. a.: Gate-Zustände, OB200-Gap, OI-Kontext, null Liquidationen, Trade-Dedup-Coverage, forbid-write SQL, kein Raw im Loader-Modul, CLI-Defaults, OB200-absent → `DATA_NOT_AVAILABLE`, Exit-Codes `--require-complete`.

Manuelle Integration: Golden, Partial, DOGE, NOT_AVAILABLE, coverage-only (siehe Logs in diesem Ordner).

---

## 18. Neue/geänderte Dateien (Kern)

Neu:

- `research/btc_ob_fight/eligibility_contract.py`
- `research/btc_ob_fight/coverage_gate.py`
- `research/btc_ob_fight/research_db_loader.py`
- `research/btc_ob_fight/research_db_cli.py`
- `tests/research/test_btc_ob_fight_research_db_cli.py`
- `results/btc_ob_fight_research_db_cli_v1/*`

Geändert (Auswahl):

- `research/btc_ob_fight/cli.py`, `config.py`
- `wall_events.py` (Trade-Index, Symbol-Tick)
- `edge_book_coverage.py`, `edge_observability.py` (Perf)
- `formatting.py` (adaptive Preisformatierung DOGE)
- `tpo_profile.py`, `volume_profile.py` (Tick-Hilfen)

Unverändert gelassen: `run_011`–`run_017`, Collector, Dashboard, keine CH-Writes in dieser Phase.

---

## 19. Offene Punkte

1. Voll-Lauf &lt;10 s: Sequence/Edge-Book-Coverage und große Companion-Trade-Loads dominieren
2. Materialisierte `research_public_trades`-Events für volle Historie (statt Companion)
3. Restliche BTC-Tick-Hardcodes in Edge-/Profile-Metadaten vollständig auf Symbol-Tick umstellen
4. `--coverage-only` ohne schweren Trade-Load weiter verkürzen
5. Automatisierte Integrationstests für alle 25 Pflichtfälle aus der Spezifikation

---

## 20. Bestätigung

- keine Trading-Regeln / kein LONG/SHORT / kein Breakout-/Absorptions-Verdict
- kein Dashboard-/Collector-Change
- keine ClickHouse-Writes in dieser Phase
- kein Commit / kein Push
- `rules_frozen=false`, `trade_verdict_evaluated=false`, `direction=null`

---

## Finale Kurzformel

Die Research-DB-Fakten-CLI ist **betriebsfähig und kausal gated**, mit Golden-Parität und korrekten Partial/NOT_AVAILABLE-Pfaden, aber wegen Runtime-Zielverfehlung und Companion-Trade-Pfad als **`BTC_DOGE_FIGHT_RESEARCH_DB_CLI_PARTIAL`** eingestuft.
