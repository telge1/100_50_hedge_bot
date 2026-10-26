# PHASE BT-D5 — Frozen Short Strategy Discovery + Full PnL Backtester Audit V1

**Date:** 2026-10-25  
**Mode:** Read-only. No code changes, no backtest runs, no SSOT/chart edits.

**BT-D3/BT-D4 baseline:** Canonical Depth-2 provider + `depth2_density_research_v1` enforced; Golden **39 / 176 / 215** @ `2026-09-25T11:00:00Z` (not legacy Python 218).

---

## VERDICT

### `FROZEN_SHORT_SEMANTIC_APPROVAL_REQUIRED`

**Zusatz (Teil D):** `BACKTESTER_PNL_BLOCKED` — Gesamt-PnL in USDT/Equity/Drawdown ist im Pool-Landscape-Backtester **nicht** implementiert; nur Prozent-PnL pro Trade mit `roundtrip_fees_pct`.

**Begründung Kurz:** Die Pool-Landscape-**SHORT**-Linie ist als **`pool_landscape_v1_candidate`** mit klaren USER_CONFIRMED-Regeln auffindbar, aber **nicht** `FINAL_APPROVED`. Mehrere **USER_CONFIRMED**-Texte (dense Lower-TP-Cluster, große Upper-Gap, „relevanter“ Upper-Pool) weichen von der **4D/2A-Implementierung** ab. Depth-2-Anbindung ist **technisch vorbereitet** (BT-D4), erfordert aber **Freigabe** der Semantik-Lücken vor Strategie+PnL-Integration.

---

## 1. Gefundene „Frozen“-Short-Strategie

### 1.1 Primärer Freeze-Kandidat (Pool Landscape, nicht LMV2)

| Feld | Wert |
|------|------|
| **ID** | `pool_landscape_v1_candidate` |
| **Pfad** | `strategy_freeze/pool_landscape_v1_candidate/` |
| **Approval** | `FREEZE_CANDIDATE` / **`NOT_FINAL_APPROVED`** (`SOURCE_PROVENANCE.json` L52) |
| **Generiert** | `2026-10-22` (Provenance-Metadatum) |
| **Implementierung Backtest** | `tools/phase4d_pool_landscape_backtester_v2/` |
| **Signale (SHORT)** | `tools/phase2a_5m_pd1_depth2_short_candidates_v1/` |
| **Lifecycle / Pending** | `pool_zone_tracker.py`, `pending_limit_simulator.py` (4E) |
| **User-Regel-SSOT (Text)** | `Signal_definierung.md` (SHA in `SOURCE_PROVENANCE.json`) |

**Explizit getrennt von Original LMV2 Short V2:**  
`SOURCE_PROVENANCE.json` L4–5; `Strategie_feinopt.md` (Nearest-Lower, 15m/4h); `backtester/short/dashboard/research_charts/historical_lmv2/`; `frozen_strategies/e1r_cluster3_ema200_v1/` — **andere Linie**.

### 1.2 Weitere Kandidaten (nicht als „zuletzt freigegeben“ wählen)

| Kandidat | Pfad / Version | Approval | Rolle |
|----------|----------------|----------|--------|
| **STRATEGY_SPEC_CANDIDATE** (Basis) | `strategy_freeze/.../STRATEGY_SPEC_CANDIDATE.md` | NOT FINAL | Regelkatalog DATA/SHORT |
| **4D amendment** | `STRATEGY_SPEC_CANDIDATE_4D.md` | NOT FINAL | Preisformeln SL/TP/Entry für 4D |
| **4E amendment** | `STRATEGY_SPEC_CANDIDATE_4E.md` | NOT FINAL | Pending-Limit-Ausführung |
| **4H OCO** | `tools/phase4h_oco_pending_entry_v1/strategy_config_oco_v1.json` | candidate → 4E | OCO SHORT+LONG pending — **Erweiterung**, nicht Basis-Short-only Freeze |
| **Phase 4A** | `tools/phase4a_single_position_backtest_pilot_v1/` | research | Vorgänger; 4D ersetzt Preislogik, nicht Signalerzeugung |
| **Phase 4M market** | `tools/phase4m_touch_rejection_market_backtester_v1/` | isolated | Touch/Rejection — **nicht** eingefrorene Pool-Landscape-Short-V1 |
| **Original LMV2 Short V2** | `Strategie_feinopt.md`, historical_lmv2 | separate frozen line | **Nicht** Pool-Landscape |

**Git:** Keine Commits nur für `strategy_freeze/pool_landscape_v1_candidate/` in diesem Workspace-Log — Freeze liegt als **Artefakt-Bundle** mit Provenance-JSON, nicht als `FINAL_APPROVED` git tag.

### 1.3 „Zuletzt bestätigt“ — Beleglage

| Beleg | Inhalt |
|-------|--------|
| `USER_CONFIRMED_RULES.md` | Status `FREEZE_CANDIDATE`; Authority = `Signal_definierung.md` + Recovery 2026-10-22 |
| `Signal_definierung.md` § SHORT – ENTRY / SL / TP V1 (L561–571) | **USER_CONFIRMED** Preisanker |
| `Signal_definierung.md` § PENDING LIMIT V1 (L579–593) | **USER_CONFIRMED** Ausführung |
| `Signal_definierung.md` § Scanner Min-Depth 2 (L344–374) | **Verbindlich ab 22.10.2026** Forschung |
| `reports/DOGEUSDT_PHASE4D_STRICT_STRATEGY_BACKTESTER_V2.md` | 4D Preisformeln; Execution teils OPEN |
| `IMPLEMENTATION_DEVIATIONS.md` | Systematische Abweichungen User vs Code |

**Kein Dokument** mit Status `FINAL_APPROVED` für Pool-Landscape-Short.

---

## 2. Freeze- und Approval-Belege

- **Highest authority (user text):** `Signal_definierung.md`
- **Structured freeze bundle:** `strategy_freeze/pool_landscape_v1_candidate/*`
- **Explicit non-approval:** `SOURCE_PROVENANCE.json` → `"status": "NOT_FINAL_APPROVED"`
- **Open decisions gate:** `OPEN_STRATEGY_DECISIONS.md` (O-01 … O-15)
- **Integration blocked until final:** `BACKTESTER_INTEGRATION_CONTRACT.md` → `CANDIDATE_ONLY`

---

## 3. Vollständige Regelübersicht (15 Nutzerfragen)

Legende Status: **FROZEN** = USER_CONFIRMED in Signal/freeze docs; **EXPERIMENTAL** = implementiert aber nicht validiert; **UNCONFIRMED** = OPEN / research default.

### 3.1 Landschaft & Signalerkennung

| # | Frage | Antwort (exakt) | Status | Referenz |
|---|--------|-----------------|--------|----------|
| 1 | Pools ober-/unterhalb Preis? | Short nutzt **Upper** Depth-2-Bänder für Entry-Kontext; **Lower** Depth-2 für TP-Landschaft. Vollständige obere/untere Depth-2-Sicht wie Chart (keine PD-Mix). | FROZEN | `Signal_definierung.md` L350–363; DATA-03 `USER_CONFIRMED_RULES.md` |
| 2 | Auswahl kleiner Upper-Entry-Pool? | **User:** „relevanter“ Upper bei Interaktion + Rejection. **Code 2A:** `pickUpperPool` = **nächste** Proximity unter `proximity_pct` **0.05%** unter Depth-2-`display_intervals`. | FROZEN (Intent) / **DEVIATION** (Algo) | `timeline_scanner.js` L48–60; `IMPLEMENTATION_DEVIATIONS.md` #1 |
| 3 | Große Gap oberhalb? | **≥0.5%** zwischen benachbarten sichtbaren Depth-2-Bereichen; „groß“ für Landschaft/Bewertung. **2A:** nur in **Profil** (`large_gap_pct` 0.5), **kein** Scan-Gate. | FROZEN (Definition) / **MISSING** (Gate) | `Signal_definierung.md` L365–367; `IMPLEMENTATION_DEVIATIONS.md` #4 |
| 4 | Gültiges Short-Signal? | Abgeschlossene 5m-Kerze: Depth-2-Upper in Proximity; `rejection_v1` (close &lt; open); Cooldown **6** Kerzen/Pool; max **20** Exporte chronologisch. | PARTIAL FROZEN | `timeline_scanner.js` L40–46, L107; Defaults **UNCONFIRMED** (#21–23 deviations) |
| 5 | Pool-Alter? | Research `min_pool_age_bars=5` in Config; **kein** Hard-Gate (`pool_age_hard_gate=false` in 4D). User: C003-Gegenbeispiel → nicht hart. | UNCONFIRMED hard gate | `strategy_config_v2.json`; O-04 |
| 6 | Mindestdistanz zwischen Pool-Strukturen? | **Gap „groß“:** ≥**0.5%** zwischen Depth-2-Bändern. **Entry→TP:** separat ≥**0.5%** (s. #10). `min_pool_gap_pct` 0.5 in Config **nicht** als Emit-Gate erzwungen. | FROZEN (Gap-Def) / OPEN (Enforcement) | O-05; `strategy_config_v2.json` L8 |
| 7 | Entry (Level)? | **Sell-Limit** an **Unterkante** des gewählten Upper-Pools (`upper_pool_lower_edge`). | FROZEN | `STRATEGY_SPEC_CANDIDATE_4D` SHORT-ENTRY-LEVEL-01; `Signal_definierung.md` L565 |
| 8 | Stop-Loss? | **0.2%** über **Oberkante** desselben Upper-Pools: `sl = upper_top × 1.002`. | FROZEN | `sl_above_upper_pool_pct: 0.2`; `strategy_rules.short_sl` |
| 9 | Lower-TP-Pool? | **User:** großer/**dichter** Lower-**Cluster**, nicht stärkster Einzelpool. **4D:** **nächstes** Depth-2-**Band** (nach `top` sortiert), dessen **Top** ≥**0.5%** unter **Fill**. | FROZEN (User) / **DEVIATION** (4D algo) | `USER_CONFIRMED_RULES.md` SHORT-05; `select_short_tp_depth2`; O-01 |
| 10 | Mindestabstand Entry–TP? | **0.5%** vom **tatsächlichen Entry (Fill)** zur **Oberkante** des TP-Lower-Bands (`top_price`). | FROZEN | `min_tp_distance_pct: 0.5`; `select_short_tp_depth2` |
| 11 | ACTIVE / Depth / Origins? | Depth-2 = Exact Unique-Origin (≥2 Origins im Band). Entry-Zone-Storno: Depth-2-**Zone** ACTIVE per 3A-Lifecycle + Origin-Overlap (`pool_zone_tracker`). | FROZEN (4E) / EXPERIMENTAL (3A) | `STRATEGY_SPEC_CANDIDATE_4E`; `zone_status_at_bar` |
| 12 | Berührte / verschwundene Pools? | Pending cancel: `POOL_DISAPPEARED` / Depth-2-Verlust → `ENTRY_POOL_DEPTH2_LOST` (4M-Pattern); 4D `EXEC-POOL-DEACTIVATED-01`. Einzel-Origin-Verlust **ohne** Zonenverlust → **nicht** auto-cancel (4E). | FROZEN (4E text) / UNRESOLVED (edge cases) | `Signal_definierung.md` L591; `pool_zone_tracker.py` |
| 13 | Signal verworfen? | Kein TP (Depth-2 + 0.5%); Suppressed bei offener Position; unresolved Execution/ambiguous exit; optional quality gate off. | IMPLEMENTED | `engine.py`; `exit_simulator.py` |
| 14 | Erneuter Short? | Max **1** Position; neue Signale während offen → `SUPPRESSED_POSITION_OPEN`. Re-entry gleiche Opportunity **UNRESOLVED** (O-10). | FROZEN (1 pos) / OPEN (re-entry) | POS-01; `engine.py` |
| 15 | Exit / Lifecycle? | Nach Fill: SL/TP aus 5m-OHLC; gleiche Kerze SL+TP → **ambiguous**, kein optimistisches TP (`AMBIGUOUS_INTRABAR_EXIT`). Offen am Datenende → `POSITION_OPEN_AT_DATA_END`. Pending bis Fill oder Zone-Loss. | FROZEN (policy intent) / UNRESOLVED (user sign-off O-11) | `exit_simulator.py` L50–61; `OPEN_STRATEGY_DECISIONS` O-11 |

---

## 4. Unterschiede Strategieversionen

| Thema | User / Freeze Text | Phase 2A Scanner | Phase 4D Backtest |
|--------|-------------------|------------------|-------------------|
| Density | Chart Depth-2 exact | `display_intervals` via 1B analyzer (chart path) | TP aus `landscape_intervals` depth==2 (frozen at signal) |
| Upper pick | Relevant + rejection | Nearest proximity 0.05% | Übernimmt 2A-Signal |
| Upper large gap | ≥0.5% landscape | Profile only | Nicht gefiltert |
| TP | Dense cluster | N/A | Nearest depth-2 lower **top** |
| Entry timing | Next 5m open (signal doc) + Pending limit at pool edge | Signal close + `entry_open_utc` | `USER_PENDING_LIMIT_V1` + lifecycle |
| Market structure | WATCH / two-lower | N/A | **disabled** in 4D config |
| Datenfenster | May–Sep SSOT (heute) | Sep pilot window only | Sep 11–12 pilot |

---

## 5. Depth-2-Kompatibilitätsmatrix (BT-D4 ↔ Frozen Short)

| Anforderung | BT-D4 `collect_at_close` | Frozen Short heute | Kompatibel? |
|-------------|--------------------------|-------------------|-------------|
| 5m PD1 exact depth-2 Bänder | Ja (Gateway → BT-D3) | 2A nutzt gleiche JS-Kette **einmal** pro SSOT-Fenster | **Ja**, wenn Signale **pro Close** aus BT-D4 regeneriert werden |
| Volle Upper/Lower-Feld | `upper_bands` / `lower_bands` + origins | 2A: ein Upper-Band + `landscape_intervals` Snapshot | **Teilweise** — Signal speichert **Teilmenge** |
| Gap ≥0.5% | Nicht im Provider (nur Bänder) | `computeGaps` in 2A-Profil | **Adapter nötig** — Gap aus BT-D4-Bändern berechnen (gleiche JS-Hilfe) |
| Entry-Pool-Auswahl | Nicht im Provider | 2A-Logik auf `upper_bands` | **Semantik** — Proximity vs „relevant“ |
| TP dense cluster | Nicht im Provider | 4D `select_short_tp_depth2` | **Nein ohne Freigabe** — User will Cluster, 4D nutzt nearest band |
| Kein Lookahead | Ja (kausal SSOT) | Signale frozen; Lifecycle 3A pilot | **Ja** nach Close-keyed Refresh |
| 168h + alte ACTIVE | Ja (`build_landscape_window`) | 2A/3A altes Fenster | **Ja** mit BT-D4-Fensterpolitik |
| `min_depth` JSON only | Erzwungen `min_unique_origins=2` | `min_depth:2` in 4D JSON **ohne** Engine-Check | **4D muss** BT-D4-Gateway nutzen, nicht JSON allein |

### Minimale Adapter-Anbindung (nach Freigabe)

1. **Pro `decision_close`:** `ResearchDensityRunV1.collect_at_close` → Bänder-SSOT.
2. **Signal-Scan:** 2A-Logik auf `upper_bands` (oder refactor shared module) — **ohne** zweite Density.
3. **TP:** Entweder User bestätigt **4D nearest depth-2** als Amendment **oder** neues Cluster-Ranking (O-01) — **kein** stiller Fix.
4. **Gap-Gate:** `computeGaps` auf BT-D4-Bänder — nur wenn User O-05/O-06 schließt.
5. **Lifecycle:** 3A-CSV durch Close-keyed Depth-2-Status aus Bändern **ersetzen** oder aus voller Timeline (4T) — nur nach Causalitäts-Review.

### Semantischer Informationsverlust beim Wechsel

- **Einzel-Upper-Rechteck** im Signal vs **volles** Bandfeld → Verlust von Alternativ-Upper-Kontext, sofern nicht neu gescannt.
- **Dense-Cluster-TP** vs **ein** Depth-2-Intervall → **material** (O-01).
- **Origin-Enrichment** in Bändern vs Lifecycle-IoU-Matching → Randfälle `IDENTITY_AMBIGUOUS`.

---

## 6. Backtester PnL-Audit (Phase 4D V2)

### 6.1 Implementiert

| Feature | Implementierung | Referenz |
|---------|-----------------|----------|
| Entry fill | `USER_PENDING_LIMIT_V1` / diagnostic limit touch | `entry_order_simulator.py` |
| Exit SL/TP | 5m H/L bracket | `exit_simulator.py` |
| SL+TP same bar | Ambiguous, **no** price / PnL | `exit_simulator.py` L50–61 |
| Open at end | `POSITION_OPEN_AT_DATA_END` | `exit_simulator.py` L84–92 |
| Fees | **Round-trip %** subtracted from gross (`roundtrip_fees_pct` **0.11**) | `strategy_config_v2.json`; `simulate_exit` |
| PnL per trade | **gross_pnl_pct**, **net_pnl_pct** only | `engine.py` L272–273 |
| Max positions | **1** | `engine.py` |
| `performance_validated` flag | Requires approved execution + clean exit | `engine.py` L232–236 |
| Trade ledger CSV | `simulated_trades.csv` (prices, %, reasons) | `results/phase4d_.../` |

### 6.2 Nur in Config / nicht implementiert

| Gefordertes Feld | Status |
|------------------|--------|
| `total_trades`, `winning_trades`, `losing_trades`, `win_rate` | **UNRESOLVED** — nicht aggregiert (manuell aus CSV möglich) |
| `gross_pnl_usdt`, `net_pnl_usdt`, `ending_equity_usdt` | **UNRESOLVED** — keine Positionsgröße / kein Startkapital |
| `trading_fees_usdt`, `slippage_cost_usdt`, `funding_fees_usdt` | **UNRESOLVED** — nur % fee; `slippage_modeled: false`, `funding_modeled: false` |
| `total_return_pct`, `max_drawdown_pct`, `profit_factor` | **UNRESOLVED** |
| Equity curve | **UNRESOLVED** |
| Leverage / margin | **UNRESOLVED** |
| Multi-coin capital binding | **UNRESOLVED** |
| Maker/taker split | **UNRESOLVED** |
| Run-Manifest mit Strategy+Density Provenance | **Teilweise** (4D `config_hash`; **kein** BT-D4-Binding) |

**Referenz-Lauf:** `results/phase4d_pool_landscape_backtester_v2/backtest_summary.md` — 8 `performance_validated` trades im aktuellen Stand; **kein** Portfolio-PnL-Report.

---

## 7. Fehlende Parameter (PnL + Strategie)

| Parameter | Status |
|-----------|--------|
| Startkapital USDT | UNRESOLVED |
| Positionsgröße / Risiko pro Trade | UNRESOLVED |
| Leverage / Margin-Modell | UNRESOLVED |
| Slippage Modell | Config false, UNRESOLVED |
| Funding (Perp) | Config false, UNRESOLVED |
| Dense-TP-Cluster Algorithmus | OPEN O-01 |
| Upper relevance + large-gap gate | OPEN O-05, O-06 |
| Intrabar SL-vs-TP priority user sign-off | OPEN O-11 |
| `FINAL_APPROVED` freeze manifest | **Missing** |

---

## 8. Realistische Backtest-Semantik (Teil E)

| Frage | Befund |
|-------|--------|
| Signal nach 5m close? | Ja — 2A scannt abgeschlossene Kerze; Order ab `entry_open_utc` / `order_active_from_utc`. |
| Fill-Regel eingefroren? | **PENDING LIMIT V1** USER_CONFIRMED; 4D implementiert mit 3A-Lifecycle; technische Pool-Storno-Details **offen** (L591). |
| TP/SL kausal? | Ja — nur Bars ≥ fill; Pools aus Vorsignal-`landscape_intervals` (nicht live refresh pro Bar außer Zone-Tracker). |
| Lookahead? | Verboten in SSOT; **Risiko** wenn `landscape_intervals` aus späterem Fenster stammen — BT-D4 schließt das für **neue** Pipeline. |
| Gebühren | 0.11% round-trip **confirmed**; keine USDT-Umrechnung. |
| Multi-coin später | UNRESOLVED — 4D single-symbol pilot. |

---

## 9. Minimaler Implementierungsplan (nach Freigaben)

1. **Freeze-Gate:** User `FINAL_APPROVED` auf `pool_landscape_v1_candidate` oder explizites Amendment **4D-TP-01** (nearest depth-2 vs dense cluster).
2. **Density:** Pro Close `ResearchDensityRunV1` — **keine** zweite Density; Signale aus BT-D4-Bändern neu erzeugen oder validieren.
3. **Strategie unverändert:** `strategy_rules.py` + 4E pending **nur** wenn Semantik approved; sonst STOP.
4. **Lifecycle:** 3A-CSV → Close-keyed Zone-Status aus BT-D4 + bestehende 4E-Regeln, oder Phase-4T-Timeline.
5. **PnL-Schicht (neu, isoliert):** Ledger → USDT aus **deklariertem** `starting_equity_usdt` + `position_size_rule`; aggregiere geforderte Metriken; Manifest mit `density_contract` + `strategy_freeze_ref` + `pipeline_line_id`.
6. **Pilot:** 1 Symbol, 2–5 Tage published SSOT, deterministischer Manifest-Hash.
7. **Skalierung:** 5 Monate DOGE; dann 20–50 Coins — **Precalc** Close→Bands-Cache (kausal keyed), ~6.8 s/Close × Bars beachten.

**Keine** Ausführung in BT-D5.

---

## 10. Risiken & offene Entscheidungen

| ID | Risiko |
|----|--------|
| R-01 | `NOT_FINAL_APPROVED` — kein produktiver Backtest ohne Freeze |
| R-02 | TP dense cluster vs 4D nearest band — **material PnL impact** |
| R-03 | Large-gap / relevance nicht gescannt — S5/S6-Fälle |
| R-04 | 2A Defaults (0.05%, cooldown 6, max 20) nicht user-frozen |
| R-05 | PnL nur % — **BACKTESTER_PNL_BLOCKED** für Gesamt-USDT |
| R-06 | Phase 3B Longs in 4D — **experimental**, nicht Short-Freeze |
| R-07 | Semantik-Änderung ≠ technischer Fix — **User-Freigabe** vor Adapter |

---

## 11. Verdict-Einordnung

| Option | Erfüllt? |
|--------|----------|
| `FROZEN_SHORT_NOT_FOUND` | **Nein** — Kandidat klar identifiziert |
| `FROZEN_SHORT_READY_FOR_DEPTH2_PNL_INTEGRATION` | **Nein** — Semantik-Lücken + kein Full-PnL |
| `BACKTESTER_PNL_BLOCKED` | **Ja** (Teil D) |
| **`FROZEN_SHORT_SEMANTIC_APPROVAL_REQUIRED`** | **Ja** — primäres Gate |

---

**STOP** — Read-only Audit abgeschlossen.
