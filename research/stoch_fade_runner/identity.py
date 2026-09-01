"""Prove canonical causal Frozen identity against the Gold runtime root."""

from __future__ import annotations

import json

from .config import (
    CAUSAL_MANIFEST_HASH,
    CONFIRMATION_POLICY,
    EXIT_POLICY,
    INTRABAR_POLICY,
    OUTCOME_ENGINE,
    SOURCE_COMMIT_PIN,
    STRATEGY_ID,
    ensure_sg_on_path,
    sg_root,
)

BLOCKED_BY_FROZEN_STRATEGY_MISMATCH = "CAUSAL_DASHBOARD_IDENTITY_FAIL_CLOSED"
CANDIDATE_LIVE_STRATEGY = "wave_fade_no_be50_v1"
EDGES_VERSION_PIN = "apt_is_q4_frozen_20260808"
SIGNAL_TFS_PIN = ("15m", "30m", "1h", "4h")
BE50_OUTCOME_ACTIVE = False


def frozen_identity() -> dict:
    ensure_sg_on_path()
    root = sg_root().resolve()
    manifest_path = root / "research" / "stoch_fade_causal_runner" / "causal_implementation_manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"{BLOCKED_BY_FROZEN_STRATEGY_MISMATCH}: missing causal_implementation_manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if str(manifest.get("combined_manifest_hash") or "") != CAUSAL_MANIFEST_HASH:
        raise RuntimeError(
            f"{BLOCKED_BY_FROZEN_STRATEGY_MISMATCH}: manifest {manifest.get('combined_manifest_hash')} != {CAUSAL_MANIFEST_HASH}"
        )
    from signal_generator.pipeline.versions import (
        EDGES_VERSION,
        STRATEGY_VERSION,
        STRATEGY_VERSION_BE50_FROZEN,
        STRATEGY_VERSION_FROZEN_CAUSAL_ENTRY_V1,
        STRATEGY_VERSION_NO_BE50,
        exit_policy_for,
        intrabar_policy_for,
        uses_be50_exit,
    )
    from signal_generator.strategy.wave_fade.parameters import (
        CONFIRMATION_CROSS_RECOGNITION,
        SIGNAL_TFS,
        SOURCE_COMMIT,
    )
    from signal_generator.strategy.wave_fade.signals import (
        build_symbol_signals,
        resolve_entries,
    )

    if not SOURCE_COMMIT.startswith(SOURCE_COMMIT_PIN):
        raise RuntimeError(f"{BLOCKED_BY_FROZEN_STRATEGY_MISMATCH}: SOURCE_COMMIT {SOURCE_COMMIT}")
    if STRATEGY_VERSION_FROZEN_CAUSAL_ENTRY_V1 != STRATEGY_ID:
        raise RuntimeError(
            f"{BLOCKED_BY_FROZEN_STRATEGY_MISMATCH}: {STRATEGY_VERSION_FROZEN_CAUSAL_ENTRY_V1}"
        )
    if STRATEGY_VERSION_NO_BE50 != CANDIDATE_LIVE_STRATEGY:
        raise RuntimeError(
            f"{BLOCKED_BY_FROZEN_STRATEGY_MISMATCH}: live {STRATEGY_VERSION_NO_BE50}"
        )
    if STRATEGY_VERSION != CANDIDATE_LIVE_STRATEGY:
        raise RuntimeError(f"{BLOCKED_BY_FROZEN_STRATEGY_MISMATCH}: default {STRATEGY_VERSION}")
    if tuple(SIGNAL_TFS) != SIGNAL_TFS_PIN:
        raise RuntimeError(f"{BLOCKED_BY_FROZEN_STRATEGY_MISMATCH}: TFs {SIGNAL_TFS}")
    if EDGES_VERSION != EDGES_VERSION_PIN:
        raise RuntimeError(f"{BLOCKED_BY_FROZEN_STRATEGY_MISMATCH}: edges {EDGES_VERSION}")
    if CONFIRMATION_POLICY != CONFIRMATION_CROSS_RECOGNITION:
        raise RuntimeError(f"{BLOCKED_BY_FROZEN_STRATEGY_MISMATCH}: confirmation_policy")
    if uses_be50_exit(STRATEGY_VERSION_NO_BE50):
        raise RuntimeError(f"{BLOCKED_BY_FROZEN_STRATEGY_MISMATCH}: live BE50 unexpectedly on")
    if uses_be50_exit(STRATEGY_ID):
        raise RuntimeError(f"{BLOCKED_BY_FROZEN_STRATEGY_MISMATCH}: causal BE50 unexpectedly on")
    if not uses_be50_exit(STRATEGY_VERSION_BE50_FROZEN):
        raise RuntimeError(f"{BLOCKED_BY_FROZEN_STRATEGY_MISMATCH}: frozen tag is not BE50-exit id")
    if exit_policy_for(STRATEGY_ID) != EXIT_POLICY:
        raise RuntimeError(f"{BLOCKED_BY_FROZEN_STRATEGY_MISMATCH}: exit_policy")
    if intrabar_policy_for(STRATEGY_ID) != INTRABAR_POLICY:
        raise RuntimeError(f"{BLOCKED_BY_FROZEN_STRATEGY_MISMATCH}: intrabar_policy")
    if BE50_OUTCOME_ACTIVE:
        raise RuntimeError(f"{BLOCKED_BY_FROZEN_STRATEGY_MISMATCH}: runner BE50 outcome must be off")
    if build_symbol_signals.__module__ != "signal_generator.strategy.wave_fade.signals":
        raise RuntimeError(f"{BLOCKED_BY_FROZEN_STRATEGY_MISMATCH}: signals import")
    if resolve_entries.__module__ != "signal_generator.strategy.wave_fade.signals":
        raise RuntimeError(f"{BLOCKED_BY_FROZEN_STRATEGY_MISMATCH}: entry import")

    return {
        "strategy_id": STRATEGY_ID,
        "source_commit": SOURCE_COMMIT,
        "source_commit_pin": SOURCE_COMMIT_PIN,
        "runtime_root": str(root),
        "manifest_hash": CAUSAL_MANIFEST_HASH,
        "candidate_live_strategy": CANDIDATE_LIVE_STRATEGY,
        "signal_tfs": list(SIGNAL_TFS_PIN),
        "edges_version": EDGES_VERSION_PIN,
        "be50_outcome_active": BE50_OUTCOME_ACTIVE,
        "be50_exit_id_on_frozen_tag": True,
        "generation_shared_with_live": True,
        "confirmation_policy": CONFIRMATION_POLICY,
        "exit_policy": EXIT_POLICY,
        "intrabar_policy": INTRABAR_POLICY,
        "outcome_engine": OUTCOME_ENGINE,
        "manifest": manifest,
    }
