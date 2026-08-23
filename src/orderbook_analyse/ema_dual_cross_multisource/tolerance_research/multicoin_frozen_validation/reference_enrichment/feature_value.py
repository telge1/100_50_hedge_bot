"""Feature value container: null + coverage, never fake 0/NEUTRAL."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any


@dataclass
class FeatureValue:
    name: str
    value: float | int | str | bool | None
    feature_asof: str | None
    window_start: str | None
    window_end: str | None
    coverage_status: str  # OK | MISSING | STALE | INSUFFICIENT | NOT_AVAILABLE | CAUSALITY_UNPROVEN
    missing_reason: str | None
    source_table: str | None
    causal: bool

    def to_export(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "feature_asof": self.feature_asof,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "coverage_status": self.coverage_status,
            "missing_reason": self.missing_reason,
            "source_table": self.source_table,
            "causal": self.causal,
        }


def ok(
    name: str,
    value: Any,
    *,
    asof: datetime | str,
    window_start: datetime | str | None,
    window_end: datetime | str | None,
    source: str,
) -> FeatureValue:
    return FeatureValue(
        name=name,
        value=value,
        feature_asof=_iso(asof),
        window_start=_iso(window_start) if window_start is not None else None,
        window_end=_iso(window_end) if window_end is not None else None,
        coverage_status="OK",
        missing_reason=None,
        source_table=source,
        causal=True,
    )


def missing(
    name: str,
    *,
    reason: str,
    status: str = "MISSING",
    source: str | None = None,
    asof: datetime | str | None = None,
    window_start: datetime | str | None = None,
    window_end: datetime | str | None = None,
    causal: bool = True,
) -> FeatureValue:
    return FeatureValue(
        name=name,
        value=None,
        feature_asof=_iso(asof) if asof is not None else None,
        window_start=_iso(window_start) if window_start is not None else None,
        window_end=_iso(window_end) if window_end is not None else None,
        coverage_status=status,
        missing_reason=reason,
        source_table=source,
        causal=causal,
    )


def _iso(dt: datetime | str | None) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    return dt.isoformat()


def flatten_features(feats: dict[str, FeatureValue], *, prefix: str = "feature__") -> dict[str, Any]:
    """Export value columns + parallel meta columns."""
    out: dict[str, Any] = {}
    for name, fv in feats.items():
        key = name if name.startswith(prefix) else f"{prefix}{name}"
        out[key] = fv.value
        out[f"{key}__coverage_status"] = fv.coverage_status
        out[f"{key}__missing_reason"] = fv.missing_reason
        out[f"{key}__causal"] = fv.causal
        out[f"{key}__feature_asof"] = fv.feature_asof
        out[f"{key}__source_table"] = fv.source_table
    return out
