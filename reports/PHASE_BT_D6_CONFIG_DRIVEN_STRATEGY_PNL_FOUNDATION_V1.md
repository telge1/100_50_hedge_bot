# PHASE BT-D6 — Config-Driven Frozen Short Strategy & PnL Foundation V1

**Date:** 2026-10-25  
**Mode:** New isolated config + PnL foundation only. No Phase 4D edits, no market backtests, no freeze final approval.

---

## VERDICT

### `CONFIG_PNL_FOUNDATION_PASS`

JSON-Schema + fail-closed Validator, Baseline-Config, Parameter-Registry, isolierter **synthetischer** PnL-Aggregator, Experiment-Ordner mit Config-Hash und Manifest sind implementiert und getestet (**11 pytest** + Smoke).

**Bewusst PARTIAL (kein Blocker für Foundation):** Strategie-**Ausführung** (Signale, Fills, BT-D4-Anbindung) folgt in späterer Phase — BT-D6 liefert nur Config + PnL-Kern.

---

## 1. Artefakte

| Pfad | Rolle |
|------|--------|
| `tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1/schema/bt_d6_strategy_backtest_config_v1.schema.json` | JSON-Schema (Dokumentation + Struktur) |
| `tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1/configs/pool_landscape_baseline_v1.json` | Beispiel-Baseline (Freeze-Kandidat abgebildet) |
| `tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1/parameter_registry_v1.json` | FROZEN / CONFIGURABLE / UNRESOLVED / EXPERIMENTAL |
| `tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1/config_validate_v1.py` | Fail-closed Python-Validator |
| `tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1/pnl_aggregator_v1.py` | Gesamt-PnL nur für **SyntheticTrade** |
| `tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1/manifest_v1.py` | `config_hash` + Run-Manifest |
| `tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1/experiment_v1.py` | Immutable Config-Kopie + Ergebnisordner |
| `tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1/run_foundation_checks.py` | Smoke |
| `results/phase_bt_d6_config_driven_strategy_pnl_foundation_v1/` | Smoke-Outputs |
| `results/phase_bt_d6_config_experiments_v1/` | Experiment-Stubs |

**Unverändert:** `phase4d_pool_landscape_backtester_v2/`, Original Short V2, Chart/SSOT, `strategy_freeze/*` Inhalte.

---

## 2. Config-Schema (Abschnitte)

### 2.1 `strategy_identity`
- `strategy_id`, `strategy_version`, `frozen_source_reference`, `research_mode`

### 2.2 `density` — **V1 verbindlich, fail-closed**
| Feld | Wert |
|------|------|
| timeframe | `5m` |
| pool_detail | `1` |
| method | `exact_unique_origin` |
| min_unique_origins | `2` |

### 2.3 `short_entry`
- `upper_gap_min_pct` — **CONFIGURABLE** (Baseline **0.5** per `Signal_definierung.md` L365)
- `upper_gap_measurement_mode` — nur `adjacent_union_interval_gap_pct_v1` (entspricht `computeGaps` / benachbarte unionierte Depth-2-Intervalle, `gap_pct = (next_bottom - prev_top)/prev_top*100`)
- Weitere Felder: Proximity, Pending-Limit, Pool-Alter, Selection-Mode — siehe Registry

### 2.4 `risk` / `take_profit`
- SL **0.2%**, max 1 Position
- TP: `next_eligible_band` (4D) oder `dense_cluster` nur mit `dense_cluster_semantics_approved: true` (sonst **FAIL**)

### 2.5 `pnl` — **keine stillen Defaults im Loader**
Explizit: `initial_equity_usdt`, `sizing_mode`, Notional **oder** Risk-Fraction, `leverage`, Fee/Slippage/Funding-Modelle, Intrabar- und EOT-Policies.

### 2.6 `backtest`
- Symbole, UTC-Fenster, `execution_timeframe=5m`, `outcome_blind_signals`, `deterministic_seed`

---

## 3. Validierung (fail-closed)

| Check | Verhalten |
|-------|-----------|
| Unbekannte Keys | `UNKNOWN_CONFIG_KEYS` |
| Fehlende Pflichtfelder | `MISSING_REQUIRED_KEYS` |
| Density ≠ V1-Contract | `DENSITY_*_LOCKED` |
| `dense_cluster` ohne Approval | `DENSE_CLUSTER_NOT_APPROVED` |
| `user_relevant_upper_unresolved` | `ENTRY_SELECTION_UNRESOLVED` |
| Kapital ohne Notional/Risk | `MISSING_*` |
| Unbekanntes Gap-Measurement | `GAP_MEASUREMENT_NOT_EXECUTABLE` |

```bash
python -m pytest tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1/tests -q
python tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1/run_foundation_checks.py
```

**Baseline config hash (Smoke):** `a2fa78fe739aa3c950528380b502b865c403d86fcd10433b2dd63b5a03dd97ff`

---

## 4. Strategie-Abgleich (Auszug Registry)

| Parameter | Klassifikation |
|-----------|----------------|
| Density 5m / PD1 / exact / depth 2 | **FROZEN** |
| `upper_gap_min_pct` | **CONFIGURABLE_APPROVED** |
| `upper_gap_measurement_mode` (v1) | **CONFIGURABLE_APPROVED** (dokumentierte Semantik) |
| `entry_pool_selection_mode` (nearest proximity) | **EXPERIMENTAL** |
| `pool_min_age_bars` hard gate | **UNRESOLVED** (O-04) |
| `take_profit.dense_cluster` | **UNRESOLVED** (O-01) — Config blockiert ohne Approval-Flag |
| `sl_above_upper_top_pct` 0.2 | **FROZEN** |
| `min_distance_pct` 0.5 | **FROZEN** |
| Fees 0.11% roundtrip | **FROZEN** (in Config explizit) |
| `initial_equity_usdt` | **CONFIGURABLE_APPROVED** (muss gesetzt sein) |

Vollständig: `parameter_registry_v1.json`. **Kein** `FINAL_APPROVED` des Freeze-Kandidaten.

---

## 5. PnL-Kern (synthetisch)

`aggregate_pnl(config, trades)` liefert:

- `total_trades`, `winning_trades`, `losing_trades`, `win_rate`
- `gross_pnl_usdt`, `trading_fees_usdt`, `slippage_cost_usdt`, `funding_fees_usdt`, `net_pnl_usdt`
- `total_return_pct`, `max_drawdown_pct`, `profit_factor`, `ending_equity_usdt`
- `equity_curve`, `trade_ledger`
- `synthetic_only: true`

**Keine** Behauptung realer Markt-Backtests. Beispiel: `results/phase_bt_d6_config_driven_strategy_pnl_foundation_v1/synthetic_pnl_summary.json`

---

## 6. Experiment-Infrastruktur

- `prepare_experiment(config, name)` → `results/phase_bt_d6_config_experiments_v1/<name>_<hash>_<ts>/`
  - `config.immutable.json`
  - `run_manifest.json` mit `config_hash`
- `configs/experiments/README.md` — Hinweise für spätere `upper_gap_min_pct` / SL / TP-Vergleiche **ohne** Codeänderung
- **Kein** Sweep in BT-D6

---

## 7. Tests

| Test | Ergebnis |
|------|----------|
| Baseline load | PASS |
| Depth 1 / unknown keys / missing equity | FAIL as expected |
| Gap nur via JSON-Wert | PASS |
| dense_cluster gate | PASS |
| Synthetic PnL + fees/slippage/funding | PASS |
| Manifest hash determinism | PASS |
| Phase 4D unit regression | PASS |

---

## 8. Nächste Schritte (nach Freigabe, nicht BT-D6)

1. Runner liest **nur** validierte JSON-Config (kein Hardcode in Strategiepfad).
2. BT-D4 `collect_at_close` + Config-`upper_gap_min_pct` für Gap-Filter (bestehende JS-`computeGaps`-Semantik).
3. Phase-4D-**Fork** oder neuer Engine-Wrapper — **ohne** Änderung an `phase4d` Original.
4. Echte Trades → `SyntheticTrade`-äquivalentes Ledger → `aggregate_pnl`.

---

## 9. Risiken

- Baseline mappt **2A-Defaults** (Proximity 0.05) — EXPERIMENTAL, nicht user-frozen.
- `dense_cluster` bleibt gesperrt bis User O-01 schließt.
- Gap-Mode v1 deckt **Signal_definierung** ab; weitere Modi → neues Schema + Freigabe.

---

**STOP** — Config-/PnL-Foundation und Bericht abgeschlossen.
