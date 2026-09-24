"""
Helpers for loading/selecting Bybit API keys.

Supports:
- Legacy config/config.yaml with top-level `api_key` / `secret_key`
- New config/config.yaml with `master:` and `sub:` mappings:
    master: { api_key: "...", secret_key: "..." }
    sub:    { api_key: "...", secret_key: "..." }
- Environment overrides (highest priority):
    - BYBIT_API_KEY / BYBIT_SECRET_KEY
    - BYBIT_MASTER_API_KEY / BYBIT_MASTER_SECRET_KEY
    - BYBIT_SUB_API_KEY / BYBIT_SUB_SECRET_KEY
"""

from __future__ import annotations

import os
from typing import Any, Mapping


def select_bybit_keys(config: Mapping[str, Any] | None, profile: str) -> tuple[str, str]:
    """
    Select (api_key, secret_key) for the given profile ("master" or "sub").
    """
    profile_norm = (profile or "master").strip().lower()
    if profile_norm not in ("master", "sub"):
        raise ValueError(f"Invalid key profile: {profile!r} (expected 'master' or 'sub')")

    # 1) Explicit per-process override (recommended for launch scripts)
    env_api = (os.getenv("BYBIT_API_KEY") or "").strip()
    env_sec = (os.getenv("BYBIT_SECRET_KEY") or "").strip()
    if env_api and env_sec:
        return env_api, env_sec

    # 2) Profile-specific env overrides
    if profile_norm == "master":
        env_api = (os.getenv("BYBIT_MASTER_API_KEY") or "").strip()
        env_sec = (os.getenv("BYBIT_MASTER_SECRET_KEY") or "").strip()
    else:
        env_api = (os.getenv("BYBIT_SUB_API_KEY") or "").strip()
        env_sec = (os.getenv("BYBIT_SUB_SECRET_KEY") or "").strip()
    if env_api and env_sec:
        return env_api, env_sec

    cfg: Mapping[str, Any] = config or {}

    hedge_profile = (os.getenv("HEDGE_PROFILE") or "").strip().lower()
    profiles_block = cfg.get("profiles")
    if hedge_profile and isinstance(profiles_block, dict):
        prof = profiles_block.get(hedge_profile)
        if isinstance(prof, dict):
            key = "long_account" if profile_norm == "master" else "short_account"
            account_name = prof.get(key)
            if account_name:
                bucket = cfg.get(account_name) if isinstance(cfg.get(account_name), dict) else {}
                api = str((bucket or {}).get("api_key") or "").strip()
                sec = str((bucket or {}).get("secret_key") or "").strip()
                if api and sec:
                    return api, sec

    # 3) New structured config
    if isinstance(cfg.get(profile_norm), dict):
        bucket = cfg.get(profile_norm) or {}
        api = str(bucket.get("api_key") or "").strip()
        sec = str(bucket.get("secret_key") or "").strip()
        if api and sec:
            return api, sec

    # 4) Legacy flat config
    api = str(cfg.get("api_key") or "").strip()
    sec = str(cfg.get("secret_key") or "").strip()
    if api and sec:
        return api, sec

    raise RuntimeError(
        "Bybit API keys not found. Expected either:\n"
        "- ENV: BYBIT_API_KEY/BYBIT_SECRET_KEY (or BYBIT_MASTER_*/BYBIT_SUB_*), or\n"
        "- config.yaml: master/sub mappings, or\n"
        "- legacy config.yaml: api_key/secret_key"
    )

