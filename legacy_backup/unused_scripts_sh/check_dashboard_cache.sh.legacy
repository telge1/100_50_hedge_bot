#!/usr/bin/env bash

set -uo pipefail

DASH_URL="https://dash.immotel.de/dashboard"
TMP_HTML="/tmp/dashboard-html.cache"

echo "Checking dashboard template freshness..."
echo "Fetching with Cache-Control: no-cache"
curl -sSf -H "Cache-Control: no-cache" "${DASH_URL}" -o "${TMP_HTML}"

if grep -q "startFixedCycleScript" "${TMP_HTML}"; then
  echo "[1/2] Header check: found entrypoint string."
else
  echo "[1/2] WARNING: expected entrypoint not found in HTML."
fi

echo "[2/2] Proxy headers:"
curl -sI -H "Cache-Control: no-cache" "${DASH_URL}" | awk '
  /^ETag:/ {print "ETag: " $0}
  /^Last-Modified:/ {print "Last-Modified: " $0}
  /^Cache-Control:/ {print "Cache-Control: " $0}
  /^Age:/ {print "Age: " $0}
  /^Warning:/ {print "Warning: " $0}
'

echo "Done. Inspect ${TMP_HTML} if you need full HTML."
