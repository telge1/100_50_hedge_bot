"""Incremental 1m candle window planning. Reuses existing backfill listing/repair CLI."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .config import REQUESTED_FROM, freshness_grace_minutes
from .coverage import last_closed_open_time

COIN_ALREADY_CURRENT = "ALREADY_CURRENT"
COIN_NEEDS_UPDATE = "NEEDS_UPDATE"

FORBIDDEN_CLI_TOKENS = (
    "--cleanup-first",
    "cleanup-first",
    "run_wave_fade",
    "wave_fade_shadow",
    "processing_state",
    "live_universe.json",
    "publish",
    "latest",
)


def _ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return _ensure_utc(datetime.fromisoformat(text)).replace(second=0, microsecond=0)


def iso_z(ts: datetime) -> str:
    return _ensure_utc(ts).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def last_closed_end_exclusive(now: datetime | None = None) -> datetime:
    """CLI --end is exclusive; last closed open_time is included, forming minute is not."""
    return last_closed_open_time(now) + timedelta(minutes=1)


def validate_update_symbols(raw: list[str], allowed: list[str]) -> tuple[list[str] | None, str | None]:
    if not isinstance(raw, list) or not raw:
        return None, "EMPTY_SYMBOLS"
    if len(raw) > 51:
        return None, "TOO_MANY_SYMBOLS"
    out: list[str] = []
    seen: set[str] = set()
    allowed_set = set(allowed)
    for item in raw:
        if not isinstance(item, str):
            return None, "INVALID_SYMBOL"
        symbol = item.strip().upper()
        if not symbol.isascii() or not symbol.isalnum():
            return None, "UNKNOWN_SYMBOL"
        if not symbol.endswith("USDT"):
            return None, "UNKNOWN_SYMBOL"
        if symbol not in allowed_set:
            return None, "UNKNOWN_SYMBOL"
        if symbol in seen:
            return None, "DUPLICATE_SYMBOLS"
        seen.add(symbol)
        out.append(symbol)
    return out, None


def plan_symbol_update(coin: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    last_closed = last_closed_open_time(now)
    end_exclusive = last_closed + timedelta(minutes=1)
    requested_from = parse_iso(coin.get("requested_from")) or REQUESTED_FROM
    data_to = parse_iso(coin.get("data_to"))
    data_from = parse_iso(coin.get("data_from"))
    freshness = str(coin.get("freshness_status") or "")
    coverage = str(coin.get("coverage_status") or "")

    if freshness == "CURRENT":
        return {
            "symbol": coin["symbol"],
            "action": COIN_ALREADY_CURRENT,
            "calls": [],
            "message": "bereits aktuell",
        }
    lag = coin.get("lag_minutes")
    try:
        lag_i = int(lag) if lag is not None else None
    except (TypeError, ValueError):
        lag_i = None
    if lag_i is not None and lag_i <= freshness_grace_minutes() and data_to is not None:
        return {
            "symbol": coin["symbol"],
            "action": COIN_ALREADY_CURRENT,
            "calls": [],
            "message": "bereits aktuell",
        }

    calls: list[dict[str, Any]] = []
    if coverage == "NO_DATA" or data_to is None:
        start = requested_from
        if start < end_exclusive:
            calls.append(
                {
                    "kind": "backfill",
                    "start": iso_z(start),
                    "end": iso_z(end_exclusive),
                    "repair_missing": True,
                    "resume": False,
                }
            )
    else:
        incremental_start = data_to + timedelta(minutes=1)
        if incremental_start < end_exclusive:
            calls.append(
                {
                    "kind": "incremental",
                    "start": iso_z(incremental_start),
                    "end": iso_z(end_exclusive),
                    "repair_missing": True,
                    "resume": False,
                }
            )
        repair_start = data_from or requested_from
        if repair_start < end_exclusive:
            calls.append(
                {
                    "kind": "repair_missing",
                    "start": iso_z(repair_start),
                    "end": iso_z(end_exclusive),
                    "repair_missing": True,
                    "resume": True,
                }
            )

    if not calls:
        return {
            "symbol": coin["symbol"],
            "action": COIN_ALREADY_CURRENT,
            "calls": [],
            "message": "bereits aktuell",
        }
    return {
        "symbol": coin["symbol"],
        "action": COIN_NEEDS_UPDATE,
        "calls": calls,
        "message": "wird aktualisiert",
        "update_from": calls[0]["start"],
        "update_to_exclusive": calls[0]["end"],
        "listing_limited": coverage == "LISTING_LIMITED",
    }


def argv_for_call(
    *,
    python: str,
    script: str,
    universe_file: str,
    symbol: str,
    start: str,
    end: str,
    out_dir: str,
    checkpoint: str,
    repair_missing: bool,
    resume: bool,
) -> list[str]:
    argv = [
        python,
        script,
        "--universe",
        universe_file,
        "--start",
        start,
        "--end",
        end,
        "--symbols",
        symbol,
        "--out-dir",
        out_dir,
        "--checkpoint",
        checkpoint,
    ]
    if repair_missing:
        argv.append("--repair-missing")
    if resume:
        argv.append("--resume")
    joined = " ".join(argv).lower()
    for token in FORBIDDEN_CLI_TOKENS:
        if token.lower() in joined and token in ("--cleanup-first", "run_wave_fade", "wave_fade_shadow"):
            raise RuntimeError("forbidden CLI token")
    if "--cleanup-first" in argv:
        raise RuntimeError("cleanup-first is forbidden")
    return argv
