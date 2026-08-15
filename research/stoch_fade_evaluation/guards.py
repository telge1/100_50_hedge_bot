"""Refuse CH writers and BE50 as the evaluation path."""

from __future__ import annotations

from pathlib import Path

_DIR = Path(__file__).resolve().parent


def package_source() -> str:
    parts: list[str] = []
    for path in sorted(_DIR.glob("*.py")):
        if path.name == "guards.py":
            continue
        parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def assert_no_writers_or_be50_eval_path() -> None:
    text = package_source()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("from signal_generator.db.signals import"):
            raise RuntimeError("must not import Signal db")
        if line.startswith("from signal_generator.db.outcomes import"):
            raise RuntimeError("must not import outcomes db")
        if line.startswith("from signal_generator.db.processing_state import"):
            raise RuntimeError("must not import processing_state")
        if "OutcomeEvaluator(" in line and not line.startswith("#"):
            raise RuntimeError("must not construct OutcomeEvaluator")
        if line.startswith("from ") and "evaluate_signal_be50" in line:
            raise RuntimeError("must not import evaluate_signal_be50")
        if "simulate_be50_trade" in line and not line.startswith("#"):
            raise RuntimeError("must not import simulate_be50_trade")
