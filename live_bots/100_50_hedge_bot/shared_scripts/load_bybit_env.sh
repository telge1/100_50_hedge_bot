#!/usr/bin/env bash
set -euo pipefail

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  echo "ERROR: ${BASH_SOURCE[0]} must be sourced, not executed." >&2
  exit 1
fi

if [[ $# -ne 2 ]]; then
  echo "Usage: source ${BASH_SOURCE[0]} <bot_name> <long|short>" >&2
  return 1
fi

BOT_NAME="$1"
SIDE="$2"

if [[ "${SIDE}" != "long" && "${SIDE}" != "short" ]]; then
  echo "ERROR: side must be 'long' or 'short', got '${SIDE}'." >&2
  return 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_GROUP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG_YAML="${BOT_GROUP_DIR}/config/config.yaml"

if [[ ! -f "${CONFIG_YAML}" ]]; then
  echo "ERROR: config file not found at ${CONFIG_YAML}" >&2
  return 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not available" >&2
  return 1
fi

mapfile -t _account_data < <(
  python3 - "${CONFIG_YAML}" "${BOT_NAME}" "${SIDE}" <<'PY'
import json
import sys
from pathlib import Path

try:
    import yaml
except ImportError as exc:
    print(f"ERROR: missing PyYAML: {exc}", file=sys.stderr)
    sys.exit(1)

config_path = Path(sys.argv[1])
bot_name = sys.argv[2]
side = sys.argv[3]

if not config_path.exists():
    print(f"ERROR: config file not found at {config_path}", file=sys.stderr)
    sys.exit(1)

with config_path.open(encoding="utf-8") as fh:
    data = yaml.safe_load(fh) or {}

def find_key(mapping, candidate):
    if candidate in mapping:
        return candidate
    candidate_lower = candidate.lower()
    for key in mapping:
        if isinstance(key, str) and key.lower() == candidate_lower:
            return key
    return None

profiles = data.get("profiles") or {}
profile_key = find_key(profiles, bot_name)
if profile_key is None and "_" in bot_name:
    suffix = bot_name.split("_", 1)[1]
    profile_key = find_key(profiles, suffix)
profile = profiles.get(profile_key, {}) if profile_key else {}
account_name = profile.get("long_account") if side == "long" else profile.get("short_account")

fallback_map = {
    "long_bot_1": ["Long_bot_1", "master"],
    "long_bot_2": ["Long_bot_2", "bot_2"],
}

if not account_name:
    for candidate in fallback_map.get(bot_name, []):
        match = find_key(data, candidate)
        if match:
            account_name = match
            break

if not account_name:
    print(f"ERROR: no account mapping for bot '{bot_name}' side '{side}'", file=sys.stderr)
    sys.exit(1)

account_data = data.get(account_name)
if not isinstance(account_data, dict):
    print(f"ERROR: account '{account_name}' missing in config", file=sys.stderr)
    sys.exit(1)

api_key = str(account_data.get("api_key") or "").strip()
secret_key = str(account_data.get("secret_key") or "").strip()
if not api_key or not secret_key:
    print(f"ERROR: account '{account_name}' missing api_key/secret_key", file=sys.stderr)
    sys.exit(1)

print(account_name)
print(api_key)
print(secret_key)
PY
)

if [[ ${#_account_data[@]} -ne 3 ]]; then
  echo "ERROR: failed to load account data for ${BOT_NAME}/${SIDE}" >&2
  return 1
fi

ACCOUNT_NAME="${_account_data[0]}"
API_KEY="${_account_data[1]}"
SECRET_KEY="${_account_data[2]}"

export BYBIT_API_KEY="${API_KEY}"
export BYBIT_API_SECRET="${SECRET_KEY}"
export FIXED_CYCLE_ACCOUNT_NAME="${ACCOUNT_NAME}"
unset BYBIT_SUB_API_KEY BYBIT_SUB_SECRET_KEY

printf "bot=%s side=%s account=%s\n" "${BOT_NAME}" "${SIDE}" "${ACCOUNT_NAME}"
