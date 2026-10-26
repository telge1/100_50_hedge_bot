"""Smoke tests for Phase 4J outputs."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "results" / "phase4j_causal_pool_landscape_comparison_v1"


def test_outputs_exist():
    for name in (
        "reference_landscape_profiles.csv",
        "contrast_findings.md",
        "spatial_density_concentrations.csv",
    ):
        assert (OUT / name).is_file()
