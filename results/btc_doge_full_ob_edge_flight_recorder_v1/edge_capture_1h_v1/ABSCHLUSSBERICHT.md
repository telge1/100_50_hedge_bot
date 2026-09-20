# Abschlussbericht — Full-OB Edge 1h Capture Timing

**Verdict:** `RESTART_BLOCKED_OPEN_EVENT_RECOVERY_REQUIRED`

Kein Collector-Restart, kein Dashboard-Restart, keine Env-Änderung, keine DB-Writes, kein Commit/Push.

## 1. Root Cause / alte Semantik

Siehe `PHASE0_TIMING_AUDIT.md`.

Kurz:

- 30-Minuten-Regel war **nur Segmentierung** (`_maybe_rotate_segment` → `SEGMENT_CONTINUED`), nicht Event-Ende.
- Event-Ende über `_maybe_end_event`: Default **15 min** Mindest-Post, **5 min** Reclaim-Tail, **90 min** Hard-Cap.
- Ringbuffer Default **5 min**, nicht 10.
- Trigger auch per `fast_approach` ohne Zoneneintritt; kein CROSS_IN/BOOTSTRAP/REARMED.
- SIGTERM: `Collector.run()` `finally` schloss OB200-Archive, **nicht** den Flight Recorder. PID **1467869** lädt diesen Disk-Fix **nicht** (Health ohne `writer_backlog` / `timing_contract` / `full_book_lock_hold_ns_last`).

## 2. Geänderte Dateien / Funktionen

| Datei | Änderung |
|---|---|
| `full_ob_edge_flight_recorder/config.py` | Defaults + `EDGE_CAPTURE_*` Mapping, `extension_minutes` |
| `full_ob_edge_flight_recorder/capture_plan.py` | Timing-Contract, `CapturePlan`, Segmente |
| `full_ob_edge_flight_recorder/watcher.py` | CROSS_IN, BOOTSTRAP, REARMED, Retouch/Secondary, Profil-Update-Marker |
| `full_ob_edge_flight_recorder/manager.py` | 60-min-Post, Extension, Hard-Limit, Marker, `shutdown()`, Event-Manifest |
| `full_ob_edge_flight_recorder/event_writer.py` | Segment-SHA-Felder, `extra_manifest` |
| `full_ob_edge_flight_recorder/replay.py` | Marker-Kanal ignorieren |
| `orderbook_v2_live/collector.py` | `finally` → `full_ob_flight_recorder.shutdown(INTERRUPTED_BY_CONTROLLED_RESTART)` |
| Tests | `test_full_ob_edge_capture_timing_v1.py` + Watcher-Anpassung |

Lock-Offload unverändert: Snapshot unter `_book_lock`, JSON/zstd/Disk im Writer-Thread, `depth=0` uncapped, OB1000/OB200 unangetastet.

## 3. Timing-Contract

`contract_version = full_ob_edge_capture_timing_v1`

| Contract | Bestehendes Feld | Env (neu, optional) | Env (alt) | Default jetzt |
|---|---|---|---|---|
| PRE 600s | `ringbuffer_minutes` | `EDGE_CAPTURE_PRE_SECONDS` | `OB_V3_FULL_OB_FR_RINGBUFFER_MIN` | 10 min |
| MIN POST 3600s | `minimum_post_capture_minutes` | `EDGE_CAPTURE_MIN_POST_SECONDS` | `OB_V3_FULL_OB_FR_MIN_POST_MIN` | 60 min |
| EXT 1800s | `extension_minutes` | `EDGE_CAPTURE_EXTENSION_SECONDS` | `OB_V3_FULL_OB_FR_EXTENSION_MIN` | 30 min |
| TAIL 600s | `reclaim_post_capture_minutes` | `EDGE_CAPTURE_RESULT_TAIL_SECONDS` | `OB_V3_FULL_OB_FR_RECLAIM_MIN` | 10 min |
| MAX 10800s | `maximum_event_minutes` | `EDGE_CAPTURE_MAX_SECONDS` | `OB_V3_FULL_OB_FR_MAX_EVENT_MIN` | 180 min |
| SEG 1800s | `segment_minutes` | `EDGE_CAPTURE_SEGMENT_SECONDS` | `OB_V3_FULL_OB_FR_SEGMENT_MIN` | 30 min |
| SEG 256 MiB | `max_open_tmp_bytes` | `EDGE_CAPTURE_SEGMENT_MAX_BYTES` | `OB_V3_FULL_OB_FR_MAX_OPEN_TMP_BYTES` | 256 MiB |

`normal_end_ts = max(trigger+3600s, result_ts+600s)`. Fight aktiv → +1800s, gleiche `fight_event_id`. Hard: `MAX_CAPTURE_DURATION_REACHED` / `UNRESOLVED_AT_CAPTURE_LIMIT`. Segment schließt nur Datei.

## 4. Prebuffer-Nachweis

Test `test_prebuffer_10min_taken`: Ringbuffer 650s Warmup, `first_persisted_ts <= trigger_ts`, `pre_trigger_seconds_actual >= 599`. Kürzere Laufzeit wird als `process_uptime_at_trigger_sec` dokumentiert, nie als falsche 600s behauptet. Leerer Buffer → `PREBUFFER_EMPTY` / `data_quality=INCOMPLETE`.

## 5. Mindestdauer-Nachweis

- `test_cannot_close_before_3600`
- `test_reclaim_at_2min_still_runs_60min` (Resultat-Marker, Capture bis +3600s)
- `test_result_at_minute_58_runs_to_68`
- `test_extension_at_60_and_90`
- `test_hard_limit_unresolved`

## 6. Segment vs Event

`test_segment_does_not_end_event`, `test_replay_multi_segment_same_fight_id`: gleiche `fight_event_id`, lückenloser `continuation_index`, `previous_segment_sha256` verkettet. 256-MiB-Pfad: `max_open_tmp_bytes=200` rotiert Segment, Event bleibt offen.

## 7. Dedup / Rearm

Retouch → `EDGE_RETOUCH`, kein zweites File. Andere Kante → `SECONDARY_EDGE_TRIGGER`. Nach Ende: COOLDOWN, bei Zone `OUT` → `REARMED`, dann neuer `CROSS_IN`. Bootstrap in der Zone ≠ `CROSS_IN`.

## 8. Tests

73 passed (`timing_v1` + FR + Lock-Offload + Sync + Full-Book + OB1000 + OB200 discovery).

## 9. Schutz der alten .tmp

PID **1467869** hält die Files offen (fds 16–19). Read-only Kopie:

`results/.../edge_capture_1h_v1/open_event_backup_readonly/`

| Symbol | fight_event_id | deltas.tmp | first ts/u/seq | last ts/u/seq | snap u | Writer-Backlog | Queue-Drops |
|---|---|---|---|---|---|---|---|
| BTCUSDT | `BTCUSDT_20260903T184212Z_4a22a89fe6` | 18 561 467 B | 1788460930473 / 4107240 / 804699175253 | 1788464412472 / 4124650 / 804729663044 | 4107241 | **nicht im Live-Health** (Alter Code) | nicht im Live-Health |
| DOGEUSDT | `DOGEUSDT_20260903T184212Z_f5d68293cd` | 2 111 723 B | 1788460931534 / 4106932 / 345785497659 | 1788464413734 / 4124343 / 345806031825 | 4106933 | dito | dito |

Originale **nicht** umbenannt/gelöscht. Scan der Kopie: zstd-Frames lesbar (`incomplete_frame: false` zum Kopierzeitpunkt).

## 10. PID / Restart

- Alt/live: **1467869** (ELAPSED ~58 min, RSS ~205 MB)
- Neu: **kein Restart**
- Dashboard / OI unverändert

SIGTERM dieses Prozesses würde `request_stop` setzen, OB200-Archive schließen, Full-Book `close()`, **Flight-Recorder-.tmp nicht finalisieren** (kein `shutdown` im geladenen Code; Writer schreibt synchron im Ingest-Thread des alten Builds). Daemon-Offload existiert live nicht.

## 11. Live-Smoke

Nicht nach Restart. Live (alter Code) zum Audit-Zeitpunkt:

- `collector_state=LIVE`, `book_ready` BTC/DOGE true, `gap_count=0`, `reconnect_count=0`
- `full_book_active_topics=2`
- Recorder `FIGHT_ACTIVE`, `capturing=true`, gleiche Event-IDs seit 18:42:12Z
- Lock-Offload und 1h-Contract **nicht** aktiv

## 12. Ressourcen / projected daily

Live-Health ohne `projected_daily_bytes` (altes Schema). RSS 204.69 MB. BTC-tmp ~18.6 MB / ~58 min ≈ grob 0.46 GB/Tag nur BTC-Full-OB-Eventstream, plus DOGE, plus OB200-Archive — **Schätzung**, kein Live-Feld.

## 13. Verbleibende Grenzen

- Nächster **einmaliger** Collector-Restart erst nach expliziter Freigabe, wenn `.tmp` entweder vom neuen `shutdown` finalisiert werden können **oder** Offline-Recovery nach Prozessende (fds geschlossen) Manifest+SHA schreibt.
- Live-Events laufen unter **alter** 15-min-Endelogik; sie können vor 60 min enden, sobald `_maybe_end_event` greift — oder bei 90 min Cap.
- Profil-Provider/ClickHouse unverändert; keine DB-Writes durch diese Arbeit.
- `fast_approach` startet **kein** Event mehr (nur CROSS_IN / Bootstrap).

## Recovery-Plan (kein Restart jetzt)

1. PID 1467869 weiterlaufen lassen; Originale nicht anfassen.
2. Read-only Backup liegt unter `open_event_backup_readonly/` (SCAN.json).
3. Wenn später SIGTERM des **neuen** Codes genehmigt: `collector.finally` ruft `shutdown` → Queue-Join, zstd close, `os.replace`, Manifest `INTERRUPTED_BY_CONTROLLED_RESTART`.
4. Wenn dieser **alte** PID beendet werden muss: zuerst Backup aktualisieren; nach Exit prüfen `lsof` auf die `.tmp`; dann Offline: zstd-Frame flushen falls nötig, `.tmp` → finale Namen, Manifest `INTERRUPTED_BY_CONTROLLED_RESTART`, SHA, Replay. **Kein** zweiten Collector auf dieselben offenen tmp starten.
5. Neuer Prozess schreibt neue `event_id`s; alte Verzeichnisse nicht überschreiben (Pfad enthält Event-ID).

`RESTART_BLOCKED_OPEN_EVENT_RECOVERY_REQUIRED`
