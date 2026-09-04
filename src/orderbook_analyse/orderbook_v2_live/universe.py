"""Deterministic Orderbook V3 live universe. Same 51 coins as historical rollout."""

from __future__ import annotations

SHADOW3_SYMBOLS: tuple[str, ...] = ("ADAUSDT", "BTCUSDT", "ETHUSDT")
ADA_SYMBOLS: tuple[str, ...] = ("ADAUSDT",)
SYMBOLS_48: tuple[str, ...] = (
    "SOLUSDT",
    "XRPUSDT",
    "BNBUSDT",
    "DOGEUSDT",
    "LINKUSDT",
    "AVAXUSDT",
    "LTCUSDT",
    "DOTUSDT",
    "SUIUSDT",
    "APTUSDT",
    "NEARUSDT",
    "ATOMUSDT",
    "UNIUSDT",
    "AAVEUSDT",
    "ARBUSDT",
    "OPUSDT",
    "TRXUSDT",
    "XLMUSDT",
    "HBARUSDT",
    "ALGOUSDT",
    "INJUSDT",
    "TIAUSDT",
    "ICPUSDT",
    "RENDERUSDT",
    "CRVUSDT",
    "MNTUSDT",
    "HYPEUSDT",
    "ZECUSDT",
    "XMRUSDT",
    "TAOUSDT",
    "WLDUSDT",
    "ENAUSDT",
    "ONDOUSDT",
    "JTOUSDT",
    "1000PEPEUSDT",
    "SHIB1000USDT",
    "1000BONKUSDT",
    "WIFUSDT",
    "PENGUUSDT",
    "TRUMPUSDT",
    "PUMPFUNUSDT",
    "FARTCOINUSDT",
    "KAITOUSDT",
    "WLFIUSDT",
    "XPLUSDT",
    "LITUSDT",
    "XAUTUSDT",
    "PAXGUSDT",
)
SYMBOLS_51: tuple[str, ...] = SHADOW3_SYMBOLS + SYMBOLS_48
FORBIDDEN_SYMBOLS: frozenset[str] = frozenset({"XAUUSDT"})

MODES = ("ada", "shadow3", "universe51", "raw-archive-only")


class UniverseError(ValueError):
    pass


def validate_universe(symbols: tuple[str, ...]) -> None:
    if len(symbols) != len(set(symbols)):
        raise UniverseError("duplicate symbols in universe")
    bad = [s for s in symbols if s in FORBIDDEN_SYMBOLS]
    if bad:
        raise UniverseError("forbidden symbols: " + ",".join(bad))
    if "XAUTUSDT" not in SYMBOLS_51:
        raise UniverseError("XAUTUSDT missing from SYMBOLS_51")


def symbols_for_mode(mode: str) -> tuple[str, ...]:
    if mode == "ada":
        return ADA_SYMBOLS
    if mode == "shadow3":
        return SHADOW3_SYMBOLS
    if mode == "universe51":
        validate_universe(SYMBOLS_51)
        if len(SYMBOLS_51) != 51:
            raise UniverseError(f"SYMBOLS_51 length {len(SYMBOLS_51)}")
        return SYMBOLS_51
    raise UniverseError(f"unknown mode {mode!r}")


validate_universe(SYMBOLS_51)
