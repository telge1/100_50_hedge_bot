# Backfill Recovery v1

## 1. Finales Verdict

`BTC_DOGE_RESEARCH_DB_BACKFILL_RECOVERED_AND_RESUMED`

Root Cause behoben, ClickHouse-Fortschritt rekonstruiert, Smoke/Idempotenz bestanden,
Full-History-Backfill per `nohup --resume` gestartet.

## 2. Repository / Branch / HEAD / Dirty

- Branch: `feature/btc-doge-research-db`
- HEAD: `2af0477` (`research: add resumable BTC DOGE full history backfill`)
- Uncommitted: Recovery-Fixes (atomic JSON, CANDLES-Contract, Lock/PID, `backfill_recovery.py`, Tests)
- Kein Commit, kein Push

## 3. Primäre CANDLES-Ursache

**Kette:** `build_modality_coverage()` → `build_backfill_plan()` → `_filter_plan()` → `_load_segment()` → `load_segment()`

Der **erste** nohup-Lauf (vor dem CANDLES-Fix in `backfill_plan._eligibility`) markierte CANDLES als `ELIGIBLE`.
`_filter_plan()` filterte nur `eligibility == "ELIGIBLE"`, nicht `IMPORTABLE_MODALITIES`.
CANDLES erreichte `load_segment()`, das keine Loader-Implementierung hat → `ValueError: unsupported modality: CANDLES`.

**Batch in ClickHouse:** `fh:BTCUSDT:CANDLES:20260719T000000Z:20260720T000000Z:CLICKHOUSE_C` → `FAILED` mit genau dieser Meldung.

**Fix:**
- `IMPORTABLE_MODALITIES` Allowlist (ohne CANDLES)
- Plan-Feld `import_eligible=false` / `target_mode=COVERAGE_ONLY` für CANDLES
- `_assert_importable_modality()` vor Batch-Registrierung
- `ModalityContractError` in `segment_loader` als Contract-Gate

## 4. Ursache der beschädigten progress.json

**Mechanismus:** `write_progress()` in `run_state.py` (alt) nutzte **read-modify-write ohne Lock** via `path.write_text()`.

**Beweis aus Datei** (`run/btc_doge_research_full_history/progress.json`):
- Zeilen 1–36: vollständiges JSON (`updated_at: ...200254Z`)
- Zeilen 38–39: trailing Fragment (`updated_at: ...200243Z`) — zweites, unvollständiges JSON-Endstück

Die Timestamps liegen **11 µs** auseinander → zwei nahezu gleichzeitige Writes.

**Konkurrierende Prozesse (Logs + PIDs):**
1. Erster Start: `nohup.pid` = **1201611** (Shell-Wrapper, existiert nicht mehr)
2. Zweiter Start: Runner-PID **1202258** (Python, tatsächlicher Backfill)

Zwei nohup-Starts kurz hintereinander (first run CANDLES crash + second run nach Plan-Fix) erzeugten parallele RMW-Writes auf dieselbe Datei.

## 5. Writer und konkurrierende Pfade

| Funktion | Datei | Alt | Neu |
|---|---|---|---|
| `write_progress()` | progress.json | RMW + write_text | atomar via `atomic_write_json` |
| `write_heartbeat()` | heartbeat.json | write_text | atomar + Terminal-State-Schutz |
| `update_watermark()` | watermarks.json | RMW + write_text | atomar |
| `acquire_runner_lock()` | runner.pid, runner.lock | backfill.pid | runner.pid + runner_owner.json |
| `finally` in `run_backfill()` | heartbeat | **immer COMPLETED** | COMPLETED nur bei Erfolg; Exception → FAILED |

Kein separater Heartbeat-Thread. Kein Signal-Handler schrieb Progress (nur SIGTERM → STOPPING).

## 6. PID-Differenz 1201611 vs. 1202258

| PID | Rolle | Status |
|---|---|---|
| 1201611 | Shell `$!` aus erstem nohup (in `nohup.pid` gespeichert) | stale, beendet |
| 1202258 | Tatsächlicher Python-Runner (in `backfill.pid`) | beendet nach Crash |

**Fehler:** `nohup.pid` speicherte Shell-PID statt Python-Runner-PID.

**Fix:** getrennte Dateien `launcher.pid` und `runner.pid` + `runner_owner.json` mit Boot-Time für PID-Reuse-Erkennung.

## 7. ClickHouse-Fortschritt vor Recovery

Quelle: `research_batch_runs WHERE batch_id LIKE 'fh:%'`

| Metrik | Wert |
|---|---|
| READY-Zeilen (batch_runs) | 58 |
| Eindeutige READY-Segmente | **46** |
| FAILED | 1 (CANDLES) |
| Verwaiste RUNNING (ohne READY) | 0 (gruppiert: RUNNING+READY → READY) |
| Phase-2-Pilot | unverändert (`phase2:20260826`) |

Progress-JSON (`completed: 43/45`) war ** nicht** Source of Truth — zählte Loop-Iterationen inkl. Skips und korrupte Writes.

## 8. READY / PARTIAL / FAILED / ORPHANED / offen

| Status | Anzahl |
|---|---|
| Coverage-Inventar gesamt | 2.580 |
| COVERAGE_ONLY (CANDLES) | 86 |
| Import-eligible | 662 |
| READY (ClickHouse, unique) | 46 |
| PARTIAL | 0 |
| FAILED | 1 (CANDLES, pre-fix) |
| ORPHANED RUNNING | 0 |
| Offen | 616 |

## 9. Gesicherte korrupte Datei

- SHA256: `34f2d0879613207b99ecf403357813454555dabc87cc4cca35055f823d99d37e`
- Größe: 1.296 Bytes
- Kopie: `results/btc_doge_research_db_backfill_recovery_v1/progress.json.corrupted`
- Trailing-Fragment: `results/btc_doge_research_db_backfill_recovery_v1/progress.json.trailing_fragment.txt`
- Metadaten: `results/btc_doge_research_db_backfill_recovery_v1/progress_corruption.json`

## 10. CANDLES-Contract nach Fix

```
modality=CANDLES
target_mode=COVERAGE_ONLY
import_eligible=false
source=signal_generator.candles_1m
→ niemals segment_loader
→ zählt nicht in importable_segments
→ blockiert keine anderen Modalitäten
```

## 11. Atomarer JSON-Writer

Neu: `research/btc_doge_research/atomic_json.py`
- `fcntl.flock` (inter-process)
- Thread-Lock (intra-process)
- temp → fsync → `os.replace`
- JSON parse-validate vor replace

## 12. Lock / PID-Ownership

Neu:
- `runner.lock`, `runner.pid`, `launcher.pid`, `runner_owner.json`
- Zweiter Start → `ALREADY_RUNNING`
- Owner-only Lock-Release
- PID-Reuse-Check via `/proc/PID/stat` starttime

## 13. Heartbeat-State-Machine

Erlaubt: STARTING, RUNNING, STOPPING, STOPPED, FAILED, COMPLETED

- Exception → **FAILED** (mit error_type, failed_modality, failed_segment)
- Exception → **niemals COMPLETED** (Terminal-State-Schutz)
- SIGTERM → STOPPING → STOPPED
- Erfolg → COMPLETED

Felder: `importable_segments`, `ready_segments`, `skipped_segments`, `failed_segments`, `remaining_segments`

## 14. Recovery aus ClickHouse

`rebuild_run_state_from_clickhouse()`:
- Rekonstruiert progress.json, heartbeat.json, watermarks.json
- Output: `results/btc_doge_research_db_backfill_recovery_v1/clickhouse_progress.json`
- CLI: `--rebuild-state`, `--secure-corrupted`

## 15. Tests

```
tests/research/test_btc_doge_research_full_history.py — 12 passed
tests/research/test_btc_doge_research_phase2.py — 7 passed
git diff --check — PASS
```

Abgedeckt: CANDLES COVERAGE_ONLY, atomare Writes (Threads/Prozesse), trailing JSON, Lock, ModalityContractError.

## 16. Resume-Smoke

`--run --resume --max-segments 3`:
- Run 1: 3× IDEMPOTENT_SKIP, status COMPLETED
- Run 2: 3× IDEMPOTENT_SKIP, status COMPLETED
- progress.json + heartbeat.json: valides JSON

## 17. Idempotenz

Smoke bestätigt: keine neuen Facts, keine doppelten Volumina bei wiederholtem Resume.

## 18. nohup-Full-Backfill gestartet

**Ja** — `2026-09-02T19:48:57Z`

## 19. Launcher-PID

`run/btc_doge_research_full_history/launcher.pid` → Shell-PID des nohup-Aufrufs (Beispiel: 1207201)

## 20. Runner-PID

`run/btc_doge_research_full_history/runner.pid` → **1207220** (Python)

## 21. Logpfad

`logs/btc_doge_research_full_history.log` (append, Resume-Marker gesetzt)

## 22. Heartbeat / Progress nach Start

```json
{
  "status": "RUNNING",
  "runner_pid": 1207220,
  "importable_segments": 662,
  "skipped_segments": 45,
  "ready_segments": 1,
  "remaining_segments": 616,
  "completed": 46
}
```

progress.json: valides JSON, `current_segment` sichtbar (LIQUIDATIONS).

## 23. Monitoring-Befehle

```bash
# Runner-PID (nicht launcher.pid für Prozesscheck)
RUNNER_PID=$(cat run/btc_doge_research_full_history/runner.pid)
LAUNCHER_PID=$(cat run/btc_doge_research_full_history/launcher.pid)

ps -p "$RUNNER_PID" -o pid,etime,rss,cmd
cat run/btc_doge_research_full_history/runner_owner.json
cat run/btc_doge_research_full_history/heartbeat.json
python3 -m json.tool run/btc_doge_research_full_history/progress.json

tail -f logs/btc_doge_research_full_history.log

.venv/bin/python -m research.btc_doge_research.full_history_runner --status

.venv/bin/python -c "
from research.btc_doge_research.clickhouse import connect, rows
c=connect()
print(rows(c, \"SELECT status,count() FROM btc_doge_research.research_batch_runs WHERE batch_id LIKE 'fh:%' GROUP BY status\"))
c.close()"

# Stoppen (READY-Daten bleiben)
kill -TERM "$RUNNER_PID"

# Resume
echo \"=== RESUME \$(date -u +%Y-%m-%dT%H:%M:%SZ) ===\" >> logs/btc_doge_research_full_history.log
nohup env PYTHONUNBUFFERED=1 LAUNCHER_PID=$$ .venv/bin/python -m research.btc_doge_research.full_history_runner --run --resume >> logs/btc_doge_research_full_history.log 2>&1 &
echo $! > run/btc_doge_research_full_history/launcher.pid
```

## 24. Collector-/Live-Sicherheit

PIDs unverändert: 147111, 1661773, 3946369. Keine Collector-/Dashboard-/Quelltabellen-Änderungen.

## 25. Kein Commit / Push

Nicht durchgeführt.
