# Phase-5S batch prefill environment flags

Read-only gates for PD1 window member prefill. **Default OFF** when unset — legacy per-close path unchanged.

| Variable | Effect when `1` / `true` / `yes` |
|----------|----------------------------------|
| `HISTORICAL_LMV2_BATCH_READER` | Enables Phase-5S batch prefill (`prefill_member_cache_phase5s`) instead of per-close loads. |
| `PHASE5S_BATCH_PREFILL_DIRECT_LOOKUP` | Uses `peek_publication` / `peek_catalog` during prefill (skips deepcopy). |
| `PHASE5S_COMPACT_BATCH_LOADER` | MC4K-style compact SQL batch loader. |
| `PHASE5S_MEMBER_PREFILL_REUSE_ENABLED` | Reuses member rows across windows (`phase5s_member_prefill_reuse_v1`). |
| `PHASE5S_MEMBER_PREFILL_LOW_COPY_ENABLED` | Low-copy path inside reuse module. |

Local dev helper: `dashboard/start_tf1d_local.sh` exports `HISTORICAL_LMV2_BATCH_READER=1` only. Production systemd is unchanged unless flags are added explicitly.