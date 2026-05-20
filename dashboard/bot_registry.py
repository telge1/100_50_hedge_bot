import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

BOT_DIR_PATTERN = re.compile(r"^long_bot_(\d+)$", re.IGNORECASE)


def get_project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def get_hedge_bot_root() -> Path:
    return get_project_root() / "live_bots" / "100_50_hedge_bot"


def _normalize_key(value: str | None) -> str:
    return (value or "").strip().casefold()


def discover_long_bots() -> list[dict[str, Any]]:
    root = get_hedge_bot_root()
    if not root.exists():
        return []
    entries: list[dict[str, Any]] = []
    indexes: set[int] = set()
    for child in root.iterdir():
        if not child.is_dir():
            continue
        match = BOT_DIR_PATTERN.match(child.name)
        if not match:
            continue
        index = int(match.group(1))
        indexes.add(index)
        entries.append(
            {
                "index": index,
                "profile": f"bot_{index}",
                "bot_name": child.name,
                "label": f"Bot {index}",
                "long_account": f"Long_bot_{index}",
                "short_account": f"Short_bot_{index}",
                "bot_dir": child,
            }
        )
    entries.sort(key=lambda item: item["index"])
    if entries:
        max_index = max(item["index"] for item in entries)
        missing = [idx for idx in range(1, max_index + 1) if idx not in indexes]
        if missing:
            logger.warning("Skipped bot indexes detected; missing %s", missing)
    return entries


@lru_cache(maxsize=None)
def get_bot_profiles() -> list[dict[str, Any]]:
    return discover_long_bots()


@lru_cache(maxsize=None)
def _build_lookup() -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for entry in get_bot_profiles():
        for key in (
            entry["bot_name"],
            entry["profile"],
            entry["long_account"],
            entry["short_account"],
        ):
            normalized = _normalize_key(key)
            lookup[normalized] = entry
    return lookup


def get_dashboard_accounts() -> list[str]:
    accounts = ["main", "sub"]
    for profile in get_bot_profiles():
        accounts.append(profile["long_account"])
        accounts.append(profile["short_account"])
    return accounts


def get_live_charts_accounts() -> list[str]:
    return get_dashboard_accounts()


def get_closed_pnl_accounts() -> list[str]:
    return get_dashboard_accounts()


def resolve_account(account: str | None) -> dict[str, Any] | None:
    normalized = (account or "").strip()
    if not normalized:
        return None
    lowered = normalized.casefold()
    if lowered == "main":
        return {
            "account": "main",
            "profile": "main",
            "side": "main",
            "bot_name": None,
            "index": None,
            "bot_dir": None,
        }
    if lowered == "sub":
        return {
            "account": "sub",
            "profile": "sub",
            "side": "sub",
            "bot_name": None,
            "index": None,
            "bot_dir": None,
        }
    lookup = _build_lookup()
    entry = lookup.get(lowered)
    if not entry:
        return None
    account_name = entry["long_account"]
    side = "long"
    if lowered == _normalize_key(entry["short_account"]):
        account_name = entry["short_account"]
        side = "short"
    return {
        "account": account_name,
        "profile": entry["profile"],
        "side": side,
        "bot_name": entry["bot_name"],
        "index": entry["index"],
        "bot_dir": entry["bot_dir"],
    }


def get_bot_paths(bot_identifier: str | None) -> dict[str, Path] | None:
    if not bot_identifier:
        return None
    lookup = _build_lookup()
    entry = lookup.get(_normalize_key(bot_identifier))
    if not entry:
        return None
    bot_dir = entry["bot_dir"]
    logs_dir = bot_dir / "logs"
    return {
        "bot_dir": bot_dir,
        "logs_dir": logs_dir,
        "state_file": bot_dir / "state" / "fixed_cycle_state.json",
        "snapshot_file": bot_dir / "snapshots" / "fixed_cycle_wallet_snapshot.json",
        "runtime_log_file": logs_dir / "fixed_cycle_hedge_runtime.log",
        "dashboard_closed_pnl_history_file": logs_dir / "dashboard_closed_pnl_history.jsonl",
        "confirmed_order_pnl_history_file": logs_dir / "confirmed_order_pnl_history.jsonl",
    }


def _dump(name: str, value: Iterable[str]) -> None:
    print(f"{name}:")
    for line in value:
        print(f"  - {line}")


if __name__ == "__main__":
    print("Discovered hedge bots:")
    for profile in get_bot_profiles():
        print(f"- {profile['bot_name']} ({profile['long_account']} / {profile['short_account']})")
    _dump("Dashboard accounts", get_dashboard_accounts())
    _dump("Live charts accounts", get_live_charts_accounts())
    _dump("Closed pnl accounts", get_closed_pnl_accounts())
