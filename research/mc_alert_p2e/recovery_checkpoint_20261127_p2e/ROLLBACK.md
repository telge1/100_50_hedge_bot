# Rollback P2E Fanout (8787 only)

1. Restore backed-up modules:
   cp research/mc_alert_p2e/recovery_checkpoint_20261127_p2e/modules/collector.py \
      /home/telgenbuescher/projects/Signal_Generator_Ralf/signal_generator_stoch_waves/src/signal_generator/bybit/live/collector.py
   cp .../modules/control_api.py .../control_api.py
   rm -f .../public_trade_event_fanout.py
2. Restart: systemctl --user restart bybit-live-1m-collector.service
3. Verify: curl http://127.0.0.1:8787/healthz && POST trade_fanout → expect 404
