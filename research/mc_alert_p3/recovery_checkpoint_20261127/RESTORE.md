# MC-ALERT-P3 Restore (read with MC_ALERT_P3_FINAL_RECOVERY_CHECKPOINT.md)

## Dashboard (spread_recovery_hedge_short_dev)
git checkout stable-mc-alert-p2h-20261127
# or: git checkout <dashboard-sha-from-report>
# systemctl --user restart dashboard.service  # only when explicitly restoring

## Collector (signal_generator_stoch_waves)
git checkout stable-collector-trade-fanout-p2e-20261127
# systemctl --user restart bybit-live-1m-collector.service  # only when explicitly restoring

## Runtime NOT in git
- data/state/price_alert.json (active alerts)
- data/state/price_alert_fired.jsonl
- Fanout subscriber cursors in collector memory (re-subscribe after collector restart)

## Prior checkpoints (do not delete)
- stable-mc-pool-p9-20261126
- stable-mc-baseline-pre-pool-p1
- research/mc_alert_p2e/recovery_checkpoint_20261127_p2e/
