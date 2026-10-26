from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from config_validate_v1 import ConfigValidationError, validate_config_dict

BASELINE = Path(__file__).resolve().parents[1] / "configs/pool_landscape_baseline_v1.json"


def _base() -> dict:
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def test_validated_cache_requires_bundle_path():
    c = _base()
    c["density"]["density_source"] = "validated_cache"
    with pytest.raises(ConfigValidationError) as exc:
        validate_config_dict(c)
    assert exc.value.code == "VALIDATED_CACHE_MISSING"


def test_validated_cache_with_path_ok(tmp_path):
    c = _base()
    c["density"]["density_source"] = "validated_cache"
    c["density"]["validated_cache"] = {"bundle_path": str(tmp_path)}
    validate_config_dict(c)
