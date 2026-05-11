#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: ${BASH_SOURCE[0]} long_bot_<number> [--start] [--force] [--with-wrappers]" >&2
  exit 1
}

if [[ $# -lt 1 ]]; then
  usage
fi

BOT_NAME=""
START=false
FORCE=false
WITH_WRAPPERS=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --start)
      START=true
      shift
      ;;
    --force)
      FORCE=true
      shift
      ;;
    --with-wrappers)
      WITH_WRAPPERS=true
      shift
      ;;
    --*)
      usage
      ;;
    *)
      if [[ -z "${BOT_NAME}" ]]; then
        BOT_NAME="$1"
      else
        usage
      fi
      shift
      ;;
  esac
done

if [[ -z "${BOT_NAME}" ]]; then
  usage
fi

if [[ ! "${BOT_NAME}" =~ ^long_bot_[0-9]+$ ]]; then
  usage
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_GROUP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PROJECT_ROOT="$(cd "${BOT_GROUP_DIR}/../.." && pwd)"
BOT_DIR="${BOT_GROUP_DIR}/${BOT_NAME}"
TEMPLATE_CONFIG="${BOT_GROUP_DIR}/long_bot_1/config/fixed_cycle_config.json"
BOT_CONFIG="${BOT_DIR}/config/fixed_cycle_config.json"
GROUP_CREDENTIALS="${BOT_GROUP_DIR}/config/config.yaml"

# Check bot dir existence
if [[ -d "${BOT_DIR}" && "${FORCE}" != true ]]; then
  echo "ERROR: ${BOT_DIR} already exists; use --force to repair" >&2
  exit 1
fi

created_dirs=()
ensure_dir() {
  local target="$1"
  if [[ -d "${target}" ]]; then
    return
  fi
  mkdir -p "${target}"
  created_dirs+=("${target}")
}

ensure_dir "${BOT_DIR}/config"
ensure_dir "${BOT_DIR}/logs"
ensure_dir "${BOT_DIR}/pids"
ensure_dir "${BOT_DIR}/snapshots"
ensure_dir "${BOT_DIR}/state"

# fixed_cycle_config
if [[ ! -f "${BOT_CONFIG}" ]]; then
  if [[ ! -f "${TEMPLATE_CONFIG}" ]]; then
    echo "ERROR: template config missing at ${TEMPLATE_CONFIG}" >&2
    exit 1
  fi
  cp "${TEMPLATE_CONFIG}" "${BOT_CONFIG}"
  config_status="created"
else
  config_status="existing"
fi

creds_available=false
creds_profile=""
creds_note="missing"
creds_message=""

check_credentials_with_python() {
  python3 - "${GROUP_CREDENTIALS}" "${BOT_NAME}" <<'PY'
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit(5)

path = Path(sys.argv[1])
bot = sys.argv[2]

if not path.exists():
    sys.exit(4)

cfg = yaml.safe_load(path.read_text()) or {}
profiles = cfg.get("profiles", {})
profile = profiles.get(bot)
if not profile:
    sys.exit(2)
account = profile.get("long_account")
if not account:
    sys.exit(2)
account_cfg = cfg.get(account, {}) or {}
api = str(account_cfg.get("api_key") or "").strip()
sec = str(account_cfg.get("secret_key") or "").strip()
if api and sec:
    print(account)
    sys.exit(0)
print(account or "unknown")
sys.exit(3)
PY
}

CREDS_OUT="$(mktemp)"
CREDS_ERR="$(mktemp)"
trap 'rm -f "${CREDS_OUT}" "${CREDS_ERR}"' EXIT

if command -v python3 >/dev/null 2>&1; then
  if check_credentials_with_python >"${CREDS_OUT}" 2>"${CREDS_ERR}"; then
    creds_profile="$(head -n1 "${CREDS_OUT}")"
    creds_available=true
    creds_note="found"
  else
    status=$?
    if [[ ${status} -eq 2 ]]; then
      creds_message="missing profile"
    elif [[ ${status} -eq 3 ]]; then
      creds_profile="$(head -n1 "${CREDS_OUT}")"
      creds_message="missing keys for ${creds_profile}"
    elif [[ ${status} -eq 4 ]]; then
      creds_message="missing group credentials ${GROUP_CREDENTIALS}"
    elif [[ ${status} -eq 5 ]]; then
      creds_message="PyYAML unavailable in Python3"
    else
      creds_message="loader error: $(cat "${CREDS_ERR}")"
    fi
  fi
else
  creds_message="python3 not available"
fi

if [[ "${creds_available}" != true ]]; then
  echo "WARNING: ${creds_message}"
  echo "Example profile entry:"
  echo "profiles:"
  echo "  ${BOT_NAME}:"
  echo "    long_account: Long_${BOT_NAME#*_}"
  echo ""
  echo "Long_${BOT_NAME#*_}:"
  echo "  api_key: \"YOUR_API_KEY\""
  echo "  secret_key: \"YOUR_SECRET_KEY\""
  if [[ "${START}" == true ]]; then
    echo "ABORT: --start requested but credentials invalid"
    exit 1
  fi
else
  echo "Detected credential profile: ${creds_profile}"
fi

create_wrapper() {
  local name="$1"
  local target="$2"
  local wrapper_dir="${BOT_DIR}/scripts"
  if [[ ! -d "${wrapper_dir}" ]]; then
    mkdir -p "${wrapper_dir}"
  fi
  cat <<EOF > "${wrapper_dir}/${name}.sh"
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="\$(cd "\$(dirname "\${BASH_SOURCE[0]}")" && pwd)"
BOT_DIR="\$(cd "\${SCRIPT_DIR}/.." && pwd)"
BOT_GROUP_DIR="\$(cd "\${BOT_DIR}/.." && pwd)"
exec "\${BOT_GROUP_DIR}/shared_scripts/${target}" "${BOT_NAME}"
EOF
  chmod +x "${wrapper_dir}/${name}.sh"
}

if [[ "${WITH_WRAPPERS}" == true ]]; then
  create_wrapper "start" "start_long_bot.sh"
  create_wrapper "stop" "stop_bot.sh"
  create_wrapper "stop_with_cleanup" "stop_with_cleanup.sh"
  create_wrapper "cancel_open_orders" "cancel_open_orders.sh"
  create_wrapper "hard_reset" "hard_reset_bot.sh"
  create_wrapper "clean_logs" "clean_bot_logs.sh"
  wrappers_created="yes"
else
  wrappers_created="no"
fi

summary_dirs="${created_dirs[*]:-none}"

cat <<EOF
BOT_NAME=${BOT_NAME}
BOT_DIR=${BOT_DIR}
Directories created: ${summary_dirs}
fixed_cycle_config.json: ${config_status}
Credentials: ${creds_note}${creds_profile:+ (${creds_profile})}
Wrappers created: ${wrappers_created}
EOF

if [[ "${START}" == true ]]; then
  if [[ "${creds_available}" != true ]]; then
    echo "Skipping start because credentials invalid" >&2
    exit 1
  fi
  echo "Starting bot via ${SCRIPT_DIR}/start_long_bot.sh ${BOT_NAME}"
  "${SCRIPT_DIR}/start_long_bot.sh" "${BOT_NAME}"
else
  echo "To start later: ${SCRIPT_DIR}/start_long_bot.sh ${BOT_NAME}"
fi
