#!/bin/bash
# Wrapper um 203/EXEC zu vermeiden – systemd führt /bin/bash aus, nicht .venv/python direkt

set -e
PROJECT_ROOT="/home/telgenbuescher/projects/burn_reentry_simple"

# Log-Verzeichnis (systemd schreibt dorthin)
mkdir -p "$PROJECT_ROOT/data/logs"

cd "$PROJECT_ROOT/dashboard"

# Python aus venv – Fallback auf python3 wenn venv kaputt
if [ -x "$PROJECT_ROOT/.venv/bin/python" ]; then
    PYTHON="$PROJECT_ROOT/.venv/bin/python"
elif [ -x "$PROJECT_ROOT/.venv/bin/python3" ]; then
    PYTHON="$PROJECT_ROOT/.venv/bin/python3"
else
    echo "ERROR: Kein Python in $PROJECT_ROOT/.venv/bin/ gefunden." >&2
    echo "Venv neu anlegen: cd $PROJECT_ROOT && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r dashboard/requirements.txt" >&2
    exit 1
fi

exec "$PYTHON" app.py
