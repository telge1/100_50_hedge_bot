"""Closed enumerations for StrategySpec V1 (normative contract)."""

from __future__ import annotations

from enum import Enum


class ModelingStatus(str, Enum):
    """Explicit modeling status — never imply neutral for missing domains."""

    MODELED = "modeled"
    NOT_MODELED = "not_modeled"
    NOT_APPLICABLE = "not_applicable"
    UNAVAILABLE = "unavailable"


class DataRequirementStatus(str, Enum):
    REQUIRED = "required"
    OPTIONAL = "optional"
    REQUIRED_FOR_GROUP = "required_for_group"
    UNAVAILABLE = "unavailable"
    NOT_APPLICABLE = "not_applicable"


class CausalityStatus(str, Enum):
    CAUSAL_PROVEN = "causal_proven"
    CAUSAL_REUSABLE_WHEN_DEPENDENCY_AVAILABLE = (
        "causal_reusable_when_dependency_available"
    )
    CAUSALITY_UNPROVEN = "causality_unproven"
    NOT_APPLICABLE = "not_applicable"


class PluginKind(str, Enum):
    SIGNAL = "signal"
    GATE = "gate"
    ENTRY = "entry"
    EXIT = "exit"
    FEATURE = "feature"
    OTHER = "other"


class Directionality(str, Enum):
    LONG = "long"
    SHORT = "short"
    BOTH = "both"


class SideName(str, Enum):
    LONG = "long"
    SHORT = "short"


class MirrorMode(str, Enum):
    NONE = "none"
    FULL_MIRROR = "full_mirror"
    SIGN_FLIP = "sign_flip"


class RateUnit(str, Enum):
    """Explicit rate unit — no inference between percent / fraction / bps."""

    PERCENT = "percent"  # 0.75 means 0.75 percent
    FRACTION = "fraction"  # 0.0075 means 0.75 percent
    BASIS_POINTS = "basis_points"  # 15 means 0.15 percent


class DurationUnit(str, Enum):
    MINUTES = "minutes"
    HOURS = "hours"
    BARS = "bars"


class TimeframeUnit(str, Enum):
    """Bar period unit for signal/execution timeframes (not holding duration)."""

    MINUTES = "minutes"


class SameBarPriority(str, Enum):
    SL_FIRST = "sl_first"
    TP_FIRST = "tp_first"


class ExitMode(str, Enum):
    """Parametric TP/SL/horizon or fully plugin-described exit."""

    PARAMETRIC = "parametric"
    PLUGIN = "plugin"
