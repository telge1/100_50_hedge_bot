# EMA Dual Cross Multi-Source v1 — Requirement Matrix (Recovery Validation)

Policy: `EMA_MULTI_SOURCE_GATE_V1` · Strategy: `ema_dual_cross_multisource_v1`

Status codes: **PASS** (automated test) · **VERIFIED_CODE_ONLY** (code inspection only) · **NOT_OBSERVED_REAL** (real data audit) · **BLOCKED_BROWSER_QA**

| Requirement | Test / Evidence | Status |
|-------------|-----------------|--------|
| Bull Sync-Cross | `test_bull_synchronous_cross_detected` | PASS |
| Bear Sync-Cross | `test_bear_mirror` | PASS |
| Bull EMA9-first staggered | `test_bull_ema9_first_staggered_reject` | PASS |
| Bear EMA9-first staggered | `test_bear_ema9_first_staggered_reject` | PASS |
| Bull EMA20-first staggered | `test_bull_ema20_first_staggered_reject` | PASS |
| Bear EMA20-first staggered | `test_bear_ema20_first_staggered_reject` | PASS |
| EMA9-only | `test_bull_ema9_only_reject` | PASS |
| EMA20-only | `test_bull_ema20_only_reject` | PASS |
| Bull expanded band | `test_bull_expanded_band_reject` | PASS |
| Bear expanded band | `test_bear_expanded_band_reject` | PASS |
| Bull flat noise | `test_bull_flat_noise_reject` | PASS |
| Bear flat noise | `test_bear_flat_noise_reject` | PASS |
| gültiger nicht-flacher Cross | `test_bull_valid_non_flat_sync_control` | PASS |
| schwacher Rebound | `test_weak_rebound_no_candidate`, `test_weak_rebound_no_turn_together` | PASS |
| gültiger Bull-Rebound | `test_valid_bull_rebound_control` | PASS |
| gültiger Bear-Rebound | `test_valid_bear_rebound_control` | PASS |
| max_total_band_atr Rebound | `test_max_total_band_atr_used_for_rebound` | PASS |
| kein Lookahead | `test_weak_rebound_no_candidate` (single-bar `_detect_rebound`) | PASS |
| Entry nächstes Open | `test_entry_next_open_only_on_allow` | PASS |
| BLOCK keine aktive Episode | `test_block_does_not_open_active_entry_episode` | PASS |
| INCONCLUSIVE keine aktive Episode | `test_inconclusive_does_not_block_later_sync` | PASS |
| Rebound blockiert keinen Sync | `test_rebound_block_does_not_block_sync` | PASS |
| Sync-Priorität | `test_sync_priority_over_rebound_same_bar` | PASS |
| struktureller Reset | `test_update_compression_wired` | PASS |
| fehlendes OB → INCONCLUSIVE | `test_missing_ob_inconclusive_coverage` | PASS |
| stale OB → INCONCLUSIVE | `test_stale_ob_inconclusive` | PASS |
| Cluster allein → kein Candidate | `test_cluster_alone_no_candidate` | PASS |
| hypothetisches Outcome nächstes Open | `test_hypothetical_entry_all_verdicts` | PASS |
| Long-/Short-Symmetrie | stagger/flat/expanded bear tests | PASS |
| UTC | pipeline meta + smoke exports (`timezone: UTC`) | PASS |
| Warmup | `features.warmup_coverage` in smoke export | PASS |
| Idempotenz | `test_idempotent_run` | PASS |
| Dashboardmarker | `test_block_marker_no_ent_without_allow` | PASS |
| Detailpanel | `test_ui_edc_panel_fields` | VERIFIED_CODE_ONLY |
| Navigation | `test_api_supports_edc_strategy` + `/ema-dual-cross/nav` in api.py | VERIFIED_CODE_ONLY |
| Zoom | `test_edc_zoom_wiring` | VERIFIED_CODE_ONLY |
| Legacy Cluster Sweep unverändert | `test_legacy_cluster_sweep_unchanged`, `test_cluster_sweep_backtester` (20 tests) | PASS |
| Stochastic Fade unverändert | `test_stoch_unchanged` | PASS |
| Realer Stagger Aug 1–15 | XRPUSDT audit: 17× `REJECTED_STAGGERED_CROSS` (e.g. 2026-08-03 02:00 BEARISH lag=3) | PASS |
| Browser E2E Visual QA | dash.immotel.de:8080 | BLOCKED_BROWSER_QA (401 auth) |
| Real Sync Outcomes 1h/4h | smoke export `recovery_validation/sync_outcomes_and_stagger.json` | PASS |

## Smoke runs

| Smoke | Path | Status |
|-------|------|--------|
| A sync-only full OB | `results/ema_dual_cross_multisource/recovery_validation/` | PASS |
| B sync-only no OB | same | PASS |
| C rebound-only | prior session `recovery_smokes/smoke_c_rebound_only` | PASS |

## Test commands (2026-08-22)

```bash
cd orderbook_analyse && PYTHONPATH=src pytest -q tests/test_ema_dual_cross_multisource.py tests/test_ema_dual_cross_antifake.py
# 44 passed

PYTHONPATH=src pytest -q tests/test_cluster_sweep_research.py tests/test_outcome_analysis_1h_4h.py
# 36 passed

cd spread_recovery_hedge_short_dev/dashboard && python -m pytest -q tests/test_ema_dual_cross_backtester.py tests/test_cluster_sweep_backtester.py tests/test_cluster_sweep_outcomes.py
# 20 passed
```
