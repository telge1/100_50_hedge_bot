#!/usr/bin/env python3
"""Executor for optional wallet transfers (refill/cashout)."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from fixed_cycle_hedge_bot.order_manager import BybitOrderManager

BOT_ROOT = PROJECT_ROOT / "live_bots" / "100_50_hedge_bot"
DEFAULT_CONFIG_PATH = BOT_ROOT / "config" / "config.yaml"
LOG_PATH = BOT_ROOT / "logs" / "wallet_transfer_executor.log"
JSON_LOG_PATH = BOT_ROOT / "logs" / "wallet_transfer_executor.jsonl"
DEFAULT_COIN = "USDT"
MAIN_SECTION_CANDIDATES = ("Main_bot", "main_bot", "master", "Master", "main", "main_account")
MAIN_UID_CANDIDATES = ("uid", "main_uid", "account_uid", "member_id")
BOT_SUB_UID_CANDIDATES = ("sub_uid", "uid", "account_uid", "member_id")


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("wallet_transfer_executor")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        logger.handlers.clear()
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(console)
    return logger


def write_json_event(event: str, payload: dict[str, Any]) -> None:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **payload,
    }
    try:
        JSON_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with JSON_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Execute wallet transfers for hedge bots.")
    parser.add_argument("--bot-name", required=True)
    parser.add_argument("--direction", choices=("refill", "cashout"), required=True)
    parser.add_argument("--amount", type=float, required=True)
    parser.add_argument("--coin", default=DEFAULT_COIN)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-transfer-usdt", type=float, default=0.0)
    parser.add_argument("--min-transfer-usdt", type=float, default=1.0)
    parser.add_argument("--config-file", type=Path, default=DEFAULT_CONFIG_PATH)
    return parser.parse_args()


def get_config_section_case_insensitive(config: dict[str, Any], key: str) -> dict[str, Any]:
    if key in config:
        section = config[key]
        if isinstance(section, dict):
            return section
        return {}
    key_lower = key.lower()
    for existing_key, value in config.items():
        if str(existing_key).lower() == key_lower and isinstance(value, dict):
            return value
    return {}


def get_value_case_insensitive(section: dict[str, Any], key: str) -> Any | None:
    if key in section:
        return section[key]
    key_lower = key.lower()
    for existing_key, value in section.items():
        if str(existing_key).lower() == key_lower:
            return value
    return None


def get_config_section_key_case_insensitive(config: dict[str, Any], key: str) -> tuple[str | None, dict[str, Any]]:
    if key in config:
        section = config[key]
        if isinstance(section, dict):
            return str(key), section
        return None, {}
    key_lower = key.lower()
    for existing_key, value in config.items():
        if str(existing_key).lower() == key_lower and isinstance(value, dict):
            return str(existing_key), value
    return None, {}


def find_main_account_section(
    config: dict[str, Any],
) -> tuple[str | None, dict[str, Any] | None, str | None, str | None, list[dict[str, Any]]]:
    checked_sections: list[dict[str, Any]] = []

    def evaluate(section_key: str | None, section: dict[str, Any]) -> tuple[str | None, dict[str, Any], str | None, str | None, dict[str, Any]]:
        api_present = bool((get_value_case_insensitive(section, "api_key") or "").strip())
        secret_present = bool((get_value_case_insensitive(section, "secret_key") or "").strip())
        uid_value, uid_field = get_first_field_value(section, MAIN_UID_CANDIDATES)
        info = {
            "key": section_key or "unknown",
            "api_key": api_present,
            "secret_key": secret_present,
            "uid_field": uid_field,
        }
        checked_sections.append(info)
        if api_present and secret_present and uid_value:
            return section_key or "unknown", section, uid_value, uid_field, info
        return None, None, None, None, info

    for candidate in MAIN_SECTION_CANDIDATES:
        found_key, section = get_config_section_key_case_insensitive(config, candidate)
        if not section:
            continue
        selected_key, selected_section, uid_value, uid_field, _ = evaluate(found_key, section)
        if selected_section:
            return selected_key, selected_section, uid_value, uid_field, checked_sections

    profiles_key, profiles = get_config_section_key_case_insensitive(config, "profiles")
    if profiles:
        found_key, main_section = get_config_section_key_case_insensitive(profiles, "main")
        if main_section:
            section_label = f"{profiles_key or 'profiles'}.main"
            selected_key, selected_section, uid_value, uid_field, _ = evaluate(section_label, main_section)
            if selected_section:
                return selected_key, selected_section, uid_value, uid_field, checked_sections
    return None, None, None, None, checked_sections


def format_available_keys(config: dict[str, Any]) -> list[str]:
    return [str(key) for key, value in config.items() if isinstance(value, dict)]


def get_first_field_value(section: dict[str, Any], candidates: tuple[str, ...]) -> tuple[str, str | None]:
    for candidate in candidates:
        value = get_value_case_insensitive(section, candidate)
        if value is not None and str(value).strip():
            return str(value).strip(), candidate
    return "", None


def load_config(config_path: Path, logger: logging.Logger) -> dict[str, Any]:
    if not config_path.exists():
        logger.error("Config file missing: %s", config_path)
        return {}
    try:
        return yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.exception("Failed to load config: %s", exc)
        return {}


def format_decimal(amount: Decimal) -> str:
    normalized = amount.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def build_payload(
    bot_name: str,
    direction: str,
    coin: str,
    requested_amount: Decimal,
    final_amount: Decimal,
    from_member_id: str | None,
    to_member_id: str | None,
    from_account_type: str | None,
    to_account_type: str | None,
    transfer_id: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    payload = {
        "bot_name": bot_name,
        "direction": direction,
        "coin": coin.upper(),
        "requested_amount": float(requested_amount),
        "final_amount": float(final_amount),
        "from_member_id": from_member_id,
        "to_member_id": to_member_id,
        "from_account_type": from_account_type,
        "to_account_type": to_account_type,
    }
    if transfer_id:
        payload["transfer_id"] = transfer_id
    if status:
        payload["status"] = status
    return payload


def main() -> None:
    args = parse_args()
    requested_amount = Decimal(str(args.amount))
    logger = setup_logger()
    config = load_config(args.config_file, logger)
    if not config:
        message = f"Missing config file or empty configuration at {args.config_file}"
        logger.error(message)
        print(message, file=sys.stderr)
        write_json_event(
            "wallet_transfer_skipped_missing_main_account",
            build_payload(
                args.bot_name,
                args.direction,
                args.coin,
                requested_amount,
                Decimal("0"),
                None,
                None,
                None,
                None,
            ),
        )
        sys.exit(1)
    (
        main_key_name,
        main_account,
        main_uid,
        main_uid_field,
        checked_main_sections,
    ) = find_main_account_section(config)
    logger.debug("Config path: %s", args.config_file)
    logger.debug("Checked main sections: %s", checked_main_sections)
    if not main_account:
        message = (
            "No complete main account section found. Need api_key, secret_key and one of "
            f"{'/'.join(MAIN_UID_CANDIDATES)}. Checked: {', '.join(MAIN_SECTION_CANDIDATES)}"
        )
        logger.error(message)
        print(message, file=sys.stderr)
        write_json_event(
            "wallet_transfer_skipped_missing_main_account",
            build_payload(
                args.bot_name,
                args.direction,
                args.coin,
                requested_amount,
                Decimal("0"),
                None,
                None,
                None,
                None,
            ),
        )
        sys.exit(1)
    logger.debug("Selected main section key: %s", main_key_name)
    logger.debug("Main account '%s' fields: %s", main_key_name, list(main_account.keys()))
    logger.debug("Selected main uid field: %s", main_uid_field or "(none)")
    main_api_key = (get_value_case_insensitive(main_account, "api_key") or "").strip()
    main_secret_key = (get_value_case_insensitive(main_account, "secret_key") or "").strip()
    if not main_api_key or not main_secret_key or not main_uid:
        message = (
            "No complete main account section found. Need api_key, secret_key and one of "
            f"{'/'.join(MAIN_UID_CANDIDATES)}. Checked: {', '.join(MAIN_SECTION_CANDIDATES)}"
        )
        logger.error(message)
        print(message, file=sys.stderr)
        write_json_event(
            "wallet_transfer_skipped_missing_main_account",
            build_payload(
                args.bot_name,
                args.direction,
                args.coin,
                requested_amount,
                Decimal("0"),
                None,
                None,
                None,
                None,
            ),
        )
        sys.exit(1)
    bot_key_name, bot_config = get_config_section_key_case_insensitive(config, args.bot_name)
    if not bot_config:
        available_keys = format_available_keys(config)
        message = (
            f"Missing bot config for {args.bot_name}. Available bot keys: {available_keys}"
        )
        logger.error(message)
        print(message, file=sys.stderr)
        write_json_event(
            "wallet_transfer_skipped_missing_bot_config",
            build_payload(
                args.bot_name,
                args.direction,
                args.coin,
                requested_amount,
                Decimal("0"),
                main_uid,
                None,
                "FUND",
                "UNIFIED",
            ),
        )
        sys.exit(1)
    logger.debug("Found bot section key: %s", bot_key_name or args.bot_name)
    logger.debug("Bot config '%s' fields: %s", bot_key_name or args.bot_name, list(bot_config.keys()))
    sub_uid, sub_uid_field = get_first_field_value(bot_config, BOT_SUB_UID_CANDIDATES)
    logger.debug("Found bot sub uid field: %s", sub_uid_field or "(none)")
    if not sub_uid:
        message = f"Missing sub_uid for {args.bot_name}"
        logger.error(message)
        print(message, file=sys.stderr)
        write_json_event(
            "wallet_transfer_skipped_missing_sub_uid",
            build_payload(
                args.bot_name,
                args.direction,
                args.coin,
                requested_amount,
                Decimal("0"),
                main_uid,
                None,
                "FUND",
                "UNIFIED",
            ),
        )
        sys.exit(1)
    direction = args.direction
    if direction not in ("refill", "cashout"):
        message = f"Invalid transfer direction: {direction}"
        logger.error(message)
        print(message, file=sys.stderr)
        write_json_event(
            "wallet_transfer_invalid_direction",
            build_payload(
                args.bot_name,
                direction,
                args.coin,
                requested_amount,
                requested_amount,
                main_uid,
                sub_uid or None,
                None,
                None,
                status="INVALID_DIRECTION",
            ),
        )
        sys.exit(1)
    if direction == "refill":
        from_member_id, to_member_id = main_uid, sub_uid
        from_account, to_account = "FUND", "UNIFIED"
    else:
        from_member_id, to_member_id = sub_uid, main_uid
        from_account, to_account = "UNIFIED", "FUND"
    if requested_amount <= 0:
        message = "Requested transfer amount must be positive"
        logger.error(message)
        print(message, file=sys.stderr)
        write_json_event(
            "wallet_transfer_skipped_below_min",
            build_payload(
                args.bot_name,
                direction,
                args.coin,
                requested_amount,
                requested_amount,
                from_member_id,
                to_member_id,
                from_account,
                to_account,
                status="BELOW_MIN",
            ),
        )
        return
    final_amount = requested_amount
    if args.max_transfer_usdt > 0:
        max_amount = Decimal(str(args.max_transfer_usdt))
        if final_amount > max_amount:
            final_amount = max_amount
            write_json_event(
                "wallet_transfer_capped_to_max",
                build_payload(
                    args.bot_name,
                    direction,
                    args.coin,
                    requested_amount,
                    final_amount,
                    from_member_id,
                    to_member_id,
                    from_account,
                    to_account,
                    status="CAPPED",
                ),
            )
    min_amount = Decimal(str(args.min_transfer_usdt))
    if final_amount < min_amount:
        message = f"Transfer amount {final_amount} below min {min_amount}"
        logger.error(message)
        print(message, file=sys.stderr)
        write_json_event(
            "wallet_transfer_skipped_below_min",
            build_payload(
                args.bot_name,
                direction,
                args.coin,
                requested_amount,
                final_amount,
                from_member_id,
                to_member_id,
                from_account,
                to_account,
                status="BELOW_MIN",
            ),
        )
        return

    transfer_id = str(uuid.uuid4())
    write_json_event(
        "wallet_transfer_requested",
        build_payload(
            args.bot_name,
            direction,
            args.coin,
            requested_amount,
            final_amount,
            from_member_id,
            to_member_id,
            from_account,
            to_account,
            transfer_id=transfer_id,
        ),
    )
    if args.dry_run:
        write_json_event(
            "wallet_transfer_dry_run",
            build_payload(
                args.bot_name,
                direction,
                args.coin,
                requested_amount,
                final_amount,
                from_member_id,
                to_member_id,
                from_account,
                to_account,
                transfer_id=transfer_id,
                status="DRY_RUN",
            ),
        )
        print(
            f"DRY RUN transfer {direction} {final_amount} {args.coin} "
            f"from {from_member_id}:{from_account} to {to_member_id}:{to_account} (id={transfer_id})"
        )
        return
    write_json_event(
        "wallet_transfer_submitted",
        build_payload(
            args.bot_name,
            direction,
            args.coin,
            requested_amount,
            final_amount,
            from_member_id,
            to_member_id,
            from_account,
            to_account,
            transfer_id=transfer_id,
            status="SUBMITTED",
        ),
    )
    manager = BybitOrderManager(main_api_key, main_secret_key)
    result = manager.create_universal_transfer(
        coin=args.coin,
        amount=format_decimal(final_amount),
        from_member_id=from_member_id,
        to_member_id=to_member_id,
        from_account_type=from_account,
        to_account_type=to_account,
        transfer_id=transfer_id,
    )
    failed_status = result.get("status") if result else None
    if not result or not result.get("ok"):
        failed_payload = build_payload(
            args.bot_name,
            direction,
            args.coin,
            requested_amount,
            final_amount,
            from_member_id,
            to_member_id,
            from_account,
            to_account,
            transfer_id=transfer_id,
            status=failed_status,
        )
        failed_payload["error"] = (result or {}).get(
            "error", "create_universal_transfer returned no result"
        )
        write_json_event("wallet_transfer_failed", failed_payload)
        logger.error("Transfer failed: %s", failed_payload.get("error"))
        sys.exit(1)
    write_json_event(
        "wallet_transfer_success",
        build_payload(
            args.bot_name,
            direction,
            args.coin,
            requested_amount,
            final_amount,
            from_member_id,
            to_member_id,
            from_account,
            to_account,
            transfer_id=transfer_id,
            status=result.get("status"),
        ),
    )


if __name__ == "__main__":
    main()
