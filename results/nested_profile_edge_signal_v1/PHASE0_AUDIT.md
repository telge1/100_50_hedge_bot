# Phase 0 — Bestehender Signalpfad (Read-Only Audit)

**Audit-Quelle:** `results/full_ob_active_capture_signal_suppression_audit_v1/`  
**Verdict (vor Fix):** `ACTIVE_CAPTURE_BLOCKS_NEW_SIGNAL_CONFIRMED`

---

## Signalpfad während FIGHT_ACTIVE (vor nested_profile_edge_signal_v1)

```text
neues abgeschlossenes Profil (poll_profiles, ~20s)
  → ClickHouseCompletedProfileProvider.load
  → EdgeWatcher.set_edges
      if CAPTURING|FIGHT_ACTIVE|POST_CAPTURE → pending_profile_update (deferred)
  → _handle_open_event_tick
      → PROFILE_UPDATE_DURING_CAPTURE Marker
  → evaluate() auf jedem Tick (weiter aktiv)
      if lifecycle == FIGHT_ACTIVE → action=extend (niemals trigger)
  → nearest-edge Kind-Wechsel vs frozen trigger_edge
      → SECONDARY_EDGE_TRIGGER (level-triggered, jeder Tick)
  → _start_or_merge_event
      if sym in _writers → kein neues Event, kein GENUINE_CROSS_IN
```

---

## Wo SECONDARY_EDGE_TRIGGER erzeugt wird

| Datei | Funktion | Mechanismus |
|-------|----------|-------------|
| `manager.py` | `_handle_open_event_tick` | Wenn `decision.marker == "SECONDARY_EDGE_TRIGGER"` und nested deaktiviert → persistenter Marker |
| `watcher.py` | `evaluate` | Während FIGHT_ACTIVE: `nearest.kind != trigger_edge.kind` → marker gesetzt |

**Warum tausendfach:** Level-triggered, nicht edge-triggered. Jeder Tick mit abweichendem nearest-edge erzeugt einen Marker — keine Dedup. Beobachtet: ~7000 Marker pro Profil-Cutoff (09:00, 09:30, 10:00 UTC).

---

## Profilmetadaten (bereits vorhanden)

- `profile_id`, `session_start`, `cutoff`, `bracket_minutes`
- `tpo_source` (`volume_proxy_fallback` live)
- `volume_vah`, `volume_val`, `volume_poc`
- `frozen_edges` am Parent-Trigger (bleiben für Parent-Watcher maßgeblich)

---

## Arm-/Entry-/Rearm-Zustände

**Parent-Watcher (`watcher.py`):** IDLE → ARMED → CROSS_IN → CAPTURING/FIGHT_ACTIVE → … → REARMED  
**Neu (`nested_profile_signal.py`):** Separater `ProfileSignalRegistry` mit per-Edge-Tracks (VAH/VAL unabhängig):

```text
PROFILE_OBSERVING → PROFILE_ARMED → PROFILE_CROSS_IN → PROFILE_INSIDE → PROFILE_REARMED
```

Schwellen (live defaults): Arm 50 bps, Entry 20 bps, Rearm/OUT 75 bps.

---

## Profilverfolgung während Capture

- Neue Profile: parallel als `pending_profile_update`, nicht als Ersatz für frozen parent edges
- Nested-Registry: bis zu `max_active_profile_watches=8` pro Symbol, Eviction nach ältestem `cutoff`
- Signalberechtigung: ab `cutoff` (Profil vollständig abgeschlossen), bis EXPIRED oder Parent-Event-Ende

**UNFROZEN_RESEARCH_PARAMETER:** `profile_watch_ttl_after_cutoff` — keine willkürliche TTL eingeführt; Eviction nur bei RAM-Limit.

---

## Persistierte Marker (Writer-Queue)

| Marker | Wann | Nach Fix |
|--------|------|----------|
| `GENUINE_CROSS_IN` | Parent-Trigger, kein offener Writer | unverändert |
| `PROFILE_UPDATE_DURING_CAPTURE` | Neues Profil während Capture | unverändert |
| `SECONDARY_EDGE_TRIGGER` | nearest-edge Wechsel | **suppressed** (Zähler only) wenn nested enabled |
| `NESTED_PROFILE_EDGE_SIGNAL` | Kanonisches Nested-Signal | **neu** |
| Resync-Checkpoint-Marker | Reconnect-Epoche | unverändert (Fix intakt) |

---

## Parent-Event und Continuity-Epoch

- `fight_event_id`: `{symbol}_{trigger_ts}_{hash}` bei Parent-Start
- `continuity_epoch_id`: aus `ContinuityContract` / Resync-Gate (`manager._epoch`)
- Nested-Signale referenzieren: `parent_fight_event_id`, `parent_segment_index`, `continuity_epoch_id`

---

## Resync-Checkpoint-Fix (unverändert)

Geänderte Funktionen (vorheriger Fix, nicht angetastet):

- `continuity_contract.py` — Epoch-/Checkpoint-Vertrag
- `manager.py` — `_write_resync_checkpoint`, Reconnect-Gate, `research_eligible`-Matrix
- Tests: `tests/test_full_ob_resync_checkpoint_v1.py` (9/9 PASS)

**Verdict Resync-Fix:** `FULL_OB_RESYNC_CHECKPOINT_READY_RESTART_REQUIRED`

---

## Root Cause (bestätigt)

1. `FIGHT_ACTIVE` → `evaluate()` liefert nur `action=extend` → kein `GENUINE_CROSS_IN`
2. `_writers[sym]` blockiert zweite Capture-Datei
3. `SECONDARY_EDGE_TRIGGER` ohne Dedup → Marker-Flut ohne kanonisches Signal

**Lösung:** Getrennte Signal-Lifecycle-Engine (`ProfileSignalRegistry`) — nicht zweite Capture, sondern Erweiterung der Secondary-Edge-Beobachtung.
