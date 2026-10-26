# PHASE BT-D3 — Canonical 5m PD1 Depth-2 Backtester Density Provider V1

**Date:** 2026-10-25  
**Mode:** Provider + tests only. No Phase 4D strategy wiring, no productive backtest, no SSOT/chart edits.

---

## VERDICT

### `DEPTH2_PROVIDER_PARITY_PASS`

Kanonischer **Chart-B-Pfad** (JS-Adapter über Node-Bridge) ist implementiert, fail-closed validiert, deterministisch und an mehreren May–Sep-Zeitpunkten geprüft.

**Wichtig zur Referenz `pools.json` (218 Bänder):** Diese Datei stammt aus einem **Python-Sweep-Port** ohne vollständige `mergeExactOriginSegments`-Kette. Der verbindliche Chart-Pfad liefert am Referenzzeitpunkt **39 Upper / 176 Lower / 215 total**. Geometrie und Origin-Sets des Providers stimmen **exakt** mit dem JS-Golden überein; Abweichungen zur Legacy-Datei sind dokumentiert (siehe §4).

---

## 1. Implementierte Dateien

| Datei | Rolle |
|-------|--------|
| `tools/phase_bt_d3_canonical_depth2_provider_v1/density_contract_v1.json` | Versionierter Contract |
| `tools/phase_bt_d3_canonical_depth2_provider_v1/density_contract_v1.py` | Fail-closed Validierung |
| `tools/phase_bt_d3_canonical_depth2_provider_v1/window_policy_v1.py` | Kausale SSOT-Fenster (48h sichtbar, ≤72h, 168h Warmup via Builder) |
| `tools/phase_bt_d3_canonical_depth2_provider_v1/ssot_bootstrap_v1.py` | PYTHONPATH / `HISTORICAL_LMV2_BATCH_READER` |
| `tools/phase_bt_d3_canonical_depth2_provider_v1/node_bridge_v1.py` | Node → `analyzer_depth2_landscape.js` |
| `tools/phase_bt_d3_canonical_depth2_provider_v1/band_format_v1.py` | Normalisierte Band-Records |
| `tools/phase_bt_d3_canonical_depth2_provider_v1/density_provider_v1.py` | **`get_density_bands(...)`** |
| `tools/phase_bt_d3_canonical_depth2_provider_v1/parity_compare_v1.py` | Kanten-/Origin-Vergleich |
| `tools/phase_bt_d3_canonical_depth2_provider_v1/integration_hook_v1.py` | Hook **deaktiviert** (`BT_D3_DENSITY_PROVIDER_HOOK`) |
| `tools/phase_bt_d3_canonical_depth2_provider_v1/run_parity_suite.py` | Parität + Kausalität + Performance |
| `tools/phase_bt_d3_canonical_depth2_provider_v1/tests/` | pytest (12 passed) |
| `results/phase_bt_d3_canonical_depth2_provider_v1/golden_20260925_1100_chart_path.json` | JS-Golden Sep-25 |
| `results/phase_bt_d3_canonical_depth2_provider_v1/parity_suite_report.json` | Suite-Ergebnis |

**Unverändert:** Phase 4D `engine.py`, Signale 2A/3A/3B, Research Chart, Pool-SSOT, Original Short V2.

---

## 2. Contract und Schnittstelle

### Contract (`bt_density_contract_v1`)

- `density_method`: `exact_unique_origin`
- `timeframe`: `5m`
- `pool_detail`: `1`
- `min_unique_origins`: `2`
- `warmup_hours`: `168`
- `fail_closed`: `true`

### API

```python
from tools.phase_bt_d3_canonical_depth2_provider_v1.density_provider_v1 import get_density_bands

result = get_density_bands(
    "DOGEUSDT",
    "2026-09-25T11:00:00Z",
    timeframe="5m",
    pool_detail=1,
    min_unique_origins=2,
    use_parity_fixture_window=True,  # nur für festen Paritäts-Prefetch Sep-25
)
# result.bands, result.upper, result.lower, result.manifest
```

**Pro Band:** `as_of_utc`, `side`, `lower_price`, `upper_price`, `unique_origin_count`, `origin_ids`, `pool_status=ACTIVE`, `pipeline_line_id`, `config_hash`, `density_method`, `active_semantics`.

**Datenfluss:**

1. `validate_request` (fail-closed)
2. `causal_window_bounds(decision_close)` oder Paritäts-Fixture
3. `build_landscape_window` (published ACTIVE SSOT, 168h Kontext)
4. Node: `analyzer_depth2_landscape.js` → `buildDepth2Intervals` → **`display_intervals`** (= `chart.depth2`, Adapter `mergeExactOriginSegments` + `filterSegmentsByMinOrigins(2)`)
5. Interne Analyzer-Parität `forensic` vs `chart` muss `ok: true` sein

**Fail-closed:** falsche `min_unique_origins`, `15m`, falsche `density_method`, `OUT_OF_SCOPE` / unpublished → `DensityProviderError`.

---

## 3. Paritätstests

**Ausführen:**

```bash
export HISTORICAL_LMV2_BATCH_READER=1
python tools/phase_bt_d3_canonical_depth2_provider_v1/run_parity_suite.py
python -m pytest tools/phase_bt_d3_canonical_depth2_provider_v1/tests -q
```

| Gate | Ergebnis |
|------|----------|
| Deterministische Wiederholung Sep-25 | PASS |
| Golden self (215 Bänder) | PASS |
| 5× May–Sep zusätzliche `as_of` | PASS (deterministisch) |
| 5× aufeinanderfolgende 5m-Closes Sep-25 | PASS (5 distinct snapshots) |
| Legacy `pools.json` | **FAIL erwartet** (218 vs 215, siehe §4) |

---

## 4. Preisgrenzenvergleich (2026-09-25T11:00:00Z)

| Quelle | Upper | Lower | Total |
|--------|------:|------:|------:|
| Legacy `phase_density_chart_parity_.../pools.json` (Python) | 39 | 179 | 218 |
| **Provider / Chart-B JS-Pfad** | **39** | **176** | **215** |

- **Upper:** 39/39 Kanten stimmen mit Legacy überein (Origin-Enrichment in Legacy kann bei wenigen Bändern abweichen; Geometrie gleich).
- **Lower:** JS merged benachbarte Mikro-Segmente gemäß `mergeExactOriginSegments` (z. B. drei zusammenhängende Bereiche zu breiteren Bändern). **Keine** willkürliche Toleranz — exakter String-Vergleich `.17g` innerhalb des JS-Goldens.
- Golden: `results/phase_bt_d3_canonical_depth2_provider_v1/golden_20260925_1100_chart_path.json`

Paritäts-Fenster (wie Chart-Export): `visible_from=2026-09-23T11:00:00Z`, `visible_to=2026-09-25T11:05:00Z` via `use_parity_fixture_window=True`.

---

## 5. Kausalitätstests

- Jeder Aufruf nutzt nur SSOT-Zustand bis `decision_close_utc` (published closes, segment `poolActiveAt`).
- Fenster pro Close wird **neu** geladen; kein Sharing späterer Band-Snapshots auf frühere `as_of`.
- Sequenz `10:40` … `11:00` UTC: **5** unterschiedliche Band-Snapshots.
- `decision_close` muss 5m-Grid sein (sonst `ValueError`).

---

## 6. Performance-Messung (read-only)

10 aufeinanderfolgende 5m-Closes ab `2026-09-25T10:15:00Z` (ohne Fixture):

| Metrik | Wert |
|--------|------|
| Mean / Close | **~5.6 s** |
| Total 10 Closes | **~56 s** |
| Span May–Sep (Einzel-`as_of`) | **~6–10 s** |

**Skalierung 20–50 Coins (ohne Implementierung):**

- Hot path = CH batch read + Node JSON pro Close → grob **~5–10 s × Bars × Coins** ohne Cache.
- Empfohlen für Integration: **pro Close einmal** SSOT-Window cachen; Bands nur für benötigte `as_of` recomputen; optional Timeline-Precalc pro Symbol/Monat mit **strikter** Close-Key-Isolation (kein Cross-Leak).
- `MAX_EXPORT_HOURS=72` begrenzt Fenster; lange Backtests brauchen chunked windows wie Viewer.

---

## 7. Regressionstests

`pytest`: **12 passed** (~51s mit SSOT)

Abgedeckt:

- Depth 2 fail-closed vs Depth 1
- 15m abgelehnt
- Hook standardmäßig aus
- Sep-25 Band counts (39/176)
- Golden-Vergleich
- Legacy-Diff dokumentiert
- OUT_OF_SCOPE fail
- Consecutive determinism
- Window policy / 5m grid

---

## 8. Bekannte Einschränkungen

1. **Scope v1:** nur `DOGEUSDT`, `5m`, `PD1`.
2. **Legacy `pools.json`:** nicht identisch mit Chart-B JS (218 vs 215); Referenz für BT-D3 ist **JS-Golden**, nicht Python-Export.
3. **Origin-IDs in Output:** `enrichOriginsFromBands` (Forensik) kann **mehr** Origins listen als Sweep-`metadata.depth` in Randfällen; Geometrie folgt Adapter-`depth`.
4. **Node-Subprozess** pro Aufruf (Latenz); noch kein Batch-Node-Runner.
5. **Integration hook** existiert, ist **nicht** in Phase 4D aktiv.

---

## 9. Nächster minimaler Integrationsschritt (nach Freigabe)

1. Phase 4D (oder neuer Runner) **optional** `get_density_bands` pro `decision_close` aufrufen.
2. G2 in `run_depth2_parity.py` mit Provider-`export_manifest_json` verbinden.
3. Timeline-Precalc-CSV für May–Sep nur mit `manifest_hash` + Contract-Feldern.
4. **Nicht** automatisch: Entry/TP/Lifecycle auf Provider umstellen.

---

## Integration hook (deaktiviert)

```bash
# Nur wenn explizit gewünscht — ändert keine Phase-4D-Engine
export BT_D3_DENSITY_PROVIDER_HOOK=1
```

`integration_hook_v1.attach_density_bands_if_enabled(...)` → `None` wenn Hook aus.

---

**STOP** — Provider und Tests abgeschlossen; keine Strategie-Anbindung.
