"""Versioned instrument metadata for BTC/DOGE fight analysis (no live REST)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any, Final

INSTRUMENT_CONTRACT_VERSION: Final[str] = "fight_instrument_contract_v1"
INSTRUMENT_METADATA_SOURCE: Final[str] = "research.btc_doge_research.phase2_contracts.TICK_SIZE"
INSTRUMENT_METADATA_VERSION: Final[str] = "btc_doge_research_phase_2_pilot_v1"


@dataclass(frozen=True)
class InstrumentContract:
    symbol: str
    tick_size: Decimal
    quantity_step: Decimal | None
    price_bin_step_rule: str
    price_precision: int
    quantity_precision: int
    contract_type: str
    source: str
    metadata_version: str

    def tick_size_f(self) -> float:
        return float(self.tick_size)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["tick_size"] = str(self.tick_size)
        if self.quantity_step is not None:
            d["quantity_step"] = str(self.quantity_step)
        d["contract_version"] = INSTRUMENT_CONTRACT_VERSION
        return d


_INSTRUMENTS: dict[str, InstrumentContract] = {
    "BTCUSDT": InstrumentContract(
        symbol="BTCUSDT",
        tick_size=Decimal("0.1"),
        quantity_step=Decimal("0.001"),
        price_bin_step_rule="resolve_price_step(low,high,target_bins) with tick alignment",
        price_precision=1,
        quantity_precision=3,
        contract_type="linear_perp",
        source=INSTRUMENT_METADATA_SOURCE,
        metadata_version=INSTRUMENT_METADATA_VERSION,
    ),
    "DOGEUSDT": InstrumentContract(
        symbol="DOGEUSDT",
        tick_size=Decimal("0.00001"),
        quantity_step=Decimal("1"),
        price_bin_step_rule="resolve_price_step(low,high,target_bins) with tick alignment",
        price_precision=5,
        quantity_precision=0,
        contract_type="linear_perp",
        source=INSTRUMENT_METADATA_SOURCE,
        metadata_version=INSTRUMENT_METADATA_VERSION,
    ),
}


_TICK_INV: dict[str, float] = {
    "BTCUSDT": 10.0,  # 1 / 0.1
    "DOGEUSDT": 100_000.0,  # 1 / 0.00001
}


def instrument_for(symbol: str) -> InstrumentContract:
    key = symbol.upper()
    if key not in _INSTRUMENTS:
        raise KeyError(f"unsupported instrument symbol: {symbol}")
    return _INSTRUMENTS[key]


def price_to_tick(price: float | Decimal, symbol_or_tick: str | float | Decimal) -> int:
    """Map price → tick index.

    Fast float path for known symbols (multiply by exact inverse tick).
    Falls back to Decimal for arbitrary tick sizes.
    """
    if isinstance(symbol_or_tick, str):
        inv = _TICK_INV.get(symbol_or_tick.upper())
        if inv is not None:
            return int(round(float(price) * inv))
        step = instrument_for(symbol_or_tick).tick_size
        return int((Decimal(str(price)) / step).to_integral_value())
    # Numeric tick size: prefer exact inverse for power-of-ten ticks.
    step_f = float(symbol_or_tick)
    if step_f in (0.1, 0.00001):
        return int(round(float(price) / step_f)) if step_f == 0.00001 else int(round(float(price) * 10.0))
    return int((Decimal(str(price)) / Decimal(str(symbol_or_tick))).to_integral_value())


def tick_to_price(tick: int, symbol_or_tick: str | float | Decimal) -> float:
    if isinstance(symbol_or_tick, str):
        inv = _TICK_INV.get(symbol_or_tick.upper())
        if inv is not None:
            return float(tick) / inv
        step = instrument_for(symbol_or_tick).tick_size
        return float(Decimal(tick) * step)
    step_f = float(symbol_or_tick)
    if step_f in (0.1, 0.00001):
        return float(tick) * step_f
    return float(Decimal(tick) * Decimal(str(symbol_or_tick)))
