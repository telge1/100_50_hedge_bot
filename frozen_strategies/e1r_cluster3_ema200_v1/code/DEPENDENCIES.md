# E1R V1 frozen code — direct Python dependency closure

Copied from `results/pool_scan/context_study_v1/` per `docs/E1R_FREEZE_V1.md`.

## Entry points

- `analyze_crosscoin_e1_onebar_reclaim_shadow_v1.py` — canonical E1R simulation + `signal_ignore`
- `analyze_crosscoin_e1r_final_pnl_comparison_v1.py` — Baseline vs E1R PnL (imports `analyze_coin` from onebar script)

## Required helpers (in this `code/` folder)

| File | Role |
|------|------|
| `analyze_xrp_ema200_cluster3_persistent_shadow_v1.py` | EMA200 crosses, `ladder_at_index`, `rising_3`, outcomes |
| `analyze_xrp_bull_regime_exit_shadow_v1.py` | `episode_start_payload`, `close_episode` |
| `analyze_xrp_ema200_cluster_transition_shadow_v1.py` | `structural_lower_ladder`, `detect_crosses`, cluster flags |
| `analyze_xrp_pool_ladder_forensics_v1.py` | `price_clusters` |
| `analyze_4h_lower_short_block_v1.py` | H4 horizon constants / snapshots |
| `find_short_entry_15m_v1.py` | 15m bars, scanner entry helpers (runtime data paths) |

## Not copied (explicitly out of V1 scope)

- GAP3, D4, entry-distance / momentum / pool-age filter scripts
- `context_study_v2/*`, OOS runner scripts (OOS outputs only under `reports/oos_validation/`)
- `analyze_floor_guard_signal_list_v1.py` — signals shipped as `data_reference/floor_guard_signal_list_v1.json`

## External runtime (not in this folder)

Reproduction on a dev machine still requires repo paths documented in `docs/E1R_FREEZE_V1.md` (`lld_research_kernel`, `pool_pattern.market`, candle stores).
