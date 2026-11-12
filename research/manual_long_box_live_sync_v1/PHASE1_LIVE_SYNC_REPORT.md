# Manual Long Box — Phase 1 Live Sync (DRY-RUN)

**Status:** STOP nach Implementierung und DRY-RUN-Tests. **Keine echten Orders.**

## 1. Wiederverwendbare Komponenten

| Komponente | Pfad | Nutzung |
|----------|------|---------|
| Long-Box-Erkennung | `research/manual_long_box_research_v1/recognition.py` | `is_manual_long_box`, Preis-/Geometrie-Felder |
| Geometrie-Validierung | `research/manual_long_box_research_v1/validation.py` | `SL < Entry < TP` |
| Zeichnungs-Persistenz (live) | `spread_recovery.../workspace_session.py` | `on_drag` / `update_position` → `persist_drawings()` |
| API | `POST /api/research/drawings/event` (`type=drag`) | Entry/SL/TP + Zeitfenster beim Verschieben |
| Bybit (Live, später) | `dashboard/vendor/core/bybit_order_manager.py` | `open_long_limit`, `cancel_order_direct` — **nicht** in Phase 1 aufgerufen |

## 2. Wie Box-Änderungen erkannt werden

- **Phase 1:** `SyncService` pollt `drawings.json` (Default: Spread-Dashboard-Pfad).
- Änderung = **SHA-256** über serialisierten Inhalt (nicht nur mtime).
- **Debounce 0,35 s** nach letzter Dateiänderung → vermeidet Doppelverarbeitung bei schnellen Speichern.
- Drag im Chart ruft serverseitig sofort `persist_drawings()` auf (`on_drag`, Zeile ~4295); der Service sieht die Änderung **nach** dem Schreiben der Datei (kein Dashboard-Neustart).
- **Drawing-ID:** bleibt bei `on_drag`/`update_position` erhalten (`overlay_id` → `drawing_id`).

**SL/TP-Verschiebung:** wird erkannt und als `desired_sl_tp_documented` im State/Event-Log protokolliert — **kein** SL/TP auf der Exchange in Phase 1.

## 3. Exchange-API

- Vorhanden: **Bybit v5** via `BybitOrderManager` (echte Keys nur im Live-Dashboard).
- Phase 1: **`DryRunExchangeAdapter`** (`exchange_dry_run.py`) — rein in-memory.

## 4. Native Limit-Amendment

- Bybit API unterstützt `/v5/order/amend`; im vorhandenen **`BybitOrderManager` keine dedizierte amend-Methode** gefunden → Live-Phase vermutlich **Cancel/Replace** (mit Fill-Race-Checks).
- DRY-RUN: **`amend_limit()`** bevorzugt (simuliert natives Amend).

## 5. Drawing-ID ↔ Exchange-Order-ID

- Persistenz: `research/manual_long_box_live_sync_v1/data/sync_state.json`
- Felder: `drawing_id`, `exchange_order_id`, `order_link_id` (`mlb-sync-{drawing_id}`), letzte Entry/SL/TP, `order_status`, `content_hash`.
- Event-Audit: `data/sync_events.jsonl`
- **Aktivierung:** `data/manual_long_box_activations.json` — `enabled: true` **pro Drawing-ID** (ohne Aktivierung: **keine** Order).

## 6. Implementierte Dateien

```
research/manual_long_box_live_sync_v1/
  __init__.py
  config.py
  activation.py
  drawing_watch.py
  exchange_dry_run.py
  persistence.py
  sync_engine.py
  service.py
  run_sync_service.py
  data/manual_long_box_activations.json
  tests/test_live_sync_dry_run.py
  PHASE1_LIVE_SYNC_REPORT.md
```

## 7. Änderungen ohne Neustart

- Service läuft als Hintergrund-Poller (`python run_sync_service.py`).
- State wird nach jedem erfolgreichen Sync auf Disk geschrieben → **Neustart** lädt `sync_state.json` (`test_state_survives_restart` **PASS**).

## 8. Gemessene Latenz (DRY-RUN, isolierter Test)

`test_service_detects_file_change_latency`: Poll **0,05 s**, Debounce **0,08 s**.

- Dateiänderung → erkannt typisch **&lt; 40 ms** (ein Poll-Tick).
- Sync nach Debounce: **~0,08–0,15 s** zusätzlich.
- **End-to-End (Schreiben → AMEND im State):** typisch **&lt; 0,5 s**, in CI-Lauf **&lt; 1,5 s** (Obergrenze im Test).

Keine garantierte Echtzeit-Latenz über das Netzwerk/Chart; Polling-Intervall konfigurierbar (`--poll-sec`).

## 9. Doppelorders verhindern

- `order_link_id` pro Drawing-ID; zweiter CREATE bei offenem Link → **REJECT** `duplicate_link_id_open`.
- Pro `drawing_id` **`threading.Lock`** im Sync-Engine.
- Debounce im Service.
- Nach **FILLED:** `skipped_filled_no_second_order` — **kein** zweites CREATE bei Box-Verschiebung.
- Fill während Amend: `test_fill_during_amend_no_second_order` **PASS**.

## 10. DRY-RUN-Funktionstest (Erfolgskriterien)

| Kriterium | Ergebnis |
|-----------|----------|
| Dauerhafter Service (Poller) | **PASS** (`run_sync_service.py`) |
| Automatische Erkennung | **PASS** (Hash + Poll) |
| Gleiche Drawing-ID → gleiche Order | **PASS** |
| Entry-Änderung → AMEND | **PASS** |
| Keine Doppel-Orders | **PASS** |
| Exchange-ACK geprüft | **PASS** (`ExchangeAck.ok`) |
| Neustart / State | **PASS** |
| Fill während Änderung | **PASS** (kein 2. Order) |
| Kein Watcher | **PASS** |
| Keine echte Börse | **PASS** (`DRY_RUN_ONLY = True`) |
| Explizite Aktivierung | **PASS** (`test_not_activated_no_order`) |

**Tests:** `7 passed, 1 skipped` in `manual_long_box_live_sync_v1/tests/`.

**Hinweis Live-Datei (READ-ONLY Stand Testlauf):** In `drawings.json` war **keine** manuelle `d-*` `long_position` vorhanden (nur P17-Strategieboxen + `d-*` hline). Funktionstest daher **isoliert** mit synthetischer Box `d-test-sync-phase1`. Sobald eine echte Box aktiv ist, `drawing_id` in `manual_long_box_activations.json` setzen und Service gegen Live-Pfad starten.

## 11. Start (DRY-RUN)

```bash
cd /home/telgenbuescher/projects/pools+ob+delta_bot
PYTHONPATH=. python research/manual_long_box_live_sync_v1/run_sync_service.py \
  --drawings /home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard/research_charts/user_data/drawings.json
```

Einmaliger Lauf: `--once`

## 12. Nächste Phase (nicht implementiert)

- Optional: Hook nach `persist_drawings()` (Push statt Poll).
- Live-Bybit-Adapter mit Cancel/Replace + Reconcile gegen `get_open_orders`.
- SL/TP-Sync-Regeln.
- Live-Orderbetrieb erst nach expliziter Freigabe und `DRY_RUN_ONLY = False`-Gate.
