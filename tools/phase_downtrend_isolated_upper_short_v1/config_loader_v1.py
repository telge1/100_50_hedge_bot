"""Load isolated strategy YAML + validated BT-D6 baseline JSON."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
BT_D6 = ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1"
if str(BT_D6) not in sys.path:
    sys.path.insert(0, str(BT_D6))

from config_validate_v1 import validate_config_dict  # noqa: E402
from config_yaml_v1 import yaml_document_to_config_dict  # noqa: E402

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = PACKAGE_DIR / "strategy_config_v1.yaml"


def load_raw_strategy_yaml(path: Path | None = None) -> dict[str, Any]:
    p = path or DEFAULT_CONFIG
    text = p.read_text(encoding="utf-8")
    return yaml.safe_load(text)


def split_isolated_and_baseline(raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    isolated = raw.get("isolated_strategy_v1") or {}
    baseline_doc = {k: v for k, v in raw.items() if k != "isolated_strategy_v1"}
    return isolated, baseline_doc


def load_validated_baseline_config(path: Path | None = None) -> dict[str, Any]:
    raw = load_raw_strategy_yaml(path)
    _, baseline_doc = split_isolated_and_baseline(raw)
    cfg = yaml_document_to_config_dict(baseline_doc)
    validate_config_dict(cfg)
    return cfg


def load_isolated_strategy_bundle(path: Path | None = None) -> dict[str, Any]:
    raw = load_raw_strategy_yaml(path)
    isolated, baseline_doc = split_isolated_and_baseline(raw)
    cfg = yaml_document_to_config_dict(baseline_doc)
    validate_config_dict(cfg)
    return {
        "isolated_strategy_v1": isolated,
        "baseline_config": cfg,
        "baseline_yaml_path": str(path or DEFAULT_CONFIG),
    }
