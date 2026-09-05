# TEST_REPORT

**UTC:** 2026-09-04T18:41:23Z

```text
pytest tests/test_oi_liquidation_spool_meta_race_v1.py \
       tests/test_oi_liquidation_resilience_v1.py \
       tests/test_oi_liquidation_collector.py -q
→ 52 passed
```

## Coverage map

| # | Requirement | Test |
|---|-------------|------|
| 1 | parallel append+ack | `test_1_parallel_append_and_ack` |
| 2 | parallel ack+health | `test_2_parallel_ack_and_health_snapshot` |
| 3 | rollover during ack | `test_3_rollover_during_ack` |
| 4 | shutdown during meta | `test_4_shutdown_during_meta_commit` |
| 5 | 100 concurrent meta | `test_5_hundred_concurrent_meta_updates` |
| 6 | temp already renamed | `test_6_temp_already_renamed` |
| 7 | crash before fsync | `test_7_crash_before_fsync` |
| 8 | crash after fsync before replace | `test_8_crash_after_fsync_before_replace` |
| 9 | crash after replace | `test_9_crash_after_replace` |
| 10 | corrupt meta.json | `test_10_*` |
| 11 | orphan meta.json.tmp | `test_11_orphan_meta_tmp` |
| 12 | ack never backwards | `test_12_ack_never_goes_backwards` |
| 13 | insert ok, ack fails first | `test_13_insert_ok_ack_fails_then_recovers` |
| 14 | replay no logical dups | `test_14_replay_without_logical_duplicates` |
| 15 | DB outage recovery | `test_15_db_outage_and_recovery` |
| 16 | writer error → fail-fast | `test_16_writer_error_reaches_fail_fast_supervisor` |
| 17 | 51-symbol load | `test_17_load_all_51_symbols` |
| 18 | existing regressions | resilience_v1 + collector suites |
