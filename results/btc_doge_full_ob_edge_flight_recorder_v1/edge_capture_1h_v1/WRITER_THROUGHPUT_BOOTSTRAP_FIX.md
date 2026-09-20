# WRITER THROUGHPUT + BOOTSTRAP FIX

**ENDVERDICT:** `FULL_OB_WRITER_THROUGHPUT_AND_BOOTSTRAP_FIX_READY_RESTART_REQUIRED`

Live PID **1481866 was not restarted**. Code/tests/benchmarks are ready offline. A controlled restart is required to load the fix.

---

## 1. Bewiesene Root Cause

Read-only Messung an PID 1481866 + Code-Inspektion + Microbenchmark + Legacy-Recovery-Paketstatistik.

### Beweis A–F

| Hypothese | Ergebnis | Beweis |
|---|---|---|
| **A.** 1 Queue-Item pro WS-Paket | **TRUE** | `on_full_ob_message` machte genau ein `try_put` / Observer-Call; Observer wird einmal pro Bybit-Full-OB-Message aufgerufen |
| **B.** 1 Queue-Item pro Preislevel | **FALSE** | Recovered BTC: mean **~205–271 Levels/Paket**, 1 JSONL-Record/Paket |
| **C.** Full-Book statt Deltas | **FALSE** | Records enthalten `data.b`/`data.a` Delta-Arrays, nicht Full-Book-Snapshots ( gelegentlich große Delta-Pakete bis ~5100 Levels ) |
| **D.** flush/fsync pro Record | **FALSE** | `append_delta` schrieb nur in zstd `stream_writer`; `fsync` nur in `finalize` |
| **E.** Ein gemeinsamer Writer blockiert beide Symbole | **FALSE** | Je Symbol eigener `NonBlockingDeltaSink` + Thread; zwei offene Delta-FDs |
| **F.** JSON/zstd/Disk-Flaschenhals | **TRUE (unter Live-Last)** | Offline Microbench ~**45k msg/s**; Live-Writer nur ~**10 msg/s/Symbol** (aus Dateiwachstum); Ingress (Writes+Drops) ~**35+/s/Symbol** |

### Quantifizierung (kein Rechenfehler)

- Cutover-Smoke: ~**66 000 Drops / ~19 min ≈ 58 Drops/s gesamt** (nicht 33 000/s).
- Spätere Sample: ~**27–50 Drops/s gesamt**, korreliert mit `u`-Rate.
- BTC/DOGE `queue_drops` waren über **264 Health-Zeilen exakt gleich** — beide Queues dauerhaft gesättigt bei nahezu identischer Bybit-Update-Kadenz.
- `writer_backlog` in Health oft `0` trotz Drops: Queue füllt sich burstweise bis Cap (**4096**), Writer drainiert zwischen Health-Samples; Verlust bleibt.

### Root Cause (kausal)

1. Persistente Capture lief sofort (Bootstrap) und speicherte **jedes** Full-OB-Delta.
2. Writer-Pfad: **1× orjson + 1× zstd-write pro Item**, ohne Batch-Drain, unter GIL-Konkurrenz mit asyncio + OB200-Raw-Archive + zweitem Symbol.
3. Live-Writer-Durchsatz < Ingress → bounded Queue voll → fail-closed Drops.
4. Zusätzlich: Ringbuffer machte früher `orjson.dumps` pro Append nur zur Größenabschätzung (Hot-Path-Last vor Capture).

Artefakt: `PHASE0_WRITER_THROUGHPUT_MEASUREMENT.json`

---

## 2. Queue-Item-Granularität vorher/nachher

| | Vorher | Nachher |
|---|---|---|
| Item | `dict(payload)` shallow copy der dekodierten WS-Message | `build_delta_envelope(...)`: genau **ein Item pro Bybit-Delta** |
| Inhalt | gesamte Payload-Struktur (shared nested lists) | nur `topic/type/ts/cts` + kopierte `data.b`/`data.a` + `u`/`seq` + `receive_time_ns` |
| Full-Book | nein, aber unnötig große/shallow Struktur | explizit verboten |
| Level-Metrik | fehlte | `level_update_count` im Envelope |
| Hot-Path Size | `orjson.dumps` im Ringbuffer | `approx_envelope_bytes` ohne Serialize |

Modul: `record_envelope.py`

---

## 3. Writer-Architektur

**Entscheidung (Benchmark):** **ein Writer-Thread pro Symbol beibehalten** (Ordering + Isolation). Fairer Multi-Symbol-Single-Writer wurde nicht gewählt — Dual-Symbol-Lasttest mit per-symbol Writers war drop-frei bei 3× Peak.

Änderungen:

- `NonBlockingDeltaSink`: Batch-Drain (bis 64 Items / 256 KiB), Metrics, fail-closed Drops mit Level/Byte-Schätzung
- `ActiveEventWriter.append_delta_batch`: gebündeltes JSONL → streaming zstd
- **Kein fsync pro Delta**; `flush_pending` nur Intervall / Rollover / Finalize / Shutdown
- Default `queue_size` 4096 → **16384** (sekundär; allein unzureichend)
- Pflichtmetriken in `health_dict()`:
  - `ingress_messages_per_second`, `ingress_level_updates_per_second`
  - `writer_messages_per_second`, `writer_bytes_per_second`
  - `queue_backlog_items/bytes`, `queue_oldest_age_ms`, `queue_high_watermark`
  - `queue_drop_count`, `dropped_messages`, `dropped_price_level_updates`, `dropped_bytes_estimate`
  - `writer_batch_size`, `writer_flush_count`, `writer_error_count`

asyncio/Book-Lock wartet weiterhin **nicht** auf Disk (`put_nowait`).

---

## 4. Bootstrap-Semantik vorher/nachher

| | Vorher | Nachher |
|---|---|---|
| Start in Edge-Zone | `action=trigger` / `BOOTSTRAP_ALREADY_IN_EDGE_ZONE` → **öffnet Eventdatei** | `action=bootstrap_observe` → **nur Audit**, RAM-Ringbuffer |
| Persistente Datei | ja (`.tmp`) | **nein** (`bootstrap_persistent_capture=false`) |
| Echtes Signal | Bootstrap zählte wie Capture | nur `CROSS_IN` mit `edge_entry_crossed=true` erhöht `signal_count` |
| Ablauf | Bootstrap-Event unvollständig | Bootstrap → Exit → (Arm/)Rearm-Pfad → **CROSS_IN** → Datei + 10m Prebuffer + ≥60m Post |
| Zähler | fehlten | `bootstrap_observation_count` vs `signal_count` |

Live Bootstrap-Events von PID 1481866 (Annotation only, Dateien unangetastet):

`LIVE_BOOTSTRAP_EVENTS_RESEARCH_INELIGIBLE.json`

- `data_quality=INCOMPLETE_QUEUE_DROP`
- `trigger_quality=BOOTSTRAP_NOT_REAL_CROSS`
- `research_eligible=false`

---

## 5. 1× / 2× / 3× Benchmarks

Gemessene Peak-Referenz aus Live-Sampling: **~20 msg/s/Symbol** (konservativ über beobachtete `u`-Spitzen ~17/s).

Last aus Legacy-Recovery-Deltas (reale Paketgrößen BTC+DOGE), beide Symbole parallel, inkl. Segment-Rollover-Druck.

| Multiple | Target msg/s/Sym | Sent/Sym | `queue_drop_count` | Persist=Input | Replay | Crossed |
|---|---|---|---|---|---|---|
| 1× | 20 | 160 | **0** | yes | OK | false |
| 2× | 40 | 320 | **0** | yes | OK | false |
| 3× | 60 | 360 | **0** | yes | OK | false |

Artefakt: `WRITER_THROUGHPUT_BENCH.jsonl`

Slow-Disk-Test: Queue-Überlauf sichtbar (`try_put=False`), `drops>=1`, Event fail-closed markierbar — kein stiller Drop.

---

## 6. Queue- / Ingress- / Writer-Metriken

Beispiel 1× Bench-Health:

- `ingress_messages_per_second` ≈ 40 (beide Symbole)
- `writer_messages_per_second` ≈ 40
- `ingress_level_updates_per_second` ≈ 5800
- `writer_bytes_per_second` ≈ 120 kB/s
- `queue_high_watermark` = 1
- `queue_drop_count` = 0

Live vorher (PID 1481866, unverändert): Writer ~10 msg/s/Sym, Drops steigen weiter.

---

## 7. Timing-Regression

Contract unverändert:

- Prebuffer 600 s
- min post 3600 s
- Extension 1800 s
- Result-Tail 600 s
- Hard-Cap 10800 s
- Segment 1800 s / 256 MiB (beendet Event nicht)
- Retouch ≠ Doppel-Event
- Rearm erforderlich für neuen Fight

Zusatztests grün: Bootstrap Upper/Lower ohne Datei; Bootstrap→Exit→CROSS_IN Upper/Lower; Warmup-Prebuffer; kein Signal ⇒ keine Datei; Queue-Drop ⇒ `research_eligible=false`; 1 Item = 1 Delta; Multi-Symbol-Order; Segment-Burst ohne Drop.

---

## 8. Geänderte Dateien / Funktionen

**orderbook_analyse** (Live-Prozess lädt dies noch nicht):

- `full_ob_edge_flight_recorder/record_envelope.py` *(neu)*
- `full_ob_edge_flight_recorder/async_sink.py` — Batch-Drain, Metrics
- `full_ob_edge_flight_recorder/event_writer.py` — `append_delta_batch`, `flush_pending`
- `full_ob_edge_flight_recorder/ringbuffer.py` — kein Hot-Path-orjson
- `full_ob_edge_flight_recorder/config.py` — queue/batch/flush Defaults
- `full_ob_edge_flight_recorder/watcher.py` — `bootstrap_observe`
- `full_ob_edge_flight_recorder/manager.py` — Envelope, Bootstrap-Gate, Health-Metriken, `signal_count`
- `full_ob_edge_flight_recorder/capture_plan.py` — `research_eligible`, `trigger_quality`

**Tests:**

- `tests/test_full_ob_writer_throughput_bootstrap_v1.py` *(neu)*
- Anpassungen: `test_full_ob_edge_capture_timing_v1.py`, `test_full_ob_edge_flight_recorder_v1.py`, `test_full_ob_socket_lock_offload.py`

---

## 9. Tests

```
49 passed
```

Suites: throughput/bootstrap + timing + FR v1 + lock-offload + legacy recovery.

---

## 10. Erwarteter Live-Restart

**Nicht in dieser Aufgabe.** Nächster kontrollierter Cutover soll:

1. Offline-Fix bereits geladen (dieser Stand)
2. PID 1481866 einmal stoppen (SIGTERM)
3. Legacy/Bootstrap-`.tmp` der laufenden Events nicht fortsetzen
4. Neu starten mit gleicher Env (`OB_V3_FULL_OB_FLIGHT_RECORDER_ENABLE=true`, BTC/DOGE)
5. Erwartung: **kein** persistentes Event bis echter CROSS_IN nach Exit/Rearm; nach Warmup voller 600 s-Prebuffer; `queue_drop_count=0` unter Last

---

## 11. Verbleibendes Risiko

- Live-Prozess 1481866 schreibt weiterhin droppende Bootstrap-Events (bekannt, unverändert gelassen).
- Extreme Burst >3× Peak oder sehr langsame Disk kann Queue erneut füllen — dann fail-closed `INCOMPLETE` / `research_eligible=false` (sichtbar).
- Raw WS-Bytes werden noch nicht durchgereicht (Collector dekodiert zuerst); Envelope kopiert Delta-Arrays statt Raw-Bytes — akzeptabel, Contract erfüllt.
- Segment-Replay über viele Continuations weiter beobachten.

---

## ENDVERDICT

```
FULL_OB_WRITER_THROUGHPUT_AND_BOOTSTRAP_FIX_READY_RESTART_REQUIRED
```
