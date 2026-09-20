# Full-OB Unix-Socket Lock-Offload (offline)

Ausgangslage: `SHADOW_PILOT_LIVE_CAPTURE_OK_CHART_FULL_SOCKET_BLOCKED_WHILE_CAPTURING`

**Verdict:** `FULL_OB_SOCKET_LOCK_OFFLOAD_READY_RESTART_REQUIRED`

Kein Collector-/Dashboard-Restart, keine Env-Änderung, keine DB-Writes, kein Commit/Push. Live-Capture (alter Code im laufenden Prozess) bleibt unangetastet.

## 1. Root Cause

Zwei gekoppelte Fehler auf dem **gleichen asyncio-Loop** wie der Unix-Socket:

1. `FullBookOnDemandManager.handle_message` hielt `_book_lock` und rief Observer **innerhalb** des Locks.
2. `FullObEdgeFlightRecorder.on_full_ob_message` schrieb im Observer `orjson.dumps` + zstd + Disk (`ActiveEventWriter.append_delta`).
3. `_snapshot_response` sortierte/aggregierte das Full-Book (~60k Levels) ebenfalls unter `_book_lock`.

Folge: WS-Ingest blockierte das Book-Lock und den Event-Loop. `depth=0` status/snapshot (Timeout 2s) hing, während Capture lief. OB1000-Socket war nur in Lücken erreichbar. Die Operation, die den Lock **lange** hielt, war **nicht** die Map-Kopie, sondern Serialisierung/Kompression/Aggregation.

Beweis (60 000 Levels, offline):

| Operation | Unter `_book_lock`? | Zeit |
|---|---|---|
| `dict`-Kopie bids/asks + Metadaten (`copy_consistent_snapshot`) | ja (Soll) | **0.46 ms** |
| `aggregate_full_book` (Sort + Bucket) | bisher ja / jetzt nein | **18.3 ms** |
| `handle_message` Lock-Hold nach Fix (Delta apply, Observer danach) | nur Apply | **0.012 ms** (Observer-Sleep 20 ms außerhalb) |
| Chart-Snapshot gesamt nach Fix | Kopie unter Lock, Agg außerhalb | Wall **22.7 ms**, Lock **0.85 ms** |
| `full_levels=true` Snapshot | Sort/JSON außerhalb | Wall **25.7 ms** |
| Live-Pilot vorher | zstd+Disk+Agg unter Lock + Loop-Stall | status/snapshot **Timeout 2s** |

## 2. Lock-Pfad Socket (nach Fix)

`OnDemandSocketServer._handle_client` (sync Handler auf dem WS-Loop)
→ `Collector._dispatch_on_demand_request` (`depth==0`)
→ `FullBookOnDemandManager.handle_request`
→ `_snapshot_response`:
  - **mit** `_book_lock`: nur `copy_consistent_snapshot()` (bids, asks, u, seq, ts/cts, receive_time_ns, book_ready)
  - **ohne** Lock: `aggregate_full_book`, optionales `full_levels()`, Response-Dict
→ Socket-Write (`json.dumps` + drain) **ohne** Book-Lock

OB1000 bleibt `OnDemandManager.handle_request` (unverändert, `depth` default 1000).

`depth=0` setzt `levels_capped_at_1000=false`. Chart-Default bleibt aggregierte Bars (max 600/Seite, UI-only). `full_levels=true` liefert alle Levels ohne 1000-Cap.

## 3. Lock-Pfad Flight-Recorder-Writer (nach Fix)

`handle_message`: Apply/Buffer **mit** `_book_lock` → Lock frei → `_notify_observers`
→ `FullObEdgeFlightRecorder.on_full_ob_message`
→ `NonBlockingDeltaSink.try_put` (`Queue.put_nowait`, kein Disk)
→ Daemon-Thread: `append_delta` (orjson + zstd + Datei) **ohne** `_book_lock`

Queue: bounded (`queue_size`, default 4096). `QueueFull` → `drops++`, Status `INCOMPLETE_QUEUE_DROP`, Lifecycle `DEGRADED`. Kein stilles Drop. WS-Consumer blockiert nicht (`put_nowait`).

Segmentierung: nach `segment_minutes=30` oder `max_open_tmp_bytes=256MiB` → `SEGMENT_CONTINUED`, gleiche `fight_event_id`, `continuation_index`, Deltas unter `cont_NNN/`. Keine Auto-Löschung. Health: `writer_backlog`, `writer_queue_drops`, `open_tmp_bytes`, `disk_free_gb`, `projected_daily_bytes`, `projected_daily_warn`.

## 4. Betroffene Dateien / Funktionen

| Datei | Funktionen |
|---|---|
| `full_book_state.py` | `ConsistentBookSnapshot`, `copy_consistent_snapshot`, `full_levels`, `aggregate_full_book` |
| `on_demand_full.py` | `handle_message`, `_snapshot_response`, `_notify_observers`, `health_dict` |
| `full_ob_edge_flight_recorder/async_sink.py` | `NonBlockingDeltaSink` (neu) |
| `full_ob_edge_flight_recorder/event_writer.py` | `continuation_index`, `fight_event_id`, `open_tmp_bytes`, `mark_incomplete` |
| `full_ob_edge_flight_recorder/manager.py` | `on_full_ob_message`, `_start_or_merge_event`, `_rotate_segment`, `_finalize_event`, `health_dict` |
| `full_ob_edge_flight_recorder/config.py` | `segment_minutes`, `max_open_tmp_bytes`, `warn_free_disk_gb`, `projected_daily_warn_bytes` |
| `full_ob_edge_flight_recorder/replay.py` | Segment-Kette + SHA je Datei |
| `tests/test_full_ob_socket_lock_offload.py` | neu |

Unverändert: OB1000-`on_demand_manager.py`, OB200-Raw-Archive, Socket-Framing, Collector-Dispatch-Trennung `depth==0` vs sonst.

## 5. Tests (60 passed, 1.11s)

- 60k-Book: Copy vs Aggregate unter Lock; Observer außerhalb Lock; `depth=0` Full-Levels ohne 1000-Cap
- paralleler Delta-Writer + Snapshot < 0.5s; u/seq atomar; kein Crossed Book
- Queue-Full fail-closed (sichtbare `drops`)
- Segment-Continuation gleiche `fight_event_id`, kein Doppel-Event, Replay/SHA unverändert (Tamper fail-closed)
- Regression: `test_full_ob_edge_flight_recorder_v1`, `test_full_ob_sync_contract`, `test_orderbook_v3_full_book_on_demand`, `test_orderbook_v3_on_demand_ob1000`, `test_ob200_v3_raw_discovery_v3`

## 6. Späterer Restart (nicht jetzt)

Nur **Collector** `raw-archive-only` laden den neuen Code. Dashboard nicht erforderlich für diesen Socket/Lock-Fix. Env unverändert lassen. Offene `.tmp`-Events des laufenden Piloten gehören zum **alten** Prozess; nach Restart neue Event-IDs / neue Segmente.

## 7. Live-Risiko

Prozess **1467869** läuft weiter mit altem Code: Chart `depth=0` kann während Capture weiter timeouten. Capture/Continuity des Piloten durch diese Code-Änderung **nicht** beeinflusst (kein Reload). Nach Restart: kurzer WS-Reconnect, Full-OB Resync, Recorder startet neue Events (alte `.tmp` nicht automatisch finalisiert).

`FULL_OB_SOCKET_LOCK_OFFLOAD_READY_RESTART_REQUIRED`
