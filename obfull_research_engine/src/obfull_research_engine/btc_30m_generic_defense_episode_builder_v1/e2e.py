"""CLI entry for btc_30m_generic_defense_episode_builder_v1."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..bounded_level_first_analyzer_pilot_v1.persist import atomic_write_json
from . import AUDIT_ID, CONTRACT_HASH, WORKTREE_ROOT
from .config import BuilderConfig, load_config_json
from .pipeline import run_builder


def assert_worktree_imports(modules: list[str] | None = None) -> list[str]:
    """Imported research modules must resolve under WORKTREE_ROOT (not dirty checkout)."""
    import importlib

    names = modules or [
        "obfull_research_engine",
        "obfull_research_engine.btc_30m_generic_defense_episode_builder_v1",
        "obfull_research_engine.btc_30m_generic_defense_episode_builder_v1.pipeline",
        "obfull_research_engine.btc_30m_generic_defense_episode_builder_v1.config",
        "obfull_research_engine.btc_30m_generic_defense_episode_builder_v1.zones",
        "obfull_research_engine.btc_30m_generic_defense_episode_builder_v1.visits",
        "obfull_research_engine.btc_30m_generic_defense_episode_builder_v1.attack_clusters",
        "obfull_research_engine.btc_30m_generic_defense_episode_builder_v1.walls_past_only",
        "obfull_research_engine.btc_30m_generic_defense_episode_builder_v1.outcomes_apply",
        "obfull_research_engine.wall_defense_outcome_contract_v1",
        "obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1",
        "obfull_research_engine.level_first_episode1_price_response_reclaim_v1",
        "obfull_research_engine.level_first_episode1_defense_chain_v1",
        "obfull_research_engine.level_first_episode1_touch_detection_independent_v1",
        "obfull_research_engine.level_first_episode1_detection_to_wall_flow_integration_v1",
        "obfull_research_engine.bounded_level_first_analyzer_pilot_v1.episodes",
    ]
    bad: list[str] = []
    prefix = str(WORKTREE_ROOT)
    for name in names:
        try:
            m = importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{name} IMPORT_ERROR: {exc}")
            continue
        f = getattr(m, "__file__", None)
        if not f or not str(Path(f).resolve()).startswith(prefix):
            bad.append(f"{name} -> {f}")
    return bad


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=AUDIT_ID)
    p.add_argument(
        "--pilot",
        action="store_true",
        help="Pilot mode: apply max_pilot_clusters (default 20) unless --max-clusters overrides",
    )
    p.add_argument(
        "--max-clusters",
        type=int,
        default=None,
        help="Explicit enrichment ceiling for pilot or full mode (None = mode default)",
    )
    p.add_argument("--out-dir", type=str, default=None)
    p.add_argument("--run-key", type=str, default=None)
    p.add_argument("--config", type=str, default=None)
    p.add_argument("--discovery-only", action="store_true")
    p.add_argument("--check-imports", action="store_true")
    args = p.parse_args(argv)

    if args.check_imports:
        bad = assert_worktree_imports()
        if bad:
            print("IMPORT_PATH_VIOLATIONS:")
            for b in bad[:50]:
                print(" ", b)
            return 2
        print(f"ok: all loaded modules under {WORKTREE_ROOT}")
        print(f"contract_hash={CONTRACT_HASH}")
        return 0

    if args.config:
        cfg = load_config_json(args.config)
    else:
        cfg = BuilderConfig()
    if args.pilot:
        cfg.pilot = True
    if args.max_clusters is not None:
        # Explicit limit for either mode — does NOT silently mutate max_pilot_clusters.
        cfg.max_clusters = int(args.max_clusters)
    if args.out_dir:
        cfg.out_root = str(Path(args.out_dir))
    if args.run_key:
        cfg.run_key = args.run_key

    result = run_builder(cfg, dry_discovery_only=bool(args.discovery_only))
    out = Path(result["out_dir"])
    atomic_write_json(out / "e2e_result.json", result)
    print(
        f"ok={result.get('ok')} run_mode={result.get('run_mode')} "
        f"limit={result.get('effective_cluster_limit')} "
        f"run_key={result.get('run_key')} out={result.get('out_dir')}"
    )
    print(f"contract_hash={result.get('outcome_contract_hash') or CONTRACT_HASH}")
    if result.get("input_error"):
        print(f"input_error={result['input_error']}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
