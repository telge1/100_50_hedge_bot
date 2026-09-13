#!/usr/bin/env bash
# Live status: 1m candles + public trades collector (:8787)
set -euo pipefail
python3 - <<'PY'
import json
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8787/api/collector/status", timeout=5) as resp:
    d = json.load(resp)
m = d.get("public_trade_metrics") or {}
print("state", d.get("collector_state"), "ws", d.get("websocket_connected"))
print("candle_symbols", len(d.get("candle_symbols") or []))
print(
    "trades rows_inserted", m.get("rows_inserted"),
    "lag_s", m.get("lag_seconds"),
    "last", m.get("last_trade_event_ts"),
)
sample = list((d.get("last_closed_candle_by_symbol") or {}).items())[:3]
print("last_closed_sample", sample)
PY
