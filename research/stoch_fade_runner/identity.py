"""Prove Frozen Wave-Fade identity by import + SHA-256 of SG modules. No formula copy."""

from __future__ import annotations

import hashlib
from pathlib import Path

from .config import SOURCE_COMMIT_PIN, STRATEGY_ID, ensure_sg_on_path, sg_root

BLOCKED_BY_FROZEN_STRATEGY_MISMATCH = "BLOCKED_BY_FROZEN_STRATEGY_MISMATCH"
CANDIDATE_LIVE_STRATEGY = "wave_fade_no_be50_v1"
EDGES_VERSION_PIN = "apt_is_q4_frozen_20260808"
SIGNAL_TFS_PIN = ("15m", "30m", "1h", "4h")
BE50_OUTCOME_ACTIVE = False

# SHA-256 of the Signal-Generator files this runner imports (not copies).
FROZEN_SOURCE_SHA256 = {
    "src/signal_generator/pipeline/versions.py": (
        "0919699d7ac702bc13983a9f810afc201898d7e3d565dd83a535f265afd12680"
    ),
    "src/signal_generator/strategy/wave_fade/parameters.py": (
        "0840ab112f4ca9685bab79901a5b378fa0f70340dbf995a8a3644322e082b7cd"
    ),
    "src/signal_generator/strategy/wave_fade/signals.py": (
        "adbc4d940c69a12ee5dc37af6ee2aa6a35ab35aadb36d5815ccc28516fbe286a"
    ),
    "src/signal_generator/strategy/wave_fade/edges.py": (
        "b3ada4d09ad6c5a588ccdbfa9d724f0ca6757e2b83d5dc6256ac6cd84d6317f7"
    ),
    "src/signal_generator/strategy/wave_fade/trend.py": (
        "d1c1941f3b22eb25400230f60cd92d2bc600dda899fa8460b72e3e22f54c9207"
    ),
    "src/signal_generator/pipeline/trade_plan.py": (
        "e8faaf2909e3d2f726ea344c7e535f9e9a8b1b406fdc262f91591d3faa9b5642"
    ),
    "src/signal_generator/pipeline/mapper.py": (
        "6bf812b467f11f4c3b6e51f8d07323cbe5ab9bdf84af79e10a9dabe856171962"
    ),
}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def frozen_identity() -> dict:
    ensure_sg_on_path()
    root = sg_root()
    from signal_generator.pipeline.versions import (
        EDGES_VERSION,
        STRATEGY_VERSION,
        STRATEGY_VERSION_BE50_FROZEN,
        STRATEGY_VERSION_NO_BE50,
        uses_be50_exit,
    )
    from signal_generator.strategy.wave_fade.parameters import SIGNAL_TFS, SOURCE_COMMIT
    from signal_generator.strategy.wave_fade.signals import (
        build_symbol_signals,
        resolve_entries,
    )

    hashes: dict[str, str] = {}
    for rel, expected in FROZEN_SOURCE_SHA256.items():
        path = root / rel
        if not path.is_file():
            raise RuntimeError(f"{BLOCKED_BY_FROZEN_STRATEGY_MISMATCH}: missing {rel}")
        got = _sha256_file(path)
        hashes[rel] = got
        if got != expected:
            raise RuntimeError(
                f"{BLOCKED_BY_FROZEN_STRATEGY_MISMATCH}: hash {rel} {got} != {expected}"
            )

    if not SOURCE_COMMIT.startswith(SOURCE_COMMIT_PIN):
        raise RuntimeError(f"{BLOCKED_BY_FROZEN_STRATEGY_MISMATCH}: SOURCE_COMMIT {SOURCE_COMMIT}")
    if STRATEGY_VERSION_BE50_FROZEN != STRATEGY_ID:
        raise RuntimeError(
            f"{BLOCKED_BY_FROZEN_STRATEGY_MISMATCH}: {STRATEGY_VERSION_BE50_FROZEN}"
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
    if uses_be50_exit(STRATEGY_VERSION_NO_BE50):
        raise RuntimeError(f"{BLOCKED_BY_FROZEN_STRATEGY_MISMATCH}: live BE50 unexpectedly on")
    if not uses_be50_exit(STRATEGY_VERSION_BE50_FROZEN):
        raise RuntimeError(f"{BLOCKED_BY_FROZEN_STRATEGY_MISMATCH}: frozen tag is not BE50-exit id")
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
        "candidate_live_strategy": CANDIDATE_LIVE_STRATEGY,
        "signal_tfs": list(SIGNAL_TFS_PIN),
        "edges_version": EDGES_VERSION_PIN,
        "be50_outcome_active": BE50_OUTCOME_ACTIVE,
        "be50_exit_id_on_frozen_tag": True,
        "generation_shared_with_live": True,
        "source_sha256": hashes,
    }
