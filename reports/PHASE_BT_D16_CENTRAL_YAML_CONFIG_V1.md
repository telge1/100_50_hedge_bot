# PHASE BT-D16 — Central Commented YAML Backtester Config

**Datum:** 2026-10-25  
**Projekt:** `pools+ob+delta_bot`  
**Verdict:** `CENTRAL_YAML_CONFIG_PASS`

---

## Ziel

Eine zentrale, deutsch kommentierte `configs/backtester.yaml` als Frontend für die bestehende BT-D6-JSON-Konfiguration — ohne Strategie-, Density- oder Dashboard-Änderungen.

## Lieferumfang

| Artefakt | Zweck |
|----------|--------|
| `configs/backtester.yaml` | Zentrale Einstellungen inkl. deutscher Kommentare (FROZEN / CONFIGURABLE / EXPERIMENTAL) |
| `tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1/config_yaml_v1.py` | Safe YAML → normalisierte BT-D6-Config, Provenance + Hash |
| `config_validate_v1.py` | `.yaml`/`.yml` via `load_and_validate_config()` |
| `run_simple_backtest.py` | `--config configs/backtester.yaml`; schreibt `config.normalized.json` + `config.provenance.json` im Run-Ordner |
| `tests/test_config_yaml_v1.py` | Parser-/Validator-Tests ohne Density-Neuberechnung |

## Startbefehl

```bash
python tools/phase_bt_d12_simple_config_latest_json_overlay_v1/run_simple_backtest.py \
  --config configs/backtester.yaml
```

JSON bleibt unverändert nutzbar:

```bash
python tools/phase_bt_d12_simple_config_latest_json_overlay_v1/run_simple_backtest.py \
  --config tools/phase_bt_d9_first_24h_short_pnl_v1/configs/bt_d9_24h_v1.json
```

## Pipeline (Fail-Closed)

1. YAML mit `yaml.SafeLoader` + `UniqueKeyLoader` (doppelte Keys → `DUPLICATE_YAML_KEY`)
2. Unbekannte Top-Level-Keys → `UNKNOWN_YAML_KEYS`
3. Mapping auf BT-D6-Felder (`entry_mode: pool_lower_edge` → `entry_price_reference: upper_pool_lower_edge`)
4. Bestehender `validate_config_dict()` (BT-D6)
5. `config_content_hash` identisch zu semantisch gleicher JSON-Config

Optionaler Schlüssel `density.validated_cache.required_config_hash: null` wird beim Normalisieren weggelassen (Hash-Parität zu JSON).

## Baseline (BT-D9 24h)

- Normalisierter Config-Hash: `0f512e5d75f30a39b09bd091ed4c107e5a7c4a4d78d687051ef57ac9aa9d0667`
- YAML und `bt_d9_24h_v1.json` liefern denselben Hash (pytest `test_central_yaml_matches_bt_d9_json_hash`).

### Lauf-Parität (validated_cache, kein neuer Density-Build)

| Run | Signale | Trades | orders/trades/pnl vs. JSON |
|-----|---------|--------|----------------------------|
| `bt_d16_yaml_parity_check` | 3 | 0 | identisch |
| `bt_d16_json_parity_check` | 3 | 0 | identisch |

Signal-Kern (ohne eingebettete `config_hash`-Metadaten in Snapshots): YAML ≡ JSON.

## Parameter-Klassifikation

### FROZEN (nicht über YAML lockern)

- `density.method` = `exact_unique_origin`
- `density.min_unique_origins` = 2 (Depth 2)
- Strategie-Identität / Freeze-Referenz wie BT-D9

### CONFIGURABLE (über YAML, Validator-gestützt)

- Backtest-Fenster UTC, Symbol, Timeframe/PD (innerhalb Schema)
- Short-Entry: `entry_mode: pool_lower_edge`, Gap, Proximity, Pool-Mindestalter, Pending-Limit
- Risk / TP / PnL / Leverage / Fees / Slippage / Funding / Intrabar / max positions
- `density_source: validated_cache` + `bundle_path`

### EXPERIMENTAL / UNRESOLVED (YAML dokumentiert, Lauf **abgelehnt**)

| Feature | YAML | Laufzeit |
|---------|------|----------|
| `short_entry.entry_mode: pool_upper_edge` | dokumentiert | `ENTRY_MODE_NOT_EXECUTABLE` |
| `short_entry.entry_mode: pool_lower_edge_offset` | dokumentiert | `ENTRY_MODE_NOT_EXECUTABLE` |
| `short_entry.entry_offset_pct` ≠ null/0 | dokumentiert | `ENTRY_OFFSET_NOT_IMPLEMENTED` (Engine hat kein Offset) |
| `long_entry.enabled: true` | vorbereitet | `LONG_ENTRY_NOT_IMPLEMENTED` |

LONG-Entry-Modi (`pool_upper_edge`, `pool_lower_edge`, `pool_upper_edge_offset`) sind nur in Kommentaren beschrieben; keine Engine-Anbindung.

## Tests

```
pytest tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1/tests/test_config_yaml_v1.py \
       tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1/tests/test_config_validate_v1.py \
       tools/phase_bt_d12_simple_config_latest_json_overlay_v1/tests/test_result_publish_v1.py
```

**19 passed** — u. a.:

- JSON-Pfad unverändert
- YAML ≡ JSON Hash
- Gap-Alias `gap_min_pct`
- Fensteränderung in YAML
- Duplicate keys, unknown keys, falsche Timestamps, falsche Depth
- Long / Offset / fehlende `pnl`-Sektion
- `latest_result.json` bleibt bei fehlgeschlagenem Publish unverändert (`test_failed_publish_leaves_latest`)

## Output / Dashboard

- Erfolgreicher Run: `results/backtester_runs/<run_id>/result.json`, atomisches `latest_result.json`
- Zusätzlich: `config.normalized.json`, `config.provenance.json` unter `<run_id>/`
- **Kein** Dashboard-Neustart in dieser Phase

## STOP

Keine Strategieänderung, kein SSOT/Density-Recalc für Parser-Tests, keine mehrmonatigen Backtests.
