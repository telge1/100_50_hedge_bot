# Implementation Report — nested_profile_edge_signal_v1

**Datum:** 2026-09-04  
**Repo:** `/home/telgenbuescher/projects/orderbook_analyse`  
**Verdict:** `NESTED_PROFILE_EDGE_SIGNAL_READY_RESTART_REQUIRED`

---

## Geänderte / neue Dateien

| Datei | Änderung |
|-------|----------|
| `nested_profile_signal.py` | **NEU** — Contract, Registry, Dedup, per-Edge-Tracks, Replay |
| `manager.py` | Registry-Integration, nested eval/emit, Secondary-Suppression, Extension, Health |
| `capture_plan.py` | `nested_signal_count`, `nested_signals`, `nested_extension_applied_ids`, `secondary_edge_observation_count` |
| `config.py` | `nested_profile_signals_enabled`, `max_active_profile_watches=8` |
| `tests/test_nested_profile_edge_signal_v1.py` | **NEU** — 12 Tests |
| `tests/test_full_ob_edge_capture_timing_v1.py` | Secondary-Edge-Test für nested mode angepasst |

**Nicht geändert:** Resync-Checkpoint-Pfad (`continuity_contract.py`, `_write_resync_checkpoint`).

---

## Kernfunktionen

### `ProfileSignalRegistry` (`nested_profile_signal.py`)

- `register_profile()` — nach `PROFILE_UPDATE_DURING_CAPTURE`, skip parent profile
- `evaluate_profile()` — unabhängige VAH/VAL EdgeTracks
- `build_signal_if_cross()` — Dedup + nearest-edge bei Dual-Cross
- `stable_profile_id()` — deterministische Profilidentität
- `replay_minute_series()` — offline historische Regression

### `FullObEdgeFlightRecorder` (`manager.py`)

- `_register_nested_profile_from_pending()` — Profil in Registry bei pending update
- `_evaluate_nested_profile_signals()` — Tick-Pfad während offener Capture
- `_emit_nested_signal()` — Ledger + `NESTED_PROFILE_EDGE_SIGNAL` Marker
- `_append_nested_ledger()` → `nested_profile_signals.jsonl`
- `_maybe_extend_for_nested_signal()` — einmalige Extension pro Signal
- `SECONDARY_EDGE_TRIGGER` → `note_secondary_observation()` wenn nested enabled

---

## Lifecycle-Trennung

```text
Parent Capture:  FIGHT_ACTIVE controls writer/prebuffer/extension/finalize
Signal Registry: PROFILE_* states run independently on each tick
                 → NESTED_PROFILE_EDGE_SIGNAL without _start_or_merge_event
```

`FIGHT_ACTIVE` blockiert weiterhin Parent-`GENUINE_CROSS_IN` (by design). Nested-Signale umgehen diesen Guard über separaten Registry-Pfad.

---

## Dedup

Key: `symbol|profile_id|edge_kind|arm_cycle_id`  
10.000 identische Ticks → 1 Signal (verifiziert in `deduplication_audit.json`).

---

## Parent-/Epoch-Verknüpfung

Jedes Signal speichert `parent_fight_event_id`, `continuity_epoch_id`, `parent_segment_index`.  
Bei Reconnect-Gap: `signal_capture_continuous=false`, `signal_research_eligible=false` — Parent-`research_eligible` bleibt unverändert.

---

## Full-OB-Duplizierung

**Nein.** Ein Writer pro Symbol; Nested-Signale nur Marker + JSONL-Ledger in bestehendem Event-Root.

---

## Live-Sicherheit

| Check | Status |
|-------|--------|
| Collector PID 1565672 | unverändert |
| OI PID 147111 | unverändert |
| Restart durchgeführt | **NEIN** |
| Produktionsdaten geändert | **NEIN** |
| Commit/Push | **NEIN** |

---

## Nächster Schritt

Explizite Freigabe für **einen** kontrollierten Collector-Restart, der gleichzeitig aktiviert:

1. `FULL_OB_RESYNC_CHECKPOINT_READY_RESTART_REQUIRED`
2. `NESTED_PROFILE_EDGE_SIGNAL_READY_RESTART_REQUIRED`
