# Episode-1 Wall Defense Research Checkpoint V1

**Checkpoint-Name:** `EPISODE1_WALL_DEFENSE_EVENT_TIME_PROVEN_V1`
**Erstellungsdatum:** 2026-09-10
**Git-Repo:** `/home/telgenbuescher/projects/orderbook_analyse`
**Git-Branch:** `research/episode1-wall-defense-proven-v1`
**Git-Tag:** `episode1-wall-defense-event-time-proven-v1`
**Commit-Hash:** _(eingetragen nach Commit)_

## Scope

Dieser Checkpoint friert den **vollständig bewiesenen Episode-1 Wall-/Defense-Chain-
Research-Stand** ein (event-time-only). Er enthält **keinen** Trading-Classifier,
kein Live-Signal und **keine** generische Multi-Episode-Kalibrierung.

Abgrenzung zu alten Paketen: die hier versionierten `level_first_episode1_*`- und
`wall_defense_outcome_contract_v1`-Module sind die kanonische Episode-1-Pipeline und
ersetzen keine älteren Strategy-Lab-/Dashboard-Pfade.

## Erfolgs-Verdicts

1. `EPISODE1_TOUCH_DETECTION_EVENT_TIME_INDEPENDENTLY_DERIVED`
2. `EPISODE1_WALL_FLOW_QDH_BASE_EVENT_TIME_PROVEN`
3. `EPISODE1_INDEPENDENT_DETECTION_TO_WALL_FLOW_E2E_PROVEN`
4. `EPISODE1_PRICE_RESPONSE_MICROPRICE_RECLAIM_EVENT_TIME_PROVEN`
5. `EPISODE1_POST_CROSS_CENSORED_BY_EPOCH_BOUNDARY`
6. `EPISODE1_WITHIN_EPOCH_WALL_MIGRATION_STRUCTURE_PROVEN`
7. `EPISODE1_DEFENSE_CHAIN_EVENT_TIME_PROVEN`
8. `WALL_DEFENSE_OUTCOME_CONTRACT_V1_PROVEN`
9. `EPISODE1_NEXT_MAJOR_ASK_BARRIER_HEADROOM_EVENT_TIME_PROVEN`

## Paketpfade (Code)

Unter `obfull_research_engine/src/obfull_research_engine/`:

| Paket | Rolle |
|---|---|
| `level_first_episode1_touch_detection_independent_v1` | Independent Touch/Detection |
| `level_first_episode1_wall_flow_qdh_base_v1` | Wall-Flow / QDH Base |
| `level_first_episode1_detection_to_wall_flow_integration_v1` | Typed E2E Handoff |
| `level_first_episode1_price_response_reclaim_v1` | Price/Microprice Response |
| `level_first_episode1_wall_migration_structure_v1` | Within-epoch Wall Structure |
| `level_first_episode1_defense_chain_v1` | Defense Chain |
| `wall_defense_outcome_contract_v1` | Versioned OutcomeFacts/Labels |
| `level_first_episode1_next_major_ask_barrier_headroom_v1` | Next-major Ask Headroom |

Foundation-Dependencies (ebenfalls eingefroren, weil Import-Closure):

- `level_first_episode1_corrected_sms1_persist_v1`
- `bounded_level_first_analyzer_pilot_v1`
- `drilldown`
- `market_profile_lld_shared_event_materialization_v1`
- `level_first_window_native_full_ob_direction_v1`
- `paths.py`, `timeparse.py`, `__init__.py`
- unterstützende Module: `episodes`, `outcomes`, `localized_coverage`, `avr`,
  `avr_multiscale`, `single_case_inspector`, `market_profile_context`, sowie
  kleine Root-Module (`interval_coverage`, `partition_io`, `cli_report`,
  `schema_freeze`, `cli`)

Tests unter `obfull_research_engine/tests/test_level_first_episode1_*.py` und
`test_wall_defense_outcome_contract_v1.py`.

## Zentrale Run-IDs (Server-Artefakte, nicht im Git)

Pfadbasis: `obfull_research_engine/results/`

| Stufe | Run-A / Run-B |
|---|---|
| Touch/Detection | `tdi1_a8f3c1e92b704d6aa` / `…b` |
| Wall-Flow/QDH | `wfq1_58db1918314881b6a` / `…b` |
| Detection→Wall-Flow | `d2w1_eee5d0eefaffa` / `…b` |
| Price Response | `prr1_5c62bb55455da` / `…b` |
| Wall Migration | `wms1_fa0b8dd6daf1a` / `…b` |
| Defense Chain | `dch1_9bb0b8ff5ce8a` / `…b` |
| Outcome Contract | `odc1_efd620a305e8a` / `…b` |
| Headroom | `nabh1_cb3769fe1c26a` / `…b` |

Coverage/Cross-Boundary-Audit:
`results/level_first_episode1_price_response_reclaim_v1/BTCUSDT/prr1_5c62bb55455d_coverage_cross_boundary_audit`

Große CSV/Parquet/Book-Persist-Artefakte sind **nicht** versioniert; Run-IDs und
Hashes reichen zur Wiederauffindung auf dem Research-Server.

## Zentrale Hashes

- **Wall-Flow Feature-Only Semantic:**
  `f10b6398f2aa5f49f8a8834ac1a992e9e3bbe92d1a5975411938cfdeaecd5c36`
- **Outcome-Contract Hash:**
  `efd620a305e8252e61f03c516c69de364049153e6fb02c091caf6137e4e7e46a`
- **Headroom Semantic:**
  `cb3769fe1c26ea71565ed86556f459af5c3e712f0a80283cb0a999696c2a5fe4`

## Episode-1 Bindung

- Symbol: `BTCUSDT`
- Datum: `2026-09-06`
- Zone Touch: `20:19:02.229Z`
- Wall Touch: `20:19:02.232Z`
- Joint Breach: `20:19:41.900Z`
- Detection: `20:21:00.000Z`
- Zone: `[79774.75, 79775.25]`
- Ursprüngliche Ask-Wall: `79780.0`
- Große sichtbare Ask-Barriere: `80000.0`
- Epoch-4 Coverage Ende: `20:19:59.800Z`
- Epoch-5 Checkpoint: `20:19:59.873Z` (keine Continuity-Bridge)

## Bewiesene fachliche Ergebnisse (kurz)

- Independent Touch/Detection event-time abgeleitet.
- Wall-Flow/QDH-Base feature-only, kausal.
- Typed Handoff Detection→Wall-Flow.
- Price/Microprice-Response; nach Joint Breach kein Reclaim innerhalb Epoch-4.
- Post-cross Horizonte jenseits Epoch-Grenze zensiert.
- Within-epoch Wall-Migration / gestaffelte Ask-Verteidigung.
- Defense-Chain event-time (relevante vs. Churn-Knoten getrennt).
- OutcomeFacts/Labels versioniert, outcome-blind, Feature-Leakage verboten.
- Next-major Ask-Barrier Headroom: ausführbarer Entry = Best Ask; bei Breach
  nearest past-only Q95 ~`79824.9` (~0.027 %); Ask `80000.0` sichtbar mit
  ~0.246 % Distanz — **unter** required gross `0.410 %` / net `0.30 %`.
  `DETECTION` → `HEADROOM_CENSORED_AT_DETECTION`.

## Fee- / Headroom-Vertrag

- `entry_fee_pct = 0.055`
- `exit_fee_pct = 0.055`
- `roundtrip_fee_pct = 0.110`
- `required_net_profit_pct = 0.300`
- `required_gross_headroom_pct = 0.410`
- Slippage separat ausgewiesen (nicht in Fees versteckt).

## Event-Time-only / keine Live-Causal-Behauptung

Alle Entscheidungspunkte und Outcomes sind **Exchange-Event-Time**. Dieser
Checkpoint behauptet **keine** Receive-Time-/Live-Collector-Kausalität und
ändert keine Collector-/ClickHouse-Produktion.

## Censoring-Grenzen

- Epoch-Boundary (4→5) darf nicht gebridged werden.
- Unvollständige Horizonte → `CENSORED` / `OUTCOME_CENSORED` / Headroom-Censor.
- Zensierte Outcomes zählen nicht als Win/Loss/Reclaim/Breakout.

## Testbefehle

```bash
cd /home/telgenbuescher/projects/orderbook_analyse/obfull_research_engine
PYTHONPATH=src:../src python -m pytest \
  tests/test_level_first_episode1_touch_detection_independent_v1.py \
  tests/test_level_first_episode1_independent_derivation_v1.py \
  tests/test_level_first_episode1_wall_flow_qdh_base_v1.py \
  tests/test_level_first_episode1_detection_to_wall_flow_integration_v1.py \
  tests/test_level_first_episode1_price_response_reclaim_v1.py \
  tests/test_level_first_episode1_wall_migration_structure_v1.py \
  tests/test_level_first_episode1_defense_chain_v1.py \
  tests/test_wall_defense_outcome_contract_v1.py \
  tests/test_level_first_episode1_next_major_ask_barrier_headroom_v1.py \
  tests/test_level_first_episode1_corrected_sms1_persist_v1.py \
  tests/test_level_first_episode1_causal_availability_v1.py \
  tests/test_level_first_episode1_touch_provenance_v1.py \
  -q
```

Import-Smoke:

```bash
PYTHONPATH=src:../src python - <<'PY'
import importlib
for m in [
  'obfull_research_engine.level_first_episode1_touch_detection_independent_v1',
  'obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1',
  'obfull_research_engine.level_first_episode1_detection_to_wall_flow_integration_v1',
  'obfull_research_engine.level_first_episode1_price_response_reclaim_v1',
  'obfull_research_engine.level_first_episode1_wall_migration_structure_v1',
  'obfull_research_engine.level_first_episode1_defense_chain_v1',
  'obfull_research_engine.wall_defense_outcome_contract_v1',
  'obfull_research_engine.level_first_episode1_next_major_ask_barrier_headroom_v1',
]:
    importlib.import_module(m)
    print('OK', m)
PY
```

## Datenvoraussetzungen (nicht im Git)

- Frozen Full-OB Persist: `results/.../wfq1_58db1918314881b6a/`
- Handoff JSON: `.../d2w1_eee5d0eefaffa/episode1_handoff.json`
- Defense-Chain / scored walls: `.../dch1_9bb0b8ff5ce8a/`
- Public trades / independent inputs unter corrected-sms1 persist results

## Wiederherstellung

```bash
cd /home/telgenbuescher/projects/orderbook_analyse
git fetch origin
git switch research/episode1-wall-defense-proven-v1
# oder:
git switch --detach episode1-wall-defense-event-time-proven-v1
```

## Explizit nicht Teil dieses Checkpoints

- Generischer BTC-Multi-Episode-Builder / unfertige Kalibrierung
- WALL_STATE-Classifier / Trading-Signal / profitable Threshold-Auswahl
- Dashboard-/Collector-/Live-Konfigurationsänderungen
- OI / Liquidationen / Cross-Exchange / Receive-Time-Simulation
- ClickHouse-Writes, Restarts, große Result-Verzeichnisse
