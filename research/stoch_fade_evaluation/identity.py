"""Prove Frozen signals + existing NO_BE50 engine by import + SHA-256. No formula copy."""

from __future__ import annotations

import hashlib
from pathlib import Path

from .config import (
    ENGINE_SHA256,
    EXIT_POLICY,
    OUTCOME_ENGINE_NAME,
    SIGNAL_SOURCE_COMMIT,
    SIGNAL_STRATEGY_VERSION,
    ensure_sg_on_path,
    sg_root,
)

BLOCKED = "BLOCKED_BY_FROZEN_OUTCOME_ENGINE_ISOLATION"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def frozen_outcome_identity() -> dict:
    ensure_sg_on_path()
    root = sg_root()
    from signal_generator.pipeline.outcome_eval import evaluate_signal_no_be50, summarize_trade_views
    from signal_generator.pipeline.versions import uses_be50_exit
    from signal_generator.strategy.wave_fade.exits import scan_exit_sl_first

    hashes: dict[str, str] = {}
    for rel, expected in ENGINE_SHA256.items():
        path = root / rel
        if not path.is_file():
            raise RuntimeError(f"{BLOCKED}: missing {rel}")
        got = _sha256_file(path)
        hashes[rel] = got
        if got != expected:
            raise RuntimeError(f"{BLOCKED}: hash {rel}")
    if uses_be50_exit("wave_fade_no_be50_v1"):
        raise RuntimeError(f"{BLOCKED}: NO_BE50 unexpectedly uses BE50")
    if not uses_be50_exit(SIGNAL_STRATEGY_VERSION):
        raise RuntimeError(
            f"{BLOCKED}: signal tag {SIGNAL_STRATEGY_VERSION} is not the Frozen signal identity"
        )
    _ = evaluate_signal_no_be50, summarize_trade_views, scan_exit_sl_first
    return {
        "signal_strategy_version": SIGNAL_STRATEGY_VERSION,
        "signal_source_commit": SIGNAL_SOURCE_COMMIT,
        "exit_policy": EXIT_POLICY,
        "uses_be50_exit_for_evaluation": False,
        "outcome_engine": OUTCOME_ENGINE_NAME,
        "scan_exit": scan_exit_sl_first.__module__ + "." + scan_exit_sl_first.__name__,
        "hashes": hashes,
        "source_commit": SIGNAL_SOURCE_COMMIT,
    }
