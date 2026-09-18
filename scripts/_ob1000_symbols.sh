#!/usr/bin/env bash
# Resolve comma-separated OB1000 live symbols from canonical config (SoT).
# Sourced by raw-archive / materializer wrappers. No OB200.
#
# Precedence:
#   1) config file: OB1000_SYMBOLS_FILE or $root/config/ob1000_live_symbols.json
#      (canonical SoT — wins over systemd Environment= unless prefer-env is set)
#   2) OB_V3_OB1000_RAW_ARCHIVE_SYMBOLS only when:
#        - config file is missing, OR
#        - OB1000_SYMBOLS_PREFER_ENV=1
#   3) BTCUSDT,DOGEUSDT fallback
ob1000_resolve_symbols() {
  local root="${1:?root}"
  local cfg="${OB1000_SYMBOLS_FILE:-$root/config/ob1000_live_symbols.json}"
  local from_env="${OB_V3_OB1000_RAW_ARCHIVE_SYMBOLS:-}"
  local prefer_env="${OB1000_SYMBOLS_PREFER_ENV:-0}"

  if [[ -f "$cfg" && "${prefer_env}" != "1" ]]; then
    python3 - "$cfg" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path, encoding="utf-8"))
syms = data.get("symbols") if isinstance(data, dict) else data
if not isinstance(syms, list) or not syms:
    raise SystemExit(f"empty symbols in {path}")
print(",".join(str(s).strip().upper() for s in syms if str(s).strip()))
PY
    return 0
  fi
  if [[ -n "${from_env}" ]]; then
    printf '%s' "$from_env"
    return 0
  fi
  printf '%s' "BTCUSDT,DOGEUSDT"
}
