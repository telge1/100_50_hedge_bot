from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from config_enforcement_v1 import ResearchBacktesterConfigV1, ResearchConfigError

BASE = json.loads(
    (Path(__file__).resolve().parents[1] / "research_config_v1.json").read_text(encoding="utf-8")
)


def _write_cfg(tmp: Path, overrides: dict) -> Path:
    data = {**BASE, **overrides}
    p = tmp / "cfg.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_valid_config_loads():
    cfg = ResearchBacktesterConfigV1.load()
    assert cfg.min_unique_origins == 2
    assert cfg.density_method == "exact_unique_origin"


def test_depth_one_fails(tmp_path):
    with pytest.raises(ResearchConfigError) as ei:
        ResearchBacktesterConfigV1.load(_write_cfg(tmp_path, {"min_unique_origins": 1}))
    assert ei.value.code == "MIN_UNIQUE_ORIGINS_MISMATCH"


def test_depth_three_fails(tmp_path):
    with pytest.raises(ResearchConfigError) as ei:
        ResearchBacktesterConfigV1.load(_write_cfg(tmp_path, {"min_unique_origins": 3}))
    assert ei.value.code == "MIN_UNIQUE_ORIGINS_MISMATCH"


def test_missing_contract_key_fails(tmp_path):
    bad = {k: v for k, v in BASE.items() if k != "density_method"}
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ResearchConfigError) as ei:
        ResearchBacktesterConfigV1.load(p)
    assert ei.value.code == "CONFIG_INCOMPLETE"
