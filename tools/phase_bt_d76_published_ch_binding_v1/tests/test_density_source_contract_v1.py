"""Contract tests for published_clickhouse density_source."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ISOLATED = ROOT / "tools/phase_downtrend_isolated_upper_short_v1"
for p in (
    ISOLATED,
    ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1",
    ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1",
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from config_loader_v1 import load_validated_baseline_config  # noqa: E402
from config_validate_v1 import ConfigValidationError, validate_config_dict  # noqa: E402
from density_bridge_v1 import create_density_session  # noqa: E402


def test_published_ch_requires_research_mode() -> None:
    cfg = copy.deepcopy(load_validated_baseline_config())
    cfg["density"]["density_source"] = "published_clickhouse"
    cfg["density"]["published_clickhouse"] = {}
    cfg["strategy_identity"]["research_mode"] = "config_experiment_v1"
    try:
        validate_config_dict(cfg)
        raise AssertionError("expected ConfigValidationError")
    except ConfigValidationError as exc:
        assert exc.code == "PUBLISHED_CH_RESEARCH_ONLY"


def test_canonical_default_unchanged() -> None:
    cfg = load_validated_baseline_config()
    assert cfg["density"]["density_source"] == "canonical_provider"
    sess = create_density_session(cfg)
    assert sess.__class__.__name__ == "ResearchDensitySessionV1"
