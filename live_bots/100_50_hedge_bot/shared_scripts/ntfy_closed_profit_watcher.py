#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import requests
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[3]
LOGS_ROOT = PROJECT_ROOT / "live_bots" / "100_50_hedge_bot" / "logs"
DASHBOARD_LOGS = PROJECT_ROOT / "logs"
DEFAULT_LOG_FILE = LOGS_ROOT / "ntfy_closed_profit_watcher.log"
DEFAULT_DEDUPE_FILE = LOGS_ROOT / "ntfy_sent_trade_notifications.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger("ntfy_closed_profit_watcher")


def load_ntfy_config():
    config_path = PROJECT_ROOT / "config" / "config.yaml"
    if not config_path.exists():
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
            return cfg.get("notifications") or {}
    except Exception:
        LOGGER.exception("ntfy_profit_watcher_state_error failed_loading_config")
        return {}


def resolve_ntfy_url_topic(args):
    config = load_ntfy_config()
    ntfy_url = args.ntfy_url or os.getenv("NTFY_URL") or config.get("ntfy_url") or ""
    ntfy_topic = args.ntfy_topic or os.getenv("NTFY_TOPIC") or config.get("ntfy_topic") or ""
    if ntfy_url and ntfy_topic:
        if not ntfy_url.endswith("/"):
            ntfy_url = f"{ntfy_url.rstrip('/')}/{ntfy_topic.lstrip('/')}"
        else:
            ntfy_url = f"{ntfy_url.rstrip('/')}/{ntfy_topic.lstrip('/')}"
    elif ntfy_topic and not ntfy_url:
        ntfy_url = f"https://ntfy.sh/{ntfy_topic}"
    return ntfy_url, args.ntfy_token or os.getenv("NTFY_TOKEN")


def _log_event(event: str, **kwargs):
    LOGGER.info("%s %s", event, json.dumps(kwargs, ensure_ascii=False))


def _list_history_paths(profile: str, explicit: str | None = None):
    if explicit:
        explicit_path = Path(explicit)
        if not explicit_path.exists():
            _log_event("ntfy_profit_watcher_source_missing", path=str(explicit_path))
            return []
        return [explicit_path]

    files = [
        DASHBOARD_LOGS / "dashboard_closed_pnl_history.jsonl",
        DASHBOARD_LOGS / "confirmed_order_pnl_history.jsonl",
    ]
    if profile and profile.lower().startswith("bot_"):
        idx = profile.split("_", 1)[-1]
        bot_name = f"long_bot_{idx}"
    elif profile and profile.lower().startswith("long_bot_"):
        bot_name = profile.lower()
    elif profile and profile.lower().startswith("longbot"):
        bot_name = profile.lower()
    else:
        bot_name = None
    if profile and profile.lower().startswith("bot_") and profile.lower() != "bot_1":
        pass
    bots = ["long_bot_1", "long_bot_2", "long_bot_3", "long_bot_4"]
    selected = bots if profile == "all" or not profile else [bot_name] if bot_name else bots
    for bot in selected:
        bot_prefix = LOGS_ROOT.parent / bot / "logs"
        if not bot_prefix.exists():
            continue
        files.extend(
            [
                bot_prefix / "dashboard_closed_pnl_history.jsonl",
                bot_prefix / "confirmed_order_pnl_history.jsonl",
            ]
        )
    return [path for path in files if path.exists()]


def _load_jsonl(path: Path):
    rows = []
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                with suppress(Exception):
                    rows.append(json.loads(line))
    except Exception:
        LOGGER.exception("ntfy_profit_watcher_state_error error_reading_history path=%s", path)
    return rows


def _normalize_entry(entry: dict, path: Path, skip_logger: dict):
    bot = entry.get("bot_name") or entry.get("bot")
    if not bot and entry.get("account"):
        bot = str(entry["account"]).replace(" ", "_").lower()
    symbol = entry.get("symbol")
    tbid = entry.get("trade_block_id") or entry.get("trade_block") or entry.get("tradeId")
    value = (
        entry.get("total_trade_pnl")
        or entry.get("total_profit")
        or entry.get("profit")
        or entry.get("pnl")
        or (entry.get("breakdown") or {}).get("total_trade_pnl")
        or entry.get("final_exit_net_pnl")
    )
    status = (entry.get("status") or entry.get("trade_status") or "").lower()
    source = str(entry.get("source") or "").lower()
    pnl_complete = entry.get("pnl_complete")
    finalized = entry.get("finalized_at") or entry.get("created_at") or entry.get("created_at_utc3")
    is_closed = (
        status == "closed"
        or source == "bybit_closed_pnl"
        or bool(pnl_complete)
        or bool(entry.get("finalized_at"))
        or bool(finalized)
    )
    if not (bot and symbol and tbid and value is not None and is_closed):
        if skip_logger["count"] < 20:
            available = list(entry.keys())
            _log_event(
                "ntfy_profit_watcher_trade_parse_skipped",
                reason="missing_required_field_or_not_closed",
                available_keys=available,
                file=str(path),
            )
        skip_logger["count"] += 1
        return None
    try:
        profit = float(value)
    except (TypeError, ValueError):
        if skip_logger["count"] < 20:
            _log_event(
                "ntfy_profit_watcher_trade_parse_skipped",
                reason="invalid_profit_value",
                available_keys=list(entry.keys()),
                file=str(path),
            )
        skip_logger["count"] += 1
        return None
    normalized = {
        "bot_name": str(bot).lower(),
        "symbol": str(symbol).upper(),
        "trade_block_id": str(tbid),
        "total_trade_pnl": profit,
        "status": status or "closed",
        "timestamp": finalized or entry.get("timestamp") or entry.get("updated_at") or entry.get("created_at"),
        "finalized_at": entry.get("finalized_at") or entry.get("created_at_utc3"),
    }
    return normalized


def _dedupe_records(records: list[dict]):
    seen = {}
    for record in records:
        key = record["trade_block_id"]
        existing = seen.get(key)
        if not existing:
            seen[key] = record
            continue
        if existing["status"] == "closed":
            continue
        if record["status"] == "closed":
            seen[key] = record
            continue
        seen[key] = record
    return list(seen.values())


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return None


def _trade_sort_key(record: dict) -> tuple[datetime, int]:
    for key in (
        "finalized_at",
        "created_at_utc3",
        "end_time",
        "timestamp",
        "updated_at",
        "created_at",
    ):
        dt = _parse_timestamp(record.get(key))
        if dt:
            return (dt, 0)
    return (datetime.min.replace(tzinfo=timezone.utc), 0)


def _load_history(profile: str, source_file: str | None = None):
    if source_file:
        paths = _list_history_paths(profile, explicit=source_file)
        if not paths:
            _log_event("ntfy_profit_watcher_source_missing", source=source_file)
            return []
    else:
        paths = _list_history_paths(profile)
    if not paths:
        _log_event("ntfy_profit_watcher_source_missing", profile=profile)
        return []
    LOGGER.info("ntfy_profit_watcher_source_loaded profile=%s files=%s", profile, [str(p) for p in paths])
    rows: list[tuple[dict, Path]] = []
    for path in paths:
        for payload in _load_jsonl(path):
            rows.append((payload, path))
    normalized = []
    skip_logger = {"count": 0}
    for payload, path in rows:
        entry = _normalize_entry(payload, path, skip_logger)
        if entry:
            normalized.append(entry)
    if normalized:
        _log_event(
            "ntfy_profit_watcher_trades_collected",
            count=len(normalized),
            files=[str(p) for p in paths],
        )
    return _dedupe_records(normalized)
    normalized = []
    for payload in rows:
        entry = _normalize_entry(payload)
        if entry:
            if entry["status"] != "closed":
                continue
            normalized.append(entry)
    return _dedupe_records(normalized)


def _load_dedupe(path: Path):
    if not path.exists():
        return {"sent_keys": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        LOGGER.exception("ntfy_profit_watcher_state_error dedupe_read_failed")
        return {"sent_keys": {}}


def _atomic_write(path: Path, payload: dict):
    tmp = path.parent / f"{path.name}.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _new_sent_entry(record: dict) -> dict:
    return {
        "bot_name": record["bot_name"],
        "symbol": record["symbol"],
        "trade_block_id": record["trade_block_id"],
        "total_trade_pnl": record["total_trade_pnl"],
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }


def _send_ntfy(ntfy_url: str, token: str | None, message: str):
    headers = {
        "Title": "Trade closed",
        "Priority": "default",
        "Tags": "moneybag,chart_with_upwards_trend",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = requests.post(ntfy_url, data=message.encode("utf-8"), headers=headers, timeout=10)
    return response.status_code == 200


def _format_message(record: dict):
    profit = record["total_trade_pnl"]
    sign = "+" if profit > 0 else ""
    formatted = f"{record['bot_name']} | {record['symbol']} | {sign}{profit:.4f} USDT"
    return formatted


def run_once(args):
    dedupe_file = Path(args.dedupe_file)
    dedupe = _load_dedupe(dedupe_file)
    sent = dedupe.setdefault("sent_keys", {})
    profile = args.profile or "all"
    trades = _load_history(profile, source_file=args.source_file)

    if not dedupe_file.exists() and not args.seed_only:
        sorted_trades = sorted(trades, key=_trade_sort_key)
        send_last = args.send_last_on_initial_seed
        latest = sorted_trades[-1] if sorted_trades else None
        older = sorted_trades[:-1] if send_last and latest else sorted_trades
        for record in older:
            key = f"{record['bot_name']}|{record['symbol']}|{record['trade_block_id']}"
            sent[key] = _new_sent_entry(record)
        _atomic_write(dedupe_file, dedupe)
        _log_event(
            "ntfy_profit_watcher_seeded_existing",
            count_old_seeded=len(older),
            send_last_on_initial_seed=send_last,
        )
        if send_last and latest:
            key = f"{latest['bot_name']}|{latest['symbol']}|{latest['trade_block_id']}"
            _log_event(
                "ntfy_profit_watcher_initial_seed_send_last_selected",
                key=key,
                bot_name=latest["bot_name"],
                symbol=latest["symbol"],
                total_trade_pnl=latest["total_trade_pnl"],
            )
            ntfy_url, token = resolve_ntfy_url_topic(args)
            if not ntfy_url:
                _log_event(
                    "ntfy_profit_watcher_initial_seed_last_send_failed",
                    reason="missing_ntfy_url",
                    key=key,
                )
                return
            message = _format_message(latest)
            if _send_ntfy(ntfy_url, token, message):
                sent[key] = _new_sent_entry(latest)
                _atomic_write(dedupe_file, dedupe)
                _log_event(
                    "ntfy_profit_watcher_initial_seed_last_sent",
                    key=key,
                    message=message,
                )
            else:
                _log_event(
                    "ntfy_profit_watcher_initial_seed_last_send_failed",
                    key=key,
                    message=message,
                )
        return

    if args.seed_only:
        for record in trades:
            key = f"{record['bot_name']}|{record['symbol']}|{record['trade_block_id']}"
            sent[key] = {
                "bot_name": record["bot_name"],
                "symbol": record["symbol"],
                "trade_block_id": record["trade_block_id"],
                "total_trade_pnl": record["total_trade_pnl"],
                "sent_at": datetime.now(timezone.utc).isoformat(),
            }
        _atomic_write(dedupe_file, dedupe)
        _log_event("ntfy_profit_watcher_seeded_existing", reason="seed_only")
        return

    new_trades = []
    for record in trades:
        key = f"{record['bot_name']}|{record['symbol']}|{record['trade_block_id']}"
        if key in sent:
            _log_event("ntfy_profit_watcher_trade_known_skipped", key=key)
            continue
        new_trades.append((key, record))

    if not new_trades:
        _log_event("ntfy_profit_watcher_no_new_trades", profile=profile)
        return

    ntfy_url, token = resolve_ntfy_url_topic(args)
    if not ntfy_url:
        _log_event("ntfy_profit_watcher_state_error", missing_ntfy_url=True)
        return

    for key, record in new_trades:
        message = _format_message(record)
        success = _send_ntfy(ntfy_url, token, message)
        if success:
            sent[key] = {
                "bot_name": record["bot_name"],
                "symbol": record["symbol"],
                "trade_block_id": record["trade_block_id"],
                "total_trade_pnl": record["total_trade_pnl"],
                "sent_at": datetime.now(timezone.utc).isoformat(),
            }
            _log_event("ntfy_profit_watcher_trade_sent", key=key, message=message)
        else:
            _log_event("ntfy_profit_watcher_send_failed", key=key, message=message)
    _atomic_write(dedupe_file, dedupe)


def main(args: Iterable[str] | None = None):
    parser = argparse.ArgumentParser(description="ntfy closed trade profit watcher")
    parser.add_argument("--no-send-last-on-initial-seed", dest="send_last_on_initial_seed", action="store_false", default=True)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--profile", type=str, default="all")
    parser.add_argument("--dedupe-file", type=str, default=str(DEFAULT_DEDUPE_FILE))
    parser.add_argument("--log-file", type=str, default=str(DEFAULT_LOG_FILE))
    parser.add_argument("--source-file", type=str, default=None)
    parser.add_argument("--seed-only", action="store_true")
    parser.add_argument("--ntfy-url", type=str)
    parser.add_argument("--ntfy-topic", type=str)
    parser.add_argument("--ntfy-token", type=str)
    parsed = parser.parse_args(args=args)

    log_path = Path(parsed.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(str(log_path), encoding="utf-8")
    logging.getLogger().addHandler(handler)

    _log_event("ntfy_profit_watcher_started", profile=parsed.profile, reason="loop" if parsed.loop else "once")

    if parsed.once:
        run_once(parsed)
        return

    while True:
        run_once(parsed)
        if not parsed.loop:
            break
        time.sleep(parsed.interval)


if __name__ == "__main__":
    main(sys.argv[1:])
