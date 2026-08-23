"""Research-only causal fake-impulse / whipsaw filter.

Does not modify frozen ONDO pump gates. Not wired to live scanners.
"""

from .states import ImpulseState, Side
from .thresholds import FrozenGateThresholds, ResearchExploreParams, FROZEN_DEFAULT
from .frozen_gate import classify_long_frozen, classify_short_frozen, FrozenGateLabel
from .persistence import ImpulseMetrics, compute_impulse_metrics
from .whipsaw import WhipsawDecision, evaluate_whipsaw
from .state_machine import decide_state, DecisionSnapshot

__all__ = [
    "ImpulseState",
    "Side",
    "FrozenGateThresholds",
    "ResearchExploreParams",
    "FROZEN_DEFAULT",
    "classify_long_frozen",
    "classify_short_frozen",
    "FrozenGateLabel",
    "ImpulseMetrics",
    "compute_impulse_metrics",
    "WhipsawDecision",
    "evaluate_whipsaw",
    "decide_state",
    "DecisionSnapshot",
]
