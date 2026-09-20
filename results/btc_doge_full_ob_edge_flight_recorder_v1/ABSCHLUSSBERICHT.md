# ABSCHLUSSBERICHT — BTC/DOGE Full-OB Edge Flight Recorder V1

## 1. Verdict

`BTC_DOGE_FULL_OB_EDGE_FLIGHT_RECORDER_READY_SHADOW_PILOT_RESTART_REQUIRED`

Phase 0 hatte den Sync-Vertrag als blockiert ausgewiesen. Der bestehende Full-OB-Pfad wurde **repariert** (kein zweiter Collector), der Flight Recorder ist als Shadow-Pilot verdrahtet (Default **aus**). Aktivierung braucht einen Collector-Restart — **noch nicht ausgeführt**.

## 2. Branch / HEAD

| Repo | Branch | HEAD |
|---|---|---|
| `spread_recovery_hedge_short_dev` | `feature/btc-doge-research-db` | `2af0477` |
| `orderbook_analyse` (Collector-Host) | (lokaler Worktree) | siehe `git status` |

## 3. Dirty-Worktree

Beide Worktrees bleiben dirty. **Kein Commit, kein Push.**  
Hinweis: frühere unvollständige `ob1000_flight_recorder/`-Dateien in `orderbook_analyse` sind **nicht** Teil dieses Contracts (Spec verbietet OB1000).

## 4. Identifizierte Full-OB-Komponenten

- `orderbook_analyse/.../full_book_state.py` — RAM-Book
- `orderbook_analyse/.../on_demand_full.py` — Lease + REST + WS
- `orderbook_analyse/.../collector.py` — Shared Bybit-WS
- Dashboard-Bridge `depth=0` in `spread_recovery.../ob1000_on_demand.py`

## 5. Kein zweiter Collector

- Observer-Hook `FullBookOnDemandManager.add_observer`
- Keeper-Lease über bestehende `_acquire` / gleiche Topic-Subscription
- Kein zweiter `orderbook.full.*` WebSocket
- Charts und Recorder teilen denselben `FullBookState`

## 6. Full-OB Sync-Contract (repariert)

Modul: `full_ob_sync.py` + Integration in `on_demand_full.py` / `full_book_state.py`

1. Subscribe → Deltas puffern (`BufferState`)
2. `u`-Gap im Buffer → Clear
3. sinkendes `seq` → Discard
4. REST Snapshot; Refetch wenn `snap.seq < first.seq` oder Seq-gleich/`u`-Mismatch
5. Match `seq`+`u` → Snapshot setzen → Rest-Buffer mit `u+1`
6. Live: stale/dup ignorieren; Gap/`u=1` → Clear + Resync
7. `book_ready` erst nach erfolgreichem Align
8. `cts` + `receive_time_ns` werden mitgeführt
9. RPI: `rpi_included=false` (Bybit Full-OB ohne RPI)

## 7. Geänderte / neue Dateien

**orderbook_analyse**

- `full_ob_sync.py` (neu)
- `full_book_state.py` (Continuity, cts, book_ready, RPI-Flag)
- `on_demand_full.py` (Align-Loop, Observer, Health)
- `collector.py` (FR-Attach, tick, OB200-Mid-Provider)
- `full_ob_edge_flight_recorder/` (config, ringbuffer, watcher, profiles, event_writer, replay, manager)
- Tests: `test_full_ob_sync_contract.py`, `test_full_ob_edge_flight_recorder_v1.py`

**spread_recovery_hedge_short_dev**

- `results/btc_doge_full_ob_edge_flight_recorder_v1/PHASE0_AUDIT.md`
- `results/btc_doge_full_ob_edge_flight_recorder_v1/ABSCHLUSSBERICHT.md`

## 8. Watcher- / Lifecycle-Contract

Zustände: `IDLE → ARMED → SUBSCRIBING → SYNCING → BOOK_READY → CAPTURING → FIGHT_ACTIVE → POST_CAPTURE → COOLDOWN` (+ `DEGRADED` / `UNAVAILABLE`).

Defaults: arm 50 bps, capture 20 bps, disarm 75 bps, RB 5 min, post 15 min, reclaim 5 min, max 90 min, cooldown 5 min.  
Nur abgeschlossene 30m-Profile; Freeze während Fight. BTC/DOGE isoliert.

## 9. Ringbuffer

- Zeitfenster + Message-/Byte-Cap
- nur Raw-Deltas (kein 10k-Snapshot-Copy pro Tick)
- REST-Snapshot einmalig im Event
- Overflow → `BUFFER_OVERFLOW` / incomplete
- Flush ohne Doppelzählung

## 10. Eventformat / Root

`<OB_V3_FULL_OB_FR_ROOT>/<SYMBOL>/YYYY-MM-DD/<event_id>/`

Pflicht: `manifest.json`, `profile_context.json`, `lifecycle.json`, `rest_full_snapshot.json.zst`, `full_ob_raw_deltas.jsonl.zst`, `public_trades_raw.jsonl.zst`, `coverage_audit.json`, `sequence_integrity.json`, `health_summary.json`, `event_summary.json`, `REPORT.md`.

Default-Root: `orderbook_analyse/data/orderbook_raw_shadow/full_ob_edge_flight_recorder` (überschreibbar).

## 11. Replay / Integrität

`replay_event_directory`: Snapshot + Deltas, `u`-Continuity, Crossed-Check, SHA256 der Delta-Datei. Fail-closed bei Gap/Tamper.

## 12. Tests

```
tests/test_full_ob_sync_contract.py
tests/test_orderbook_v3_full_book_on_demand.py
tests/test_full_ob_edge_flight_recorder_v1.py
→ 21 passed
```

Abgedeckt: Buffer/Align/Gap/u=1/seq↓, Hysterese, Fast-Approach, Isolation, kein Doppel-Event, Replay+SHA, Settings default off.

## 13. Ressourcen

Kein Live-Soak (kein Restart). Code: bounded Queue/Ringbuffer, max 2 parallele Events, zstd, Symbolisolation.

## 14. Daten- / RPI-Grenzen

- Full-OB ohne RPI
- REST max ~10k Levels/Seite
- Trade↔L2 ohne Shared-ID → höchstens temporal associated
- `UNMATCHED` ≠ Cancellation
- Shadow-V1: Live-`publicTrade`-WS noch nicht mitgeschrieben; Event referenziert kanonische Trades über Zeitfenster im Manifest/Coverage

## 15. Impact Charts

- Default: FR **disabled** → Verhalten unverändert
- Sync-Repair ändert Gap/Stale-Verhalten korrekt (Charts profitieren)
- Keine API-/Template-Änderung in diesem Schritt

## 16. Restart erforderlich?

**Ja**, für Pilot-Aktivierung des Collectors (Env-Flags).  
Dashboard-Restart: **nein** (solange nur Collector-Env).  
**Bisher kein Restart ausgeführt.**

## 17. Pilotaktivierung (Freigabe abwarten)

```bash
# in orderbook_analyse/.env (oder Start-Wrapper) ergänzen:
OB_V3_FULL_OB_FLIGHT_RECORDER_ENABLE=true
OB_V3_FULL_OB_FR_SYMBOLS=BTCUSDT,DOGEUSDT
OB_V3_FULL_OB_FR_ROOT=/home/telgenbuescher/projects/orderbook_analyse/data/orderbook_raw_shadow/full_ob_edge_flight_recorder
# benötigt weiterhin:
OB_V3_ON_DEMAND_ENABLE=true
OB_V3_FULL_BOOK_ENABLE=true

# Danach erst nach Freigabe:
# geordneten Restart des orderbook_v2_live Collectors
# Dashboard NICHT anfassen, außer Bridge-Probleme
```

Smoke nach Restart: Health-Feld `full_ob_flight_recorder_enabled=true`, Lifecycle BTC/DOGE, kein zweites Full-OB-Topic.

## 18. Bestätigungen

- keine Tradingaktionen
- keine DB-Writes in produktive Tabellen
- keine Live-Restarts ausgeführt
- kein Commit
- kein Push

## STOP

Pilot nicht aktiviert. Freigabe für Collector-Restart abwarten.
