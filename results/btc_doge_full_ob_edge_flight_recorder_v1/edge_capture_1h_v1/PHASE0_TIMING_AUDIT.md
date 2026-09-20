# Phase 0 — Full-OB Edge Capture Timing Audit

Stand: Code in `orderbook_analyse` **vor** `full_ob_edge_capture_timing_v1` (Defaults und Pfade wie im geladenen Modul). Live-Prozess PID **1467869** hat diesen Code **nicht** neu geladen.

## 1. 30-Minuten-Regel: Segment oder Event-Ende?

**Antwort: (a) nur Dateisegmentierung**, sofern `segment_minutes` / `_maybe_rotate_segment` gilt.

Beleg:

- Default `FlightRecorderSettings.segment_minutes = 30.0` (`config.py`).
- Env: `OB_V3_FULL_OB_FR_SEGMENT_MIN` (Default `"30"`).
- `FullObEdgeFlightRecorder._maybe_rotate_segment`: wenn `(now - writer.started_at) >= segment_minutes * 60` **oder** `open_tmp_bytes >= max_open_tmp_bytes` (Default `256 * 1024 * 1024`) → `_rotate_segment`.
- `_rotate_segment` finalisiert das **aktuelle** Writer-Paket mit Status `SEGMENT_CONTINUED`, gleiche `fight_event_id`, `continuation_index + 1`, neues Verzeichnis `cont_NNN/`. Es ruft **nicht** `_finalize_event` / `end_capture` auf.

Zusätzlich existiert eine **andere** 30-Minuten-Zahl: `profile_window_minutes = 30` für das kausale abgeschlossene Profilfenster (`ClickHouseCompletedProfileProvider` / `last_completed_window`). Das ist **kein** Capture-Ende.

## 2. Welche Bedingungen schließen ein Event?

Funktion: `FullObEdgeFlightRecorder._maybe_end_event` (aufgerufen aus `tick` bei Watcher-Action `extend`).

Elapsed: `(now - st.capture_started_at).total_seconds()`.

| Bedingung | Status |
|---|---|
| `elapsed >= maximum_event_minutes * 60` | `MAX_DURATION_REACHED` |
| `reclaim_seen` und nicht „active“, nach `post_until` und `elapsed >= reclaim_post` | `COMPLETE_WITH_NON_ATOMIC_STREAM_LIMIT` |
| gekreuzt, nicht active, `elapsed >= min_post` | `COMPLETE_WITH_NON_ATOMIC_STREAM_LIMIT` |
| **nicht** gekreuzt, nicht active, `elapsed >= min_post` | `COMPLETE_WITH_NON_ATOMIC_STREAM_LIMIT` |

„active“ in diesem Code:

```
acceptance_active OR outside_since is not None OR last_sample.distance_bps <= capture_distance_bps
```

Defaults (`config.py` / `load_flight_recorder_settings`):

- `minimum_post_capture_minutes = 15.0` (`OB_V3_FULL_OB_FR_MIN_POST_MIN`, Default `"15"`)
- `reclaim_post_capture_minutes = 5.0` (`OB_V3_FULL_OB_FR_RECLAIM_MIN`, Default `"5"`)
- `maximum_event_minutes = 90.0` (`OB_V3_FULL_OB_FR_MAX_EVENT_MIN`, Default `"90"`)

Nach Finalize: `watcher.end_capture` → Lifecycle `COOLDOWN` für `cooldown_minutes = 5.0`; **und** `release_keeper_lease` (Full-OB-Lease kann wegfallen bis zum nächsten `tick`/`ensure_keeper_lease`).

Kein Shutdown-Pfad: `collector.run()` `finally` ruft **kein** Flight-Recorder-Finalize auf (siehe Frage 6 / Phase 4).

## 3. Wird nach Trigger mindestens 60 Minuten weitergeschrieben?

**Nein.** Mindest-Post ist **15 Minuten** (`minimum_post_capture_minutes`), nicht 60.

Zusätzlich: wenn der Mid **nicht** mehr `<= capture_distance_bps` (Default 20 bps) und kein Outside/Acceptance, darf `_maybe_end_event` bereits bei `elapsed >= 15 min` schließen — auch ohne Reclaim/Breakout (`not st.crossed and elapsed >= min_post and not active`).

Reclaim-Nachlauf ist **5 Minuten** (`reclaim_post_capture_minutes`), nicht 10.

Hard-Cap ist **90 Minuten**, nicht 3 Stunden.

## 4. Pre-Trigger-Ringbuffer

`BoundedRawRingBuffer` (`ringbuffer.py`), erzeugt in `_buf()`:

- `window_sec = ringbuffer_minutes * 60`
- Default `ringbuffer_minutes = 5.0` → **300 Sekunden**, nicht 600
- Env: `OB_V3_FULL_OB_FR_RINGBUFFER_MIN` Default `"5"`
- `max_buffer_messages = 50_000`
- `max_bytes = 256 MiB`

Eviction: Nachrichten älter als `window_sec` **oder** über Message/Byte-Cap (`overflow_count` / `dropped_oldest`).

Beim Trigger: `_start_or_merge_event` → `flush()` → `sink.try_put`. `prebuffer_start_ns = flushed[0].receive_time_ns` falls nicht leer. Es gibt **keine** Prüfung `pre_trigger_seconds_actual >= 600` und keine Warmup-Dokumentation gegen Prozessstart. Leerer Buffer → kein `prebuffer_start_ns`, Event startet trotzdem; `pre_trigger_incomplete` nur wenn `book_ready=False`.

Außerhalb eines Events: nur RAM-Ringbuffer, keine dauerhafte Full-OB-Rohdatei (Writer existiert nur in `_writers`).

## 5. Wiederholter Tick in derselben Edge-Zone → neues Event?

**Kein zweites Writer-File**, solange bereits ein Writer für das Symbol existiert.

Pfad:

- Während `CAPTURING` / `FIGHT_ACTIVE` / `POST_CAPTURE`: `EdgeWatcher.evaluate` gibt immer `action="extend"` zurück (kein `trigger`).
- Falls dennoch `trigger`: `_start_or_merge_event` bei `sym in self._writers` merged nur `trigger_meta["edges"]` und **return**.

Kein `EDGE_RETOUCH`-Marker, kein `retouch_count`. Timer (`capture_started_at`) wird **nicht** neu gesetzt (kein Reset), aber es gibt auch **keinen** festen `minimum_capture_end_ts = trigger+3600`.

Nach Event-Ende: 5-Minuten-Cooldown, danach wieder `IDLE`/`BOOK_READY` → erneut `arm` bei `dist <= 50 bps` und `trigger` bei `dist <= 20` **oder Contact oder Cross oder fast_approach**. Das ist **kein** Rearm-CROSS_IN-Vertrag. Start bereits in der Zone wird **nicht** als `BOOTSTRAP_ALREADY_IN_EDGE_ZONE` markiert.

`fast_approach` (Default 8 bps/s) kann ein Event starten, **ohne** Zoneneintritt (`test_watcher_fast_approach_trigger`).

## 6. Offenes Event: gleiche Kante / andere Kante / Profil / u=1 / Gap

| Ereignis | Ist-Verhalten |
|---|---|
| Gleiche Kante erneut | `evaluate` → `extend`. Kein Marker. Capture läuft weiter. |
| Andere Profilkante | `nearest` kann wechseln; immer `extend`. `_start_or_merge` würde Secondary-Edge nur in `trigger_meta["edges"]` schreiben, wird aber bei offenem Event nicht über `trigger` erreicht. **Kein** `SECONDARY_EDGE_TRIGGER`. |
| Profil-Update | `EdgeWatcher.set_edges`: bei CAPTURING/FIGHT_ACTIVE/POST_CAPTURE **return ohne Update**. Frozen `frozen_edges` / `frozen_profile` bleiben. **Kein** Marker `PROFILE_UPDATE_DURING_CAPTURE`. |
| `u=1` | `FullBookOnDemandManager.handle_message`: `U_RESET` → Book clear, `resync_needed`, Observer `outcome=u_reset`. Recorder schreibt Delta in Sink (wenn capturing) oder Buffer. **Kein** explizites `data_quality=INCOMPLETE` / `RESYNC` am Event, außer `writer.gap_count` einmalig vom Runtime-Zähler beim **Start**. |
| u-Gap | Book clear + `gap_count++` auf Runtime. Observer `outcome=gap`. Event-Status wird dadurch **nicht** automatisch `INCOMPLETE`. `writer.gap_count` wird beim Start kopiert, während Capture nicht fortlaufend aus Runtime nachgezogen. Queue-Drop setzt `INCOMPLETE_QUEUE_DROP` / `DEGRADED`. |

## Trigger-Pfad (vollständig)

`Collector._session` Loop → `full_ob_flight_recorder.tick()` → `poll_profiles` → pro Symbol Mid aus Full-Book (sonst OB200 `mid_provider`) → `EdgeWatcher.evaluate` → bei `trigger` `_start_or_merge_event`.

Trigger in `ARMED` (`watcher.py`): `dist <= capture_bps` **oder** `contact` (`dist <= 1`) **oder** Preis-Cross VAH/VAL **oder** `approach >= fast_approach`.

## SIGTERM / Shutdown (Phase-4-relevant)

`async_main`: SIGTERM → `collector.request_stop()` (`_stop` Event).

`Collector.run()` `finally` (Reihenfolge):

1. `request_stop`
2. On-Demand-Socket stop
3. `full_book.close()`
4. ClickHouse-Writer join (archive-only: skip)
5. `raw_archive.stop()` (OB200-Segmente werden geschlossen)
6. Health `STOPPED`

**Kein** Aufruf `full_ob_flight_recorder.shutdown` / `_finalize_event`. Offene `.tmp` bleiben bei Kill des **laufenden** Prozesses unfinalisiert. Auch der Lock-Offload-Code auf Disk (nicht im PID 1467869) hatte diesen Hook noch nicht.

Daemon-Writer-Thread (`NonBlockingDeltaSink`, `daemon=True`): Prozessende bricht den Thread ohne garantiertes zstd-`FLUSH_FRAME` / `os.replace`.

## Mapping vorhandener Env-Namen (vor Contract-Änderung)

| Gewünschter Contract | Vorhanden | Default vorher |
|---|---|---|
| PRE 600s | `OB_V3_FULL_OB_FR_RINGBUFFER_MIN` | 5 min = 300s |
| MIN POST 3600s | `OB_V3_FULL_OB_FR_MIN_POST_MIN` | 15 min |
| Extension 1800s | — | nicht vorhanden |
| Result tail 600s | `OB_V3_FULL_OB_FR_RECLAIM_MIN` (nur nach Reclaim) | 5 min |
| Max 10800s | `OB_V3_FULL_OB_FR_MAX_EVENT_MIN` | 90 min |
| Segment 1800s | `OB_V3_FULL_OB_FR_SEGMENT_MIN` | 30 min |
| Segment 256 MiB | `OB_V3_FULL_OB_FR_MAX_OPEN_TMP_BYTES` | 256 MiB |

`contract_version` Speicher: `full_ob_edge_flight_recorder_v1`.
