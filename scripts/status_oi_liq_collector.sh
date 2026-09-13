#!/usr/bin/env bash
# Live status: OI + liquidations collector
set -euo pipefail
python3 - <<'PY'
import json
import time
from pathlib import Path

path = Path("/home/telgenbuescher/projects/orderbook_analyse/logs/oi_liquidation_collector.health.json")
d = json.loads(path.read_text(encoding="utf-8"))
now = time.time()
print("health", d.get("health_status"), "ws", d.get("websocket_alive"))
print("oi_age_s", round(now - float(d.get("last_oi_persisted_ts") or 0), 1))
print("liq_age_s", round(now - float(d.get("last_liquidation_persisted_ts") or 0), 1))
print("lag_s", d.get("persistence_lag_seconds"))
print("queue_depth", d.get("queue_depth"))
PY
