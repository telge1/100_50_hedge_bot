# Full-OB Active Capture — Signal Suppression Audit v1

**Verdict:** `ACTIVE_CAPTURE_BLOCKS_NEW_SIGNAL_CONFIRMED`  
**Audit time (UTC):** 2026-09-04T10:30:50Z  
**Collector PID:** 1565672 (unchanged) | **OI PID:** 147111 (unchanged)

---

## Executive summary

`FIGHT_ACTIVE` **blockiert die Speicherung neuer `GENUINE_CROSS_IN`-Signale** vollständig. Der Watcher läuft weiter, neue 30m-Profile werden geladen und als Marker protokolliert, aber der Lifecycle-Zweig erlaubt kein `action=trigger`, und `_start_or_merge_event` verhindert eine zweite Capture-Datei.

Die laufenden Events (BTC/DOGE seit ~08:05 UTC) sind **regulär aktiv**, nicht festgehangen: drei Extensions (`BREAKOUT_ACCEPTANCE_PENDING`) haben `normal_end_ts` auf ~10:35 UTC verschoben; Hard-Cap ~11:05 UTC.

---

## Phase A — Live-Zustand

| | BTCUSDT | DOGEUSDT |
|---|---------|----------|
| Lifecycle | `FIGHT_ACTIVE` | `FIGHT_ACTIVE` |
| Event-ID | `…080534Z_1fd9a66d36` | `…080551Z_2c38905508` |
| Trigger UTC | 08:05:34 | 08:05:51 |
| Capture-Dauer | ~2,4 h | ~2,4 h |
| `minimum_end` | 09:05:34 ✓ past | 09:05:51 ✓ past |
| `normal_end_ts` | **10:35:34** | **10:35:51** |
| `hard_capture_end_ts` | **11:05:34** | **11:05:51** |
| Extensions | 3 @ 09:05, 09:35, 10:05 | 3 (gleiches Muster) |
| Extension-Grund | `BREAKOUT_ACCEPTANCE_PENDING` | gleich |
| Retouches | 1 | 14 |
| Segment | cont_004 `.tmp` offen | cont_004 `.tmp` offen |
| Queue backlog / drops | 0 / 0 | 0 / 0 |
| Writer errors | 0 | 0 |
| Frozen VAH/VAL | 80765 / 80635 | 0.087315 / 0.08707 |
| Profil (frozen) | 07:30–08:00 UTC | 07:30–08:00 UTC |
| `research_eligible` | false | false |

**Regulär offen?** Ja — `now < normal_end_ts`; Extensions monoton; Writer alive.

Details: `live_lifecycle_state.json`

---

## Phase B — Code (Kurzfassung)

Siehe `watcher_code_path.md`. Kern:

```python
# watcher.py — FIGHT_ACTIVE never returns action="trigger"
if st.lifecycle in {CAPTURING, FIGHT_ACTIVE, POST_CAPTURE}:
    return WatchDecision(action="extend", ...)

# manager.py — no second capture
if sym in self._writers:
    self._handle_open_event_tick(sym, decision, now)
    return
```

Neue Profile während Capture: `set_edges` → `pending_profile_update` → Marker `PROFILE_UPDATE_DURING_CAPTURE`. **Frozen edges** am Trigger bleiben für Zoneneinteilung maßgeblich.

---

## Phase C — Spätere BTC-Profilfenster

Quelle: ClickHouse `ClickHouseCompletedProfileProvider` + `load_public_trade_records` (read-only).

| Fenster (UTC) | VAH | VAL | POC | Regelkonforme Kandidaten* | Gespeichert | Stattdessen beobachtet |
|---------------|-----|-----|-----|---------------------------|-------------|------------------------|
| 08:00–08:30 | 80790 | 80460 | 80695 | UPPER ~09:25 (19 bps) | **NEIN** | PROFILE_UPDATE ab 08:35, SECONDARY_EDGE |
| 08:30–09:00 | 81120 | 81010 | 81117.5 | UPPER @ 09:00 (2 bps) | **NEIN** | 7028 SECONDARY_EDGE @ 09:00±2min |
| 09:00–09:30 | 81265 | 81045 | 81087.5 | LOWER @ 09:30 (7 bps) | **NEIN** | 7059 SECONDARY_EDGE @ 09:30±2min |
| 09:30–10:00 | 81125 | 80967.5 | 81041.25 | LOWER @ 10:00 (1.6 bps) | **NEIN** | 7068 SECONDARY_EDGE @ 10:00±2min |

\*Hypothetisch: neues Profil + `REARMED`-Lifecycle + Entry ≤20 bps. Live: `FIGHT_ACTIVE` unterdrückt `GENUINE_CROSS_IN`.

CSV: `later_profile_candidates.csv`

---

## Phase D — Hypothesen

| Hypothese | Ergebnis |
|-----------|----------|
| **1 ACTIVE_CAPTURE_BLOCKS_NEW_SIGNAL** | **PRIMARY** ✓ |
| 2 BLOCKS_ONLY_SECOND_CAPTURE | Teilweise — Marker ja, GENUINE_CROSS_IN nein |
| 3 NO_NEW_SIGNAL_RULES_NOT_MET | Abgelehnt als Primärursache — Regeln wären erfüllt |
| 4 EVENT_LIFECYCLE_STUCK | Abgelehnt — Extensions + normal_end korrekt |
| 5 UNRESOLVED | Abgelehnt |

Matrix: `signal_vs_capture_state_matrix.json`

---

## Phase E — Zukünftige Semantik

Entwurf `nested_profile_edge_signal_v1`: siehe `nested_signal_contract_proposal.md`. **Nicht implementiert.**

---

## Empfohlener nächster Schritt

1. **Collector nicht neu starten** — aktive Captures bis Hard-Cap (~11:05 UTC) laufen lassen.
2. Offline `nested_profile_edge_signal_v1` implementieren + Tests.
3. Erst nach expliziter Freigabe: Collector-Restart für Resync-Checkpoint + Nested-Signal-Code.

---

## Artefakte

- `REPORT.md` (this file)
- `live_lifecycle_state.json`
- `watcher_code_path.md`
- `later_profile_candidates.csv`
- `signal_vs_capture_state_matrix.json`
- `nested_signal_contract_proposal.md`
