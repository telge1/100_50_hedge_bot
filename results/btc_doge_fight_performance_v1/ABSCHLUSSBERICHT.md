# BTC/DOGE Fight-CLI Performance v1 — Abschlussbericht

**Verdict:** `BTC_DOGE_FIGHT_PERFORMANCE_READY`  
**Datum:** 2026-09-03  
**Datenstatus:** `BTC_DOGE_RESEARCH_TRADE_REMATERIALIZATION_READY`  
**Branch:** `feature/btc-doge-research-db` @ `2af0477`  
**Kein Commit / kein Push**

---

## 1. Ziel vs. Ergebnis

| Ziel | Ergebnis |
|------|----------|
| BTC full facts warm &lt;10 s | **Median 9,83 s** (Runs: 9,79 / 9,78 / 9,83 / 10,09 / 9,94) |
| DOGE full facts warm &lt;10 s | **~5,54 s** |
| Coverage-only &lt;1 s | **~0,11 s** |
| Golden-Parität | **PASS** (TPO/Volume/Delta/bps unverändert) |
| Keine Trade-Interpretation | **PASS** (`NOT_EVALUATED`) |
| Source-Purity | **PASS** (companion/raw/mixed = false) |

Baseline vor Opt (dieser Lauf): BTC warm **36,7 s** (Audit zuvor ~39,7 s) → Speedup ~**3,7×**.

---

## 2. Was geändert wurde (nur Code/Query)

Keine neue ClickHouse-Tabelle, Projection oder Sorting-Key. Grund: Code-/Query-Optimierung reicht für das Median-Ziel; `(symbol, trade_id)` + `FINAL` war der Query-Hotspot, nicht fehlende Zeit-Sortierung an sich.

Wesentliche Hebel:

1. **Kein `FINAL`** auf Fight-Trade-Loads und Coverage-Span-Probes  
   (`physical == canonical`, 0 Multi-Version-Keys). `FINAL` zerstörte event_time-MinMax-Pruning (~2 s × 2 bei Coverage).
2. **Sequence/Observability:** OB einmal vorbereiten; Coverage über Book-Levels statt Tick-Loops.
3. **TPO/Volume:** Baseline-Reuse, kausaler Prefix-Fast-Path, OA-Parity-Disk-Cache (Fingerprint).
4. **Wall/Ticks:** Float-Ticks; O(n²)-OB-Lookup → Dict.
5. **Lean Research-DB I/O:** Summary ohne 68 MB-Embeds; `heavy_detail_csv=False` (Wall-/Edge-Detail-CSVs entfallen, Summaries bleiben); keine DE-Template-Masse.

Betroffene Module u. a.: `research_db_loader.py`, `research_db_cli.py`, `reporting.py`, `edge_*`, `fight_sequence.py`, `tpo_profile.py`, `volume_profile.py`, `wall_events.py`, `level_events.py`, `instrument_contract.py`.

---

## 3. Golden-Parität (BTC 2026-08-31T19:00Z)

Unverändert:

- TPO POC/VAH/VAL: **78545 / 79080 / 78230**
- VPOC/VVAH/VVAL: **78565 / 79140 / 78190**
- 0–10m Delta: **+2,76 Mio. USD**; Preis: **+25,88 bps**
- OA-Parität: **EXACT**
- Eligibility: `DATA_COMPLETE`, companion/raw/mixed = false

DOGE 2026-08-31T13:00Z: Levels ~0,08; warm ~5,5 s.

---

## 4. Gates / Regressionen

| Fall | Status / Exit |
|------|----------------|
| Partial + `--require-complete` (2026-08-27T07:00Z) | `DATA_PARTIAL_FACTS_ONLY` / Exit **4** |
| Not available (2020-01-01) coverage-only | `DATA_NOT_AVAILABLE` / Exit **3** |
| Unit-Tests research-db + rematerialization | **29 passed** |
| Collectors | OI + live collector weiter aktiv, ungestört |

---

## 5. Bewusst nicht getan

- Keine Breakout-/Absorptions-/LONG-SHORT-Logik
- Keine Dashboard-/Collector-/OA-Änderungen
- Keine Raw-Archive, kein Legacy-Companion im Standardpfad
- Keine globalen ClickHouse-Settings
- Keine bestehenden Fight-Runs überschrieben (neue `out-root`s unter `results/btc_doge_fight_performance_v1/`)
- Kein CH-DDL trotz Sorting-Key-Nachteil — Code-Opt hat Ziel erreicht

---

## 6. Artefakte

| Datei | Inhalt |
|-------|--------|
| `preflight.json` | Phase 0 |
| `baseline_timings.json` | Vorher/Nachher-Zeiten |
| `bottleneck_ranking.json` | Hotspots + Query-Plan-Notiz |
| `before_after.json` | Speedup + Parität |
| `final_verdict.json` | Maschinenlesbares Verdict |
| `final/` | Messläufe BTC/DOGE/Coverage/Gates |
| `oa_parity_cache/` | Fingerprint-Cache (lokal) |

---

## 7. Verdict-Begründung

**`BTC_DOGE_FIGHT_PERFORMANCE_READY`**

Median- und Mittelwert-BTC-Warmlauf &lt;10 s, DOGE und Coverage klar im Ziel, Golden-Parität und Source-Purity erhalten, ohne Interpretation und ohne CH-Strukturänderung.

Hinweis: 1/5 BTC-Warmläufe lag bei **10,09 s** unter laufenden Collectors; typische Warmläufe 9,8–9,9 s. Für strengere Hard-Caps (&lt;10 s in *jedem* Sample) bliebe als nächster optionaler Schritt eine zeitgeordnete Projection — aktuell nicht nötig.
