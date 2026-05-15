import re
from pathlib import Path
from typing import Any


_BOT_NAME_RE = re.compile(r"^long_bot_(\d+)$")
_PROFILE_RE = re.compile(r"^bot_(\d+)$")


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _long_bots_root() -> Path:
    return _project_root() / "live_bots" / "100_50_hedge_bot"


def _shared_scripts_root() -> Path:
    return _long_bots_root() / "shared_scripts"


def is_bot_profile(profile: str | None) -> bool:
    return bool(_PROFILE_RE.match(str(profile or "").strip().lower()))


def profile_to_long_bot_name(profile: str | None) -> str | None:
    match = _PROFILE_RE.match(str(profile or "").strip().lower())
    if not match:
        return None
    return f"long_bot_{int(match.group(1))}"


def profile_to_account_name(profile: str | None) -> str | None:
    match = _PROFILE_RE.match(str(profile or "").strip().lower())
    if not match:
        return None
    return f"Long_bot_{int(match.group(1))}"


def _bot_entry_from_dir(bot_dir: Path, bot_number: int) -> dict[str, Any]:
    shared_scripts_root = _shared_scripts_root()
    return {
        "bot_name": f"long_bot_{bot_number}",
        "profile": f"bot_{bot_number}",
        "account_name": f"Long_bot_{bot_number}",
        "display_name": f"Bot {bot_number}",
        "bot_number": bot_number,
        "bot_dir": str(bot_dir),
        "state_file": str(bot_dir / "state" / "fixed_cycle_state.json"),
        "wallet_snapshot_file": str(bot_dir / "snapshots" / "fixed_cycle_wallet_snapshot.json"),
        "runtime_log_file": str(bot_dir / "logs" / "fixed_cycle_hedge_runtime.log"),
        "confirmed_pnl_history_file": str(bot_dir / "logs" / "confirmed_order_pnl_history.jsonl"),
        "dashboard_closed_pnl_history_file": str(bot_dir / "logs" / "dashboard_closed_pnl_history.jsonl"),
        "audit_log_file": str(bot_dir / "logs" / "generic_hedge_runtime_audit.jsonl"),
        "status_file": str(bot_dir / "run" / "status.json"),
        "config_file": str(bot_dir / "config" / "fixed_cycle_config.json"),
        "central_start_script": str(shared_scripts_root / "start_long_bot.sh"),
        "central_stop_script": str(shared_scripts_root / "stop_bot.sh"),
        "central_restart_script": None,
        "central_stop_with_cleanup_script": str(shared_scripts_root / "stop_with_cleanup.sh"),
    }


def get_available_long_bots() -> list[dict[str, Any]]:
    root = _long_bots_root()
    if not root.exists():
        return []
    entries: list[dict[str, Any]] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        match = _BOT_NAME_RE.match(child.name)
        if not match:
            continue
        config_file = child / "config" / "fixed_cycle_config.json"
        if not config_file.exists():
            continue
        entries.append(_bot_entry_from_dir(child, int(match.group(1))))
    entries.sort(key=lambda item: item["bot_number"])
    return entries


def get_long_bot_by_profile(profile: str | None) -> dict[str, Any] | None:
    normalized = str(profile or "").strip().lower()
    for bot in get_available_long_bots():
        if bot["profile"] == normalized:
            return bot
    return None


def normalize_profile(profile: str | None, *, fallback_to_main: bool = True) -> str | None:
    normalized = str(profile or "").strip().lower()
    if normalized == "main":
        return "main"
    if is_bot_profile(normalized) and get_long_bot_by_profile(normalized):
        return normalized
    return "main" if fallback_to_main else None
