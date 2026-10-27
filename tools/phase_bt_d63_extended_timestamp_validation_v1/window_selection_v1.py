"""Deterministic pilot windows (not outcome-selected)."""

from __future__ import annotations

from typing import Any

# Fixed pilot — spread Aug/Sep 2026; development windows flagged separately.
PILOT_WINDOWS: list[dict[str, Any]] = [
    {
        "window_id": "aug_2026_08_12_mid",
        "start_utc": "2026-08-12T12:00:00Z",
        "end_utc": "2026-08-12T18:00:00Z",
        "market_hypothesis": "mid_aug_consolidation",
        "evidence_class": "generalization_pilot",
        "density_mode": "canonical_provider",
        "bar_stride": 6,
    },
    {
        "window_id": "aug_2026_08_26_mid",
        "start_utc": "2026-08-26T12:00:00Z",
        "end_utc": "2026-08-26T18:00:00Z",
        "market_hypothesis": "late_aug_structure",
        "evidence_class": "generalization_pilot",
        "density_mode": "canonical_provider",
        "bar_stride": 6,
    },
    {
        "window_id": "sep_2026_09_04_transition",
        "start_utc": "2026-09-04T12:00:00Z",
        "end_utc": "2026-09-04T18:00:00Z",
        "market_hypothesis": "sideways_transition",
        "evidence_class": "generalization_pilot",
        "density_mode": "canonical_provider",
        "bar_stride": 6,
    },
    {
        "window_id": "sep_2026_09_06_resume",
        "start_utc": "2026-09-06T12:00:00Z",
        "end_utc": "2026-09-06T18:00:00Z",
        "market_hypothesis": "post_drop_resume_probe",
        "evidence_class": "generalization_pilot",
        "density_mode": "canonical_provider",
        "bar_stride": 6,
    },
    {
        "window_id": "sep_dev_sep9_evening",
        "start_utc": "2026-09-09T16:00:00Z",
        "end_utc": "2026-09-09T22:00:00Z",
        "market_hypothesis": "development_downtrend_reference",
        "evidence_class": "development_sep9",
        "density_mode": "d54_artifact",
        "bar_stride": 1,
    },
    {
        "window_id": "sep_dev_sep11_dense_am",
        "start_utc": "2026-09-11T11:00:00Z",
        "end_utc": "2026-09-11T14:00:00Z",
        "market_hypothesis": "development_dense_upper",
        "evidence_class": "development_sep11",
        "density_mode": "validated_cache",
        "bar_stride": 1,
    },
    {
        "window_id": "sep_control_sep11_pm",
        "start_utc": "2026-09-11T15:30:00Z",
        "end_utc": "2026-09-11T17:00:00Z",
        "market_hypothesis": "chronological_control",
        "evidence_class": "chronological_control",
        "density_mode": "validated_cache",
        "bar_stride": 1,
    },
]

DEVELOPMENT_EVIDENCE = frozenset({"development_sep9", "development_sep11"})
