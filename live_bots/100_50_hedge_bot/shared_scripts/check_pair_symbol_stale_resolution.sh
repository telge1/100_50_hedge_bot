#!/usr/bin/env bash
# Documented checks for stale pair-state resolution + best_coin fallback.
# Usage (from repo root):
#   bash live_bots/100_50_hedge_bot/shared_scripts/check_pair_symbol_stale_resolution.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
RESOLVER="${ROOT}/live_bots/100_50_hedge_bot/shared_scripts/pair_symbol_resolve.py"
BEST_COIN="${ROOT}/logs/best_coin.json"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

LONG_PID="${TMP}/long.pid"
SHORT_PID="${TMP}/short.pid"
: > "${LONG_PID}"
: > "${SHORT_PID}"

best_coin_symbol() {
  python3 - <<'PY' "${BEST_COIN}"
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists():
    print("")
    raise SystemExit(0)
data = json.loads(path.read_text(encoding="utf-8") or "{}")
print(str(data.get("symbol") or "").upper())
PY
}

echo "== Check 1: stale JTOUSDT + both stopped => empty pair symbol (best_coin fallback) =="
PAIR_STATE="${TMP}/pair_symbol_bot_1.json"
cat > "${PAIR_STATE}" <<'EOF'
{"symbol":"JTOUSDT","long_running":false,"short_running":false}
EOF
RESOLVED="$(python3 "${RESOLVER}" "${PAIR_STATE}" "${LONG_PID}" "${SHORT_PID}" 2>/dev/null || true)"
if [[ -n "${RESOLVED}" ]]; then
  echo "FAIL: expected empty pair symbol, got ${RESOLVED}"
  exit 1
fi
if [[ -f "${PAIR_STATE}" ]]; then
  echo "FAIL: stale pair state file was not archived/removed"
  exit 1
fi
BC="$(best_coin_symbol)"
echo "OK: pair_symbol='' best_coin='${BC}'"

echo "== Check 2: JTOUSDT + long_running=true => keep JTOUSDT =="
cat > "${PAIR_STATE}" <<'EOF'
{"symbol":"JTOUSDT","long_running":true,"short_running":false}
EOF
RESOLVED="$(python3 "${RESOLVER}" "${PAIR_STATE}" "${LONG_PID}" "${SHORT_PID}" 2>/dev/null)"
if [[ "${RESOLVED}" != "JTOUSDT" ]]; then
  echo "FAIL: expected JTOUSDT, got '${RESOLVED}'"
  exit 1
fi
echo "OK: pair_symbol='JTOUSDT'"

echo "== Check 3: no pair state => empty pair symbol (best_coin fallback) =="
rm -f "${PAIR_STATE}"
RESOLVED="$(python3 "${RESOLVER}" "${PAIR_STATE}" "${LONG_PID}" "${SHORT_PID}" 2>/dev/null || true)"
if [[ -n "${RESOLVED}" ]]; then
  echo "FAIL: expected empty pair symbol, got ${RESOLVED}"
  exit 1
fi
BC="$(best_coin_symbol)"
echo "OK: pair_symbol='' best_coin='${BC}'"

echo "All pair-symbol stale resolution checks passed."
