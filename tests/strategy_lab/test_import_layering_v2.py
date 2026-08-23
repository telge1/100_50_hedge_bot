"""Import layering and isolation tests for P3.6."""

from __future__ import annotations

import ast
import importlib
import pkgutil
from pathlib import Path

import pytest

STRATEGY_LAB_SRC = (
    Path(__file__).resolve().parents[2] / "src" / "orderbook_analyse" / "strategy_lab"
)
MODELS_ROOT = STRATEGY_LAB_SRC / "models"


def _python_files_under(path: Path) -> list[Path]:
    return sorted(path.rglob("*.py"))


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_models_never_import_catalogs() -> None:
    forbidden_prefix = "orderbook_analyse.strategy_lab.catalogs"
    for path in _python_files_under(MODELS_ROOT):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module in _imported_modules(tree):
            assert not module.startswith(forbidden_prefix), (
                f"{path.relative_to(STRATEGY_LAB_SRC.parent)} imports {module}"
            )


def test_catalogs_v2_imports_only_models() -> None:
    catalogs_v2 = STRATEGY_LAB_SRC / "catalogs" / "v2"
    allowed_prefixes = (
        "orderbook_analyse.strategy_lab.models",
        "orderbook_analyse.strategy_lab.catalogs.v2",
        "__future__",
    )
    for path in _python_files_under(catalogs_v2):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module in _imported_modules(tree):
            if not module.startswith("orderbook_analyse"):
                continue
            if module.startswith("orderbook_analyse.strategy_lab.catalogs.v1"):
                pytest.fail(f"catalog/v2 must not import v1: {path} -> {module}")
            if module.startswith("orderbook_analyse.strategy_lab.catalogs") and (
                not module.startswith("orderbook_analyse.strategy_lab.catalogs.v2")
            ):
                pytest.fail(f"catalog/v2 must not import frozen v1: {path} -> {module}")
            if module.startswith("orderbook_analyse.strategy_lab") and not any(
                module.startswith(prefix) for prefix in allowed_prefixes
            ):
                if module.startswith("orderbook_analyse.strategy_lab"):
                    pytest.fail(f"unexpected import in catalog/v2: {path} -> {module}")


def test_models_package_imports_without_cycles() -> None:
    for module_info in pkgutil.walk_packages(
        [str(MODELS_ROOT)],
        prefix="orderbook_analyse.strategy_lab.models.",
    ):
        importlib.import_module(module_info.name)


def test_contracts_v2_import_isolation() -> None:
    contracts_root = MODELS_ROOT / "contracts_v2"
    forbidden_prefix = "orderbook_analyse.strategy_lab.catalogs"
    for path in _python_files_under(contracts_root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module in _imported_modules(tree):
            assert not module.startswith(forbidden_prefix), path
