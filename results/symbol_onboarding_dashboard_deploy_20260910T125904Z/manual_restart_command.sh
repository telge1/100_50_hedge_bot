#!/bin/bash
# Rollback: stop systemd dashboard if running, then manual start (exactly one :3000 listener)
set -euo pipefail
cd /home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard
# Prefer: sudo systemctl stop dashboard.service
# Then:
nohup ../.venv/bin/python app.py >> ../logs/dashboard.nohup.log 2>&1 &
echo $! > ../logs/dashboard.pid
sleep 1
ss -ltnp | grep ':3000'
