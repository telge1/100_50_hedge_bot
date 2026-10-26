# PHASE BT-D4 — Enforced Depth-2 Backtester Data Integration V1

**Date:** 2026-10-25  
**Mode:** Isolated research integration only. No trades, no Phase 4D engine changes, no CH writes.

---

## VERDICT

### `BACKTESTER_DEPTH2_INTEGRATION_PASS`

Der **Research-Modus** `depth2_density_research_v1` bindet den BT-D3-Provider **zwingend** über einen einzigen Gateway-Pfad an. Sep-25-Golden (39/176/215) ist **End-to-End** über `ResearchDensityRunV1.collect_at_close` verifiziert. Phase 4D-Unit-Tests und `strategy_config_v2.json` bleiben unverändert.

---

## 1. Implementierte Dateien

| Pfad | Rolle |
|------|--------|
| `tools/phase_bt_d4_enforced_depth2_backtester_v1/research_config_v1.json` | Erzwungene Research-Config |
| `tools/phase_bt_d4_enforced_depth2_backtester_v1/config_enforcement_v1.py` | Start- + Contract-Validierung (fail-closed) |
| `tools/phase_bt_d4_enforced_depth2_backtester_v1/research_density_gateway_v1.py` | **Einziger** Pfad zu `get_density_bands` |
| `tools/phase_bt_d4_enforced_depth2_backtester_v1/research_backtester_v1.py` | `ResearchDensityRunV1` — isolierter Research-Backtester |
| `tools/phase_bt_d4_enforced_depth2_backtester_v1/integration_verification_v1.py` | Golden-Vergleich |
| `tools/phase_bt_d4_enforced_depth2_backtester_v1/run_research_integration.py` | Integrationslauf + Report |
| `tools/phase_bt_d4_enforced_depth2_backtester_v1/tests/` | 12 pytest |
| `results/phase_bt_d4_enforced_depth2_backtester_v1/integration_report.json` | Gate-Ergebnis |
| `results/phase_bt_d4_enforced_depth2_backtester_v1/<run_id>/run_manifest.json` | Lauf-Manifest |
| `results/phase_bt_d4_enforced_depth2_backtester_v1/<run_id>/density_snapshots.json` | Pro-Close-Bänder |

**Unverändert:** `phase4d_pool_landscape_backtester_v2/*`, 2A/3A/3B, Chart/SSOT, Short V2, BT-D3-Provider-Logik.

---

## 2. Architektur (erzwungen)

```
research_config_v1.json
    → ResearchBacktesterConfigV1.enforce_contract()
    → ResearchDensityRunV1.collect_at_close()
        → research_density_gateway_v1.fetch_density_bands_enforced()
            → density_provider_v1.get_density_bands()  [BT-D3, keine zweite Density]
                → build_landscape_window + analyzer_depth2_landscape.js
```

- **Kein** direkter Provider-Aufruf in Produktions-4D.
- **Kein** Fallback auf `landscape_intervals` oder Einzelpools.
- **Kein** `min_depth` in JSON ohne Enforcement — Research-Modus nutzt **`min_unique_origins=2`** mit Prüfung in Config + Gateway + Band-Scan.

---

## 3. Config enforcement

Erzwungen bei Load und vor jedem Fetch:

| Feld | Wert |
|------|------|
| `timeframe` | `5m` |
| `pool_detail` | `1` |
| `density_method` | `exact_unique_origin` |
| `min_unique_origins` | `2` |
| `expected_pipeline_line_id` | `historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1` |

**Fail-closed:** Depth 1/3, fehlende Keys, falsche Pipeline, fehlende Provenance (`pipeline_line_id` / `config_hash`), unpublished Close → Abbruch mit `ResearchConfigError` / `ResearchDensityGatewayError`.

---

## 4. Data contract (pro Decision-Close)

`snapshot_payload` / `density_snapshots.json` je Close:

- `upper_bands` / `lower_bands` (vollständige Depth-2-Felder)
- `lower_price`, `upper_price`, `unique_origin_count`, `origin_ids`, `as_of_utc`, `pool_status`
- `pipeline_line_id`, `config_hash`, `data_provenance`, `adapter_path`
- **Keine** Entry/SL/TP/Handelsfilter

---

## 5. End-to-End Parität

**Golden (kanonisch):**  
`results/phase_bt_d3_canonical_depth2_provider_v1/golden_20260925_1100_chart_path.json`  
**Nicht** `phase_density_chart_parity_.../pools.json` (218 / Python-Sweep).

| `2026-09-25T11:00:00Z` | Upper | Lower | Total |
|------------------------|------:|------:|------:|
| Erwartet / Golden | 39 | 176 | 215 |
| Über Research-Modus | 39 | 176 | 215 |

- **215/215** Kanten + Origin-Mengen **exakt** (`field_mismatch_total: 0`).
- Zusätzlich **5** May–Sep-Closes über `collect_at_close` (nicht Bypass-Skript).
- Integrationstest `test_e2e_sep25_through_research_backtester_not_direct_provider` erzwingt den Modus-Pfad.

**Ausführen:**

```bash
export HISTORICAL_LMV2_BATCH_READER=1
python tools/phase_bt_d4_enforced_depth2_backtester_v1/run_research_integration.py
python -m pytest tools/phase_bt_d4_enforced_depth2_backtester_v1/tests -q
```

---

## 6. Kausalität

- Pro Run: jeder `decision_close` maximal **einmal** (`CAUSAL_STATE_LEAK` sonst).
- Sequenz `10:40` … `11:00`: **5** unterschiedliche Snapshots.
- SSOT + `poolActiveAt` nur bis `as_of`; kein Lookahead im Provider.

---

## 7. Manifest (Beispiel)

`results/phase_bt_d4_enforced_depth2_backtester_v1/20261025T093055Z/run_manifest.json`:

- `research_mode`, `min_unique_origins: 2`, `exact_unique_origin_density: true`
- `adapter_bridge`, `density_provider_module`, `golden_reference_path`
- `expected_pipeline_line_id`, `decision_closes_collected`, `run_manifest_hash`
- `integration_path`: dokumentierter Gateway-String

---

## 8. Performance (Integrationslauf, 10 Closes)

| Metrik | Wert |
|--------|------|
| Total | **~68.2 s** |
| Mean / Close | **~6.8 s** |
| Sep-25 Fixture | **~5.8 s** |

**Read-only Skalierung:** Einmalige kausale Vorberechnung pro Symbol/Monat (Close-keyed Cache der **Snapshots**, nicht gemischte Fenster) wäre möglich, ohne den JS-Adapter zu duplizieren. 20–50 Coins × ~6.8 s/Close × Bars erfordert Batch/Cache in einer späteren Phase — **nicht** in BT-D4 implementiert.

---

## 9. Tests

| Test | Status |
|------|--------|
| Config Depth 1 / 3 → FAIL | PASS |
| Fehlender Contract-Key → FAIL | PASS |
| Falsche Pipeline → FAIL | PASS |
| Unpublished Close → FAIL | PASS |
| Sep-25 Golden E2E via Research-Modus | PASS |
| 5 zusätzliche Closes | PASS |
| Kausal-Sequenz | PASS |
| Determinismus | PASS |
| Phase 4D Unit (`-k not integration`) | PASS |
| `strategy_config_v2.json` ohne `density_method` | PASS (unverändert) |

**pytest:** 12 passed (~91 s mit SSOT).

---

## 10. Bekannte Einschränkungen

1. Research-Modus ist **separat** — Phase 4D `run_phase4d_backtest.py` ruft den Provider **nicht** auf.
2. `min_depth: 2` in 4D-JSON bleibt historisch; **nicht** dasselbe wie erzwungenes `min_unique_origins` im Research-Modus.
3. Hook `BT_D3_DENSITY_PROVIDER_HOOK` bleibt unabhängig und standardmäßig aus.

---

## 11. Nächster minimaler Schritt (nach Freigabe)

Optional Phase-4D-**Fork** oder neuer Runner, der **nur** im explizit gesetzten Research-Flag `ResearchDensityRunV1` für Signal-**Validierung** nutzt — weiterhin ohne Änderung an Entry/SL/TP-Regeln.

---

**STOP** — isolierte Integrationstests abgeschlossen; kein produktiver Strategie-Backtest.
