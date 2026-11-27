# Dashboard rollback (P2F)

```bash
cp research/mc_alert_p2f/rollback_20261127/price_alert_monitor.py \
  /home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard/price_alert_monitor.py
cp research/mc_alert_p2f/rollback_20261127/app.py \
  /home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard/app.py
systemctl --user restart dashboard.service
```

Verify OpenAPI no longer lists `/api/price-alert/monitor-status` if reverted to pre-P2F `app.py`.