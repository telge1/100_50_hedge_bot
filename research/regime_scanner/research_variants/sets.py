"""Predefined variant sets."""

from __future__ import annotations

from research.regime_scanner.research_variants.model import ResearchVariant, ResearchVariantSet

SIMPLE_REGIME_STABILITY_V1 = ResearchVariantSet(
    name="simple_regime_stability_v1",
    description=(
        "Five controlled variants around baseline TrendStateConfig: "
        "confirmation hold bars and indicator strength thresholds."
    ),
    variants=(
        ResearchVariant(
            name="baseline",
            description="Unchanged baseline parameter set.",
            parameter_overrides={},
            tags=("baseline",),
        ),
        ResearchVariant(
            name="faster_confirmation",
            description="Reduces min_hold for established trend states by 1 bar.",
            parameter_overrides={
                "trend_state.min_hold_bars.strong_bullish": 3,
                "trend_state.min_hold_bars.strong_bearish": 3,
                "trend_state.min_hold_bars.early_bullish": 2,
                "trend_state.min_hold_bars.early_bearish": 2,
            },
            tags=("confirmation",),
        ),
        ResearchVariant(
            name="slower_confirmation",
            description="Increases min_hold for established trend states by 1 bar.",
            parameter_overrides={
                "trend_state.min_hold_bars.strong_bullish": 5,
                "trend_state.min_hold_bars.strong_bearish": 5,
                "trend_state.min_hold_bars.early_bullish": 4,
                "trend_state.min_hold_bars.early_bearish": 4,
            },
            tags=("confirmation",),
        ),
        ResearchVariant(
            name="stricter_trend_strength",
            description="Raises ADX and DI-spread confirmation thresholds.",
            parameter_overrides={
                "trend_state.adx_confirm": 22.0,
                "trend_state.di_spread_confirm": 7.0,
            },
            tags=("trend_strength",),
        ),
        ResearchVariant(
            name="looser_trend_strength",
            description="Lowers ADX and DI-spread confirmation thresholds.",
            parameter_overrides={
                "trend_state.adx_confirm": 15.0,
                "trend_state.di_spread_confirm": 3.0,
            },
            tags=("trend_strength",),
        ),
    ),
)

_KNOWN_SETS = {
    SIMPLE_REGIME_STABILITY_V1.name: SIMPLE_REGIME_STABILITY_V1,
}


def get_variant_set(name: str) -> ResearchVariantSet:
    key = str(name).strip()
    if key not in _KNOWN_SETS:
        raise ValueError(f"unknown variant set: {name!r}")
    return _KNOWN_SETS[key]
