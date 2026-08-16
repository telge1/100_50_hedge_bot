"""Shadow Wave-Fade signal pipeline package."""

from __future__ import annotations

from signal_generator.pipeline.processor import (
    PipelineMetrics,
    ShadowPipelineConfig,
    WaveFadeShadowPipeline,
)
from signal_generator.pipeline.signal_id import deterministic_signal_id
from signal_generator.pipeline.versions import (
    EDGES_VERSION,
    GENERATOR_VERSION,
    GLOBAL_FROZEN_TIER_A,
    STRATEGY_VERSION,
)

__all__ = [
    "EDGES_VERSION",
    "GENERATOR_VERSION",
    "GLOBAL_FROZEN_TIER_A",
    "PipelineMetrics",
    "STRATEGY_VERSION",
    "ShadowPipelineConfig",
    "WaveFadeShadowPipeline",
    "deterministic_signal_id",
]
