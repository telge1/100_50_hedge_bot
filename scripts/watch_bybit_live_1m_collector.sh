#!/usr/bin/env bash
# Watchdog: keep Bybit live 1m + public-trades collector alive AND ingesting.
# - Ensures systemd unit is active
# - Ensures API reports LIVE + fresh public trades (not just process up)
# - Auto-restarts on inactive/stale; ignores startup grace window
# - Appends structured alerts to a log
set -euo pipefail

UNIT="bybit-live-1m-collector.service"
API="http://127.0.0.1:8787/api/collector/status"
LOG_DIR="/home/telgenbuescher/projects/Signal_Generator_Ralf/signal_generator_stoch_waves/logs"
ALERT_LOG="${LOG_DIR}/bybit_live_1m_collector.watchdog.log"
STALE_LAG_S="${PT_WATCHDOG_STALE_LAG_S:-120}"
STARTUP_GRACE_S="${PT_WATCHDOG_STARTUP_GRACE_S:-180}"
STATE_FILE="${LOG_DIR}/bybit_live_1m_collector.watchdog.state"

mkdir -p "$LOG_DIR"
ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { echo "$(ts) $*" | tee -a "$ALERT_LOG" >/dev/null; echo "$(ts) $*"; }

active="$(systemctl --user is-active "$UNIT" 2>/dev/null || true)"
if [[ "$active" != "active" ]]; then
  log "ALERT unit_inactive state=${active} → systemctl start ${UNIT}"
  systemctl --user start "$UNIT" || true
  sleep 3
  active="$(systemctl --user is-active "$UNIT" 2>/dev/null || true)"
  log "INFO after_start state=${active}"
  exit 0
fi

# Seconds since ActiveEnterTimestamp (monotonic-ish via systemd property)
age_s="$(systemctl --user show "$UNIT" -p ActiveEnterTimestampMonotonic --value 2>/dev/null || echo 0)"
now_mono="$(cut -d' ' -f1 /proc/uptime 2>/dev/null || echo 0)"
# ActiveEnterTimestampMonotonic is usec since boot; /proc/uptime is seconds.
# Fallback: use ActiveEnterTimestamp wall clock if mono parse fails.
unit_age_s=999999
if [[ -n "$age_s" && "$age_s" =~ ^[0-9]+$ && -n "$now_mono" ]]; then
  # age_s is microseconds; now_mono seconds with decimal
  now_us="$(python3 -c "print(int(float('$now_mono')*1_000_000))")"
  unit_age_s="$(python3 -c "print(max(0, int(($now_us - int('$age_s'))/1_000_000)))")"
fi

if (( unit_age_s < STARTUP_GRACE_S )); then
  log "INFO startup_grace age_s=${unit_age_s} grace_s=${STARTUP_GRACE_S} skip_ingest_check"
  exit 0
fi

python3 - "$API" "$STALE_LAG_S" "$STATE_FILE" "$ALERT_LOG" <<'PY' || true
import json, sys, time, urllib.request
from pathlib import Path

api, stale_s, state_path, alert_log = sys.argv[1], float(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4])

def utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def alert(msg: str) -> None:
    line = f"{utc()} {msg}"
    print(line)
    with alert_log.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")

try:
    with urllib.request.urlopen(api, timeout=5) as resp:
        data = json.loads(resp.read().decode())
except Exception as exc:
    alert(f"ALERT api_unreachable err={exc} → request restart")
    sys.exit(10)

state = str(data.get("collector_state") or data.get("state") or "").upper()
ws = bool(data.get("websocket_connected"))
pt = data.get("public_trade_metrics") or {}
lag = pt.get("lag_seconds")
drops = int(pt.get("dropped_events") or 0)
inserted = pt.get("rows_inserted")
writer_fatal = bool(pt.get("writer_fatal", False))
writer_alive = pt.get("writer_alive")
writer_ok = (writer_alive is not False) and (not writer_fatal)

prev = {}
if state_path.is_file():
    try:
        prev = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        prev = {}

payload = {
    "checked_at": utc(),
    "collector_state": state,
    "websocket_connected": ws,
    "lag_seconds": lag,
    "dropped_events": drops,
    "rows_inserted": inserted,
    "writer_ok": writer_ok,
}
state_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

need_restart = False
reasons = []
if state not in {"LIVE", "RECOVERING"}:
    need_restart = True
    reasons.append(f"state={state}")
if state == "LIVE" and not ws:
    need_restart = True
    reasons.append("ws_disconnected")
if lag is not None and float(lag) > stale_s:
    need_restart = True
    reasons.append(f"lag_s={lag}>{stale_s}")
if writer_fatal:
    need_restart = True
    reasons.append("writer_fatal")

prev_drops = prev.get("dropped_events")
if prev_drops is not None and drops - int(prev_drops) >= 50000:
    alert(f"WARN drop_jump prev={prev_drops} now={drops}")

# RECOVERING after grace is still unhealthy if stuck
if state == "RECOVERING":
    need_restart = True
    reasons.append("stuck_recovering")

if need_restart:
    alert("ALERT ingest_unhealthy reasons=" + ",".join(reasons) + " → restart unit")
    sys.exit(10)

alert(
    f"OK state={state} ws={ws} lag_s={lag} drops={drops} inserted={inserted}"
)
sys.exit(0)
PY
rc=$?
if [[ "$rc" -eq 10 ]]; then
  log "ACTION systemctl restart ${UNIT}"
  systemctl --user restart "$UNIT" || true
  sleep 5
  log "INFO after_restart $(systemctl --user is-active "$UNIT" 2>/dev/null || true)"
fi
exit 0
