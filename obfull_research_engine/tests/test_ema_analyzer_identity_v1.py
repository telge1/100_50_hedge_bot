"""Identity gates: only ema_trend_live_analyzer_v1 is active."""

from __future__ import annotations

import ast
import importlib
import subprocess
import sys
from pathlib import Path

import pytest

_ENGINE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ENGINE_ROOT / "src"))


def test_retired_package_import_fails() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("obfull_research_engine.ema_trend_analyzer_v1")


def test_retired_module_cli_cannot_start() -> None:
    r = subprocess.run(
        [sys.executable, "-m", "obfull_research_engine.ema_trend_analyzer_v1"],
        cwd=str(_ENGINE_ROOT),
        env={**dict(**{k: v for k, v in __import__("os").environ.items()}), "PYTHONPATH": str(_ENGINE_ROOT / "src")},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert r.returncode != 0


def test_active_package_importable() -> None:
    m = importlib.import_module("obfull_research_engine.ema_trend_live_analyzer_v1")
    assert "ema_trend_live_analyzer_v1" in m.__name__


def test_active_callgraph_excludes_retired_name() -> None:
    root = _ENGINE_ROOT / "src" / "obfull_research_engine" / "ema_trend_live_analyzer_v1"
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "ema_trend_analyzer_v1" not in node.module or "live" in node.module
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "ema_trend_analyzer_v1" not in alias.name or "live" in alias.name


def test_run_prefix_contract() -> None:
    from obfull_research_engine import ema_trend_live_analyzer_v1 as pkg

    assert pkg.ALLOWED_RUN_PREFIX == "ema_trend_live_"
    assert pkg.FORBIDDEN_RUN_PREFIX == "ema_trend_analyzer_"
    assert pkg.RETIRED_PACKAGE == "ema_trend_analyzer_v1"
    assert pkg.PACKAGE_NAME == "ema_trend_live_analyzer_v1"


def test_repo_retired_name_only_in_allowed_places() -> None:
    allowed_markers = (
        "FORENSIC",
        "RETIRED",
        "INVALID",
        "test_",
        "ACTIVE_EMA_ANALYZER",
        "prevent",
        "forbidden",
        "retirement",
    )
    hits = []
    for path in (_ENGINE_ROOT / "src").rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in {".py", ".md", ".txt", ".json"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "ema_trend_analyzer_v1" not in text:
            continue
        rel = str(path.relative_to(_ENGINE_ROOT))
        if "ema_trend_live_analyzer_v1" in rel:
            # live package may mention retired name in docs/tests/guards
            continue
        if any(m.lower() in rel.lower() or m.lower() in text[:200].lower() for m in allowed_markers):
            continue
        hits.append(rel)
    assert hits == []
