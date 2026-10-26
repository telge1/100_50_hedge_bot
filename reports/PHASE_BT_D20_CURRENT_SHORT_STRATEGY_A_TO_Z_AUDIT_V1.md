# PHASE BT-D20 — Complete Current SHORT Strategy Logic Audit

**Datum:** 2026-10-25  
**Projekt:** `pools+ob+delta_bot`  
**Verdict:** `CURRENT_STRATEGY_FULLY_MAPPED`  
**Modus:** Read-only — kein Code-, Config-, Backtest- oder Dashboard-Change.

**Artefakte:** `results/phase_bt_d20_current_strategy_audit_v1/`  
(`strategy_rules.json`, `active_config_resolved.json`, `codepath_map.json`, `original_vs_implemented.md`, `two_signal_walkthrough.md`)

---

## 1. Strategie-Identität

| Frage | Antwort (aus Code + Config) |
|-------|-----------------------------|
| **Welche Strategieklasse läuft?** | **Pool Landscape SHORT — BT-D7 Research Engine** (`run_short_research_backtest`), angebunden über **BT-D12 CLI** (`run_simple_backtest.py`). Kein `phase4d/engine.py`-Hauptloop im Produktionspfad. |
| **Signal-Engine** | `tools/phase_bt_d7_config_driven_short_pnl_integration_v1/signal_engine_v1.py` → `detect_short_signal` |
| **Aktive Version (Config)** | `strategy_version: bt_d9_first_24h_v1`, `strategy_id: pool_landscape_v1_candidate`, `research_mode: depth2_density_research_v1` |
| **Freeze-Referenz** | `strategy_freeze/pool_landscape_v1_candidate/SOURCE_PROVENANCE.json` (verweist u. a. auf `Signal_definierung.md`, Phase 2A/4A/4D — **Referenz**, nicht automatisch Code) |
| **LMV2 Short V2** | **Separate** Strategie im Dashboard-Freeze — **nicht** dieser Pfad (explizit in SOURCE_PROVENANCE ausgeschlossen). |

### Klassifikation (operativ)

| Bereich | Status |
|---------|--------|
| 5m, PD1, exact_unique_origin, min_unique_origins=2 | **FROZEN** (Validator) |
| `nearest_upper_proximity_v1` Poolwahl | **EXPERIMENTAL** (im Code-String + Registry) |
| `next_eligible_band` TP | **CONFIGURABLE**; Freeze-Text oft **EXPERIMENTAL** |
| `dense_cluster` TP | **UNRESOLVED** (Validator gate; Engine **nicht** ausführbar) |
| `pending_limit_fill_model` | **CONFIGURABLE** (BT-D18; YAML: `causal_sell_limit_v2`) |
| Long YAML | **NOT_IMPLEMENTED** |

### Dokumentation vs Code (Auszug)

| Dokument | Passt zu Code? |
|----------|----------------|
| `Signal_definierung.md` | **Teilweise** — Gap/SL/TP-Richtung ja; Entry-Pool „user relevant“ vs **nearest proximity** nein |
| Phase 4D `strategy_rules.py` | **Ja** für SL/TP-Math (von BT-D7 importiert) |
| Phase 2A Scanner | **Inspiration** für Gap; BT-D7 nutzt `gap_detection_v1`, nicht JS-Scanner direkt |
| `configs/backtester.yaml` | **Ja**, nach YAML→JSON-Mapping (`config_yaml_v1.py`) |

---

## 2. Daten und Pool-Landschaft

| Element | Implementierung |
|---------|-----------------|
| **Timeframe** | 5m (`backtest.execution_timeframe`, Density `timeframe: 5m`) |
| **PD** | 1 (`density.pool_detail`) |
| **Density-Methode** | `exact_unique_origin` |
| **Depth-Schwelle** | `min_unique_origins: 2` — Bänder mit `unique_origin_count` (≥2 im Snapshot) |
| **Upper/Lower** | Snapshot-Felder `upper_bands` / `lower_bands` je Close |
| **ACTIVE / Remaining** | Im **Cache-JSON** pro Segment: `pool_status: ACTIVE` etc.; **Engine-Cancel** nutzt nur **exakte** `lower_price|upper_price`-Paarität (`execution_v1.pool_still_active`) |
| **168h Kontext** | BT-D4-Provider: `warmup_hours` default **168** bei Live-Fetch; BT-D9-Läufe nutzen **`validated_cache`**-Bundle (voraggregierte Snapshots pro Close) |
| **As-of** | `density_session.collect_at_close(decision_close_utc)` — Stand **nach** Schluss der 5m-Kerze mit diesem **Close-Zeitstempel** |
| **OHLC** | `price_feed_v1.load_prices_from_ssot_window` — Keys = **Bar-CLOSE UTC** |
| **Provenance** | Cache: `bundle_manifest.json` + Einträge unter `cache_bundle/entries/`; Snapshots enthalten `pipeline_line_id`, `config_hash` |

### Begriffe

| Begriff | In Strategie |
|---------|--------------|
| **Einzelner Pool / Origin** | LLDV2-Origin-ID in `origin_ids` (Density-SSOT) |
| **Exact-Density-Segment** | Ein Eintrag in `upper_bands`/`lower_bands` mit `[lower_price, upper_price]` und Origins |
| **Density-Cluster (Union)** | In **Gap-Logik** (`union_intervals`) — **nicht** als Entry-Objekt; Entry = **ein** gewähltes Upper-Band |
| **TP-Ziel** | **Ein** Lower-Band (depth 2) via `select_short_tp_depth2` — **nicht** multi-band cluster merge |

---

## 3. Signal-Erzeugung (Filterkatalog)

Auswertung: **am Signal-Close** `bar`, Candle `prices[bar]`, Snapshot `collect_at_close(bar)`.

| ID | Bedingung | Formel / Logik | YAML (backtester) | Funktion | PASS | REJECT |
|----|-----------|----------------|-------------------|----------|------|--------|
| A | Poolwahl-Modus | `entry_pool_selection_mode == nearest_upper_proximity_v1` | `nearest_upper_proximity_v1` | `detect_short_signal` | Modus ok | `return None` |
| B | Mikro-Pools | Kein separater Min-Band-Breite-Filter | — | — | — | **NOT_IMPLEMENTED** |
| C | Mindestalter | `pool_age_bars[pk] >= pool_min_age_bars` | `0` | `detect_short_signal` | immer | wenn >0 und zu jung |
| D | Depth 2 | Band in Snapshot mit Origins (Provider filtert ≥2) | `min_unique_origins: 2` | Density-Provider | Band vorhanden | kein Band |
| E | Upper-Gap | Large gap mit `gap_bottom >= pool_upper` | `gap_min_pct: 0.5` | `has_upper_large_gap_above_pool` | true | false |
| F | Gap-Kanten | Zwischen **unionierten** Upper-Intervallen | `adjacent_union_interval_gap_pct_v1` | `compute_side_gaps` | large gap | — |
| G | Nächster Upper | **Nicht** Mindestabstand zum nächsten Upper — nur **Proximity + nearest high→pool_lo** | `pool_proximity_pct: 0.05` | `_proximity_ok` + Distanzsortierung | best band | keine Kandidaten |
| H | Preis vs Pool | Inside / touch / near_below (close unter Pool, nahe tol) | 0.05% | `_proximity_ok` | siehe Code | keine Nähe |
| I | Rejection | `close < open` | — | `_bearish_rejection` | bearish | nicht bearish |
| J | EMA | — | — | — | — | **NOT_IMPLEMENTED** |
| K | Weitere | `upper_gap_measurement_mode` muss exakt `adjacent_union_interval_gap_pct_v1` sein | fixed | `detect_short_signal` | match | `None` |

**Experimental-Tag im Signal:** `nearest_upper_proximity_v1_EXPERIMENTAL_NOT_FINAL_APPROVED`.

---

## 4. Signal-Timing

| Frage | Antwort |
|-------|---------|
| **5m-Close** | Zeitstempel `T` in `prices` = **Ende** der Kerze `[T-5m, T)`. |
| **Density** | Berechnet/gelesen für **diesen Close** `T`. |
| **Signal bestätigt** | Am Ende der Verarbeitung von Bar `T` (nach Snapshot + Candle). |
| **Signalkerze Touch** | **Erlaubt** — Proximity verlangt Touch/Nähe; **kein** Verbot vorheriger Berührung. |
| **Preistouch → Poolstatus** | **Nicht** im BT-D7-Signalpfad modelliert; Cancel nur über **Snapshot-Bandliste**. |
| **Signal trotz später inactive Pool** | **Ja** — Signal nur am Close `T`; Pending kann später canceln. |
| **Pending start** | `order_active_from_utc = signal_close + 5min` (`_next_5m_open`) |

### Demo 15:30–15:45 UTC (Fall A, SSOT)

| Close UTC | Open | High | Low | Close | Ereignis |
|-----------|------|------|-----|-------|----------|
| 15:30 | 0.08663 | 0.08681 | 0.08654 | 0.08681 | Touch Unterkante; kein Signal |
| **15:35** | 0.08681 | 0.08682 | 0.08655 | 0.08668 | **SIGNAL**, Pending |
| **15:40** | 0.08668 | 0.08671 | 0.08639 | 0.08639 | Order **aktiv**, kein Fill, **Cancel** (exact band weg) |
| 15:45 | … | … | … | … | Kein Pending mehr |

**Lookahead:** Signale nutzen nur `prices[bar]` und Snapshot@`bar` — **kein** Peek auf zukünftige Bars bei Detektion.

---

## 5. Entry-Preis

| Modus YAML | JSON `entry_price_reference` | Formel |
|------------|------------------------------|--------|
| `pool_lower_edge` | `upper_pool_lower_edge` | `pool_lo * (1 - offset/100)` |
| `pool_upper_edge` | `upper_pool_upper_edge` | `pool_hi` |
| `pool_lower_edge_offset` | `upper_pool_lower_edge` + `entry_offset_pct` | wie lower mit offset>0 |

**Aktive YAML:** `pool_lower_edge`, `entry_offset_pct: null` → offset 0.

**Tick-Rundung:** Keine explizite Tick-Runde — **float** aus Density-Preisen.

**Eingefroren:** `entry_level` im `ShortSignalV1` bei Signal; Pending nutzt **diesen** Wert (keine Neuberechnung pro Bar).

---

## 6. Pending-Order und Fill

| Thema | Verhalten |
|-------|-----------|
| **Ordertyp** | **Sell limit** (`pending_limit_enabled: true`) |
| **Aktiv** | Ab `order_active_from_utc` (verarbeitet auf Bar mit `bar >= active`, OHLC = diese Close-Bar) |
| **Dauer** | Bis Fill, `CANCELLED_POOL_INACTIVE`, oder Testende; **`pending_limit_expiry_bars` nicht in Engine** |
| **Pool-Deaktivierung** | Exakt-Match `pool_lower`/`pool_upper` fehlt im Snapshot |
| **Split/Merge** | Gleiche Origins, andere Grenzen → **inactive** für alte Kante |
| **Parallele Signale** | `pending is not None` → **keine** neuen Signale |
| **Position** | `max_concurrent_positions: 1`; offene Position unterdrückt Signale |
| **YAML Fill-Modell** | `causal_sell_limit_v2` (Default JSON ohne Feld: `legacy_v1`) |

### Fill-Formeln

**legacy_v1:** Fill iff `low ≤ limit ≤ high`, Preis = limit.

**causal_sell_limit_v2:**

1. `high < limit` → kein Fill  
2. `open ≥ limit` → Fill @ **open**  
3. `open < limit ≤ high` → Fill @ **limit**

**Priorität (BT-D18):** Auf aktiver Bar **zuerst Fill**, dann Pool-Cancel.

---

## 7. Stop-Loss

| Punkt | Wert |
|-------|------|
| Bezug | **Oberkante des Entry-Upper-Pools** (`pool_upper`) |
| Formel | `pool_upper * (1 + sl_above_upper_top_pct/100)` |
| YAML | `0.2` % |
| Abhängig von Entry-Offset? | **Nein** — nur `pool_upper` |
| Snapshot @ Signal | `stop_loss_price_planned` eingefroren |
| Nach Fill | Gleiche Formel mit `pending.pool_upper` |

---

## 8. Take-Profit

| Frage | Implementiert |
|-------|---------------|
| Lower-TP finden | `select_short_tp_depth2(entry, intervals, min_tp_pct)` |
| Nächster vs stärkster | **Nächster unter Entry:** unter allen Lower **depth=2** mit `top < entry` und Abstand ≥ `min_distance_pct` wird das Intervall mit **höchstem top** (nächstes Ziel unter Entry) gewählt |
| Dense Cluster | Config-Wert `dense_cluster` — **BT-D7 bricht ab** wenn nicht `next_eligible_band` |
| Depth | `depth == 2` in Intervallen (`bands_adapter` setzt depth aus `unique_origin_count`) |
| Mindestdistanz | `0.5` % von **Entry/Fill** zur **Oberkante** des Lower-Zielbands (`top`) |
| Zwischenpools | Kein explizites „Skip“ — nur depth-2-Liste |
| Qualitätsfilter | `tp_pool_quality_gate_enabled: false` |
| TP-Preis | **Top** des gewählten Lower-Bands |
| @ Signal | `build_signal_snapshot` mit `entry_level` |
| @ Fill | `_process_entry_fill` mit **Fill-Preis** und Snapshot **vom Signal** (`pending_snapshot_at_signal`) |
| Ohne TP | Order-Status `NO_TP` — **kein** Trade |

**Abweichung Nutzerziel „dichter Lower-Cluster“:** Nicht implementiert — siehe `original_vs_implemented.md`.

---

## 9. Position, Exit, PnL (YAML-wirksam)

| Parameter | YAML-Wert |
|-----------|-----------|
| `initial_equity_usdt` | 10000 |
| `sizing_mode` | `fixed_notional_usdt` |
| `position_notional_usdt` | 1000 |
| `leverage` | 1.0 |
| `roundtrip_pct` | 0.11 % |
| Slippage | `none` |
| Funding | `none` |
| Intrabar SL+TP | `no_trade_ambiguous` |
| EOT offen | `exclude_from_realized_pnl` |

**Exit:** `simulate_short_exit` — pro Bar nach Entry: SL wenn `high ≥ sl`, TP wenn `low ≤ tp`; beides → ambiguous policy.

**PnL:** `aggregate_pnl` — Netto nach Fees; Drawdown/Winrate in Rückgabe-Dict.

---

## 10. Fallbeispiele BT-D19

Siehe `two_signal_walkthrough.md` und `results/phase_bt_d19_complete_two_short_forensics_v1/`.

---

## 11. Strategie-Contract (Kurztabelle)

| Regel | Wert (YAML) | Code | Status |
|-------|-------------|------|--------|
| Entry-Pool | nearest_upper_proximity_v1 | `signal_engine_v1` | EXPERIMENTAL |
| Upper-Gap | 0.5 % adjacent union | `gap_detection_v1` | IMPLEMENTED |
| Rejection | c < o | `_bearish_rejection` | PARTIAL vs Doc |
| Pool-Cancel | exact band | `pool_still_active` | IMPLEMENTED |
| Pending | sell limit, +5m | `short_research_engine_v1` | IMPLEMENTED |
| Fill | causal_v2 in YAML | `sell_limit_fill_v1` | IMPLEMENTED |
| SL | 0.2 % über upper top | `short_sl` | IMPLEMENTED |
| TP | next_eligible_band → depth2 nearest lower top | `select_short_tp_depth2` | PARTIAL vs dense cluster doc |
| TTL pending | null | — | MISSING in engine |
| EMA | — | — | NOT_IMPLEMENTED |

---

## 12. Offene Fragen (A–F)

**A. Was handeln wir?**  
Config-gesteuerte **Pool-Landscape-Short-Research**-Engine (BT-D7), nicht LMV2 V2, nicht Phase-4D-Vollbacktester.

**B. Gewollt, aber anders?**  
User-relevante Poolwahl; dense-cluster TP; reichere Rejection; ggf. Market-Entry nach Touch — **nicht** dieser Code.

**C. Trade-Blocker?**  
Sell-limit unter Markt ohne marketable Open; strict pool identity cancel; single pending; no fill ohne high≥limit (legacy).

**D. Signale nach Abprall?**  
**Ja** — Proximity erlaubt Touch in Signalkerze; Entry erst **nächste** Bar; Kurs kann schon unter Limit sein (Fall A).

**E. Pool-Semantik unklar?**  
Chart-ACTIVE vs exact `lower|upper`; Re-Segmentierung vs PRICE_TOUCH; **168h-Remaining** vs Cancel-Test.

**F. Vor Groß-Backtest entscheiden?**  
Fill-Modell Default; Pool-Identity für Cancel; TP-Algorithmus (dense vs nearest); Entry-Poolwahl final; pending TTL; 1m-Intrabar.

---

## STOP

Keine Empfehlung als implementierte Regel. Keine weiteren Läufe in dieser Phase.
