# Research-DB Source Purity & Performance — Abschlussbericht

**Verdict:** `BTC_DOGE_FIGHT_RESEARCH_DB_CLI_PARTIAL`

**Hard Gate:** `RESEARCH_TRADE_EVENTS_MISSING`

---

## 1. Finales Verdict

Nicht `READY`: normale Fight-Trade-Events fehlen korrekt zeitgestempelt in
`btc_doge_research.research_public_trades`. Der frühere Lineage-Companion lud
echte Events aus `orderbook_analysis.public_trades_canonical` und verletzt
Source-Purity.

Erreicht in dieser Phase:

- Companion im **Standardpfad entfernt**
- `mixed_sources_used` wahrheitsgemäß
- Coverage-only warm **~0.09–0.12 s** (Ziel &lt;1 s)
- Instrument-Contract für BTC/DOGE; Edge-Ticks über aktives Symbol
- Wall-/Edge-Algorithmen bereits indexiert (aus Vorphase)
- 19 Unit-Tests grün
- Importplan dokumentiert; **kein ungeprüftes Nachladen**

---

## 2. Source-Purity (Kernbefund)

| Frage | Antwort |
|-------|---------|
| Companion = nur Metadaten? | **Nein** — lädt Trade-Events |
| Tabelle | `orderbook_analysis.public_trades_canonical` |
| Warum research_public_trades nicht? | Full-History schrieb **Buckets**; Events nur spärlich |
| Golden 18:30–19:30 Events in Research? | **0** |
| Vorhandene 80738 BTC-Rows 16:30–17:30? | = OA 18:30–19:30 trade_ids mit **−2h Shift** |
| Companion entfernt? | **Ja** (Default); nur `--allow-legacy-trade-companion` |
| mixed_sources vorher? | Manifest `false`, faktisch gemischt |

Details: `RESEARCH_TRADE_EVENTS_MISSING.md`, `source_purity_matrix.csv`

---

## 3. Performance

| Modus | Vorher | Nachher |
|-------|--------|---------|
| Coverage-only warm | ~5 s | **~0.1 s** |
| Source-pure Full Facts Golden | n/a (Companion) | **blockiert** Exit 3 in ~1 s |
| Companion Full Facts (Vorphase) | ~34 s | nicht READY-fähig |

Bottlenecks (Companion-Ära): Sequence/Edge ~14 s, Trade-Load ~2.5 s, OB-Adapt ~4 s.
Siehe `phase_timings_before.csv`, `bottleneck_ranking.json`, `algorithmic_optimizations.csv`.

**PREAGGREGATION_REQUIRED** für spätere CH-Bin/Bracket-Aggregation — erst nach
korrekter Event-Rematerialisierung sinnvoll für &lt;10 s Full-Lauf ohne Companion.

---

## 4. Eligibility

- Fehlende Research-Trade-Events → `DATA_NOT_AVAILABLE` /
  `RESEARCH_TRADE_EVENTS_MISSING`
- OB-Gap Partial unverändert
- `--require-complete` Exit 4 bei PARTIAL; Exit 3 bei NOT_AVAILABLE
- Coverage-only lädt keine OB-Arrays / keine Trade-Event-Bodies

---

## 5. Symbol-Contract

`fight_instrument_contract_v1` in `instrument_contract.py`  
`set_active_symbol()` in Fight-Pfad; `profile_edge_state.price_to_tick` symbolabhängig.

---

## 6. Tests

`tests/research/test_btc_ob_fight_research_db_cli.py` — **19 passed**  
(Gates, Instrument, CLI-Flags, Loader-Purity-Strings, …)

---

## 7. Bestätigung

- keine Trading-Regeln / kein LONG/SHORT
- kein Dashboard/Collector-Change
- keine CH-Writes / kein Event-Import in dieser Phase
- run_011–run_029 unverändert
- kein Commit/Push

---

## 8. Nächster Schritt (separat)

Ausführen des Importplans in `RESEARCH_TRADE_EVENTS_MISSING.md`, dann erneut:

1. Source-pure Golden ohne Companion  
2. Full-Lauf Timing &lt;10 s (ggf. PREAGGREGATION)  
3. READY-Prüfung
