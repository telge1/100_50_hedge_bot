"""Request parsing and validation (no onboarding business logic)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .config import (
    ALLOWED_PRESET_DAYS,
    DASHBOARD_MAX_HISTORY_DAYS,
    FORBIDDEN_REQUEST_KEYS,
)

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}USDT$")


@dataclass(frozen=True)
class OnboardForm:
    symbol: str
    days: int
    candles_1m: bool
    open_interest_5m: bool
    public_trades: bool
    with_ob1000: bool
    restart_live: bool
    purpose: str = "onboard"  # onboard | extend_history

    def to_public(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "days": self.days,
            "candles_1m": self.candles_1m,
            "open_interest_5m": self.open_interest_5m,
            "public_trades": self.public_trades,
            "with_ob1000": self.with_ob1000,
            "restart_live": self.restart_live,
            "purpose": self.purpose,
            "live_only": {
                "ob1000": self.with_ob1000,
                "open_interest_5s": True,
                "liquidations": True,
            },
            "historical": {
                "candles_1m": self.candles_1m,
                "open_interest_5m": self.open_interest_5m,
                "public_trades": self.public_trades,
            },
        }

    def skip_flags(self) -> dict[str, bool]:
        skip_candles = not self.candles_1m
        skip_oi = not self.open_interest_5m
        skip_trades = not self.public_trades
        skip_backfill = skip_candles and skip_oi and skip_trades
        return {
            "skip_backfill": skip_backfill,
            "skip_candles": skip_candles,
            "skip_oi": skip_oi,
            "skip_trades": skip_trades,
        }


def _reject_forbidden_keys(raw: dict[str, Any]) -> str | None:
    for key in raw:
        norm = str(key).strip().lower().replace("-", "_")
        if norm in FORBIDDEN_REQUEST_KEYS:
            return "FORBIDDEN_FIELD"
        if "full_ob" in norm or "orderbook_full" in norm:
            return "FORBIDDEN_FIELD"
    return None


def parse_days(raw: dict[str, Any]) -> tuple[int | None, str | None]:
    if "days" in raw and raw.get("days") is not None:
        try:
            days = int(raw["days"])
        except (TypeError, ValueError):
            return None, "INVALID_DAYS"
    elif raw.get("history_preset") is not None:
        try:
            days = int(raw["history_preset"])
        except (TypeError, ValueError):
            return None, "INVALID_DAYS"
    else:
        days = 30
    if days < 1 or days > DASHBOARD_MAX_HISTORY_DAYS:
        return None, "DAYS_OUT_OF_RANGE"
    # custom allowed within max; presets are just UI helpers
    _ = ALLOWED_PRESET_DAYS
    return days, None


def parse_onboard_form(raw: dict[str, Any]) -> tuple[OnboardForm | None, str | None]:
    if not isinstance(raw, dict):
        return None, "INVALID_JSON"
    forbidden = _reject_forbidden_keys(raw)
    if forbidden:
        return None, forbidden

    symbol = str(raw.get("symbol") or "").strip().upper()
    if not symbol or not _SYMBOL_RE.match(symbol):
        return None, "INVALID_SYMBOL"

    days, err = parse_days(raw)
    if err or days is None:
        return None, err or "INVALID_DAYS"

    def flag(name: str, default: bool = True) -> bool:
        if name not in raw:
            return default
        return bool(raw.get(name))

    purpose = str(raw.get("purpose") or "onboard").strip().lower()
    if purpose not in {"onboard", "extend_history"}:
        return None, "INVALID_PURPOSE"

    with_ob1000 = flag("with_ob1000", False)
    restart_live = flag("restart_live", False)
    # History enlarge: backfill only — never enable OB1000 or collector restarts.
    if purpose == "extend_history":
        with_ob1000 = False
        restart_live = False
        if not (
            flag("candles_1m", True)
            or flag("open_interest_5m", True)
            or flag("public_trades", True)
        ):
            return None, "EXTEND_REQUIRES_HISTORY_STREAM"

    form = OnboardForm(
        symbol=symbol,
        days=days,
        candles_1m=flag("candles_1m", True),
        open_interest_5m=flag("open_interest_5m", True),
        public_trades=flag("public_trades", True),
        with_ob1000=with_ob1000,
        restart_live=restart_live,
        purpose=purpose,
    )
    return form, None
