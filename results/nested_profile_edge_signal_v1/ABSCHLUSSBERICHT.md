# Abschlussbericht — nested_profile_edge_signal_v1

**Datum:** 2026-09-04T12:55 UTC+3  
**Verdict:** `NESTED_PROFILE_EDGE_SIGNAL_READY_RESTART_REQUIRED`

---

## 1. Verdict

Offline-Implementierung und Tests vollständig. Live-Aktivierung erfordert kontrollierten Collector-Restart (zusammen mit Resync-Checkpoint-Fix). **Kein Restart in dieser Session.**

---

## 2. Root Cause

Während `FIGHT_ACTIVE` liefert `EdgeWatcher.evaluate()` ausschließlich `action=extend`. `_start_or_merge_event` blockiert via `_writers[sym]` jede zweite Capture. Neue Profilkontakte wurden als level-getriggerte `SECONDARY_EDGE_TRIGGER`-Marker persistiert (~7000× pro Cutoff) ohne kanonisches Signal oder Dedup.

---

## 3. Geänderte Dateien und Funktionen

| Komponente | Datei |
|------------|-------|
| Signal-Registry (neu) | `nested_profile_signal.py` |
| Manager-Integration | `manager.py`: `_register_nested_profile_from_pending`, `_evaluate_nested_profile_signals`, `_emit_nested_signal`, `_maybe_extend_for_nested_signal` |
| Capture-Plan | `capture_plan.py` |
| Config | `config.py` |
| Tests | `test_nested_profile_edge_signal_v1.py`, `test_full_ob_edge_capture_timing_v1.py` |

Resync-Checkpoint (`continuity_contract.py`, `_write_resync_checkpoint`) **unverändert**.

---

## 4. Trennung Signal-/Capture-Lifecycle

- **Capture:** IDLE→ARMED→FIGHT_ACTIVE — steuert Writer, Prebuffer, Extension, Finalize
- **Signal:** PROFILE_OBSERVING→…→PROFILE_CROSS_IN — läuft parallel, unabhängig von Parent-Lifecycle
- `FIGHT_ACTIVE` blockiert nur Parent-`GENUINE_CROSS_IN`, nicht Nested-Signale

---

## 5. Signal- und Dedup-Key

```text
dedup_key = symbol|profile_id|edge_kind|arm_cycle_id
profile_id = SHA256(symbol, basis, window, start, end, version, fallback, vah, val, poc)
```

---

## 6. Verhalten während FIGHT_ACTIVE

1. Neues Profil → `PROFILE_UPDATE_DURING_CAPTURE` + Registry-Eintrag  
2. Arm/Cross auf per-Edge-Tracks (VAH/VAL getrennt)  
3. Kanonisches `NESTED_PROFILE_EDGE_SIGNAL` + JSONL-Ledger  
4. Optional: eine Extension pro Signal  
5. `SECONDARY_EDGE_TRIGGER` nur noch gezählt, nicht persistiert

---

## 7. Verhalten bei Rearm

Preis verlässt Entry-Zone (≥75 bps) → `PROFILE_REARMED` → neuer Arm-Zyklus (`arm_cycle_id++`) → erlaubt genau ein weiteres Signal.

---

## 8. Verhalten bei Profilwechsel

Bis zu 8 parallele Profile pro Symbol; älteste werden bei Limit deterministisch expired. Parent-Profil wird bei Registration übersprungen.

---

## 9. Parent-/Epoch-Verknüpfung

Jedes Nested-Signal: `parent_fight_event_id`, `continuity_epoch_id`, `parent_segment_index`. Bei Gap: `signal_capture_continuous=false`, `signal_research_eligible=false`.

---

## 10. Full-OB-Duplizierung

**Nein.** Ein Writer, eine Capture-Datei pro Symbol. Nested = Marker + Ledger im Parent-Event-Root.

---

## 11. Historische vier BTC-Kandidaten

| Profil | Audit Edge | Replay Edge | Signale |
|--------|------------|-------------|---------|
| 08:00–08:30 | UPPER ~09:25 | UPPER ✓ | 1 |
| 08:30–09:00 | UPPER ~09:00 | UPPER ✓ | 1 |
| 09:00–09:30 | LOWER ~09:30 | LOWER ✓ | 1 |
| 09:30–10:00 | LOWER ~10:00 | LOWER ✓ | 1 |

Offline-Replay nutzt synthetische Arm/Cross-Minutenreihen; `signal_ts` ist kausaler Cross-Tick, nicht exakte Audit-Minute. Alle vier erfüllen Regeln kausal — kein Signal erzwungen.

CSV: `historical_btc_four_candidate_replay.csv`

---

## 12. Secondary-Marker vs. kanonische Signale

| Vorher | Nachher |
|--------|---------|
| ~7000× `SECONDARY_EDGE_TRIGGER`/Cutoff | 0 persistente Secondary-Marker (nested enabled) |
| Kein GENUINE_CROSS_IN während Capture | 1× `NESTED_PROFILE_EDGE_SIGNAL` pro Arm-Zyklus |
| Kein Signal-Ledger | `nested_profile_signals.jsonl` |

---

## 13. Resync-Checkpoint-Regression

9/9 Tests PASS. Checkpoint-Fix intakt. Nested-Marker werden in derselben Parent-Queue nach Checkpoint geschrieben.

---

## 14. Tests

**73 passed, 0 failed** — alle FR-Suites inkl. Resync, Writer, Timing, Sync, Socket-Lock.

---

## 15. Collector und OI unverändert

| Prozess | PID | Status |
|---------|-----|--------|
| Collector (raw-archive) | **1565672** | läuft, nicht angefasst |
| OI Collector | **147111** | läuft, nicht angefasst |

---

## 16. Live-Risiko

**Niedrig solange kein Restart:** Code liegt offline im Repo, Live-Prozess nutzt alten Bytecode. Nach Restart: Nested-Signale aktiv, Secondary-Flut stoppt, keine zweite Capture — aber Verhalten ändert sich produktiv → kontrollierter Restart empfohlen.

---

## 17. Notwendiger nächster Schritt

Explizite Freigabe für **genau einen** kontrollierten Collector-Restart, der beide ausstehenden Fixes aktiviert:

```text
FULL_OB_RESYNC_CHECKPOINT_READY_RESTART_REQUIRED
NESTED_PROFILE_EDGE_SIGNAL_READY_RESTART_REQUIRED
```

**Bis dahin: keinen Restart durchführen.**

---

## Artefakte

```text
results/nested_profile_edge_signal_v1/
├── PHASE0_AUDIT.md
├── CONTRACT.md
├── IMPLEMENTATION_REPORT.md
├── historical_btc_four_candidate_replay.csv
├── deduplication_audit.json
├── parent_capture_linkage.json
├── resync_epoch_signal_test.json
├── performance_report.json
├── TEST_REPORT.md
└── ABSCHLUSSBERICHT.md
```
