"""Prove causal Frozen signals + NO_BE50 full-1m engine against Gold manifest."""

from __future__ import annotations

import json

from .config import (
    CAUSAL_MANIFEST_HASH,
    CONFIRMATION_POLICY,
    EXIT_POLICY,
    OUTCOME_ENGINE_NAME,
    SIGNAL_SOURCE_COMMIT,
    SIGNAL_STRATEGY_VERSION,
    ensure_sg_on_path,
    sg_root,
)

BLOCKED = "CAUSAL_DASHBOARD_IDENTITY_FAIL_CLOSED"


def frozen_outcome_identity() -> dict:
    ensure_sg_on_path()
    root = sg_root().resolve()
    manifest_path = root / "research" / "stoch_fade_causal_runner" / "causal_implementation_manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"{BLOCKED}: missing causal_implementation_manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if str(manifest.get("combined_manifest_hash") or "") != CAUSAL_MANIFEST_HASH:
        raise RuntimeError(f"{BLOCKED}: manifest hash mismatch")
    from signal_generator.pipeline.outcome_eval import evaluate_signal_no_be50, summarize_trade_views
    from signal_generator.pipeline.versions import exit_policy_for, intrabar_policy_for, uses_be50_exit
    from signal_generator.strategy.wave_fade.exits import hold_end_i, scan_exit_sl_first
    from signal_generator.strategy.wave_fade.parameters import CONFIRMATION_CROSS_RECOGNITION

    from .full_1m_scan import evaluate_signal_no_be50_full_1m

    if uses_be50_exit("wave_fade_no_be50_v1"):
        raise RuntimeError(f"{BLOCKED}: NO_BE50 unexpectedly uses BE50")
    if uses_be50_exit(SIGNAL_STRATEGY_VERSION):
        raise RuntimeError(f"{BLOCKED}: causal signal tag unexpectedly uses BE50")
    if exit_policy_for(SIGNAL_STRATEGY_VERSION) != EXIT_POLICY:
        raise RuntimeError(f"{BLOCKED}: exit_policy mismatch")
    if intrabar_policy_for(SIGNAL_STRATEGY_VERSION) != "SL_FIRST":
        raise RuntimeError(f"{BLOCKED}: intrabar mismatch")
    if CONFIRMATION_POLICY != CONFIRMATION_CROSS_RECOGNITION:
        raise RuntimeError(f"{BLOCKED}: confirmation mismatch")
    _ = evaluate_signal_no_be50, summarize_trade_views, scan_exit_sl_first, hold_end_i
    return {
        "signal_strategy_version": SIGNAL_STRATEGY_VERSION,
        "signal_source_commit": SIGNAL_SOURCE_COMMIT,
        "confirmation_policy": CONFIRMATION_POLICY,
        "manifest_hash": CAUSAL_MANIFEST_HASH,
        "runtime_root": str(root),
        "exit_policy": EXIT_POLICY,
        "uses_be50_exit_for_evaluation": False,
        "outcome_engine": OUTCOME_ENGINE_NAME,
        "sg_no_be50_engine_unchanged": evaluate_signal_no_be50.__module__ + "." + evaluate_signal_no_be50.__name__,
        "scan_exit": evaluate_signal_no_be50_full_1m.__module__ + "." + evaluate_signal_no_be50_full_1m.__name__,
        "sg_hold_end_i_still_present": hold_end_i.__module__ + "." + hold_end_i.__name__,
        "max_hold_applied": False,
        "source_commit": SIGNAL_SOURCE_COMMIT,
        "manifest": manifest,
    }
