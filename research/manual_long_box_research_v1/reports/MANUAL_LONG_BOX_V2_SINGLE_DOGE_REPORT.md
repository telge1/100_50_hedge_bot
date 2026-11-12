# Manual Long Box V2 — Single DOGE

- FSM: `watcher_fsm.replay_watcher_from_config`
- Config: `watcher_integration_v3ao.v3ao_context.DEFAULT_WATCHER_CONFIG`
- cfg: `{"schema_version": "touch_watcher/v1", "touch_tolerance_pct": 0.03, "confirmation_mode": "A2", "confirmation_min_gain_pct": 0.2, "watch_window_bars": 12, "reclaim_confirmation_bars": 2, "require_approach_from_above": true, "approach_epsilon_pct": 0.05, "sl_pct": 1.2}`
- terminal: `entry_filled`
- trades: 1
- trade_status: CLOSED_SL

See `WATCHER_PARITY_V2_REPORT.md` for parity tests.