# PHASE BT-D17 — Configurable Short Entry Edge / Offset via YAML

**Datum:** 2026-10-25  
**Projekt:** `pools+ob+delta_bot`  
**Verdict:** `SHORT_ENTRY_YAML_PASS`

---

## Ziel

Short-Entry-Preis vollständig über `configs/backtester.yaml` (bzw. BT-D6-JSON) steuern — ohne spätere Codeänderung bei Modus-/Offset-Wechsel.

## Implementierung

| Komponente | Änderung |
|------------|----------|
| `entry_price_v1.py` | `compute_short_entry_level(pool_lo, pool_hi, short_entry)` |
| `signal_engine_v1.py` | `entry_level` aus Config, nicht mehr fest `pool_lo` |
| `signal_snapshot_v1.py` | `entry_price_reference`, `entry_offset_pct` im Snapshot |
| `config_validate_v1.py` | `entry_price_reference`: `upper_pool_lower_edge` \| `upper_pool_upper_edge`; optionales `entry_offset_pct` ≥ 0; Offset nur mit Lower-Edge-Referenz |
| `config_yaml_v1.py` | `entry_price_mode` (+ Alias `entry_mode`): `pool_lower_edge`, `pool_upper_edge`, `pool_lower_edge_offset` |
| Schema + `parameter_registry_v1.json` | BT-D17-Felder dokumentiert |
| `run_simple_backtest.py` | `--no-publish-latest` für Pilotläufe |

### Formeln (SHORT)

| Modus YAML | JSON `entry_price_reference` | Entry |
|------------|------------------------------|--------|
| `pool_lower_edge` | `upper_pool_lower_edge` | `pool_lo` |
| `pool_upper_edge` | `upper_pool_upper_edge` | `pool_hi` |
| `pool_lower_edge_offset` | `upper_pool_lower_edge` + `entry_offset_pct` | `pool_lo * (1 - offset/100)` |

Beispiel Offset 0,1 %: `0.086810 → 0.08672319` (Unit-Test).

**SL:** unverändert `short_sl(pool_upper, sl_above_upper_top_pct)` — unabhängig vom Entry-Modus.  
**TP:** `select_short_tp_depth2(entry, intervals, min_distance_pct)` — Mindestdistanz vom **tatsächlichen Entry** (Fill-/Plan-Preis).

## Fail-Closed (YAML / JSON)

- `pool_lower_edge` / `pool_upper_edge` mit `entry_offset_pct ≠ 0` → `ENTRY_MODE_OFFSET_CONFLICT`
- `upper_pool_upper_edge` mit Offset → `ENTRY_OFFSET_REFERENCE_MISMATCH`
- Widerspruch `entry_price_mode` vs. `entry_mode` → `ENTRY_MODE_CONFLICT`
- Long `enabled: true` → unverändert blockiert

## Execution / marketable Sell-Limit

**Unveränderte Fill-Regel (BT-D7):** Pending ab `order_active_from_utc`; Fill wenn `low ≤ entry ≤ high` auf einer Bar; Pool-Cancel vor Fill wie bisher.

**Nicht implementiert (separater Fix nötig):** explizites „sofort marketable“ beim Aktivierungs-Open, wenn Sell-Limit unter dem Markt liegt, aber die Bar das Limit nicht bracketet. Unit-Test `test_marketable_sell_limit_no_bracket_no_fill` dokumentiert das Verhalten.

Cancel-vs-Fill-Priorität wurde **nicht** geändert.

## Tests

```bash
pytest tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1/tests/test_config_yaml_v1.py \
       tools/phase_bt_d7_config_driven_short_pnl_integration_v1/tests/test_entry_price_v1.py \
       tools/phase_bt_d7_config_driven_short_pnl_integration_v1/tests/test_bt_d7_integration_v1.py \
       tools/phase_bt_d12_simple_config_latest_json_overlay_v1/tests/test_result_publish_v1.py
```

**27 passed** — Baseline-Parität YAML≡JSON (BT-D9-Hash), Modi, Offset-Validierung, Synthetic-Engine, Publish ohne `latest`.

## Pilot (BT-D9 24h Cache, `--no-publish-latest`)

Configs: `configs/pilot_bt_d17/*.yaml`  
Artefakte: `results/phase_bt_d17_configurable_short_entry_yaml_v1/pilot_*_result.json`

| Variante | Signale | Trades | Entry-Beispiel 15:35 UTC |
|----------|---------|--------|---------------------------|
| A `pool_lower_edge` | 3 | 0 | 0.08681 (= Baseline) |
| B `pool_upper_edge` | 3 | 0 | 0.086838 |
| C `pool_lower_edge_offset` 0,1 % | 3 | 0 | 0.08672319 |

SL/TP geplant pro Signal unverändert zur Baseline (SL an Pool-Oberkante, TP-Band gleich); nur `entry_price_planned` / `entry_level` variieren.

`latest_result.json` wurde durch Pilotläufe **nicht** überschrieben.

## Baseline

`configs/backtester.yaml` mit `entry_price_mode: pool_lower_edge` und `entry_offset_pct: null` — identisch zu BT-D9-JSON (Hash `0f512e5d…d0667`).

## STOP

Kein Dashboard, kein SSOT/Density-Change, keine Long-Strategie, keine Parameteroptimierung.
