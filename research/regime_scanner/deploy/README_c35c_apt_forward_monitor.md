# C3.5c APT forward monitor — deploy templates

Paper/research only. Do **not** enable without explicit approval.

## Manual start

```bash
cd /home/telgenbuescher/projects/spread_recovery_hedge_short_dev
PYTHONPATH=. python3 -m research.regime_scanner.pullback_entry_c3_5c_apt_forward_monitor run-once
```

Optional fixed forward boundary (first start only):

```bash
PYTHONPATH=. python3 -m research.regime_scanner.pullback_entry_c3_5c_apt_forward_monitor run-once \
  --forward-start-utc "2026-07-17T15:00:00Z"
```

## systemd (templates only — not installed)

```bash
sudo cp research/regime_scanner/deploy/c35c-apt-forward-monitor.service /etc/systemd/system/
sudo cp research/regime_scanner/deploy/c35c-apt-forward-monitor.timer /etc/systemd/system/
sudo systemctl daemon-reload
# ONLY after explicit approval:
# sudo systemctl enable --now c35c-apt-forward-monitor.timer
```

Cron alternative (every 5 minutes, flock):

```bash
*/5 * * * * flock -n /tmp/c35c-apt-fwd.lock -c 'cd /home/telgenbuescher/projects/spread_recovery_hedge_short_dev && PYTHONPATH=. python3 -m research.regime_scanner.pullback_entry_c3_5c_apt_forward_monitor run-once'
```
