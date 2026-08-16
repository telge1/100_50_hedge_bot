"""Deterministic Bybit linear USDT perpetual universe selection."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

from signal_generator.bybit.history import HttpTransport, HttpxTransport, millis_to_utc

INSTRUMENTS_URL = "https://api.bybit.com/v5/market/instruments-info"
TICKERS_URL = "https://api.bybit.com/v5/market/tickers"

MUST_INCLUDE = ("BTCUSDT", "ETHUSDT", "DOGEUSDT", "APTUSDT")
SELECTION_METHOD = "top_n_by_turnover24h_linear_usdt_perpetual_trading"


@dataclass(slots=True)
class UniverseSymbol:
    symbol: str
    rank: int
    turnover24h: str
    volume24h: str
    launch_time: str | None
    contract_type: str
    status: str
    settle_coin: str


@dataclass(slots=True)
class Universe:
    generated_at: str
    source: str
    selection_method: str
    target_size: int
    symbols: list[str]
    details: list[UniverseSymbol]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "source": self.source,
            "selection_method": self.selection_method,
            "target_size": self.target_size,
            "symbols": list(self.symbols),
            "details": [asdict(d) for d in self.details],
        }


def _paginate_instruments(transport: HttpTransport) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"category": "linear", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        payload = transport.get_json(INSTRUMENTS_URL, params)
        result = payload.get("result") or {}
        rows = result.get("list") or []
        out.extend(rows)
        cursor = result.get("nextPageCursor") or None
        if not cursor:
            break
    return out


def filter_active_linear_usdt_perpetuals(
    instruments: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep only trading Linear USDT perpetuals (no options / dated futures)."""
    out: list[dict[str, Any]] = []
    for item in instruments:
        if item.get("category") not in (None, "linear"):
            # instruments-info list items often omit category; already requested linear
            pass
        if item.get("settleCoin") != "USDT":
            continue
        if item.get("status") != "Trading":
            continue
        if item.get("contractType") != "LinearPerpetual":
            continue
        # deliveryTime "0" means perpetual; reject dated contracts defensively
        delivery = str(item.get("deliveryTime") or "0")
        if delivery not in ("0", ""):
            continue
        out.append(item)
    return out


def rank_by_turnover24h(
    instruments: Sequence[dict[str, Any]],
    tickers: Sequence[dict[str, Any]],
) -> list[UniverseSymbol]:
    """Deterministic ranking: turnover24h DESC, then symbol ASC."""
    by_symbol = {t["symbol"]: t for t in tickers if "symbol" in t}
    ranked: list[tuple[Decimal, str, UniverseSymbol]] = []
    for item in instruments:
        symbol = str(item["symbol"]).upper()
        t = by_symbol.get(symbol, {})
        turnover = Decimal(str(t.get("turnover24h") or "0"))
        volume = str(t.get("volume24h") or "0")
        launch_raw = item.get("launchTime")
        launch_iso = None
        if launch_raw not in (None, "", "0"):
            launch_iso = millis_to_utc(launch_raw).isoformat()
        detail = UniverseSymbol(
            symbol=symbol,
            rank=0,
            turnover24h=str(turnover),
            volume24h=volume,
            launch_time=launch_iso,
            contract_type=str(item.get("contractType")),
            status=str(item.get("status")),
            settle_coin=str(item.get("settleCoin")),
        )
        ranked.append((turnover, symbol, detail))
    ranked.sort(key=lambda r: (-r[0], r[1]))
    out: list[UniverseSymbol] = []
    for i, (_, _, detail) in enumerate(ranked, start=1):
        detail.rank = i
        out.append(detail)
    return out


def select_universe(
    ranked: Sequence[UniverseSymbol],
    *,
    target_size: int = 100,
    must_include: Sequence[str] = MUST_INCLUDE,
) -> list[UniverseSymbol]:
    """Take top N by rank, then force-include must-have symbols (dropping lowest)."""
    if target_size <= 0:
        raise ValueError("target_size must be > 0")
    by_symbol = {d.symbol: d for d in ranked}
    selected: list[UniverseSymbol] = list(ranked[:target_size])
    selected_set = {d.symbol for d in selected}
    for must in must_include:
        must = must.upper()
        if must not in by_symbol:
            raise ValueError(f"Must-include symbol not eligible/tradable: {must}")
        if must in selected_set:
            continue
        # Drop lowest-ranked selected to make room
        selected.sort(key=lambda d: d.rank)
        dropped = selected.pop()
        selected_set.remove(dropped.symbol)
        selected.append(by_symbol[must])
        selected_set.add(must)
    selected.sort(key=lambda d: d.rank)
    # Re-number ranks within the selected universe for clarity (stable global rank kept)
    return selected


def build_universe(
    transport: HttpTransport | None = None,
    *,
    target_size: int = 100,
    must_include: Sequence[str] = MUST_INCLUDE,
) -> Universe:
    transport = transport or HttpxTransport()
    instruments = filter_active_linear_usdt_perpetuals(_paginate_instruments(transport))
    tickers_payload = transport.get_json(TICKERS_URL, {"category": "linear"})
    tickers = (tickers_payload.get("result") or {}).get("list") or []
    ranked = rank_by_turnover24h(instruments, tickers)
    selected = select_universe(ranked, target_size=target_size, must_include=must_include)
    return Universe(
        generated_at=datetime.now(timezone.utc).isoformat(),
        source="bybit_v5_instruments_info+tickers",
        selection_method=SELECTION_METHOD,
        target_size=target_size,
        symbols=[d.symbol for d in selected],
        details=selected,
    )


def save_universe(universe: Universe, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(universe.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_universe(path: Path) -> Universe:
    raw = json.loads(path.read_text(encoding="utf-8"))
    details = [
        UniverseSymbol(
            symbol=d["symbol"],
            rank=int(d["rank"]),
            turnover24h=str(d["turnover24h"]),
            volume24h=str(d["volume24h"]),
            launch_time=d.get("launch_time"),
            contract_type=str(d.get("contract_type", "LinearPerpetual")),
            status=str(d.get("status", "Trading")),
            settle_coin=str(d.get("settle_coin", "USDT")),
        )
        for d in raw.get("details") or []
    ]
    symbols = list(raw["symbols"])
    if not details:
        details = [
            UniverseSymbol(
                symbol=s,
                rank=i,
                turnover24h="0",
                volume24h="0",
                launch_time=None,
                contract_type="LinearPerpetual",
                status="Trading",
                settle_coin="USDT",
            )
            for i, s in enumerate(symbols, start=1)
        ]
    return Universe(
        generated_at=str(raw["generated_at"]),
        source=str(raw["source"]),
        selection_method=str(raw["selection_method"]),
        target_size=int(raw.get("target_size") or len(symbols)),
        symbols=symbols,
        details=details,
    )


def launch_time_utc(detail: UniverseSymbol | None) -> datetime | None:
    if detail is None or not detail.launch_time:
        return None
    text = detail.launch_time
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text).astimezone(timezone.utc)
