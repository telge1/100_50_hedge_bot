# Contract: nested_profile_edge_signal_v1

## Zwei getrennte Zustandsautomaten

### Capture-Lifecycle (unverändert)

```text
IDLE → ARMED → FIGHT_ACTIVE → POST_CAPTURE → COOLDOWN
```

Steuert: Full-OB-Öffnung, Prebuffer, Nachlauf, Extension, Segmentierung, Finalisierung.

### Profil-Signal-Lifecycle (neu, entkoppelt)

```text
PROFILE_OBSERVING → PROFILE_ARMED → PROFILE_CROSS_IN → PROFILE_INSIDE → PROFILE_REARMED → PROFILE_EXPIRED
```

Läuft parallel während `FIGHT_ACTIVE`. Erzeugt kanonische Signale ohne zweite Capture.

---

## profile_id (deterministisch)

Abhängig von: `symbol`, `profile_basis`, `profile_window_minutes`, `profile_start`, `profile_end`, `calculation_version`, `profile_fallback_used`, `vah`, `val`, `poc` (stable decimal keys, SHA256-Digest).

Format: `{SYMBOL}_{start}_{end}_v{version}_{digest12}`

---

## NestedSignalRecord (Pflichtfelder)

```text
nested_signal_contract = nested_profile_edge_signal_v1
nested_signal_id
parent_fight_event_id
continuity_epoch_id
symbol
signal_ts
receive_time_ns
profile_id
profile_basis
profile_window_minutes
profile_start_ts
profile_end_ts
profile_calculation_version
profile_fallback_used
true_tpo_computed
vah / val / poc
edge / edge_side / edge_price
trigger_price / distance_bps
arm_threshold_bps / entry_threshold_bps / rearm_threshold_bps
arm_ts / cross_ts / arm_cycle_id
causal_cutoff_ts
capture_status
signal_capture_continuous / signal_research_eligible
dedup_key
```

Live ehrlicher Vertrag: `profile_basis=VOLUME`, `profile_fallback_used=true`, `true_tpo_computed=false`.

---

## Kausale Signalregeln

| Regel | Schwellen |
|-------|-----------|
| Arm | ≥50 bps von Kante (zone ≠ IN) |
| Entry/Cross | ≤20 bps (zone IN) |
| Rearm | ≥75 bps (zone OUT) |

Signal nur wenn:

1. Profil abgeschlossen (`now >= cutoff`)
2. Kausaler Cutoff = Profilende
3. Edge-Track korrekt armed
4. Preis crossed kausal in Entry-Zone (saw_outside vorher)
5. Arm-Zyklus noch kein Signal emittiert

**Bootstrap:** Preis bereits IN bei Profil-Registrierung → `BOOTSTRAP_ALREADY_IN_EDGE_ZONE`, kein Signal bis Rearm + Cross.

**Dual-Edge-Tick:** Wenn VAH und VAL gleichzeitig IN → nearest-edge by `distance_bps`.

---

## Dedup-Key

```text
symbol|profile_id|edge_kind|arm_cycle_id
```

- Ein Tick-Sturm → ein Signal
- Same-Timestamp-Batch → ein Signal
- Rearm → neue `arm_cycle_id` → neues Signal erlaubt
- UPPER/LOWER getrennt (per-Edge-Tracks)
- Restart/Replay: deterministisch via `_dedup_keys` Set

---

## Persistenz

1. Keine zweite Full-OB-Datei / kein zweiter Writer
2. Marker: `NESTED_PROFILE_EDGE_SIGNAL` in Parent-Queue
3. Ledger: `{event_root}/nested_profile_signals.jsonl`
4. Parent-Verknüpfung: `parent_fight_event_id`, `parent_segment_index`, `continuity_epoch_id`
5. Reconnect-Gap: `signal_capture_continuous=false`, `signal_research_eligible=false`

---

## Extension-Semantik

- Maximal **eine** Extension pro `nested_signal_id`
- Grund: `NESTED_PROFILE_EDGE_SIGNAL`
- Monotones `normal_end_ts`, Hard-Cap unverändert
- Segment-Rollover beendet weder Parent noch Ledger

---

## Health-Metriken

```text
PARENT_CAPTURE_COUNT
GENUINE_PARENT_SIGNAL_COUNT
NESTED_PROFILE_EDGE_SIGNAL_COUNT
SECONDARY_OBSERVATIONS_SUPPRESSED
ACTIVE_PROFILE_WATCH_COUNT
secondary_edge_observation_count
nested_signal_count
duplicate_secondary_trigger_suppressed_count
profile_arm_count / profile_rearm_count / profile_expiry_count
```

Lifetime-Zähler über Segment-Rollover hinweg (Registry-Level, nicht pro Segment reset).

---

## Aktivierung

Env: `OB_V3_FULL_OB_FR_NESTED_SIGNALS_ENABLE=1` (Default in Code: `True` für Offline-Tests; Live erst nach Restart).
