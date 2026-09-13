#!/usr/bin/env bash
# Live status: OB1000 + Full-OB raw archive collector
set -euo pipefail
HEALTH="/home/telgenbuescher/projects/orderbook_analyse/logs/orderbook_v3_raw_archive_btc_doge.health.ndjson"
python3 - <<'PY'
import json
from pathlib import Path
path = Path("/home/telgenbuescher/projects/orderbook_analyse/logs/orderbook_v3_raw_archive_btc_doge.health.ndjson")
line = path.read_text(encoding="utf-8").splitlines()[-1]
d = json.loads(line)
o = d.get("ob1000_raw_archive") or d
print("state", d.get("collector_state"), "connected", d.get("connected"))
print("ob1000 written", o.get("raw_events_written"), "last", o.get("raw_last_write_at"))
print("full_ob written", d.get("full_ob_raw_archive_messages_written"), "queue", d.get("full_ob_raw_archive_queue_depth"))
print("topics", d.get("confirmed_topics"))
PY
