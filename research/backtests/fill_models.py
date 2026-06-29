"""Fill model selection for 5m OHLC backtests (Phase 7)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .purpose_utils import preserve_bot_purpose

FillModel = Literal["conservative", "conservative_multi", "paired_exit"]

FILL_MODELS: tuple[str, ...] = ("conservative", "conservative_multi", "paired_exit")

DEFAULT_MAX_FILLS_BY_MODEL: dict[str, int] = {
    "conservative": 1,
    "conservative_multi": 2,
    "paired_exit": 2,
}

EXIT_PURPOSES = frozenset(
    {
        "LONG_TP_EXIT",
        "LONG_SL_EXIT",
        "SHORT_TP_EXIT",
        "SHORT_SL_EXIT",
    }
)

COMPARE_FILL_MODELS: tuple[tuple[str, int | None], ...] = (
    ("conservative", None),
    ("conservative_multi", None),
    ("paired_exit", None),
)


@dataclass(frozen=True)
class FillModelConfig:
    fill_model: str
    max_fills_per_candle: int

    @property
    def uses_conservative_ranking(self) -> bool:
        return self.fill_model in {"conservative", "conservative_multi"}


def normalize_fill_model(fill_model: str | None) -> str:
    normalized = str(fill_model or "conservative").strip().lower()
    if normalized not in FILL_MODELS:
        raise ValueError(f"unsupported fill_model: {fill_model}")
    return normalized


def default_max_fills_for_model(fill_model: str) -> int:
    return int(DEFAULT_MAX_FILLS_BY_MODEL[normalize_fill_model(fill_model)])


def resolve_fill_model_config(
    *,
    fill_model: str | None = "conservative",
    max_fills_per_candle: int | None = None,
) -> FillModelConfig:
    model = normalize_fill_model(fill_model)
    limit = (
        int(max_fills_per_candle)
        if max_fills_per_candle is not None
        else default_max_fills_for_model(model)
    )
    return FillModelConfig(fill_model=model, max_fills_per_candle=max(0, limit))


def is_exit_purpose(purpose: object) -> bool:
    return preserve_bot_purpose(purpose) in EXIT_PURPOSES
