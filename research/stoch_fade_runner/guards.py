"""Refuse production writers. Phase 2A never imports ClickHouse signal/state repos."""

from __future__ import annotations

from pathlib import Path

from .config import FORBIDDEN_CLI_TOKENS, PHASE

_RUNNER_DIR = Path(__file__).resolve().parent

FORBIDDEN_IMPORTS = (
    "run_wave_fade_shadow_pipeline",
    "WaveFadeShadowPipeline",
    "SignalRepository",
    "ProcessingStateRepository",
    "setup_clickhouse",
    "delete_by_generator_version",
    "delete_for_strategy",
)


def runner_source_text() -> str:
    parts: list[str] = []
    for path in sorted(_RUNNER_DIR.glob("*.py")):
        if path.name.startswith("test_"):
            continue
        parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def assert_runner_has_no_production_writers() -> None:
    text = runner_source_text()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("from signal_generator.db.signals import"):
            raise RuntimeError(f"phase {PHASE} runner must not import Signal db")
        if line.startswith("from signal_generator.db.processing_state import"):
            raise RuntimeError(f"phase {PHASE} runner must not import processing_state")
        if line.startswith("from signal_generator.pipeline.processor import"):
            raise RuntimeError(f"phase {PHASE} runner must not import pipeline processor")
        if line.startswith("import ") and "SignalRepository" in line:
            raise RuntimeError(f"phase {PHASE} runner must not import SignalRepository")
    for token in FORBIDDEN_IMPORTS:
        if token in text and token != "FORBIDDEN_IMPORTS":
            if f'"{token}"' in text or f"'{token}'" in text:
                continue
            raise RuntimeError(f"phase {PHASE} runner must not reference {token}")


def reject_forbidden_argv(argv: list[str]) -> str | None:
    joined = " ".join(argv)
    for token in FORBIDDEN_CLI_TOKENS:
        if token in joined:
            return f"FORBIDDEN_ARG:{token}"
    if any(a == "--cleanup-first" or a.startswith("--cleanup") for a in argv):
        return "FORBIDDEN_ARG:cleanup"
    return None
