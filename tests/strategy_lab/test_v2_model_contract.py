"""AST / contract tests for StrategySpec V2 model modules."""

from __future__ import annotations

import ast
import inspect
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import get_args, get_origin

import pytest

from orderbook_analyse.strategy_lab.models import StrategySpec, StrategySpecV2

V2_MODEL_MODULES = (
    "orderbook_analyse.strategy_lab.models.identifiers",
    "orderbook_analyse.strategy_lab.models.features",
    "orderbook_analyse.strategy_lab.models.rules",
    "orderbook_analyse.strategy_lab.models.state_machine",
    "orderbook_analyse.strategy_lab.models.signals",
    "orderbook_analyse.strategy_lab.models.strategy_v2",
)

STRATEGY_LAB_SRC = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "orderbook_analyse"
    / "strategy_lab"
)


def _v2_dataclasses() -> list[type]:
    classes: list[type] = []
    for mod_name in V2_MODEL_MODULES:
        mod = __import__(mod_name, fromlist=["*"])
        for _name, obj in inspect.getmembers(mod, inspect.isclass):
            if obj.__module__ != mod_name:
                continue
            if is_dataclass(obj):
                classes.append(obj)
    return classes


def test_v2_dataclasses_frozen_slots_kwonly() -> None:
    for cls in _v2_dataclasses():
        params = getattr(cls, "__dataclass_params__")
        assert params.frozen is True, cls
        assert getattr(cls, "__slots__", None) is not None, cls
        for field in fields(cls):
            assert field.kw_only is True, (cls, field.name)


def test_v2_no_dict_or_any_or_callable_fields() -> None:
    forbidden_names = {"dict", "Dict", "Any", "Callable"}
    for cls in _v2_dataclasses():
        for field in fields(cls):
            ann = field.type
            if isinstance(ann, str):
                assert ann not in forbidden_names, (cls, field.name)
                continue
            origin = get_origin(ann)
            if origin is dict:
                pytest.fail(f"{cls.__name__}.{field.name} uses dict")
            assert ann.__name__ not in forbidden_names if hasattr(ann, "__name__") else True


def test_v2_root_separate_from_v1() -> None:
    v1_names = {f.name for f in fields(StrategySpec)}
    v2_names = {f.name for f in fields(StrategySpecV2)}
    assert "setup" in v1_names and "setup" not in v2_names
    assert "long" in v1_names and "long" not in v2_names
    assert "signal" in v2_names
    assert "features" in v2_names


def test_v2_modules_no_legacy_imports() -> None:
    forbidden = (
        "orderbook_analyse.ema_dual_cross_multisource",
        "orderbook_analyse.cluster_sweep_research",
    )
    for mod_name in V2_MODEL_MODULES:
        rel = mod_name.removeprefix("orderbook_analyse.strategy_lab.")
        path = STRATEGY_LAB_SRC / Path(*rel.split(".")).with_suffix(".py")
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for root in forbidden:
                        assert not alias.name.startswith(root), path
            elif isinstance(node, ast.ImportFrom) and node.module:
                for root in forbidden:
                    assert not node.module.startswith(root), path
