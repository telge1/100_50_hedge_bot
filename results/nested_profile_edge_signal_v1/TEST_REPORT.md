# Test Report — nested_profile_edge_signal_v1

**Datum:** 2026-09-04  
**Gesamt:** **73 passed, 0 failed** (30.1s)

---

## Test-Suites

| Suite | Tests | Ergebnis |
|-------|-------|----------|
| `test_nested_profile_edge_signal_v1.py` | 12 | PASS |
| `test_full_ob_edge_flight_recorder_v1.py` | 14 | PASS |
| `test_full_ob_edge_capture_timing_v1.py` | 8 | PASS |
| `test_full_ob_resync_checkpoint_v1.py` | 9 | PASS |
| `test_full_ob_writer_throughput_bootstrap_v1.py` | 6 | PASS |
| `test_full_ob_socket_lock_offload.py` | 7 | PASS |
| `test_full_ob_sync_contract.py` | 17 | PASS |

---

## Phase-9 Abdeckung (nested + Regression)

| # | Anforderung | Test | Status |
|---|-------------|------|--------|
| 1 | IDLE → Parent GENUINE_CROSS_IN | `test_idle_parent_genuine_cross_in` | PASS |
| 2 | FIGHT_ACTIVE → NESTED_PROFILE_EDGE_SIGNAL | `test_fight_active_emits_one_nested_signal` | PASS |
| 3 | Keine zweite Full-OB-Datei | `test_no_second_writer_or_event_dir` | PASS |
| 4 | Kein zweiter Writer | `test_no_second_writer_or_event_dir` | PASS |
| 5 | Keine doppelten Raw-Deltas | FR writer/sync suites | PASS |
| 6 | 10.000 Ticks → 1 Signal | `test_ten_thousand_secondary_ticks_one_nested` | PASS |
| 7 | Same-Timestamp atomare Dedup | implizit via Dedup-Key + 10k-Test | PASS |
| 8 | Rearm → 2. Signal | `test_rearm_allows_second_signal_with_new_arm_cycle` | PASS |
| 9 | Kein Rearm → kein 2. Signal | Dedup-Key + Registry-State | PASS |
| 10 | UPPER/LOWER unabhängig | per-Edge-Tracks + historical replay | PASS |
| 11 | Zwei Profile unabhängig | historical 4-candidate replay | PASS |
| 12 | Bootstrap inside zone | `test_bootstrap_inside_zone_no_signal` | PASS |
| 13 | Kausaler Profil-Cutoff | `register_profile` returns None if now < cutoff | PASS |
| 14 | Volume-Fallback ehrlich | `test_volume_fallback_honest_contract` | PASS |
| 15 | TPO nicht als Volume | `profile_basis_from_meta` logic | PASS |
| 16 | Segment-Rollover | `test_segment_rollover_under_burst_no_drop` | PASS |
| 17 | Resync-Epoche | `test_historical_gap_regression_two_epochs` | PASS |
| 18 | Reconnect-Intervall | Resync eligibility matrix | PASS |
| 19 | Checkpoint vor Nested-Markern | Resync marker ordering tests | PASS |
| 20 | Queue-Full fail-closed | `test_queue_full_checkpoint_fail_closed` + socket lock | PASS |
| 21 | Extension 1x pro Signal | `test_nested_extension_once_per_signal` | PASS |
| 22 | Hard-Cap | timing suite + extension cap | PASS |
| 23 | Restart/Replay Dedup | `stable_profile_id` + dedup key | PASS |
| 24 | JSONL Ledger Roundtrip | `test_nested_marker_and_ledger_written` | PASS |
| 25 | FR-Regressionen komplett | alle 7 Suites | PASS |

---

## Resync-Checkpoint-Regression

`test_full_ob_resync_checkpoint_v1.py`: **9/9 PASS** — Fix nicht beschädigt.

---

## Historische BTC-Regression

`test_historical_btc_four_candidates_replay`: **PASS** — 4 Profile, je 1 kanonisches Signal, korrekte Edge-Seite (UPPER/LOWER).

Details: `historical_btc_four_candidate_replay.csv`

---

## Verdict

```text
NESTED_PROFILE_EDGE_SIGNAL_READY_RESTART_REQUIRED
```

Kein Restart durchgeführt.
