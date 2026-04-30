#!/bin/bash

set -u

PROJECT_ROOT="/home/telgenbuescher/projects/spread_recovery_hedge"

echo "Stopping fixed cycle bot via hard-reset..."
cd "${PROJECT_ROOT}" || exit 1

./bot_control.sh hard-reset
RESET_CODE=$?
echo "hard-reset exit code: ${RESET_CODE}"
