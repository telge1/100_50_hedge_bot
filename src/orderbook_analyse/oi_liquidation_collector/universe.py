"""Universe mapping: Gold 51-coin list ∩ Bybit linear USDT perpetuals."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen

EXCLUDED_SYMBOLS = frozenset({"XAUUSDT", "XAU"})
SPECIAL_REVIEW = ("XAUTUSDT", "PAXGUSDT", "XAUUSDT", "XAU")


@dataclass
class SymbolDecision:
    symbol: str
    requested: bool
    supported: bool
    subscribed: bool
    unsupported_reason: str | None = None


@dataclass
class UniversePlan:
    source_path: str
    requested: tuple[str, ...]
    supported: tuple[str, ...]
    subscribed: tuple[str, ...]
    decisions: list[SymbolDecision]
    universe_hash: str
    special_review: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "requested": list(self.requested),
            "supported": list(self.supported),
            "subscribed": list(self.subscribed),
            "universe_hash": self.universe_hash,
            "n_requested": len(self.requested),
            "n_supported": len(self.supported),
            "n_subscribed": len(self.subscribed),
            "special_review": self.special_review,
            "decisions": [
                {
                    "symbol": d.symbol,
                    "requested": d.requested,
                    "supported": d.supported,
                    "subscribed": d.subscribed,
                    "unsupported_reason": d.unsupported_reason,
                }
                for d in self.decisions
            ],
        }


def universe_hash(symbols: list[str]) -> str:
    payload = json.dumps(sorted(symbols), separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_requested_symbols(path: Path) -> list[str]:
    data = json.loads(path.read_text())
    symbols = data.get("symbols") if isinstance(data, dict) else data
    if not isinstance(symbols, list) or not all(isinstance(s, str) for s in symbols):
        raise ValueError(f"invalid universe file: {path}")
    return list(symbols)


def fetch_bybit_linear_usdt_perps(rest_url: str, *, timeout_sec: float = 30.0) -> set[str]:
    out: set[str] = set()
    cursor = ""
    base = rest_url.rstrip("/") + "/v5/market/instruments-info"
    while True:
        params = {"category": "linear", "limit": "1000"}
        if cursor:
            params["cursor"] = cursor
        url = base + "?" + urlencode(params)
        with urlopen(url, timeout=timeout_sec) as resp:
            payload = json.loads(resp.read().decode())
        if payload.get("retCode") not in (0, None):
            raise RuntimeError(f"instruments-info failed: {payload.get('retMsg')}")
        result = payload.get("result") or {}
        for item in result.get("list") or []:
            if not isinstance(item, dict):
                continue
            if (
                item.get("quoteCoin") == "USDT"
                and item.get("contractType") == "LinearPerpetual"
                and item.get("status") == "Trading"
            ):
                out.add(str(item["symbol"]))
        cursor = str(result.get("nextPageCursor") or "").strip()
        if not cursor:
            break
    return out


def plan_universe(
    *,
    universe_path: Path,
    bybit_symbols: set[str],
    subscribe: bool = True,
) -> UniversePlan:
    requested = load_requested_symbols(universe_path)
    decisions: list[SymbolDecision] = []
    supported: list[str] = []
    for symbol in requested:
        if symbol in EXCLUDED_SYMBOLS:
            decisions.append(
                SymbolDecision(
                    symbol=symbol,
                    requested=True,
                    supported=False,
                    subscribed=False,
                    unsupported_reason="explicitly_excluded",
                )
            )
            continue
        if symbol in bybit_symbols:
            supported.append(symbol)
            decisions.append(
                SymbolDecision(
                    symbol=symbol,
                    requested=True,
                    supported=True,
                    subscribed=bool(subscribe),
                )
            )
        else:
            decisions.append(
                SymbolDecision(
                    symbol=symbol,
                    requested=True,
                    supported=False,
                    subscribed=False,
                    unsupported_reason="not_bybit_linear_usdt_perpetual_trading",
                )
            )
    extra_review = {}
    for name in SPECIAL_REVIEW:
        extra_review[name] = {
            "in_requested_universe": name in requested,
            "on_bybit_linear_usdt_perp": name in bybit_symbols,
        }
    subscribed = tuple(supported if subscribe else [])
    return UniversePlan(
        source_path=str(universe_path),
        requested=tuple(requested),
        supported=tuple(supported),
        subscribed=subscribed,
        decisions=decisions,
        universe_hash=universe_hash(supported),
        special_review=extra_review,
    )
