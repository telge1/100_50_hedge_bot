# Controlled Pilot Write — Runbook (nicht automatisch ausführen)

**Freigabe:** Nur nach expliziter Nutzer-Freigabe und erfolgreichem Phase-3A-Preflight.

## 1. Vorbedingungen

- [ ] Phase-3A-Preflight: keine Konflikte für Pilotlinie und geplante `snapshot_id`s
- [ ] `HISTORICAL_JSON_IMPORT_ALLOW_WRITE=1` **nur** in der Write-Shell setzen
- [ ] Collector `.env` / CH erreichbar (gleiche DB wie Research: `signal_generator`)
- [ ] Dry-Run unmittelbar vor Write: `cli.py` → `errors: []`, 2750/2750

## 2. Write-Befehl (exakt)

```bash
cd /home/telgenbuescher/projects/pools+ob+delta_bot/backtester/short/dashboard

export HISTORICAL_JSON_IMPORT_ALLOW_WRITE=1

python3 <<'PY'
import sys, uuid
from pathlib import Path
_DASH = Path(".").resolve()
sys.path.insert(0, str(_DASH.parents[1]))
sys.path.insert(0, str(_DASH))
from research_charts.trp_import import ensure_trp_on_path
ensure_trp_on_path()
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.pilot_config import (
    PILOT_DECISION_CLOSE_STEMS,
    PILOT_PIPELINE_LINE_ID,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.pilot_execute import (
    execute_pilot_import,
)
run_id = str(uuid.uuid4())
print("test_run_id", run_id)
print(execute_pilot_import(PILOT_DECISION_CLOSE_STEMS, pipeline_line_id=PILOT_PIPELINE_LINE_ID, test_run_id=run_id))
PY
```

**Write-Gate:** Ohne `HISTORICAL_JSON_IMPORT_ALLOW_WRITE=1` brechen `catalog_write` und `pilot_execute` ab.

## 3. Erwartete Ergebnisse

| Tabelle | Delta |
|---------|--------|
| `research_lmv2_active_snapshot_meta_v1` | +10 |
| `research_lmv2_active_snapshot_member_v1` | +2750 |
| `research_lmv2_snapshot_active_pool_v1` | +2750 |
| `research_lmv2_active_snapshot_publish_v1` | +10 (Pilotlinie) |

Pro Close: `import_historical_close` → staged → catalog (vollständig) → publish ledger.

## 4. Readback-Gate (alle 10 Closes)

```bash
cd /home/telgenbuescher/projects/pools+ob+delta_bot/backtester/short/dashboard

python3 <<'PY'
import sys
from pathlib import Path
_DASH = Path(".").resolve()
sys.path.insert(0, str(_DASH.parents[1]))
sys.path.insert(0, str(_DASH))
from research_charts.trp_import import ensure_trp_on_path
ensure_trp_on_path()
from research_charts.live_micro_lld_v2_ch_test.identity import build_live_micro_lld_v2_config, config_hash
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.pilot_config import (
    CATALOG_ROOT, PILOT_DECISION_CLOSE_STEMS, PILOT_PIPELINE_LINE_ID,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.readback_parity import run_pilot_readback
ch = config_hash(build_live_micro_lld_v2_config(10))
reports = run_pilot_readback(
    PILOT_DECISION_CLOSE_STEMS,
    catalog_root=CATALOG_ROOT,
    pipeline_line_id=PILOT_PIPELINE_LINE_ID,
    config_hash_value=ch,
    use_clickhouse=True,
)
print("OK", len(reports), "closes")
PY
```

**Erwartung:** Kein `ReadbackGateFailure`; alle `ok=True`.

## 5. Audit-Pools (20 IDs)

In `compare_json_to_published` / `AUDIT_POOL_IDS` (`pilot_config.py`): bei Readback müssen alle 20 in JSON und CH-Catalog pro Close vorhanden sein, sofern der Pool an diesem Close ACTIVE ist.

Zusätzlich manuell stichprobenartig einen Stem prüfen:

```bash
# Nach Write: ein Stem — JSON vs CH-Felder (readback meldet Abweichungen fail-closed)
```

## 6. Fehlerverhalten

| Fehler | Aktion |
|--------|--------|
| `PublishConflictError` | STOP — nicht überschreiben; Ursache dokumentieren |
| `CatalogIncompleteError` (partial) | STOP — **kein** blindes Retry; CH-Catalog für `snapshot_id` prüfen |
| `ReadbackGateFailure` | Publish als **nicht freigegeben** behandeln; keine Chart/Remaining-Produktion |
| `CatalogIdentityError` | Import-Daten vs JSON prüfen |

## 7. Kontrollierter Wiederanlauf

- **Idempotent:** Vollständig importierter Close → Skip staged/catalog/publish (`import_historical_close`).
- **Publish ohne Catalog:** Repair-Pfad schreibt nur Catalog (wenn `count==0`).
- **Partial Catalog (`0 < n < expected`):** **Kein** Auto-Repair — manuelle CH-Bereinigung nötig, dann erneut mit Freigabe.

## Pilot-Identifikation (Recovery)

- `pipeline_line_id`: `historical_mixed_frozen_doge_15m_pd10_aug2026_pilot_v1`
- `config_hash`: `bd9ac4f30e177321b688f5adb0fe2681ebb4966d51701e3b323498bfcd0d06c2`
- 10 feste `snapshot_id` (Dry-Run `summary.snapshot_ids`)
- Catalog-Zeilen tragen dieselbe `pipeline_line_id`
