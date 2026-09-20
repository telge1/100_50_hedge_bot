# Security Contract — Full-OB Cache Bridge V1

**Generated (UTC):** `2026-09-05T15:12:26Z`

## Defaults

- `FULL_OB_CACHE_BRIDGE_ENABLED` default **false**
- No socket created when disabled
- No dump root activity when disabled

## Transport

- Unix domain socket only
- Mode **0600**
- No TCP fallback
- No dashboard endpoint
- No internet egress from bridge code

## Dump FS

- Fixed `FULL_OB_CACHE_BRIDGE_DUMP_ROOT`
- Request id allowlist `[A-Za-z0-9._-]`
- Reject `..`, `/`, symlink escape, overwrite
- Atomic temp + rename; mode 0600 files / 0700 dirs

## Protocol

- JSON via orjson only (no pickle)
- Protocol version required
- Symbol allowlist
- Max request bytes / max payload bytes
- Max concurrent freeze = 1 + rate limit
- Unknown status codes fail-closed on client

## Forbidden

- Arbitrary file read/write paths
- Shell / SQL / env dump / secrets
- Caller-controlled deserialization gadgets
- OB200 silent fallback
